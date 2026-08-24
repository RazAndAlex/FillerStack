"""Test W1 (M2) — unit del fault engine + loader YAML (piano §5.1).

Copre: parsing/validazione YAML (healthy + demo + rifiuti), severità di
onset (abrupt/gradual, confini), costruzione dell'engine (pre-applicazione
per start_cycle=1), mapping GT (rec.cycle_id, nessun contatore engine),
timeline, derivazione eventi CMD:OPEN/CMD:CLOSE, determinismo senza RNG,
separazione stream SeedSequence (ADR-0013), non-leakage del PLC (ADR-0012).

NOTA (stato parallelo W2): plant.py è in modifica da W2 (Plant._restriction,
ValveMechanics.open_delay_ms/close_delay_ms). Se gli attributi non sono
ancora presenti, l'helper _make_plant li materializza SOLO per i test
(mai codice di produzione); al merge W2 il fallback diventa no-op.
"""
from __future__ import annotations

import copy
import inspect
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plcsim.clock import SimulationClock            # noqa: E402
from plcsim.config import SimConfig                 # noqa: E402
from plcsim.plant import Plant                      # noqa: E402
from plcsim.scenario import (                       # noqa: E402
    FaultEngine, FaultOnset, FaultSpec, Scenario,
    load_scenario, severity_at,
)
from plcsim.validation import CycleRecord           # noqa: E402

N_VALVES = 35


# --------------------------------------------------------------------------
# Fixture / helper
# --------------------------------------------------------------------------
def _make_plant(seed: int = 42):
    """Plant reale senza loop; fallback sugli attributi W2 (in arrivo).

    W2 sta aggiungendo a plant.py: Plant._restriction (np.ones di default) e
    ValveMechanics.open_delay_ms/close_delay_ms. Se non ancora presenti,
    questo helper li materializza SOLO per i test (mai in produzione) —
    ValveMechanics ha __slots__ rigidi, quindi serve una sottoclasse con
    __dict__ per gli attributi delay.
    """
    cfg = SimConfig.build(seed=seed)
    plant = Plant(cfg, SimulationClock(), np.random.default_rng(seed))
    if not hasattr(plant, "_restriction"):
        plant._restriction = np.ones(len(cfg.valves))   # W2 non ancora atterrato
    m0 = plant.mech[0]
    if not (hasattr(m0, "open_delay_ms") and hasattr(m0, "close_delay_ms")):

        class _Mech(type(m0)):
            __slots__ = ("__dict__",)

        mech = []
        for m in plant.mech:
            nm = _Mech(m.plant, m.i, m.cfg, m.v)
            for slot in type(m).__slots__:
                setattr(nm, slot, getattr(m, slot))
            nm.open_delay_ms = 0.0
            nm.close_delay_ms = 0.0
            mech.append(nm)
        plant.mech = mech
    return cfg, plant


def _rec(vi: int, cycle_id: int, ts_beg: int = 1_000_000, ft: int = 1910,
         tt: int = 300, tp: int = 25, pc: int = 2500,
         reason: str = "target") -> CycleRecord:
    """CycleRecord sintetico (stessi campi richiesti di validation.complete_cycle)."""
    return CycleRecord(
        machine_code=f"valve{vi}", ts_beg=ts_beg, fillingtime=ft, tailtime=tt,
        tailpulse=tp, pulsecount=pc, target=2500, deltapulse=2500 - pc,
        filling_step_out=20, fillingok=True, fill_quality_ok=True,
        sequence_ok=True, sample_valid=True, diagnostic_status="NORMAL",
        close_reason=reason, cycle_id=cycle_id,
    )


# --------------------------------------------------------------------------
# Loader YAML
# --------------------------------------------------------------------------
def test_load_healthy_yaml():
    """scenarios/m2_healthy.yaml: scenario sano (faults == [])."""
    sc = load_scenario(Path(__file__).resolve().parent.parent
                       / "scenarios" / "m2_healthy.yaml")
    assert sc.scenario_id == 1
    assert sc.name == "baseline sana M1"
    assert sc.seed is None
    assert sc.faults == []


