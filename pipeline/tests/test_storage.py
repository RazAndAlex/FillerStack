"""Test storage M10 (pipeline/storage.py) — schema + CRUD.

I test che richiedono un PostgreSQL live sono marcati `postgres` e vengono
skippati se il DB di test non è raggiungibile (o se il driver non è
installato). Il layer è PostgreSQL-specifico (JSONB/UUID/ON CONFLICT),
quindi non si testa contro SQLite: si testa la *struttura* dello schema
(introspection) sempre, e il CRUD solo con un server reale.

Contratto alert (fix wave 2026-08-13, review m10-standards B1/B2/B5):
- UN vincolo UNIQUE `uq_alerts_valve_fault` su (valve_id, fault_type): UNA
  riga di stato corrente per lineage (NON il vecchio a 3 colonne
  `uq_alerts_valve_fault_status`);
- alert_id di lineage deterministico (`storage.alert_id_for`), stabile su
  open→sustained→closed→reopen; la ri-apertura RINFRESCA opened_ts /
  opened_at_cycle_id;
- `alert_transitions.alert_id` è FK verso `alerts.alert_id` (spec M10 §2);
- upsert FULL-STATE (il chiamante passa lo stato intero) con vocabolario
  stati `ALERT_STATUSES = ("open", "sustained", "closed")`.

Avvio manuale del server per il test live:
    docker compose -f edge/docker-compose.yml up -d postgres
    PLCSIM_TEST_DATABASE_URL=postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim_test_fix \
        python -m pytest pipeline/tests/test_storage.py -v

Il DB di test ha nome unico per processo (vedi sotto) per isolare i test dai
worker paralleli che girano sullo stesso Postgres (residual risk F2/2.10).
"""
from __future__ import annotations

import os
import re
import secrets
import uuid

import pytest
from sqlalchemy import UniqueConstraint, select
from sqlalchemy.exc import IntegrityError

from pipeline.storage import ALERT_STATUSES, Storage, alert_id_for, build_metadata, make_engine, _to_dt


# ---------------------------------------------------------------------------
# DB di test: nome UNICO PER PROCESSO (isolamento dai worker paralleli)
# ---------------------------------------------------------------------------
# I worker del fix wave girano in parallelo sullo stesso albero e sullo stesso
# Postgres: un DB di test con nome fisso condiviso produce corse DDL
# (UndefinedTable/pg_type — residual risk F2/2.10 delle review). Ogni
# processo pytest si crea il proprio DB (`plcsim_test_fix_<random>`),
# creato al volo se manca (richiede CREATEDB, default plcsim:plcsim).
def _test_db_url() -> str:
    if "PLCSIM_TEST_DATABASE_URL" in os.environ:
        return os.environ["PLCSIM_TEST_DATABASE_URL"]
    url = (f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
           f"plcsim_test_fix_{secrets.token_hex(4)}")
    os.environ["PLCSIM_TEST_DATABASE_URL"] = url  # condivisa da tutto il processo
    return url


def _ensure_test_db(url: str) -> None:
    """Crea il DB di test se manca (idempotente, best-effort)."""
    m = re.match(r"postgresql\+psycopg://([^/]+)/([A-Za-z0-9_]+)$", url)
    if not m:
        return
    try:
        from sqlalchemy import create_engine, text
        admin = create_engine(
            f"postgresql+psycopg://{m.group(1)}/postgres",
            connect_args={"connect_timeout": 3}, future=True)
        with admin.connect().execution_options(
                isolation_level="AUTOCOMMIT") as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": m.group(2)}).first()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{m.group(2)}"'))
        admin.dispose()
    except Exception:
        pass  # best-effort: se non si può creare, il ping qui sotto decide (skip)


_TEST_DB_URL = _test_db_url()

# Run esplicito per le prove. Dal 2026-08-22 `predictions`, `alerts` e
# `alert_transitions` hanno un discriminante di run, e queste prove non
# hanno un KV `current_run_id` da cui risolverlo.
RUN_DI_PROVA = "run_di_prova"
_ensure_test_db(_TEST_DB_URL)


