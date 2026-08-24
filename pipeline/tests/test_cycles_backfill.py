"""Test del backfill `cycles` (M10) — pipeline/cycles_backfill.py.

Copertura:
- mapping raw appiattito (envelope v1.3, 15 campi) → 22 colonne operazionali:
  rinomina 1:1, machine_id/valve_id/cycle_id pass-through, delta_pulse AS-IS;
  event_ts/source_ts/ingest_ts da String ISO → timestamptz; assenti → NULL;
- raw v1.1 pre-M9 (12 campi): close_reason/position_limit/filling_overtime
  assenti → NULL (nessuna fabbricazione di default "sani", a differenza di
  features.py che li azzera per l'anti-skew ML);
- run bulk del simulatore (machine_code "valveN"): valve_id derivato N+1,
  machine_id NULL (assente nel layout), ts_beg → event_ts, source_ts/ingest_ts NULL;
- event_ts population + null fallback (onestà: sorgente senza campo → NULL);
- identità CONTEXT.md: delta_pulse == target − pulse_count sui run bulk;
- filtro --valve (contratto 1-35), fonte assente/vuota → NoRawDataError,
  layout sconosciuto/misto → RawFormatError, duplicati (valve_id, cycle_id)
  → RawFormatError (mai scelta silenziosa di riga);
- CYCLES_COLUMNS length 22 (run_id in testa) + ordine stabile dopo filling_overtime;
- integrazione differita: smoke su DB privato con `CyclesStorage` (SKIPPED
  finché pipeline/cycles_storage.py non è installato e non c'è un
  PostgreSQL raggiungibile) — idempotenza del re-run (stesso count, 0 dupes).

Nessun Docker, nessun broker: tutto polars in-memory deterministico.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.cycles_backfill import (  # noqa: E402
    CYCLES_COLUMNS,
    BackfillError,
    BackfillResult,
    NoRawDataError,
    RawFormatError,
    backfill,
    load_cycles,
)

# ---------------------------------------------------------------------------
# Fixture: raw appiattito sintetico (stessa struttura di pipeline/ingest.py)
# ---------------------------------------------------------------------------
_INT_FIELDS = ("filling_time_ms", "tail_time_ms", "tail_pulse", "pulse_count",
               "target", "delta_pulse", "filling_step_out")
_BOOL_FIELDS = ("filling_ok", "fill_quality_ok", "sequence_ok", "sample_valid",
                "position_limit", "filling_overtime")
_V13_FIELDS = _INT_FIELDS + _BOOL_FIELDS + ("diagnostic_status", "close_reason")
_V11_FIELDS = _INT_FIELDS + _BOOL_FIELDS[:4] + ("diagnostic_status",)  # 12 campi pre-M9


def _flat_rows(fields, n_valves=2, n_cycles=3, machine="filler01",
               include_ts=True, ts_value="2026-08-01T10:00:00Z",
               source_ts_value="2026-08-01T10:00:00.500Z",
               ingest_ts_value="2026-08-01T10:00:01Z",
               cid_start=1):
    rows = []
    for valve in range(1, n_valves + 1):
        for cid in range(cid_start, cid_start + n_cycles):
            data = {
                "filling_time_ms": 1900 + cid * 5,
                "tail_time_ms": 300 + cid * 8,
                "tail_pulse": 220 + cid,
                "pulse_count": 2500 + cid - 2,
                "target": 2500,
                "delta_pulse": 2 - cid,
                "filling_step_out": 24 + cid % 3,
                "filling_ok": True,
                "fill_quality_ok": True,
                "sequence_ok": True,
                "sample_valid": True,
                "position_limit": False,
                "filling_overtime": False,
                "diagnostic_status": "NORMAL",
                "close_reason": "target",
            }
            flat = {"machine_id": machine, "cycle_id": cid, "valve_id": valve}
            if include_ts:
                flat["event_ts"] = ts_value
                flat["source_ts"] = source_ts_value
                flat["ingest_ts"] = ingest_ts_value
            for k in fields:
                flat[f"data.{k}"] = data[k]
            rows.append(flat)
    return rows


def _write_flat_raw(root: Path, fields, date="2026-08-01", **kw) -> Path:
    part = root / "machine=filler01" / f"date={date}"
    part.mkdir(parents=True, exist_ok=True)
    path = part / "valve_cycles.parquet"
    pl.DataFrame(_flat_rows(fields, **kw)).write_parquet(path)
    return path


@pytest.fixture
def raw_flattened_v13(tmp_path: Path) -> Path:
    """data/raw v1.3 (15 campi): 2 valvole × 3 cicli, con timestamps."""
    return _write_flat_raw(tmp_path, _V13_FIELDS)


@pytest.fixture
def raw_flattened_v11(tmp_path: Path) -> Path:
    """data/raw v1.1 pre-M9 (12 campi): stesse righe, 3 campi in meno, con ts."""
    return _write_flat_raw(tmp_path, _V11_FIELDS)


@pytest.fixture
def raw_flattened_no_ts(tmp_path: Path) -> Path:
    """data/raw v1.3 ma SENZA event_ts/source_ts/ingest_ts (simula sorgente vecchia)."""
    return _write_flat_raw(tmp_path, _V13_FIELDS, include_ts=False)


@pytest.fixture
def raw_mixed(tmp_path: Path) -> Path:
    """v1.3 + v1.1 in date diverse (stessa fonte, due versioni envelope).

    I cicli del secondo giorno proseguono la numerazione (4-6) invece di
    ripartire da 1. Un giorno successivo che riusa gli stessi cycle_id non
    esiste sulla macchina, e la tabella `cycles` non potrebbe rappresentarlo:
    la chiave e' (valve_id, cycle_id). Cio' che questo caso deve provare e' il
    merge fra due versioni di envelope, non una collisione di chiave.
    """
    root = _write_flat_raw(tmp_path, _V13_FIELDS).parent.parent
    _write_flat_raw(root, _V11_FIELDS, date="2026-08-02", cid_start=4)
    return root


@pytest.fixture
def run_bulk(tmp_path: Path) -> Path:
    """run bulk del simulatore (telemetry.py): 2 valvole × 3 cicli, ts_beg datetime."""
    rows = []
    for v in (0, 11):  # valve1, valve12 (0-based)
        for cid in range(1, 4):
            rows.append({
                "machine_code": f"valve{v}",
                "cycle_id": cid,
                "fillingtime": 1900 + cid * 5,
                "tailtime": 300 + cid * 8,
                "tailpulse": 220 + cid,
                "pulsecount": 2500 + cid - 2,
                "target": 2500,
                "deltapulse": 2 - cid,
                "filling_step_out": 24 + cid % 3,
                "fillingok": True,
                "fill_quality_ok": True,
                "sequence_ok": True,
                "sample_valid": True,
                "diagnostic_status": "NORMAL",
                "close_reason": "target",
                "position_limit": False,
                "filling_overtime": False,
                "ts_beg": datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
            })
    run = tmp_path / "run_x"
    run.mkdir(parents=True, exist_ok=True)
    path = run / "valve_cycles.parquet"
    pl.DataFrame(rows, schema_overrides={"ts_beg": pl.Datetime(time_zone="UTC")}).write_parquet(path)
    return path


# ---------------------------------------------------------------------------
# Contratto CYCLES_COLUMNS
# ---------------------------------------------------------------------------
def test_cycles_columns_length_and_order():
    assert len(CYCLES_COLUMNS) == 22
    assert CYCLES_COLUMNS == (
        "run_id",
        "machine_id", "cycle_id", "valve_id",
        "filling_time_ms", "tail_time_ms", "tail_pulse",
        "pulse_count", "target", "delta_pulse", "filling_step_out",
        "filling_ok", "fill_quality_ok", "sequence_ok", "sample_valid",
        "diagnostic_status", "close_reason", "position_limit", "filling_overtime",
        "event_ts", "source_ts", "ingest_ts",
    )
    # ordre stabile: dopo filling_overtime aggiungi event_ts, source_ts, ingest_ts
    assert CYCLES_COLUMNS[0] == "run_id"
    assert CYCLES_COLUMNS[18] == "filling_overtime"
    assert CYCLES_COLUMNS[19] == "event_ts"
    assert CYCLES_COLUMNS[20] == "source_ts"
    assert CYCLES_COLUMNS[21] == "ingest_ts"


# ---------------------------------------------------------------------------
# Mapping: raw appiattito v1.3 + timestamps
# ---------------------------------------------------------------------------
def test_flattened_v13_maps_all_22_columns(raw_flattened_v13):
    cycles = load_cycles(raw_flattened_v13.parent.parent)
    assert cycles.columns == list(CYCLES_COLUMNS)
    assert cycles.height == 6
    # pass-through chiavi
    assert cycles["machine_id"].to_list() == ["filler01"] * 6
    assert sorted(cycles["valve_id"].unique().to_list()) == [1, 2]
    # rinomina 1:1 dei KPI
    row = cycles.filter(pl.col("valve_id") == 1,
                        pl.col("cycle_id") == 2).row(0, named=True)
    assert row["filling_time_ms"] == 1910
    assert row["tail_time_ms"] == 316
    assert row["tail_pulse"] == 222
    assert row["pulse_count"] == 2500
    assert row["target"] == 2500
    assert row["delta_pulse"] == 0          # AS-IS dalla fonte raw
    assert row["filling_step_out"] == 26
    assert row["filling_ok"] is True
    assert row["fill_quality_ok"] is True
    assert row["sequence_ok"] is True
    assert row["sample_valid"] is True
    assert row["diagnostic_status"] == "NORMAL"
    assert row["close_reason"] == "target"
    assert row["position_limit"] is False
    assert row["filling_overtime"] is False


def test_flattened_v13_event_ts_populated(raw_flattened_v13):
    cycles = load_cycles(raw_flattened_v13.parent.parent)
    assert cycles.height == 6
    # event_ts/source_ts/ingest_ts popolati da String ISO → Datetime UTC
    assert cycles["event_ts"].null_count() == 0
    assert cycles["source_ts"].null_count() == 0
    assert cycles["ingest_ts"].null_count() == 0
    assert cycles["event_ts"].dtype == pl.Datetime(time_zone="UTC")
    assert cycles["source_ts"].dtype == pl.Datetime(time_zone="UTC")
    assert cycles["ingest_ts"].dtype == pl.Datetime(time_zone="UTC")
    # valore atteso
    expected = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    assert cycles["event_ts"][0] == expected


def test_flattened_no_ts_null_fallback(raw_flattened_no_ts):
    """Sorgente senza event_ts/source_ts/ingest_ts → NULL (mai fabbricare)."""
    cycles = load_cycles(raw_flattened_no_ts.parent.parent)
    assert cycles.height == 6
    assert cycles.columns == list(CYCLES_COLUMNS)
    assert cycles["event_ts"].null_count() == 6
    assert cycles["source_ts"].null_count() == 6
    assert cycles["ingest_ts"].null_count() == 6
    # dtype deve essere Datetime UTC anche quando tutto null (non generico Null)
    assert cycles["event_ts"].dtype == pl.Datetime(time_zone="UTC")
    assert cycles["source_ts"].dtype == pl.Datetime(time_zone="UTC")
    assert cycles["ingest_ts"].dtype == pl.Datetime(time_zone="UTC")


def test_flattened_null_ts_stays_null(tmp_path):
    """event_ts presente ma valore NULL (policy T6) → resta NULL."""
    part = tmp_path / "machine=filler01" / "date=2026-08-01"
    part.mkdir(parents=True, exist_ok=True)
    rows = _flat_rows(_V13_FIELDS, n_valves=1, n_cycles=2)
    rows[0]["event_ts"] = None
    rows[0]["source_ts"] = None
    rows[0]["ingest_ts"] = None
    pl.DataFrame(rows).write_parquet(part / "valve_cycles.parquet")
    cycles = load_cycles(tmp_path)
    assert cycles.height == 2
    # prima riga null, seconda popolata
    assert cycles.sort(["valve_id", "cycle_id"])["event_ts"].null_count() == 1


# ---------------------------------------------------------------------------
# Mapping: raw v1.1 pre-M9 → NULL onesti, mai default fabbricati
# ---------------------------------------------------------------------------
def test_flattened_v11_missing_fields_are_null(raw_flattened_v11):
    cycles = load_cycles(raw_flattened_v11.parent.parent)
    assert cycles.height == 6
    assert cycles["close_reason"].null_count() == 6
    assert cycles["position_limit"].null_count() == 6
    assert cycles["filling_overtime"].null_count() == 6
    # il resto è mappato normalmente (non si perde nulla)
    assert cycles["filling_time_ms"].null_count() == 0
    assert cycles["diagnostic_status"].null_count() == 0
    # timestamps presenti → popolati anche su v1.1
    assert cycles["event_ts"].null_count() == 0


def test_flattened_mixed_versions_merge(raw_mixed):
    """v1.3 + v1.1 nella stessa fonte: merge verticale, NULL solo dove assenti."""
    cycles = load_cycles(raw_mixed)
    assert cycles.height == 12
    # 6 righe v1.1 (date=2026-08-02) con close_reason NULL, 6 con "target"
    assert cycles["close_reason"].null_count() == 6
    assert cycles["close_reason"].drop_nulls().unique().to_list() == ["target"]


# ---------------------------------------------------------------------------
# Mapping: run bulk del simulatore (ts_beg → event_ts)
# ---------------------------------------------------------------------------
def test_bulk_run_maps_and_derives_valve_id(run_bulk):
    cycles = load_cycles(run_bulk.parent)  # puntata la run_dir
    assert cycles.columns == list(CYCLES_COLUMNS)
    assert cycles.height == 6
    # machine_code "valve0"/"valve11" → valve_id 1/12 (contratto 1-35)
    assert sorted(cycles["valve_id"].unique().to_list()) == [1, 12]
    # ordinato per (valve_id, cycle_id): cid 1,2,3 per ogni valvola
    assert cycles["filling_time_ms"].to_list() == [1905, 1910, 1915, 1905, 1910, 1915]
    # machine_id assente nei run bulk → NULL (nessuna fabbricazione)
    assert cycles["machine_id"].null_count() == 6
    # identità CONTEXT.md verificata sulla fonte reale
    assert (cycles["delta_pulse"]
            == (cycles["target"] - cycles["pulse_count"])).all()


def test_bulk_run_event_ts_populated_from_ts_beg(run_bulk):
    cycles = load_cycles(run_bulk.parent)
    assert cycles.height == 6
    assert cycles["event_ts"].null_count() == 0
    assert cycles["event_ts"].dtype == pl.Datetime(time_zone="UTC")
    expected = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    assert cycles["event_ts"][0] == expected
    # source_ts/ingest_ts assenti nel bulk → NULL (onestà)
    assert cycles["source_ts"].null_count() == 6
    assert cycles["ingest_ts"].null_count() == 6


def test_bulk_run_direct_path(run_bulk):
    """Anche puntando direttamente il file (glob ** lo raggiunge)."""
    cycles = load_cycles(run_bulk)
    assert cycles.height == 6


def test_backfill_bulk_without_machine_id_clear_error(run_bulk):
    """machine_id è NOT NULL nel DDL di cycles: fonte senza la colonna →
    errore chiaro PRIMA del DB, identità macchina mai dedotta."""
    with pytest.raises(BackfillError, match="machine_id"):
        backfill(run_bulk.parent, run_id="run_test")


# ---------------------------------------------------------------------------
# Filtri e difese
# ---------------------------------------------------------------------------
def test_valve_filter(raw_flattened_v13):
    cycles = load_cycles(raw_flattened_v13.parent.parent, valve=2)
    assert cycles.height == 3
    assert cycles["valve_id"].unique().to_list() == [2]
    assert cycles["cycle_id"].to_list() == [1, 2, 3]  # ordinato per ciclo


def test_dates_filter_selects_exact_partition_and_excludes_june(tmp_path):
    """La selezione live legge solo la partizione data richiesta."""
    _write_flat_raw(tmp_path, _V13_FIELDS, date="2026-06-30", cid_start=1)
    _write_flat_raw(tmp_path, _V13_FIELDS, date="2026-08-21", cid_start=4)

    cycles = load_cycles(tmp_path, dates=["2026-08-21"])

    assert cycles.height == 6
    assert cycles["cycle_id"].to_list() == [4, 5, 6, 4, 5, 6]
    with pytest.raises(NoRawDataError):
        load_cycles(tmp_path, dates=["2026-07-01"])


def test_dates_cli_is_propagated_to_backfill(tmp_path, monkeypatch):
    """La CLI inoltra la selezione esplicita senza fallback a tutta la root."""
    from pipeline import cycles_backfill as module

    seen = {}

    def fake_backfill(raw_dir, run_id, db_url=None, valve=None,
                      machine_id=None, dates=None):
        seen.update(raw_dir=raw_dir, run_id=run_id, db_url=db_url,
                    valve=valve, machine_id=machine_id, dates=dates)
        return BackfillResult(files=[], rows_read=0, rows_mapped=0,
                              rows_inserted=0)

    monkeypatch.setattr(module, "backfill", fake_backfill)
    assert module.main([
        "--run-id", "live-2026-08-21", "--raw-dir", str(tmp_path),
        "--dates", "2026-08-21",
    ]) == 0
    assert seen["dates"] == ["2026-08-21"]


def test_valve_filter_out_of_contract(raw_flattened_v13):
    with pytest.raises(ValueError):
        load_cycles(raw_flattened_v13.parent.parent, valve=36)


def test_missing_raw_dir_clear_error(tmp_path):
    with pytest.raises(NoRawDataError):
        load_cycles(tmp_path / "non-esiste")


def test_empty_raw_dir_clear_error(tmp_path):
    (tmp_path / "vuota").mkdir()
    with pytest.raises(NoRawDataError):
        load_cycles(tmp_path / "vuota")


def test_unknown_layout_clear_error(tmp_path):
    path = tmp_path / "machine=filler01" / "date=2026-08-01"
    path.mkdir(parents=True)
    pl.DataFrame({"colonna_misteriosa": [1, 2]}).write_parquet(
        path / "valve_cycles.parquet")
    with pytest.raises(RawFormatError):
        load_cycles(tmp_path)


def test_duplicate_cycle_ids_are_an_error(tmp_path):
    path = tmp_path / "machine=filler01" / "date=2026-08-01"
    path.mkdir(parents=True)
    rows = _flat_rows(_V13_FIELDS, n_valves=1, n_cycles=2)
    rows.append(dict(rows[0]))  # duplicato (valve_id=1, cycle_id=1)
    pl.DataFrame(rows).write_parquet(path / "valve_cycles.parquet")
    with pytest.raises(RawFormatError, match="duplicate"):
        load_cycles(tmp_path)


def test_duplicate_key_with_different_measures_is_an_error(tmp_path):
    """Stessa (valve_id, cycle_id), MISURE diverse: il caso di due run.

    E' il caso che conta e che sfuggiva: il guard usava
    `cycles.is_duplicated()` sull'intero frame, che marca duplicata solo la
    riga identica su tutte le colonne. Due run del simulatore rinumerano
    cycle_id da 1 con misure diverse, quindi passavano il guard e finivano al
    DB, dove `ON CONFLICT DO NOTHING` li scartava in silenzio: `rows_inserted`
    piu' basso e nessuna spiegazione. Deve essere un errore, non una perdita.
    """
    path = tmp_path / "machine=filler01" / "date=2026-08-01"
    path.mkdir(parents=True)
    rows = _flat_rows(_V13_FIELDS, n_valves=1, n_cycles=2)
    collisione = dict(rows[0])
    collisione["data.filling_time_ms"] = 2400   # stessa chiave, altra misura
    rows.append(collisione)
    pl.DataFrame(rows).write_parquet(path / "valve_cycles.parquet")
    with pytest.raises(RawFormatError, match="duplicate"):
        load_cycles(tmp_path)


# ---------------------------------------------------------------------------
# Integrazione differita (SKIPPED finché cycles_storage non è installato
# e/o non c'è un PostgreSQL raggiungibile): smoke idempotente su DB privato.
# ---------------------------------------------------------------------------
def _pg_available() -> bool:
    try:
        from pipeline.storage import make_engine
        from sqlalchemy import text
        url = os.environ.get("PLCSIM_DATABASE_URL",
                             "postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim")
        with make_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL non raggiungibile (avvia `docker compose up -d postgres`)")

_TEST_DB_URL = os.environ.get(
    "PLCSIM_TEST_DATABASE_URL",
    "postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim_test")


@requires_postgres
def test_backfill_smoke_idempotent(raw_flattened_v13):
    """Smoke con CyclesStorage (contratto: bulk_insert -> int, ON CONFLICT
    su (run_id, valve_id, cycle_id)). SKIPPED finché pipeline/cycles_storage.py non
    è installato: il mapping è già coperto dai test puri sopra."""
    cs_mod = pytest.importorskip("pipeline.cycles_storage")
    from pipeline.storage import make_engine
    from sqlalchemy import text

    engine = make_engine(_TEST_DB_URL)
    store = cs_mod.CyclesStorage(engine)
    store.metadata.drop_all(engine)  # tabella `cycles` pulita (niente drop_all)
    store.init()

    raw_root = raw_flattened_v13.parent.parent
    res1 = backfill(raw_root, run_id="run_test", db_url=_TEST_DB_URL)
    assert res1.rows_read == 6
    assert res1.rows_inserted == 6

    # re-run: idempotente (stesso count, nessun duplicato)
    res2 = backfill(raw_root, run_id="run_test", db_url=_TEST_DB_URL)
    assert res2.rows_read == 6
    assert res2.rows_inserted == 0

    with engine.connect() as conn:
        total = conn.execute(text("SELECT count(*) FROM cycles")).scalar()
        dupes = conn.execute(text(
            "SELECT count(*) FROM (SELECT run_id, valve_id, cycle_id FROM cycles "
            "GROUP BY run_id, valve_id, cycle_id HAVING count(*) > 1) t")).scalar()
    assert total == 6
    assert dupes == 0

    # verifica che event_ts sia stato persistito correttamente
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT event_ts FROM cycles")).fetchall()
        assert all(r[0] is not None for r in rows)
        # source_ts/ingest_ts pure popolati nel smoke v13
        src_rows = conn.execute(text("SELECT source_ts, ingest_ts FROM cycles")).fetchall()
        assert all(sr[0] is not None and sr[1] is not None for sr in src_rows)

    # pulizia del DB di test (non inquina lo storico reale)
    store.metadata.drop_all(engine)


@requires_postgres
def test_backfill_bulk_with_machine_id_override(run_bulk):
    """Run bulk (machine_id assente nel raw) + `--machine-id` esplicito:
    override applicato, insert ok, re-run idempotente. SKIPPED finché
    cycles_storage non è installato."""
    cs_mod = pytest.importorskip("pipeline.cycles_storage")
    from pipeline.storage import make_engine
    from sqlalchemy import text

    engine = make_engine(_TEST_DB_URL)
    store = cs_mod.CyclesStorage(engine)
    store.metadata.drop_all(engine)  # tabella `cycles` pulita (niente drop_all)
    store.init()

    res1 = backfill(run_bulk.parent, run_id="run_test", db_url=_TEST_DB_URL, machine_id="filler01")
    assert res1.rows_read == 6
    assert res1.rows_inserted == 6
    res2 = backfill(run_bulk.parent, run_id="run_test", db_url=_TEST_DB_URL, machine_id="filler01")
    assert res2.rows_inserted == 0

    with engine.connect() as conn:
        total = conn.execute(text("SELECT count(*) FROM cycles")).scalar()
        machines = conn.execute(
            text("SELECT DISTINCT machine_id FROM cycles")).fetchall()
        # bulk: event_ts popolato da ts_beg, source/ingest null
        ev = conn.execute(text("SELECT count(*) FROM cycles WHERE event_ts IS NOT NULL")).scalar()
        src_null = conn.execute(text("SELECT count(*) FROM cycles WHERE source_ts IS NULL")).scalar()
        ing_null = conn.execute(text("SELECT count(*) FROM cycles WHERE ingest_ts IS NULL")).scalar()
    assert total == 6
    assert [r[0] for r in machines] == ["filler01"]
    assert ev == 6
    assert src_null == 6
    assert ing_null == 6

    store.metadata.drop_all(engine)


@requires_postgres
def test_backfill_flattened_null_ts_persisted(raw_flattened_no_ts):
    """Flattened senza timestamps → NULL persistiti (onestà)."""
    cs_mod = pytest.importorskip("pipeline.cycles_storage")
    from pipeline.storage import make_engine
    from sqlalchemy import text

    engine = make_engine(_TEST_DB_URL)
    store = cs_mod.CyclesStorage(engine)
    store.metadata.drop_all(engine)
    store.init()

    res1 = backfill(raw_flattened_no_ts.parent.parent, run_id="run_test", db_url=_TEST_DB_URL)
    assert res1.rows_read == 6
    assert res1.rows_inserted == 6

    with engine.connect() as conn:
        null_ev = conn.execute(text("SELECT count(*) FROM cycles WHERE event_ts IS NULL")).scalar()
        null_src = conn.execute(text("SELECT count(*) FROM cycles WHERE source_ts IS NULL")).scalar()
        null_ing = conn.execute(text("SELECT count(*) FROM cycles WHERE ingest_ts IS NULL")).scalar()
    assert null_ev == 6
    assert null_src == 6
    assert null_ing == 6

    store.metadata.drop_all(engine)


# ===========================================================================
# run_id obbligatorio + carico a blocchi (aggiunta 2026-08-19)
# ===========================================================================
def test_run_id_mancante_errore_prima_del_db(raw_flattened_v13):
    """--run-id assente/vuoto → errore chiaro PRIMA di toccare il DB.

    `db_url` punta a un host inesistente: se il codice arrivasse al DB il
    test fallirebbe con un errore di connessione, non con BackfillError.
    """
    root = raw_flattened_v13.parent.parent
    for bad in (None, "", "   "):
        with pytest.raises(BackfillError, match="run_id"):
            backfill(root, run_id=bad,
                     db_url="postgresql+psycopg://x:x@127.0.0.1:1/none")


def test_cli_run_id_obbligatorio():
    """La CLI rifiuta l'invocazione senza --run-id (argparse: exit 2)."""
    from pipeline.cycles_backfill import build_arg_parser, main
    with pytest.raises(SystemExit) as e:
        build_arg_parser().parse_args(["--raw-dir", "data/raw"])
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        main(["--raw-dir", "data/raw"])


