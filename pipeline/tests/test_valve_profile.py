"""Test di `GET /valves/{id}/profile` e delle colonne di profilo del riepilogo.

## Cosa decide se questo lavoro e' giusto

**L'identita' delle medie.** La route non legge i cicli uno per uno: legge somme
e conteggi precalcolati. Il patto e' che la media che ne esce sia la stessa che
darebbe un `AVG` diretto su `cycles` nello stesso periodo, coda parziale
compresa. Se quel confronto passa, il resto sono regole di presentazione; se
fallisce, la route mente con tre cifre di precisione.

Le altre proprieta' verificate, e perche' ognuna:

- **una colonna con valori NULL**: le colonne KPI sono nullable (cicli parziali,
  policy T6). Il conteggio deve essere per colonna (`COUNT(colonna)`) e non
  `total`: dividere per `total` darebbe una media piu' bassa del vero, e il
  dato di prova e' costruito perche' i due numeri siano diversi. Se fossero
  uguali il test non proverebbe nulla.
- **periodo senza cicli**: `media: null` e `n: 0`, mai `0.0`. Zero cicli
  significa "non misurata", non "riempimento istantaneo".
- **isolamento per run**: i due run di prova si sovrappongono nel tempo di
  parete e hanno medie DIVERSE; senza filtro la media sarebbe la loro mistura,
  che e' un numero plausibile e sbagliato.
- **migrazione su tabella gia' popolata**: e' il caso vero (34.090 righe). Le
  colonne si aggiungono, le righe vecchie restano a NULL, e chi legge lo
  dichiara invece di servire una media su una parte del periodo. Eseguita due
  volte, la migrazione non e' distinguibile da una volta sola.

## Isolamento

DB dedicato per processo (`plcsim_test_prof_<random>`, prefisso registrato in
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

from pipeline.cycle_rollup import (PROFILE_METRICS, ROLLUP_TABLE, CycleRollup,
                                   n_col, sum_col)
from pipeline.cycles_storage import CyclesStorage
from pipeline.storage import Storage, make_engine

from .conftest import drop_db_if_ephemeral


def _test_db_url() -> str:
    if "PLCSIM_PROF_TEST_DATABASE_URL" in os.environ:
        return os.environ["PLCSIM_PROF_TEST_DATABASE_URL"]
    url = (f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
           f"plcsim_test_prof_{secrets.token_hex(4)}")
    os.environ["PLCSIM_PROF_TEST_DATABASE_URL"] = url
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
RUN_A = "prof_test_a"
RUN_B = "prof_test_b"
VALVOLE = (1, 2)
T0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
# Fine volutamente NON allineata all'ora: l'ultima ora resta incompleta, quindi
# il riepilogo non la contiene e la route deve leggerla da `cycles`. E' il caso
# che distingue una route agganciata al riepilogo da una che taglia in silenzio.
T1 = datetime(2026, 7, 3, 5, 37, 12, tzinfo=timezone.utc)
# Ore senza alcun ciclo: servono al test del periodo vuoto.
FERMO = (datetime(2026, 7, 2, 2, 0, tzinfo=timezone.utc),
         datetime(2026, 7, 2, 6, 0, tzinfo=timezone.utc))
PASSO = timedelta(minutes=2)


def _cicli(run_id: str, sfasamento: timedelta, passo: timedelta,
           base_riemp: int) -> list[dict]:
    """Cicli deterministici, con `tail_pulse` NULL un ciclo su tre.

    Il buco in `tail_pulse` non e' decorativo: e' la prova che il conteggio e'
    per colonna. Un ciclo su tre significa che `n_tail_pulse` e `total`
    differiscono di circa un terzo, cioe' abbastanza perche' una media
    calcolata su `total` sbagli in modo visibile.

    I valori variano per valvola e per ciclo: se fossero costanti, una media
    sbagliata darebbe lo stesso numero di una giusta.
    """
    righe = []
    t = T0 + sfasamento
    cid = 0
    while t < T1:
        if not (FERMO[0] <= t < FERMO[1]):
            for v in VALVOLE:
                cid += 1
                righe.append({
                    "run_id": run_id, "machine_id": "filler01",
                    "cycle_id": cid, "valve_id": v, "event_ts": t,
                    "fill_quality_ok": (cid % 5 != 0),
                    "filling_time_ms": base_riemp + 10 * v + (cid % 7),
                    "tail_time_ms": 300 + (cid % 11),
                    # NULL un ciclo su tre: cicli parziali (policy T6).
                    "tail_pulse": None if cid % 3 == 0 else 200 + (cid % 13),
                    "pulse_count": 2500 - (cid % 17),
                    "delta_pulse": -5 + (cid % 9),
                    "filling_step_out": cid % 4,
                })
        t += passo
    return righe


@pytest.fixture(scope="module")
def db():
    engine = make_engine(_TEST_DB_URL)
    cs = CyclesStorage(engine)
    cs.drop_all()
    cs.init()
    cs.bulk_insert(_cicli(RUN_A, timedelta(0), PASSO, 1900))
    # Run B materialmente diverso: cadenza e livello di riempimento altri,
    # altrimenti l'isolamento passerebbe anche ignorando `run_id`.
    cs.bulk_insert(_cicli(RUN_B, timedelta(minutes=1), timedelta(minutes=3), 2100))
    # `machine_state` serve al KV `baseline_window` (test della base).
    Storage(engine).init()
    r = CycleRollup(engine)
    r.drop_all()
    r.init()
    r.fill(RUN_A)
    r.fill(RUN_B)
    yield engine
    cs.drop_all()
    r.drop_all()


@pytest.fixture()
def client(db, monkeypatch):
    """TestClient con lo storage puntato al DB effimero (mai `plcsim`)."""
    from pipeline import api as api_mod
    st = Storage(db)
    monkeypatch.setattr(api_mod, "_store", st, raising=False)
    monkeypatch.setattr(api_mod, "_storage", lambda: st)
    return TestClient(api_mod.app)


def _q(t: datetime) -> str:
    """ISO8601 pronto per la query string.

    Il `+` del fuso e' un carattere riservato nell'URL: senza escape arriva al
    server come spazio e la data non si legge (422). Non e' un dettaglio del
    test — e' come chiunque deve passare questi parametri.
    """
    return t.isoformat().replace("+", "%2B")


def _media_diretta(engine, run: str, valve_id: int, metrica: str,
                   lo: datetime, hi: datetime) -> tuple[float | None, int]:
    """La verita' di riferimento: `AVG`/`COUNT` letti da `cycles`."""
    with engine.connect() as conn:
        row = conn.execute(text(
            f"SELECT AVG({metrica}::numeric), COUNT({metrica}) FROM cycles "
            "WHERE run_id = :r AND valve_id = :v "
            "AND event_ts >= :lo AND event_ts < :hi"),
            {"r": run, "v": valve_id, "lo": lo, "hi": hi}).first()
    return (float(row[0]) if row[0] is not None else None, int(row[1]))


