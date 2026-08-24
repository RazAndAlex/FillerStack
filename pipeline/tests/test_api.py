"""Test API M10 (pipeline/api.py) — contratto read-only, machine-agnostic.

ISOLAMENTO (fix bug 2.4 m10-spec-correctness): tutti i test girano su un DB
di test PRIVATO. Il nome è UNICO PER PROCESSO (`plcsim_test_fix_<random>`,
creato al volo — vedi sotto): i worker del fix wave girano in parallelo
sullo stesso Postgres e un nome fisso condiviso produce corse DDL
(residual risk F2/2.10 delle review). `PLCSIM_DATABASE_URL` è puntato al DB
di test PRIMA della prima richiesta, quindi l'API non legge MAI lo storico
reale `plcsim` — i 4 test originali leggevano il DB di produzione (la
claim "DB di test dedicato isolato" del report era falsa per l'API).

Senza server PostgreSQL raggiungibile i test vengono skippati (`postgres`).
Coprono: health, catalogo valvole FISSO 1..35 senza LIMIT (regressione bug
C1), dettaglio valvola, serie score, KPI per valvola (spec §5), alerts
(filtri + ordinamento opened_ts con NULL in coda, fix bug C3).
"""
from __future__ import annotations

import os
import re
import secrets
import uuid

import pytest

from .conftest import drop_db_if_ephemeral
from fastapi.testclient import TestClient


def _test_db_url() -> str:
    """DB di test: env esplicito O nome unico per processo.

    Ogni processo pytest si crea il proprio DB (`plcsim_test_fix_<random>`):
    i worker del fix wave girano in parallelo sullo stesso Postgres e un
    nome fisso condiviso produce corse DDL (residual risk F2/2.10).
    """
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

# Run delle prediction di prova. Dal 2026-08-22 `predictions` ha un
# discriminante di run e le rotte lo usano per filtrare; le fixture devono
# quindi dichiararlo, e il KV `current_run_id` del DB di prova va allineato
# a questo valore perché le rotte trovino le righe.
RUN_API_TEST = "run_api_test"


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

# PRIMA di qualunque richiesta: l'API costruisce lo storage lazy al primo
# uso con PLCSIM_DATABASE_URL (make_engine → _default_url). Puntiamo al DB
# di test privato — mai a `plcsim` (storico reale).
os.environ["PLCSIM_DATABASE_URL"] = _TEST_DB_URL

from pipeline import api  # noqa: E402
from pipeline.storage import Storage, alert_id_for, make_engine  # noqa: E402
from sqlalchemy import text  # noqa: E402


def _pg_available() -> bool:
    try:
        return Storage(make_engine()).ping()  # legge PLCSIM_DATABASE_URL (test DB)
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL non raggiungibile (avvia `docker compose up -d postgres`)")


def _seed(s: Storage) -> None:
    """Dati deterministici minimi:

    - 2 prediction per ognuna delle 35 valvole (70 righe) → regressione
      bug C1: il catalogo fisso 1..35 deve restare completo, senza LIMIT;
    - alert: open con opened_ts (valve 1), sustained con opened_ts NULL
      (valve 2), closed (valve 3) → ordinamento NULL-last e filtri.
    """
    for v in range(1, 36):
        for k, wcid in enumerate((100 + v, 200 + v), start=1):
            s.insert_prediction({
                "prediction_id": str(uuid.uuid4()),
                "model_version": "test-model",
                "feature_schema_version": "ML-F1",
                "prediction_ts": f"2026-08-13T00:00:{k:02d}Z",
                "machine_id": "filler01",
                "valve_id": v,
                "window_idx": k,
                "window_end_cycle_id": wcid,
                "predicted_label": "healthy" if k == 1 else "restriction",
                "anomaly_score": 0.1 if k == 1 else 0.9,
                "probabilities": {"healthy": 0.9, "restriction": 0.1},
                "feature_fingerprint": "a" * 64,
            }, RUN_API_TEST)
    s.upsert_alert(alert_id=str(alert_id_for(1, "restriction", RUN_API_TEST)),
                   valve_id=1, fault_type="restriction", status="open",
                   run_id=RUN_API_TEST,
                   opened_ts="2026-08-13T00:00:00Z", opened_at_cycle_id=101,
                   last_seen_ts="2026-08-13T00:01:00Z",
                   max_score_seen=0.9, n_cycles_above=2)
    s.upsert_alert(
        alert_id=str(alert_id_for(2, "flowmeter_dropout", RUN_API_TEST)),
        valve_id=2, fault_type="flowmeter_dropout",
        status="sustained", run_id=RUN_API_TEST,
        last_seen_ts="2026-08-13T00:02:00Z",
        max_score_seen=0.95, n_cycles_above=3)
    s.upsert_alert(alert_id=str(alert_id_for(3, "closing_delay", RUN_API_TEST)),
                   valve_id=3, fault_type="closing_delay", status="closed",
                   run_id=RUN_API_TEST,
                   opened_ts="2026-08-13T00:00:00Z", opened_at_cycle_id=150,
                   closed_ts="2026-08-13T00:05:00Z", closed_at_cycle_id=170,
                   last_seen_ts="2026-08-13T00:05:00Z",
                   max_score_seen=0.8, n_cycles_above=2)


