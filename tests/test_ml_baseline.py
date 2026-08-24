"""Test W3 — ml_baseline (Track D): detector 3σ·√2/√n cross-seed + σ-shift.

Port D5 (tests/test_fault_integration.py:559-590) con baseline CROSS-SEED
(healthy_baseline s202 — mai same-seed del train s101, vincolo post-M2).
Tutti su frame SINTETICI deterministici (LCG aritmetico puro, nessun RNG,
nessun run di simulazione — pre-marker). Casi: shift grande → flag / piccolo
→ no flag; ramo σ-shift flagga σ_FT ×2 e NON ×1.05 (separato da `flagged`,
D-ML-5); FP ≈ 0 healthy-vs-healthy cross-seed; pattern M2/AC-7 (flagga
esattamente le valvole con shift iniettato, nessuna sana).
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plcsim.ml_baseline import (  # noqa: E402
    DETECTOR_KPIS, SIGMA_SHIFT_KPI, detect_faulted_valves,
)

# --------------------------------------------------------------------------
# Harness sintetico deterministico (LCG aritmetico puro)
# --------------------------------------------------------------------------


def _uni(i: int) -> float:
    """Uniforme pseudo-casuale deterministica in [0,1) (aritmetica pura)."""
    return ((i * 1103515245 + 12345) % 2147483648) / 2147483648.0


def _make_cycles(valves: dict, n: int, seed: int) -> pl.DataFrame:
    """Frame cicli sintetico con i 3 KPI del detector.

    valves: {machine_code: (ft_mean, ft_sig, tt_mean, tt_sig, pc_mean, pc_sig)}
    — ogni KPI = mean + sig·(u − 0.5) con u da LCG (spread uniforme ±sig/2,
    std popolazione ≈ sig/√12). Seed diversi → draw diversi (cross-seed).
    """
    rows = []
    cid = 0
    for i in range(n):
        for vi, (mc, (ft_m, ft_s, tt_m, tt_s, pc_m, pc_s)) in enumerate(
                valves.items()):
            base = seed * 1_000_000_000 + vi * 10_000_000
            rows.append({
                "machine_code": mc,
                "cycle_id": cid,
                "fillingtime": ft_m + ft_s * (_uni(base + i * 7) - 0.5),
                "tailtime": tt_m + tt_s * (_uni(base + i * 7 + 1) - 0.5),
                "pulsecount": pc_m + pc_s * (_uni(base + i * 7 + 2) - 0.5),
            })
            cid += 1
    return pl.DataFrame(rows)


HEALTHY_CFG = {
    "v1": (2000.0, 50.0, 300.0, 30.0, 2500.0, 20.0),
    "v2": (2000.0, 50.0, 300.0, 30.0, 2500.0, 20.0),
    "v3": (2000.0, 50.0, 300.0, 30.0, 2500.0, 20.0),
    "v4": (2000.0, 50.0, 300.0, 30.0, 2500.0, 20.0),
    "v5": (2000.0, 50.0, 300.0, 30.0, 2500.0, 20.0),
}


def _shift(ft=0.0, tt=0.0, pc=0.0, target=None) -> dict:
    """Copia di HEALTHY_CFG con shift di media SOLO su `target` (o nessuno)."""
    out = {}
    for mc, (ft_m, ft_s, tt_m, tt_s, pc_m, pc_s) in HEALTHY_CFG.items():
        if target is not None and mc != target:
            out[mc] = (ft_m, ft_s, tt_m, tt_s, pc_m, pc_s)
        else:
            out[mc] = (ft_m + ft, ft_s, tt_m + tt, tt_s, pc_m + pc, pc_s)
    return out


# --------------------------------------------------------------------------
# Mean-shift 3σ·√2/√n: shift grande → flag, shift piccolo → no flag
# --------------------------------------------------------------------------


def test_large_shift_flagged_small_shift_not():
    baseline = _make_cycles(HEALTHY_CFG, n=1000, seed=1)
    # v1: FT +300 (Δ ≫ soglia ~2); v2 sana
    run_big = _make_cycles(_shift(ft=300.0, target="v1"), n=1000, seed=2)
    res = detect_faulted_valves(run_big, baseline)
    assert res.flagged == frozenset({"v1"})
    d = res.details["v1"]
    assert d.kpi == "fillingtime"
    assert d.n == 1000
    assert d.threshold is not None and d.threshold > 0
    assert abs(d.delta_mean - 300.0) < 10.0
    assert d.sigma_shift is False
    # v1: FT +1 (Δ ~1 < soglia ~2 anche col rumore) → nessun flag
    run_small = _make_cycles(_shift(ft=1.0, target="v1"), n=1000, seed=3)
    res2 = detect_faulted_valves(run_small, baseline)
    assert res2.flagged == frozenset()


# --------------------------------------------------------------------------
# Ramo σ-shift (M3, pressure_instability): ×2 flagga, ×1.05 NO — SEPARATO
# --------------------------------------------------------------------------


def test_sigma_shift_flags_doubled_sigma_only():
    n = 100
    baseline = _make_cycles({"v1": (2000.0, 50.0, 300.0, 30.0, 2500.0, 20.0)},
                            n=n, seed=10)
    # σ_FT ×2 (media invariata): ramo mean-shift silente, σ-shift attivo
    run_x2 = _make_cycles({"v1": (2000.0, 100.0, 300.0, 30.0, 2500.0, 20.0)},
                          n=n, seed=11)
    res = detect_faulted_valves(run_x2, baseline)
    assert res.sigma_shift_flagged == frozenset({"v1"})
    assert res.flagged == frozenset()          # SEPARAZIONE D-ML-5
    assert res.details["v1"].sigma_shift is True
    assert res.details["v1"].sigma_ratio is not None and \
        res.details["v1"].sigma_ratio > 1.5    # ratio ≈ 2
    # σ_FT ×1.05: sotto (1 + 3/√(n−1)) ≈ 1.30 per n=100 → nessun flag
    run_x105 = _make_cycles({"v1": (2000.0, 52.5, 300.0, 30.0, 2500.0, 20.0)},
                            n=n, seed=12)
    res2 = detect_faulted_valves(run_x105, baseline)
    assert res2.sigma_shift_flagged == frozenset()
    assert res2.flagged == frozenset()


# --------------------------------------------------------------------------
# Sanity cross-seed: healthy vs healthy di seed diverso → FP ≈ 0
# --------------------------------------------------------------------------


def test_cross_seed_healthy_vs_healthy_no_false_positives():
    baseline = _make_cycles({mc: HEALTHY_CFG[mc] for mc in ("v1", "v2")},
                            n=2000, seed=20)
    run = _make_cycles({mc: HEALTHY_CFG[mc] for mc in ("v1", "v2")},
                       n=2000, seed=21)
    res = detect_faulted_valves(run, baseline)
    assert res.flagged == frozenset()
    assert res.sigma_shift_flagged == frozenset()


# --------------------------------------------------------------------------
# Coerenza col pattern M2/AC-7: flagga ESATTAMENTE le valvole con shift
# iniettato (una per KPI primario), nessuna sana
# --------------------------------------------------------------------------


def test_flags_exactly_injected_valves_ac7_pattern():
    baseline = _make_cycles(HEALTHY_CFG, n=1000, seed=30)
    # v1 restriction → FT; v2 closing_delay → TT; v3 glitch → PC; v4/v5 sane
    cfg = _shift(ft=300.0, target="v1")
    cfg["v2"] = (2000.0, 50.0, 600.0, 30.0, 2500.0, 20.0)   # TT +300
    cfg["v3"] = (2000.0, 50.0, 300.0, 30.0, 2800.0, 20.0)   # PC +300
    run = _make_cycles(cfg, n=1000, seed=31)
    res = detect_faulted_valves(run, baseline)
    assert res.flagged == frozenset({"v1", "v2", "v3"})
    assert not (res.flagged & frozenset({"v4", "v5"}))      # nessuna sana
    assert res.details["v1"].kpi == "fillingtime"
    assert res.details["v2"].kpi == "tailtime"
    assert res.details["v3"].kpi == "pulsecount"


# --------------------------------------------------------------------------
# API/contratto del port
# --------------------------------------------------------------------------


def test_kpi_or_set_matches_contract():
    assert DETECTOR_KPIS == ("fillingtime", "pulsecount", "tailtime")
    assert SIGMA_SHIFT_KPI == "fillingtime"


def test_valve_missing_from_baseline_skipped_gracefully():
    # v2 assente dalla baseline (frame vuoto per quella valvola): guardia
    # difensiva sul port → nessun errore, non flaggata
    baseline = _make_cycles({"v1": HEALTHY_CFG["v1"]}, n=100, seed=40)
    run = _make_cycles({"v1": HEALTHY_CFG["v1"], "v2": HEALTHY_CFG["v2"]},
                       n=100, seed=41)
    res = detect_faulted_valves(run, baseline)
    assert res.flagged == frozenset()
    assert res.details["v2"].n == 100
    assert res.details["v2"].kpi is None
    assert res.details["v2"].sigma_shift is False