# -- identita' --------------------------------------------------------------
@requires_postgres
def test_medie_identiche_a_cycles_coda_compresa(client, db):
    """Le sei medie coincidono con `AVG` su `cycles`, coda parziale inclusa.

    Il periodo chiesto sborda oltre l'ultima ora completa, quindi la risposta
    contiene per forza ore intere dal riepilogo PIU' la coda letta da `cycles`.
    Se la route tagliasse alla fine del riepilogo, `n` sarebbe piu' piccolo del
    conteggio diretto e il confronto fallirebbe.
    """
    r = client.get(f"/valves/1/profile?from={_q(T0)}"
                   f"&to={_q(T1 + timedelta(hours=2))}&run_id={RUN_A}")
    assert r.status_code == 200, r.text
    body = r.json()
    lo = datetime.fromisoformat(body["from"])
    hi = datetime.fromisoformat(body["to"])
    for m in PROFILE_METRICS:
        atteso, n = _media_diretta(db, RUN_A, 1, m, lo, hi)
        assert body["periodo"][m]["n"] == n, m
        assert body["periodo"][m]["media"] == pytest.approx(round(atteso, 1)), m
    # la coda c'e' davvero: l'ultima ora non e' completa
    assert n > 0


@requires_postgres
def test_conteggio_per_colonna_non_total(client, db):
    """`n_tail_pulse` e' minore di `n_pulse_count`, e la media lo riflette.

    Se la media fosse `somma / total`, `tail_pulse` uscirebbe circa un terzo
    piu' bassa del vero. Il test confronta contro `AVG` diretto, quindi
    quell'errore non passa.
    """
    r = client.get(f"/valves/1/profile?from={_q(T0)}"
                   f"&to={_q(T1)}&run_id={RUN_A}")
    p = r.json()["periodo"]
    assert p["tail_pulse"]["n"] < p["pulse_count"]["n"]
    atteso, n = _media_diretta(db, RUN_A, 1, "tail_pulse",
                               datetime.fromisoformat(r.json()["from"]),
                               datetime.fromisoformat(r.json()["to"]))
    assert p["tail_pulse"]["n"] == n
    assert p["tail_pulse"]["media"] == pytest.approx(round(atteso, 1))
    # la prova che il dato di prova distingue i due calcoli
    assert abs(p["tail_pulse"]["media"] * n / p["pulse_count"]["n"]
               - p["tail_pulse"]["media"]) > 1