def test_run_id_valorizzato_su_tutte_le_righe(raw_flattened_v13, monkeypatch):
    """`run_id` è la prima colonna e viene riempito col valore esplicito."""
    visti = []

    class _FakeStore:
        def init(self):
            pass

        def bulk_insert(self, records):
            visti.extend(records)
            return len(records)

    import pipeline.cycles_storage as cs_mod
    monkeypatch.setattr(cs_mod, "CyclesStorage", lambda engine: _FakeStore())
    monkeypatch.setattr("pipeline.storage.make_engine", lambda url: None)
    res = backfill(raw_flattened_v13.parent.parent, run_id="run_x")
    assert res.rows_inserted == 6
    assert {r["run_id"] for r in visti} == {"run_x"}
    assert list(visti[0].keys())[0] == "run_id"


def test_cursore_non_perde_i_cicli_arrivati_fuori_ordine(tmp_path, monkeypatch):
    """REGRESSIONE 2026-08-22: un cycle_id alto arrivato per primo non deve
    zittire tutti quelli veri che arrivano dopo.

    Il difetto, misurato su una corsa live: alla ripartenza di Node-RED la
    prima lettura della subscription consegna il valore CORRENTE di
    `LastCycleId`. Per la valvola 1 è arrivato un `cycle_id` 274 prima della
    sequenza reale, ripartita da 4. Il cursore, che allora era un high-water
    mark per valvola sul `cycle_id`, ha preso 274 come soglia e ha scartato
    ogni ciclo autentico successivo — in silenzio, con il log che diceva
    «backfill ok … 0 righe inserite».

    Il cursore ora è sull'`ingest_ts`, cioè sull'ordine di ARRIVO, che è
    monotono per costruzione: una riga che arriva tardi ha comunque un
    `ingest_ts` più grande e passa il filtro, qualunque sia il suo cycle_id.
    """
    part = tmp_path / "machine=filler01" / "date=2026-08-21"
    part.mkdir(parents=True, exist_ok=True)
    raw_path = part / "valve_cycles.parquet"

    def righe(cicli, ingest):
        out = []
        for r in _flat_rows(_V13_FIELDS, n_valves=1, n_cycles=1):
            for cid in cicli:
                riga = dict(r)
                riga["cycle_id"] = cid
                riga["ingest_ts"] = ingest
                out.append(riga)
        return out

    state, inserted = {}, []

    class _FakeState:
        def get_machine_state(self, key):
            return state.get(key)

        def set_machine_state(self, key, value):
            state[key] = value

    class _FakeStore:
        def init(self):
            pass

        def bulk_insert(self, records):
            inserted.extend(records)
            return len(records)

    import pipeline.cycles_storage as cs_mod
    monkeypatch.setattr(cs_mod, "CyclesStorage", lambda engine: _FakeStore())
    monkeypatch.setattr("pipeline.storage.make_engine", lambda url: object())
    monkeypatch.setattr("pipeline.storage.Storage", lambda engine: _FakeState())

    # 1) arriva per primo il valore corrente, fuori sequenza
    pl.DataFrame(righe([274], "2026-08-21T10:00:00Z")).write_parquet(raw_path)
    primo = backfill(tmp_path, run_id="run-live", dates=["2026-08-21"])
    assert primo.rows_inserted == 1

    # 2) poi la sequenza vera, con cycle_id MOLTO più bassi ma arrivati DOPO
    pl.DataFrame(
        righe([274], "2026-08-21T10:00:00Z")
        + righe([4, 5, 6, 7, 8], "2026-08-21T10:00:05Z")
    ).write_parquet(raw_path)
    secondo = backfill(tmp_path, run_id="run-live", dates=["2026-08-21"])

    # 6 e non 5: la riga sull'istante esatto del cursore viene riproposta,
    # perché il confronto è `>=` (vedi `_filter_with_cursor`). La ridondanza
    # la assorbe l'ON CONFLICT del writer vero; qui il fake store non
    # deduplica, quindi il 274 si ripresenta.
    assert secondo.rows_inserted == 6
    assert {4, 5, 6, 7, 8} <= {r["cycle_id"] for r in inserted}, (
        "i cicli 4-8, arrivati dopo il 274, devono essere scritti: con il "
        "vecchio cursore sul cycle_id venivano scartati in silenzio")


