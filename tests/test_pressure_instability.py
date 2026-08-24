"""Test W1 (M3) — pressure_instability: parsing, engine group/global, plant channel.

Copre: caricamento YAML (m3_healthy + fixture inline group/global + rifiuti),
affected_valves, mapping GT e iniezione amp per i fault group/global (con
confini di onset e QA-F7 start_cycle=1), eventi FAULT_START/FAULT_RAMP con
note M3, timeline per-valvola, overlap gruppo/locale, canale Plant
(_amp_mult: scaling esatto dell'ampiezza del driver, identità x1.0,
zero draw RNG) e determinismo/isolation (grep sorgenti, ADR-0012/0013).

Harness: helper _make_plant/_rec riusati da tests/test_scenario.py; nessun
output in work/ (tutto su tmp_path).
"""
from __future__ import annotations

import copy
import inspect
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plcsim.plant import Plant                      # noqa: E402
from plcsim.scenario import (                       # noqa: E402
    FaultEngine, FaultOnset, FaultSpec, Scenario,
    affected_valves, load_scenario, severity_at,
)
from tests.test_scenario import _make_plant, _rec   # noqa: E402

SCAN_MS = 10       # passo di scan del PLC (config)
N_STEPS = 300      # 3 s di integrazione: apertura completa (tau_open 180 ms)
VALVE = 0          # valvola target dei test plant (valve0)
G2 = list(range(12, 18))    # cfg.groups[2] = [12..17] (default_groups(35, 6))


# --------------------------------------------------------------------------
# Loader YAML (M3)
# --------------------------------------------------------------------------
def test_load_healthy_yaml():
    """scenarios/m3_healthy.yaml: baseline sana M3 (faults == [])."""
    sc = load_scenario(ROOT / "scenarios" / "m3_healthy.yaml")
    assert sc.scenario_id == 50
    assert sc.name == "baseline sana M3 (equiv M2/M1)"
    assert sc.seed is None
    assert sc.faults == []


GROUP_YAML = """\
scenario_id: 51
name: demo M3 gruppo G2
seed: null
faults:
  - fault_type: pressure_instability
    scope: group
    group_id: 2
    severity: 0.5
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
"""

GLOBAL_YAML = """\
scenario_id: 52
name: demo M3 globale
seed: null
faults:
  - fault_type: pressure_instability
    scope: global
    severity: 0.3
    onset: {mode: abrupt, start_cycle: 50}
"""


def test_load_group_yaml(tmp_path):
    """Fixture inline: scope group, group_id 2, severity 0.5, gradual."""
    p = tmp_path / "m3_group.yaml"
    p.write_text(GROUP_YAML, encoding="utf-8")
    sc = load_scenario(p)
    assert sc.scenario_id == 51
    assert len(sc.faults) == 1
    f = sc.faults[0]
    assert f.fault_type == "pressure_instability"
    assert f.scope == "group"
    assert f.group_id == 2
    assert f.valve_id is None
    assert f.severity == 0.5
    assert f.onset.mode == "gradual"
    assert f.onset.start_cycle == 100
    assert f.onset.ramp_cycles == 200


def test_load_global_yaml(tmp_path):
    """Variante global: senza group_id né valve_id (entrambi None)."""
    p = tmp_path / "m3_global.yaml"
    p.write_text(GLOBAL_YAML, encoding="utf-8")
    sc = load_scenario(p)
    f = sc.faults[0]
    assert f.scope == "global"
    assert f.group_id is None
    assert f.valve_id is None
    assert f.severity == 0.3
    assert f.onset.mode == "abrupt" and f.onset.start_cycle == 50


BASE_M3 = {
    "scenario_id": 51,
    "name": "test rifiuti M3",
    "seed": None,
    "faults": [
        {"fault_type": "pressure_instability", "scope": "group",
         "group_id": 2, "severity": 0.5,
         "onset": {"mode": "abrupt", "start_cycle": 50}},
    ],
}