@requires_postgres
def test_periodo_senza_cicli_da_media_null(client):
    """Ore di fermata: `media: null` e `n: 0`, mai zero."""
    r = client.get(f"/valves/1/profile?from={_q(FERMO[0])}"
                   f"&to={_q(FERMO[1])}&run_id={RUN_A}")
    assert r.status_code == 200, r.text
    p = r.json()["periodo"]
    for m in PROFILE_METRICS:
        assert p[m]["n"] == 0, m
        assert p[m]["media"] is None, m


@requires_postgres
def test_isolamento_per_run(client, db):
    """Stessa valvola, stesso periodo, due run: due medie diverse ed esatte."""
    q = f"from={_q(T0)}&to={_q(FERMO[0])}"
    a = client.get(f"/valves/1/profile?{q}&run_id={RUN_A}").json()
    b = client.get(f"/valves/1/profile?{q}&run_id={RUN_B}").json()
    ma = a["periodo"]["filling_time_ms"]["media"]
    mb = b["periodo"]["filling_time_ms"]["media"]
    assert ma is not None and mb is not None
    assert abs(ma - mb) > 100, "i due run devono essere distinguibili"
    for corpo, run in ((a, RUN_A), (b, RUN_B)):
        atteso, n = _media_diretta(db, run, 1, "filling_time_ms",
                                   datetime.fromisoformat(corpo["from"]),
                                   datetime.fromisoformat(corpo["to"]))
        assert corpo["periodo"]["filling_time_ms"]["n"] == n
        assert corpo["periodo"]["filling_time_ms"]["media"] == pytest.approx(
            round(atteso, 1))


@requires_postgres
def test_valvole_diverse_profili_diversi(client, db):
    """Il filtro per valvola c'e': le due valvole hanno riempimenti diversi."""
    q = f"from={_q(T0)}&to={_q(FERMO[0])}&run_id={RUN_A}"
    v1 = client.get(f"/valves/1/profile?{q}").json()
    v2 = client.get(f"/valves/2/profile?{q}").json()
    assert (v1["periodo"]["filling_time_ms"]["media"]
            != v2["periodo"]["filling_time_ms"]["media"])