def _pg_available() -> bool:
    try:
        eng = make_engine(_TEST_DB_URL)
        from sqlalchemy import text
        # timeout breve: se il server non c'è, salta in fretta (non bloccare la suite)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL non raggiungibile (avvia `docker compose up -d postgres`)")


@pytest.fixture
def storage():
    s = Storage(make_engine(_TEST_DB_URL))
    # checkfirst=True: tollera i residui di DDL concorrente (residual risk
    # F2/2.10: drop_all può trovare tabelle già droppate → UndefinedTable).
    s.metadata.drop_all(s.engine, checkfirst=True)
    s.init()
    return s


def test_schema_table_names():
    m = build_metadata()
    assert set(m.tables.keys()) == {
        "predictions", "alerts", "alert_transitions", "machine_state",
        "machine_state_history",
    }


def test_schema_columns_predictions():
    m = build_metadata()
    cols = {c.name for c in m.tables["predictions"].columns}
    assert cols == {
        "prediction_id", "model_version", "feature_schema_version",
        "prediction_ts", "machine_id", "valve_id", "window_idx",
        "window_end_cycle_id", "predicted_label", "anomaly_score",
        "probabilities", "feature_fingerprint",
        # run_id (2026-08-22): discriminante di run, come su `cycles`. Non
        # e' un campo del contratto wire prediction-v1 — vive solo qui.
        "run_id",
    }


def test_schema_columns_alerts():
    m = build_metadata()
    cols = {c.name for c in m.tables["alerts"].columns}
    assert cols == {
        "alert_id", "valve_id", "fault_type", "status", "opened_ts",
        "last_seen_ts", "closed_ts", "max_score_seen", "n_cycles_above",
        "opened_at_cycle_id", "closed_at_cycle_id",
        # run_id (2026-08-22): un allarme appartiene al run che l'ha generato.
        "run_id",
    }


def test_schema_transitions_columns():
    m = build_metadata()
    cols = {c.name for c in m.tables["alert_transitions"].columns}
    assert {
        "transition_id", "alert_id", "transition_ts", "from_status",
        "to_status", "anomaly_score", "threshold_open", "threshold_close",
        "window_end_cycle_id", "valve_id", "fault_type",
    } <= cols


def test_schema_alerts_unique_per_run_valve_fault():
    """ADR-0021: UNA riga per (run_id, valve_id, fault_type).

    Il vincolo è `uq_alerts_run_valve_fault` — NON il vecchio a 3 colonne
    `uq_alerts_valve_fault_status` che accumulava una riga per stato (bug B1
    della review m10-standards), e NON più quello a 2 colonne
    `uq_alerts_valve_fault`, che condivideva la riga fra run diversi e
    lasciava che un run nuovo chiudesse gli allarmi del vecchio.
    """
    m = build_metadata()
    alerts = m.tables["alerts"]
    uniques = {c.name: tuple(col.name for col in c.columns)
               for c in alerts.constraints
               if isinstance(c, UniqueConstraint)}
    assert uniques["uq_alerts_run_valve_fault"] == (
        "run_id", "valve_id", "fault_type")
    assert "uq_alerts_valve_fault" not in uniques
    assert "uq_alerts_valve_fault_status" not in uniques


def test_schema_alert_transitions_fk_to_alerts():
    """Spec M10 §2: alert_transitions.alert_id è FK verso alerts.alert_id."""
    m = build_metadata()
    at = m.tables["alert_transitions"]
    fks = [fk for fk in at.foreign_keys if fk.parent.name == "alert_id"]
    assert len(fks) == 1
    assert fks[0].column.table.name == "alerts"
    assert fks[0].column.name == "alert_id"


def test_alert_statuses_vocabulary():
    assert ALERT_STATUSES == ("open", "sustained", "closed")


