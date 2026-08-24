"""Test W2 — iniezione fisica dei fault nel plant (contratto M2 §1.1-§1.3).

Unit sull'iniezione a livello Plant/ValveMechanics:
- restriction: moltiplicatore per-valvola `Plant._restriction` applicato nel
  loop caldo DOPO il rumore (`q *= z`) e PRIMA del cutoff del flussimetro
  (`np.greater_equal(q, c.flow_cutoff_mls, ...)`) in Plant.step (contratto
  §1.1). Fattore 1.0 -> percorso bit-identico al plant senza iniezione
  (x1.0 esatto in IEEE) — fondamento di AC-5/AC-6/AC-9.
- open_delay_ms / close_delay_ms: offset di tempo deterministici sulla rampa
  meccanica (ValveMechanics.begin_open/begin_close, contratto §1.2/§1.3) con
  ZERO draw RNG aggiuntivi (ADR-0013: l'engine M2 non consuma stream).

Harness condiviso: make_cfg/TPL/END_MS da tests/test_engine.py (stesso
template macchina e giornata compressa). Nessun output su work/: i test non
scrivono file (gli output M2, quando servono, vanno su tmp_path). Esegue
dalla root del repo.

NOTA: `test_healthy_equiv_m1` NON è qui — appartiene a W4 (contratto §5.4).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from plcsim.clock import SimulationClock          # noqa: E402
from plcsim.plant import Plant                    # noqa: E402
from test_engine import make_cfg, TPL, END_MS     # noqa: E402

SCAN_MS = 10       # passo di scan del PLC (config)
N_STEPS = 300      # 3 s di integrazione: apertura completa (tau_open 180 ms)
VALVE = 0          # valvola target dei test (valve0, flow_base ~135 ml/s)


def _make_plant(seed: int = 42):
    """Plant isolato con l'harness di test_engine (template macchina TPL)."""
    cfg = make_cfg(42)
    plant = Plant(cfg, SimulationClock(), np.random.default_rng(seed))
    return cfg, plant


def _assert_same_rng_state(a: np.random.Generator, b: np.random.Generator) -> None:
    """Stato del bit_generator identico tra due generatori (PCG64/Philox)."""
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


def test_restriction_scales_flow():
    """La restriction scala la portata ESATTAMENTE di (1-s) (stessi draw RNG).

    Due plant con lo stesso seed: stessa sequenza di step, stessa valvola
    aperta (begin_open); l'unica differenza è `plant._restriction[i] = 1-s`.
    L'iniezione è un moltiplicatore puro dopo il rumore -> dopo ogni step
    `flow_now[i]` del plant iniettato è il prodotto esatto (IEEE) di quello
    sano per (1-s); le altre valvole (fattore 1.0) restano identiche.
    """
    s = 0.20
    _, p_ok = _make_plant(42)        # sano
    _, p_f = _make_plant(42)         # iniettato
    factor = 1.0 - s
    p_f._restriction[VALVE] = factor
    r = p_f._restriction.copy()      # fattore per-valvola (1.0 altrove)
    t0 = 0
    p_ok.mech[VALVE].begin_open(t0)
    p_f.mech[VALVE].begin_open(t0)
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_ok.step(t)
        p_f.step(t)
        np.testing.assert_array_equal(p_f.flow_now, p_ok.flow_now * r)
    # la valvola è davvero aperta e sopra il cutoff: il confronto non è banale
    assert p_ok.flow_now[VALVE] > 10.0


def test_restriction_before_cutoff():
    """L'iniezione agisce PRIMA del cutoff: portata ridotta -> flusso escluso.

    Con (1-s) tale che la portata iniettata resti sotto la soglia del
    flussimetro (flow_cutoff_mls = 10 ml/s) per ogni scan, il cutoff esclude
    il flusso: volume_carry e last_pulses restano a zero nonostante la
    valvola aperta. Il plant sano stesso-seed produce invece flusso pieno
    sopra soglia (con impulsi) — la differenza è attribuibile solo alla
    restriction, che opera tra `q *= z` e il cutoff (plant.py:305-306).
    """
    cfg, p_ok = _make_plant(42)
    t0 = 0
    p_ok.mech[VALVE].begin_open(t0)
    max_q = 0.0
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_ok.step(t)
        max_q = max(max_q, p_ok.flow_now[VALVE])
    # baseline: valvola aperta a flusso pieno, ben sopra la soglia
    assert p_ok.flow_now[VALVE] > cfg.plant.flow_cutoff_mls
    assert p_ok.last_pulses[VALVE] > 0
    # (1-s)*max_q < soglia con margine (0.4*soglia): il cutoff esclude tutto
    factor = 0.4 * cfg.plant.flow_cutoff_mls / max_q
    assert 0.0 < factor < 1.0
    _, p_f = _make_plant(42)
    p_f._restriction[VALVE] = factor
    p_f.mech[VALVE].begin_open(t0)
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_f.step(t)
        assert p_f.flow_now[VALVE] < cfg.plant.flow_cutoff_mls
    # cutoff attivo: nessun flusso integrato, nessun impulso
    assert p_f.volume_carry[VALVE] == 0.0
    assert p_f.last_pulses[VALVE] == 0