@requires_postgres
def test_bordi_allineati_e_restituiti_effettivi(client):
    """`from`/`to` in risposta sono i bordi del secchiello, non quelli chiesti."""
    da = T0 + timedelta(minutes=17)
    a = T0 + timedelta(hours=3, minutes=41)
    r = client.get(f"/valves/1/profile?from={_q(da)}"
                   f"&to={_q(a)}&run_id={RUN_A}").json()
    assert datetime.fromisoformat(r["from"]) == T0
    assert datetime.fromisoformat(r["to"]) == T0 + timedelta(hours=4)


@requires_postgres
def test_base_dalla_finestra_sana_kv(client, db):
    """`base` e' la STESSA valvola sulla finestra dichiarata nel KV."""
    st = Storage(db)
    b_start = T0
    b_end = T0 + timedelta(hours=6)
    st.set_machine_state("baseline_window", {
        "run_id": RUN_A, "start": b_start.isoformat(), "end": b_end.isoformat()})
    try:
        r = client.get(f"/valves/1/profile?from={_q(T0)}"
                       f"&to={_q(T1)}&run_id={RUN_A}").json()
        assert r["base"] is not None, r["reason"]
        assert set(r["base"]) == set(r["periodo"]), "stessa forma di `periodo`"
        atteso, n = _media_diretta(db, RUN_A, 1, "filling_time_ms",
                                   b_start, b_end)
        assert r["base"]["filling_time_ms"]["n"] == n
        assert r["base"]["filling_time_ms"]["media"] == pytest.approx(
            round(atteso, 1))
        assert datetime.fromisoformat(r["base_from"]) == b_start
    finally:
        st.set_machine_state("baseline_window", None)


@requires_postgres
def test_base_con_bordi_non_allineati(client, db):
    """La finestra sana non e' allineata all'ora: i bordi vengono da `cycles`.

    E' il caso vero — la finestra sana del run `storico_60d` comincia alle
    04:08:38. Il secchiello che contiene quel bordo porta anche i cicli PRIMA
    del bordo: prenderlo intero gonfierebbe il conteggio, scartarlo perderebbe
    51 minuti in silenzio. Il confronto con `AVG` diretto scopre entrambi gli
    errori.
    """
    st = Storage(db)
    b_start = T0 + timedelta(hours=1, minutes=8, seconds=38)
    b_end = T0 + timedelta(hours=9, minutes=41, seconds=5)
    st.set_machine_state("baseline_window", {
        "run_id": RUN_A, "start": b_start.isoformat(), "end": b_end.isoformat()})
    try:
        r = client.get(f"/valves/2/profile?from={_q(T0)}"
                       f"&to={_q(T1)}&run_id={RUN_A}").json()
        assert r["base"] is not None, r["reason"]
        for m in PROFILE_METRICS:
            atteso, n = _media_diretta(db, RUN_A, 2, m, b_start, b_end)
            assert r["base"][m]["n"] == n, m
            assert r["base"][m]["media"] == pytest.approx(round(atteso, 1)), m
        assert n > 0
    finally:
        st.set_machine_state("baseline_window", None)


@requires_postgres
def test_base_null_con_motivo_se_non_dichiarata(client, db):
    """Senza KV la base non si inventa: `null`, motivo scritto, periodo servito."""
    Storage(db).set_machine_state("baseline_window", None)
    r = client.get(f"/valves/1/profile?from={_q(T0)}"
                   f"&to={_q(T1)}&run_id={RUN_A}").json()
    assert r["base"] is None
    assert r["degraded"] is True
    assert "base non disponibile" in r["reason"]
    assert r["periodo"]["filling_time_ms"]["media"] is not None


