"""Unit test del consumer M8-04 — senza Docker/broker (spec §8, T2/T6 + dedup/flush/partitioning/stats).

Copertura (issue M8-04, criteri di done):
- T2: validatore wire v1.0 — happy path + mutazioni rifiutate (ingest_ts
  PRESENTE nel wire, schema_version errata, recipe_id, campo mancante, tipo
  errato, event_id non uuid);
- T6: iniezione ``ingest_ts`` + stored v1.1 — happy, record senza ingest_ts
  rifiutato, recipe_id rifiutato, schema_version errata, date-time cattivo,
  doppia iniezione → ValueError, policy partial (null);
- dedup: set in memoria, rebuild da Parquet, ``--dedup-store`` (load + append);
- flush atomico (nessun file parziale), partizionamento da ``event_ts`` UTC,
  stats (identità received == written + duplicates + scartati), stats-json.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest
from jsonschema import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.ingest import (  # noqa: E402
    FLATTENED_COLUMNS,
    IngestConsumer,
    flatten_record,
    partition_of,
    records_to_df,
)
from pipeline.validator import inject_ingest_ts, validate_stored, validate_wire  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture: envelope wire v1.0 valido (spec M7 §4.2, parity_check)
# ---------------------------------------------------------------------------
def make_wire(**overrides) -> dict:
    payload = {
        "schema_version": "1.0",
        "event_id": str(uuid.uuid4()),
        "event_type": "valve_cycle",
        "event_ts": "2026-08-12T10:00:00.000Z",
        "source_ts": "2026-08-12T09:59:59.900Z",
        "machine_id": "filler01",
        "cycle_id": 42,
        "valve_id": 1,
        "data": {
            "filling_time_ms": 3200,
            "tail_time_ms": 150,
            "tail_pulse": 2,
            "pulse_count": 2500,
            "target": 2500,
            "delta_pulse": 0,
            "filling_step_out": 7,
            "filling_ok": True,
            "fill_quality_ok": True,
            "sequence_ok": True,
            "sample_valid": True,
            "diagnostic_status": "NORMAL",
        },
        "quality": {"valid": True, "completeness": "complete"},
    }
    payload.update(overrides)
    return payload


def make_partial_wire(**overrides) -> dict:
    """Record partial (policy T6): campi data.* non leggibili → null."""
    wire = make_wire(**overrides)
    wire["data"] = {key: None for key in wire["data"]}
    wire["data"]["diagnostic_status"] = None
    wire["quality"] = {"valid": False, "completeness": "partial"}
    return wire


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def consumer(tmp_path: Path) -> IngestConsumer:
    return IngestConsumer(out=tmp_path / "raw", flush_records=100, flush_seconds=60.0)


# ===========================================================================
# T2 — validazione wire v1.0
# ===========================================================================
class TestWireV10:
    def test_t2_wire_happy(self):
        validate_wire(make_wire())  # non alza

    def test_t2_wire_rejects_ingest_ts_present(self):
        with pytest.raises(ValidationError):
            validate_wire(make_wire(ingest_ts="2026-08-12T10:00:01.000Z"))

    def test_t2_wire_rejects_bad_schema_version(self):
        with pytest.raises(ValidationError):
            validate_wire(make_wire(schema_version="1.1"))

    def test_t2_wire_rejects_recipe_id(self):
        with pytest.raises(ValidationError):
            validate_wire(make_wire(recipe_id="maxima"))

    def test_t2_wire_rejects_missing_field(self):
        wire = make_wire()
        del wire["data"]["filling_time_ms"]
        with pytest.raises(ValidationError):
            validate_wire(wire)

    def test_t2_wire_rejects_wrong_type(self):
        with pytest.raises(ValidationError):
            validate_wire(make_wire(cycle_id="not-an-int"))

    def test_t2_wire_rejects_non_uuid_event_id(self):
        with pytest.raises(ValidationError):
            validate_wire(make_wire(event_id="not-a-uuid"))


# ===========================================================================
# T6 — iniezione ingest_ts + stored v1.1
# ===========================================================================
class TestStoredV11:
    def test_t6_inject_happy_stored_valid(self):
        wire = make_wire()
        record = inject_ingest_ts(wire, now_utc())
        assert record["schema_version"] == "1.1"
        assert record["ingest_ts"].endswith("Z")
        assert record is not wire
        assert "ingest_ts" not in wire  # input non modificato (copia)
        validate_stored(record)  # non alza

    def test_t6_stored_without_ingest_ts_rejected(self):
        with pytest.raises(ValidationError):
            validate_stored(make_wire())  # wire v1.0: ingest_ts ASSENTE

    def test_t6_stored_rejects_recipe_id(self):
        record = inject_ingest_ts(make_wire(), now_utc())
        record["recipe_id"] = "maxima"
        with pytest.raises(ValidationError):
            validate_stored(record)

    def test_t6_stored_rejects_wrong_schema_version(self):
        record = inject_ingest_ts(make_wire(), now_utc())
        record["schema_version"] = "1.0"
        with pytest.raises(ValidationError):
            validate_stored(record)

    @pytest.mark.parametrize("bad_ts", ["non-una-data", "2026-02-30T10:00:00Z"])
    def test_t6_stored_rejects_bad_datetime(self, bad_ts):
        record = inject_ingest_ts(make_wire(), now_utc())
        record["ingest_ts"] = bad_ts
        with pytest.raises(ValidationError):
            validate_stored(record)

    def test_t6_double_injection_raises_valueerror(self):
        record = inject_ingest_ts(make_wire(), now_utc())
        with pytest.raises(ValueError):
            inject_ingest_ts(record, now_utc())

    def test_t6_partial_record_valid_with_nulls(self):
        wire = make_partial_wire()
        validate_wire(wire)  # il wire partial è comunque v1.0-valid (null ammessi)
        record = inject_ingest_ts(wire, now_utc())
        validate_stored(record)  # policy partial (calibration T6): record valido


# ===========================================================================
# Consumer: elaborazione, dedup, flush, partizionamento, stats
# ===========================================================================
class TestConsumerProcessing:
    def test_consumer_buffers_then_flushes_atomically(self, consumer, tmp_path):
        for i in range(3):
            assert consumer.handle_payload(
                json.dumps(make_wire(cycle_id=i)).encode()) == "buffered"
        assert consumer.stats["written"] == 0
        result = consumer.flush()
        assert result == {"written": 3, "parquet_written": 1}
        assert consumer.stats["written"] == 3
        assert consumer.stats["parquet_written"] == 1
        target = tmp_path / "raw" / "machine=filler01" / "date=2026-08-12" / "valve_cycles.parquet"
        assert target.exists()
        df = pl.read_parquet(target)
        assert df.height == 3
        assert df.columns == list(FLATTENED_COLUMNS)
        # flush atomico: nessun file temporaneo parziale
        assert list(target.parent.glob("*.tmp.*")) == []

    def test_consumer_auto_flush_on_flush_records(self, tmp_path):
        c = IngestConsumer(out=tmp_path / "raw", flush_records=2, flush_seconds=60.0)
        assert c.handle_payload(json.dumps(make_wire(cycle_id=1))) == "buffered"
        assert c.handle_payload(json.dumps(make_wire(cycle_id=2))) == "written"
        assert c.stats["written"] == 2
        assert c.stats["parquet_written"] == 1
        assert (tmp_path / "raw" / "machine=filler01" / "date=2026-08-12"
                / "valve_cycles.parquet").exists()

    def test_consumer_dedup_on_event_id(self, consumer):
        wire = make_wire(cycle_id=7)
        payload = json.dumps(wire)
        assert consumer.handle_payload(payload) == "buffered"
        assert consumer.handle_payload(payload) == "duplicate"  # stesso event_id
        assert consumer.stats["duplicates"] == 1
        consumer.flush()
        assert consumer.stats["written"] == 1
        df = pl.read_parquet(consumer.out / "machine=filler01" / "date=2026-08-12"
                             / "valve_cycles.parquet")
        assert df.height == 1

    def test_consumer_invalid_payload_counters(self, consumer):
        assert consumer.handle_payload(b"not-json") == "json_invalid"
        assert consumer.handle_payload(b"\xff\xfe") == "json_invalid"  # utf-8 invalido
        assert consumer.handle_payload(
            json.dumps(make_wire(event_id="not-a-uuid"))) == "schema_invalid"
        assert consumer.handle_payload(
            json.dumps(make_wire(ingest_ts="2026-08-12T10:00:01Z"))) == "schema_invalid"
        assert consumer.stats["json_invalid"] == 2
        assert consumer.stats["schema_invalid"] == 2
        # identità stats: received == written + duplicates + json_invalid + schema_invalid
        received = consumer.stats["received"]
        accounted = (consumer.stats["written"] + consumer.stats["duplicates"]
                     + consumer.stats["json_invalid"] + consumer.stats["schema_invalid"])
        assert received == accounted == 4

    def test_consumer_partial_record_roundtrip_nulls(self, consumer, tmp_path):
        wire = make_partial_wire()
        assert consumer.handle_payload(json.dumps(wire)) == "buffered"
        consumer.flush()
        df = pl.read_parquet(consumer.out / "machine=filler01" / "date=2026-08-12"
                             / "valve_cycles.parquet")
        assert df.height == 1
        assert df["data.filling_time_ms"][0] is None
        assert df["data.filling_ok"][0] is None
        assert df["quality.valid"][0] is False
        assert df["quality.completeness"][0] == "partial"
        assert df.schema["cycle_id"] == pl.Int64
        assert df.schema["data.filling_ok"] == pl.Boolean

    def test_consumer_multiple_partitions_one_flush(self, consumer):
        assert consumer.handle_payload(json.dumps(make_wire(cycle_id=1))) == "buffered"
        assert consumer.handle_payload(json.dumps(
            make_wire(cycle_id=2, event_ts="2026-08-13T00:00:00.000Z"))) == "buffered"
        consumer.flush()
        assert consumer.stats["parquet_written"] == 2
        assert (consumer.out / "machine=filler01" / "date=2026-08-12"
                / "valve_cycles.parquet").exists()
        assert (consumer.out / "machine=filler01" / "date=2026-08-13"
                / "valve_cycles.parquet").exists()


class TestDedupRebuild:
    def test_rebuild_from_parquet(self, tmp_path):
        c1 = IngestConsumer(out=tmp_path / "raw", flush_records=100, flush_seconds=60.0)
        eid = str(uuid.uuid4())
        c1.handle_payload(json.dumps(make_wire(event_id=eid)))
        c1.handle_payload(json.dumps(make_wire()))  # secondo record
        c1.flush()
        # nuovo consumer sullo stesso out: rebuild da Parquet
        c2 = IngestConsumer(out=tmp_path / "raw", flush_records=100, flush_seconds=60.0)
        assert c2.rebuild_dedup() == 2
        assert c2.handle_payload(json.dumps(make_wire(event_id=eid))) == "duplicate"
        assert c2.stats["duplicates"] == 1
        # nuovo event_id non duplicato
        assert c2.handle_payload(json.dumps(make_wire(cycle_id=99))) == "buffered"

    def test_rebuild_empty_when_no_parquet(self, tmp_path):
        c = IngestConsumer(out=tmp_path / "raw", flush_records=100, flush_seconds=60.0)
        assert c.rebuild_dedup() == 0
        assert c.seen == set()

    def test_dedup_store_load_and_append(self, tmp_path):
        store = tmp_path / "dedup.jsonl"
        c1 = IngestConsumer(out=tmp_path / "raw", flush_records=100, flush_seconds=60.0,
                            dedup_store=store)
        eid = str(uuid.uuid4())
        c1.handle_payload(json.dumps(make_wire(event_id=eid)))
        c1.flush()
        lines = store.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"event_id": eid}
        # append al flush successivo (nessuna sovrascrittura)
        c1.handle_payload(json.dumps(make_wire(cycle_id=5)))
        c1.flush()
        assert len(store.read_text(encoding="utf-8").splitlines()) == 2
        # nuovo consumer: load dallo store → dedup
        c2 = IngestConsumer(out=tmp_path / "raw", flush_records=100, flush_seconds=60.0,
                            dedup_store=store)
        assert c2.rebuild_dedup() == 2
        assert c2.handle_payload(json.dumps(make_wire(event_id=eid))) == "duplicate"

    def test_dedup_store_optional_off(self, tmp_path):
        c = IngestConsumer(out=tmp_path / "raw", flush_records=100, flush_seconds=60.0)
        c.handle_payload(json.dumps(make_wire()))
        c.flush()
        assert list(tmp_path.glob("dedup.jsonl")) == []

    def test_rebuild_failure_keeps_running(self, tmp_path):
        """Rebuild con dir non leggibile/path invalido: WARNING + set vuoto."""
        c = IngestConsumer(out=tmp_path / "raw", flush_records=100, flush_seconds=60.0)
        # out inesistente → nessun errore, set vuoto
        assert c.rebuild_dedup() == 0


class TestPartitioningAndStats:
    def test_partition_uses_event_ts_utc_date_not_ingest_ts(self, tmp_path):
        c = IngestConsumer(out=tmp_path / "raw", flush_records=100, flush_seconds=60.0)
        # event_ts del giorno prima, ingest avviene il giorno dopo
        wire = make_wire(event_ts="2026-08-11T23:59:59.000Z", cycle_id=1)
        assert partition_of(inject_ingest_ts(wire, datetime(
            2026, 8, 12, 0, 0, 1, tzinfo=timezone.utc))) == ("filler01", "2026-08-11")
        c.handle_payload(json.dumps(wire))
        c.flush()
        assert (tmp_path / "raw" / "machine=filler01" / "date=2026-08-11"
                / "valve_cycles.parquet").exists()
        assert not (tmp_path / "raw" / "machine=filler01" / "date=2026-08-12").exists()

    def test_flush_atomic_no_partial_files(self, consumer):
        for i in range(5):
            consumer.handle_payload(json.dumps(make_wire(cycle_id=i)))
        consumer.flush()
        # nessun tmp in tutta la tree
        assert list(consumer.out.glob("**/*.tmp.*")) == []
        # file leggibile e completo
        df = pl.read_parquet(consumer.out / "machine=filler01" / "date=2026-08-12"
                             / "valve_cycles.parquet")
        assert df.height == 5

    def test_stats_json_atomic(self, tmp_path):
        stats_path = tmp_path / "work" / "ingest_stats.json"
        c = IngestConsumer(out=tmp_path / "raw", flush_records=100, flush_seconds=60.0,
                           stats_json=stats_path)
        c.handle_payload(json.dumps(make_wire()))
        payload_dup = json.dumps(make_wire())
        c.handle_payload(payload_dup)
        c.handle_payload(payload_dup)  # stesso event_id → duplicato
        c.handle_payload(payload_dup)  # duplicato di nuovo
        c.handle_payload(b"garbage")
        c.flush()
        c.write_stats_json(stats_path)
        data = json.loads(stats_path.read_text(encoding="utf-8"))
        assert data["received"] == 5
        assert data["written"] == 2
        assert data["duplicates"] == 2
        assert data["json_invalid"] == 1
        assert data["schema_invalid"] == 0
        assert data["reconnect_count"] == 0
        assert data["parquet_written"] == 1
        assert data["start_ts"].endswith("Z")
        assert data["end_ts"].endswith("Z")
        assert list(stats_path.parent.glob("*.tmp.*")) == []  # scrittura atomica

    def test_flush_records_and_seconds_validation(self, tmp_path):
        with pytest.raises(ValueError):
            IngestConsumer(out=tmp_path, flush_records=0)
        with pytest.raises(ValueError):
            IngestConsumer(out=tmp_path, flush_seconds=0)


class TestHelpers:
    def test_flatten_record_matches_spec_columns(self):
        record = inject_ingest_ts(make_wire(), now_utc())
        flat = flatten_record(record)
        assert list(flat.keys()) == list(FLATTENED_COLUMNS)
        assert flat["schema_version"] == "1.1"
        assert flat["data.filling_time_ms"] == 3200
        assert flat["quality.valid"] is True

    def test_records_to_df_explicit_schema(self):
        records = [inject_ingest_ts(make_wire(), now_utc())]
        df = records_to_df(records)
        assert df.columns == list(FLATTENED_COLUMNS)
        assert df.schema["cycle_id"] == pl.Int64
        assert df.schema["quality.valid"] == pl.Boolean
        assert df.schema["event_id"] == pl.String

    def test_flush_on_empty_buffer_returns_none(self, consumer):
        assert consumer.flush() is None