@pytest.fixture
def client():
    """Storage fresco sul DB di test (nome unico per processo) + reset dello
    storage lazy dell'API (ogni test riparte da un DB deterministico).
    checkfirst=True: tollera i residui di DDL concorrente (F2/2.10)."""
    api._store = None
    s = Storage(make_engine(_TEST_DB_URL))
    s.metadata.drop_all(s.engine, checkfirst=True)
    s.init()
    _seed(s)
    return TestClient(api.app)


def _max_wcid(valve_id: int) -> int | None:
    """Massimo window_end_cycle_id presente nel DB di test per la valvola.

    Gli assert sull'"ultima prediction" si fanno sul massimo REALE letto
    dallo stesso DB (robusto al writer esterno che inserisce prediction
    in plcsim_test — residual risk F2/2.10 delle review), non su costanti
    del seed.
    """
    from sqlalchemy import func, select
    st = api._store
    assert st is not None, "nessuna richiesta ancora avvenuta (api._store None)"
    with st.engine.connect() as conn:
        return conn.execute(
            select(func.max(st.predictions.c.window_end_cycle_id))
            .where(st.predictions.c.valve_id == valve_id)).scalar()


@requires_postgres
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["db"] is True


@requires_postgres
def test_valve_detail_range_guard(client):
    assert client.get("/valves/0").status_code == 404
    assert client.get("/valves/36").status_code == 404
    assert client.get("/valves/0/score").status_code == 404
    assert client.get("/valves/0/kpi").status_code == 404
    assert client.get("/valves/36/kpi").status_code == 404


@requires_postgres
def test_valves_catalog_fixed_35_no_limit(client):
    """Catalogo FISSO 1..35 con prediction su tutte le valvole.

    Regressione bug C1 (m10-standards): il vecchio `LIMIT 1000` ordinato per
    valve_id faceva sparire silenziosamente le valvole oltre la soglia di
    righe; il catalogo deve restare completo e l'ultima prediction per
    valvola deve essere la finestra maggiore.
    """
    r = client.get("/valves")
    assert r.status_code == 200
    valves = r.json()["valves"]
    assert set(valves.keys()) == {str(v) for v in range(1, 36)}
    # l'ultima prediction servita è la finestra MASSIMA REALE del DB
    # (robusto al writer esterno: l'assert è sul massimo, non su una costante)
    assert valves["7"]["last_prediction"]["window_end_cycle_id"] == _max_wcid(7)
    assert valves["35"]["last_prediction"]["window_end_cycle_id"] == _max_wcid(35)
    # ogni valvola del catalogo ha la sua ultima prediction (nessuna troncatura)
    # alert attivi agganciati al catalogo; closed NON è attivo
    assert [a["status"] for a in valves["1"]["active_alerts"]] == ["open"]
    assert [a["status"] for a in valves["2"]["active_alerts"]] == ["sustained"]
    assert valves["3"]["active_alerts"] == []


@requires_postgres
def test_valve_detail(client):
    r = client.get("/valves/7")
    assert r.status_code == 200
    body = r.json()
    assert body["valve_id"] == 7
    assert body["last_prediction"]["window_end_cycle_id"] == _max_wcid(7)
    assert body["active_alerts"] == []
    r2 = client.get("/valves/1")
    assert r2.status_code == 200
    assert r2.json()["active_alerts"][0]["status"] == "open"


@requires_postgres
def test_valve_score_series(client):
    r = client.get("/valves/7/score")
    assert r.status_code == 200
    series = r.json()["series"]
    # le 2 finestre del seed sono presenti; la serie è ordinata DESC e la
    # prima voce è il massimo REALE del DB (robusto al writer esterno)
    wcids = [s["window_end_cycle_id"] for s in series]
    assert {107, 207} <= set(wcids)
    assert wcids == sorted(wcids, reverse=True)
    assert wcids[0] == _max_wcid(7)