@requires_postgres
def test_periodo_che_comincia_prima_del_run_non_degrada(client, db):
    """Chiedere da prima dell'inizio del run non costa e non degrada.

    Prima della copertura del riepilogo il run non ha alcun ciclo: quel tratto
    e' vuoto per un fatto, non per ignoranza, e leggerlo da `cycles` e' una
    scansione di indice su zero righe. Il tetto sul letto diretto pesa le righe,
    non il tempo di parete — misurato sul run vero, un periodo di 60 giorni
    chiesto da mezzanotte invece che dalle 04:08 veniva rifiutato.
    """
    da = T0 - timedelta(days=2)
    r = client.get(f"/valves/1/profile?from={_q(da)}"
                   f"&to={_q(FERMO[0])}&run_id={RUN_A}").json()
    assert r["periodo"]["filling_time_ms"]["media"] is not None, r["reason"]
    assert datetime.fromisoformat(r["from"]) == da
    atteso, n = _media_diretta(db, RUN_A, 1, "filling_time_ms", da,
                               datetime.fromisoformat(r["to"]))
    assert r["periodo"]["filling_time_ms"]["n"] == n
    assert r["periodo"]["filling_time_ms"]["media"] == pytest.approx(
        round(atteso, 1))


@requires_postgres
def test_valvola_fuori_range(client):
    assert client.get(
        f"/valves/99/profile?from={_q(T0)}&to={_q(T1)}"
    ).status_code == 404


@requires_postgres
def test_intervallo_vuoto_422(client):
    assert client.get(
        f"/valves/1/profile?from={_q(T1)}&to={_q(T0)}"
    ).status_code == 422


# -- migrazione -------------------------------------------------------------
@requires_postgres
def test_migrazione_su_tabella_popolata_due_volte(db):
    """Colonne tolte e rimesse su righe esistenti; la seconda passata e' un no-op.

    Riproduce il caso vero: il riepilogo esiste gia' con i suoi conteggi, e la
    migrazione deve aggiungere le colonne senza toccare `total`/`good` e senza
    perdere righe. Le righe vecchie restano a NULL finche' `fill()` non le
    ricalcola — e quel NULL deve poter essere distinto da uno zero.
    """
    r = CycleRollup(db)
    with db.begin() as conn:
        prima = conn.execute(text(
            f"SELECT COUNT(*), SUM(total), SUM(good) FROM {ROLLUP_TABLE}")).first()
        for m in PROFILE_METRICS:
            conn.execute(text(
                f"ALTER TABLE {ROLLUP_TABLE} DROP COLUMN IF EXISTS {sum_col(m)}"))
            conn.execute(text(
                f"ALTER TABLE {ROLLUP_TABLE} DROP COLUMN IF EXISTS {n_col(m)}"))

    aggiunte = r.migrate()
    assert len(aggiunte) == 2 * len(PROFILE_METRICS)
    # seconda esecuzione: nessuna colonna aggiunta, nessun errore
    assert r.migrate() == []

    with db.connect() as conn:
        dopo = conn.execute(text(
            f"SELECT COUNT(*), SUM(total), SUM(good) FROM {ROLLUP_TABLE}")).first()
        nulle = conn.execute(text(
            f"SELECT COUNT(*) FROM {ROLLUP_TABLE} "
            f"WHERE {n_col(PROFILE_METRICS[0])} IS NULL")).scalar_one()
    assert tuple(dopo) == tuple(prima), "la migrazione non tocca i conteggi"
    assert nulle == prima[0], "le righe preesistenti restano da ricalcolare"

    # e il riempimento le completa (idempotente, ON CONFLICT DO UPDATE)
    r.fill(RUN_A)
    r.fill(RUN_B)
    with db.connect() as conn:
        residue = conn.execute(text(
            f"SELECT COUNT(*) FROM {ROLLUP_TABLE} "
            f"WHERE {n_col(PROFILE_METRICS[0])} IS NULL")).scalar_one()
    assert residue == 0