def _load_m3_mutated(tmp_path, mutate, expected_sub: str):
    d = copy.deepcopy(BASE_M3)
    mutate(d)
    p = tmp_path / "bad_m3.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_scenario(p)
    assert expected_sub in str(exc.value)


def _set_fault(d, **kw):
    d["faults"][0].update(kw)


@pytest.mark.parametrize("mutate,sub", [
    (lambda d: (_set_fault(d, scope="local"),
                d["faults"][0].pop("group_id")), "scope"),
    (lambda d: d["faults"][0].pop("group_id"), "group_id"),
    (lambda d: _set_fault(d, scope="local"), "group_id"),
    (lambda d: _set_fault(d, valve_id=3), "valve_id"),
    (lambda d: (_set_fault(d, scope="global"), _set_fault(d, valve_id=3)),
     "valve_id"),
    (lambda d: _set_fault(d, group_id=6), "group_id"),
    (lambda d: _set_fault(d, group_id=-1), "group_id"),
    (lambda d: _set_fault(d, severity=0.0), "severity"),
    (lambda d: _set_fault(d, severity=1.5), "severity"),
    (lambda d: _set_fault(d, scope="global"), "group_id"),
    (lambda d: _set_fault(d, fault_type="vibrazione"), "fault_type"),
])
def test_validate_rejects_pressure_instability(tmp_path, mutate, sub):
    """Rifiuti M3: scope, group_id, valve_id, severity, fault_type."""
    _load_m3_mutated(tmp_path, mutate, sub)


def test_validate_rejects_group_id_with_scope_local(tmp_path):
    """group_id con scope local → 'group_id' (check del gruppo, non scope)."""
    _load_m3_mutated(tmp_path,
                     lambda d: (_set_fault(d, scope="local"),
                                d["faults"][0].pop("group_id")), "scope")
    # con group_id PRESENTE l'errore è su group_id, non su scope
    _load_m3_mutated(tmp_path,
                     lambda d: _set_fault(d, scope="local"), "group_id")


# --------------------------------------------------------------------------
# affected_valves
# --------------------------------------------------------------------------
def test_affected_valves():
    """local → [valve_id]; group → cfg.groups[2] (12..17); global → 35."""
    cfg, _ = _make_plant()
    loc = FaultSpec("restriction", "local", 7, 0.1, FaultOnset("abrupt", 50))
    grp = FaultSpec("pressure_instability", "group", None, 0.5,
                    FaultOnset("abrupt", 50), group_id=2)
    glo = FaultSpec("pressure_instability", "global", None, 0.3,
                    FaultOnset("abrupt", 50))
    assert affected_valves(loc, cfg) == [7]
    assert affected_valves(grp, cfg) == cfg.groups[2] == G2
    assert affected_valves(glo, cfg) == list(range(35))
    with pytest.raises(ValueError, match="fuori range"):
        affected_valves(FaultSpec("pressure_instability", "group", None, 0.5,
                                  FaultOnset("abrupt", 50), group_id=6), cfg)


def _group_scenario(severity: float = 0.5, mode: str = "gradual",
                    start: int = 100, ramp: int = 200, scope: str = "group",
                    group_id: int | None = 2, sid: int = 51) -> Scenario:
    return Scenario(sid, "m3", None, [
        FaultSpec("pressure_instability", scope, None, severity,
                  FaultOnset(mode, start, ramp), group_id=group_id)])


