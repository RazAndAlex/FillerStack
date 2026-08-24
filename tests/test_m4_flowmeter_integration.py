"""Test di integrazione M4 (piano M4 §4.2) — catena evento → GT → KPI flowmeter.

Harness condiviso (pattern test_fault_integration.py/test_m3_integration.py):
run compresso `run_days(make_cfg(42), 0.02125, out=tmp_path, progress=False,
scenario=...)` (≈562 cicli/valvola, ~8 s/run); i 3 run canonici
(m4_healthy 41, m4_dropout 44, m4_glitch 45) + run plain (scenario=None)
sono fixture MODULE-scoped; i casi estremi (dropout totale s=1.0, glitch
continuo s=0.5, restriction 0.30 per T5) sono scratch MODULE-scoped su
YAML inline. Finestre calibrate in work/m4-calibration.md (W3: misura
prima, margine ≥2×, direzioni vincolanti mai allargate).

Scenari canonici: valve12, gradual start_cycle 100 / ramp_cycles 200 →
severità piena dal ciclo 299 (filtro GT `severity == piena`, pattern
test_fault_integration.py full_severity_ids).
Nessun output in work/: tutto su tmp_path.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plcsim.run import run_days                       # noqa: E402
from plcsim.scenario import load_scenario             # noqa: E402
from plcsim.sensors import FLOWMETER_BACKSTOP_MS      # noqa: E402
from plcsim.telemetry import EVENT_COLUMNS            # noqa: E402
from tests.test_engine import make_cfg                # noqa: E402
from tests.test_fault_integration import (            # noqa: E402
    assert_sane_statistical_bands,
    csv_bytes, full_severity_ids, read_frames, stats_of,
)

VALVE_ID = 12
MC = "valve12"
SEV_DROPOUT = 0.30          # severità piena dropout (scenario 44)
SEV_GLITCH = 0.05           # severità piena glitch (scenario 45)
START = 100                 # start_cycle canonico (R0: pattern M3 provato)
RAMP = 200                  # ramp_cycles canonico → piena dal ciclo 299
BACKSTOP = FLOWMETER_BACKSTOP_MS          # 1000 ms (sensors.py)
TT_BOUND = BACKSTOP + 150   # TT max atteso: backstop + SilenceTimer (150)
SANE_34 = {f"valve{v}" for v in range(35) if v != VALVE_ID}
SIGMA_EXCLUDED = {8, 20}    # profilo anomalo driver_scale 1.35 (D7): escluse
                            # dal conteggio banda σ, verificate esplicitamente

SCENARIOS = {
    "healthy": "m4_healthy.yaml",
    "dropout": "m4_dropout.yaml",
    "glitch": "m4_glitch.yaml",
}


def fingerprint(out: Path) -> str:
    """SHA-256 di (cycles+events+GT) serializzati in csv (D1)."""
    frames = read_frames(out)
    payload = b"".join(csv_bytes(frames[k])
                       for k in ("valve_cycles", "events", "ground_truth"))
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture(scope="module")
def run_dirs_m4(tmp_path_factory) -> dict[str, Path]:
    """I 3 run canonici compressi (una sola esecuzione per modulo)."""
    root = tmp_path_factory.mktemp("m4_integration")
    out = {}
    for name, fname in SCENARIOS.items():
        sc = load_scenario(ROOT / "scenarios" / fname)
        d = root / name
        run_days(make_cfg(42), 0.02125, out=d, progress=False, scenario=sc)
        out[name] = d
    return out


@pytest.fixture(scope="module")
def plain_run(tmp_path_factory) -> Path:
    """Run compresso senza scenario (percorso M1) per la bit-identità."""
    d = tmp_path_factory.mktemp("m4_plain") / "out"
    run_days(make_cfg(42), 0.02125, out=d, progress=False, scenario=None)
    return d


@pytest.fixture(scope="module")
def scratch_runs(tmp_path_factory) -> dict[str, Path]:
    """3 run scratch (YAML inline su tmp_path): dropout totale s=1.0,
    glitch continuo s=0.5 (livelock/backstop), restriction 0.30 (T5)."""
    root = tmp_path_factory.mktemp("m4_scratch")
    specs = {
        "extreme": (71, "scratch dropout totale valve12",
                    "flowmeter_dropout", 1.0),
        "heavy": (72, "scratch glitch continuo valve12",
                  "flowmeter_glitch", 0.5),
        "restr": (73, "scratch restriction 0.30 valve12", "restriction", 0.30),
    }
    out = {}
    for name, (sid, sname, ftype, sev) in specs.items():
        y = root / f"{name}.yaml"
        y.write_text(f"""scenario_id: {sid}
