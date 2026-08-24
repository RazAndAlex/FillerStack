"""Test unit — cycles storage (pipeline/cycles_storage.py) su PostgreSQL live.

DB privato dedicato `plcsim_test_cycles`: NON tocca `plcsim`/`plcsim_test`
usati dagli altri test (test_storage/test_inference/test_api). Se il database
non esiste (il container compose parte con solo plcsim/plcsim_test/postgres),
il fixture lo crea via il DB di manutenzione `postgres` — bootstrap
autosufficiente, nessun setup manuale. Setup di ogni test: `drop_all` + `init`
→ suite idempotente e indipendente dallo stato precedente.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError

from pipeline.cycles_storage import (
    CURRENT_RUN_ID_KEY,
    CYCLES_COLUMNS,
    LEGACY_RUN_ID,
    AmbiguousRunError,
    CyclesStorage,
)
from pipeline.storage import make_engine

TEST_DB_URL = os.environ.get(
    "PLCSIM_CYCLES_TEST_DATABASE_URL",
    "postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim_test_cycles",
)


def _ensure_test_database() -> None:
    """Crea `plcsim_test_cycles` se assente (bootstrap idempotente)."""
    url = make_url(TEST_DB_URL)
    admin = make_engine(url.set(database="postgres")
                        .render_as_string(hide_password=False))
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database}).first()
        if exists is None:
            # CREATE DATABASE non può girare dentro una transazione: connessione
            # AUTOCOMMIT (altrimenti: ActiveSqlTransaction).
            with admin.connect().execution_options(
                    isolation_level="AUTOCOMMIT") as conn:
                try:
                    conn.execute(text(f'CREATE DATABASE "{url.database}"'))
                except ProgrammingError:
                    pass  # corsa benigna: un altro processo l'ha appena creata
    finally:
        admin.dispose()


@pytest.fixture()
def cs():
    _ensure_test_database()
    st = CyclesStorage(url=TEST_DB_URL)
    st.drop_all()  # setup: DB privato pulito (idempotenza)
    st.init()
    yield st
    st.drop_all()  # teardown: lascia pulito


def _rec(valve_id, cycle_id, run_id="runA", **over):
    d = {
        "run_id": run_id,
        "machine_id": "filler01",
        "cycle_id": cycle_id,
        "valve_id": valve_id,
        "filling_time_ms": 2480 + cycle_id,
        "tail_time_ms": 421,
        "tail_pulse": 244,
        "pulse_count": 3,
        "target": 2500,
        "delta_pulse": 244,
        "filling_step_out": 18,
        "filling_ok": True,
        "fill_quality_ok": True,
        "sequence_ok": True,
        "sample_valid": True,
        "diagnostic_status": "NORMAL",
        "close_reason": None,
        "position_limit": False,
        "filling_overtime": False,
        "event_ts": None,
        "source_ts": None,
        "ingest_ts": None,
    }
    d.update(over)
    return d


# -- contratto colonne ------------------------------------------------------

def test_columns_contract():
    assert CYCLES_COLUMNS == (
        "run_id",
        "machine_id", "cycle_id", "valve_id",
        "filling_time_ms", "tail_time_ms", "tail_pulse", "pulse_count",
        "target", "delta_pulse", "filling_step_out",
        "filling_ok", "fill_quality_ok", "sequence_ok", "sample_valid",
        "diagnostic_status", "close_reason", "position_limit",
        "filling_overtime",
        "event_ts", "source_ts", "ingest_ts",
    )
    assert len(CYCLES_COLUMNS) == 22


def test_standalone_metadata(cs):
    # la tabella cycles vive SOLO qui: storage.build_metadata() (M10) non la
    # conosce — ciclo di vita indipendente (spec dashboard §7).
    from pipeline.storage import build_metadata
    assert cs.metadata.tables.keys() == {"cycles"}
    assert "cycles" not in build_metadata().tables


def test_init_idempotent(cs):
    cs.init()  # seconda chiamata: checkfirst → nessun errore
    cs.init()


def test_columns_order_stable_after_filling_overtime():
    # le 3 timestamp sono in coda dopo filling_overtime, ordine stabile
    idx = {name: i for i, name in enumerate(CYCLES_COLUMNS)}
    assert idx["run_id"] == 0          # discriminante di run in testa
    assert idx["filling_overtime"] == 18
    assert idx["event_ts"] == 19
    assert idx["source_ts"] == 20
    assert idx["ingest_ts"] == 21


def test_metadata_has_timestamp_columns_nullable(cs):
    from sqlalchemy import inspect as sa_inspect
    # verifica via inspector che le 3 colonne siano timestamptz nullable
    insp = sa_inspect(cs.engine)
    cols = {c["name"]: c for c in insp.get_columns("cycles")}
    for name in ("event_ts", "source_ts", "ingest_ts"):
        assert name in cols, f"{name} mancante in cycles"
        assert cols[name]["nullable"] is True, f"{name} deve essere nullable"
    # build_cycles_metadata deve esporre DateTime(timezone=True)
    from pipeline.cycles_storage import build_cycles_metadata
    m = build_cycles_metadata()
    tbl = m.tables["cycles"]
    for name in ("event_ts", "source_ts", "ingest_ts"):
        col = tbl.c[name]
        assert col.nullable is True
        # type is DateTime with timezone
        assert isinstance(col.type, type(tbl.c.cycle_id.type)) is False  # sanity different from Integer
        # check timezone flag
        assert getattr(col.type, "timezone", False) is True


# -- bulk_insert ------------------------------------------------------------

def test_bulk_insert_idempotent(cs):
    records = [_rec(1, i) for i in range(1, 6)]
    assert cs.bulk_insert(records) == 5
    # replay dello stesso batch: ON CONFLICT (valve_id, cycle_id) DO NOTHING
    assert cs.bulk_insert(records) == 0
    # mescolanza nuove + duplicate → solo le nuove contano
    assert cs.bulk_insert([_rec(1, 3), _rec(1, 6), _rec(2, 1)]) == 2
    assert cs.bulk_insert([]) == 0


def test_bulk_insert_required_fields(cs):
    bad = _rec(5, 1)
    del bad["machine_id"]  # NOT NULL → IntegrityError (la tabella resta pulita)
    with pytest.raises(IntegrityError):
        cs.bulk_insert([bad])


def test_partial_cycle_nulls(cs):
    # cicli parziali (policy T6): colonne dati nullable
    rec = _rec(4, 1, filling_time_ms=None, diagnostic_status=None,
               filling_ok=None)
    assert cs.bulk_insert([rec]) == 1
    row = cs.kpi_series(4)[0]
    assert row["filling_time_ms"] is None
    assert row["diagnostic_status"] is None
    assert row["filling_ok"] is None


# -- event_ts population + null fallback (OEE windowabile) -------------------

def test_bulk_insert_with_event_ts_populated(cs):
    ts = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
    ts2 = datetime(2026, 8, 1, 11, 30, 0, tzinfo=timezone.utc)
    rec1 = _rec(10, 1, event_ts=ts, source_ts=ts, ingest_ts=ts2)
    rec2 = _rec(10, 2, event_ts=ts2, source_ts=None, ingest_ts=ts2)
    assert cs.bulk_insert([rec1, rec2]) == 2
    series = cs.kpi_series(10)
    # ordinato DESC per cycle_id: rec2 prima
    assert series[0]["cycle_id"] == 2
    assert series[0]["event_ts"] == ts2
    assert series[0]["source_ts"] is None
    assert series[0]["ingest_ts"] == ts2
    assert series[1]["cycle_id"] == 1
    assert series[1]["event_ts"] == ts
    assert series[1]["source_ts"] == ts
    assert series[1]["ingest_ts"] == ts2


def test_event_ts_null_fallback_when_source_has_no_timestamp(cs):
    # se sorgente non ha il campo → NULL (mai fabbricare), backfill onesto
    rec = _rec(11, 1)  # default event_ts=None
    assert cs.bulk_insert([rec]) == 1
    row = cs.kpi_series(11)[0]
    assert row["event_ts"] is None
    assert row["source_ts"] is None
    assert row["ingest_ts"] is None


def test_migration_idempotent_existing_db(cs):
    # init già chiamato dal fixture; seconda init deve preservare colonne + dati
    ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    cs.bulk_insert([_rec(12, 1, event_ts=ts)])
    cs.init()
    cs.init()
    row = cs.kpi_series(12)[0]
    assert row["event_ts"] == ts
    # verifica che la tabella abbia ancora 21 colonne logiche
    with cs.engine.connect() as conn:
        r = conn.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_name='cycles'")).scalar()
        # almeno 21 (potrebbe avere extra se migrazione parziale, ma mai meno)
        assert r >= 22


# -- kpi_series -------------------------------------------------------------

def test_kpi_series_ordering_and_shape(cs):
    cs.bulk_insert([_rec(1, i) for i in range(1, 11)])  # ciclo 1..10
    series = cs.kpi_series(1)
    assert [r["cycle_id"] for r in series] == list(range(10, 0, -1))  # DESC
    assert len(series) == 10
    row = series[0]
    assert set(row.keys()) == set(CYCLES_COLUMNS)
    assert len(row) == 22
    assert row["machine_id"] == "filler01"
    assert row["filling_ok"] is True
    # nuove colonne esposte via SELECT *
    assert "event_ts" in row and "source_ts" in row and "ingest_ts" in row
    # limit
    assert [r["cycle_id"] for r in cs.kpi_series(1, limit=3)] == [10, 9, 8]
    # valvola senza cicli → []
    assert cs.kpi_series(99) == []


def test_kpi_series_limit_validation(cs):
    with pytest.raises(ValueError):
        cs.kpi_series(1, limit=0)


# -- latest_kpi_by_valve ----------------------------------------------------

def test_latest_kpi_by_valve(cs):
    cs.bulk_insert([
        _rec(1, 5), _rec(1, 7),
        _rec(2, 3), _rec(2, 8),
        _rec(3, 9),
    ])
    latest = cs.latest_kpi_by_valve()
    assert sorted(latest) == [1, 2, 3]
    assert latest[1]["cycle_id"] == 7
    assert latest[2]["cycle_id"] == 8
    assert latest[3]["cycle_id"] == 9
    assert set(latest[1].keys()) == set(CYCLES_COLUMNS)
    assert len(latest[1]) == 22


def test_latest_kpi_by_valve_empty(cs):
    assert cs.latest_kpi_by_valve() == {}


# === formato dei timestamp servito dall'API (aggiunta 2026-08-19) ==========

def test_api_serve_i_timestamp_con_offset_esplicito(cs):
    """Un solo formato di timestamp nella stessa risposta.

    `kpi_series` / `latest_kpi_by_valve` ritornano oggetti `datetime`, che
    FastAPI serializzava con il suffisso `Z`, mentre ogni altra route passa da
    `api._iso()` e produce `+00:00`: nella STESSA risposta `/valves`
    convivevano `prediction_ts` con `+00:00` e `event_ts` con `Z` — stesso
    istante, due grafie (CONFRONTO-API-FIXTURE.md sez. 2.4). Lo storage
    continua a restituire `datetime`; la conversione avviene al confine di
    presentazione, e questo test la verifica dall'esterno.
    """
    import os

    from fastapi.testclient import TestClient

    from pipeline import api
    from pipeline.storage import Storage

    ts = datetime(2026, 6, 1, 23, 41, 55, 210000, tzinfo=timezone.utc)
    cs.bulk_insert([_rec(13, 1, event_ts=ts, ingest_ts=ts)])
    prev = os.environ.get("PLCSIM_DATABASE_URL")
    os.environ["PLCSIM_DATABASE_URL"] = TEST_DB_URL
    api._store = None
    st = Storage(make_engine(TEST_DB_URL))
    st.init()                       # tabelle M10 per /valves, sullo stesso DB
    try:
        c = TestClient(api.app)
        r = c.get("/valves/13/kpi")
        assert r.status_code == 200, r.text
        riga = r.json()["series"][0]
        assert riga["event_ts"] == "2026-06-01T23:41:55.210000+00:00"
        assert riga["ingest_ts"].endswith("+00:00")
        assert riga["source_ts"] is None          # NULL resta NULL
        # stessa grafia nel catalogo, accanto a prediction_ts
        last = c.get("/valves").json()["valves"]["13"]["last_kpi"]
        assert last["event_ts"] == "2026-06-01T23:41:55.210000+00:00"
        assert not any(str(v).endswith("Z") for v in last.values()
                       if isinstance(v, str))
    finally:
        st.metadata.drop_all(st.engine, checkfirst=True)
        st.engine.dispose()
        api._store = None
        if prev is None:
            os.environ.pop("PLCSIM_DATABASE_URL", None)
        else:
            os.environ["PLCSIM_DATABASE_URL"] = prev


# === run_id: discriminante di run (aggiunta 2026-08-19) ====================

def test_due_run_con_cycle_id_sovrapposti_convivono(cs):
    """Il caso che prima veniva scartato in silenzio.

    Due run con ESATTAMENTE gli stessi (valve_id, cycle_id): con la vecchia
    PK (valve_id, cycle_id) il secondo spariva dentro ON CONFLICT DO NOTHING
    e `rows_inserted` scendeva senza spiegazione. Ora tutte le righe di
    entrambi entrano.
    """
    a = [_rec(1, i, run_id="run_a") for i in range(1, 6)]
    b = [_rec(1, i, run_id="run_b") for i in range(1, 6)]
    assert cs.bulk_insert(a) == 5
    assert cs.bulk_insert(b) == 5          # nessuna collisione fra run
    with cs.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM cycles")).scalar() == 10
    # idempotenza ancora per-run
    assert cs.bulk_insert(b) == 0
    assert sorted(cs.known_run_ids()) == ["run_a", "run_b"]
    # la serie di un run non contiene righe dell'altro
    serie = cs.kpi_series(1, run_id="run_b")
    assert len(serie) == 5
    assert {r["run_id"] for r in serie} == {"run_b"}


def test_latest_kpi_by_valve_non_prende_il_run_piu_lungo(cs):
    """Run A lungo, run B corto e più recente → deve tornare B.

    Senza filtro di run, `DISTINCT ON (valve_id) ORDER BY cycle_id DESC`
    restituirebbe il ciclo del run PIÙ LUNGO (A, cycle_id 50) invece di
    quello del run corrente (B, cycle_id 3): una macchina sana mostrata al
    posto di quella guasta.
    """
    cs.bulk_insert([_rec(1, i, run_id="run_lungo") for i in range(1, 51)])
    cs.bulk_insert([_rec(1, i, run_id="run_corto") for i in range(1, 4)])
    latest = cs.latest_kpi_by_valve(run_id="run_corto")
    assert latest[1]["cycle_id"] == 3
    assert latest[1]["run_id"] == "run_corto"
    # e senza indicazione alcuna: errore esplicito, MAI il run più lungo
    with pytest.raises(AmbiguousRunError):
        cs.latest_kpi_by_valve()
    with pytest.raises(AmbiguousRunError):
        cs.kpi_series(1)


def test_run_id_dal_kv_machine_state(cs):
    """La chiave KV `current_run_id` risolve l'ambiguità (stesso meccanismo
    di speed_target/baseline_window)."""
    from pipeline.storage import Storage
    cs.bulk_insert([_rec(1, i, run_id="run_lungo") for i in range(1, 51)])
    cs.bulk_insert([_rec(1, i, run_id="run_corto") for i in range(1, 4)])
    st = Storage(cs.engine)
    st.init()
    try:
        st.set_machine_state(CURRENT_RUN_ID_KEY, "run_corto")
        assert cs.current_run_id() == "run_corto"
        assert cs.latest_kpi_by_valve()[1]["cycle_id"] == 3   # niente errore
        # l'argomento esplicito ha comunque la precedenza sul KV
        assert cs.latest_kpi_by_valve(run_id="run_lungo")[1]["cycle_id"] == 50
    finally:
        st.metadata.drop_all(st.engine, checkfirst=True)


def test_un_solo_run_non_richiede_indicazioni(cs):
    cs.bulk_insert([_rec(2, i, run_id="solo") for i in range(1, 4)])
    assert cs.latest_kpi_by_valve()[2]["cycle_id"] == 3
    assert len(cs.kpi_series(2)) == 3


def test_indici_creati(cs):
    with cs.engine.connect() as conn:
        idx = {r[0] for r in conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename='cycles'"))}
    assert "ix_cycles_run_event_ts" in idx
    assert "ix_cycles_run_valve_cycle_desc" in idx


def test_migrazione_run_id_su_db_preesistente(cs):
    """DB pre-esistente (schema vecchio, righe senza run_id) → init() migra
    in modo idempotente e NON perde righe."""
    cs.drop_all()
    # ricostruisce lo schema PRE-run_id (21 colonne, PK valve_id+cycle_id)
    with cs.engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE cycles (
                machine_id VARCHAR NOT NULL,
                cycle_id INTEGER NOT NULL,
                valve_id INTEGER NOT NULL,
                filling_time_ms INTEGER, tail_time_ms INTEGER,
                tail_pulse INTEGER, pulse_count INTEGER, target INTEGER,
                delta_pulse INTEGER, filling_step_out INTEGER,
                filling_ok BOOLEAN, fill_quality_ok BOOLEAN,
                sequence_ok BOOLEAN, sample_valid BOOLEAN,
                position_limit BOOLEAN, filling_overtime BOOLEAN,
                diagnostic_status VARCHAR, close_reason VARCHAR,
                event_ts TIMESTAMPTZ, source_ts TIMESTAMPTZ,
                ingest_ts TIMESTAMPTZ,
                CONSTRAINT pk_cycles_valve_cycle PRIMARY KEY (valve_id, cycle_id)
            )"""))
        conn.execute(text(
            "INSERT INTO cycles (machine_id, cycle_id, valve_id, target) "
            "SELECT 'filler01', g, 1, 2500 FROM generate_series(1, 137) g"))

    cs.init()
    cs.init()   # idempotente: la seconda non fa nulla
    with cs.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM cycles")).scalar() == 137
        assert conn.execute(text(
            "SELECT count(*) FROM cycles WHERE run_id IS NULL")).scalar() == 0
        assert conn.execute(text(
            "SELECT DISTINCT run_id FROM cycles")).scalar() == LEGACY_RUN_ID
        pk = conn.execute(text(
            "SELECT conname FROM pg_constraint WHERE conrelid='cycles'::regclass "
            "AND contype='p'")).scalar()
        assert pk == "pk_cycles_run_valve_cycle"
        notnull = conn.execute(text(
            "SELECT attnotnull FROM pg_attribute WHERE attrelid='cycles'::regclass "
            "AND attname='run_id'")).scalar()
        assert notnull is True
    # dopo la migrazione un secondo run entra senza collidere
    assert cs.bulk_insert([_rec(1, 1, run_id="nuovo")]) == 1