# --------------------------------------------------------------------------
# FaultEngine — GT e iniezione (group/global)
# --------------------------------------------------------------------------
def test_engine_gt_group_rows():
    """GT group: sano fuori gruppo/confine; pressure_instability sui membri.

    on_cycle(valve11, 100) → None/0.0 (valvola fuori gruppo);
    on_cycle(valve12, 99) → None (prima del confine); on_cycle(valve12, 100)
    e on_cycle(valve17, 100) → pressure_instability con severity = 1° passo
    di rampa e valve_id del membro.
    """
    cfg, plant = _make_plant()
    eng = FaultEngine(plant, _group_scenario(), cfg)
    r = eng.on_cycle(_rec(11, 100))
    assert r["fault_type"] is None and r["severity"] == 0.0
    assert r["valve_id"] == 11 and r["machine_code"] == "valve11"
    r = eng.on_cycle(_rec(12, 99))
    assert r["fault_type"] is None and r["severity"] == 0.0
    r = eng.on_cycle(_rec(12, 100))
    assert r["fault_type"] == "pressure_instability"
    assert r["severity"] == pytest.approx(0.5 / 200.0)     # 1° passo rampa
    assert r["valve_id"] == 12 and r["machine_code"] == "valve12"
    r = eng.on_cycle(_rec(17, 100))
    assert r["fault_type"] == "pressure_instability"
    assert r["severity"] == pytest.approx(0.5 / 200.0)
    assert r["valve_id"] == 17
    assert r["scenario_id"] == 51


def test_engine_injection_group():
    """Iniezione amp: on_cycle(k) applica il ciclo k+1 ai membri del gruppo.

    Nessuna iniezione prima del confine; dopo il confine _amp_mult[v] ==
    1 + severity_at(k+1) per ogni membro; valve11 (fuori gruppo) resta 1.0.
    """
    cfg, plant = _make_plant()
    f = _group_scenario()
    eng = FaultEngine(plant, f, cfg)
    f0 = f.faults[0]
    eng.on_cycle(_rec(12, 98))
    assert plant._amp_mult[12] == 1.0                      # pre-confine: no-op
    assert all(plant._amp_mult[v] == 1.0 for v in range(35))
    # k = start_cycle − 1: iniezione per il ciclo 100 (1° passo rampa)
    eng.on_cycle(_rec(12, 99))
    assert plant._amp_mult[12] == pytest.approx(
        1.0 + severity_at(f0, 100))
    assert plant._amp_mult[11] == 1.0                      # fuori gruppo
    # gli altri membri vengono iniettati al loro ciclo k
    for v in (13, 14, 15, 16, 17):
        eng.on_cycle(_rec(v, 99))
    for v in G2:
        assert plant._amp_mult[v] == pytest.approx(
            1.0 + severity_at(f0, 100)), v
    assert plant._amp_mult[11] == 1.0
    # rampa monotona: dopo ogni ciclo k il moltiplicatore segue severity_at(k+1)
    for k in range(100, 110):
        eng.on_cycle(_rec(12, k))
        assert plant._amp_mult[12] == pytest.approx(
            1.0 + severity_at(f0, k + 1)), k


def test_engine_events_group():
    """Eventi group: FAULT_START 1× (nota con group_id=2); FAULT_RAMP sui
    cicli di rampa di CIASCUN membro; CMD:OPEN/CMD:CLOSE per ogni ciclo."""
    cfg, plant = _make_plant()
    eng = FaultEngine(plant, _group_scenario(), cfg)
    evs = []
    for v in G2:
        for c in (98, 99, 100, 101, 102):
            eng.on_cycle(_rec(v, c, ts_beg=1000 * v + c))
            evs.extend(eng.take_events())
    starts = [e for e in evs if e[2] == "FAULT_START"]
    ramps = [e for e in evs if e[2] == "FAULT_RAMP"]
    cmds = [e for e in evs if e[2].startswith("CMD:")]
    assert len(starts) == 1
    assert starts[0][1] == "valve12"                       # primo membro
    assert starts[0][0] == 1000 * 12 + 100
    assert starts[0][3] == ("pressure_instability severity=0.5 "
                            "start_cycle=100 group_id=2")
    # rampa: 3 cicli (100, 101, 102) × 6 membri
    assert len(ramps) == 6 * 3
    assert all(e[3].startswith("ramp severity=") for e in ramps)
    assert len(cmds) == 6 * 5 * 2
    # nessun FAULT_START per i cicli pre-onset
    assert starts[0][4] == 100


