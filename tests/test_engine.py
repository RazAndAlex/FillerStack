"""Test M1 — motore del simulatore V3.

Copre: ciclo completo per-valvola (nessuno skip sano), stati raggiunti,
determinismo per seed, sanity KPI vs target V2, percorso SAFE_DEPRESSURIZATION,
integrazione fisica di base. Giornata compressa (template macchina fittizio
che somma a 24 h) per tenere i test sotto il minuto.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np
import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plcsim.clock import SimulationClock          # noqa: E402
from plcsim.config import SimConfig, MachineTemplate  # noqa: E402
from plcsim.plant import Plant                     # noqa: E402
from plcsim.plc import PLC, SAFE, DEAD_ZONE, FILLING  # noqa: E402
from plcsim.run import build_sim, _run_loop        # noqa: E402
from plcsim.telemetry import Telemetry, EVENT_COLUMNS  # noqa: E402

# giornata fittizia: 0.5 h Running dopo ~7 s, somma esatta 24 h
TPL = (("Idle", 0.001), ("Starting", 0.001), ("Running", 0.5),
       ("Stopping", 0.001), ("Stopped", 0.001), ("Idle", 23.496))
END_MS = int(0.51 * 3600 * 1000)
RUNNING_S = 0.5 * 3600.0


def make_cfg(seed: int = 42) -> SimConfig:
    cfg = SimConfig.build(seed=seed)
    cfg.machine = MachineTemplate(day_hours=TPL)
    return cfg


def run_once(seed: int = 42):
    cfg = make_cfg(seed)
    tel = Telemetry(SimulationClock(), Path("work") / "tests")
    clock, plant, plc = build_sim(cfg, tel)
    elapsed = _run_loop(cfg, tel, clock, plant, plc, END_MS, progress=False)
    return cfg, tel, plc, elapsed


@pytest.fixture(scope="module")
def sim():
    return run_once()


def test_cycle_counts_no_skip(sim):
    """Ogni valvola completa un ciclo per rotazione in Running (nessuno skip)."""
    _, tel, plc, _ = sim
    df = tel.collect_cycles()
    counts = df.group_by("machine_code").len()["len"]
    expected = RUNNING_S / 3.2          # rotazioni in 0.5 h = 562,5
    assert counts.min() >= int(expected) - 1
    assert counts.max() <= int(expected) + 1
    # 9 stati su 10 raggiunti; SAFE mai in sano (ADR-0009)
    assert plc.states_seen == {"IDLE", "FLUSHING", "PRESSURIZING", "FILLING",
                               "TAIL", "VALIDATE_FILL", "PAUSE", "SNIFT",
                               "DEAD_ZONE"}


def test_kpi_sanity(sim):
    """Medie per-valvola vicine ai target V2 (FT); fillingok emergente ~76%.

    TP: con D2 gli impulsi dello snap di fine corsa sono contati (flusso
    reale), quindi la media TP sale di ~+6..9% finché il worker di
    calibrazione ricalibra tau_close/k_ramp (GATE-DECISIONS D2: qui NON si
    compensano i valori medi).
    """
    cfg, tel, _, _ = sim
    df = tel.collect_cycles()
    tg = {v.machine_code: v for v in cfg.valves}
    agg = df.group_by("machine_code").agg(
        pl.col("fillingtime").mean().alias("ft"),
        pl.col("tailpulse").mean().alias("tp"),
        pl.col("pulsecount").mean().alias("pc"),
        pl.col("tailtime").mean().alias("tt"),
    )
    n_ft_ok = 0
    for r in agg.iter_rows(named=True):
        v = tg[r["machine_code"]]
        # FT entro ±2% per valvola (valve8/20: profilo anomalo oltre il
        # time_limit, divergenza documentata); TP entro ±10% (D2, vedi sopra)
        assert abs(r["ft"] - v.ft_mean) / v.ft_mean <= 0.02, r["machine_code"]
        assert abs(r["tp"] - v.tp_mean) / v.tp_mean <= 0.10, r["machine_code"]
        if abs(r["ft"] - v.ft_mean) / v.ft_mean <= 0.01:
            n_ft_ok += 1
        # PC: valvole sane ~ target + overshoot di scan (~+6,9)
        if r["machine_code"] not in ("valve8", "valve20"):
            assert 2500 <= r["pc"] <= 2512, r["machine_code"]
    assert n_ft_ok >= 33      # barra ADR-0004: solo valve8/20 fuori ±1%
    assert 0.60 <= df["fillingok"].mean() <= 0.90
    assert df["filling_step_out"].max() <= 26
    # D1: il target è la chiusura NORMALE; il 2000 ms non chiude più. Il limite
    # encoder (~2127 ms) e il SafetyTimeout (2500 ms) restano interruzioni
    # hard, ma in sano dominano le chiusure per target.
    cr = df["close_reason"].value_counts()
    counts = dict(zip(cr["close_reason"], cr["count"]))
    assert counts["target"] >= 0.98 * df.height, counts
    # eventuali chiuse encoder in sano: coerenza dei flag D1
    enc = df.filter(pl.col("close_reason") == "encoder_limit")
    if enc.height:
        assert (enc["position_limit"] == True).all()
        assert (enc["fill_quality_ok"] == False).all()
        assert (enc["sequence_ok"] == True).all()


def test_overtime_diagnostic(sim):
    """D1: FT > 2000 ms è SOLO diagnostico -> filling_overtime + SUSPECT."""
    _, tel, _, _ = sim
    df = tel.collect_cycles()
    # valve8/20 chiudono al target a FT~2035: l'overtime emerge davvero
    over = df.filter(pl.col("fillingtime") > 2000)
    assert over.height > 100, over.height
    assert (over["filling_overtime"] == True).all()
    assert (over["diagnostic_status"] == "SUSPECT").all()
    # nessuna chiusura forzata dal 2000 ms: FT max <= limite encoder (~2130)
    assert df["fillingtime"].max() <= 2130
    # il flag è coerente: mai filling_overtime con FT <= 2000
    ok = df.filter(pl.col("fillingtime") <= 2000)
    assert (ok["filling_overtime"] == False).all()


def test_encoder_limit_close():
    """D1: zona geometrica esaurita -> chiusura encoder_limit.

    Portata ridotta (x0.9): il target arriva dopo l'uscita di zona; la valvola
    chiude per limite encoder a ~2127 ms con PositionLimit=TRUE e
    FillQualityOK=FALSE (SequenceOK resta TRUE: sequenza completata).
    """
    cfg = make_cfg()
    for v in cfg.valves:
        v.flow_base_mls *= 0.90
    tel = Telemetry(SimulationClock(), Path("work") / "tests")
    clock, plant, plc = build_sim(cfg, tel)
    _run_loop(cfg, tel, clock, plant, plc, END_MS, progress=False)
    df = tel.collect_cycles()
    enc = df.filter(pl.col("close_reason") == "encoder_limit")
    assert enc.height > 100, enc.height
    assert (enc["position_limit"] == True).all()
    assert (enc["fill_quality_ok"] == False).all()
    assert (enc["filling_overtime"] == True).all()      # 2127 ms > 2000 ms
    assert (enc["diagnostic_status"] == "SUSPECT").all()
    assert (enc["sequence_ok"] == True).all()           # solo safety rompe la sequenza
    tgt = df.filter(pl.col("close_reason") == "target")
    assert (tgt["position_limit"] == False).all()
    # il limite encoder taglia il FT: nessun ciclo oltre ~2130 ms
    assert df["fillingtime"].max() <= 2130


def test_valve_events_and_determinism(sim):
    """ADR-0012: l'event log contiene le transizioni VALVOLA (non solo
    macchina) e il conteggio è deterministico per seed."""
    _, tel, _, _ = sim
    ev = tel._collect("events", EVENT_COLUMNS)
    by_code = ev.group_by("machine_code").len()
    counts = {r["machine_code"]: r["len"]
              for r in by_code.iter_rows(named=True)}
    assert counts.get("MACHINE", 0) > 0
    v0 = ev.filter(pl.col("machine_code") == "valve0")
    names = set(v0["event"].unique().to_list())
    assert {"IDLE", "FLUSHING", "PRESSURIZING", "FILLING", "TAIL",
            "VALIDATE_FILL", "PAUSE", "SNIFT", "DEAD_ZONE"} <= names
    # FILLING per valvola ~ cicli completati + aborti di fermo macchina (<=35)
    n_fill = v0.filter(pl.col("event") == "FILLING").height
    n_cyc = tel.collect_cycles().filter(
        pl.col("machine_code") == "valve0").height
    assert 0 <= n_fill - n_cyc <= 40
    # determinismo: stesso seed -> stesso numero di eventi per tipo/valvola
    _, tel2, _, _ = run_once(42)
    ev2 = tel2._collect("events", EVENT_COLUMNS)
    agg = lambda d: d.group_by(["machine_code", "event"]).len().sort(
        ["machine_code", "event"])
    assert agg(ev).equals(agg(ev2))


def test_determinism_per_seed():
    """Stesso seed -> stessi output (cicli ed eventi); seed diverso -> diverso."""
    def fingerprint(seed):
        _, tel, _, _ = run_once(seed)
        cyc = tel.collect_cycles().write_csv().encode()
        ev = tel._collect("events", EVENT_COLUMNS).write_csv().encode()
        return (hashlib.sha256(cyc).hexdigest(),
                hashlib.sha256(ev).hexdigest())

    assert fingerprint(42) == fingerprint(42)
    assert fingerprint(42) != fingerprint(7)


def test_safe_path():
    """FILLING oltre il SafetyTimeout -> SAFE_DEPRESSURIZATION, ciclo rifiutato."""
    cfg = make_cfg()
    tel = Telemetry(SimulationClock(), Path("work") / "tests")
    clock, plant, plc = build_sim(cfg, tel)
    t = 100_000
    v = 3
    # forza la valvola in FILLING oltre il SafetyTimeout (2500 ms)
    plc._state[v] = FILLING
    plc._is_filling[v] = True
    plc._open_cmd[v] = t - 2600
    plc._due[v] = np.iinfo(np.int64).max // 2
    plc._pt[v] = 100
    plant.mech[v].begin_open(t - 2600)
    plc.scan(t)
    assert plc._state[v] == SAFE
    assert "SAFE_DEPRESSURIZATION" in plc.states_seen
    rec = tel.cycles[-1]
    assert rec["close_reason"] == "safety_timeout"
    assert rec["sequence_ok"] is False or rec["sequence_ok"] == 0
    assert rec["diagnostic_status"] == "SUSPECT"
    # D1: FT > 2000 ms => filling_overtime (diagnostica) anche sul percorso safe
    assert rec["filling_overtime"] is True or rec["filling_overtime"] == 1
    # la valvola è spenta e dopo la depressurizzazione va in DEAD_ZONE
    assert plant._mask_closed[v]
    plc.scan(t + 600)
    assert plc._state[v] == DEAD_ZONE


def test_flow_integration():
    """Valvola aperta: portata ~flow_base, impulsi coerenti col volume."""
    cfg = make_cfg()
    clock = SimulationClock()
    plant = Plant(cfg, clock, np.random.default_rng(0))
    v = cfg.valves[0]
    plant.mech[0].begin_open(1_000)
    pulses = 0
    for k in range(1, 200):            # 2 s a valvola aperta (rampa inclusa)
        plant.step(1_000 + k * 10)
        pulses += int(plant.last_pulses[0])
    volume_ml = pulses * 0.1 + float(plant.volume_carry[0])
    # atteso: flow_base * (2 s - tau_open/2) * p_local * noise ~ +/-5%
    expected = v.flow_base_mls * (2.0 - cfg.plant.tau_open_ms / 2000.0)
    assert abs(volume_ml - expected) / expected < 0.05