@requires_postgres
def test_ore_non_migrate_degradano_invece_di_mentire(client, db):
    """Una riga di profilo a NULL non diventa uno zero: la route lo dichiara.

    E' il caso in cui il riepilogo e' stato riempito prima della migrazione. Una
    `SUM` che ignora i NULL darebbe una media su una parte del periodo, senza
    che nulla lo segnali: la route deve invece rifiutarsi e dire quale comando
    ricalcola quelle ore.
    """
    ora = T0 + timedelta(hours=1)
    with db.begin() as conn:
        conn.execute(text(
            f"UPDATE {ROLLUP_TABLE} SET "
            + ", ".join(f"{sum_col(m)} = NULL, {n_col(m)} = NULL"
                        for m in PROFILE_METRICS)
            + " WHERE run_id = :r AND bucket_ts = :b"),
            {"r": RUN_A, "b": ora})
    try:
        r = client.get(f"/valves/1/profile?from={_q(T0)}"
                       f"&to={_q(T0 + timedelta(hours=4))}"
                       f"&run_id={RUN_A}").json()
        assert r["degraded"] is True
        assert "prima che il riepilogo avesse le colonne di profilo" in r["reason"]
    finally:
        CycleRollup(db).fill(RUN_A)


# -- GET /valves/profile: la stessa lettura, tutte le valvole ---------------
# La route per una valvola sola non poteva rispondere alla domanda «quale
# valvola riempie piu' lentamente»: servivano 35 chiamate. Cio' che questi test
# devono provare non e' che la route esista, e' che dia gli STESSI numeri —
# altrimenti in giro ci sono due verita' sul tempo di riempimento.


@requires_postgres
def test_tutte_medie_identiche_a_cycles(client, db):
    """Per ogni valvola e ogni grandezza, la media coincide con `AVG` diretto.

    Periodo che sborda oltre l'ultima ora completa: la risposta e' per forza
    ore intere dal riepilogo piu' la coda letta da `cycles`.
    """
    r = client.get(f"/valves/profile?from={_q(T0)}"
                   f"&to={_q(T1 + timedelta(hours=2))}&run_id={RUN_A}")
    assert r.status_code == 200, r.text
    body = r.json()
    lo = datetime.fromisoformat(body["from"])
    hi = datetime.fromisoformat(body["to"])
    assert set(body["valves"]) == {str(v) for v in VALVOLE}
    for v in VALVOLE:
        p = body["valves"][str(v)]["periodo"]
        assert set(p) == set(PROFILE_METRICS), v
        for m in PROFILE_METRICS:
            atteso, n = _media_diretta(db, RUN_A, v, m, lo, hi)
            assert p[m]["n"] == n, (v, m)
            assert p[m]["media"] == pytest.approx(round(atteso, 1)), (v, m)
    assert n > 0


@requires_postgres
def test_coerenza_con_la_route_per_una_valvola(client):
    """Stessa valvola, stesso periodo: le due route danno numeri IDENTICI.

    E' la proprieta' che giustifica di avere due route invece di una. Se
    divergessero, chi disegna sceglierebbe la verita' in base a quale chiamata
    gli e' comoda.
    """
    q = f"from={_q(T0)}&to={_q(T1)}&run_id={RUN_A}"
    tutte = client.get(f"/valves/profile?{q}").json()
    for v in VALVOLE:
        una = client.get(f"/valves/{v}/profile?{q}").json()
        assert tutte["valves"][str(v)]["periodo"] == una["periodo"], v
        assert tutte["valves"][str(v)]["base"] == una["base"], v
    for k in ("run_id", "from", "to", "base_from", "base_to", "degraded"):
        assert tutte[k] == una[k], k


@requires_postgres
def test_conteggio_per_colonna_anche_qui(client, db):
    """`tail_pulse` ha un `n` piu' piccolo delle altre, e la media lo riflette.

    La colonna e' NULL un ciclo su tre: se il conteggio fosse `total` la media
    uscirebbe circa un terzo piu' bassa.
    """
    r = client.get(f"/valves/profile?from={_q(T0)}"
                   f"&to={_q(T1)}&run_id={RUN_A}").json()
    p = r["valves"]["1"]["periodo"]
    assert p["tail_pulse"]["n"] < p["pulse_count"]["n"]
    atteso, n = _media_diretta(db, RUN_A, 1, "tail_pulse",
                               datetime.fromisoformat(r["from"]),
                               datetime.fromisoformat(r["to"]))
    assert p["tail_pulse"]["n"] == n
    assert p["tail_pulse"]["media"] == pytest.approx(round(atteso, 1))