def test_engine_events_global():
    """Eventi global: FAULT_START con nota 'scope=global'."""
    cfg, plant = _make_plant()
    eng = FaultEngine(plant, _group_scenario(scope="global", group_id=None,
                                             severity=0.3, mode="abrupt",
                                             start=50), cfg)
    evs = []
    eng.on_cycle(_rec(5, 50, ts_beg=9000))
    evs.extend(eng.take_events())
    starts = [e for e in evs if e[2] == "FAULT_START"]
    assert len(starts) == 1
    assert starts[0][3] == ("pressure_instability severity=0.3 "
                            "start_cycle=50 scope=global")


def test_timeline_group_rows():
    """Timeline group: 6 righe (valve_id 12..17, fault_id 0, end null,
    start_ts per-valvola); global → 35 righe."""
    cfg, plant = _make_plant()
    eng = FaultEngine(plant, _group_scenario(), cfg)
    rows = eng.timeline()
    assert len(rows) == 6
    assert [r["valve_id"] for r in rows] == G2
    assert all(r["fault_id"] == 0 for r in rows)
    assert all(r["end_cycle"] is None and r["end_ts"] is None for r in rows)
    assert all(r["start_ts"] is None for r in rows)
    eng.on_cycle(_rec(12, 100, ts_beg=5000))
    eng.on_cycle(_rec(13, 100, ts_beg=6000))
    rows = eng.timeline()
    assert rows[0]["start_ts"] == 5000
    assert rows[1]["start_ts"] == 6000
    assert rows[2]["start_ts"] is None                    # membro non ancora
    assert rows[0]["fault_type"] == "pressure_instability"
    assert rows[0]["severity"] == 0.5
    assert rows[0]["start_cycle"] == 100
    # global → una riga per valvola (35)
    eng2 = FaultEngine(plant, _group_scenario(scope="global", group_id=None,
                                              severity=0.3, mode="abrupt",
                                              start=50), cfg)
    rows2 = eng2.timeline()
    assert len(rows2) == 35
    assert [r["valve_id"] for r in rows2] == list(range(35))


def test_engine_overlap_rejects():
    """Overlap sulla stessa valvola → ValueError alla costruzione engine.

    Locale su valve13 + gruppo G2 (che contiene 13) → rifiutato; due gruppi
    disgiunti (G2 + G3) → OK (l'overlap via gruppo si risolve qui, non in
    _parse che è None-safe sui valve_id).
    """
    cfg, plant = _make_plant()
    sc = Scenario(1, "overlap", None, [
        FaultSpec("restriction", "local", 13, 0.07, FaultOnset("abrupt", 50)),
        FaultSpec("pressure_instability", "group", None, 0.5,
                  FaultOnset("abrupt", 50), group_id=2),
    ])
    with pytest.raises(ValueError, match="stessa valvola"):
        FaultEngine(plant, sc, cfg)
    sc2 = Scenario(1, "gruppi disgiunti", None, [
        FaultSpec("pressure_instability", "group", None, 0.5,
                  FaultOnset("abrupt", 50), group_id=2),
        FaultSpec("pressure_instability", "group", None, 0.4,
                  FaultOnset("abrupt", 50), group_id=3),
    ])
    eng = FaultEngine(plant, sc2, cfg)                    # nessuna eccezione
    assert len(eng.timeline()) == 12


def test_start_cycle_one_group():
    """QA-F7: group start_cycle=1 → iniezione PRE-APPLICATA alla costruzione
    (_amp_mult[12..17] == 1+severity) e GT del ciclo 1 valorizzata."""
    cfg, plant = _make_plant()
    f = FaultSpec("pressure_instability", "group", None, 0.4,
                  FaultOnset("abrupt", 1), group_id=2)
    eng = FaultEngine(plant, Scenario(1, "start1", None, [f]), cfg)
    for v in G2:
        assert plant._amp_mult[v] == pytest.approx(1.4), v
    assert plant._amp_mult[11] == 1.0                      # fuori gruppo
    gt = eng.on_cycle(_rec(12, 1))
    assert gt["fault_type"] == "pressure_instability"
    assert gt["severity"] == pytest.approx(0.4)
    # gradual start=1: pre-applicato al 1° passo di rampa
    cfg2, plant2 = _make_plant()
    f2 = FaultSpec("pressure_instability", "group", None, 0.5,
                   FaultOnset("gradual", 1, 200), group_id=2)
    FaultEngine(plant2, Scenario(1, "start1g", None, [f2]), cfg2)
    for v in G2:
        assert plant2._amp_mult[v] == pytest.approx(
            1.0 + severity_at(f2, 1)), v