DEMO_YAML = """\
scenario_id: 42
name: demo M2 — 3 fault meccanici, 3 severità
seed: 42
faults:
  - fault_type: restriction
    scope: local
    valve_id: 2
    severity: 0.04
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
  - fault_type: restriction
    scope: local
    valve_id: 12
    severity: 0.07
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
  - fault_type: restriction
    scope: local
    valve_id: 0
    severity: 0.12
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
  - fault_type: closing_delay
    scope: local
    valve_id: 3
    severity: 50
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
  - fault_type: closing_delay
    scope: local
    valve_id: 6
    severity: 100
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
  - fault_type: closing_delay
    scope: local
    valve_id: 7
    severity: 150
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
  - fault_type: opening_delay
    scope: local
    valve_id: 9
    severity: 40
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
  - fault_type: opening_delay
    scope: local
    valve_id: 10
    severity: 80
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
  - fault_type: opening_delay
    scope: local
    valve_id: 1
    severity: 120
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
"""


def test_load_demo_yaml(tmp_path):
    """Fixture INLINE (piano §8): 9 fault, campi corretti (valvole/severità/onset)."""
    p = tmp_path / "m2_demo.yaml"
    p.write_text(DEMO_YAML, encoding="utf-8")
    sc = load_scenario(p)
    assert sc.scenario_id == 42
    assert sc.name == "demo M2 — 3 fault meccanici, 3 severità"
    assert sc.seed == 42
    assert len(sc.faults) == 9
    attesi = [
        ("restriction", 2, 0.04), ("restriction", 12, 0.07),
        ("restriction", 0, 0.12), ("closing_delay", 3, 50.0),
        ("closing_delay", 6, 100.0), ("closing_delay", 7, 150.0),
        ("opening_delay", 9, 40.0), ("opening_delay", 10, 80.0),
        ("opening_delay", 1, 120.0),
    ]
    for f, (ftype, v, sev) in zip(sc.faults, attesi):
        assert f.fault_type == ftype
        assert f.scope == "local"
        assert f.valve_id == v
        assert f.severity == sev
        assert f.onset.mode == "gradual"
        assert f.onset.start_cycle == 100
        assert f.onset.ramp_cycles == 200


BASE = {
    "scenario_id": 42,
    "name": "test rifiuti",
    "seed": None,
    "faults": [
        {"fault_type": "restriction", "scope": "local", "valve_id": 2,
         "severity": 0.07,
         "onset": {"mode": "abrupt", "start_cycle": 50}},
    ],
}


def _load_mutated(tmp_path, mutate, expected_sub: str):
    d = copy.deepcopy(BASE)
    mutate(d)
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_scenario(p)
    assert expected_sub in str(exc.value)


def _set_fault(d, **kw):
    d["faults"][0].update(kw)


@pytest.mark.parametrize("mutate,sub", [
    (lambda d: d.pop("scenario_id"), "scenario_id"),
    (lambda d: d.__setitem__("scenario_id", "x"), "scenario_id"),
    (lambda d: d.pop("name"), "name"),
    (lambda d: d.__setitem__("seed", "x"), "seed"),
    (lambda d: d.__setitem__("faults", {}), "faults"),
    (lambda d: _set_fault(d, fault_type="vibrazione"), "fault_type"),
    (lambda d: _set_fault(d, scope="group"), "scope"),
    (lambda d: _set_fault(d, valve_id=35), "valve_id"),
    (lambda d: _set_fault(d, valve_id=-1), "valve_id"),
    (lambda d: _set_fault(d, severity=0.0), "severity"),
    (lambda d: _set_fault(d, severity=1.5), "severity"),
])
def test_validate_rejects_restriction(tmp_path, mutate, sub):
    """Rifiuti restriction: tipo ignoto, scope, valve_id, severità fuori (0,1]."""
    _load_mutated(tmp_path, mutate, sub)


@pytest.mark.parametrize("severity,sub", [
    (0, "severity"), (-5, "severity"), (50.5, "severity"),
])
def test_validate_rejects_delay_severity(tmp_path, severity, sub):
    """Delay severity deve essere int >= 1 ms (0, negativi, non-int rifiutati)."""
    def mutate(d):
        _set_fault(d, fault_type="closing_delay", severity=severity)
    _load_mutated(tmp_path, mutate, sub)


