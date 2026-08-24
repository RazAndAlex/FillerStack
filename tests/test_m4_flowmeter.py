"""Test W1 — classe Flowmeter in plcsim/sensors.py (guasti flowmeter M4).

Unit sulla fisica sensori (layer 3): l'iniezione avviene sul canale
osservabile plant.last_pulses, tra plant.step(t) e plc.scan(t) — nel run
reale la chiama il wrapper dell'engine (onda W2). Qui la Flowmeter è usata
isolata con un Plant seedato (harness di test_engine, come test_fault_engine):
- identità: maschere a 0 -> zero draw RNG, zero modifiche (AC-M4-5)
- stream:   sensori = spawn(3)[2], separato da fisica/PLC (ADR-0013)
- dropout:  azzera gli impulsi dello scan (frazione s di scan persi);
            volume_carry intatto (il mismatch volume/impulsi è la firma)
- glitch:   +1 impulso spurio (tasso s di scan spuri); volume_carry intatto
- backstop: finestra post-close di 1000 ms sugli spuri (protegge TAIL dal
            livelock del SilenceTimer)
- late:     impulsi oltre la finestra di silenzio -> evento LATE_PULSE
Sezione engine (W2): i tipi flowmeter_dropout/flowmeter_glitch nel fault
engine di scenario.py — parsing/validazione YAML, severità di onset,
iniezione maschere per-valvola, GT/eventi/timeline type-agnostici,
wrapper per-scan (installato SOLO con fault flowmeter: bit-identità
M4-sano ≡ M3) e per-scan dropout/glitch + LATE_PULSE via engine.
Nessun output su work/: i test non scrivono file. Esegue dalla root del repo.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from plcsim.clock import SimulationClock                     # noqa: E402
from plcsim.config import SimConfig                          # noqa: E402
from plcsim.plant import Plant                               # noqa: E402
from plcsim.scenario import (                                # noqa: E402
    FaultEngine, FaultOnset, FaultSpec, Scenario,
    load_scenario, severity_at,
)
from plcsim.sensors import FLOWMETER_BACKSTOP_MS, Flowmeter  # noqa: E402
from plcsim.validation import CycleRecord                    # noqa: E402
from test_engine import make_cfg, TPL, END_MS                # noqa: E402

SCAN_MS = 10     # passo di scan del PLC (config)
N_STEPS = 300    # 3 s di integrazione: apertura completa (tau_open 180 ms)
VALVE = 0        # valvola target dei test (valve0, flow_base ~135 ml/s)


def _make_plant(seed: int = 42):
    """Plant isolato senza Flowmeter (harness di test_engine, stesso seed)."""
    cfg = make_cfg(seed)
    plant = Plant(cfg, SimulationClock(), np.random.default_rng(seed))
    return cfg, plant


def _make_flowmeter(seed: int = 42):
    """Plant isolato + Flowmeter collegata (stesso seed per plant e cfg)."""
    cfg, plant = _make_plant(seed)
    fm = Flowmeter(cfg, plant)
    return cfg, plant, fm


def _state_snapshot(g: np.random.Generator) -> dict:
    """Snapshot profondo dello stato del bit_generator (nested ndarray)."""
    return copy.deepcopy(g.bit_generator.state)


def _assert_state_identical(snap: dict, g: np.random.Generator) -> None:
    """Lo stato corrente del bit_generator è identico allo snapshot."""
    cur = g.bit_generator.state
    assert snap["bit_generator"] == cur["bit_generator"]
    assert snap["has_uint32"] == cur["has_uint32"]
    assert snap["uinteger"] == cur["uinteger"]
    assert snap["state"].keys() == cur["state"].keys()
    for key in snap["state"]:
        va, vb = snap["state"][key], cur["state"][key]
        if isinstance(va, np.ndarray):
            np.testing.assert_array_equal(va, vb)
        else:
            assert va == vb


def test_flowmeter_identity_no_draws():
    """Maschere a 0: percorso sano = zero draw e zero modifiche (AC-M4-5).

    Senza setter chiamati, la guardia _active resta False: apply() esce a
    costo quasi zero — lo stato del bit_generator dello stream sensori resta
    INVARIATO dopo N scan e last_pulses/volume_carry del plant con
    Flowmeter sono byte-identici a quelli di un plant senza Flowmeter
    (stesso seed, stessi step).
    """
    seed = 42
    _, p_ok = _make_plant(seed)            # plant senza Flowmeter
    _, p_fm, fm = _make_flowmeter(seed)    # plant con Flowmeter (maschere a 0)
    assert not fm._active
    assert fm._active_valves == []
    state = _state_snapshot(fm._stream)
    t0 = 0
    p_ok.mech[VALVE].begin_open(t0)
    p_fm.mech[VALVE].begin_open(t0)
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_ok.step(t)
        p_fm.step(t)
        assert fm.apply(t) == []           # nessun evento, nessun draw
    _assert_state_identical(state, fm._stream)
    np.testing.assert_array_equal(p_fm.last_pulses, p_ok.last_pulses)
    np.testing.assert_array_equal(p_fm.volume_carry, p_ok.volume_carry)


def test_setters_keep_active_list():
    """I setter mantengono la guardia e la lista delle valvole attive (W2).

    set_dropout/set_glitch alzano il flag _active quando s > 0 e lo
    abbassano quando tutte le maschere tornano a 0; la lista è ordinata
    per indice (ordine di draw deterministico).
    """
    _, _, fm = _make_flowmeter(42)
    assert not fm._active
    fm.set_dropout(7, 0.3)
    assert fm._active and fm._active_valves == [7]
    fm.set_glitch(2, 0.5)
    assert fm._active_valves == [2, 7]     # ordine per indice, no duplicati
    fm.set_dropout(7, 0.0)
    assert fm._active_valves == [2]
    fm.set_glitch(2, 0.0)
    assert not fm._active
    assert fm._active_valves == []


def test_stream_separation_sensors():
    """Lo stream sensori è spawn(3)[2], separato da fisica (0) e PLC (1).

    Pattern di test_scenario.py:495-519: con un SeedSequence(seed) FRESCO,
    spawn(3)[2] è la terza chiave — i primi 1000 draw della Flowmeter sono
    identici a quelli di default_rng(SeedSequence(seed).spawn(3)[2]) e
    diversi dagli stream spawn(2)[0]/[1] usati da build_sim (fisica/PLC).
    """
    seed = 42
    _, _, fm = _make_flowmeter(seed)
    a, b = np.random.SeedSequence(seed).spawn(2)         # fisica, PLC
    _, _, s3 = np.random.SeedSequence(seed).spawn(3)    # genitore FRESCO
    ref = np.random.default_rng(s3)
    seq = fm._stream.random(1000)
    np.testing.assert_array_equal(seq, ref.random(1000))
    fa = np.random.default_rng(a).random(1000)
    fb = np.random.default_rng(b).random(1000)
    assert not np.array_equal(seq, fa)                  # diverso da fisica
    assert not np.array_equal(seq, fb)                  # diverso da PLC
    assert not np.array_equal(fa, fb)


def test_dropout_keeps_carry_loses_pulses():
    """Dropout: perde impulsi OSSERVATI, la fisica (volume_carry) resta intatta.

    (a) s=1.0 -> deterministico: ZERO impulsi osservati su N scan mentre
        volume_carry cresce identico al plant sano (confronto esatto — il
        dropout non tocca la fisica; il mismatch volume/impulsi è la firma
        del guasto).
    (b) s=0.3 su 1000 scan -> somma osservata in [0.55, 0.85] x somma sana
        (Bernoulli per scan, seed fisso) e volume_carry IDENTICO al sano
        (bit-a-bit).
    """
    seed = 42
    # (a) severità totale: nessun impulso osservato, carry identico
    _, p_ok = _make_plant(seed)
    _, p_f, fm = _make_flowmeter(seed)
    fm.set_dropout(VALVE, 1.0)
    t0 = 0
    p_ok.mech[VALVE].begin_open(t0)
    p_f.mech[VALVE].begin_open(t0)
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_ok.step(t)
        p_f.step(t)
        fm.apply(t)
        assert p_f.last_pulses[VALVE] == 0      # tutti gli impulsi persi
    np.testing.assert_array_equal(p_f.volume_carry, p_ok.volume_carry)
    # (b) severità parziale: somma osservata in banda attesa, carry bit-identico
    _, p_ok2 = _make_plant(seed)
    _, p_f2, fm2 = _make_flowmeter(seed)
    fm2.set_dropout(VALVE, 0.3)
    p_ok2.mech[VALVE].begin_open(t0)
    p_f2.mech[VALVE].begin_open(t0)
    n_steps = 1000
    sum_ok = 0
    sum_f = 0
    for k in range(n_steps):
        t = t0 + (k + 1) * SCAN_MS
        p_ok2.step(t)
        p_f2.step(t)
        fm2.apply(t)
        sum_ok += int(p_ok2.last_pulses[VALVE])
        sum_f += int(p_f2.last_pulses[VALVE])
    assert sum_ok > 0
    assert 0.55 * sum_ok <= sum_f <= 0.85 * sum_ok
    np.testing.assert_array_equal(p_f2.volume_carry, p_ok2.volume_carry)


def test_glitch_adds_spurious_without_carry():
    """Glitch: +1 impulso spurio per scan; volume_carry mai toccato.

    glitch[0]=1.0 -> deterministico: somma osservata = somma sana + N_STEPS
    (un spurio per scan, valvola aperta pre-close: gate libero) e
    volume_carry identico al sano (bit). glitch[0]=0.05 -> la somma supera
    quella sana (direzione: il sensore conta più della fisica).
    """
    seed = 42
    # s = 1.0: spurio deterministico a ogni scan
    _, p_ok = _make_plant(seed)
    _, p_f, fm = _make_flowmeter(seed)
    fm.set_glitch(VALVE, 1.0)
    t0 = 0
    p_ok.mech[VALVE].begin_open(t0)
    p_f.mech[VALVE].begin_open(t0)
    sum_ok = 0
    sum_f = 0
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_ok.step(t)
        p_f.step(t)
        fm.apply(t)
        sum_ok += int(p_ok.last_pulses[VALVE])
        sum_f += int(p_f.last_pulses[VALVE])
    assert sum_f == sum_ok + N_STEPS
    np.testing.assert_array_equal(p_f.volume_carry, p_ok.volume_carry)
    # s = 0.05: direzione (somma osservata > somma sana, seed fisso)
    _, p_ok3 = _make_plant(seed)
    _, p_f3, fm3 = _make_flowmeter(seed)
    fm3.set_glitch(VALVE, 0.05)
    p_ok3.mech[VALVE].begin_open(t0)
    p_f3.mech[VALVE].begin_open(t0)
    sum_ok3 = 0
    sum_f3 = 0
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_ok3.step(t)
        p_f3.step(t)
        fm3.apply(t)
        sum_ok3 += int(p_ok3.last_pulses[VALVE])
        sum_f3 += int(p_f3.last_pulses[VALVE])
    assert sum_f3 > sum_ok3


def test_glitch_backstop_limits_tail():
    """Backstop: gli spuri post-close si fermano dopo FLOWMETER_BACKSTOP_MS.

    Con glitch[0]=1.0 e nessun record emesso, il gate permette spuri solo
    nella finestra [close, close + BACKSTOP): dopo il backstop ZERO spuri
    finché il record non viene emesso (primo scan senza impulsi con
    gap >= cfg.silence_ms), poi gli spuri riprendono come late pulses.
    """
    seed = 42
    _, p, fm = _make_flowmeter(seed)
    fm.set_glitch(VALVE, 1.0)
    t0 = 0
    p.mech[VALVE].begin_open(t0)
    for k in range(50):                      # FILLING: spuri liberi pre-close
        t = t0 + (k + 1) * SCAN_MS
        p.step(t)
        fm.apply(t)
    close_t = 50 * SCAN_MS
    p.mech[VALVE].begin_close(close_t, p.rng)
    spur_times = []
    rec_at = None
    for k in range(250):                     # fino a close + 2500 ms
        t = close_t + (k + 1) * SCAN_MS
        p.step(t)
        before = int(p.last_pulses[VALVE])
        fm.apply(t)
        after = int(p.last_pulses[VALVE])
        if after == before + 1:              # spurio: +1 esatto rispetto al plant
            spur_times.append(t)
        if fm._record_emitted[VALVE] and rec_at is None:
            rec_at = t
    assert spur_times                        # coda con spuri attivi
    assert spur_times[0] < close_t + FLOWMETER_BACKSTOP_MS
    assert rec_at is not None                # il record viene emesso
    # gli spuri pre-record vivono SOLO nella finestra backstop
    pre_rec = [t for t in spur_times if t < rec_at]
    assert pre_rec
    assert max(pre_rec) < close_t + FLOWMETER_BACKSTOP_MS   # ultimo spurio
    gated = [t for t in spur_times
             if close_t + FLOWMETER_BACKSTOP_MS <= t < rec_at]
    assert gated == []                       # ZERO spuri tra backstop e record
    after_rec = [t for t in spur_times if t > rec_at]
    assert after_rec                         # dopo il record riprendono (late)


def test_late_pulse_detection():
    """Record emesso -> impulsi oltre la finestra di silenzio sono LATE.

    Ciclo completo (glitch[0]=1.0): apertura, chiusura, spuri in coda
    (gap = 10 ms <= silence: MAI late), backstop, record emesso al primo scan
    senza impulsi con gap >= silence, poi gli spuri riprendono e producono
    eventi LATE_PULSE (n_pulses=1, late_count++) a ogni scan. Un impulso a
    gap ESATTAMENTE silence_ms NON è late (strict >): è ancora un impulso di
    coda per il PLC — il record viene emesso solo allo scan successivo senza
    impulsi. Il flag si azzera al begin_open successivo (close_start torna -1).
    """
    seed = 42
    cfg, p, fm = _make_flowmeter(seed)
    fm.set_glitch(VALVE, 1.0)
    t0 = 0
    p.mech[VALVE].begin_open(t0)
    for k in range(50):                      # FILLING: spuri liberi, gap = 10 ms
        t = t0 + (k + 1) * SCAN_MS
        p.step(t)
        assert fm.apply(t) == []
    close_t = 50 * SCAN_MS
    p.mech[VALVE].begin_close(close_t, p.rng)
    # (A) coda con spuri (gap = 10 ms <= silence): nessun late, nessun record
    for k in range(99):                      # t = 510..1490 (finestra backstop)
        t = close_t + (k + 1) * SCAN_MS
        p.step(t)
        assert fm.apply(t) == []
    assert fm.late_count[VALVE] == 0
    assert not fm._record_emitted[VALVE]
    # (B) tratto backstop senza impulsi (gap 10..140 ms): ancora nessun record
    for k in range(99, 113):                 # t = 1500..1630
        t = close_t + (k + 1) * SCAN_MS
        p.step(t)
        assert fm.apply(t) == []
    assert fm.late_count[VALVE] == 0
    assert not fm._record_emitted[VALVE]
    # (C) impulso a gap ESATTAMENTE silence_ms (t=1640, gap=150): NON late
    t = close_t + 114 * SCAN_MS
    p.step(t)
    p.last_pulses[VALVE] = 1                 # impulso di coda al confine
    assert fm.apply(t) == []
    assert fm.late_count[VALVE] == 0
    assert not fm._record_emitted[VALVE]     # record rimandato (no_pulse False)
    # (D) silenzio: record emesso al primo scan con gap >= silence (t=1790)
    for k in range(114, 129):                # t = 1650..1790
        t = close_t + (k + 1) * SCAN_MS
        p.step(t)
        assert fm.apply(t) == []
    assert fm._record_emitted[VALVE]
    # (E) impulsi post-record: LATE a ogni scan (gap > silence, flag attivo)
    for k in range(129, 132):                # t = 1800..1820
        t = close_t + (k + 1) * SCAN_MS
        p.step(t)
        events = fm.apply(t)
        assert len(events) == 1
        assert events[0] == (t, cfg.valves[VALVE].machine_code,
                             "LATE_PULSE", "n_pulses=1",
                             int(fm._cycle_cnt[VALVE]))
    assert fm.late_count[VALVE] == 3
    # (F) begin_open successivo: close_start torna -1 -> flag azzerato
    p.mech[VALVE].begin_open(2000)
    p.step(2010)
    assert fm.apply(2010) == []
    assert not fm._record_emitted[VALVE]


def test_late_pulse_not_in_tail():
    """Coda normale (impulsi a gap < silence): MAI late.

    Glitch attivo: in coda ogni scan ha un impulso (gap = 10 ms < silence)
    — la coda è ancora viva per il PLC; nessun record emesso, nessun evento,
    nessun conteggio. Anche nel tratto [backstop, record) senza impulsi
    (gap < silence) nulla viene contato.
    """
    seed = 42
    _, p, fm = _make_flowmeter(seed)
    fm.set_glitch(VALVE, 1.0)
    t0 = 0
    p.mech[VALVE].begin_open(t0)
    for k in range(50):
        t = t0 + (k + 1) * SCAN_MS
        p.step(t)
        fm.apply(t)
    close_t = 50 * SCAN_MS
    p.mech[VALVE].begin_close(close_t, p.rng)
    for k in range(113):                     # t = 510..1630 (coda + backstop)
        t = close_t + (k + 1) * SCAN_MS
        p.step(t)
        assert fm.apply(t) == []
    assert fm.late_count[VALVE] == 0
    assert not fm._record_emitted[VALVE]


def test_backstop_constant_documented():
    """FLOWMETER_BACKSTOP_MS == 1000: valore di design del piano M4 (R3).

    Il backstop è il limite assoluto della finestra di iniezione post-close:
    non è mai raggiunto in un ciclo sano (TT max reale 422 ms < 1000 ms), ma
    in caso di glitch in coda impedisce al SilenceTimer di venire spazzato
    all'infinito (livelock, piano §6 R3).
    """
    assert FLOWMETER_BACKSTOP_MS == 1000


# ---------------------------------------------------------------------------
# W2 — fault engine M4 (scenario.py): flowmeter_dropout / flowmeter_glitch
# ---------------------------------------------------------------------------
# Pattern di test_scenario.py: fixture YAML inline su tmp_path con
# load_scenario; CycleRecord sintetico via _rec; engine su plant isolato.

M4_DROPOUT_YAML = """\
scenario_id: 43
name: flowmeter dropout valve12
seed: 42
faults:
  - fault_type: flowmeter_dropout
    scope: local
    valve_id: 12
    severity: 0.30
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
"""

M4_GLITCH_YAML = """\
scenario_id: 44
name: flowmeter glitch valve12
seed: 42
faults:
  - fault_type: flowmeter_glitch
    scope: local
    valve_id: 12
    severity: 0.05
    onset: {mode: gradual, start_cycle: 100, ramp_cycles: 200}