# --------------------------------------------------------------------------
# Canale Plant — _amp_mult
# --------------------------------------------------------------------------
def _sh_chain(p: Plant, i: int) -> float:
    """Replica ESATTA della catena 'sh' del plant (stesso ordine IEEE).

    sh = ((s·s)·s·a + s)·shape_gain·amp_mult — identica alla sequenza di
    step() fino a p_local = 1 + sh (np.multiply/np.add elementwise, nessun
    FMA): la replica sui buffer del plant (_s, _shape_a, _shape_gain,
    _amp_mult) coincide bit-a-bit con p._sh[i] − 1 DOPO step().
    """
    s = p._s[i]
    sh = s * s
    sh *= s
    sh *= p._shape_a
    sh += s
    sh *= p._shape_gain[i]
    sh *= p._amp_mult[i]
    return sh


def test_amp_mult_scales_pressure():
    """M3: _amp_mult[i] scala ESATTAMENTE l'ampiezza del driver lento.

    Due plant stesso seed, stessa sequenza di step (valvola aperta); l'unica
    differenza è _amp_mult[i] = 1.5. La replica IEEE della catena sh deve
    coincidere bit-a-bit con la p_local del plant (p._sh) e la deviazione
    dalla baseline +1 scala ESATTAMENTE di m.

    NOTA (semantica fisica): la PORTATA non scala di 1.5 — p_local = 1 +
    amp·shaped ha l'offset +1: anche in aritmetica esatta il rapporto è
    (1+1.5x)/(1+x) ≈ 1±3% (x = amp·shaped ∈ ±0.06), NON 1.5. A scalare
    esattamente di m è l'AMPIEZZA (la deviazione p_local − 1).
    """
    _, p_ok = _make_plant(42)
    _, p_f = _make_plant(42)
    p_f._amp_mult[VALVE] = 1.5
    t0 = 0
    p_ok.mech[VALVE].begin_open(t0)
    p_f.mech[VALVE].begin_open(t0)
    diff_scans = 0
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_ok.step(t)
        p_f.step(t)
        i = VALVE
        sh_ok = _sh_chain(p_ok, i)
        assert p_ok._sh[i] == 1.0 + sh_ok                  # m=1: p_local esatta
        sh_f = _sh_chain(p_f, i)
        assert p_f._sh[i] == 1.0 + sh_f                    # p_local iniettata
        assert sh_f == sh_ok * 1.5             # deviazione (ampiezza) x1.5 ESATTA
        assert p_f._sh[i] != p_ok._sh[i]                   # iniezione attiva
        # portata: q = fb·f·p_local·z (buffer del plant) — uguaglianza esatta
        for p in (p_ok, p_f):
            q = p._flow_base[i] * p._f[i]
            q *= p._sh[i]
            q *= p._z[i]
            assert p.flow_now[i] == q
        if p_f.flow_now[i] != p_ok.flow_now[i]:
            diff_scans += 1
            r = p_f.flow_now[i] / p_ok.flow_now[i]
            assert 0.95 < r < 1.05, r                      # offset +1: ≈1±3%
    assert diff_scans > 0                                  # confronto non banale
    # le altre valvole (fattore 1.0) restano identiche
    np.testing.assert_array_equal(p_f.flow_now[1:], p_ok.flow_now[1:])