def test_backfill_cursor_restart_is_incremental_and_idempotent(tmp_path, monkeypatch):
    """Il cursore per (run, data) evita di rileggere il pregresso tra restart."""
    from pipeline import cycles_backfill as module

    raw_path = _write_flat_raw(tmp_path, _V13_FIELDS, date="2026-08-21",
                               cid_start=1)
    state = {}
    inserted = []

    class _FakeState:
        def get_machine_state(self, key):
            return state.get(key)

        def set_machine_state(self, key, value):
            state[key] = value

    class _FakeStore:
        def init(self):
            pass

        def bulk_insert(self, records):
            inserted.extend(records)
            return len(records)

    store = _FakeStore()
    import pipeline.cycles_storage as cs_mod
    monkeypatch.setattr(cs_mod, "CyclesStorage", lambda engine: store)
    monkeypatch.setattr("pipeline.storage.make_engine", lambda url: object())
    monkeypatch.setattr("pipeline.storage.Storage", lambda engine: _FakeState())

    first = backfill(tmp_path, run_id="run-live", dates=["2026-08-21"])
    assert first.rows_inserted == 6
    assert len(state) == 1
    cursor = next(iter(state.values()))
    assert cursor["max_ingest_ts"].startswith("2026-08-01T10:00:01")

    # La partizione cresce con righe arrivate DOPO (ingest_ts maggiore).
    vecchie = _flat_rows(_V13_FIELDS, cid_start=1)
    medie = _flat_rows(_V13_FIELDS, cid_start=4,
                       ingest_ts_value="2026-08-01T10:05:00Z")
    pl.DataFrame(vecchie + medie).write_parquet(raw_path)
    second = backfill(tmp_path, run_id="run-live", dates=["2026-08-21"])
    assert second.rows_read == 12
    cursor = next(iter(state.values()))
    assert cursor["max_ingest_ts"].startswith("2026-08-01T10:05:00")

    # Terzo giro con un lotto ancora più recente: il cursore taglia il
    # pregresso. Delle 18 righe lette ne passano 12 — il lotto sul bordo
    # (10:05, riproposto perché il confronto è `>=`) e quello nuovo. Le sei
    # righe delle 10:00:01 NON tornano al writer: è il pregresso che non si
    # rilegge.
    recenti = _flat_rows(_V13_FIELDS, cid_start=7,
                         ingest_ts_value="2026-08-01T10:09:00Z")
    pl.DataFrame(vecchie + medie + recenti).write_parquet(raw_path)
    prima_del_terzo = len(inserted)
    third = backfill(tmp_path, run_id="run-live", dates=["2026-08-21"])

    assert third.rows_read == 18
    assert third.rows_inserted == 12, \
        "il pregresso non si rilegge: le righe più vecchie del cursore restano fuori"
    scritte = {row["cycle_id"] for row in inserted[prima_del_terzo:]}
    assert scritte == {4, 5, 6, 7, 8, 9}
    assert 1 not in scritte and 2 not in scritte and 3 not in scritte