"""

BASE_M4 = {
    "scenario_id": 43,
    "name": "test rifiuti m4",
    "seed": None,
    "faults": [
        {"fault_type": "flowmeter_dropout", "scope": "local",
         "valve_id": 12, "severity": 0.30,
         "onset": {"mode": "abrupt", "start_cycle": 50}},
    ],
}


def _engine_plant(seed: int = 42):
    """Plant isolato per i test dell'engine (pattern test_scenario)."""
    cfg = SimConfig.build(seed=seed)
    plant = Plant(cfg, SimulationClock(), np.random.default_rng(seed))
    return cfg, plant


def _rec(vi: int, cycle_id: int, ts_beg: int = 1_000_000, ft: int = 1910,
         tt: int = 300, tp: int = 25, pc: int = 2500,
         reason: str = "target") -> CycleRecord:
    """CycleRecord sintetico (stessi campi di validation.complete_cycle)."""
    return CycleRecord(
        machine_code=f"valve{vi}", ts_beg=ts_beg, fillingtime=ft, tailtime=tt,
        tailpulse=tp, pulsecount=pc, target=2500, deltapulse=2500 - pc,
        filling_step_out=20, fillingok=True, fill_quality_ok=True,
        sequence_ok=True, sample_valid=True, diagnostic_status="NORMAL",
        close_reason=reason, cycle_id=cycle_id,
    )


