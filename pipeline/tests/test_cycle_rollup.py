"""Test del riepilogo orario (`pipeline/cycle_rollup.py`) e del suo lettore.

## Cosa decide se questo lavoro e' finito

**L'identita' numerica.** Il riepilogo esiste per rispondere in millisecondi
invece che in decine di secondi, e il patto e' che non cambi un solo numero.
Non "vicino": identico — sono conteggi interi. `test_identita_*` confronta,
per sette forme di finestra scelte apposta, la quadrupla
`(total, good, with_ts, per_valve)` calcolata dal riepilogo con quella
calcolata leggendo `cycles` direttamente.

Le sette forme, e perche' ognuna:

1. **allineata all'ora** — il caso banale, l'unico che un riepilogo orario
   servirebbe anche se fosse scritto male;
2. **disallineata di secondi** — il caso vero (`19:29:35`): entrambi i bordi
   sono parziali, ed e' qui che una somma di secchielli sbaglia;
3. **piu' corta di un'ora** — nessuna ora intera da riusare: la finestra e'
   tutta bordo;
4. **a cavallo di due secchielli soltanto** — sempre nessuna ora intera, ma
   due bordi invece di uno;
5. **con ore completamente vuote dentro** — macchina ferma: un'ora senza
   cicli non ha righe nel riepilogo, e "riga assente" deve valere zero, non
   "salta la finestra";
6. **che sborda oltre i bordi del run** — fuori copertura non vale zero,
   vale "non lo so": il lettore deve tornare a `cycles`;
7. **su un run diverso** — due run si sovrappongono nel tempo di parete, e
   senza `run_id` in testa alla chiave i conteggi si sommerebbero.

Piu' due proprieta' del riempimento: rieseguirlo sullo stesso periodo non
cambia una riga (idempotenza), e riempire un periodo parziale e poi
completarlo da' lo stesso risultato di un riempimento unico.

## Isolamento

DB dedicato per processo (`plcsim_test_rollup_<random>`, prefisso
registrato in `conftest._PREFISSI_EFFIMERI` con il teardown gia' previsto).
**Nessun test scrive sul database operazionale `plcsim`.**
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from pipeline.cycle_rollup import (ROLLUP_TABLE, CycleRollup, CycleRollupError,
                                   ceil_hour, floor_hour)
from pipeline.cycles_storage import CyclesStorage
from pipeline.storage import Storage, make_engine

from .conftest import drop_db_if_ephemeral


def _test_db_url() -> str:
    if "PLCSIM_ROLLUP_TEST_DATABASE_URL" in os.environ:
        return os.environ["PLCSIM_ROLLUP_TEST_DATABASE_URL"]
    url = (f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
           f"plcsim_test_rollup_{secrets.token_hex(4)}")
    os.environ["PLCSIM_ROLLUP_TEST_DATABASE_URL"] = url
    return url


def _ensure_test_db(url: str) -> None:
    m = re.match(r"postgresql\+psycopg://([^/]+)/([A-Za-z0-9_]+)$", url)
    if not m:
        return
    try:
        from sqlalchemy import create_engine
        admin = create_engine(f"postgresql+psycopg://{m.group(1)}/postgres",
                              connect_args={"connect_timeout": 3}, future=True)
        with admin.connect().execution_options(
                isolation_level="AUTOCOMMIT") as conn:
            if not conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"),
                                {"n": m.group(2)}).first():
                conn.execute(text(f'CREATE DATABASE "{m.group(2)}"'))
        admin.dispose()
    except Exception:
        pass  # best-effort: il ping decide (skip)


_TEST_DB_URL = _test_db_url()


@pytest.fixture(scope="session", autouse=True)
def _pulizia_db_effimero():
    yield
    drop_db_if_ephemeral(_TEST_DB_URL)


_ensure_test_db(_TEST_DB_URL)


def _pg_available() -> bool:
    try:
        return Storage(make_engine(_TEST_DB_URL)).ping()
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL non raggiungibile (avvia `docker compose up -d postgres`)")


# -- dati di prova ----------------------------------------------------------
# Due run che si SOVRAPPONGONO nel tempo di parete: e' la condizione che rende
# `run_id` indispensabile, e un dato di prova in cui i run non si toccano non
# proverebbe nulla sull'isolamento.
RUN_A = "rollup_test_a"
RUN_B = "rollup_test_b"
VALVOLE = (1, 2, 3)
T0 = datetime(2026, 7, 1, 6, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 3, 11, 20, 37, tzinfo=timezone.utc)
# Fermata: nessun ciclo fra queste due ore, cosi' la finestra 5 contiene ore
# davvero vuote (e non ore "quasi vuote", che non distinguerebbero i due casi).
FERMO = (datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc),
         datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc))
PASSO_CICLI = timedelta(seconds=90)


def _cicli(run_id: str, sfasamento: timedelta, passo: timedelta,
           valvole: tuple[int, ...]) -> list[dict]:
    """Cicli deterministici: cadenza fissa, fermata in mezzo, qualita' 4 su 5.

    I due run devono essere **materialmente** diversi, non solo sfasati: con la
    stessa cadenza uno sfasamento di pochi minuti da' esattamente lo stesso
    conteggio su una finestra di 24 ore, e il test di isolamento passerebbe
    anche se `run_id` venisse ignorato. Lo ha mostrato la prima stesura di
    questo file, dove `test_i_due_run_hanno_conteggi_diversi` e' fallito.
    """
    righe = []
    t = T0 + sfasamento
    cid = 0
    while t < T1:
        if not (FERMO[0] <= t < FERMO[1]):
            for v in valvole:
                cid += 1
                righe.append({
                    "run_id": run_id, "machine_id": "filler01",
                    "cycle_id": cid, "valve_id": v,
                    "event_ts": t, "fill_quality_ok": (cid % 5 != 0),
                })
        t += passo
    return righe


@pytest.fixture(scope="module")
def db():
    """`cycles` popolata con i due run + riepilogo vuoto (DB dedicato)."""
    engine = make_engine(_TEST_DB_URL)
    cs = CyclesStorage(engine)
    cs.drop_all()
    cs.init()
    cs.bulk_insert(_cicli(RUN_A, timedelta(0), PASSO_CICLI, VALVOLE))
    cs.bulk_insert(_cicli(RUN_B, timedelta(minutes=7),
                          timedelta(seconds=140), VALVOLE[:2]))
    r = CycleRollup(engine)
    r.drop_all()
    r.init()
    yield engine
    cs.drop_all()
    r.drop_all()


@pytest.fixture(scope="module")
def riempito(db):
    r = CycleRollup(db)
    r.fill(RUN_A)
    r.fill(RUN_B)
    return r


# -- le sette finestre ------------------------------------------------------
def _finestre() -> dict[str, tuple[datetime, datetime]]:
    ora = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    return {
        "1 allineata all'ora": (ora - timedelta(hours=3), ora),
        "2 disallineata di secondi": (T1 - timedelta(hours=24), T1),
        "3 dentro un solo secchiello": (T1 - timedelta(minutes=10), T1),
        "4 a cavallo di due secchielli": (
            datetime(2026, 7, 2, 12, 40, 5, tzinfo=timezone.utc),
            datetime(2026, 7, 2, 13, 20, 55, tzinfo=timezone.utc)),
        "5 con ore vuote dentro": (
            datetime(2026, 7, 1, 20, 15, 3, tzinfo=timezone.utc),
            datetime(2026, 7, 2, 10, 42, 17, tzinfo=timezone.utc)),
        "6 sborda i bordi del run": (
            T0 - timedelta(hours=5, minutes=17),
            T1 + timedelta(hours=6, minutes=3)),
    }


def _confronta(engine, run: str, finestre: dict) -> list[tuple[str, tuple, tuple, bool]]:
    from pipeline import api
    st = Storage(engine)
    diretto = api._CycleCounts(st, run)
    c = api._CycleCountsRollup(st, run, anchor=max(e for _, e in finestre.values()),
                               lo=min(s for s, _ in finestre.values()),
                               grain=timedelta(hours=1))
    c.prepara(list(finestre.values()))
    out = []
    for nome, (s, e) in finestre.items():
        a, b = c.window(s, e), diretto.window(s, e)
        out.append((nome, a, b, a == b))
    return out


@requires_postgres
@pytest.mark.parametrize("nome", list(_finestre()))
def test_identita_numerica_run_a(riempito, db, nome):
    """Finestre 1-6 sul run corrente: dal riepilogo == da `cycles`."""
    f = {nome: _finestre()[nome]}
    for n, dal_riepilogo, diretto, uguali in _confronta(db, RUN_A, f):
        assert uguali, f"{n}: riepilogo {dal_riepilogo[:2]} != diretto {diretto[:2]}"
        # non solo i totali: anche la disaggregazione per valvola, che e' cio'
        # che la dashboard mostra accanto alla Q di macchina.
        assert dal_riepilogo[3] == diretto[3]


@requires_postgres
@pytest.mark.parametrize("nome", list(_finestre()))
def test_identita_numerica_run_b(riempito, db, nome):
    """Finestra 7: le stesse forme su un run diverso, sugli stessi istanti.

    I due run si sovrappongono nel tempo di parete: se `run_id` non isolasse,
    questi conteggi sarebbero la somma dei due e il confronto fallirebbe.
    """
    f = {nome: _finestre()[nome]}
    for n, dal_riepilogo, diretto, uguali in _confronta(db, RUN_B, f):
        assert uguali, f"{n}: riepilogo {dal_riepilogo[:2]} != diretto {diretto[:2]}"


@requires_postgres
def test_i_due_run_hanno_conteggi_diversi(riempito, db):
    """Guardia del test precedente: se A e B dessero gli stessi numeri,
    l'identita' passerebbe anche con `run_id` ignorato."""
    f = {"2": _finestre()["2 disallineata di secondi"]}
    a = _confronta(db, RUN_A, f)[0][1][:2]
    b = _confronta(db, RUN_B, f)[0][1][:2]
    assert a != b, f"i due run devono essere distinguibili: {a} == {b}"


# -- riempimento ------------------------------------------------------------
@requires_postgres
def test_solo_ore_complete(riempito, db):
    """L'ora che contiene l'ultimo ciclo non entra nel riepilogo.

    E' la regola che permette di non avere una colonna "completo si'/no": un
    secchiello presente e' sempre un'ora finita.
    """
    _, ultimo = riempito.coverage(RUN_A)
    assert ultimo == floor_hour(T1) - timedelta(hours=1)


@requires_postgres
def test_idempotenza(riempito, db):
    """Riempire due volte lo stesso periodo non cambia una riga."""
    with db.connect() as conn:
        prima = conn.execute(text(
            f"SELECT run_id, bucket_ts, valve_id, total, good FROM {ROLLUP_TABLE} "
            "ORDER BY run_id, bucket_ts, valve_id")).all()
    riempito.fill(RUN_A)
    riempito.fill(RUN_B)
    with db.connect() as conn:
        dopo = conn.execute(text(
            f"SELECT run_id, bucket_ts, valve_id, total, good FROM {ROLLUP_TABLE} "
            "ORDER BY run_id, bucket_ts, valve_id")).all()
    assert prima == dopo


@requires_postgres
def test_parziale_poi_completato(db):
    """Riempire meta' periodo e poi tutto == riempire tutto in una volta.

    E' il caso operativo del riempimento incrementale: la seconda esecuzione
    non deve ne' duplicare, ne' saltare, ne' lasciare indietro l'ora di
    confine (che `--since-last` ricalcola apposta).
    """
    r = CycleRollup(db)
    r.drop_all()
    r.init()
    meta = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    r.fill(RUN_A, end=meta)
    parziale = r.rows_for(RUN_A)
    r.fill(RUN_A, since_last=True)
    with db.connect() as conn:
        incrementale = conn.execute(text(
            f"SELECT bucket_ts, valve_id, total, good FROM {ROLLUP_TABLE} "
            "WHERE run_id = :r ORDER BY bucket_ts, valve_id"), {"r": RUN_A}).all()
    r.drop_all()
    r.init()
    r.fill(RUN_A)
    with db.connect() as conn:
        unico = conn.execute(text(
            f"SELECT bucket_ts, valve_id, total, good FROM {ROLLUP_TABLE} "
            "WHERE run_id = :r ORDER BY bucket_ts, valve_id"), {"r": RUN_A}).all()
    assert parziale > 0 and parziale < len(unico)
    assert incrementale == unico
    # rimette il DB nello stato che il fixture di modulo si aspetta
    r.fill(RUN_B)


@requires_postgres
def test_run_senza_cicli_non_scrive_nulla(db):
    with pytest.raises(CycleRollupError):
        CycleRollup(db).fill("run_che_non_esiste")


# -- la route, con e senza riepilogo ----------------------------------------
@pytest.fixture
def client(db):
    """TestClient puntato al DB di prova (ripristino dell'env nel teardown)."""
    from fastapi.testclient import TestClient

    from pipeline import api
    prev = os.environ.get("PLCSIM_DATABASE_URL")
    os.environ["PLCSIM_DATABASE_URL"] = _TEST_DB_URL
    api._store = None
    try:
        Storage(make_engine(_TEST_DB_URL)).init()
        yield TestClient(api.app)
    finally:
        api._store = None
        if prev is None:
            os.environ.pop("PLCSIM_DATABASE_URL", None)
        else:
            os.environ["PLCSIM_DATABASE_URL"] = prev


def _serie(client, **q):
    r = client.get("/machine/oee/series", params=q)
    assert r.status_code == 200, r.text
    return r.json()


@requires_postgres
@pytest.mark.parametrize("window", ["hour", "shift", "day", "week", "month"])
def test_serie_identica_con_e_senza_riepilogo(db, client, window):
    """La prova che conta davvero, a livello di route.

    La stessa serie viene servita due volte: una con `cycle_rollup_hour`
    riempita e una con la tabella rimossa, cioe' esattamente il codice
    precedente a questo lavoro. Le due risposte devono essere **lo stesso
    JSON**. Se una sola cifra si muove, il riepilogo ha cambiato un numero —
    che e' l'unica cosa che non gli e' permessa.
    """
    r = CycleRollup(db)
    q = {"windows": window, "at": T1.isoformat(), "run_id": RUN_A}
    r.drop_all()
    r.init()
    senza = _serie(client, **q)
    r.fill(RUN_A)
    r.fill(RUN_B)
    con = _serie(client, **q)
    assert con["__meta"][window]["conteggi_da"].startswith("cycle_rollup_hour")
    assert senza["__meta"][window]["conteggi_da"] == "cycles"
    assert con[window] == senza[window]


@requires_postgres
def test_intervallo_esplicito(riempito, client):
    """`from`/`to`: la serie copre il periodo chiesto, non "quanto indietro
    rispetto ad adesso". Senza questo un selettore di periodo non e'
    costruibile, ed e' il motivo per cui esiste tutto il lavoro."""
    da = datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc)
    a = datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc)
    d = _serie(client, windows="shift", **{"from": da.isoformat(),
                                           "to": a.isoformat(),
                                           "run_id": RUN_A})
    ats = [p["at"] for p in d["shift"]]
    assert ats[-1] == a.isoformat()
    assert all(da <= datetime.fromisoformat(t) <= a for t in ats)
    assert d["__meta"]["intervallo_esplicito"] == {
        "from": da.isoformat(), "to": a.isoformat()}