@requires_postgres
def test_valve_kpi_endpoint_exists(client):
    """GET /valves/{id}/kpi esiste (spec M10 §5).

    La serie KPI vive in `pipeline/cycles_storage.py` (tabella `cycles`,
    modulo di un altro worker dello stesso pool): se non è ancora
    installato, l'API degrada con 501 chiaro (import lazy). Il test
    tollera entrambi gli esiti, ma MAI 404 per una valvola in range.
    """
    r = client.get("/valves/7/kpi")
    assert r.status_code in (200, 501), r.status_code
    if r.status_code == 200:
        assert "series" in r.json()
    else:
        detail = r.json()["detail"].lower()
        assert "kpi" in detail or "cycles" in detail


@requires_postgres
def test_alerts_active_and_closed_filter(client):
    r = client.get("/alerts")
    assert r.status_code == 200
    alerts = r.json()["alerts"]
    assert {a["status"] for a in alerts} == {"open", "sustained"}
    assert {a["valve_id"] for a in alerts} == {1, 2}
    rc = client.get("/alerts", params={"closed": True})
    assert rc.status_code == 200
    assert [a["valve_id"] for a in rc.json()["alerts"]] == [3]


@requires_postgres
def test_alerts_nulls_last(client):
    """Ordinamento /alerts: opened_ts NULL in CODA (fix bug C3 — prima i
    NULL fluttuavano in testa con DESC)."""
    r = client.get("/alerts")
    alerts = r.json()["alerts"]
    assert len(alerts) == 2
    assert alerts[0]["valve_id"] == 1          # opened_ts valorizzato
    assert alerts[0]["opened_ts"] is not None
    assert alerts[1]["valve_id"] == 2          # opened_ts NULL → ultimo
    assert alerts[1]["opened_ts"] is None


@requires_postgres
def test_alerts_filters(client):
    r = client.get("/alerts", params={"valve_id": 1})
    assert r.status_code == 200
    assert [a["valve_id"] for a in r.json()["alerts"]] == [1]
    r2 = client.get("/alerts", params={"fault_type": "restriction"})
    assert r2.status_code == 200
    assert [a["fault_type"] for a in r2.json()["alerts"]] == ["restriction"]
    # filtro per valvola fuori range → 422 (query ge/le)
    assert client.get("/alerts", params={"valve_id": 99}).status_code == 422


# === /alerts/history e il terzo valore di stato (aggiunte 2026-08-19) ======

def _righe_alerts_nel_db() -> int:
    """Quante righe ci sono DAVVERO nella tabella `alerts` del DB di test."""
    from sqlalchemy import func, select
    st = api._store
    assert st is not None
    with st.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(st.alerts)).scalar()


@requires_postgres
def test_alerts_history_serve_la_tabella_intera(client):
    """GET /alerts/history = la tabella `alerts` senza filtro di stato.

    La vista storico ha bisogno del terzo caso — «tutti» — che `closed: bool`
    non copre: `closed=0` da' gli attivi, `closed=1` i soli chiusi.
    """
    r = client.get("/alerts/history")
    assert r.status_code == 200
    righe = r.json()["alerts"]
    assert {a["status"] for a in righe} == {"open", "sustained", "closed"}
    assert sorted(a["valve_id"] for a in righe) == [1, 2, 3]
    # stessa forma di /alerts: stesse chiavi, stesso ordinamento NULL-last
    attivi = client.get("/alerts").json()["alerts"]
    assert set(righe[0]) == set(attivi[0])
    assert righe[-1]["opened_ts"] is None      # valvola 2, opened_ts NULL in coda


@requires_postgres
def test_alerts_history_solo_righe_persistite(client):
    """Solo le righe PERSISTITE, mai lo stato interno del motore.

    Una fixture di v6 conteneva 27 righe contro le 6 reali del database: 21
    erano voci vuote (`n_cycles_above=0`, `max_score_seen=0.0`,
    `opened_ts=null`) prodotte iterando lo stato interno del motore invece di
    cio' che il motore aveva emesso. La route conta esattamente le righe della
    tabella, e nessuna di esse e' vuota.
    """
    righe = client.get("/alerts/history").json()["alerts"]
    assert len(righe) == _righe_alerts_nel_db() == 3
    for a in righe:
        assert a["n_cycles_above"] > 0
        assert a["max_score_seen"] > 0.0


@requires_postgres
def test_alerts_status_all_equivalente_a_history(client):
    """`status` aggiunge il terzo valore al parametro esistente e prevale su
    `closed` quando entrambi sono presenti."""
    tutti = client.get("/alerts", params={"status": "all"})
    assert tutti.status_code == 200
    assert tutti.json() == client.get("/alerts/history").json()
    assert ([a["valve_id"] for a in
             client.get("/alerts", params={"status": "closed"}).json()["alerts"]]
            == [3])
    assert (sorted(a["valve_id"] for a in
                   client.get("/alerts",
                              params={"status": "active"}).json()["alerts"])
            == [1, 2])
    # status prevale su closed
    misto = client.get("/alerts", params={"closed": True, "status": "all"})
    assert len(misto.json()["alerts"]) == 3
    assert client.get("/alerts", params={"status": "bogus"}).status_code == 422