def _load_mutated_m4(tmp_path, mutate, expected_sub: str):
    """Scrive BASE_M4 mutato e verifica che load_scenario rifiuti (W2)."""
    d = copy.deepcopy(BASE_M4)
    mutate(d)
    p = tmp_path / "bad_m4.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_scenario(p)
    assert expected_sub in str(exc.value)


def test_load_m4_dropout_yaml(tmp_path):
    """YAML flowmeter_dropout: parse ok, campi esatti (valvola/severità/onset)."""
    p = tmp_path / "m4_dropout.yaml"
    p.write_text(M4_DROPOUT_YAML, encoding="utf-8")
    sc = load_scenario(p)
    assert sc.scenario_id == 43
    assert sc.name == "flowmeter dropout valve12"
    assert sc.seed == 42
    assert len(sc.faults) == 1
    f = sc.faults[0]
    assert f.fault_type == "flowmeter_dropout"
    assert f.scope == "local"
    assert f.valve_id == 12
    assert f.severity == 0.30
    assert f.onset.mode == "gradual"
    assert f.onset.start_cycle == 100
    assert f.onset.ramp_cycles == 200


def test_load_m4_glitch_yaml(tmp_path):
    """YAML flowmeter_glitch: parse ok, campi esatti (valvola/severità/onset)."""
    p = tmp_path / "m4_glitch.yaml"
    p.write_text(M4_GLITCH_YAML, encoding="utf-8")
    sc = load_scenario(p)
    assert sc.scenario_id == 44
    assert sc.name == "flowmeter glitch valve12"
    assert sc.seed == 42
    assert len(sc.faults) == 1
    f = sc.faults[0]
    assert f.fault_type == "flowmeter_glitch"
    assert f.scope == "local"
    assert f.valve_id == 12
    assert f.severity == 0.05
    assert f.onset.mode == "gradual"
    assert f.onset.start_cycle == 100
    assert f.onset.ramp_cycles == 200