def test_amp_mult_identity():
    """amp_mult = 1.0 → step() byte-identico al plant senza iniezione.

    x1.0 è esatto in IEEE: stessa sequenza di step (stesso seed, valvola
    aperta, chiusura con gli stessi draw jitter/snap) → flow_now,
    volume_carry e last_pulses IDENTICI bit-a-bit (fondamento AC-5/AC-6/AC-9
    anche per M3: le valvole sane dei run M3 restano identiche al sano).
    """
    _, p_ok = _make_plant(42)
    _, p_f = _make_plant(42)
    p_f._amp_mult[:] = 1.0       # identità esplicita (default: np.ones)
    t0 = 0
    p_ok.mech[VALVE].begin_open(t0)
    p_f.mech[VALVE].begin_open(t0)
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_ok.step(t)
        p_f.step(t)
    t = t0 + N_STEPS * SCAN_MS
    p_ok.mech[VALVE].begin_close(t, p_ok.rng)
    p_f.mech[VALVE].begin_close(t, p_f.rng)
    for k in range(120):
        t = t0 + (N_STEPS + 1 + k) * SCAN_MS
        p_ok.step(t)
        p_f.step(t)
    np.testing.assert_array_equal(p_f.flow_now, p_ok.flow_now)
    np.testing.assert_array_equal(p_f.volume_carry, p_ok.volume_carry)
    np.testing.assert_array_equal(p_f.last_pulses, p_ok.last_pulses)


def _assert_same_rng_state(a: np.random.Generator, b: np.random.Generator
                           ) -> None:
    """Stato del bit_generator identico (helper, stesso pattern di
    test_fault_engine._assert_same_rng_state)."""
    sa, sb = a.bit_generator.state, b.bit_generator.state
    assert sa["bit_generator"] == sb["bit_generator"]
    assert sa["has_uint32"] == sb["has_uint32"]
    assert sa["uinteger"] == sb["uinteger"]
    assert sa["state"].keys() == sb["state"].keys()
    for key in sa["state"]:
        va, vb = sa["state"][key], sb["state"][key]
        if isinstance(va, np.ndarray):
            np.testing.assert_array_equal(va, vb)
        else:
            assert va == vb


def test_amp_mult_zero_rng_draws():
    """L'iniezione M3 NON consuma stream RNG (ADR-0013): dopo N step lo
    stato del bit_generator è identico con e senza _amp_mult (moltiplicatore
    puro nel loop caldo, zero draw aggiuntivi)."""
    _, p_a = _make_plant(11)
    _, p_b = _make_plant(11)
    p_b._amp_mult[3] = 1.5
    p_a.mech[3].begin_open(0)
    p_b.mech[3].begin_open(0)
    for k in range(200):
        t = (k + 1) * SCAN_MS
        p_a.step(t)
        p_b.step(t)
    _assert_same_rng_state(p_a.rng, p_b.rng)


# --------------------------------------------------------------------------
# Determinismo / isolamento (M3)
# --------------------------------------------------------------------------
def test_engine_determinism_no_rng():
    """Il modulo scenario.py non tocca MAI un RNG anche con M3 (grep) e
    l'engine group è puro: stesso input → stessi eventi/timeline (due
    istanze indipendenti, stesso seed)."""
    src = (ROOT / "plcsim" / "scenario.py").read_text(encoding="utf-8")
    assert "numpy.random" not in src
    assert "rng" not in src
    assert "default_rng" not in src

    def sequenza():
        cfg, plant = _make_plant(seed=11)
        eng = FaultEngine(plant, _group_scenario(), cfg)
        out = []
        for v in G2:
            for c in (50, 99, 100, 150, 250, 299, 300):
                out.append(eng.on_cycle(_rec(v, c, ts_beg=20_000 + c)))
                out.append(eng.take_events())
        out.append(eng.timeline())
        return out

    assert sequenza() == sequenza()


def test_non_leakage_plc():
    """ADR-0012: il PLC non sa nulla di fault/GT/M3; l'engine non riceve il
    PLC e i parametri di FaultEngine.__init__ restano invariati."""
    src = (ROOT / "plcsim" / "plc.py").read_text(encoding="utf-8")
    assert "scenario" not in src
    assert "FaultEngine" not in src
    assert "amp_mult" not in src
    params = list(inspect.signature(FaultEngine.__init__).parameters)
    assert params == ["self", "plant", "scenario", "cfg"]