@pytest.mark.parametrize("mutate,sub", [
    (lambda d: d["faults"][0].__setitem__("onset", {"mode": "gradual"}), "ramp_cycles"),
    (lambda d: _set_fault(d, onset={"mode": "abrupt", "start_cycle": 50,
                                    "ramp_cycles": 200}), "ramp_cycles"),
    (lambda d: _set_fault(d, onset={"mode": "gradual", "start_cycle": 50,
                                    "ramp_cycles": 0}), "ramp_cycles"),
    (lambda d: _set_fault(d, onset={"mode": "gradual", "start_cycle": 50,
                                    "ramp_cycles": -1}), "ramp_cycles"),
    (lambda d: _set_fault(d, onset={"mode": "gradual", "start_cycle": 0,
                                    "ramp_cycles": 200}), "start_cycle"),
    (lambda d: _set_fault(d, onset={"mode": "step", "start_cycle": 50}), "mode"),
    (lambda d: d["faults"][0].pop("onset"), "onset"),
])
def test_validate_rejects_onset(tmp_path, mutate, sub):
    """Rifiuti onset: gradual senza ramp, ramp con abrupt, ramp <= 0, mode, start."""
    _load_mutated(tmp_path, mutate, sub)


def test_validate_rejects_duplicate_valve(tmp_path):
    """Due fault sulla stessa valvola: M2 ammette max 1 fault per valvola."""
    def mutate(d):
        d["faults"].append(
            {"fault_type": "closing_delay", "scope": "local", "valve_id": 2,
             "severity": 100, "onset": {"mode": "abrupt", "start_cycle": 50}})
    _load_mutated(tmp_path, mutate, "stessa valvola")


# --------------------------------------------------------------------------
# Severità di onset
# --------------------------------------------------------------------------
def test_onset_abrupt():
    """Abrupt: 0 prima di start_cycle, piena dopo (via severity_at)."""
    f = FaultSpec("restriction", "local", 5, 0.07, FaultOnset("abrupt", 100))
    assert severity_at(f, 0) == 0.0
    assert severity_at(f, 99) == 0.0
    assert severity_at(f, 100) == pytest.approx(0.07)
    assert severity_at(f, 101) == pytest.approx(0.07)
    assert severity_at(f, 10_000) == pytest.approx(0.07)


def test_onset_gradual():
    """Gradual: rampa lineare su ramp_cycles (via severity_at)."""
    f = FaultSpec("closing_delay", "local", 5, 100.0,
                  FaultOnset("gradual", 100, 200))
    assert severity_at(f, 99) == 0.0
    assert severity_at(f, 100) == pytest.approx(100.0 / 200.0)    # 1° passo
    assert severity_at(f, 199) == pytest.approx(50.0)             # metà rampa
    assert severity_at(f, 299) == pytest.approx(100.0)            # fine rampa
    assert severity_at(f, 300) == pytest.approx(100.0)            # satura


def test_faults_active_at():
    """faults_active_at(valve_id, cycle_id): attivi solo da start_cycle."""
    sc = Scenario(1, "attivi", None, [
        FaultSpec("restriction", "local", 2, 0.04, FaultOnset("abrupt", 100)),
        FaultSpec("closing_delay", "local", 3, 50.0, FaultOnset("abrupt", 100)),
    ])
    assert sc.faults_active_at(2, 99) == []
    assert sc.faults_active_at(2, 100) == [sc.faults[0]]
    assert sc.faults_active_at(3, 99) == []
    assert sc.faults_active_at(3, 150) == [sc.faults[1]]
    assert sc.faults_active_at(9, 10_000) == []     # valvola senza fault