def test_iter_record_chunks_non_materializza_tutto(raw_flattened_v13):
    """Streaming a blocchi: stessi record di to_dicts(), a pezzi."""
    from pipeline.cycles_backfill import _iter_record_chunks
    cycles = load_cycles(raw_flattened_v13.parent.parent)
    chunks = list(_iter_record_chunks(cycles, chunk_rows=4))
    assert [len(c) for c in chunks] == [4, 2]
    assert [r for c in chunks for r in c] == cycles.to_dicts()
    # frame vuoto → nessun chunk
    assert list(_iter_record_chunks(cycles.head(0), 4)) == []


@requires_postgres
def test_due_run_convivono_end_to_end(raw_flattened_v13):
    """Stessa fonte caricata con due --run-id diversi: 12 righe, non 6.

    Prima della colonna `run_id` il secondo caricamento spariva dentro
    ON CONFLICT DO NOTHING senza dirlo.
    """
    cs_mod = pytest.importorskip("pipeline.cycles_storage")
    from pipeline.storage import make_engine
    from sqlalchemy import text

    engine = make_engine(_TEST_DB_URL)
    store = cs_mod.CyclesStorage(engine)
    store.metadata.drop_all(engine)
    store.init()
    root = raw_flattened_v13.parent.parent
    try:
        assert backfill(root, run_id="run_a", db_url=_TEST_DB_URL).rows_inserted == 6
        assert backfill(root, run_id="run_b", db_url=_TEST_DB_URL).rows_inserted == 6
        # re-run di run_a: idempotente
        assert backfill(root, run_id="run_a", db_url=_TEST_DB_URL).rows_inserted == 0
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM cycles")).scalar() == 12
            runs = [r[0] for r in conn.execute(text(
                "SELECT DISTINCT run_id FROM cycles ORDER BY run_id"))]
        assert runs == ["run_a", "run_b"]
    finally:
        store.metadata.drop_all(engine)
