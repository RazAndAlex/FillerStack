"""Test OEE Home L0 (oee-backend-spec §D) — API /machine/oee + wire.

Copre il wire dati dell'OEE (spec dashboard §7.2) end-to-end:
- GET /machine/oee su DB dedicato (`plcsim_test_oee_<random>`): window
  shift|day, at, Availability da `machine_state_history`, Performance/
  Quality da `cycles.event_ts` + `fill_quality_ok`, prev window, degraded
  con reason (MAI 404), 422 su parametri invalidi;
- tabella `cycles` TEST-LOCALE con `event_ts` (la colonna arriverà dal
  worker cycles_storage — il test definisce il contratto che l'API legge:
  event_ts + fill_quality_ok); l'API degraderà chiaramente finché la
  colonna non esiste;
- writer realtime (plcsim/realtime.py StorageBridge + RealtimeSim stepped):
  transizioni OMAC su machine_state_history (source="realtime") + KV
  bottle_counter (spec oee-backend §C1);
- consumer MQTT: handle_state_payload → log_machine_state_history
  (spec oee-backend §C2, con storage stub — niente broker).

ISOLAMENTO: DB dedicato UNICO PER PROCESSO (`plcsim_test_oee_<random>`,
override con PLCSIM_OEE_TEST_DATABASE_URL). PLCSIM_DATABASE_URL è puntato
al DB di test PRIMA di ogni richiesta (il fixture lo re-imposta): l'API
non legge mai lo storico reale `plcsim`.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import timedelta

import pytest

from .conftest import drop_db_if_ephemeral
from fastapi.testclient import TestClient
from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData,
                        PrimaryKeyConstraint, Table, text)


def _test_db_url() -> str:
    if "PLCSIM_OEE_TEST_DATABASE_URL" in os.environ:
        return os.environ["PLCSIM_OEE_TEST_DATABASE_URL"]
    url = (f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
           f"plcsim_test_oee_{secrets.token_hex(4)}")
    os.environ["PLCSIM_OEE_TEST_DATABASE_URL"] = url
    return url


def _ensure_test_db(url: str) -> None:
    m = re.match(r"postgresql\+psycopg://([^/]+)/([A-Za-z0-9_]+)$", url)
    if not m:
        return
    try:
        from sqlalchemy import create_engine
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
        pass  # best-effort: il ping qui sotto decide (skip)


_TEST_DB_URL = _test_db_url()


@pytest.fixture(scope="session", autouse=True)
def _pulizia_db_effimero():
    """Rimuove il database di test a fine sessione (vedi conftest.py).

    Senza questo ogni processo pytest ne lasciava indietro uno: 50 residui e
    423 MB sul Postgres di sviluppo, con l'avvio del container salito a ~2,5
    minuti. La guardia in conftest cancella solo i nomi effimeri generati qui.
    """
    yield
    drop_db_if_ephemeral(_TEST_DB_URL)
_ensure_test_db(_TEST_DB_URL)

# NB: PLCSIM_DATABASE_URL NON va impostato a livello di modulo: gli altri
# moduli di test dello stesso processo (es. test_api) lo impostano al loro
# DB e girano prima di questo modulo — un override qui li farebbe leggere
# un DB vuoto. L'env è puntato al DB OEE nel FIXTURE (per ogni test), con
# ripristino al valore precedente.

from pipeline import api  # noqa: E402
from pipeline.storage import Storage, _to_dt, make_engine  # noqa: E402


def _pg_available() -> bool:
    try:
        return Storage(make_engine()).ping()
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL non raggiungibile (avvia `docker compose up -d postgres`)")


# -- helper tabella cycles test-locale (contratto letto dall'API) ------------
def _cycles_metadata() -> MetaData:
    """Tabella `cycles` TEST-LOCALE: le due colonne che l'API OEE legge
    (event_ts, fill_quality_ok) + la PK reale (valve_id, cycle_id)."""
    m = MetaData()
    Table("cycles", m,
          Column("valve_id", Integer, nullable=False),
          Column("cycle_id", Integer, nullable=False),
          Column("event_ts", DateTime(timezone=True), nullable=True),
          Column("fill_quality_ok", Boolean, nullable=True),
          PrimaryKeyConstraint("valve_id", "cycle_id"))
    return m


def _ensure_cycles_with_ts(engine) -> None:
    """Crea la tabella cycles (con event_ts) pulita — DB dedicato."""
    m = _cycles_metadata()
    m.drop_all(engine, checkfirst=True)
    m.create_all(engine, checkfirst=True)


# -- seed --------------------------------------------------------------------
def _seed_history(s: Storage) -> None:
    """History OMAC deterministica attorno ad at=2026-08-13T08:00:00Z.

    Finestra shift corrente [00:00, 08:00):
      Running 00:00-00:30 (1800s, inizia prima della finestra) ·
      Idle    00:30-01:30 (3600s) · Running 01:30-05:30 (14400s) ·
      Stopping 05:30-05:30:10 (10s) · Running 05:30:10-ora aperta (8990s)
      → planned 28800s, running 25190s.
    Finestra prev [16:00, 00:00): Idle 20:00-23:00 (10800s) ·
      Running 23:00-00:00 (3600s) → planned 14400s, running 3600s.
    """
    hist = [
        (4, "Idle", "2026-08-12T20:00:00Z", "2026-08-12T23:00:00Z"),
        (1, "Running", "2026-08-12T23:00:00Z", "2026-08-13T00:30:00Z"),
        (4, "Idle", "2026-08-13T00:30:00Z", "2026-08-13T01:30:00Z"),
        (1, "Running", "2026-08-13T01:30:00Z", "2026-08-13T05:30:00Z"),
        (2, "Stopping", "2026-08-13T05:30:00Z", "2026-08-13T05:30:10Z"),
        (1, "Running", "2026-08-13T05:30:10Z", None),  # corrente (aperta)
    ]
    for code, label, entered, exited in hist:
        s.log_machine_state_history(
            code, label, entered_ts=_to_dt(entered), source="test")
        if exited is not None:
            s.close_machine_state_history(_to_dt(exited))


def _seed_cycles(engine) -> None:
    """Cicli: 105.200 in finestra corrente (98.900 ok), 15.600 in prev
    (14.820 ok); quality = 0.94 / 0.95, real = 105200 / 15600."""
    t0 = _to_dt("2026-08-13T00:00:00Z")
    t0_prev = _to_dt("2026-08-12T16:00:00Z")
    m = _cycles_metadata()
    rows = []
    for i in range(105200):
        ok = True if i < 98900 else (False if i < 104000 else None)
        rows.append({"valve_id": (i % 35) + 1, "cycle_id": i + 1,
                     "event_ts": t0 + timedelta(seconds=i % 28800),
                     "fill_quality_ok": ok})
    for i in range(15600):
        rows.append({"valve_id": (i % 35) + 1, "cycle_id": 200000 + i,
                     "event_ts": t0_prev + timedelta(seconds=i % 28800),
                     "fill_quality_ok": i < 14820})
    with engine.begin() as conn:
        conn.execute(m.tables["cycles"].insert(), rows)


@pytest.fixture
def client():
    """Storage fresco sul DB OEE dedicato + cycles con event_ts.

    Punta PLCSIM_DATABASE_URL al DB di test SOLO per la durata del test
    (ripristino al valore precedente nel teardown): l'API costruisce lo
    storage lazy al primo uso, quindi ogni test riparte dal DB
    deterministico senza intaccare gli altri moduli dello stesso processo.
    """
    prev_url = os.environ.get("PLCSIM_DATABASE_URL")
    os.environ["PLCSIM_DATABASE_URL"] = _TEST_DB_URL
    api._store = None
    try:
        s = Storage(make_engine(_TEST_DB_URL))
        s.metadata.drop_all(s.engine, checkfirst=True)
        s.init()
        _ensure_cycles_with_ts(s.engine)
        yield TestClient(api.app)
    finally:
        if prev_url is None:
            os.environ.pop("PLCSIM_DATABASE_URL", None)
        else:
            os.environ["PLCSIM_DATABASE_URL"] = prev_url


def _storage() -> Storage:
    return Storage(make_engine(_TEST_DB_URL))


# -- validazione parametri ---------------------------------------------------
@requires_postgres
def test_oee_validation(client):
    assert client.get("/machine/oee", params={"window": "bogus"}).status_code == 422
    assert client.get("/machine/oee",
                      params={"at": "not-a-date"}).status_code == 422


@requires_postgres
def test_oee_naive_at_treated_as_utc(client):
    r = client.get("/machine/oee", params={"at": "2026-08-13T08:00:00"})
    assert r.status_code == 200
    assert r.json()["at"] == "2026-08-13T08:00:00+00:00"


# -- degraded (mai 404) -------------------------------------------------------
@requires_postgres
def test_oee_degraded_empty_db(client):
    """Nessun dato: 200 con oee=null + degraded + reason (mai 404)."""
    r = client.get("/machine/oee", params={"at": "2026-08-13T08:00:00Z"})
    assert r.status_code == 200
    b = r.json()
    assert b["window"] == "shift"
    assert b["oee"] is None
    assert b["availability"] is None
    assert b["performance"] is None
    assert b["quality"] is None
    assert b["prev"] == {"oee": None, "delta_pp": None}
    src = b["source"]
    assert src["degraded"] is True
    # verifica che il motivo nomini la causa, non il nome interno della tabella:
    # questi testi finiscono a schermo su una dashboard di reparto
    assert "cambio di stato macchina" in src["reason"]
    assert src["cycles_rows"] == 0
    assert src["state_transitions"] == 0


@requires_postgres
def test_oee_degraded_cycles_ts_missing(client):
    """History ok ma tabella cycles assente: A calcolata, P/Q degradate."""
    s = _storage()
    _seed_history(s)
    with s.engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS cycles"))
    r = client.get("/machine/oee", params={"at": "2026-08-13T08:00:00Z"})
    assert r.status_code == 200
    b = r.json()
    assert b["availability"] == pytest.approx(0.875)
    assert b["performance"] is None
    assert b["quality"] is None
    assert b["oee"] is None
    assert b["source"]["degraded"] is True
    assert "storico dei cicli non disponibile" in b["source"]["reason"]
    assert b["source"]["cycles_rows"] == 0
    assert b["source"]["state_transitions"] == 5


@requires_postgres
def test_oee_degraded_cycles_ts_all_null(client):
    """cycles presente ma event_ts NULL ovunque: P/Q degradate con reason."""
    s = _storage()
    _seed_history(s)
    m = _cycles_metadata()
    with s.engine.begin() as conn:
        conn.execute(m.tables["cycles"].insert(), [
            {"valve_id": 1, "cycle_id": 1, "event_ts": None,
             "fill_quality_ok": True},
            {"valve_id": 1, "cycle_id": 2, "event_ts": None,
             "fill_quality_ok": False},
        ])
    r = client.get("/machine/oee", params={"at": "2026-08-13T08:00:00Z"})
    b = r.json()
    assert b["availability"] == pytest.approx(0.875)
    assert b["performance"] is None
    assert b["quality"] is None
    assert b["source"]["degraded"] is True
    assert "non hanno un istante associato" in b["source"]["reason"]


# -- happy path ---------------------------------------------------------------
@requires_postgres
def test_oee_shift_happy_path(client):
    """Calcolo completo su finestra shift con prev window e delta_pp."""
    s = _storage()
    _seed_history(s)
    _seed_cycles(s.engine)
    r = client.get("/machine/oee",
                   params={"window": "shift", "at": "2026-08-13T08:00:00Z"})
    assert r.status_code == 200
    b = r.json()
    assert b["window"] == "shift"
    assert b["at"] == "2026-08-13T08:00:00+00:00"
    assert b["start"] == "2026-08-13T00:00:00+00:00"
    assert b["end"] == "2026-08-13T08:00:00+00:00"

    # Availability: running 25190s / planned 28800s = 0.8747 → 0.875
    assert b["availability"] == pytest.approx(0.875)
    ad = b["availability_detail"]
    assert ad["running_s"] == pytest.approx(25190.0)
    assert ad["planned_s"] == pytest.approx(28800.0)
    assert ad["by_state"]["Running"] == pytest.approx(25190.0)
    assert ad["by_state"]["Idle"] == pytest.approx(3600.0)
    assert ad["by_state"]["Stopping"] == pytest.approx(10.0)

    # Performance: real 105200 / (15500 × 6.9972h = 108456.9) = 0.970
    assert b["performance"] == pytest.approx(0.970)
    pd = b["performance_detail"]
    assert pd["real"] == 105200
    assert pd["theoretical"] == pytest.approx(108456.9)
    assert pd["speed_target"] == 15500.0
    assert pd["running_h"] == pytest.approx(6.997)

    # Quality: 98900 / 105200 = 0.94
    assert b["quality"] == pytest.approx(0.94)
    qd = b["quality_detail"]
    assert (qd["good"], qd["total"]) == (98900, 105200)
    # la disaggregazione per valvola vive nella stessa risposta (stessa
    # finestra, stessa query): di default solo la valvola con il tasso piu'
    # basso, la lista completa su richiesta (per_valve=1)
    assert qd["per_valve"] is None
    assert qd["worst_valve"] in range(1, 36)
    assert 0.0 <= qd["worst_valve_quality"] <= 1.0

    # OEE = 0.875 × 0.970 × 0.94 = 0.798
    assert b["oee"] == pytest.approx(0.798)

    # prev window [16:00, 00:00): A=0.25 (3600/14400), P=15600/15500=1.006,
    # Q=0.95 → oee 0.239; delta_pp = 0.798 - 0.239 = 0.559
    assert b["prev"]["oee"] == pytest.approx(0.239)
    assert b["prev"]["delta_pp"] == pytest.approx(0.559)

    # `run_id` = None: la tabella `cycles` test-locale non ha la colonna
    # `run_id`, quindi non c'e' alcun run da distinguere e la route lo
    # dichiara invece di inventare un nome (schema reale: vedi
    # test_api_run_id.py).
    assert b["source"] == {
        "cycles_rows": 105200, "run_id": None, "state_transitions": 5,
        "window_partial": False, "degraded": False, "reason": None,
    }


@requires_postgres
def test_oee_day_window(client):
    """window=day: finestra 24h; A = 0.5 (12h Running su 24h noti)."""
    s = _storage()
    s.log_machine_state_history(4, "Idle",
                                entered_ts=_to_dt("2026-08-12T00:00:00Z"),
                                source="test")
    s.close_machine_state_history(_to_dt("2026-08-13T00:00:00Z"))
    s.log_machine_state_history(1, "Running",
                                entered_ts=_to_dt("2026-08-13T00:00:00Z"),
                                source="test")
    r = client.get("/machine/oee",
                   params={"window": "day", "at": "2026-08-13T12:00:00Z"})
    assert r.status_code == 200
    b = r.json()
    assert b["window"] == "day"
    assert b["start"] == "2026-08-12T12:00:00+00:00"
    assert b["end"] == "2026-08-13T12:00:00+00:00"
    # clip: Idle 12h (12:00→24:00) + Running 12h (00:00→12:00)
    assert b["availability"] == pytest.approx(0.5)
    assert b["availability_detail"]["planned_s"] == pytest.approx(86400.0)


# -- writer realtime (spec oee-backend §C1) -----------------------------------
@requires_postgres
def test_realtime_bridge_writes_history_and_counter():
    """RealtimeSim stepped + StorageBridge: transizioni OMAC su
    machine_state_history (source='realtime', aperta = stato corrente) e
    KV bottle_counter aggiornato dai cicli (test stepped senza MQTT)."""
    from plcsim.realtime import RealtimeSim, StorageBridge
    # DB dedicato ma CONDIVISO tra i test del modulo: reset esplicito qui
    # (questo test non usa il fixture `client` perché non tocca l'API).
    s = _storage()
    s.metadata.drop_all(s.engine, checkfirst=True)
    s.init()
    bridge = StorageBridge(url=_TEST_DB_URL)
    assert bridge.active is True
    sim = RealtimeSim(seed=42, mode="stepped", storage_bridge=bridge)
    # template giornata: Idle → Starting → Running (skip delle fasi vuote)
    sim.advance(3)
    # cicli valvola in Running (BottleCounter cresce)
    sim.advance(600)
    # CmdStop: Stopping (500ms) → Stopped
    sim.submit_command({"name": "stop"})
    sim.advance(70)

    rows = s.get_machine_state_history(
        _to_dt("2020-01-01T00:00:00Z"), _to_dt("2030-01-01T00:00:00Z"))
    codes = [r["state_code"] for r in rows]
    assert codes == [11, 1, 2, 3], codes  # Starting → Running → Stopping → Stopped
    assert {r["source"] for r in rows} == {"realtime"}
    cur = s.current_machine_state_history()
    assert cur["state_code"] == 3          # Stopped, corrente
    assert cur["exited_ts"] is None
    assert cur["source"] == "realtime"
    # chiusura della transizione precedente (exited_ts valorizzato)
    assert rows[0]["exited_ts"] is not None
    assert s.get_bottle_counter() is not None
    assert s.get_bottle_counter() > 0


def test_storage_bridge_inactive_without_url():
    """Senza PLCSIM_DATABASE_URL il bridge nasce disattivo: nessuna
    connessione, nessun raise (i test del simulatore non toccano il DB)."""
    from plcsim.realtime import StorageBridge
    old = os.environ.pop("PLCSIM_DATABASE_URL", None)
    try:
        b = StorageBridge()
        assert b.active is False
        b.on_state(1, "Running")   # no-op
        b.on_cycle(42)             # no-op
        assert b.errors == 0
    finally:
        if old is not None:
            os.environ["PLCSIM_DATABASE_URL"] = old


# -- consumer MQTT (spec oee-backend §C2) -------------------------------------
class _FakeStorage:
    def __init__(self):
        self.calls = []

    def log_machine_state_history(self, state_code, state_label,
                                  entered_ts=None, source=None):
        self.calls.append((state_code, state_label, entered_ts, source))


def test_ingest_state_payload_writes_history(monkeypatch):
    """handle_state_payload → log_machine_state_history (source
    'mqtt:plant/filler01/state'); payload invalido → state_invalid."""
    from pipeline.ingest import IngestConsumer
    c = IngestConsumer()
    fake = _FakeStorage()
    monkeypatch.setattr("pipeline.ingest._ingest_storage", lambda: fake)
    out = c.handle_state_payload(json.dumps({
        "state_code": 1, "state": "Running",
        "ts": "2026-08-13T08:00:00Z"}))
    assert out == "state_written"
    assert c.stats["state_received"] == 1
    assert c.stats["state_written"] == 1
    assert len(fake.calls) == 1
    code, label, ts, src = fake.calls[0]
    assert code == 1
    assert label == "Running"
    assert ts == _to_dt("2026-08-13T08:00:00Z")
    assert src == "mqtt:plant/filler01/state"
    # state_label assente → fallback sul codice; ts assente → None (now UTC)
    assert c.handle_state_payload(json.dumps(
        {"state_code": 3})) == "state_written"
    assert fake.calls[1][1] == "3"
    assert fake.calls[1][2] is None
    # invalidi
    assert c.handle_state_payload(b"not-json") == "state_invalid"
    assert c.handle_state_payload(json.dumps({"state": "Running"})) == "state_invalid"
    assert c.stats["state_invalid"] == 2


def test_ingest_state_payload_storage_unavailable(monkeypatch):
    """Storage non disponibile: il consumer NON si ferma (esito dedicato,
    nessun crash, il payload valido non viene contato come scritto)."""
    from pipeline.ingest import IngestConsumer
    c = IngestConsumer()
    monkeypatch.setattr("pipeline.ingest._ingest_storage", lambda: None)
    out = c.handle_state_payload(json.dumps({"state_code": 4}))
    assert out == "state_storage_unavailable"
    assert c.stats["state_received"] == 1
    assert c.stats["state_written"] == 0


@requires_postgres
def test_target_non_verificato_con_rapporto_implausibile_degrada(client):
    """Un target di default incoerente con l'impianto NON produce un OEE.

    Caso reale che ha motivato il controllo: la cadenza dei cicli del
    simulatore (3,2 s per valvola, 35 valvole = ~39.375 cph) contro il
    target di default 15500 cph produce Performance ~2,54 e quindi un OEE
    del 194%. Non e' una macchina veloce: e' un target sbagliato
    (docs/V3-DESIGN.md sez. 188 registra il punto come "da riconciliare
    con la cadenza osservata"). L'API deve degradare con reason e NON
    moltiplicare quel fattore dentro l'OEE — pero' senza nascondere nulla:
    il rapporto grezzo resta esposto.
    """
    s = _storage()
    _seed_history(s)
    m = _cycles_metadata()
    t0 = _to_dt("2026-08-13T00:00:00Z")
    # running_h della finestra = 6.997 -> theoretical = 108456.9.
    # 300.000 cicli danno un rapporto ~2.77, oltre la soglia 1.25.
    rows = [{"valve_id": (i % 35) + 1, "cycle_id": i + 1,
             "event_ts": t0 + timedelta(seconds=i % 28800),
             "fill_quality_ok": True} for i in range(300000)]
    with s.engine.begin() as conn:
        conn.execute(m.tables["cycles"].insert(), rows)

    b = client.get("/machine/oee",
                   params={"window": "shift", "at": "2026-08-13T08:00:00Z"}).json()

    pd = b["performance_detail"]
    assert pd["speed_target_source"] == "default"
    assert pd["ratio_osservato"] > 1.25          # il rapporto grezzo resta visibile
    assert b["performance"] is None              # ma non diventa una misura
    assert b["oee"] is None                      # e non fabbrica un OEE
    assert b["source"]["degraded"] is True
    assert "speed_target non verificato" in b["source"]["reason"]


@requires_postgres
def test_sovravelocita_normale_non_degrada(client):
    """Superare il target di qualche punto e' normale e resta una misura."""
    s = _storage()
    _seed_history(s)
    _seed_cycles(s.engine)
    b = client.get("/machine/oee",
                   params={"window": "shift", "at": "2026-08-13T08:00:00Z"}).json()
    # la finestra precedente ha rapporto 1.006: sopra 1 ma ampiamente
    # sotto la soglia di implausibilita', quindi resta una Performance valida
    assert b["prev"]["oee"] == pytest.approx(0.239)


# ===========================================================================
# window=hour · qualita' per valvola · serie temporale  (aggiunte 2026-08-19)
# ===========================================================================

def _seed_cycles_leggero(engine) -> None:
    """Cicli radi su [12-08 16:00, 13-08 08:00), con un BUCO fra 02:00 e 05:00.

    Serve alle prove sulla serie: pochi cicli (960) tengono il test veloce, e
    il buco produce finestre genuinamente senza dati — cioe' i punti degradati
    che la regola di prodotto impone di emettere invece di omettere.
    """
    m = _cycles_metadata()
    t0 = _to_dt("2026-08-12T16:00:00Z")
    rows = []
    for i in range(960):                       # un ciclo al minuto per 16 h
        ts = t0 + timedelta(minutes=i)
        if _to_dt("2026-08-13T02:00:00Z") <= ts < _to_dt("2026-08-13T05:00:00Z"):
            continue                           # buco voluto
        rows.append({"valve_id": (i % 35) + 1, "cycle_id": i + 1,
                     "event_ts": ts,
                     # la valvola 3 non produce mai un ciclo buono: e' la
                     # peggiore per costruzione, e il test lo verifica come
                     # MISURA (nessuna attribuzione di causa)
                     "fill_quality_ok": ((i % 35) + 1) != 3 and i % 10 != 0})
    with engine.begin() as conn:
        conn.execute(m.tables["cycles"].insert(), rows)


# -- window=hour -------------------------------------------------------------
@requires_postgres
def test_oee_window_hour(client):
    """`hour` = finestra di 1 h: start = at - 1h, A/P/Q sulla sola ora."""
    s = _storage()
    _seed_history(s)
    _seed_cycles(s.engine)
    r = client.get("/machine/oee",
                   params={"window": "hour", "at": "2026-08-13T08:00:00Z"})
    assert r.status_code == 200
    b = r.json()
    assert b["window"] == "hour"
    assert b["start"] == "2026-08-13T07:00:00+00:00"
    assert b["end"] == "2026-08-13T08:00:00+00:00"
    # nella finestra [07:00, 08:00) la macchina e' in Running per tutti i
    # 3600 s (transizione aperta dalle 05:30:10) -> A = 1.0
    assert b["availability"] == pytest.approx(1.0)
    assert b["availability_detail"]["planned_s"] == pytest.approx(3600.0)
    # i cicli del seed cadono su [00:00, 08:00) con event_ts = t0 + i%28800:
    # l'ora [07:00, 08:00) ne contiene 3 giri completi da 3600 = 10.800
    assert b["performance_detail"]["real"] == 10800
    assert b["source"]["cycles_rows"] == 10800
    # la finestra corta e' piu' STRETTA di shift: prev = [06:00, 07:00)
    assert b["prev"]["oee"] is not None


@requires_postgres
def test_oee_window_15min_rifiutata(client):
    """`15min` NON esiste: sotto l'ora il rumore binomiale sulla Q (0,0042 a
    15 min) supera l'escursione reale della Q su una giornata (0,0071-0,0090)
    e l'Availability diventa binaria (MISURE-b-c.md sez. c.5).

    Il limite e' verso il BASSO, ed e' quello che questo test difende. Verso
    l'alto non c'e' argomento contrario: allargare la finestra riduce il
    rumore, non lo aumenta. `week` e `month` sono stati aggiunti il 2026-08-20
    perche' il riepilogo orario (`pipeline/cycle_rollup.py`) li ha resi
    calcolabili — prima una media su 30 giorni voleva dire contare milioni di
    cicli a ogni punto.
    """
    assert client.get("/machine/oee",
                      params={"window": "15min"}).status_code == 422
    assert set(api.WINDOW_INTERVALS) == {"hour", "shift", "day", "week", "month"}
    assert min(api.WINDOW_INTERVALS.values()) == api.HOUR_INTERVAL


# -- qualita' per valvola sulla finestra dell'OEE ----------------------------
@requires_postgres
def test_quality_detail_per_valvola_stessa_finestra(client):
    """La qualita' per valvola sulla STESSA finestra dell'OEE.

    E' l'unico modo di renderla confrontabile con la Q di macchina:
    `/valves/{id}/kpi` copre 400 cicli (~22 minuti) e `/valves/baseline` copre
    la run sana di riferimento, non la finestra corrente.
    """
    s = _storage()
    _seed_history(s)
    _seed_cycles_leggero(s.engine)
    b = client.get("/machine/oee",
                   params={"window": "shift", "at": "2026-08-13T08:00:00Z",
                           "per_valve": "1"}).json()
    qd = b["quality_detail"]
    pv = qd["per_valve"]
    assert pv is not None
    # i totali di macchina sono la somma esatta delle righe per valvola
    assert sum(v["total"] for v in pv) == qd["total"]
    assert sum(v["good"] for v in pv) == qd["good"]
    # la peggiore e' quella misurata piu' bassa (valvola 3 per costruzione)
    misurate = [v for v in pv if v["quality"] is not None]
    atteso = min(misurate, key=lambda v: (v["quality"], v["valve_id"]))
    assert qd["worst_valve"] == atteso["valve_id"] == 3
    assert qd["worst_valve_quality"] == atteso["quality"] == 0.0
    assert qd["worst_valve_total"] == atteso["total"]
    # una valvola senza cicli nella finestra ha qualita' NON MISURATA (null),
    # non 0.0 — e non puo' quindi risultare "la peggiore"
    for v in pv:
        assert (v["quality"] is None) == (v["total"] == 0)


@requires_postgres
def test_quality_per_valvola_e_misura_non_attribuzione(client):
    """Vincolo di prodotto: si pubblica la misura, mai l'attribuzione.

    Una valvola su 35 vale al massimo il 2,9% del prodotto, e una valvola a
    qualita' 0,000 per 15 h muove la Q di macchina di 0,7 punti: i numeri non
    reggono l'affermazione "la perdita di qualita' e' causata da questa
    valvola". Nessun campo della risposta puo' quindi nominare una causa.
    """
    s = _storage()
    _seed_history(s)
    _seed_cycles_leggero(s.engine)
    b = client.get("/machine/oee",
                   params={"window": "shift", "at": "2026-08-13T08:00:00Z",
                           "per_valve": "1"}).json()
    vietate = ("cause", "causa", "culprit", "responsabile", "colpevole",
               "blame", "root_cause")

    def _chiavi(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from _chiavi(v)
        elif isinstance(o, list):
            for v in o:
                yield from _chiavi(v)

    trovate = [k for k in _chiavi(b) if any(p in k.lower() for p in vietate)]
    assert trovate == [], trovate
    # il peso della peggiore resta leggibile accanto al totale di macchina:
    # chi consuma la route puo' calcolare da se' quanto pesa (<= 1/35)
    qd = b["quality_detail"]
    assert qd["worst_valve_total"] / qd["total"] <= 1 / 35 + 0.01
    # di default la lista completa non c'e' (la serie la moltiplicherebbe)
    b2 = client.get("/machine/oee",
                    params={"window": "shift",
                            "at": "2026-08-13T08:00:00Z"}).json()
    assert b2["quality_detail"]["per_valve"] is None
    assert b2["quality_detail"]["worst_valve"] == 3


# -- serie temporale ---------------------------------------------------------
@requires_postgres
def test_serie_punto_identico_a_machine_oee(client):
    """IL test che protegge l'unico punto di verita'.

    Ogni punto della serie deve essere la risposta ESATTA di /machine/oee con
    lo stesso `at` e la stessa `window` — campo per campo, non solo l'OEE. Se
    un giorno la serie ricalcolasse per conto suo, questo test cade.
    """
    s = _storage()
    _seed_history(s)
    _seed_cycles_leggero(s.engine)
    r = client.get("/machine/oee/series",
                   params={"at": "2026-08-13T08:00:00Z", "windows": "shift"})
    assert r.status_code == 200
    punti = r.json()["shift"]
    assert len(punti) >= 3
    for p in (punti[0], punti[len(punti) // 2], punti[-1]):
        atteso = client.get("/machine/oee",
                            params={"window": "shift", "at": p["at"]}).json()
        assert p == atteso, p["at"]
        # le 13 chiavi sono quelle della route: nessuna in piu', nessuna in meno
        assert set(p) == {
            "window", "at", "start", "end", "availability",
            "availability_detail", "performance", "performance_detail",
            "quality", "quality_detail", "oee", "prev", "source"}


@requires_postgres
def test_serie_forma_servita(client):
    """Forma di default: {shift, shift_ridotto, day, day_ridotto} (+ __meta).

    Le liste `_ridotto` portano solo `at` + le tre componenti + `oee`: e' cio'
    che un grafico consuma, senza trascinare i dettagli di ogni punto.
    """
    s = _storage()
    _seed_history(s)
    _seed_cycles_leggero(s.engine)
    b = client.get("/machine/oee/series",
                   params={"at": "2026-08-13T08:00:00Z"}).json()
    assert set(b) == {"__meta", "shift", "shift_ridotto", "day", "day_ridotto"}
    assert len(b["shift"]) == len(b["shift_ridotto"])
    assert len(b["day"]) == len(b["day_ridotto"])
    for pieno, rid in zip(b["shift"], b["shift_ridotto"]):
        assert set(rid) == {"at", "availability", "performance", "quality",
                            "oee"}
        assert all(rid[k] == pieno[k] for k in rid)
    # ordine cronologico crescente, passo costante (60 min su shift)
    ats = [p["at"] for p in b["shift"]]
    assert ats == sorted(ats)
    assert b["__meta"]["shift"]["passo"] == "60min"
    assert b["__meta"]["day"]["passo"] == "120min"
    assert b["__meta"]["shift"]["ampiezza_finestra"] == "480min"


@requires_postgres
def test_serie_non_omette_i_punti_senza_dati(client):
    """Regola di prodotto: non si omette nulla.

    Il seed ha un buco voluto fra le 02:00 e le 05:00. I punti che cadono
    interamente nel buco vengono emessi comunque, con `oee: null` e
    `source.degraded: true`: omettere il punto nasconderebbe alla vista il
    fatto "qui non c'e' dato", che e' un fatto reale.
    """
    s = _storage()
    _seed_history(s)
    _seed_cycles_leggero(s.engine)
    b = client.get("/machine/oee/series",
                   params={"at": "2026-08-13T08:00:00Z",
                           "windows": "hour"}).json()
    punti = b["hour"]
    # passo 1 h = ampiezza 1 h: nessuna sovrapposizione fra punti consecutivi
    assert b["__meta"]["hour"]["passo"] == "60min"
    assert b["__meta"]["hour"]["ampiezza_finestra"] == "60min"
    degradati = [p for p in punti if p["source"]["degraded"]]
    assert degradati, "il buco 02:00-05:00 deve produrre punti degradati"
    for p in degradati:
        assert p["oee"] is None
        assert p["source"]["reason"]
    # nessun buco nella SERIE: i punti sono contigui a passo costante
    ats = [p["at"] for p in punti]
    assert ats == sorted(ats)
    assert len(set(ats)) == len(ats)
    assert b["__meta"]["hour"]["punti_degradati"] == len(degradati)
    assert b["__meta"]["hour"]["motivi_degrado"]


@requires_postgres
def test_serie_si_ferma_al_primo_ciclo_reale(client):
    """La camminata all'indietro non fabbrica buchi prima del primo dato.

    `primo_ciclo_reale` = 2026-08-12T16:00Z, `at` = 13-08 08:00Z → copertura
    16 h. Con passo 60 min la serie shift ha 17 punti (16 h / 1 h + 1) e non
    25, che sarebbero 8 punti di vuoto anteriore al dato.
    """
    s = _storage()
    _seed_history(s)
    _seed_cycles_leggero(s.engine)
    b = client.get("/machine/oee/series",
                   params={"at": "2026-08-13T08:00:00Z",
                           "windows": "shift"}).json()
    meta = b["__meta"]
    assert meta["primo_ciclo_reale"] == "2026-08-12T16:00:00+00:00"
    assert meta["shift"]["punti"] == 17
    assert meta["shift"]["primo_at"] == "2026-08-12T16:00:00+00:00"
    assert meta["shift"]["ultimo_at"] == "2026-08-13T08:00:00+00:00"
    assert meta["speed_target"] == 15500.0
    assert meta["speed_target_source"] == "default"


@requires_postgres
def test_serie_db_vuoto_emette_un_punto_degradato(client):
    """Senza alcun ciclo la serie non e' vuota: un punto che dice 'niente dato'."""
    b = client.get("/machine/oee/series",
                   params={"at": "2026-08-13T08:00:00Z"}).json()
    assert b["__meta"]["primo_ciclo_reale"] is None
    for w in ("shift", "day"):
        assert len(b[w]) == 1
        assert b[w][0]["oee"] is None
        assert b[w][0]["source"]["degraded"] is True


@requires_postgres
def test_serie_validazione_windows(client):
    """`windows` accetta solo hour|shift|day; combinazione libera."""
    assert client.get("/machine/oee/series",
                      params={"windows": "15min"}).status_code == 422
    assert client.get("/machine/oee/series",
                      params={"windows": ""}).status_code == 422
    assert client.get("/machine/oee/series",
                      params={"at": "non-una-data"}).status_code == 422
    s = _storage()
    _seed_history(s)
    _seed_cycles_leggero(s.engine)
    b = client.get("/machine/oee/series",
                   params={"at": "2026-08-13T08:00:00Z",
                           "windows": "hour,day"}).json()
    assert set(b) == {"__meta", "hour", "hour_ridotto", "day", "day_ridotto"}


# ===========================================================================
# Separazione dei due contatori di finestra dal discriminante "esistono dati?"
# (2026-08-20). La vecchia query teneva la finestra dentro i COUNT FILTER e
# nel WHERE il solo run: contare un giorno leggeva l'INTERO run (misurato:
# 605.626 buffer, 76 s su 36 milioni di righe). Questi test fissano cio' che
# NON deve cambiare mentre la forma della query cambia.
# ===========================================================================

@requires_postgres
def test_finestra_vuota_con_dati_nel_run_degrada_come_prima(client):
    """Finestra senza cicli DENTRO un run che ha dati: il degrado di sempre.

    E' il caso che la nuova forma poteva rompere: il `GROUP BY` di una
    finestra vuota restituisce ZERO righe, non 35 righe a zero, e il
    discriminante "esistono dati?" resta VERO (i cicli ci sono, altrove).
    Quality deve restare `null` con "nessun ciclo prodotto", **mai** 0.0 —
    che leggerebbe come "tutto scarto" — e mai il motivo del `event_ts`
    mancante.
    """
    s = _storage()
    _seed_history(s)
    _seed_cycles_leggero(s.engine)          # buco voluto fra 02:00 e 05:00
    b = client.get("/machine/oee",
                   params={"window": "hour", "at": "2026-08-13T04:00:00Z",
                           "per_valve": "true"}).json()
    assert b["quality"] is None
    assert b["quality_detail"]["good"] == 0
    assert b["quality_detail"]["total"] == 0
    assert b["quality_detail"]["per_valve"] == []
    assert b["quality_detail"]["worst_valve"] is None
    assert b["source"]["cycles_rows"] == 0
    assert b["source"]["degraded"] is True
    assert "nessun ciclo prodotto in questa finestra" in b["source"]["reason"]
    # il motivo del dato assente NON deve comparire: i dati esistono
    assert "non hanno un istante associato" not in b["source"]["reason"]


@requires_postgres
def test_event_ts_null_ovunque_resta_il_degrado_del_dato_assente(client):
    """`event_ts` NULL ovunque: motivo "dato assente", non "finestra vuota".

    Gemello del test precedente, ed e' la coppia che il discriminante deve
    saper distinguere: qui non e' la finestra a essere vuota, e' il dato a
    non esistere. I due casi hanno reason diverse e devono restare diverse.
    """
    s = _storage()
    _seed_history(s)
    m = _cycles_metadata()
    with s.engine.begin() as conn:
        conn.execute(m.tables["cycles"].insert(), [
            {"valve_id": v, "cycle_id": v, "event_ts": None,
             "fill_quality_ok": True} for v in range(1, 36)])
    b = client.get("/machine/oee",
                   params={"at": "2026-08-13T08:00:00Z",
                           "per_valve": "true"}).json()
    assert b["performance"] is None and b["quality"] is None
    assert b["quality_detail"]["per_valve"] == []
    assert "non hanno un istante associato" in b["source"]["reason"]
    assert "nessun ciclo prodotto" not in b["source"]["reason"]


@requires_postgres
def test_finestra_piena_conteggi_invariati(client):
    """Finestra piena: gli stessi numeri della forma precedente.

    I valori sono quelli documentati in `_seed_cycles` (105.200 cicli,
    98.900 buoni) e sono gia' asseriti dall'happy path: qui si verifica che
    reggano anche chiedendo la disaggregazione, e che la somma per valvola
    sia ESATTAMENTE il totale di macchina (unica query, unica finestra).
    """
    s = _storage()
    _seed_history(s)
    _seed_cycles(s.engine)
    b = client.get("/machine/oee",
                   params={"window": "shift", "at": "2026-08-13T08:00:00Z",
                           "per_valve": "true"}).json()
    qd = b["quality_detail"]
    assert (qd["good"], qd["total"]) == (98900, 105200)
    assert b["performance_detail"]["real"] == 105200
    per_valve = qd["per_valve"]
    assert len(per_valve) == 35
    assert sum(v["total"] for v in per_valve) == 105200
    assert sum(v["good"] for v in per_valve) == 98900


@requires_postgres
def test_bucket_identico_alla_query_diretta(client):
    """La serie legge a secchielli: deve dare gli stessi conteggi, riga a riga.

    `/machine/oee/series` non rilegge `cycles` per ognuno dei suoi 50
    conteggi: ne fa UNA lettura aggregata a secchielli e somma. E' esatto
    solo se ogni bordo di finestra cade su un bordo di secchiello e se il
    secchiello e' chiuso a SINISTRA come la finestra ([start, end)) — con
    `floor` invece di `ceil()-1` un ciclo esattamente sul bordo cadrebbe
    nella finestra sbagliata, ed e' quello che questo test vede: i cicli
    seminati stanno sul minuto esatto, quindi sui bordi.
    """
    from math import gcd
    s = _storage()
    _seed_cycles_leggero(s.engine)
    end = _to_dt("2026-08-13T08:00:00Z")
    diretto = api._CycleCounts(s, None)
    for window in ("hour", "shift", "day"):
        passo = api.SERIES_STEP[window]
        interval = api.WINDOW_INTERVALS[window]
        n = 13
        grain = timedelta(seconds=gcd(int(passo.total_seconds()),
                                      int(interval.total_seconds())))
        secchielli = api._CycleCountsBucketed(
            s, None, anchor=end,
            lo=end - ((n - 1) * passo + 2 * interval), grain=grain)
        for k in range(n):
            e = end - k * passo
            for ws, we in ((e - interval, e), (e - 2 * interval, e - interval)):
                assert secchielli.window(ws, we) == diretto.window(ws, we), (
                    f"{window} k={k} [{ws}, {we})")


@requires_postgres
def test_serie_punto_identico_anche_su_finestra_vuota(client):
    """Un punto di serie caduto sul buco = la stessa risposta di /machine/oee.

    Il confronto campo per campo esiste gia' per un punto qualunque; qui si
    ripete sul punto che cade nel BUCO dei dati, cioe' dove il percorso a
    secchielli e quello diretto potrebbero divergere senza che nessun
    numero appaia sbagliato.
    """
    s = _storage()
    _seed_history(s)
    _seed_cycles_leggero(s.engine)
    serie = client.get("/machine/oee/series",
                       params={"at": "2026-08-13T04:00:00Z",
                               "windows": "hour"}).json()
    for punto in serie["hour"]:
        singolo = client.get("/machine/oee",
                             params={"window": "hour", "at": punto["at"]}).json()
        assert punto == singolo, punto["at"]


@requires_postgres
def test_serie_non_moltiplica_le_interrogazioni(client):
    """Il costo della serie non cresce con il numero di punti.

    (2026-08-21) `/machine/oee/series` stava a 13,7 s sul database storico.
    Profilata, la richiesta faceva **739 interrogazioni**: la history OMAC
    riletta due volte per punto — una tabella di 300 righe — e i bordi
    parziali dei cicli letti una volta per finestra chiesta invece che una
    volta per richiesta. Il difetto non era una query lenta: era il numero di
    andate e ritorno, e cresceva con i punti.

    Qui si misura proprio quello, perche' e' l'unica cosa che non torna a
    rompersi da sola: il numero di statement per una serie con decine di
    punti su due finestre. La soglia e' larga (il conto reale e' una manciata)
    ma sta un ordine di grandezza sotto la forma precedente.
    """
    from sqlalchemy import event

    s = _storage()
    _seed_history(s)
    _seed_cycles_leggero(s.engine)
    # prima richiesta: costruisce lo storage lazy dell'API
    client.get("/health")
    engine = api._store.engine
    conteggio = {"n": 0}

    def conta(conn, cursor, statement, parameters, context, executemany):
        conteggio["n"] += 1

    event.listen(engine, "before_cursor_execute", conta)
    try:
        r = client.get("/machine/oee/series",
                       params={"at": "2026-08-13T08:00:00Z",
                               "windows": "hour,shift"})
    finally:
        event.remove(engine, "before_cursor_execute", conta)

    assert r.status_code == 200
    punti = sum(len(r.json()[w]) for w in ("hour", "shift"))
    assert punti >= 20, f"servono punti a sufficienza per la prova ({punti})"
    assert conteggio["n"] <= 15, (
        f"{conteggio['n']} interrogazioni per {punti} punti: la serie e' "
        "tornata a leggere il database una volta per punto")


# -- finestra parziale: la copertura si dichiara ------------------------------
@requires_postgres
def test_finestra_interamente_coperta(client):
    """Storia che copre tutta la finestra: nessun marchio, coverage 1.0."""
    s = _storage()
    _seed_history(s)
    _seed_cycles(s.engine)
    b = client.get("/machine/oee",
                   params={"window": "shift",
                           "at": "2026-08-13T08:00:00Z"}).json()
    ad = b["availability_detail"]
    assert ad["window_s"] == pytest.approx(28800.0)
    assert ad["planned_s"] == pytest.approx(28800.0)
    assert ad["uncovered_s"] == 0.0
    assert ad["coverage"] == pytest.approx(1.0)
    assert b["source"]["window_partial"] is False
    assert "finestra parziale" not in (b["source"]["reason"] or "")


@requires_postgres
def test_finestra_comincia_prima_della_storia(client):
    """La storia comincia dentro la finestra: si dichiara quanto manca.

    Il denominatore resta la parte coperta (nessun numero inventato), ma la
    risposta dice quante ore su quante non hanno storia dietro.
    """
    s = _storage()
    _seed_history(s)
    _seed_cycles(s.engine)
    b = client.get("/machine/oee",
                   params={"window": "day",
                           "at": "2026-08-13T08:00:00Z"}).json()
    ad = b["availability_detail"]
    # storia dalle 20:00 del 12: 12 h coperte su 24
    assert ad["window_s"] == pytest.approx(86400.0)
    assert ad["planned_s"] == pytest.approx(43200.0)
    assert ad["uncovered_s"] == pytest.approx(43200.0)
    assert ad["coverage"] == pytest.approx(0.5)
    assert b["source"]["window_partial"] is True
    assert "finestra parziale: 12.0 h su 24.0 h" in b["source"]["reason"]


@requires_postgres
def test_lacuna_in_mezzo_alla_storia(client):
    """Una lacuna IN MEZZO produce lo stesso marchio di una all'inizio."""
    s = _storage()
    # Running 00:00-02:00, buco di 1 h, Running 03:00-08:00
    s.log_machine_state_history(1, "Running",
                                entered_ts=_to_dt("2026-08-13T00:00:00Z"),
                                source="test")
    s.close_machine_state_history(_to_dt("2026-08-13T02:00:00Z"))
    s.log_machine_state_history(1, "Running",
                                entered_ts=_to_dt("2026-08-13T03:00:00Z"),
                                source="test")
    s.close_machine_state_history(_to_dt("2026-08-13T08:00:00Z"))
    b = client.get("/machine/oee",
                   params={"window": "shift",
                           "at": "2026-08-13T08:00:00Z"}).json()
    ad = b["availability_detail"]
    assert ad["planned_s"] == pytest.approx(25200.0)
    assert ad["uncovered_s"] == pytest.approx(3600.0)
    assert ad["coverage"] == pytest.approx(0.875)
    assert b["source"]["window_partial"] is True
    assert "1.0 h su 8.0 h" in b["source"]["reason"]


@requires_postgres
def test_finestra_parziale_non_e_degradata(client):
    """Parziale NON e' degradata: A, P e Q escono, `degraded` resta false.

    Se cambiasse, ogni pagina che nasconde i valori degradati smetterebbe di
    mostrare l'OEE all'inizio dello storico.
    """
    s = _storage()
    _seed_history(s)
    _seed_cycles(s.engine)
    b = client.get("/machine/oee",
                   params={"window": "day",
                           "at": "2026-08-13T08:00:00Z"}).json()
    assert b["source"]["window_partial"] is True
    assert b["source"]["degraded"] is False
    assert b["availability"] is not None
    assert b["performance"] is not None
    assert b["quality"] is not None
    assert b["oee"] is not None


@requires_postgres
def test_copertura_presente_anche_nella_serie(client):
    """I campi nuovi arrivano anche su /machine/oee/series, punto per punto."""
    s = _storage()
    _seed_history(s)
    _seed_cycles(s.engine)
    r = client.get("/machine/oee/series",
                   params={"at": "2026-08-13T08:00:00Z", "windows": "shift"})
    assert r.status_code == 200
    punti = r.json()["shift"]
    assert punti
    for p in punti:
        assert "coverage" in p["availability_detail"]
        assert "window_s" in p["availability_detail"]
        assert "uncovered_s" in p["availability_detail"]
        assert isinstance(p["source"]["window_partial"], bool)
