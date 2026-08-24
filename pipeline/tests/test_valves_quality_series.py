"""Test di `GET /valves/quality/series` — qualita' per valvola nel tempo.

## Cosa decide se la route e' giusta

**L'identita' con `cycles`.** La route non legge i cicli: legge il riepilogo
orario. Il patto e' che la somma dei secchielli sia lo stesso numero che darebbe
un `GROUP BY` diretto su `cycles` nello stesso periodo — conteggi interi,
identici, non "vicini". Se quel confronto passa, il resto sono regole di
presentazione; se fallisce, la route mente.

Le altre proprieta' verificate, e perche' ognuna:

- **secchiello vuoto**: la macchina ferma per un'ora esiste, e deve uscire con
  `total: 0` e `quality: null`. Ometterlo nasconderebbe la fermata; servirlo con
  `quality: 0.0` la trasformerebbe in un'ora di soli scarti, che e' un fatto
  diverso e piu' grave.
- **le tre grane**: `day` e `week` sono somme di ore, quindi devono ridare
  esattamente il totale delle ore che contengono.
- **isolamento per run**: i due run di prova si sovrappongono nel tempo di
  parete, quindi senza filtro i conteggi sarebbero la loro somma.
- **liste allineate**: un grafico sovrappone 35 curve fidandosi che l'i-esimo
  punto di ognuna sia lo stesso istante.

## Isolamento

DB dedicato per processo (`plcsim_test_qser_<random>`, prefisso registrato in
`conftest._PREFISSI_EFFIMERI` con il teardown gia' previsto).
**Nessun test scrive sul database operazionale `plcsim`.**
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from pipeline.cycle_rollup import CycleRollup
from pipeline.cycles_storage import CyclesStorage
from pipeline.storage import Storage, make_engine

from .conftest import drop_db_if_ephemeral


def _test_db_url() -> str:
    if "PLCSIM_QSER_TEST_DATABASE_URL" in os.environ:
        return os.environ["PLCSIM_QSER_TEST_DATABASE_URL"]
    url = (f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
           f"plcsim_test_qser_{secrets.token_hex(4)}")
    os.environ["PLCSIM_QSER_TEST_DATABASE_URL"] = url
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
RUN_A = "qser_test_a"
RUN_B = "qser_test_b"
VALVOLE = (1, 2, 3)
# Due settimane intere piu' un pezzo: serve per provare `week` senza che la
# grana coincida col periodo (un solo secchiello non proverebbe l'aggregazione).
T0 = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)          # lunedi'
T1 = datetime(2026, 7, 18, 9, 20, 37, tzinfo=timezone.utc)
# Fermata di tre ore intere: sono i secchielli vuoti che devono uscire con
# `total: 0` e `quality: null`.
FERMO = (datetime(2026, 7, 8, 3, 0, tzinfo=timezone.utc),
         datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc))
PASSO_CICLI = timedelta(minutes=6)


def _cicli(run_id: str, sfasamento: timedelta, passo: timedelta,
           valvole: tuple[int, ...]) -> list[dict]:
    """Cicli deterministici, con la valvola 3 volutamente peggiore.

    Qualita' diversa per valvola: se tutte avessero lo stesso rapporto, un bug
    che mescolasse le valvole passerebbe inosservato.
    """
    righe = []
    t = T0 + sfasamento
    cid = 0
    while t < T1:
        if not (FERMO[0] <= t < FERMO[1]):
            for v in valvole:
                cid += 1
                ok = (cid % 5 != 0) if v != 3 else (cid % 2 == 0)
                righe.append({
                    "run_id": run_id, "machine_id": "filler01",
                    "cycle_id": cid, "valve_id": v,
                    "event_ts": t, "fill_quality_ok": ok,
                })
        t += passo
    return righe


@pytest.fixture(scope="module")
def db():
    engine = make_engine(_TEST_DB_URL)
    cs = CyclesStorage(engine)
    cs.drop_all()
    cs.init()
    cs.bulk_insert(_cicli(RUN_A, timedelta(0), PASSO_CICLI, VALVOLE))
    cs.bulk_insert(_cicli(RUN_B, timedelta(minutes=2),
                          timedelta(minutes=10), VALVOLE[:2]))
    r = CycleRollup(engine)
    r.drop_all()
    r.init()
    r.fill(RUN_A)
    r.fill(RUN_B)
    yield engine
    cs.drop_all()
    r.drop_all()


@pytest.fixture(scope="module")
def client(db):
    """TestClient con lo storage della route puntato al DB effimero.

    Si scrive `api._store` invece di cambiare `PLCSIM_DATABASE_URL`: l'engine
    dell'API e' un singleton costruito alla prima richiesta, e una variabile
    d'ambiente arrivata dopo non lo sposterebbe piu'. Il valore precedente
    viene rimesso a posto, cosi' l'ordine dei file di test non conta.
    """
    from pipeline import api
    prima = api._store
    api._store = Storage(db)
    yield TestClient(api.app)
    api._store = prima


def _chiama(client, **kw) -> dict:
    p = {"from": T0.isoformat(), "to": T1.isoformat(),
         "grain": "day", "run_id": RUN_A}
    p.update(kw)
    r = client.get("/valves/quality/series", params=p)
    assert r.status_code == 200, r.text
    return r.json()


def _diretto(db, run: str, lo: datetime, hi: datetime) -> dict[int, tuple[int, int]]:
    """`{valve_id: (total, good)}` letto da `cycles`, senza il riepilogo."""
    with db.connect() as c:
        rows = c.execute(text(
            "SELECT valve_id, COUNT(*), "
            "  COUNT(*) FILTER (WHERE fill_quality_ok = TRUE) "
            "FROM cycles WHERE run_id = :r AND event_ts >= :lo AND event_ts < :hi "
            "GROUP BY valve_id"), {"r": run, "lo": lo, "hi": hi}).all()
    return {int(v): (int(t), int(g)) for v, t, g in rows}


# -- identita' --------------------------------------------------------------
@requires_postgres
@pytest.mark.parametrize("grain", ["hour", "day", "week"])
def test_identita_con_cycles(client, db, grain):
    """La somma dei secchielli == il `GROUP BY` diretto sullo stesso periodo.

    Il confronto usa il periodo EFFETTIVO dichiarato dalla risposta (`from`/`to`
    allineati ai bordi), non quello chiesto: confrontare intervalli diversi
    darebbe una differenza vera scambiata per un bug, o peggio la nasconderebbe.
    """
    j = _chiama(client, grain=grain)
    lo = datetime.fromisoformat(j["from"])
    hi = datetime.fromisoformat(j["to"])
    atteso = _diretto(db, RUN_A, lo, hi)
    for v, serie in j["valves"].items():
        tot = sum(p["total"] for p in serie)
        good = sum(p["good"] for p in serie)
        assert (tot, good) == atteso[int(v)], f"valvola {v}, grana {grain}"
    assert set(map(int, j["valves"])) == set(atteso)


@requires_postgres
def test_le_tre_grane_danno_lo_stesso_totale(client):
    """`day` e `week` sono somme di ore: nessun ciclo si perde per strada.

    Il periodo e' una settimana ESATTA a partire da un lunedi': con un periodo
    qualsiasi le tre grane lo allineerebbero verso l'esterno in modo diverso e
    starebbero legittimamente misurando intervalli diversi.
    """
    fine = (T0 + timedelta(days=7)).isoformat()
    tot = {}
    for grain in ("hour", "day", "week"):
        j = _chiama(client, grain=grain, to=fine)
        assert j["grain"] == grain, "nessuna promozione attesa qui"
        tot[grain] = {v: (sum(p["total"] for p in s), sum(p["good"] for p in s))
                      for v, s in j["valves"].items()}
    assert tot["hour"] == tot["day"] == tot["week"]


# -- il buco e' un fatto ----------------------------------------------------
@requires_postgres
def test_ora_vuota_esce_con_total_zero_e_quality_null(client):
    """Le tre ore di fermata ci sono, con `total: 0` e `quality: null`.

    Non `quality: 0.0`: zero cicli significa "non misurata", non "tutti
    scarti" — la stessa regola di `_compute_oee_window`.
    """
    j = _chiama(client, grain="hour",
                **{"from": (FERMO[0] - timedelta(hours=2)).isoformat(),
                   "to": (FERMO[1] + timedelta(hours=2)).isoformat()})
    serie = j["valves"]["1"]
    vuoti = [p for p in serie
             if FERMO[0] <= datetime.fromisoformat(p["at"]) < FERMO[1]]
    assert len(vuoti) == 3, [p["at"] for p in serie]
    for p in vuoti:
        assert p["total"] == 0 and p["good"] == 0
        assert p["quality"] is None
    # ...e le ore intorno sono piene: senza questo, un periodo servito tutto a
    # zero passerebbe il test qui sopra.
    assert all(p["total"] > 0 for p in serie
               if not (FERMO[0] <= datetime.fromisoformat(p["at"]) < FERMO[1]))


@requires_postgres
def test_secchielli_contigui_e_allineati_fra_valvole(client):
    """Stessa lista di istanti per tutte le valvole, passo costante.

    E' cio' che permette a un grafico di sovrapporre 35 curve senza indovinare
    a quale istante corrisponde l'i-esimo punto.
    """
    j = _chiama(client, grain="hour",
                to=(T0 + timedelta(days=2)).isoformat())
    assert j["grain"] == "hour"
    liste = {v: [p["at"] for p in s] for v, s in j["valves"].items()}
    prima = next(iter(liste.values()))
    assert all(l == prima for l in liste.values())
    ts = [datetime.fromisoformat(x) for x in prima]
    assert all(b - a == timedelta(hours=1) for a, b in zip(ts, ts[1:]))
    assert ts[0] == datetime.fromisoformat(j["from"])


@requires_postgres
def test_quality_e_good_su_total_a_tre_decimali(client):
    j = _chiama(client, grain="day")
    for serie in j["valves"].values():
        for p in serie:
            if p["total"]:
                assert p["quality"] == round(p["good"] / p["total"], 3)


# -- isolamento per run -----------------------------------------------------
@requires_postgres
def test_run_isola_i_conteggi(client, db):
    """I due run si sovrappongono nel tempo: senza filtro sarebbero sommati."""
    a = _chiama(client, run_id=RUN_A, grain="day")
    b = _chiama(client, run_id=RUN_B, grain="day")
    assert a["run_id"] == RUN_A and b["run_id"] == RUN_B
    # B ha due valvole invece di tre: gia' questo cadrebbe se i run si
    # mescolassero.
    assert set(b["valves"]) == {"1", "2"}
    tot_a = sum(p["total"] for p in a["valves"]["1"])
    tot_b = sum(p["total"] for p in b["valves"]["1"])
    assert tot_a != tot_b
    assert tot_b == _diretto(db, RUN_B,
                             datetime.fromisoformat(b["from"]),
                             datetime.fromisoformat(b["to"]))[1][0]


# -- bordi e degrado --------------------------------------------------------
@requires_postgres
def test_periodo_allineato_ai_bordi_del_secchiello(client):
    """`from`/`to` in risposta sono quelli effettivi, allineati verso l'esterno."""
    j = _chiama(client, grain="day",
                **{"from": "2026-07-07T05:31:00+00:00",
                   "to": "2026-07-09T13:02:00+00:00"})
    assert j["from"] == "2026-07-07T00:00:00+00:00"
    assert j["to"] == "2026-07-10T00:00:00+00:00"
    assert len(j["valves"]["1"]) == 3