def test_severity_at_boundaries():
    """Confini esatti: start_cycle-1 → 0; start_cycle → piena/1° passo; fine rampa."""
    abrupt = FaultSpec("restriction", "local", 1, 0.10, FaultOnset("abrupt", 100))
    gradual = FaultSpec("restriction", "local", 2, 0.10,
                        FaultOnset("gradual", 100, 200))
    assert severity_at(abrupt, 99) == 0.0
    assert severity_at(abrupt, 100) == pytest.approx(0.10)
    assert severity_at(gradual, 99) == 0.0
    assert severity_at(gradual, 100) == pytest.approx(0.10 / 200.0)
    assert severity_at(gradual, 100 + 200 - 1) == pytest.approx(0.10)
    assert severity_at(gradual, 299) == pytest.approx(0.10)


# --------------------------------------------------------------------------
# FaultEngine — costruzione e ciclo
# --------------------------------------------------------------------------
def test_start_cycle_one():
    """QA-F7: start_cycle=1 → iniezione PRE-APPLICATA alla costruzione."""
    cfg, plant = _make_plant()
    sc = Scenario(1, "start 1", None, [
        FaultSpec("restriction", "local", 4, 0.10, FaultOnset("abrupt", 1)),
        FaultSpec("closing_delay", "local", 7, 120.0, FaultOnset("abrupt", 1)),
        FaultSpec("opening_delay", "local", 9, 80.0, FaultOnset("abrupt", 1)),
    ])
    eng = FaultEngine(plant, sc, cfg)
    # restriction: fattore 1−s già nel plant
    assert plant._restriction[4] == pytest.approx(0.90)
    assert plant._restriction[7] == pytest.approx(1.0)     # valvole delay intatte
    # delay: mech già settati alla costruzione
    assert plant.mech[7].close_delay_ms == 120
    assert plant.mech[9].open_delay_ms == 80
    # GT del ciclo 1 marcato (confine §2, rec.cycle_id)
    gt = eng.on_cycle(_rec(4, 1))
    assert gt["fault_type"] == "restriction"
    assert gt["severity"] == pytest.approx(0.10)
    assert gt["valve_id"] == 4


def test_gt_mapping():
    """Mapping GT: sano (None/0.0) e guasto (fault_type/severity/valve_id)."""
    cfg, plant = _make_plant()
    sc = Scenario(7, "gt", None, [
        FaultSpec("restriction", "local", 12, 0.07, FaultOnset("abrupt", 100)),
    ])
    eng = FaultEngine(plant, sc, cfg)
    sano = eng.on_cycle(_rec(3, 99, ts_beg=500))
    assert sano["fault_type"] is None
    assert sano["severity"] == 0.0
    assert sano["valve_id"] == 3
    assert sano["cycle_id"] == 99 and sano["ts_beg"] == 500
    assert sano["scenario_id"] == 7
    # stessa valvola, prima del confine
    pre = eng.on_cycle(_rec(12, 99, ts_beg=600))
    assert pre["fault_type"] is None and pre["severity"] == 0.0
    # ciclo affetto: severità dal rec.cycle_id (nessun contatore engine)
    guasto = eng.on_cycle(_rec(12, 100, ts_beg=700))
    assert guasto["fault_type"] == "restriction"
    assert guasto["severity"] == pytest.approx(0.07)
    assert guasto["valve_id"] == 12
    assert guasto["ts_beg"] == 700
    assert guasto["machine_code"] == "valve12"


def test_cmd_events_derivation():
    """CMD:OPEN/CLOSE da record sintetico; invariante open = ts_beg+250."""
    cfg, plant = _make_plant()
    eng = FaultEngine(plant, Scenario(1, "cmd", None, []), cfg)
    rec = _rec(5, 42, ts_beg=1_000_000, ft=1910, reason="target")
    eng.on_cycle(rec)
    assert eng.has_events
    evs = eng.take_events()
    assert not eng.has_events
    names = [e[2] for e in evs]
    assert names == ["CMD:OPEN", "CMD:CLOSE"]
    op, cl = evs
    # open_cmd = ts_beg + flush_ms + pressurize_ms (200 + 50 = 250)
    assert op[0] == 1_000_000 + cfg.flush_ms + cfg.pressurize_ms
    assert op[0] == 1_000_250
    assert cl[0] == op[0] + 1910
    assert op[3] == "" and cl[3] == "target"
    assert op[4] == 42 and cl[4] == 42
    assert op[1] == "valve5" and cl[1] == "valve5"


