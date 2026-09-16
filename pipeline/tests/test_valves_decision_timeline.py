"""Test di `GET /valves/decision/timeline` — i conteggi ora per ora.

## Cosa decide se la route e' giusta

**La striscia e il verdetto devono dire lo stesso numero.** E' la proprieta'
che conta piu' di tutte. La pagina disegna la striscia con questa route e poi,
quando si sceglie un'ora, chiede il verdetto a `GET /valves/decision`. Se le
due camminate divergessero, la casella dell'ora e il pannello dell'ora
direbbero cose diverse sullo stesso istante. Qui si prendono due ore lontane
fra loro e si confronta casella per casella.

**La forma e' un contratto stretto.** `i` e `d` sono lunghe quanto `ore`, le
ore sono contigue e distano un'ora, `prima_ora` e `ultima_ora` sono la prima e
l'ultima. La pagina non riceve nessun array di istanti: li ricostruisce
dall'indice, quindi una lunghezza sbagliata sposterebbe tutta la striscia.

**Le chiamate sono ordinate.** Le tacche si disegnano in ordine di tempo.

**Un run che non esiste degrada.** 200 con il motivo, mai un 500 e mai una
striscia di zeri che sembra una misura.

I dati di prova sono quelli di `test_valves_decision.py`: tre valvole, due
sane per sempre e una che dall'ora 24 sale di 1 ms all'ora sopra la banda.

## Isolamento

DB dedicato per processo (`plcsim_test_lin_<random>`, prefisso registrato in
`conftest._PREFISSI_EFFIMERI`).
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
    if "PLCSIM_LIN_TEST_DATABASE_URL" in os.environ:
        return os.environ["PLCSIM_LIN_TEST_DATABASE_URL"]
    url = (f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
           f"plcsim_test_lin_{secrets.token_hex(4)}")
    os.environ["PLCSIM_LIN_TEST_DATABASE_URL"] = url
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
            if not conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"),
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
RUN_A = "lin_test_a"
T0 = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)
ORE = 96                      # quattro giorni pieni
CICLI_ORA = 120
SANE = (1, 2)
MALATA = 3
FINESTRA = {"run_id": RUN_A, "start": T0.isoformat(),
            "end": (T0 + timedelta(hours=24)).isoformat()}

TESTATA = {"run_id", "prima_ora", "ultima_ora", "ore", "i", "d", "chiamate",
           "degraded", "reason"}


def _filling(valvola: int, h: int) -> int:
    if valvola != MALATA or h < 24:
        return 1900 if h % 2 == 0 else 1920
    return 1950 + (h - 24) + (3 if h % 2 else -3)


def _buoni(valvola: int, h: int) -> int:
    if valvola == MALATA and h >= 24:
        return 104
    return 108 if h % 2 == 0 else 110


def _cicli(run_id: str, valvole: tuple[int, ...]) -> list[dict]:
    rows = []
    passo = timedelta(hours=1) / CICLI_ORA
    for v in valvole:
        for h in range(ORE):
            ft = _filling(v, h)
            buoni = _buoni(v, h)
            base = T0 + timedelta(hours=h)
            for i in range(CICLI_ORA):
                rows.append({
                    "run_id": run_id, "machine_id": "filler01",
                    "cycle_id": h * CICLI_ORA + i + 1, "valve_id": v,
                    "event_ts": base + i * passo,
                    "filling_time_ms": ft,
                    "tail_time_ms": 300, "tail_pulse": 220,
                    "pulse_count": 2500, "target": 2500, "delta_pulse": 0,
                    "filling_step_out": 24,
                    "fill_quality_ok": i < buoni,
                    "filling_ok": True, "sequence_ok": True,
                    "sample_valid": True, "diagnostic_status": "NORMAL",
                    "close_reason": None, "position_limit": False,
                    "filling_overtime": False,
                })
    return rows


@pytest.fixture(scope="module")
def db():
    engine = make_engine(_TEST_DB_URL)
    cs = CyclesStorage(engine)
    cs.drop_all()
    cs.init()
    cs.bulk_insert(_cicli(RUN_A, (*SANE, MALATA)))
    r = CycleRollup(engine)
    r.drop_all()
    r.init()
    r.fill(RUN_A)
    s = Storage(engine)
    s.init()
    s.set_machine_state(CURRENT_RUN_ID_KEY, RUN_A)
    s.set_machine_state("baseline_window", FINESTRA)
    yield engine
    cs.drop_all()
    r.drop_all()


@pytest.fixture(scope="module")
def client(db):
    """TestClient con lo storage della route puntato al DB effimero."""
    from pipeline import api
    prima = api._store
    api._store = Storage(db)
    yield TestClient(api.app)
    api._store = prima


@pytest.fixture(scope="module")
def j(client):
    r = client.get("/valves/decision/timeline", params={"run_id": RUN_A})
    assert r.status_code == 200, r.text
    return r.json()


def _z(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# -- la forma ---------------------------------------------------------------
@requires_postgres
def test_forma_della_risposta(j):
    """Le nove chiavi della testata, e niente altro.

    La pagina legge questo oggetto senza rifare nessun conto: una chiave in
    piu' o in meno romperebbe chi disegna.
    """
    assert set(j) == TESTATA
    assert j["run_id"] == RUN_A
    assert j["degraded"] is False and j["reason"] is None
    assert isinstance(j["ore"], int) and j["ore"] > 0
    assert all(isinstance(x, int) and x >= 0 for x in j["i"])
    assert all(isinstance(x, int) and x >= 0 for x in j["d"])


@requires_postgres
def test_le_tre_lunghezze_coincidono(j):
    """`len(i) == len(d) == ore`.

    La pagina ricava l'istante di una casella dall'indice: una lunghezza
    sbagliata sposterebbe tutta la striscia senza dirlo.
    """
    assert len(j["i"]) == len(j["d"]) == j["ore"]


@requires_postgres
def test_le_ore_sono_contigue(j):
    """Da `prima_ora` a `ultima_ora`, un passo di un'ora, nessun buco.

    E' l'ipotesi su cui si regge la ricostruzione degli istanti dall'indice.
    """
    p = datetime.strptime(j["prima_ora"], "%Y-%m-%dT%H:%M:%SZ")
    u = datetime.strptime(j["ultima_ora"], "%Y-%m-%dT%H:%M:%SZ")
    assert (u - p) == timedelta(hours=j["ore"] - 1)


@requires_postgres
def test_la_griglia_parte_24_ore_dopo_il_primo_secchiello(j):
    """Prima di 24 h il tasso di scarti non esiste, quindi non c'e' nessuna ora.

    Stessa regola di `GET /valves/decision`, che li' risponde `senza_dati`.
    """
    assert j["prima_ora"] == _z(T0 + timedelta(hours=24))


@requires_postgres
def test_le_chiamate_sono_ordinate_e_ben_formate(j):
    """Ogni tacca porta valvola, chiamata e arrivo, in ordine di tempo.

    L'arrivo sta `L_h` ore dopo la chiamata: e' la squadra che viaggia.
    """
    from pipeline import decision
    istanti = [c["chiamata"] for c in j["chiamate"]]
    assert istanti == sorted(istanti)
    for c in j["chiamate"]:
        assert set(c) == {"valvola", "chiamata", "arrivo"}
        a = datetime.strptime(c["chiamata"], "%Y-%m-%dT%H:%M:%SZ")
        b = datetime.strptime(c["arrivo"], "%Y-%m-%dT%H:%M:%SZ")
        assert (b - a) == timedelta(hours=decision.L_H)
    # una valvola chiama al massimo una volta per corsa
    valvole = [c["valvola"] for c in j["chiamate"]]
    assert len(valvole) == len(set(valvole))


@requires_postgres
def test_le_valvole_sane_non_riempiono_la_striscia(j):
    """Le due sane non superano mai la banda: i conteggi restano sotto tre."""
    assert max(j["i"]) <= 1
    assert max(j["d"]) <= 1


# -- la proprieta' che conta ------------------------------------------------
@requires_postgres
@pytest.mark.parametrize("h", [40, 72])
def test_la_casella_dice_quello_che_dice_il_verdetto(client, j, h):
    """Stessa ora, stessa camminata, stessi numeri.

    E' la ragione per cui la regola sta in una funzione sola
    (`decision.camminata`): se la striscia e il verdetto la riscrivessero
    ciascuno per conto proprio, un giorno direbbero cose diverse sulla stessa
    ora e nessun test se ne accorgerebbe.
    """
    ora = T0 + timedelta(hours=h)
    r = client.get("/valves/decision",
                   params={"run_id": RUN_A, "adesso": ora.isoformat()})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["degraded"] is False
    p = datetime.strptime(j["prima_ora"], "%Y-%m-%dT%H:%M:%SZ")
    k = int((ora.replace(tzinfo=None) - p).total_seconds() // 3600)
    assert 0 <= k < j["ore"]
    assert j["i"][k] == v["conteggi"]["intervieni"]
    assert j["d"][k] == v["conteggi"]["continua_degradata"]


@requires_postgres
def test_la_chiamata_coincide_con_quella_del_verdetto(client, j):
    """La tacca della striscia e' la chiamata che il verdetto dichiara.

    Si interroga il verdetto all'ora della tacca e si pretende che quella
    valvola risulti in chiamata, con lo stesso arrivo.
    """
    if not j["chiamate"]:
        pytest.skip("i dati di prova non producono nessuna chiamata")
    c = j["chiamate"][0]
    ora = datetime.strptime(c["chiamata"], "%Y-%m-%dT%H:%M:%SZ")
    r = client.get("/valves/decision",
                   params={"run_id": RUN_A,
                           "adesso": ora.replace(tzinfo=timezone.utc)
                           .isoformat()})
    assert r.status_code == 200, r.text
    v = next(x for x in r.json()["valvole"]
             if x["valve_id"] == c["valvola"])
    assert v["conferma"]["chiamata"] is True
    assert v["squadra"]["arrivo"] == c["arrivo"]


# -- degrado ----------------------------------------------------------------
@requires_postgres
def test_run_inesistente_degrada_con_motivo(client):
    """Un run senza ore riassunte e' 200 + motivo, mai un 500."""
    r = client.get("/valves/decision/timeline",
                   params={"run_id": "run_che_non_esiste"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert set(j) == TESTATA
    assert j["degraded"] is True
    assert j["ore"] == 0 and j["i"] == [] and j["d"] == []
    assert j["chiamate"] == []
    assert "nessuna ora riassunta" in j["reason"]


@requires_postgres
def test_run_default_dal_kv_current_run_id(client):
    """Senza `run_id` la route segue il KV `current_run_id`, come la vicina."""
    r = client.get("/valves/decision/timeline")
    assert r.status_code == 200, r.text
    assert r.json()["run_id"] == RUN_A


@requires_postgres
def test_senza_finestra_sana_degrada(client, db):
    """Senza finestra sana dichiarata il segnale non e' definibile."""
    s = Storage(db)
    with s.engine.begin() as conn:
        conn.execute(text("DELETE FROM machine_state WHERE key = :k"),
                     {"k": "baseline_window"})
    try:
        r = client.get("/valves/decision/timeline", params={"run_id": RUN_A})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["degraded"] is True and j["ore"] == 0
        assert "nessuna finestra sana dichiarata" in j["reason"]
    finally:
        s.set_machine_state("baseline_window", FINESTRA)