def test_schema_machine_state_history_columns():
    """OEE L0 (oee-backend-spec §B1): history append-only delle transizioni
    OMAC — id SERIAL, state_code/label, entered_ts NOT NULL, exited_ts,
    source."""
    m = build_metadata()
    cols = {c.name for c in m.tables["machine_state_history"].columns}
    assert cols == {"id", "state_code", "state_label", "entered_ts",
                    "exited_ts", "source"}
    t = m.tables["machine_state_history"]
    assert not t.c.state_code.nullable
    assert not t.c.state_label.nullable
    assert not t.c.entered_ts.nullable
    assert t.c.exited_ts.nullable
    assert t.c.source.nullable


def test_schema_machine_state_history_index_on_entered_ts():
    m = build_metadata()
    t = m.tables["machine_state_history"]
    idx = {i.name: tuple(c.name for c in i.columns) for i in t.indexes}
    assert idx.get("ix_machine_state_history_entered") == ("entered_ts",)


def test_alert_id_for_deterministic_lineage():
    a1 = alert_id_for(3, "restriction", RUN_DI_PROVA)
    a2 = alert_id_for(3, "restriction", RUN_DI_PROVA)
    b = alert_id_for(4, "restriction", RUN_DI_PROVA)
    c = alert_id_for(3, "flowmeter_dropout", RUN_DI_PROVA)
    d = alert_id_for(3, "restriction", "altro_run")
    assert a1 == a2            # determinismo: stessa terna → stesso id
    assert a1 != b             # valvola diversa → id diverso
    assert a1 != c             # fault diverso → id diverso
    assert a1 != d             # run diverso → id diverso: due macchine non
    #                            condividono la stessa riga di allarme
    with pytest.raises(ValueError):
        alert_id_for(3, "restriction", "   ")


def test_upsert_alert_rejects_unknown_status():
    """Vocabolario stati vincolato: status fuori ALERT_STATUSES → ValueError
    (nessuna riga scritta con uno stato che l'API non saprebbe filtrare)."""
    s = Storage(make_engine(_TEST_DB_URL))  # nessuna connessione qui
    with pytest.raises(ValueError):
        s.upsert_alert(alert_id="11111111-1111-1111-1111-111111111111",
                       valve_id=3, fault_type="restriction", status="bogus",
                       run_id=RUN_DI_PROVA)


@requires_postgres
def test_predictions_roundtrip(storage):
    s = storage
    rec = {
        "prediction_id": "9f8b1c2d-3e4f-5a6b-7c8d-9e0f1a2b3c4d",
        "model_version": "test-model",
        "feature_schema_version": "ML-F1",
        "prediction_ts": "2026-08-13T00:00:00Z",
        "machine_id": "filler01",
        "valve_id": 7,
        "window_idx": 1,
        "window_end_cycle_id": 50,
        "predicted_label": "restriction",
        "anomaly_score": 0.9,
        "probabilities": {"healthy": 0.1, "restriction": 0.9},
        "feature_fingerprint": "a" * 64,
    }
    assert s.insert_prediction(rec, "run_di_prova") is True
    assert s.insert_prediction(rec, "run_di_prova") is False  # idempotente
    assert 50 in s.existing_window_end_cycle_ids(7, "run_di_prova")
    # Il watermark è per run: un altro run non vede questa finestra, ed è
    # esattamente la proprietà che sblocca un run live che rinumera i
    # cycle_id da 1.
    assert 50 not in s.existing_window_end_cycle_ids(7, "altro_run")


@requires_postgres
def test_alert_upsert_and_transition(storage):
    """Upsert alert + insert transizione con read-back (transizione legata
    all'alert di lineage)."""
    s = storage
    aid = str(alert_id_for(3, "restriction", RUN_DI_PROVA))
    s.upsert_alert(alert_id=aid, valve_id=3, fault_type="restriction",
                   run_id=RUN_DI_PROVA, status="open", opened_ts="2026-08-13T00:00:00Z",
                   opened_at_cycle_id=50,
                   last_seen_ts="2026-08-13T00:00:00Z",
                   max_score_seen=0.9, n_cycles_above=1)
    s.insert_transition(
        transition_id="22222222-2222-2222-2222-222222222222",
        alert_id=aid,
        transition_ts=_to_dt("2026-08-13T00:00:00Z"),
        from_status="closed", to_status="open", anomaly_score=0.9,
        threshold_open=0.5, threshold_close=0.4, window_end_cycle_id=50,
        valve_id=3, fault_type="restriction", run_id=RUN_DI_PROVA,
    )
    with s.engine.connect() as conn:
        rows = conn.execute(select(s.alert_transitions)).fetchall()
    assert len(rows) == 1
    assert rows[0].to_status == "open"
    assert str(rows[0].alert_id) == aid