def test_injection_timing():
    """on_cycle(record k) applica l'iniezione per il ciclo k+1."""
    cfg, plant = _make_plant()
    sc = Scenario(1, "timing", None, [
        FaultSpec("restriction", "local", 6, 0.07, FaultOnset("abrupt", 100)),
    ])
    eng = FaultEngine(plant, sc, cfg)
    eng.on_cycle(_rec(6, 98))
    assert plant._restriction[6] == pytest.approx(1.0)     # nessuna iniezione
    eng.on_cycle(_rec(6, 99))                              # k = start_cycle − 1
    assert plant._restriction[6] == pytest.approx(1.0 - 0.07)
    eng.on_cycle(_rec(6, 100))                             # già applicata
    assert plant._restriction[6] == pytest.approx(1.0 - 0.07)


def test_fault_events_derivation():
    """FAULT_START una volta per fault; FAULT_RAMP per ogni ciclo in rampa."""
    cfg, plant = _make_plant()
    sc = Scenario(1, "ev", None, [
        FaultSpec("restriction", "local", 8, 0.07,
                  FaultOnset("gradual", 100, 200)),
    ])
    eng = FaultEngine(plant, sc, cfg)
    evs = []
    for c in (98, 99, 100, 101, 299, 300):
        eng.on_cycle(_rec(8, c, ts_beg=10_000 + c))
        evs.extend(eng.take_events())
    starts = [e for e in evs if e[2] == "FAULT_START"]
    ramps = [e for e in evs if e[2] == "FAULT_RAMP"]
    assert len(starts) == 1
    assert starts[0][0] == 10_100 and starts[0][4] == 100
    assert starts[0][3] == "restriction severity=0.07 start_cycle=100"
    # rampa: un FAULT_RAMP per ciclo processato in rampa (100, 101, 299).
    # Confine (piano §2/§4): la rampa copre i cicli 100..299
    # (start_cycle + ramp_cycles − 1 = ultimo passo, severità piena) e
    # FAULT_RAMP è emesso per ogni ciclo in rampa; il ciclo 300 è fuori
    # rampa → nessun evento.
    assert len(ramps) == 3
    assert ramps[0][3].startswith("ramp severity=")
    assert float(ramps[0][3].split("=")[1]) == pytest.approx(0.07 / 200.0)
    assert ramps[-1][4] == 299
    assert ramps[-1][3] == "ramp severity=0.07"
    # i cicli pre-onset non emettono FAULT_START
    assert starts[0][4] == 100


def test_timeline_rows():
    """Timeline: 9 righe, fault_id 0-based, end null, start_ts primo ciclo affetto."""
    sc = _demo_scenario()
    cfg, plant = _make_plant()
    eng = FaultEngine(plant, sc, cfg)
    rows = eng.timeline()
    assert len(rows) == 9
    assert [r["fault_id"] for r in rows] == list(range(9))
    assert all(r["end_cycle"] is None and r["end_ts"] is None for r in rows)
    assert all(r["ramp_cycles"] == 200 and r["onset_mode"] == "gradual"
               for r in rows)
    # nessun ciclo processato: start_ts tutti None
    assert all(r["start_ts"] is None for r in rows)
    # processato il primo ciclo affetto del fault 0 (valve2, start_cycle 100)
    eng.on_cycle(_rec(2, 100, ts_beg=55_000))
    rows = eng.timeline()
    assert rows[0]["start_ts"] == 55_000
    assert rows[0]["scenario_id"] == sc.scenario_id
    assert rows[0]["fault_type"] == "restriction"
    assert rows[0]["valve_id"] == 2
    assert rows[0]["severity"] == 0.04
    assert all(r["start_ts"] is None for r in rows[1:])