def test_restriction_identity():
    """restriction = 1.0 -> step() byte-identico al plant senza iniezione.

    x1.0 è esatto in IEEE: stessa sequenza di step (stesso seed, valvola
    aperta, chiusura con gli stessi draw jitter/snap) -> flow_now,
    volume_carry e last_pulses IDENTICI bit-a-bit. È il fondamento di
    AC-5/AC-6/AC-9: le valvole sane dei run guasti restano identiche.
    """
    _, p_ok = _make_plant(42)
    _, p_f = _make_plant(42)
    p_f._restriction[:] = 1.0       # identità esplicita (default: np.ones)
    t0 = 0
    p_ok.mech[VALVE].begin_open(t0)
    p_f.mech[VALVE].begin_open(t0)
    for k in range(N_STEPS):
        t = t0 + (k + 1) * SCAN_MS
        p_ok.step(t)
        p_f.step(t)
    # fase di chiusura (stessi draw RNG, stessi stati meccanici)
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


def test_open_delay_shifts_open_start():
    """open_delay_ms: la rampa di apertura parte a t+d (contratto §1.3).

    begin_open(t) con open_delay_ms = d -> _open_start == t + int(d)
    (troncamento int documentato: delay frazionari da rampa gradual ->
    deterministico). Le maschere si aprono subito ma il flusso è 0 nel
    tratto di ritardo (open_factor clip [0,1]); a t+d+tau_open il fattore è
    1 (clip). In step(), la rampa `f = (t_ev - _open_start)*_inv_tau_open`
    resta 0 finché t_ev <= t+d.
    """
    d = 40.0
    _, p = _make_plant(42)
    mech = p.mech[VALVE]
    mech.open_delay_ms = d
    t0 = 0
    mech.begin_open(t0)
    assert p._open_start[VALVE] == t0 + int(d)      # shift di d ms
    assert not p._mask_closed[VALVE]                # maschere: aperte subito
    assert not p._mask_closing[VALVE]
    assert mech.open_factor(t0) == 0.0              # ritardo: rampa non partita
    assert mech.open_factor(t0 + int(d) + int(mech.tau_open_ms)) == 1.0
    # flusso fisico: zero durante il ritardo, poi rampa 0->1
    for k in range(4):
        p.step(t0 + (k + 1) * SCAN_MS)
        assert p.flow_now[VALVE] == 0.0             # t <= t0+d -> f clip a 0
    for k in range(4, N_STEPS):
        p.step(t0 + (k + 1) * SCAN_MS)
    assert p.flow_now[VALVE] > 0.0                  # valvola aperta a regime


def test_close_delay_shifts_ramp_end():
    """close_delay_ms: la rampa di chiusura è estesa di d ms (contratto §1.2).

    begin_close(t, rng) con close_delay_ms = d -> _ramp_end == t + d + ramp
    con la STESSA ramp del caso sano (stessi 2 draw jitter/snap, zero draw
    aggiuntivi) e _inv_ramp == 1/ramp invariato. Nel tratto [t, t+d] la
    valvola resta a flusso pieno: open_factor è clip a 1 (il sano è già in
    rampa, < 1). t = 0: somme esatte in IEEE (nessuna perdita di rounding).
    """
    d = 50.0
    _, p_ok = _make_plant(42)
    _, p_f = _make_plant(42)
    p_f.mech[VALVE].close_delay_ms = d
    t0 = 0
    # valvola aperta prima del comando di chiusura (begin_open: 0 draw RNG)
    p_ok.mech[VALVE].begin_open(t0)
    p_f.mech[VALVE].begin_open(t0)
    p_ok.mech[VALVE].begin_close(t0, p_ok.rng)      # stessi draw -> stessa ramp
    p_f.mech[VALVE].begin_close(t0, p_f.rng)
    ramp = p_ok._ramp_end[VALVE] - t0
    assert ramp > 0.0
    assert p_f._ramp_end[VALVE] == t0 + d + ramp    # shift esatto di d
    assert p_f._inv_ramp[VALVE] == p_ok._inv_ramp[VALVE]
    assert p_f._inv_ramp[VALVE] == 1.0 / ramp
    assert p_f.mech[VALVE].open_factor(t0 + 1) == 1.0   # tratto a flusso pieno
    assert p_ok.mech[VALVE].open_factor(t0 + 1) < 1.0   # sano: già in rampa


def test_zero_extra_rng_draws():
    """Le iniezioni NON consumano stream RNG (ADR-0013, contratto §1).

    begin_close con e senza close_delay (stesso seed): stato del
    bit_generator IDENTICO dopo la chiamata (gli stessi 2 draw jitter/snap
    del percorso sano). begin_open (0 draw) e restriction (moltiplicatore
    puro nel loop caldo): idem — dopo N step lo stato è identico con e senza
    iniezione. Stesso seed => stessa baseline sana bit-per-bit.
    """
    # close_delay: nessun draw aggiuntivo
    _, p_ok = _make_plant(7)
    _, p_f = _make_plant(7)
    p_f.mech[VALVE].close_delay_ms = 100.0
    p_ok.mech[VALVE].begin_close(0, p_ok.rng)
    p_f.mech[VALVE].begin_close(0, p_f.rng)
    _assert_same_rng_state(p_ok.rng, p_f.rng)
    # open_delay: begin_open non disegna nulla
    _, p_a = _make_plant(9)
    _, p_b = _make_plant(9)
    p_b.mech[VALVE].open_delay_ms = 80.0
    p_a.mech[VALVE].begin_open(0)
    p_b.mech[VALVE].begin_open(0)
    _assert_same_rng_state(p_a.rng, p_b.rng)
    # restriction: moltiplicatore puro nel loop -> stato identico dopo N step
    _, p_c = _make_plant(11)
    _, p_d = _make_plant(11)
    p_d._restriction[3] = 0.7
    p_c.mech[3].begin_open(0)
    p_d.mech[3].begin_open(0)
    for k in range(200):
        t = (k + 1) * SCAN_MS
        p_c.step(t)
        p_d.step(t)
    _assert_same_rng_state(p_c.rng, p_d.rng)