@requires_postgres
def test_alert_lifecycle_one_row_per_lineage(storage):
    """Ciclo open→sustained→closed→reopen: SEMPRE UNA riga per
    (valve_id, fault_type); la chiusura NON alza IntegrityError sulla PK
    (bug 2.1 m10-spec-correctness); la ri-apertura RINFRESCA opened_ts /
    opened_at_cycle_id (bug B2); l'alert_id di lineage non cambia mai."""
    s = storage
    aid = str(alert_id_for(3, "restriction", RUN_DI_PROVA))
    ts_open = "2026-08-13T00:00:00Z"
    ts_reopen = "2026-08-13T03:00:00Z"

    s.upsert_alert(alert_id=aid, valve_id=3, fault_type="restriction",
                   run_id=RUN_DI_PROVA, status="open", opened_ts=ts_open, opened_at_cycle_id=100,
                   last_seen_ts=ts_open, max_score_seen=0.9, n_cycles_above=1)
    s.upsert_alert(alert_id=aid, valve_id=3, fault_type="restriction",
                   run_id=RUN_DI_PROVA, status="sustained", opened_ts=ts_open, opened_at_cycle_id=100,
                   last_seen_ts="2026-08-13T00:01:00Z",
                   max_score_seen=0.95, n_cycles_above=2)
    # chiusura: stesso alert_id → la riga esiste già; deve AGGIORNARSI
    # (status closed), mai tentare un INSERT con la stessa PK
    s.upsert_alert(alert_id=aid, valve_id=3, fault_type="restriction",
                   run_id=RUN_DI_PROVA, status="closed", opened_ts=ts_open, opened_at_cycle_id=100,
                   closed_ts="2026-08-13T00:02:00Z", closed_at_cycle_id=300,
                   last_seen_ts="2026-08-13T00:02:00Z",
                   max_score_seen=0.95, n_cycles_above=2)
    # ri-apertura: opened_ts/opened_at_cycle_id del NUOVO episodio
    s.upsert_alert(alert_id=aid, valve_id=3, fault_type="restriction",
                   run_id=RUN_DI_PROVA, status="open", opened_ts=ts_reopen, opened_at_cycle_id=900,
                   last_seen_ts=ts_reopen, max_score_seen=0.8, n_cycles_above=1)

    with s.engine.connect() as conn:
        rows = conn.execute(select(s.alerts)).fetchall()
    assert len(rows) == 1, \
        "deve restare UNA sola riga per (valve_id, fault_type) su tutto il ciclo"
    row = rows[0]
    assert row.status == "open"
    assert str(row.alert_id) == aid          # lineage stabile
    assert row.opened_ts == _to_dt(ts_reopen)   # freschezza del nuovo episodio
    assert row.opened_at_cycle_id == 900
    assert row.closed_ts is None             # full-state: nessun residuo chiusura