def _demo_scenario() -> Scenario:
    """Scenario demo (9 fault gradual, §8) per i test dell'engine."""
    f = [
        FaultSpec("restriction", "local", 2, 0.04, FaultOnset("gradual", 100, 200)),
        FaultSpec("restriction", "local", 12, 0.07, FaultOnset("gradual", 100, 200)),
        FaultSpec("restriction", "local", 0, 0.12, FaultOnset("gradual", 100, 200)),
        FaultSpec("closing_delay", "local", 3, 50.0, FaultOnset("gradual", 100, 200)),
        FaultSpec("closing_delay", "local", 6, 100.0, FaultOnset("gradual", 100, 200)),
        FaultSpec("closing_delay", "local", 7, 150.0, FaultOnset("gradual", 100, 200)),
        FaultSpec("opening_delay", "local", 9, 40.0, FaultOnset("gradual", 100, 200)),
        FaultSpec("opening_delay", "local", 10, 80.0, FaultOnset("gradual", 100, 200)),
        FaultSpec("opening_delay", "local", 1, 120.0, FaultOnset("gradual", 100, 200)),
    ]
    return Scenario(42, "demo M2", 42, f)


# --------------------------------------------------------------------------
# Determinismo / isolamento
# --------------------------------------------------------------------------
def test_engine_determinism_no_rng():
    """Il modulo scenario.py non tocca MAI un RNG (grep) e l'engine è puro.

    Design ADR-0013: iniezioni deterministiche pure; nessun riferimento a
    numpy.random / rng / default_rng nel sorgente. Stesso input → stessi
    eventi (due istanze indipendenti, stesso seed).
    """
    src = (Path(__file__).resolve().parent.parent / "plcsim"
           / "scenario.py").read_text(encoding="utf-8")
    assert "numpy.random" not in src
    assert "rng" not in src
    assert "default_rng" not in src

    def sequenza():
        cfg, plant = _make_plant(seed=11)
        eng = FaultEngine(plant, _demo_scenario(), cfg)
        out = []
        for c in (50, 99, 100, 150, 250, 299, 300):
            out.append(eng.on_cycle(_rec(2, c, ts_beg=20_000 + c)))
            out.append(eng.take_events())
        out.append(eng.timeline())
        return out

    assert sequenza() == sequenza()


def test_stream_separation():
    """ADR-0013: i 2 stream di spawn(2) differiscono; spawn(3)[:2] ≡ spawn(2).

    Fatto VERO (piano §1.6, numpy 2.4.2, riprodotto in 3 review card): con
    due SeedSequence(42) FRESCHI e indipendenti, spawn(3)[:2] è byte-identico
    a spawn(2) — i figli con lo stesso indice sono identici per costruzione
    (spawn_key per indice). NOTA: spawn MUTA il pool del genitore
    (contatore cumulativo), quindi spawn(2) e poi spawn(3) sullo STESSO
    oggetto producono chiavi diverse ((0,),(1,) poi (2,),(3,)) — nel run
    reale ogni esecuzione crea un SeedSequence fresco e chiama spawn una
    volta sola, quindi la baseline non cambia passando a spawn(3) (piano
    §1.6c). M2 mantiene spawn(2): stream fisica e PLC.
    """
    a, b = np.random.SeedSequence(42).spawn(2)
    x, y, _ = np.random.SeedSequence(42).spawn(3)   # genitore FRESCO
    da = np.random.default_rng(a).standard_normal(1000)
    db = np.random.default_rng(b).standard_normal(1000)
    dx = np.random.default_rng(x).standard_normal(1000)
    dy = np.random.default_rng(y).standard_normal(1000)
    assert not np.array_equal(da, db)          # i 2 stream di spawn(2) differiscono
    assert np.array_equal(da, dx)              # spawn(3)[0] ≡ spawn(2)[0]
    assert np.array_equal(db, dy)              # spawn(3)[1] ≡ spawn(2)[1]
    # stati generate_state(64) identici per costruzione (genitori freschi)
    assert a.generate_state(64).tobytes() == x.generate_state(64).tobytes()
    assert b.generate_state(64).tobytes() == y.generate_state(64).tobytes()


def test_non_leakage_plc():
    """ADR-0012: il PLC non sa nulla di fault/GT; l'engine non riceve il PLC."""
    src = (Path(__file__).resolve().parent.parent / "plcsim"
           / "plc.py").read_text(encoding="utf-8")
    assert "scenario" not in src
    assert "FaultEngine" not in src
    params = list(inspect.signature(FaultEngine.__init__).parameters)
    assert params == ["self", "plant", "scenario", "cfg"]
