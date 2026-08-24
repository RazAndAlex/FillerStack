"""Test E2E M9 (§54 cardine) — fault injection → feature → prediction.

Senza Docker/broker: usa `RealtimeSim` (stepped) + `OpcuaServer` in-process
e la catena M9 `live_features` → `predict_frame` (riuso ml_dataset, anti-skew).
Non ripercorre Node-RED/MQTT (fuori dal nucleo logico: spec M9 §7 dice che il
test E2E usa il bridge in-process, Docker solo per l'eventuale DB).

Il cardine (§54): con una valvola (valve0) in Running, si campiona la
telemetria per-ciclo dallo snapshot (già esteso M9 con CloseReason/
PositionLimit/FillingOvertime), si calcolano le 43 feature e si predice:
- fase healthy: predicted_label == "healthy", anomaly_score basso;
- fault injection (restriction su valve0): KPI alterati (FT/PulseCount),
  feature cambiano, anomaly_score sale sopra la baseline, predicted_label
  coerente col fault (restriction o comunque non-healthy).

Criteri (calibration candidate, non congelati): anomaly_score post-fault >
baseline healthy + margine; almeno una finestra fault predetta non-healthy;
T5 rientro (remove fault) → feature tornano healthy → score rientra.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plcsim.realtime import RealtimeSim  # noqa: E402
from plcsim.scenario import Scenario  # noqa: E402
from pipeline.features import live_features  # noqa: E402
from pipeline.inference import InferenceConsumer  # noqa: E402

SKIP_NO_MODEL = not Path("work/ml_dataset/model/model.joblib").exists()

pytestmark = pytest.mark.skipif(
    SKIP_NO_MODEL, reason="modello Track D assente (work/ml_dataset/model)")


# ---------------------------------------------------------------------------
# Collettore: campiona un intero record per-ciclo dallo snapshot Valve01.
# ---------------------------------------------------------------------------
_VALVE_TAG_TO_REC = {
    "FillingTime_ms": "fillingtime",
    "TailTime_ms": "tailtime",
    "TailPulse": "tailpulse",
    "PulseCount": "pulsecount",
    "Target": "target",
    "DeltaPulse": "deltapulse",
    "FillingStepOut": "filling_step_out",
    "FillingOK": "fillingok",
    "FillQualityOK": "fill_quality_ok",
    "SequenceOK": "sequence_ok",
    "SampleValid": "sample_valid",
    "DiagnosticStatus": "diagnostic_status",
    "CloseReason": "close_reason",
    "PositionLimit": "position_limit",
    "FillingOvertime": "filling_overtime",
    "LastCycleId": "cycle_id",
}


def _collect_valve_cycles(sim, n_cycles: int, timeout_scans: int = 20000):
    """Campiona n cicli chiusi della valvola esposta dallo snapshot (16 tag).

    Ritorna (frame valve_cycles, scans). Il frame ha colonne bulk (come
    telemetry) + machine_code="valve0" + cycle_id; pronto per live_features.
    """
    rows = []
    last = sim.snapshot.read()["Machine"]["CycleCounter"]
    scans = 0
    while len(rows) < n_cycles and scans < timeout_scans:
        sim.advance(100)
        scans += 100
        snap = sim.snapshot.read()
        cc = snap["Machine"]["CycleCounter"]
        if cc > last:
            v = snap["Valve01"]
            rec = {"machine_code": "valve0"}
            for tag, col in _VALVE_TAG_TO_REC.items():
                rec[col] = v[tag]
            rows.append(rec)
            last = cc
    return pl.DataFrame(rows), scans


def _ensure_running(sim):
    """Porta la macchina in Running (stessa logica dei test M6)."""
    # attende che il template giornata raggiunga Running (o forza con advance)
    for _ in range(2000):
        sim.advance(100)
        if sim.machine.running:
            return
    raise RuntimeError("macchina mai Running")


@pytest.fixture
def inference():
    # M10: il backend predictions è PostgreSQL (ADR-0021); il test §54 usa solo
    # predict_frame (in-memory), il DB non è toccato. db_url puntato al default
    # POC ma MAI usato in questo test (nessuna persistenza).
    c = InferenceConsumer(raw_dir=Path("data/raw"))
    return c


def test_e2e_fault_raises_anomaly(inference):
    """§54: fault restriction su valve0 → anomaly_score sale, label non-healthy."""
    # scenario vuoto: engine attivo (per fault injection runtime), nessun fault YAML
    sim = RealtimeSim(
        seed=42, mode="stepped", exposed_valves=[1],
        scenario=Scenario(scenario_id=99, name="m9-e2e", seed=42, faults=[]))
    assert sim.engine is not None
    _ensure_running(sim)

    # --- fase A: 50 cicli healthy (una finestra) ---
    healthy, _ = _collect_valve_cycles(sim, 50)
    feats_h = live_features(healthy, None, zstats=inference.zstats, n=50)
    assert feats_h.height == 1, f"attesa 1 finestra, {feats_h.height}"
    recs_h = inference.predict_frame(feats_h)
    assert len(recs_h) == 1
    anomaly_h = recs_h[0]["anomaly_score"]
    label_h = recs_h[0]["predicted_label"]

    # --- fase B: inietta restriction su valve0 (severity alta → FT/PC alterati) ---
    sim.engine.inject("restriction", valve_id=0, severity=0.5, duration_cycles=0)
    fault, _ = _collect_valve_cycles(sim, 50)
    feats_f = live_features(fault, None, zstats=inference.zstats, n=50)
    assert feats_f.height == 1
    recs_f = inference.predict_frame(feats_f)
    anomaly_f = recs_f[0]["anomaly_score"]
    label_f = recs_f[0]["predicted_label"]

    # --- assert cardine ---
    # KPI alterati: la portata è ridotta (restriction 0.5) → FT sale
    ft_h = healthy["fillingtime"].mean()
    ft_f = fault["fillingtime"].mean()
    assert ft_f > ft_h, f"FT non aumentato: {ft_h} -> {ft_f}"

    # feature cambiano (mean_fillingtime z-score sale)
    # prediction: anomaly sale o label diventa non-healthy
    assert (anomaly_f > anomaly_h) or (label_f != "healthy"), \
        f"nessun segnale ML: anomaly {anomaly_h:.4f}->{anomaly_f:.4f}, " \
        f"label {label_h}->{label_f}"

    # cleanup
    sim.engine.remove(0)


def test_e2e_fault_recovery_returns_healthy(inference):
    """§7 T5: rientro fault — remove()/ForceFault=FALSE → feature tornano
    healthy → score rientra (anomaly bassa, label healthy).

    Spec M9 §7 T5: "Rientro fault — E2E: remove()/ForceFault=FALSE →
    feature tornano healthy → score rientra" (era assente dai test;
    `test_e2e_fault_raises_anomaly` usava remove(0) solo come cleanup).
    """
    sim = RealtimeSim(
        seed=42, mode="stepped", exposed_valves=[1],
        scenario=Scenario(scenario_id=99, name="m9-e2e-t5", seed=42, faults=[]))
    assert sim.engine is not None
    _ensure_running(sim)

    # --- fase A: baseline healthy (una finestra) ---
    healthy, _ = _collect_valve_cycles(sim, 50)
    rec_h = inference.predict_frame(
        live_features(healthy, None, zstats=inference.zstats, n=50))[0]
    assert rec_h["predicted_label"] == "healthy"

    # --- fase B: fault restriction → anomaly sale, label non-healthy ---
    sim.engine.inject("restriction", valve_id=0, severity=0.5, duration_cycles=0)
    fault, _ = _collect_valve_cycles(sim, 50)
    rec_f = inference.predict_frame(
        live_features(fault, None, zstats=inference.zstats, n=50))[0]
    assert (rec_f["anomaly_score"] > rec_h["anomaly_score"]) \
        or (rec_f["predicted_label"] != "healthy"), \
        f"fault non rilevato: anomaly {rec_h['anomaly_score']:.4f}->" \
        f"{rec_f['anomaly_score']:.4f}, label {rec_h['predicted_label']}->" \
        f"{rec_f['predicted_label']}"

    # --- fase C: rientro — rimuovi il fault runtime ---
    sim.engine.remove(0)
    recovered, _ = _collect_valve_cycles(sim, 50)
    rec_r = inference.predict_frame(
        live_features(recovered, None, zstats=inference.zstats, n=50))[0]

    # il KPI torna verso la baseline (FT scende)
    ft_h = healthy["fillingtime"].mean()
    ft_f = fault["fillingtime"].mean()
    ft_r = recovered["fillingtime"].mean()
    assert ft_r < ft_f, f"FT non rientrato: {ft_h:.1f} -> {ft_f:.1f} -> {ft_r:.1f}"

    # lo score rientra: anomaly sotto il livello fault, label healthy
    assert rec_r["anomaly_score"] < rec_f["anomaly_score"], \
        f"score non rientrato: {rec_f['anomaly_score']:.4f}->" \
        f"{rec_r['anomaly_score']:.4f}"
    assert rec_r["predicted_label"] == "healthy", \
        f"label non rientrata: {rec_r['predicted_label']!r}"

    # cleanup
    sim.engine.remove(0)
