"""Test di `GET /valves/progression/series` — serie di progressione oraria.

## Cosa decide se la route e' giusta

**Le due fonti, un solo secchiello.** Le medie e la qualita' vengono dal
riepilogo `cycle_rollup_hour`, la sigma oraria di `filling_time_ms` da una
STDDEV_POP interrogata al volo su `cycles` (passo 4: e' l'unico canale che
grada `pressure_instability`). I due dati devono cadere nello STESSO bucket:
la route serve solo ore COMPLETE (la copertura del riepilogo), dove medie e
sigma descrivono la stessa popolazione di cicli.

**Dati di prova verificabili a mano.** Ogni valvola alterna
1900/1920 ms ogni 10 s: media oraria 1910.0, sigma_pop 10.0 esatte,
qualita' 0.9 (un ciclo su dieci scartato). Il secondo run porta valori
estremi (100/9000) nella STESSA finestra di parete: se i due run si
mescolassero, media e sigma salterebbero a 4550/4450 — impossibile da non
vedere (stessa forma delle prove di non-contaminazione di
`test_api_run_id.py`).

Le altre proprieta': ora di fermata con `total: 0` e canali `null` (la
REGOLA_OMISSIONE di `/valves/quality/series`), ore incomplete escluse,
risoluzione del run (KV, ambiguo → 200 degradato, mai 500), filtro per
valvola.

## Isolamento

DB dedicato per processo (`plcsim_test_prog_<random>`, prefisso registrato in
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
from pipeline.cycles_storage import CURRENT_RUN_ID_KEY, CyclesStorage
from pipeline.storage import Storage, make_engine

from .conftest import drop_db_if_ephemeral


def _test_db_url() -> str:
    if "PLCSIM_PROG_TEST_DATABASE_URL" in os.environ:
        return os.environ["PLCSIM_PROG_TEST_DATABASE_URL"]
    url = (f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
           f"plcsim_test_prog_{secrets.token_hex(4)}")
    os.environ["PLCSIM_PROG_TEST_DATABASE_URL"] = url
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
RUN_A = "prog_test_a"
RUN_B = "prog_test_b"
VALVOLE_A = (1, 2, 3)
VALVOLE_B = (1, 2)                 # due valvole, valori estremi
T0 = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=8, minutes=30)   # 8 ore complete + 1 parziale
# Fermata di un'ora intera: il secchiello 03:00 deve uscire con `total: 0`
# e tutti i canali a null.
FERMO = (T0 + timedelta(hours=3), T0 + timedelta(hours=4))
# Serie verificabile a mano: alternanza 1900/1920 → media 1910.0,
# sigma_pop 10.0 esatte in OGNI ora completa (numero pari di cicli).
_FT_A = [1900, 1920]
# Run B: estremi nella stessa finestra. Se i run si mescolassero:
# media 4550.0, sigma 4450.0.
_FT_B = [100, 9000]

PUNTO_KEYS = {"at", "total", "good", "mean_filling_time_ms",
              "mean_tail_time_ms", "mean_tail_pulse",
              "sigma_filling_time_ms", "quality_rate"}


def _cicli(run_id: str, valvole: tuple[int, ...], passo: timedelta,
           ft: list[int], sfasamento: timedelta) -> list[dict]:
    """Cicli deterministici: alternanza `ft` e qualita' 0.9, con un'ora ferma.

    `i` avanza solo sui cicli scritti: dentro ogni ora completa il numero di
    cicli e' pari, quindi media = (ft[0]+ft[1])/2 e sigma_pop = |ft[1]-ft[0]|/2
    a mano, bucket per bucket.
    """
    rows = []
    for v in valvole:
        i = 0
        t = T0 + sfasamento
        while t < T1:
            if not (FERMO[0] <= t < FERMO[1]):
                rows.append({
                    "run_id": run_id, "machine_id": "filler01",
                    "cycle_id": i + 1, "valve_id": v,
                    "event_ts": t,
                    "filling_time_ms": ft[i % 2],
                    "tail_time_ms": 300, "tail_pulse": 220,
                    "pulse_count": 2500, "target": 2500, "delta_pulse": 0,
                    "filling_step_out": 24,
                    "fill_quality_ok": i % 10 != 0,
                    "filling_ok": True, "sequence_ok": True,
                    "sample_valid": True, "diagnostic_status": "NORMAL",
                    "close_reason": None, "position_limit": False,
                    "filling_overtime": False,
                })
                i += 1
            t += passo
    return rows


@pytest.fixture(scope="module")
def db():
    engine = make_engine(_TEST_DB_URL)
    cs = CyclesStorage(engine)
    cs.drop_all()
    cs.init()
    # RUN_A: 360 cicli/ora per valvola (passo 10 s), 8 ore complete.
    cs.bulk_insert(_cicli(RUN_A, VALVOLE_A, timedelta(seconds=10),
                          _FT_A, timedelta(0)))
    # RUN_B: stessi istanti di parete (sfasati di 2 min), passo 10 min,
    # valori estremi: la contaminazione, se c'e', salta all'occhio.
    cs.bulk_insert(_cicli(RUN_B, VALVOLE_B, timedelta(minutes=10),
                          _FT_B, timedelta(minutes=2)))
    r = CycleRollup(engine)
    r.drop_all()
    r.init()
    r.fill(RUN_A)
    r.fill(RUN_B)
    s = Storage(engine)
    s.init()
    s.set_machine_state(CURRENT_RUN_ID_KEY, RUN_A)
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


def _chiama(client, **kw):
    p = {"run_id": RUN_A, **kw}
    r = client.get("/valves/progression/series", params=p)
    assert r.status_code == 200, r.text
    return r.json()


def _sigma_diretta(db, run: str, valve_id: int, ora: datetime) -> float | None:
    """La sigma dell'ora letta da `cycles` SENZA passare dalla route.

    E' il metro dell'identita': la route deve restituire questo numero,
    cifra per cifra (a un arrotondamento a 3 decimali).
    """
    with db.connect() as c:
        row = c.execute(text(
            "SELECT STDDEV_POP(filling_time_ms::double precision) "
            "FROM cycles WHERE run_id = :r AND valve_id = :v "
            "AND event_ts >= :lo AND event_ts < :hi"),
            {"r": run, "v": valve_id, "lo": ora,
             "hi": ora + timedelta(hours=1)}).scalar_one()
    return None if row is None else float(row)


# -- happy path: forma e numeri ---------------------------------------------
@requires_postgres
def test_serie_felice_con_run_id(client):
    """Run esplicito: forma piena e numeri verificati a mano.

    8 ore complete (00:00 → 08:00, la 08 e' parziale ed e' esclusa), ora di
    fermata compresa. Ogni punto porta ESATTAMENTE le otto chiavi del
    contratto: un canale in piu' o in meno romperebbe chi disegna.
    """
    j = _chiama(client)
    assert j["run_id"] == RUN_A
    assert j["valve_id"] is None
    assert j["degraded"] is False and j["reason"] is None
    assert j["from"] == "2026-07-06T00:00:00+00:00"
    assert j["to"] == "2026-07-06T08:00:00+00:00"
    assert set(j["valves"]) == {"1", "2", "3"}
    serie = j["valves"]["1"]
    assert len(serie) == 8
    for p in serie:
        assert set(p) == PUNTO_KEYS
    pieno = serie[0]
    assert pieno["at"] == "2026-07-06T00:00:00+00:00"
    assert pieno["total"] == 360
    assert pieno["good"] == 324                     # 1 ciclo su 10 scartato
    assert pieno["quality_rate"] == 0.9
    assert pieno["mean_filling_time_ms"] == pytest.approx(1910.0)
    assert pieno["mean_tail_time_ms"] == pytest.approx(300.0)
    assert pieno["mean_tail_pulse"] == pytest.approx(220.0)
    assert pieno["sigma_filling_time_ms"] == pytest.approx(10.0)
    # l'ora di fermata: c'e', e non ha nessun numero inventato
    vuoto = serie[3]
    assert vuoto["at"] == "2026-07-06T03:00:00+00:00"
    assert vuoto["total"] == 0 and vuoto["good"] == 0
    for canale in ("mean_filling_time_ms", "mean_tail_time_ms",
                   "mean_tail_pulse", "sigma_filling_time_ms",
                   "quality_rate"):
        assert vuoto[canale] is None, canale


@requires_postgres
def test_sigma_identita_con_cycles(client, db):
    """La sigma del bucket e' la STDDEV_POP diretta su `cycles` dell'ora.

    Non un valore 'vicino': la stessa aggregazione sulla stessa popolazione.
    E la fermata non produce sigma (nessuna riga → nessun numero).
    """
    j = _chiama(client)
    for at in ("2026-07-06T00:00:00+00:00", "2026-07-06T05:00:00+00:00"):
        ora = datetime.fromisoformat(at)
        atteso = _sigma_diretta(db, RUN_A, 2, ora)
        p = next(p for p in j["valves"]["2"] if p["at"] == at)
        assert p["sigma_filling_time_ms"] == pytest.approx(atteso)
    vuoto = j["valves"]["2"][3]
    assert vuoto["total"] == 0
    assert vuoto["sigma_filling_time_ms"] is None


@requires_postgres
def test_valvole_condividono_gli_stessi_istanti(client):
    """Stessa lista di `at` per tutte le valvole, passo orario costante.

    Un grafico sovrappone le curve fidandosi che l'i-esimo punto di ogni
    serie sia lo stesso istante.
    """
    j = _chiama(client)
    liste = {v: [p["at"] for p in s] for v, s in j["valves"].items()}
    prima = next(iter(liste.values()))
    assert all(l == prima for l in liste.values())
    ts = [datetime.fromisoformat(x) for x in prima]
    assert all(b - a == timedelta(hours=1) for a, b in zip(ts, ts[1:]))
    assert ts[0] == datetime.fromisoformat(j["from"])


# -- non-contaminazione fra run ---------------------------------------------
@requires_postgres
def test_medie_e_sigma_non_contaminate_da_un_secondo_run(client):
    """I valori estremi di RUN_B nella stessa finestra non toccano RUN_A.

    E il filtro non e' vacuo: chiedendo RUN_B i numeri DEVONO essere quelli
    di RUN_B (4550/4450), altrimenti i test verdi proverebbero poco.
    """
    a = _chiama(client)["valves"]["1"][0]
    assert a["mean_filling_time_ms"] == pytest.approx(1910.0)
    assert a["sigma_filling_time_ms"] == pytest.approx(10.0)
    b_run = _chiama(client, run_id=RUN_B)
    assert b_run["run_id"] == RUN_B
    b1 = b_run["valves"]["1"][0]
    assert b1["mean_filling_time_ms"] == pytest.approx(4550.0)
    assert b1["sigma_filling_time_ms"] == pytest.approx(4450.0)
    assert b1["total"] == 6                      # passo 10 min → 6 cicli/ora
    # RUN_B ha due valvole: gia' questo cadrebbe se i run si mescolassero.
    assert set(b_run["valves"]) == {"1", "2"}


@requires_postgres
def test_run_default_dal_kv_current_run_id(client):
    """Senza `run_id` la route segue il KV `current_run_id` (RUN_A)."""
    r = client.get("/valves/progression/series")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["run_id"] == RUN_A
    assert j["valves"]["1"][0]["mean_filling_time_ms"] == pytest.approx(1910.0)


@requires_postgres
def test_run_ambiguo_degrada_mai_500(client, db):
    """Due run e nessun KV → 200 degradato con motivo, mai un 500.

    La stessa forma di degrado di `/valves/quality/series` e di
    `/valves/{id}/kpi`: `degraded: true`, `valves: {}`, motivo leggibile.
    """
    s = Storage(db)
    with s.engine.begin() as conn:
        conn.execute(text("DELETE FROM machine_state WHERE key = :k"),
                     {"k": CURRENT_RUN_ID_KEY})
    try:
        r = client.get("/valves/progression/series")
        assert r.status_code == 200
        j = r.json()
        assert j["degraded"] is True and j["valves"] == {}
        assert j["run_id"] is None
        assert "run non determinato" in j["reason"]
    finally:
        s.set_machine_state(CURRENT_RUN_ID_KEY, RUN_A)


@requires_postgres
def test_run_inesistente_degrada_con_motivo(client):
    """Un run che non ha ore riassunte non e' un 404: e' 200 + motivo."""
    j = _chiama(client, run_id="run_che_non_esiste")
    assert j["degraded"] is True and j["valves"] == {}
    assert "nessuna ora riassunta" in j["reason"]


# -- filtro per valvola -----------------------------------------------------
@requires_postgres
def test_filtro_valvola_serva_solo_quella(client):
    """`valve_id` → una sola valvola, con gli stessi numeri della risposta
    completa: due letture, un punto di verita'."""
    j = _chiama(client, valve_id=2)
    assert j["valve_id"] == 2
    assert set(j["valves"]) == {"2"}
    completa = _chiama(client)
    assert j["valves"]["2"] == completa["valves"]["2"]