@requires_postgres
def test_la_coda_non_riassunta_si_legge_da_cycles(client, db):
    """L'ora in corso non e' nel riepilogo: si legge da `cycles`, non si taglia.

    Prima di questo lavoro il periodo si fermava all'ultima ora INTERAMENTE
    riassunta, e la serie per valvola finiva prima di quella di macchina —
    `/machine/oee` i suoi bordi parziali li leggeva gia' da `cycles`. Qui si
    verifica la stessa cosa e con lo stesso metro: identita' cifra per cifra
    con il `GROUP BY` diretto, coda compresa.
    """
    ultimo = max(r["event_ts"] for r in _cicli(RUN_A, timedelta(0),
                                               PASSO_CICLI, VALVOLE))
    j = _chiama(client, grain="day",
                **{"to": (T1 + timedelta(days=2)).isoformat()})
    # oltre l'ultimo ciclo non c'e' nulla: il periodo si ferma li', e non e'
    # un degrado — `MAX(event_ts)` lo sa con certezza.
    hi = datetime.fromisoformat(j["to"])
    assert hi == (ultimo + timedelta(seconds=1)).replace(
        hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    assert j["degraded"] is False and j["reason"] is None
    # l'ultimo secchiello contiene i cicli dell'ora non riassunta
    atteso = _diretto(db, RUN_A, datetime.fromisoformat(j["from"]), hi)
    for v, serie in j["valves"].items():
        tot = sum(p["total"] for p in serie)
        good = sum(p["good"] for p in serie)
        assert (tot, good) == atteso[int(v)], f"valvola {v}"
    ultima_ora = _diretto(db, RUN_A, ultimo.replace(minute=0, second=0),
                          ultimo + timedelta(seconds=1))
    assert ultima_ora and all(t > 0 for t, _ in ultima_ora.values())


@requires_postgres
def test_riepilogo_molto_indietro_taglia_e_si_dichiara(db, client):
    """Se la coda da leggere e' enorme si torna al taglio, dichiarandolo.

    Leggere giorni di `cycles` costerebbe una scansione: li' il taglio esplicito
    e' preferibile a una risposta lenta. Il riepilogo di RUN_B viene svuotato
    negli ultimi giorni e poi rifatto, cosi' gli altri test non ne risentono.
    """
    from pipeline.api import CODA_DIRETTA_MAX
    taglio = T1 - 3 * CODA_DIRETTA_MAX
    with db.begin() as c:
        c.execute(text("DELETE FROM cycle_rollup_hour "
                       "WHERE run_id = :r AND bucket_ts >= :t"),
                  {"r": RUN_B, "t": taglio})
    try:
        j = _chiama(client, grain="day", run_id=RUN_B)
        assert j["degraded"] is True
        assert "riepilogo e' indietro" in j["reason"]
        assert datetime.fromisoformat(j["to"]) <= taglio
    finally:
        r = CycleRollup(db)
        r.fill(RUN_B)


@requires_postgres
def test_grana_promossa_invece_che_troncata(client):
    """Oltre il tetto di punti si dirada la grana; il periodo resta intero."""
    from pipeline.api import SERIES_MAX_POINTS
    j = _chiama(client, grain="hour")           # ~12 giorni = ~295 ore
    assert j["grain"] == "day"
    assert "grana promossa" in (j["reason"] or "")
    assert len(j["valves"]["1"]) <= SERIES_MAX_POINTS
    # il periodo NON e' stato accorciato
    assert datetime.fromisoformat(j["from"]) == T0


@requires_postgres
def test_run_inesistente_degrada_con_motivo(client):
    j = _chiama(client, run_id="run_che_non_esiste")
    assert j["degraded"] is True and j["valves"] == {}
    assert "nessuna ora riassunta" in j["reason"]


@requires_postgres
def test_intervallo_vuoto_e_422(client):
    r = client.get("/valves/quality/series", params={
        "from": T1.isoformat(), "to": T0.isoformat(), "run_id": RUN_A})
    assert r.status_code == 422


@requires_postgres
def test_grana_ignota_e_422(client):
    r = client.get("/valves/quality/series", params={
        "from": T0.isoformat(), "to": T1.isoformat(), "grain": "mese",
        "run_id": RUN_A})
    assert r.status_code == 422
