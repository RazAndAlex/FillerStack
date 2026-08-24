"""Test del replay offline run bulk → raw canonico (pipeline/raw_replay.py).

Copertura:
- il frame prodotto ha ESATTAMENTE `FLATTENED_COLUMNS`, nell'ordine canonico,
  con i tipi di `COLUMN_TYPES` (il layout viene da pipeline/ingest.py, non è
  riscritto qui);
- mapping bulk → data.*: rinomina 1:1, valori identici alla sorgente;
- `machine_code` "valveN" (0-based) → `valve_id` N+1, stessa mappa di
  cycles_backfill; codice fuori formato o fuori 1-35 → errore chiaro;
- onestà: event_id / source_ts / ingest_ts / quality.* restano NULL (campi
  del transport MQTT, mai fabbricati);
- partizionamento per data UTC di `event_ts` (due date → due file);
- scrittura: percorso hive `machine=<id>/date=<UTC>/valve_cycles.parquet`,
  rifiuto di sovrascrivere senza --overwrite;
- roundtrip: il raw scritto è leggibile da `pipeline.features.raw_to_valve_cycles`.

Nessun DB, nessun broker: polars in-memory + tmp_path.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.ingest import COLUMN_TYPES, FLATTENED_COLUMNS  # noqa: E402
from pipeline.raw_replay import (  # noqa: E402
    DATA_FROM_BULK,
    RawReplayError,
    build_partitions,
    load_bulk,
    replay,
    valve_id_of,
    write_partition,
)


def _bulk(n: int = 3, day: str = "2026-06-01", codes=("valve0", "valve12")) -> pl.DataFrame:
    rows = []
    for ci in range(1, n + 1):
        for code in codes:
            rows.append({
                "machine_code": code,
                "ts_beg": datetime.fromisoformat(f"{day}T08:00:0{ci}+00:00"),
                "fillingtime": 1800 + ci, "tailtime": 350, "tailpulse": 270,
                "pulsecount": 2500, "target": 2500, "deltapulse": 0,
                "filling_step_out": 24, "fillingok": True,
                "fill_quality_ok": True, "sequence_ok": True,
                "sample_valid": True, "position_limit": False,
                "filling_overtime": False, "diagnostic_status": "NORMAL",
                "close_reason": "target", "cycle_id": ci, "scenario_id": 1,
            })
    return pl.DataFrame(rows)


def test_valve_id_mapping():
    assert valve_id_of("valve0") == 1
    assert valve_id_of("valve34") == 35
    with pytest.raises(RawReplayError):
        valve_id_of("valve35")          # fuori contratto 1-35
    with pytest.raises(RawReplayError):
        valve_id_of("MACHINE")          # formato non riconosciuto


def test_layout_e_tipi_canonici():
    parts = build_partitions(_bulk(), "filler01")
    assert list(parts) == [("filler01", "2026-06-01")]
    frame = parts[("filler01", "2026-06-01")]
    assert tuple(frame.columns) == FLATTENED_COLUMNS
    for col, dtype in COLUMN_TYPES.items():
        assert frame.schema[col] == dtype, col


def test_mapping_valori_e_null_onesti():
    frame = build_partitions(_bulk(n=1, codes=("valve12",)), "filler01")
    frame = frame[("filler01", "2026-06-01")]
    row = frame.row(0, named=True)
    assert row["machine_id"] == "filler01"
    assert row["valve_id"] == 13          # valve12 → 13
    assert row["cycle_id"] == 1
    assert row["event_ts"].startswith("2026-06-01T08:00:01")
    src = _bulk(n=1, codes=("valve12",)).row(0, named=True)
    for data_key, bulk_col in DATA_FROM_BULK.items():
        assert row[f"data.{data_key}"] == src[bulk_col], data_key
    # campi del transport MQTT: mai fabbricati
    for absent in ("event_id", "source_ts", "ingest_ts",
                   "quality.valid", "quality.completeness"):
        assert row[absent] is None, absent


def test_partizionamento_per_data_utc():
    df = pl.concat([_bulk(n=1, day="2026-06-01"), _bulk(n=1, day="2026-06-02")])
    parts = build_partitions(df, "filler01")
    assert sorted(parts) == [("filler01", "2026-06-01"), ("filler01", "2026-06-02")]
    assert all(f.height == 2 for f in parts.values())


def test_scrittura_hive_e_niente_sovrascrittura(tmp_path):
    src = tmp_path / "valve_cycles.parquet"
    _bulk().write_parquet(src)
    out = tmp_path / "raw"
    summary = replay(src, "filler01", out)
    target = out / "machine=filler01" / "date=2026-06-01" / "valve_cycles.parquet"
    assert target.exists()
    assert summary["rows_written"] == summary["source_rows"] == 6
    with pytest.raises(FileExistsError):
        replay(src, "filler01", out)              # storico raw protetto
    replay(src, "filler01", out, overwrite=True)  # esplicito: consentito
    assert pl.read_parquet(target).height == 6


def test_load_bulk_rifiuta_layout_non_bulk(tmp_path):
    p = tmp_path / "x.parquet"
    pl.DataFrame({"a": [1]}).write_parquet(p)
    with pytest.raises(RawReplayError):
        load_bulk(p)
    with pytest.raises(RawReplayError):
        load_bulk(tmp_path / "manca.parquet")


def test_roundtrip_verso_features(tmp_path):
    from pipeline.features import raw_to_valve_cycles
    frame = build_partitions(_bulk(n=1, codes=("valve12",)), "filler01")
    frame = frame[("filler01", "2026-06-01")]
    vc = raw_to_valve_cycles(frame)
    # features rimappa valve_id 13 → machine_code "valve12" (inverso esatto)
    assert vc["machine_code"].to_list() == ["valve12"]
    assert vc["fillingtime"].to_list() == [1801]