@pytest.mark.parametrize("severity", [0, 1.5, True, "x"])
def test_validate_rejects_flowmeter_severity(tmp_path, severity):
    """Severità flowmeter deve essere in (0,1]: 0, >1, bool, stringa rifiutati."""
    def mutate(d):
        d["faults"][0]["severity"] = severity
    _load_mutated_m4(tmp_path, mutate, "severity")


def test_validate_rejects_flowmeter_group_scope(tmp_path):
    """I tipi flowmeter sono local-only: scope group rifiutato (ramo else)."""
    def mutate(d):
        d["faults"][0]["scope"] = "group"
        d["faults"][0]["group_id"] = 1
    _load_mutated_m4(tmp_path, mutate, "scope deve essere 'local'")


def test_validate_rejects_duplicate_valve_m4(tmp_path):
    """dropout + glitch sulla STESSA valvola: due fault sulla stessa valvola."""
    def mutate(d):
        d["faults"].append(
            {"fault_type": "flowmeter_glitch", "scope": "local",
             "valve_id": 12, "severity": 0.05,
             "onset": {"mode": "abrupt", "start_cycle": 50}})
    _load_mutated_m4(tmp_path, mutate, "stessa valvola")


def test_severity_at_flowmeter():
    """Rampa gradual dei tipi flowmeter: identica agli altri tipi (severity_at)."""
    dropout = FaultSpec("flowmeter_dropout", "local", 12, 0.30,
                        FaultOnset("gradual", 100, 200))
    glitch = FaultSpec("flowmeter_glitch", "local", 12, 0.05,
                       FaultOnset("abrupt", 100))
    assert severity_at(dropout, 99) == 0.0
    assert severity_at(dropout, 100) == pytest.approx(0.30 / 200.0)   # 1° passo
    assert severity_at(dropout, 100 + 200 - 1) == pytest.approx(0.30)  # fine rampa
    assert severity_at(dropout, 300) == pytest.approx(0.30)            # satura
    assert severity_at(glitch, 99) == 0.0
    assert severity_at(glitch, 100) == pytest.approx(0.05)
    assert severity_at(glitch, 10_000) == pytest.approx(0.05)