@requires_postgres
def test_alert_lineage_survives_wrong_alert_id(storage):
    """Se il chiamante passa un alert_id diverso per la stessa coppia
    (valve, fault), la PK di lineage NON cambia (ON CONFLICT non tocca
    mai l'alert_id)."""
    s = storage
    aid = str(alert_id_for(3, "restriction", RUN_DI_PROVA))
    s.upsert_alert(alert_id=aid, valve_id=3, fault_type="restriction",
                   run_id=RUN_DI_PROVA, status="open", opened_ts="2026-08-13T00:00:00Z",
                   opened_at_cycle_id=100,
                   last_seen_ts="2026-08-13T00:00:00Z",
                   max_score_seen=0.9, n_cycles_above=1)
    s.upsert_alert(alert_id=str(uuid.uuid4()), valve_id=3,
                   fault_type="restriction", status="sustained",
                   run_id=RUN_DI_PROVA,
                   opened_ts="2026-08-13T00:00:00Z", opened_at_cycle_id=100,
                   last_seen_ts="2026-08-13T00:01:00Z",
                   max_score_seen=0.95, n_cycles_above=2)
    with s.engine.connect() as conn:
        rows = conn.execute(select(s.alerts)).fetchall()
    assert len(rows) == 1
    assert str(rows[0].alert_id) == aid


@requires_postgres
def test_alert_two_lineages_two_rows(storage):
    """Coppie (valve, fault) diverse → righe diverse (il dedup è per coppia,
    non per valvola)."""
    s = storage
    s.upsert_alert(alert_id=str(alert_id_for(1, "restriction", RUN_DI_PROVA)),
                   valve_id=1, fault_type="restriction", status="open",
                   run_id=RUN_DI_PROVA,
                   opened_ts="2026-08-13T00:00:00Z", opened_at_cycle_id=50,
                   last_seen_ts="2026-08-13T00:00:00Z",
                   max_score_seen=0.9, n_cycles_above=1)
    s.upsert_alert(
        alert_id=str(alert_id_for(1, "flowmeter_dropout", RUN_DI_PROVA)),
        valve_id=1, fault_type="flowmeter_dropout", status="open",
        run_id=RUN_DI_PROVA,
        opened_ts="2026-08-13T00:00:00Z", opened_at_cycle_id=60,
        last_seen_ts="2026-08-13T00:00:00Z",
        max_score_seen=0.7, n_cycles_above=1)
    # stessa coppia (valvola, guasto) ma run diverso → terza riga: la
    # separazione per run è nella chiave, non in una guardia a runtime.
    s.upsert_alert(alert_id=str(alert_id_for(1, "restriction", "altro_run")),
                   valve_id=1, fault_type="restriction", status="open",
                   run_id="altro_run",
                   opened_ts="2026-08-13T00:00:00Z", opened_at_cycle_id=50,
                   last_seen_ts="2026-08-13T00:00:00Z",
                   max_score_seen=0.9, n_cycles_above=1)
    with s.engine.connect() as conn:
        rows = conn.execute(select(s.alerts)).fetchall()
    assert len(rows) == 3


@requires_postgres
def test_alert_transition_fk_enforced(storage):
    """FK alert_transitions.alert_id: transizione per alert_id inesistente
    → IntegrityError (spec M10 §2, fix B5)."""
    s = storage
    with pytest.raises(IntegrityError):
        s.insert_transition(
            transition_id=str(uuid.uuid4()),
            alert_id="99999999-9999-9999-9999-999999999999",
            transition_ts=_to_dt("2026-08-13T00:00:00Z"),
            from_status="closed", to_status="open", anomaly_score=0.9,
            threshold_open=0.5, threshold_close=0.4, window_end_cycle_id=50,
            valve_id=3, fault_type="restriction", run_id=RUN_DI_PROVA)


@requires_postgres
def test_machine_state_roundtrip(storage):
    s = storage
    s.set_machine_state("omac_state", {"state": 1, "label": "Running"})
    assert s.get_machine_state("omac_state") == {"state": 1, "label": "Running"}


@requires_postgres
def test_bottle_counter_kv(storage):
    """KV `bottle_counter` su machine_state (oee-backend-spec §B2)."""
    s = storage
    assert s.get_bottle_counter() is None
    s.set_bottle_counter(14820)
    assert s.get_bottle_counter() == 14820
    s.set_bottle_counter(14821)  # upsert: mai due righe per la chiave
    assert s.get_bottle_counter() == 14821
    assert s.get_machine_state("bottle_counter") == 14821