@requires_postgres
def test_alerts_history_filtri(client):
    """Gli stessi filtri di /alerts: valvola e tipo di guasto."""
    r = client.get("/alerts/history", params={"valve_id": 3})
    assert [a["status"] for a in r.json()["alerts"]] == ["closed"]
    r2 = client.get("/alerts/history", params={"fault_type": "closing_delay"})
    assert [a["valve_id"] for a in r2.json()["alerts"]] == [3]
    assert client.get("/alerts/history",
                      params={"valve_id": 99}).status_code == 422


# -- l'ultima prediction per valvola: LATERAL, non DISTINCT ON ---------------
# (2026-08-21) `GET /valves` stava a 15,6 s sul database storico. La causa
# misurata non era un indice mancante: `DISTINCT ON (valve_id) ORDER BY
# valve_id, window_end_cycle_id DESC` produce un piano il cui nodo `Unique`
# non sa saltare — percorreva 723.110 righe per tenerne 35. I due test qui
# sotto fissano le due cose che contano: il piano non deve piu' percorrere
# tutta la tabella, e nessuna valvola deve sparire dal catalogo.
@requires_postgres
def test_ultima_prediction_non_percorre_tutta_la_tabella(client):
    """Il piano scende sull'indice 35 volte, non scandisce le 35 finestre.

    Il test non misura il tempo (che dipende dalla macchina) ma le **righe
    realmente percorse**, che sono la differenza fra le due forme: con 200
    prediction per valvola, il `DISTINCT ON` ne percorre 7.000, il `LATERAL`
    35. La soglia sta in mezzo con ampio margine, quindi non e' un test che
    diventa rosso per rumore.
    """
    import json as _json

    st = api._store or Storage(make_engine(_TEST_DB_URL))
    api._store = st
    righe = []
    for v in range(1, 36):
        for k in range(200):
            righe.append({
                "prediction_id": str(uuid.uuid4()),
                "model_version": "test-model",
                "feature_schema_version": "ML-F1",
                "prediction_ts": "2026-08-13T00:00:00Z",
                "machine_id": "filler01",
                "valve_id": v,
                "window_idx": k,
                "window_end_cycle_id": 1000 + k,
                "predicted_label": "healthy",
                "anomaly_score": 0.1,
                "probabilities": {"healthy": 0.9},
                "feature_fingerprint": "a" * 64,
                "run_id": RUN_API_TEST,
            })
    with st.engine.begin() as conn:
        conn.execute(st.predictions.insert(), righe)
        conn.execute(text("ANALYZE predictions"))
        piano = conn.execute(text(
            "EXPLAIN (ANALYZE, FORMAT JSON) "
            + api._sql_ultima_prediction(st.predictions)),
            {"n": 35, "run_id": RUN_API_TEST}).scalar()

    if isinstance(piano, str):
        piano = _json.loads(piano)

    def percorse(nodo) -> float:
        figli = nodo.get("Plans", [])
        if not figli:                      # foglia: e' una scansione
            return nodo["Actual Rows"] * nodo.get("Actual Loops", 1)
        return sum(percorse(f) for f in figli)

    totale = percorse(piano[0]["Plan"])
    assert totale <= 350, f"il piano percorre {totale} righe: e' una scansione"


@requires_postgres
def test_valvola_senza_prediction_resta_nel_catalogo(client):
    """Nessuna valvola sparisce dalla vista macchina (bug C1 fix, invariato).

    La proprieta' che il `DISTINCT ON` era stato scelto per garantire: il
    catalogo parte da 1..35, non dalle righe di `predictions`. Il `LATERAL`
    parte dallo stesso insieme, quindi la conserva — qui si verifica invece
    di darlo per scontato, sul caso che la rompe: una valvola che in
    `predictions` non compare affatto.
    """
    st = api._store or Storage(make_engine(_TEST_DB_URL))
    api._store = st
    with st.engine.begin() as conn:
        conn.execute(st.predictions.delete().where(
            st.predictions.c.valve_id == 17))

    valves = client.get("/valves").json()["valves"]
    assert set(valves.keys()) == {str(v) for v in range(1, 36)}
    assert valves["17"]["last_prediction"] is None
    for v in (1, 16, 18, 35):
        assert valves[str(v)]["last_prediction"]["window_end_cycle_id"] \
            == _max_wcid(v)