def test_apply_dropout_sets_flowmeter_mask():
    """_apply flowmeter_dropout: maschera via setter; monotonia (niente retrocesse).

    on_cycle(record 99) applica l'iniezione per il ciclo 100 (abrupt -> piena);
    la riapplicazione con severità minore NON retrocede (rampa monotona).
    """
    cfg, plant = _engine_plant()
    sc = Scenario(9, "m4 dropout", None, [
        FaultSpec("flowmeter_dropout", "local", 12, 0.30,
                  FaultOnset("abrupt", 100)),
    ])
    eng = FaultEngine(plant, sc, cfg)
    assert eng._flowmeter.dropout[12] == 0.0          # start_cycle 100: nessuna QA-F7
    eng.on_cycle(_rec(12, 99))                        # k = start_cycle − 1
    assert eng._flowmeter.dropout[12] == pytest.approx(0.30)
    eng.on_cycle(_rec(12, 100))                       # già applicata
    assert eng._flowmeter.dropout[12] == pytest.approx(0.30)
    eng._apply(sc.faults[0], 0.10)                    # severità minore: NON retrocede
    assert eng._flowmeter.dropout[12] == pytest.approx(0.30)


def test_apply_glitch_sets_flowmeter_mask():
    """_apply flowmeter_glitch: maschera via setter (valvola 12, s = 0.05)."""
    cfg, plant = _engine_plant()
    sc = Scenario(9, "m4 glitch", None, [
        FaultSpec("flowmeter_glitch", "local", 12, 0.05,
                  FaultOnset("abrupt", 100)),
    ])
    eng = FaultEngine(plant, sc, cfg)
    assert eng._flowmeter.glitch[12] == 0.0
    eng.on_cycle(_rec(12, 99))
    assert eng._flowmeter.glitch[12] == pytest.approx(0.05)