name: "{sname}"
seed: null
faults:
  - fault_type: {ftype}
    scope: local
    valve_id: 12
    severity: {sev}
    onset:
      mode: abrupt
      start_cycle: 1
""", encoding="utf-8")
        d = root / name
        run_days(make_cfg(42), 0.02125, out=d, progress=False,
                 scenario=load_scenario(y))
        out[name] = d
    return out


# --------------------------------------------------------------------------
# 1. Scenario YAML canonici
# --------------------------------------------------------------------------
def test_scenario_yaml_m4():
    """I 3 YAML canonici caricano con campi esatti (id/seed/faults)."""
    expected = {
        "m4_healthy.yaml": (41, "baseline sana M4", None, []),
        "m4_dropout.yaml": (44, "flowmeter dropout valve12", None,
                            [("flowmeter_dropout", 12, 0.30)]),
        "m4_glitch.yaml": (45, "flowmeter glitch valve12", None,
                           [("flowmeter_glitch", 12, 0.05)]),
    }
    for fname, (sid, sname, seed, faults) in expected.items():
        sc = load_scenario(ROOT / "scenarios" / fname)
        assert sc.scenario_id == sid, fname
        assert sc.name == sname, fname
        assert sc.seed == seed, fname
        assert len(sc.faults) == len(faults), fname
        for f, (ftype, vid, sev) in zip(sc.faults, faults):
            assert f.fault_type == ftype, fname
            assert f.scope == "local", fname
            assert f.valve_id == vid, fname
            assert f.severity == sev, fname
            assert f.onset.mode == "gradual", fname
            assert f.onset.start_cycle == START, fname
            assert f.onset.ramp_cycles == RAMP, fname


# --------------------------------------------------------------------------
# 2./3. Catene fault → KPI (AC-M4-1 / AC-M4-2)
# --------------------------------------------------------------------------
def _chain_events_and_gt(frames, ftype: str, sev: float) -> pl.DataFrame:
    """Parte comune delle catene: (a) eventi + (b) GT (valve12)."""
    cyc, ev, gt = frames["valve_cycles"], frames["events"], frames["ground_truth"]
    ev_v = ev.filter(pl.col("machine_code") == MC)
    # FAULT_START: una volta, sul primo ciclo affetto, note corrette
    # (formato engine f"{severity}" → repr float, es. 0.3; vedi calibrazione)
    fs = ev_v.filter(pl.col("event") == "FAULT_START")
    assert fs.height == 1, fs.height
    r = fs.row(0, named=True)
    assert r["cycle_id"] == START, r
    assert r["note"] == f"{ftype} severity={sev} start_cycle={START}", r
    # CMD:OPEN/CMD:CLOSE con cycle_id presenti in GT (join 1:1, integrale)
    cc = ev_v.filter(pl.col("event") == "CMD:CLOSE")
    co = ev_v.filter(pl.col("event") == "CMD:OPEN")
    gt_ids = set(gt.filter(pl.col("machine_code") == MC)["cycle_id"].to_list())
    assert cc.height == co.height == len(gt_ids), (cc.height, co.height)
    assert set(cc["cycle_id"].to_list()) == gt_ids
    # invariante: CMD:CLOSE.note == cycles.close_reason (tutti i cicli)
    cr = dict(zip(cyc.filter(pl.col("machine_code") == MC)["cycle_id"],
                  cyc.filter(pl.col("machine_code") == MC)["close_reason"]))
    for row in cc.iter_rows(named=True):
        assert row["note"] == cr[row["cycle_id"]], row
    # GT: fault_type/severity sui cicli >= start, null/0.0 prima
    g = gt.filter(pl.col("machine_code") == MC)
    pre = g.filter(pl.col("cycle_id") < START)
    assert (pre["fault_type"].is_null()).all() and (pre["severity"] == 0.0).all()
    aff = g.filter(pl.col("cycle_id") >= START)
    assert aff.height > 0
    assert (aff["fault_type"] == ftype).all(), (ftype, aff.height)
    return g


def test_fault_chain_dropout(run_dirs_m4):
    """AC-M4-1: chain dropout — eventi/GT + KPI a severità piena.

    Quota encoder_limit ∈ [60%, 100%] (atteso ~100%: FT nominale
    1916/0.70 ≈ 2738 ≫ limite encoder 2127); FT medio ≈ 2127-2130 (clamp);
    deltapulse medio > 0; fillingok ≤ 0.40; ΔPC_mean vs healthy ∈
    [−30%, −10%] (misurato −21.3%).
    """
    frames = read_frames(run_dirs_m4["dropout"])
    h = read_frames(run_dirs_m4["healthy"])
    g = _chain_events_and_gt(frames, "flowmeter_dropout", SEV_DROPOUT)
    ids = full_severity_ids(g, VALVE_ID, SEV_DROPOUT)
    assert len(ids) > 200, len(ids)            # 247 cicli pieni (299..545)
    s = stats_of(frames["valve_cycles"].filter(
        (pl.col("machine_code") == MC) & pl.col("cycle_id").is_in(ids)))
    h12 = stats_of(h["valve_cycles"].filter(pl.col("machine_code") == MC))
    assert 0.60 <= s["encoder"] <= 1.00, f"encoder={s['encoder']:.3f}"
    assert 2120 <= s["ft"] <= 2130, f"FT={s['ft']:.1f}"
    assert s["delta"] > 0, f"delta={s['delta']:.1f}"
    assert s["fillingok"] <= 0.40, f"fok={s['fillingok']:.3f}"
    dpc_rel = (s["pc"] - h12["pc"]) / h12["pc"]
    assert -0.30 <= dpc_rel <= -0.10, f"ΔPC%={dpc_rel:.4f}"


def test_fault_chain_glitch(run_dirs_m4):
    """AC-M4-2: chain glitch — GT a severità piena; ΔFT ∈ [−20, −2] ms
    (atteso ≈ −8..−12); deltapulse medio ≤ healthy; fillingok ≥ 0.80;
    ΔTP ≥ 0 e ΔTT ≥ 0 (direzioni; valori calibrati in m4-calibration.md)."""
    frames = read_frames(run_dirs_m4["glitch"])
    h = read_frames(run_dirs_m4["healthy"])
    g = _chain_events_and_gt(frames, "flowmeter_glitch", SEV_GLITCH)
    ids = full_severity_ids(g, VALVE_ID, SEV_GLITCH)
    assert len(ids) > 200, len(ids)            # 264 cicli pieni (299..562)
    s = stats_of(frames["valve_cycles"].filter(
        (pl.col("machine_code") == MC) & pl.col("cycle_id").is_in(ids)))
    h12 = stats_of(h["valve_cycles"].filter(pl.col("machine_code") == MC))
    dft = s["ft"] - h12["ft"]
    assert -20 <= dft <= -2, f"ΔFT={dft:.2f} ms"
    assert s["delta"] <= h12["delta"], \
        f"delta {s['delta']:.4f} > healthy {h12['delta']:.4f}"
    assert s["fillingok"] >= 0.80, f"fok={s['fillingok']:.3f}"
    assert s["tp"] - h12["tp"] >= 0, f"ΔTP={s['tp'] - h12['tp']:.2f}"
    assert s["tt"] - h12["tt"] >= 0, f"ΔTT={s['tt'] - h12['tt']:.2f}"


# --------------------------------------------------------------------------
# 4. Finestre numeriche a severità piena (calibrate, margine ≥2×)
# --------------------------------------------------------------------------
def test_dropout_windows(run_dirs_m4):
    """AC-M4-1 (finestre): deltapulse ∈ [400, 650] (misurato 528),
    poslim/overtime/step26 ≥ 0.90 (misurato 1.0), FT max ≤ 2130,
    step_out ≤ 26, TT ≤ backstop+150 (misurato max 540)."""
    frames = read_frames(run_dirs_m4["dropout"])
    ids = full_severity_ids(frames["ground_truth"], VALVE_ID, SEV_DROPOUT)
    s = stats_of(frames["valve_cycles"].filter(
        (pl.col("machine_code") == MC) & pl.col("cycle_id").is_in(ids)))
    assert 400 <= s["delta"] <= 650, f"delta={s['delta']:.1f}"
    assert s["poslim"] >= 0.90, f"poslim={s['poslim']:.3f}"
    assert s["overtime"] >= 0.90, f"ovt={s['overtime']:.3f}"
    assert s["step26"] >= 0.90, f"step26={s['step26']:.3f}"
    assert s["ft_max"] <= 2130, s["ft_max"]
    assert s["step_max"] <= 26, s["step_max"]
    assert s["tt_max"] <= TT_BOUND, s["tt_max"]
    # bounds globali del run (tutte le valvole)
    cyc = frames["valve_cycles"]
    assert cyc["fillingtime"].max() <= 2130
    assert cyc["filling_step_out"].max() <= 26
    assert cyc["tailtime"].max() <= TT_BOUND


def test_glitch_windows(run_dirs_m4):
    """AC-M4-2 (finestre): ΔFT ∈ [−20, −2] ms (misurato −11.55),
    ΔTP ∈ [0, +10] (misurato +1.8), ΔTT ∈ [0, +180] (misurato +90.2),
    fillingok ≥ 0.80, deltapulse ≤ healthy, FT max ≤ 2130, step ≤ 26,
    TT ≤ backstop+150 (misurato max 970)."""
    frames = read_frames(run_dirs_m4["glitch"])
    h = read_frames(run_dirs_m4["healthy"])
    ids = full_severity_ids(frames["ground_truth"], VALVE_ID, SEV_GLITCH)
    s = stats_of(frames["valve_cycles"].filter(
        (pl.col("machine_code") == MC) & pl.col("cycle_id").is_in(ids)))
    h12 = stats_of(h["valve_cycles"].filter(pl.col("machine_code") == MC))
    dft = s["ft"] - h12["ft"]
    assert -20 <= dft <= -2, f"ΔFT={dft:.2f}"
    dtp = s["tp"] - h12["tp"]
    assert 0 <= dtp <= 10, f"ΔTP={dtp:.2f}"
    dtt = s["tt"] - h12["tt"]
    assert 0 <= dtt <= 180, f"ΔTT={dtt:.2f}"
    assert s["fillingok"] >= 0.80, f"fok={s['fillingok']:.3f}"
    assert s["delta"] <= h12["delta"], "deltapulse non ≤ healthy"
    assert s["ft_max"] <= 2130, s["ft_max"]
    assert s["step_max"] <= 26, s["step_max"]
    assert s["tt_max"] <= TT_BOUND, s["tt_max"]
    cyc = frames["valve_cycles"]
    assert cyc["fillingtime"].max() <= 2130
    assert cyc["filling_step_out"].max() <= 26
    assert cyc["tailtime"].max() <= TT_BOUND


# --------------------------------------------------------------------------
# 5. Segno del mismatch (verifica interna, mai GT)
# --------------------------------------------------------------------------
def _subsets(frames, sev_full: float):
    """(full, ramp) = cicli valve12 a severità piena / in rampa (dal GT)."""
    cyc, gt = frames["valve_cycles"], frames["ground_truth"]
    g12 = gt.filter(pl.col("machine_code") == MC)
    ids_full = full_severity_ids(gt, VALVE_ID, sev_full)
    ids_ramp = g12.filter((pl.col("cycle_id") >= START)
                          & (pl.col("severity") < sev_full))["cycle_id"].to_list()
    sel = pl.col("machine_code") == MC
    full = cyc.filter(sel & pl.col("cycle_id").is_in(ids_full))
    ramp = cyc.filter(sel & pl.col("cycle_id").is_in(ids_ramp))
    return full, ramp


def test_mismatch_sign_internal(run_dirs_m4):
    """Segno del mismatch su valve12 vs healthy stesso-seed (test-only).

    Il volume fisico non è osservabile nei record (W1 ha dimostrato carry
    identico a livello unit): la firma nel run è PC↓ a parità di fisica per
    il dropout e PC↑ (misurato +0.0025: effetto ~nullo, il PLC chiude al
    target) per il glitch. Ampiezza ∝ severità: severità piena > rampa
    dello stesso run — su ΔPC per il dropout, sul KPI primario osservabile
    ΔFT per il glitch (PC è invariante alle chiusure target; documentato
    in m4-calibration.md §4.2).
    """
    h12 = read_frames(run_dirs_m4["healthy"])["valve_cycles"].filter(
        pl.col("machine_code") == MC)
    # dropout: PC ↓, ampiezza ∝ severità
    full, ramp = _subsets(read_frames(run_dirs_m4["dropout"]), SEV_DROPOUT)
    dpc_full = full["pulsecount"].mean() - h12["pulsecount"].mean()
    dpc_ramp = ramp["pulsecount"].mean() - h12["pulsecount"].mean()
    assert dpc_full < 0, f"dropout ΔPC={dpc_full:.2f} non negativo"
    assert abs(dpc_full) > abs(dpc_ramp), (dpc_full, dpc_ramp)
    # glitch: PC ↑ (direzione, deterministico), ampiezza ∝ severità su ΔFT
    full, ramp = _subsets(read_frames(run_dirs_m4["glitch"]), SEV_GLITCH)
    dpc_full = full["pulsecount"].mean() - h12["pulsecount"].mean()
    assert dpc_full > 0, f"glitch ΔPC={dpc_full:.4f} non positivo"
    dft_full = full["fillingtime"].mean() - h12["fillingtime"].mean()
    dft_ramp = ramp["fillingtime"].mean() - h12["fillingtime"].mean()
    assert dft_full < 0, f"glitch ΔFT={dft_full:.2f}"
    assert abs(dft_full) > abs(dft_ramp), (dft_full, dft_ramp)


# --------------------------------------------------------------------------
# 6. Sanity valvole sane (AC-M4-7)
# --------------------------------------------------------------------------
def test_sanity_healthy_valves_m4(run_dirs_m4):
    """AC-M4-7: le 34 valvole sane dei run dropout/glitch entro le bande
    statistiche vs m4_healthy stesso-seed (bande E: |ΔFT| ≤ 1 ms, |ΔTT| ≤ 8,
    |ΔTP| ≤ 4, |ΔPC| ≤ 1.5, |Δfok| ≤ 3 p.p.); σ_FT relativo ≤ 10% (valve8/20
    escluse dal conteggio, D7 — misurate e riportate in calibrazione)."""
    h = read_frames(run_dirs_m4["healthy"])["valve_cycles"]
    for name in ("dropout", "glitch"):
        cyc = read_frames(run_dirs_m4[name])["valve_cycles"]
        assert_sane_statistical_bands(cyc, h, SANE_34)
        n_ok = 0
        for v in range(35):
            if v == VALVE_ID:
                continue
            mc = f"valve{v}"
            s = cyc.filter(pl.col("machine_code") == mc)["fillingtime"].std()
            b = h.filter(pl.col("machine_code") == mc)["fillingtime"].std()
            ds = abs(s - b) / b
            assert ds <= 0.10, f"{mc}: |Δσ|/σ={ds:.4f} > 10%"
            if v not in SIGMA_EXCLUDED:
                n_ok += 1
        assert n_ok == 32, n_ok   # 34 sane − valve8/20 escluse dal conteggio


# --------------------------------------------------------------------------
# 7./8. Bit-identità sana e determinismo (AC-M4-5 / AC determinismo)
# --------------------------------------------------------------------------
def test_healthy_equiv_plain_compressed(run_dirs_m4, plain_run):
    """AC-M4-5: run compresso m4_healthy (scenario_id 41) ≡ percorso M1
    (scenario=None) stesso seed: valve_cycles/events/GT identici a meno di
    scenario_id (pattern test_fault_integration.py:498-522)."""
    h = read_frames(run_dirs_m4["healthy"])
    p = read_frames(plain_run)
    a = h["valve_cycles"].drop("scenario_id")
    b = p["valve_cycles"].drop("scenario_id")
    assert csv_bytes(a) == csv_bytes(b)
    a = h["events"].drop("scenario_id").filter(
        ~pl.col("event").is_in(["CMD:OPEN", "CMD:CLOSE"]))
    b = p["events"].drop("scenario_id")
    assert csv_bytes(a) == csv_bytes(b)
    a = h["ground_truth"].drop("scenario_id")
    b = p["ground_truth"].drop("scenario_id")
    assert csv_bytes(a) == csv_bytes(b)


def test_determinism_fingerprint_m4(run_dirs_m4, tmp_path_factory):
    """AC determinismo: SHA-256 di (cycles+events+GT) del run dropout
    ripetuto → identico (determinismo esatto per (seed, scenario))."""
    out2 = tmp_path_factory.mktemp("m4_dropout2") / "out"
    run_days(make_cfg(42), 0.02125, out=out2, progress=False,
             scenario=load_scenario(ROOT / "scenarios" / "m4_dropout.yaml"))
    assert fingerprint(out2) == fingerprint(run_dirs_m4["dropout"])


# --------------------------------------------------------------------------
# 9. Join GT↔cycles (AC-M4-9)
# --------------------------------------------------------------------------
def test_gt_join_cycles(run_dirs_m4, plain_run):
    """AC-M4-9: join 1:1 GT↔cycles su (machine_code, cycle_id, scenario_id)
    senza orfani in tutti i run (canonici + plain); nessun evento VALVOLA
    orfano (H8, allow-list DEAD_ZONE abort_stop)."""
    dirs = dict(run_dirs_m4)
    dirs["plain"] = plain_run
    for name, d in dirs.items():
        cyc, ev, gt = (read_frames(d)["valve_cycles"],
                       read_frames(d)["events"],
                       read_frames(d)["ground_truth"])
        keys_c = set(zip(cyc["machine_code"], cyc["cycle_id"],
                         cyc["scenario_id"]))
        keys_g = set(zip(gt["machine_code"], gt["cycle_id"],
                         gt["scenario_id"]))
        assert keys_c == keys_g, f"{name}: join 1:1 GT<->cycles"
        evv = ev.filter((pl.col("machine_code") != "MACHINE")
                        & (pl.col("cycle_id") > 0))
        abort = ev.filter((pl.col("event") == "DEAD_ZONE")
                          & (pl.col("note") == "abort_stop"))
        abort_ids = set(zip(abort["machine_code"], abort["cycle_id"]))
        orfani = 0
        for r in evv.iter_rows(named=True):
            if (r["machine_code"], r["cycle_id"], r["scenario_id"]) in keys_c:
                continue
            if (r["machine_code"], r["cycle_id"]) in abort_ids:
                continue
            orfani += 1
        assert orfani == 0, f"{name}: {orfani} eventi orfani"


# --------------------------------------------------------------------------
# 10./11. Casi estremi (scratch)
# --------------------------------------------------------------------------
def test_extreme_dropout_total(scratch_runs, run_dirs_m4):
    """Dropout totale s=1.0 (abrupt start 1) su valve12 → quota
    encoder_limit 100%, position_limit True, fillingok False,
    filling_overtime True, FT max = 2130, NESSUN SAFE (encoder 2127 <
    SafetyTimeout 2500), nessun ciclo perso (562 cicli contigui 1..562,
    uguali al run sano: la cadenza è la rotazione, non la chiusura)."""
    frames = read_frames(scratch_runs["extreme"])
    cyc = frames["valve_cycles"]
    v12 = cyc.filter(pl.col("machine_code") == MC)
    s = stats_of(v12)
    assert s["encoder"] >= 0.99, f"enc={s['encoder']:.3f}"
    enc = v12.filter(pl.col("close_reason") == "encoder_limit")
    assert (enc["position_limit"] == True).all()
    assert (enc["fill_quality_ok"] == False).all()
    assert (enc["filling_overtime"] == True).all()
    assert (v12["fillingok"] == False).all()
    assert s["ft_max"] == 2130, s["ft_max"]
    assert frames["events"].filter(
        pl.col("event") == "SAFE_DEPRESSURIZATION").height == 0
    assert (cyc["close_reason"] == "safety_timeout").sum() == 0
    h12 = read_frames(run_dirs_m4["healthy"])["valve_cycles"].filter(
        pl.col("machine_code") == MC)
    assert v12.height == h12.height == 562
    assert v12["cycle_id"].min() == 1 and v12["cycle_id"].max() == 562


def test_glitch_livelock_free(scratch_runs):
    """AC-M4-8: glitch s=0.5 continuo (abrupt start 1) → il run COMPLETA
    (nessun hang: il backstop Flowmeter limita gli spuri post-close a
    FLOWMETER_BACKSTOP_MS), TT max ≤ backstop+150 (misurato 990), nessun
    SAFE, eventi LATE_PULSE presenti."""
    frames = read_frames(scratch_runs["heavy"])
    cyc, ev = frames["valve_cycles"], frames["events"]
    v12 = cyc.filter(pl.col("machine_code") == MC)
    assert v12.height > 0
    assert v12["cycle_id"].min() == 1
    assert v12["cycle_id"].max() == v12.height   # contigui: nessun hang
    s = stats_of(v12)
    assert s["tt_max"] <= TT_BOUND, f"TT max={s['tt_max']}"
    assert ev.filter(pl.col("event") == "SAFE_DEPRESSURIZATION").height == 0
    assert (cyc["close_reason"] == "safety_timeout").sum() == 0
    lp = ev.filter(pl.col("event") == "LATE_PULSE")
    assert lp.height > 0, "LATE_PULSE assenti"


# --------------------------------------------------------------------------
# 12. Eventi LATE_PULSE (AC-M4-6)
# --------------------------------------------------------------------------
def test_late_pulse_events_integration(run_dirs_m4):
    """AC-M4-6: run glitch → eventi LATE_PULSE in events.parquet con
    cycle_id/scenario_id e conteggio > 0; schema record invariato (nessuna
    colonna extra); run healthy/dropout → ZERO eventi LATE_PULSE."""
    ev = read_frames(run_dirs_m4["glitch"])["events"]
    lp = ev.filter(pl.col("event") == "LATE_PULSE")
    assert lp.height > 0, lp.height            # misurato 1315
    assert {"cycle_id", "scenario_id"} <= set(lp.columns)
    assert (lp["scenario_id"] == 45).all()
    assert (lp["cycle_id"] >= 1).all()
    assert (lp["machine_code"] == MC).all()
    assert ev.columns == EVENT_COLUMNS         # nessuna colonna extra
    for name in ("healthy", "dropout"):
        e = read_frames(run_dirs_m4[name])["events"]
        assert e.filter(pl.col("event") == "LATE_PULSE").height == 0, name
        assert e.columns == EVENT_COLUMNS, name


# --------------------------------------------------------------------------
# 13. GT distingue processo vs sensore (AC-M4-4, T5)
# --------------------------------------------------------------------------
def test_gt_distinguishes_process_vs_sensor(scratch_runs, run_dirs_m4):
    """AC-M4-4 (T5): restriction s=0.30 (scratch) vs flowmeter_dropout
    s=0.30 (scenario 44) su valve12, stesso seed → fault_type corretto al
    100% dei cicli affetti (pattern KPI coincidente: entrambi saturano
    l'encoder); join senza orfani in entrambi i run."""
    fr = read_frames(scratch_runs["restr"])
    gtr = fr["ground_truth"].filter(pl.col("machine_code") == MC)
    assert (gtr["fault_type"] == "restriction").all()
    fd = read_frames(run_dirs_m4["dropout"])
    gtd = fd["ground_truth"].filter(pl.col("machine_code") == MC)
    pre = gtd.filter(pl.col("cycle_id") < START)
    assert (pre["fault_type"].is_null()).all()
    aff = gtd.filter(pl.col("cycle_id") >= START)
    assert (aff["fault_type"] == "flowmeter_dropout").all()
    for frames in (fr, fd):
        cyc, gt = frames["valve_cycles"], frames["ground_truth"]
        kc = set(zip(cyc["machine_code"], cyc["cycle_id"], cyc["scenario_id"]))
        kg = set(zip(gt["machine_code"], gt["cycle_id"], gt["scenario_id"]))
        assert kc == kg, "join 1:1 con orfani"
    # pattern KPI coincidente (entrambi ~100% encoder): la GT discrimina
    sr = stats_of(fr["valve_cycles"].filter(pl.col("machine_code") == MC))
    ids = full_severity_ids(gtd, VALVE_ID, SEV_DROPOUT)
    sd = stats_of(fd["valve_cycles"].filter(
        (pl.col("machine_code") == MC) & pl.col("cycle_id").is_in(ids)))
    assert sr["ft"] == 2130.0 and sd["ft"] == 2130.0
    assert sr["encoder"] == 1.0 and sd["encoder"] == 1.0


# --------------------------------------------------------------------------
# 14./15./16. Bounds, summary, parquet
# --------------------------------------------------------------------------
def test_bounds_m4(run_dirs_m4, plain_run):
    """D1: FT ≤ 2130, filling_step_out ≤ 26, TT ≤ backstop+150 su tutti i
    run; nessun close_reason safety_timeout né SAFE_DEPRESSURIZATION."""
    dirs = dict(run_dirs_m4)
    dirs["plain"] = plain_run
    for name, d in dirs.items():
        frames = read_frames(d)
        cyc = frames["valve_cycles"]
        assert cyc["fillingtime"].max() <= 2130, name
        assert cyc["filling_step_out"].max() <= 26, name
        assert cyc["tailtime"].max() <= TT_BOUND, name
        assert (cyc["close_reason"] == "safety_timeout").sum() == 0, name
        assert frames["events"].filter(
            pl.col("event") == "SAFE_DEPRESSURIZATION").height == 0, name


def test_run_summary_scenario_m4(run_dirs_m4):
    """run_summary.json contiene scenario_id/scenario_name esatti."""
    expected = {"healthy": (41, "baseline sana M4"),
                "dropout": (44, "flowmeter dropout valve12"),
                "glitch": (45, "flowmeter glitch valve12")}
    for name, (sid, sname) in expected.items():
        s = json.loads((run_dirs_m4[name] / "run_summary.json")
                       .read_text(encoding="utf-8"))
        assert s["scenario_id"] == sid, name
        assert s["scenario_name"] == sname, name


def test_m4_parquet_readable(run_dirs_m4):
    """I parquet dei 3 run canonici si leggono con polars (pattern
    test_m1_events_readable); GT 1:1 con i cycle record; fault_timeline
    una riga per i run guasti, assente nel sano."""
    for name in ("healthy", "dropout", "glitch"):
        df = pl.read_parquet(run_dirs_m4[name] / "valve_cycles.parquet")
        assert df.height > 0, name
        assert {"machine_code", "cycle_id", "fillingtime", "scenario_id"} \
            <= set(df.columns), name
        ev = pl.read_parquet(run_dirs_m4[name] / "events.parquet")
        assert ev.height > 0, name
        assert ev.columns == EVENT_COLUMNS, name
        gt = pl.read_parquet(run_dirs_m4[name] / "ground_truth.parquet")
        assert gt.height == df.height, name    # 1 riga GT per ciclo
    for name, ftype in (("dropout", "flowmeter_dropout"),
                        ("glitch", "flowmeter_glitch")):
        tl = pl.read_parquet(run_dirs_m4[name] / "fault_timeline.parquet")
        assert tl.height == 1, name
        assert tl["fault_type"][0] == ftype, name
        assert tl["end_cycle"].is_null().all(), name
    assert not (run_dirs_m4["healthy"] / "fault_timeline.parquet").exists()
