"""Feature service live (M9, ADR-0020) — riuso di `plcsim/ml_dataset.py`.

Il cardine anti-skew (§60, AC-M9-2): le 43 feature live sono calcolate con lo
STESSO codice del training. Questo modulo NON reimplementa nessuna feature: è
solo un **adattatore + orchestratore** che (1) mappa le colonne del raw Parquet
(envelope v1.1/v1.3 appiattito da `pipeline/ingest.py`) sulle colonne
`valve_cycles` attese da `compute_window_features`, e (2) applica la finestra
(N=50) + lo z-score per-valvola (`model/zstats.json`).

Single source of truth: `work/ml-feature-schema.md` (ML-F1) + `plcsim/ml_dataset.py`
(le 43 colonne `FEATURE_COLUMNS`, la finestra `window_cycles`, l'estrattore
`compute_window_features`, la normalizzazione per-valvola).

Flusso (spec M9 §4):

    raw Parquet (data/raw/machine=filler01/date=*/valve_cycles.parquet)
        │  colonne appiattite: data.filling_time_ms, data.close_reason, ...
        ▼
    raw_to_valve_cycles(frame)         # mappa data.* → colonne valve_cycles
        │
        ▼
    compute_window_features(cycles, events, n=50)   # 43 feature (riuso)
        │
        ▼
    transform_zscore(features, zstats)              # z-score per-valvola
        │
        ▼
    vettore 43 feature (stesso codice batch: bit-identico, AC-M9-2)

Determinismo: funzioni pure polars, nessun RNG. `events` (per late_pulse_*) è
facoltativo: se assente le feature late_pulse_* = 0 (spec §11 Q3), ma l'anti-skew
resta garantito per il T1 (che usa eventi sintetici identici sui due percorsi).

Guardia di riuso (AC-M9-1): nessuna copia duplicata dell'estrattore — import
diretto di `compute_window_features`/`window_cycles`/`transform_zscore` da
`plcsim.ml_dataset`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import polars as pl

from plcsim.ml_dataset import (
    FEATURE_COLUMNS,
    N_CYCLES,
    compute_window_features,
    normalizer_from_manifest,
    transform_zscore,
    window_cycles,
)

# ---------------------------------------------------------------------------
# Mapping raw (envelope v1.1/v1.3 appiattito) → colonne valve_cycles.
#
# Le colonne del raw Parquet sono prodotte da pipeline/ingest.py (FLATTENED_
# COLUMNS): `data.<logical_name>` più i campi di intestazione. Le colonne
# attese da compute_window_features (ml_dataset.py) sono i nomi bulk di
# telemetry.py (fillingtime, tailtime, ...). Il mapping è 1:1, esplicito e
# testato (anti-skew: la mappa NON ricostruisce semantica, rinomina soltanto).
#
# NOTA (M9, issue M9-01 + fix F1): il raw v1.1 (12 campi, pre-M9) NON ha
# close_reason/position_limit/filling_overtime; il raw v1.3 (15 campi, M9) li
# ha. Inoltre la policy T6 ammette `null` per OGNI campo data.* (tag
# illeggibile → quality.valid=false) e il raw v1.1 storicizzato da ingest ha
# le 3 colonne M9 PRESENTI-ma-tutte-NULL. Il feature service applica i
# default sani dichiarati (0 / False / "target" / "NORMAL") sia ai campi
# ASSENTI sia ai campi PRESENTI-ma-null, PRIMA di compute_window_features:
# nessun NaN raggiunge il modello (prima del fix: 6 feature NaN → ValueError
# "Input X contains NaN" in predict_proba; il fill copriva solo le colonne
# assenti).
#
# DECISIONE DI POLICY (T6): fill-null-with-default — NON si scartano i cicli
# con quality.valid=false. Scartare cicli romperebbe la completezza della
# finestra N=50 (finestra parziale → persa) e cambierebbe la semantica
# anti-skew del confronto batch≡live. Il default sano è la scelta già
# dichiarata nel docstring del modulo ("0/False/'target'") ed è coerente con
# validation.py (healthy → close_reason="target", position_limit=False,
# overtime=False).
# ---------------------------------------------------------------------------

# data.<logical_name> → colonna valve_cycles (KPI + flag + motivo chiusura)
_RAW_TO_VALVE: dict[str, str] = {
    "data.filling_time_ms": "fillingtime",
    "data.tail_time_ms": "tailtime",
    "data.tail_pulse": "tailpulse",
    "data.pulse_count": "pulsecount",
    "data.target": "target",
    "data.delta_pulse": "deltapulse",
    "data.filling_step_out": "filling_step_out",
    "data.filling_ok": "fillingok",
    "data.fill_quality_ok": "fill_quality_ok",
    "data.sequence_ok": "sequence_ok",
    "data.sample_valid": "sample_valid",
    "data.diagnostic_status": "diagnostic_status",
    "data.close_reason": "close_reason",
    "data.position_limit": "position_limit",
    "data.filling_overtime": "filling_overtime",
}

# Default "sani" per OGNI campo data.* del raw (policy T6, fix F1): KPI → 0,
# flag → False, diagnostic_status → "NORMAL", close_reason → "target"
# (whitelist). Coerenti col comportamento sano (validation.py: healthy →
# close_reason="target", position_limit=False, overtime solo se FT>2000, qui
# False nel caso limite). Le chiavi sono i nomi RAW (data.*): il valore viene
# applicato PRIMA del rename, sia per colonne ASSENTI (raw v1.1 pre-M9) sia
# per colonne PRESENTI-ma-null (T6 / raw storicizzato v1.1).
_RAW_FIELD_DEFAULTS: dict[str, object] = {
    # KPI (Int64 → 0)
    "data.filling_time_ms": 0,
    "data.tail_time_ms": 0,
    "data.tail_pulse": 0,
    "data.pulse_count": 0,
    "data.target": 0,
    "data.delta_pulse": 0,
    "data.filling_step_out": 0,
    # flag (Boolean → False)
    "data.filling_ok": False,
    "data.fill_quality_ok": False,
    "data.sequence_ok": False,
    "data.sample_valid": False,
    "data.position_limit": False,
    "data.filling_overtime": False,
    # diagnostica / motivo chiusura (String → stati sani)
    "data.diagnostic_status": "NORMAL",
    "data.close_reason": "target",
}


def raw_to_valve_cycles(raw: pl.DataFrame) -> pl.DataFrame:
    """Mappa il raw Parquet appiattito (envelope v1.1/v1.3) a valve_cycles.

    - rinomina `data.*` → colonne bulk (KPI, flag, close_reason) e
      `valve_id` → `machine_code` ("valve{N}" 0-based per coerenza con
      compute_window_features, che usa `machine_code` come chiave valvola);
    - `cycle_id` resta `cycle_id`;
    - i campi data.* ASSENTI (raw v1.1 pre-M9: close_reason/position_limit/
      filling_overtime) o PRESENTI-ma-NULL (policy T6) sono riportati ai
      default sani dichiarati PRIMA del windowing: nessun null/NaN raggiunge
      compute_window_features (fix F1 — prima: 6 feature NaN → crash).

    L'input è il frame appiattito già letto da `pl.read_parquet` sul raw
    partizionato (una o più date). Nessuna scrittura: pura trasformazione in
    memoria, deterministica.
    """
    out = raw
    # policy null (T6): PRESENTE-ma-null → default sano. Fix F1: prima il fill
    # copriva SOLO le colonne assenti → 6 feature NaN → predict_proba crash
    # sul primo record parziale (raw v1.1 storicizzato / T6).
    for raw_name, default in _RAW_FIELD_DEFAULTS.items():
        if raw_name in out.columns:
            out = out.with_columns(pl.col(raw_name).fill_null(default))
    out = out.rename({k: v for k, v in _RAW_TO_VALVE.items() if k in out.columns})
    # machine_code: "valve{valve_id-1}" — il raw usa valve_id 1-35 (contratto),
    # compute_window_features usa machine_code "valve0".. (0-based).
    if "valve_id" in out.columns:
        out = out.with_columns(
            (pl.lit("valve") + (pl.col("valve_id") - 1).cast(pl.String))
            .alias("machine_code"))
    # campi ASSENTI nel raw (pre-M9): aggiungili con i default sani, con dtype
    # coerente col contratto valve_cycles (KPI Int64, flag Boolean, String).
    for raw_name, default in _RAW_FIELD_DEFAULTS.items():
        valve_col = _RAW_TO_VALVE[raw_name]
        if valve_col in out.columns:
            continue
        if isinstance(default, bool):
            expr = pl.lit(default)
        elif isinstance(default, int):
            expr = pl.lit(default, dtype=pl.Int64)
        else:
            expr = pl.lit(default)
        out = out.with_columns(expr.alias(valve_col))
    return out


def load_raw_valve_cycles(raw_dir: str | Path,
                          dates: Optional[Iterable[str]] = None) -> pl.DataFrame:
    """Legge il raw partizionato e lo mappa a valve_cycles (una valvola+).

    Scansione di `data/raw/machine=filler01/date=*/valve_cycles.parquet`,
    ordinata per data; `dates` opzionale limita alle date. Ritorna un frame
    ordinato per (machine_code, cycle_id) mappato (raw_to_valve_cycles).
    """
    raw_dir = Path(raw_dir)
    files = sorted(raw_dir.glob("machine=filler01/date=*/valve_cycles.parquet"))
    if dates is not None:
        wanted = {f"date={d}" for d in dates}
        files = [f for f in files if f.parent.name in wanted]
    if not files:
        return pl.DataFrame()
    raw = pl.concat([pl.read_parquet(f) for f in files], how="vertical")
    cycles = raw_to_valve_cycles(raw)
    return cycles.sort(["machine_code", "cycle_id"])


def live_features(cycles_df: pl.DataFrame,
                  events_df: pl.DataFrame | None = None,
                  zstats: dict | None = None,
                  n: int = N_CYCLES) -> pl.DataFrame:
    """43 feature live da valve_cycles + eventi + z-score (riuso ml_dataset).

    - `cycles_df`: frame valve_cycles (già mappato da raw_to_valve_cycles o
      direttamente bulk) con `machine_code`, `cycle_id` e le colonne KPI/flag;
      i null dei campi raw sono già risolti a monte da raw_to_valve_cycles
      (policy T6: default sani) — qui NON si mascherano null del percorso
      batch (anti-skew: il codice feature resta identico al training);
    - `events_df`: eventi per late_pulse_* (facoltativo; None ⇒ late = 0);
    - `zstats`: dict per-valvola (normalizer_from_manifest del model/zstats.json);
      se None, niente z-score (feature grezze — utile al test anti-skew che
      confronta i 43 valori grezzi batch≡live prima della normalizzazione);
    - `n`: finestra (default N_CYCLES=50, congelato ML-F1).

    Ritorna il frame feature con ESATTAMENTE le 43 colonne FEATURE_COLUMNS
    (più le colonne chiave `machine_code`/`window_idx`/`last_cycle_id` per
    la tracciabilità), ordinate in modo deterministico. Riusa ESATTAMENTE
    `compute_window_features` + `transform_zscore`: nessuna reimplementazione.
    """
    feats = compute_window_features(cycles_df, events_df, n=n)
    if zstats is not None and feats.height:
        feats = transform_zscore(feats, zstats)
    return feats


def load_zstats(model_dir: str | Path) -> dict:
    """Carica lo z-score per-valvola da `model/zstats.json` (normalizer)."""
    model_dir = Path(model_dir)
    import json
    stats = json.loads((model_dir / "zstats.json").read_text(encoding="utf-8"))
    return normalizer_from_manifest(stats)


__all__ = [
    "raw_to_valve_cycles", "load_raw_valve_cycles", "live_features",
    "load_zstats", "FEATURE_COLUMNS", "N_CYCLES",
]