@requires_postgres
def test_machine_state_history_append_and_current(storage):
    """Append-only: ogni log aggiunge una riga; la più recente è current
    (exited_ts NULL = stato corrente)."""
    s = storage
    assert s.current_machine_state_history() is None
    s.log_machine_state_history(4, "Idle", source="test")
    cur = s.current_machine_state_history()
    assert cur is not None
    assert cur["state_code"] == 4
    assert cur["state_label"] == "Idle"
    assert cur["exited_ts"] is None
    assert cur["source"] == "test"
    s.log_machine_state_history(11, "Starting", source="test")
    cur = s.current_machine_state_history()
    assert cur["state_code"] == 11
    # due righe, mai una update in place (append-only)
    rows = s.get_machine_state_history(
        _to_dt("2020-01-01T00:00:00Z"), _to_dt("2030-01-01T00:00:00Z"))
    assert [r["state_code"] for r in rows] == [4, 11]


@requires_postgres
def test_machine_state_history_close_current(storage):
    """close: chiude la transizione aperta più recente (writer realtime)."""
    s = storage
    s.log_machine_state_history(1, "Running", source="test")
    s.close_machine_state_history(_to_dt("2026-08-13T01:00:00Z"))
    cur = s.current_machine_state_history()
    assert cur["exited_ts"] == _to_dt("2026-08-13T01:00:00Z")
    # nessuna riga aperta: no-op, non raise
    s.close_machine_state_history(_to_dt("2026-08-13T02:00:00Z"))


@requires_postgres
def test_machine_state_history_window_query(storage):
    """Query window: solo le transizioni che si SOVRAPPONGONO a [start, end)."""
    s = storage
    # transizione CHIUSA prima della finestra → esclusa (Stopping, giorno prima)
    s.log_machine_state_history(2, "Stopping",
                                entered_ts=_to_dt("2026-08-12T20:00:00Z"),
                                source="test")
    s.close_machine_state_history(_to_dt("2026-08-12T23:00:00Z"))
    # Idle che inizia prima della finestra e chiude DENTRO → inclusa (clip)
    s.log_machine_state_history(4, "Idle",
                                entered_ts=_to_dt("2026-08-13T00:00:00Z"),
                                source="test")
    s.close_machine_state_history(_to_dt("2026-08-13T01:30:00Z"))
    # Running aperta dentro la finestra → inclusa
    s.log_machine_state_history(1, "Running",
                                entered_ts=_to_dt("2026-08-13T02:00:00Z"),
                                source="test")
    # transizione che inizia DOPO la finestra → esclusa
    s.log_machine_state_history(3, "Stopped",
                                entered_ts=_to_dt("2026-08-13T10:00:00Z"),
                                source="test")
    rows = s.get_machine_state_history(
        _to_dt("2026-08-13T01:00:00Z"), _to_dt("2026-08-13T05:00:00Z"))
    assert [r["state_label"] for r in rows] == ["Idle", "Running"]
    # transizione aperta che INIZIA dentro la finestra → inclusa
    s.log_machine_state_history(1, "Running",
                                entered_ts=_to_dt("2026-08-13T03:00:00Z"),
                                source="test")
    rows = s.get_machine_state_history(
        _to_dt("2026-08-13T01:00:00Z"), _to_dt("2026-08-13T05:00:00Z"))
    assert [r["state_label"] for r in rows] == ["Idle", "Running", "Running"]
    # chiuse prima dello start → escluse (Idle chiude 01:30 < 02:30)
    rows = s.get_machine_state_history(
        _to_dt("2026-08-13T02:30:00Z"), _to_dt("2026-08-13T05:00:00Z"))
    assert [r["state_label"] for r in rows] == ["Running", "Running"]
    # ordinamento per entered_ts; finestra senza overlap (prima di ogni
    # transizione) → lista vuota. NB: le transizioni APERTE (exited_ts NULL)
    # si sovrappongono a ogni finestra successiva — comportamento voluto.
    assert s.get_machine_state_history(
        _to_dt("2026-08-12T00:00:00Z"), _to_dt("2026-08-12T12:00:00Z")) == []