def test_gt_mapping_flowmeter():
    """Mapping GT: riga con fault_type/severity/valve_id; cicli sani None/0.0."""
    cfg, plant = _engine_plant()
    sc = Scenario(7, "gt m4", None, [
        FaultSpec("flowmeter_dropout", "local", 12, 0.30,
                  FaultOnset("abrupt", 100)),
    ])
    eng = FaultEngine(plant, sc, cfg)
    sano = eng.on_cycle(_rec(3, 99, ts_beg=500))
    assert sano["fault_type"] is None and sano["severity"] == 0.0
    assert sano["valve_id"] == 3 and sano["scenario_id"] == 7
    pre = eng.on_cycle(_rec(12, 99, ts_beg=600))
    assert pre["fault_type"] is None and pre["severity"] == 0.0
    guasto = eng.on_cycle(_rec(12, 100, ts_beg=700))
    assert guasto["fault_type"] == "flowmeter_dropout"
    assert guasto["severity"] == pytest.approx(0.30)   # dal rec.cycle_id
    assert guasto["valve_id"] == 12
    assert guasto["ts_beg"] == 700
    assert guasto["machine_code"] == "valve12"
    assert guasto["scenario_id"] == 7


def test_cmd_events_flowmeter():
    """CMD:OPEN/CLOSE: derivazione invariata (open = ts_beg + flush + pressurize)."""
    cfg, plant = _engine_plant()
    eng = FaultEngine(plant, Scenario(1, "cmd m4", None, [
        FaultSpec("flowmeter_dropout", "local", 5, 0.30,
                  FaultOnset("abrupt", 100)),
    ]), cfg)
    rec = _rec(5, 42, ts_beg=1_000_000, ft=1910, reason="target")
    eng.on_cycle(rec)
    evs = eng.take_events()
    names = [e[2] for e in evs]
    assert names == ["CMD:OPEN", "CMD:CLOSE"]
    op, cl = evs
    assert op[0] == 1_000_000 + cfg.flush_ms + cfg.pressurize_ms
    assert op[0] == 1_000_250
    assert cl[0] == op[0] + 1910
    assert op[3] == "" and cl[3] == "target"
    assert op[4] == 42 and cl[4] == 42
    assert op[1] == "valve5" and cl[1] == "valve5"


