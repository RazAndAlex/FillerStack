"""Layer ML — baseline detector 3σ·√2/√n cross-seed (D-W3, Track D).

Port del detector test-only `detect_faulted_valves`
(`tests/test_fault_integration.py:559-590`, AC-7 M2) nel layer ML, con la
differenza VINCOLANTE (D5, disposizione MUSE / vincolo post-M2):
- baseline = run `healthy_baseline` **CROSS-SEED (seed 202)** — MAI same-seed
  del train (`healthy_train` s101). La baseline stesso-seed del test-only non è
  ML-deployable (non può servire da train E da ground-truth in produzione);
- KPI dell'OR: `{fillingtime, tailtime, pulsecount}` (mappa primaria M2+M4:
  restriction→FT, closing_delay→TT, opening_delay→FT, dropout→FT/PC,
  glitch→PC — work/plan-ml-v2.md §5.1);
- **ramo σ-shift** per `pressure_instability` (criterio M3,
  work/m3_calibration.md): flag se σ_FT(run) > σ_FT(baseline)·(1 + 3/√(n−1)).
  RESTITUITO SEPARATAMENTE (D-ML-5: gli FP del ramo σ-shift sono riportati a
  parte nel report, mai fusi nel set principale);
- il detector resta **binario per (valvola, run)**: nessun tipo di fault né
  timing.

Riferimento contrattuale: work/plan-ml-v2.md §5.1/§5.2/§8 (AC-ML-4a),
work/ml-feature-schema.md (solo segnali, mai GT), ML-F6 (la banda sana può
variare tra seed: confronto baseline stesso seed del detector s202 — rischio
documentato nel report). Guardie difensive: valvola con n=0, σ baseline None o
Δmean None → KPI saltato (stesso skip del port).

Funzioni pure polars, nessun RNG, deterministiche.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

import polars as pl

# Mappa KPI primario per tipo di fault (mai usata con GT). I KPI dell'OR sono
# i valori distinti: {fillingtime, pulsecount, tailtime} — OR su 3 KPI.
DETECTOR_KPI_MAP: Mapping[str, str] = {
    "restriction": "fillingtime",
    "closing_delay": "tailtime",
    "opening_delay": "fillingtime",
    "flowmeter_dropout": "fillingtime",
    "flowmeter_glitch": "pulsecount",
}
DETECTOR_KPIS: tuple = tuple(sorted({kpi for kpi in DETECTOR_KPI_MAP.values()}))

# KPI del ramo σ-shift (pressure_instability, criterio M3): sempre fillingtime.
SIGMA_SHIFT_KPI = "fillingtime"

# Fattore z del detector (port D5: 3·σ·√2/√n, |Δmedia| di DUE medie rumorose).
DETECTOR_Z = 3.0


@dataclass(frozen=True)
class ValveDetail:
    """Dettaglio per-valvola del detector (report per-valvola).

    `kpi`/`delta_mean`/`threshold` si riferiscono al PRIMO KPI (ordine
    alfabetico) che ha superato la soglia mean-shift (None se non flaggata);
    `sigma_shift` è il verdetto del ramo σ-shift (M3) — separato (D-ML-5);
    `sigma_ratio`/`sigma_threshold` sono i valori del ramo σ-shift per il
    report (ratio None se σ_FT(baseline) == 0).
    """

    machine_code: str
    n: int
    kpi: Optional[str] = None
    delta_mean: Optional[float] = None
    threshold: Optional[float] = None
    sigma_shift: bool = False
    sigma_ratio: Optional[float] = None
    sigma_threshold: Optional[float] = None


@dataclass(frozen=True)
class DetectResult:
    """Esito del detector per un run.

    `flagged`: valvole flaggate dal ramo mean-shift 3σ·√2/√n (OR sui KPI);
    `sigma_shift_flagged`: valvole flaggate dal ramo σ-shift (M3) — SEPARATO
    (D-ML-5), mai fuso in `flagged`;
    `details`: dettaglio per-valvola (una entry per valvola presente nel run).
    """

    flagged: frozenset = field(default_factory=frozenset)
    details: dict = field(default_factory=dict)
    sigma_shift_flagged: frozenset = field(default_factory=frozenset)


def detect_faulted_valves(cycles: pl.DataFrame, baseline: pl.DataFrame,
                          primary: Mapping[str, str] = DETECTOR_KPI_MAP,
                          z: float = DETECTOR_Z) -> DetectResult:
    """Detector dai SOLI segnali di valve_cycles (mai GT), CROSS-SEED.

    Per valvola: flag mean-shift se |Δmean| > z·σ_baseline·√2/√n con
    Δmean = media(run) − media(baseline) (σ_baseline = std del KPI nel run
    baseline, n = cicli nel run) su ogni KPI degli `primary.values()` (OR,
    primo KPI in ordine alfabetico che supera). σ = 0 in baseline → soglia 0
    (qualsiasi Δ ≠ 0 flagga). Ramo σ-shift (M3, SEPARATO): σ_FT(run) >
    σ_FT(baseline)·(1 + z/√(n−1)); baseline σ_FT == 0 → flag se σ_FT(run) > 0.

    `cycles`/`baseline`: frame polars con colonne machine_code e i KPI
    (fillingtime, tailtime, pulsecount). Ritorna DetectResult.
    """
    kpis = sorted({kpi for kpi in primary.values()})
    flagged: set[str] = set()
    details: dict[str, ValveDetail] = {}
    for mc in cycles["machine_code"].unique().to_list():
        df = cycles.filter(pl.col("machine_code") == mc)
        bf = baseline.filter(pl.col("machine_code") == mc)
        n = df.height
        # --- ramo σ-shift (pressure_instability, criterio M3) — SEPARATO ---
        sigma_ratio: Optional[float] = None
        sigma_threshold: Optional[float] = None
        sigma_shift = False
        b_sigma_ft = bf[SIGMA_SHIFT_KPI].std()
        if n > 1 and b_sigma_ft is not None:
            sigma_threshold = 1.0 + z / ((n - 1) ** 0.5)
            run_sigma_ft = df[SIGMA_SHIFT_KPI].std()
            if b_sigma_ft > 0:
                sigma_ratio = (run_sigma_ft / b_sigma_ft
                               if run_sigma_ft is not None else None)
                sigma_shift = (run_sigma_ft is not None
                               and run_sigma_ft > b_sigma_ft * sigma_threshold)
            else:
                # baseline costante: qualsiasi σ>0 nel run eccede 0·(1+…)
                sigma_ratio = None
                sigma_shift = run_sigma_ft is not None and run_sigma_ft > 0
        # --- ramo mean-shift 3σ·√2/√n (OR sui KPI) ---
        flag_kpi: Optional[str] = None
        delta_mean: Optional[float] = None
        threshold: Optional[float] = None
        for kpi in kpis:
            run_mean = df[kpi].mean()
            base_mean = bf[kpi].mean()
            sigma = bf[kpi].std()
            if (n == 0 or run_mean is None or base_mean is None
                    or sigma is None):
                continue
            d_mean = run_mean - base_mean
            thr = z * sigma * (2.0 ** 0.5) / (n ** 0.5)
            if abs(d_mean) > thr:
                flag_kpi, delta_mean, threshold = kpi, float(d_mean), float(thr)
                break
        if flag_kpi is not None:
            flagged.add(mc)
        details[mc] = ValveDetail(
            machine_code=mc, n=n, kpi=flag_kpi, delta_mean=delta_mean,
            threshold=threshold, sigma_shift=sigma_shift,
            sigma_ratio=sigma_ratio, sigma_threshold=sigma_threshold,
        )
    sigma_flagged = frozenset(mc for mc, d in details.items() if d.sigma_shift)
    return DetectResult(flagged=frozenset(flagged), details=details,
                        sigma_shift_flagged=sigma_flagged)
