"""Test del feature service live M9 (ADR-0020) — anti-skew + riuso.

Copertura (spec M9 §7):
- T0: riuso — `pipeline/features.py` importa `compute_window_features` da
  `plcsim.ml_dataset` (nessuna reimplementazione: le 43 feature non sono
  ridefinite localmente).
- T1 (AC-M9-2, cardine anti-skew): a parità di sequenza di 50 cicli, il
  vettore feature del percorso batch (`compute_window_features` diretto) e del
  percorso live (`live_features`) è bit-identico (43 col Float64), e lo
  z-score è identico a parità di zstats.
- T0/T1: `raw_to_valve_cycles` mappa il raw appiattito (v1.3, 15 campi) sulle
  colonne valve_cycles; il roundtrip raw→valve→features eguaglia il percorso
  bulk diretto (anti-skew end-to-end).
- policy v1.1 pre-M9: campi close_reason/position_limit/filling_overtime
  mancanti → default sani, senza crash.
- policy T6 / fix F1: campi data.* PRESENTI-ma-null (KPI inclusi) → default
  sani (0/False/"target"/"NORMAL"); nessun NaN raggiunge il modello.

Nessun Docker, nessun broker: tutto polars in-memory deterministico.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.features import (  # noqa: E402
    FEATURE_COLUMNS,
    live_features,
    load_zstats,
    raw_to_valve_cycles,
)
from plcsim.ml_dataset import compute_window_features  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture: 100 cicli sintetici di una valvola (valve0) coi campi bulk.
# ---------------------------------------------------------------------------
@pytest.fixture
def cycles_synth() -> pl.DataFrame:
    n = 100
    # valori deterministici (simulano un run sano: FT ~1900, TT ~300, PC~2500)
    import math
    rows = []
    for i in range(n):
        rows.append({
            "machine_code": "valve0",
            "cycle_id": i + 1,
            "fillingtime": 1900 + (i % 7) * 5,
            "tailtime": 300 + (i % 5) * 8,
            "tailpulse": 220 + (i % 9),
            "pulsecount": 2500 + (i % 11) - 5,
            "deltapulse": 5 - (i % 11),
            "filling_step_out": 24 + (i % 3),
            "fillingok": True,
            "fill_quality_ok": True,
            "sequence_ok": True,
            "sample_valid": True,
            "position_limit": False,
            "filling_overtime": False,
            "diagnostic_status": "NORMAL",
            "close_reason": "target",
        })
    return pl.DataFrame(rows)


# ---------------------------------------------------------------------------
# T0 — riuso (nessuna reimplementazione)
# ---------------------------------------------------------------------------
def test_features_is_reused_not_reimplemented(cycles_synth):
    """T0 anti-riuso (COMPORTAMENTALE, non grep): le funzioni esposte dal
    feature service SONO gli oggetti di plcsim.ml_dataset (identità di
    oggetto), il loro codice sorgente fisico sta in ml_dataset.py
    (inspect.getsourcefile), e il percorso live è una delega al batch
    (output identico). Una reimplementazione o un re-export copia non
    passerebbe queste asserzioni."""
    import inspect
    import pipeline.features as pf
    import plcsim.ml_dataset as md

    # identità dell'oggetto: lo STESSO callable importato, non una copia
    for name in ("compute_window_features", "window_cycles",
                 "transform_zscore", "normalizer_from_manifest"):
        live_obj = getattr(pf, name, None)
        assert live_obj is getattr(md, name), \
            f"{name}: oggetto diverso da ml_dataset (reimplementazione?)"

    # FEATURE_COLUMNS: la STESSA tupla congelata (non ricostruita localmente)
    assert pf.FEATURE_COLUMNS is md.FEATURE_COLUMNS

    # il codice che calcola le feature vive fisicamente in ml_dataset.py
    assert inspect.getsourcefile(pf.compute_window_features) == \
        inspect.getsourcefile(md.compute_window_features)

    # comportamento: il percorso live è una delega (output identico al batch)
    batch = md.compute_window_features(cycles_synth, None, n=50)
    live = pf.live_features(cycles_synth, None, zstats=None, n=50)
    assert batch.select(FEATURE_COLUMNS).equals(live.select(FEATURE_COLUMNS))


# ---------------------------------------------------------------------------
# T1 / AC-M9-2 — anti-skew: batch ≡ live (bit-identico)
# ---------------------------------------------------------------------------
def test_antiskew_batch_equals_live(cycles_synth):
    batch = compute_window_features(cycles_synth, None, n=50)
    live = live_features(cycles_synth, None, zstats=None, n=50)
    # stesse righe, stesso ordine, ESATTAMENTE le 43 colonne feature identiche
    assert batch.height == live.height
    assert batch.select(FEATURE_COLUMNS).equals(live.select(FEATURE_COLUMNS))
    # l'ordine (machine_code, window_idx) è lo stesso
    assert batch["window_idx"].to_list() == live["window_idx"].to_list()


def test_antiskew_raw_roundtrip_equals_bulk(cycles_synth):
    """Raw v1.3 appiattito → valve_cycles → features == bulk diretto."""
    # costruisci raw v1.3 dalle stesse 100 righe bulk
    raw = pl.DataFrame({
        "schema_version": ["1.3"] * cycles_synth.height,
        "event_id": [f"00000000-0000-0000-0000-{i:012d}"
                     for i in range(cycles_synth.height)],
        "machine_id": ["filler01"] * cycles_synth.height,
        "cycle_id": cycles_synth["cycle_id"].to_list(),
        "valve_id": [1] * cycles_synth.height,
        "data.filling_time_ms": cycles_synth["fillingtime"].to_list(),
        "data.tail_time_ms": cycles_synth["tailtime"].to_list(),
        "data.tail_pulse": cycles_synth["tailpulse"].to_list(),
        "data.pulse_count": cycles_synth["pulsecount"].to_list(),
        "data.target": [2500] * cycles_synth.height,
        "data.delta_pulse": cycles_synth["deltapulse"].to_list(),
        "data.filling_step_out": cycles_synth["filling_step_out"].to_list(),
        "data.filling_ok": cycles_synth["fillingok"].to_list(),
        "data.fill_quality_ok": cycles_synth["fill_quality_ok"].to_list(),
        "data.sequence_ok": cycles_synth["sequence_ok"].to_list(),
        "data.sample_valid": cycles_synth["sample_valid"].to_list(),
        "data.diagnostic_status": cycles_synth["diagnostic_status"].to_list(),
        "data.close_reason": cycles_synth["close_reason"].to_list(),
        "data.position_limit": cycles_synth["position_limit"].to_list(),
        "data.filling_overtime": cycles_synth["filling_overtime"].to_list(),
        "quality.valid": [True] * cycles_synth.height,
        "quality.completeness": ["complete"] * cycles_synth.height,
    })
    cycles = raw_to_valve_cycles(raw)
    live_raw = live_features(cycles, None, zstats=None, n=50)
    live_bulk = live_features(cycles_synth, None, zstats=None, n=50)
    assert live_raw.height == live_bulk.height
    assert live_raw.select(FEATURE_COLUMNS).equals(
        live_bulk.select(FEATURE_COLUMNS))


def test_antiskew_zscore_batch_equals_live(cycles_synth):
    """z-score identico a parità di zstats (stesso codice transform_zscore)."""
    from plcsim.ml_dataset import fit_normalizer, transform_zscore
    feats = compute_window_features(cycles_synth, None, n=50)
    # fit_normalizer vuole un frame con machine_code + le feature
    stats = fit_normalizer(feats)
    z_batch = transform_zscore(feats, stats)
    z_live = live_features(cycles_synth, None, zstats=stats, n=50)
    assert z_batch.select(FEATURE_COLUMNS).equals(
        z_live.select(FEATURE_COLUMNS))


# ---------------------------------------------------------------------------
# T0 — policy v1.1 pre-M9 (campi mancanti → default sani)
# ---------------------------------------------------------------------------
def test_raw_pre_m9_missing_fields_get_defaults():
    """Il raw v1.1 (senza close_reason/position_limit/filling_overtime) non
    crasha: i 3 campi sono riempiti con i default sani."""
    raw = pl.DataFrame({
        "machine_id": ["filler01"] * 3,
        "cycle_id": [1, 2, 3],
        "valve_id": [1, 1, 1],
        "data.filling_time_ms": [1900, 1950, 2000],
        "data.tail_time_ms": [300, 310, 320],
        "data.tail_pulse": [220, 230, 240],
        "data.pulse_count": [2500, 2500, 2500],
        "data.target": [2500, 2500, 2500],
        "data.delta_pulse": [0, 0, 0],
        "data.filling_step_out": [24, 24, 24],
        "data.filling_ok": [True, True, True],
        "data.fill_quality_ok": [True, True, True],
        "data.sequence_ok": [True, True, True],
        "data.sample_valid": [True, True, True],
        "data.diagnostic_status": ["NORMAL", "NORMAL", "NORMAL"],
        # NO close_reason/position_limit/filling_overtime
    })
    cycles = raw_to_valve_cycles(raw)
    assert cycles["close_reason"].to_list() == ["target", "target", "target"]
    assert cycles["position_limit"].to_list() == [False, False, False]
    assert cycles["filling_overtime"].to_list() == [False, False, False]


# ---------------------------------------------------------------------------
# F1 — policy T6: campi data.* PRESENTI-ma-null → default sani (nessun NaN)
# ---------------------------------------------------------------------------
def test_raw_null_fields_get_defaults_no_nan():
    """Il raw con campi data.* PRESENTI-ma-NULL (policy T6: tag illeggibile →
    quality.valid=false; anche il raw v1.1 storicizzato da ingest) NON
    produce NaN: ogni null è sostituito dal default sano e la matrice 43
    feature che raggiunge il modello è priva di null (fix F1 — prima:
    6 feature NaN → ValueError "Input X contains NaN" in predict_proba)."""
    n = 60  # 60 cicli: 1 finestra piena da 50 + coda parziale scartata
    raw = pl.DataFrame({
        "schema_version": ["1.1"] * n,
        "event_id": [f"00000000-0000-0000-0000-{i:012d}" for i in range(n)],
        "machine_id": ["filler01"] * n,
        "cycle_id": list(range(1, n + 1)),
        "valve_id": [1] * n,
        # KPI: PRESENTI-ma-tutti-NULL (shape del bug F1 + T6)
        "data.filling_time_ms": pl.Series([None] * n, dtype=pl.Int64),
        "data.tail_time_ms": pl.Series([None] * n, dtype=pl.Int64),
        "data.tail_pulse": pl.Series([None] * n, dtype=pl.Int64),
        "data.pulse_count": pl.Series([None] * n, dtype=pl.Int64),
        "data.target": pl.Series([None] * n, dtype=pl.Int64),
        "data.delta_pulse": pl.Series([None] * n, dtype=pl.Int64),
        "data.filling_step_out": pl.Series([None] * n, dtype=pl.Int64),
        # flag: PRESENTI-ma-tutti-NULL
        "data.filling_ok": pl.Series([None] * n, dtype=pl.Boolean),
        "data.fill_quality_ok": pl.Series([None] * n, dtype=pl.Boolean),
        "data.sequence_ok": pl.Series([None] * n, dtype=pl.Boolean),
        "data.sample_valid": pl.Series([None] * n, dtype=pl.Boolean),
        "data.position_limit": pl.Series([None] * n, dtype=pl.Boolean),
        "data.filling_overtime": pl.Series([None] * n, dtype=pl.Boolean),
        # diagnostica / motivo chiusura: PRESENTI-ma-tutti-NULL
        "data.diagnostic_status": pl.Series([None] * n, dtype=pl.String),
        "data.close_reason": pl.Series([None] * n, dtype=pl.String),
        "quality.valid": [False] * n,          # T6: record parziale
        "quality.completeness": ["partial"] * n,
    })
    cycles = raw_to_valve_cycles(raw)
    # policy (T6, decisione documentata in features.py): fill-null-with-default,
    # NIENTE drop dei cicli quality.valid=false — i 60 cicli restano (1 finestra)
    assert cycles.height == n
    for col in ("fillingtime", "tailtime", "tailpulse", "pulsecount",
                "deltapulse", "filling_step_out", "fillingok",
                "fill_quality_ok", "sequence_ok", "sample_valid",
                "position_limit", "filling_overtime",
                "diagnostic_status", "close_reason"):
        assert cycles[col].null_count() == 0, f"null residuo in {col}"
    assert cycles["close_reason"].to_list()[0] == "target"
    assert cycles["diagnostic_status"].to_list()[0] == "NORMAL"

    feats = live_features(cycles, None, zstats=None, n=50)
    assert feats.height == 1  # 60 cicli → 1 finestra piena (50), coda scartata
    # NESSUN null/NaN nelle 43 feature che raggiungerebbero il modello
    nan_cols = [c for c in FEATURE_COLUMNS if feats[c].null_count() > 0]
    assert nan_cols == [], \
        f"NaN/None in {nan_cols} — raggiungerebbero il modello"
    # i default sani producono i valori attesi (non NaN)
    assert feats["close_reason_target_rate"][0] == 1.0
    assert feats["position_limit_rate"][0] == 0.0
    assert feats["filling_overtime_rate"][0] == 0.0
    assert feats["mean_fillingtime"][0] == 0.0
    # e il modello reale, se presente, accetta la matrice senza crash
    model_path = Path("work/ml_dataset/model/model.joblib")
    if model_path.exists():
        from plcsim.ml_model import MLModel
        X = feats.select(FEATURE_COLUMNS).to_numpy()
        MLModel.load(model_path).predict_proba(X)  # non alza "Input X contains NaN"