def test_timeline_rows_flowmeter():
    """Timeline: 1 riga per fault; end null; start_ts = ts_beg primo ciclo affetto."""
    cfg, plant = _engine_plant()
    sc = Scenario(8, "tl m4", None, [
        FaultSpec("flowmeter_dropout", "local", 12, 0.30,
                  FaultOnset("gradual", 100, 200)),
    ])
    eng = FaultEngine(plant, sc, cfg)
    rows = eng.timeline()
    assert len(rows) == 1
    r = rows[0]
    assert r["scenario_id"] == 8 and r["fault_id"] == 0
    assert r["fault_type"] == "flowmeter_dropout"
    assert r["valve_id"] == 12
    assert r["severity"] == 0.30
    assert r["onset_mode"] == "gradual"
    assert r["start_cycle"] == 100 and r["ramp_cycles"] == 200
    assert r["end_cycle"] is None and r["end_ts"] is None
    assert r["start_ts"] is None                     # nessun ciclo processato
    eng.on_cycle(_rec(12, 100, ts_beg=55_000))
    rows = eng.timeline()
    assert rows[0]["start_ts"] == 55_000


def test_wrapper_installed_only_for_flowmeter_faults():
    """Il wrapper per-scan è installato SOLO con fault flowmeter nello scenario.

    (a) scenario senza fault flowmeter (es. M2 restriction): plant.step resta
        l'originale (identità).
    (b) con fault flowmeter: plant.step è il wrapper; a maschere 0 (prima di
        start_cycle) N step producono last_pulses/volume_carry byte-identici
        al plant senza engine e lo stream sensori resta INVARIATO (zero draw,
        AC-M4-5 — bit-identità M4-sano ≡ M3).
    """
    seed = 42
    # (a) scenario senza fault flowmeter
    cfg, plant = _engine_plant(seed)
    eng = FaultEngine(plant, Scenario(1, "m2 solo", None, [
        FaultSpec("restriction", "local", 2, 0.04, FaultOnset("abrupt", 50)),
    ]), cfg)
    assert plant.step.__func__ is Plant.step           # originale: nessun wrapper
    assert not hasattr(eng, "_orig_step")
    assert not eng._flowmeter._active
    # (b) con fault flowmeter (start_cycle 100: maschere a 0 fino al ciclo 99)
    _, p_ref = _engine_plant(seed)                     # plant senza engine
    cfg2, p_fm = _engine_plant(seed)
    eng2 = FaultEngine(p_fm, Scenario(2, "m4 dropout", None, [
        FaultSpec("flowmeter_dropout", "local", 12, 0.30,
                  FaultOnset("abrupt", 100)),
    ]), cfg2)
    assert p_fm.step.__func__ is FaultEngine._step_flowmeter
    assert not eng2._flowmeter._active                 # maschere a 0
    state = _state_snapshot(eng2._flowmeter._stream)
    t0 = 0
    p_ref.mech[VALVE].begin_open(t0)
    p_fm.mech[VALVE].begin_open(t0)
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_ref.step(t)
        assert eng2._flowmeter.apply(t) == []          # no-op: nessun evento
        p_fm.step(t)                                   # wrapper: step + apply
    _assert_state_identical(state, eng2._flowmeter._stream)
    np.testing.assert_array_equal(p_fm.last_pulses, p_ref.last_pulses)
    np.testing.assert_array_equal(p_fm.volume_carry, p_ref.volume_carry)
    assert eng2.take_events() == []