@requires_postgres
def test_periodo_senza_cicli_nessuna_valvola_inventata(client):
    """Ore di fermata: `valves` vuoto, non 35 valvole con medie a zero."""
    r = client.get(f"/valves/profile?from={_q(FERMO[0])}"
                   f"&to={_q(FERMO[1])}&run_id={RUN_A}")
    assert r.status_code == 200, r.text
    assert r.json()["valves"] == {}


@requires_postgres
def test_isolamento_per_run_tutte(client, db):
    """Due run sovrapposti nel tempo: due mappe diverse, entrambe esatte."""
    q = f"from={_q(T0)}&to={_q(FERMO[0])}"
    a = client.get(f"/valves/profile?{q}&run_id={RUN_A}").json()
    b = client.get(f"/valves/profile?{q}&run_id={RUN_B}").json()
    ma = a["valves"]["1"]["periodo"]["filling_time_ms"]["media"]
    mb = b["valves"]["1"]["periodo"]["filling_time_ms"]["media"]
    assert ma is not None and mb is not None
    assert abs(ma - mb) > 100, "i due run devono essere distinguibili"
    for corpo, run in ((a, RUN_A), (b, RUN_B)):
        for v in VALVOLE:
            atteso, n = _media_diretta(
                db, run, v, "filling_time_ms",
                datetime.fromisoformat(corpo["from"]),
                datetime.fromisoformat(corpo["to"]))
            got = corpo["valves"][str(v)]["periodo"]["filling_time_ms"]
            assert got["n"] == n, (run, v)
            assert got["media"] == pytest.approx(round(atteso, 1)), (run, v)


@requires_postgres
def test_base_dalla_finestra_sana_per_tutte(client, db):
    """`base` e' ogni valvola contro SE STESSA sulla finestra dichiarata."""
    st = Storage(db)
    b_start = T0 + timedelta(hours=1, minutes=8, seconds=38)
    b_end = T0 + timedelta(hours=9, minutes=41, seconds=5)
    st.set_machine_state("baseline_window", {
        "run_id": RUN_A, "start": b_start.isoformat(), "end": b_end.isoformat()})
    try:
        r = client.get(f"/valves/profile?from={_q(T0)}"
                       f"&to={_q(T1)}&run_id={RUN_A}").json()
        assert datetime.fromisoformat(r["base_from"]) == b_start
        for v in VALVOLE:
            base = r["valves"][str(v)]["base"]
            assert base is not None, r["reason"]
            assert set(base) == set(r["valves"][str(v)]["periodo"])
            atteso, n = _media_diretta(db, RUN_A, v, "filling_time_ms",
                                       b_start, b_end)
            assert base["filling_time_ms"]["n"] == n, v
            assert base["filling_time_ms"]["media"] == pytest.approx(
                round(atteso, 1)), v
        assert n > 0
    finally:
        st.set_machine_state("baseline_window", None)


@requires_postgres
def test_base_null_con_motivo_se_non_dichiarata_tutte(client, db):
    """Senza KV la base non si inventa: `null` per ogni valvola, motivo scritto."""
    Storage(db).set_machine_state("baseline_window", None)
    r = client.get(f"/valves/profile?from={_q(T0)}"
                   f"&to={_q(T1)}&run_id={RUN_A}").json()
    assert r["degraded"] is True
    assert "base non disponibile" in r["reason"]
    for v in VALVOLE:
        assert r["valves"][str(v)]["base"] is None, v
        assert r["valves"][str(v)]["periodo"]["filling_time_ms"]["media"] \
            is not None, v


@requires_postgres
def test_profile_non_e_un_id_di_valvola(client):
    """`/valves/profile` non finisce in `/valves/{valve_id}`: 422, non 404."""
    r = client.get(f"/valves/profile?from={_q(T1)}&to={_q(T0)}")
    assert r.status_code == 422, r.text
    assert "intervallo vuoto" in r.text