@requires_postgres
def test_valvola_senza_cicli_degrada_con_motivo(client):
    """Valvola valida (1-35) ma senza cicli nel run: 200, `valves: {}`,
    motivo — mai una serie di zeri che sembrano misure."""
    j = _chiama(client, valve_id=35)
    assert j["degraded"] is True and j["valves"] == {}
    assert "nessun ciclo riassunto" in j["reason"]


@requires_postgres
def test_valvola_fuori_range_e_422(client):
    r = client.get("/valves/progression/series",
                   params={"run_id": RUN_A, "valve_id": 40})
    assert r.status_code == 422


# -- finestra: solo ore complete --------------------------------------------
@requires_postgres
def test_l_ora_parziale_non_esce(client, db):
    """L'ultima ora (parziale) e' fuori: il riepilogo non la ha, e la route
    non mescola a un secchiello senza medie una sigma da `cycles`."""
    j = _chiama(client)
    ultimo = max(r["event_ts"] for r in
                 _cicli(RUN_A, VALVOLE_A, timedelta(seconds=10),
                        _FT_A, timedelta(0)))
    assert datetime.fromisoformat(j["to"]) <= ultimo.replace(
        minute=0, second=0, microsecond=0) + timedelta(hours=1)
    serie = j["valves"]["1"]
    assert all(p["total"] > 0 for p in serie
               if p["at"] != "2026-07-06T03:00:00+00:00")