def test_wrapper_applies_dropout_per_scan():
    """Engine + dropout (start_cycle 1, abrupt): per-scan il wrapper perde impulsi.

    Dopo N step: somma last_pulses(valve) < somma sana (direzione: ~50% scan
    persi con s=0.5) e volume_carry IDENTICO al sano (la fisica non è toccata
    — il mismatch volume/impulsi è la firma del guasto).
    """
    seed = 42
    cfg_ref, p_ref = _engine_plant(seed)
    cfg, plant = _engine_plant(seed)
    sc = Scenario(3, "drop per-scan", seed, [
        FaultSpec("flowmeter_dropout", "local", VALVE, 0.5,
                  FaultOnset("abrupt", 1)),
    ])
    eng = FaultEngine(plant, sc, cfg)
    assert plant.step.__func__ is FaultEngine._step_flowmeter
    assert eng._flowmeter.dropout[VALVE] == pytest.approx(0.5)  # QA-F7 pre-applicata
    t0 = 0
    p_ref.mech[VALVE].begin_open(t0)
    plant.mech[VALVE].begin_open(t0)
    sum_ref = sum_f = 0
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_ref.step(t)
        plant.step(t)                                  # wrapper: step + apply
        sum_ref += int(p_ref.last_pulses[VALVE])
        sum_f += int(plant.last_pulses[VALVE])
    assert sum_ref > 0
    assert sum_f < sum_ref                             # direzione: scan persi
    np.testing.assert_array_equal(plant.volume_carry, p_ref.volume_carry)


def test_late_pulse_events_via_engine():
    """Engine + glitch (start_cycle 1, abrupt): eventi LATE_PULSE via wrapper.

    Ciclo completo: FILLING (nessun late), coda + backstop (gap <= silence:
    nessun late), record emesso al primo scan senza impulsi con gap >= silence,
    poi gli spuri riprendono come late pulses -> engine.take_events() contiene
    (ts, machine_code, "LATE_PULSE", "n_pulses=...", cycle_id) e late_count
    è coerente (1 per spurio).
    """
    seed = 42
    cfg, plant = _engine_plant(seed)
    sc = Scenario(4, "late m4", seed, [
        FaultSpec("flowmeter_glitch", "local", VALVE, 1.0,
                  FaultOnset("abrupt", 1)),
    ])
    eng = FaultEngine(plant, sc, cfg)
    assert eng._flowmeter.glitch[VALVE] == pytest.approx(1.0)
    t0 = 0
    plant.mech[VALVE].begin_open(t0)
    for k in range(50):                                # FILLING: spuri liberi
        t = t0 + (k + 1) * SCAN_MS
        plant.step(t)
    assert eng.take_events() == []
    close_t = 50 * SCAN_MS
    plant.mech[VALVE].begin_close(close_t, plant.rng)
    for k in range(113):                               # coda + backstop (510..1630)
        t = close_t + (k + 1) * SCAN_MS
        plant.step(t)
    assert eng.take_events() == []                     # gap <= silence: nessun late
    # record emesso al primo scan senza impulsi con gap >= silence (t=1640)
    for k in range(113, 129):                          # 1640..1790
        t = close_t + (k + 1) * SCAN_MS
        plant.step(t)
    assert eng._flowmeter._record_emitted[VALVE]
    # post-record: gli spuri riprendono come late pulses (dal 1650 in poi)
    for k in range(129, 132):                          # 1800..1820
        t = close_t + (k + 1) * SCAN_MS
        plant.step(t)
    evs = eng.take_events()
    lates = [e for e in evs if e[2] == "LATE_PULSE"]
    # record emesso a 1640 (primo scan senza impulsi con gap 150 >= silence) ->
    # spuri late da 1650 a 1820 inclusi: 18 eventi deterministici (glitch 1.0
    # spara a OGNI scan: il conteggio non dipende dai valori dello stream)
    assert len(lates) == 18
    assert lates[0][0] == 1650 and lates[-1][0] == 1820
    for e in lates:
        assert e[1] == cfg.valves[VALVE].machine_code
        assert e[3] == "n_pulses=1"
        assert e[4] == 1                               # cycle_id: 1° ciclo valvola
    assert eng._flowmeter.late_count[VALVE] == 18


def test_non_leakage_plc_m4():
    """ADR-0012/0013: il PLC non sa nulla di fault/GT/flowmeter (grep)."""
    src = (Path(__file__).resolve().parent.parent / "plcsim"
           / "plc.py").read_text(encoding="utf-8")
    assert "scenario" not in src
    assert "FaultEngine" not in src
    assert "flowmeter" not in src
    assert "_pulse_dropout" not in src
    assert "_glitch_rate" not in src