@requires_postgres
def test_intervallo_rovesciato_rifiutato(riempito, client):
    r = client.get("/machine/oee/series", params={
        "from": T1.isoformat(), "to": T0.isoformat(), "run_id": RUN_A})
    assert r.status_code == 422


@requires_postgres
def test_passo_si_dirada_invece_di_troncare(riempito, client):
    """Un periodo largo non perde punti in fondo: cambia il passo.

    Prima di questo lavoro `SERIES_MAX_POINTS` troncava la serie in silenzio,
    e chi guardava il grafico non aveva modo di accorgersene.
    """
    from pipeline import api
    d = _serie(client, windows="hour", at=T1.isoformat(), run_id=RUN_A)
    m = d["__meta"]["hour"]
    assert m["punti"] <= api.SERIES_MAX_POINTS
    # il primo punto risale fino al primo ciclo reale, non a 200 passi indietro
    assert datetime.fromisoformat(m["primo_at"]) <= T0 + timedelta(hours=1)
    assert m["passo"] != m["passo_base"] or m["punti"] < api.SERIES_MAX_POINTS


# -- bordi d'ora ------------------------------------------------------------
def test_ceil_e_floor_su_istante_allineato():
    """Un istante gia' allineato non si sposta: e' la condizione che impedisce
    di contare due volte l'ora di confine."""
    t = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)
    assert ceil_hour(t) == t == floor_hour(t)
    t2 = t + timedelta(seconds=1)
    assert floor_hour(t2) == t and ceil_hour(t2) == t + timedelta(hours=1)
