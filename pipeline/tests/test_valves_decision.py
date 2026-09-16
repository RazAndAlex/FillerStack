"""Test di `GET /valves/decision` — il verdetto della politica «costo atteso».

## Cosa decide se la route e' giusta

**Il verdetto e' un conto, e il conto si rifa' a mano.** Per ogni valvola la
route dichiara `D` (quanto costa aspettare un'ora), `R` (quanto fa risparmiare
aspettare un'ora) e i tre ingredienti con cui sono stati fatti: il tasso di
scarti, la probabilita' che il crollo cada nell'ora regalata, la quota di ore
di esercizio. Qui si rifanno le due identita' partendo dai numeri che la route
stessa restituisce:

    D = tasso_scarti_eur_h * 1 h + delta_p * salto_fermi_eur
    R = prezzo_valvola_eur / vita_utile_h * q_es

Se il verdetto fosse deciso altrove, o i numeri fossero cosmetici, queste due
righe cadrebbero.

**Dati di prova verificabili a mano.** Ogni ora di ogni valvola ha 120 cicli
con UN SOLO valore di `filling_time_ms`, quindi la media oraria e' quel
valore, senza arrotondamenti da interpretare. Le due valvole sane alternano
per sempre 1900/1920 ms: mu 1910, sigma 10, banda mu+3sigma = 1940 mai
superata, quindi nessun segnale e nessuna stima. La terza valvola sta nella
stessa banda per il primo giorno (la finestra sana dichiarata) e poi sale di
1 ms all'ora sopra i 1950: supera la banda, ci resta piu' di 24 h e diventa
l'unica valvola con una prognosi. Se la baseline o la regola del segnale
scivolassero, cambierebbe il numero di valvole senza segnale.

**Le tre azioni si contano.** `conteggi` deve sommare al numero di valvole
servite, e ogni azione dichiarata nella lista deve ritrovarsi nel conteggio:
la testata e il corpo della risposta non possono raccontare due cose diverse.

Le altre proprieta': la forma esatta chiave per chiave del contratto
(`work/pezzo7/CONTRATTO.md`), le 24 ore di storia che finiscono ad `adesso`,
`adesso` assente che vale l'ultima ora completa, un'ora fuori dalla corsa che
degrada invece di rompersi, la risoluzione del run (KV, ambiguo, inesistente),
e gli accenti veri nel `motivo` (la pagina non deve piu' ripararli).

## Isolamento

DB dedicato per processo (`plcsim_test_dec_<random>`, prefisso registrato in
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

from pipeline import decision
from pipeline.cycle_rollup import CycleRollup
from pipeline.cycles_storage import CURRENT_RUN_ID_KEY, CyclesStorage
from pipeline.storage import Storage, make_engine

from .conftest import drop_db_if_ephemeral


def _test_db_url() -> str:
    if "PLCSIM_DEC_TEST_DATABASE_URL" in os.environ:
        return os.environ["PLCSIM_DEC_TEST_DATABASE_URL"]
    url = (f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
           f"plcsim_test_dec_{secrets.token_hex(4)}")
    os.environ["PLCSIM_DEC_TEST_DATABASE_URL"] = url
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
RUN_A = "dec_test_a"
RUN_B = "dec_test_b"
T0 = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)
ORE = 96                      # quattro giorni pieni
CICLI_ORA = 120               # un ciclo ogni 30 s: total 120 >= MIN_TOT
SANE = (1, 2)
MALATA = 3
# La finestra sana DICHIARATA: il primo giorno, dove tutte e tre le valvole
# stanno nella stessa banda.
FINESTRA = {"run_id": RUN_A, "start": T0.isoformat(),
            "end": (T0 + timedelta(hours=24)).isoformat()}

PUNTO_KEYS = {"valve_id", "azione", "conferma", "D", "R", "p1", "delta_p",
              "tasso_scarti_eur_h", "q_es", "motivo", "canale", "classe",
              "stima", "storia_24h"}
AZIONI = {"intervieni", "continua degradata", "continua"}


def _filling(valvola: int, h: int) -> int:
    """Media oraria di `filling_time_ms`, decisa a tavolino.

    Nel primo giorno tutte le valvole alternano 1900/1920: mu 1910, sigma 10.
    Dopo, la valvola malata parte da 1950 e sale di 1 ms all'ora, con un
    dente di +-3 ms che le da' dei residui veri (senza residui la banda di
    previsione sarebbe larga zero e la probabilita' non esisterebbe).
    """
    if valvola != MALATA or h < 24:
        return 1900 if h % 2 == 0 else 1920
    return 1950 + (h - 24) + (3 if h % 2 else -3)


def _buoni(valvola: int, h: int) -> int:
    """Cicli buoni dell'ora. Sana: 108/110 → qualita' 0.9 / 0.917.

    La valvola malata, passata la finestra sana, scende a 0.867: sotto
    mu - 3 sigma della SUA finestra sana, quindi le sue lattine buttate
    entrano nel tasso di scarti. Le sane restano sempre dentro la banda e il
    loro tasso vale zero, com'e' giusto: la politica e' cieca e non deve
    pagare il rumore di una valvola sana.
    """
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
    # Un secondo run, per provare che il filtro non e' vacuo e che il run
    # ambiguo degrada invece di mescolare.
    cs.bulk_insert(_cicli(RUN_B, (1,)))
    r = CycleRollup(engine)
    r.drop_all()
    r.init()
    r.fill(RUN_A)
    r.fill(RUN_B)
    s = Storage(engine)
    s.init()
    s.set_machine_state(CURRENT_RUN_ID_KEY, RUN_A)
    s.set_machine_state("baseline_window", FINESTRA)
    yield engine
    cs.drop_all()
    r.drop_all()


@pytest.fixture(scope="module")
def client(db):
    """TestClient con lo storage della route puntato al DB effimero.

    Si scrive `api._store` invece di cambiare `PLCSIM_DATABASE_URL`: l'engine
    dell'API e' un singleton costruito alla prima richiesta, e una variabile
    d'ambiente arrivata dopo non lo sposterebbe piu'.
    """
    from pipeline import api
    prima = api._store
    api._store = Storage(db)
    yield TestClient(api.app)
    api._store = prima


def _ora(h: int) -> str:
    return (T0 + timedelta(hours=h)).isoformat()


def _chiama(client, **kw):
    p = {"run_id": RUN_A, **kw}
    r = client.get("/valves/decision", params=p)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def j(client):
    """Il verdetto a 72 h dall'inizio: due giorni dopo il segnale."""
    return _chiama(client, adesso=_ora(72))


# -- la forma del contratto -------------------------------------------------
@requires_postgres
def test_forma_della_risposta(j):
    """Le cinque chiavi della testata e le quattordici di ogni valvola.

    Una chiave in piu' o in meno romperebbe chi disegna: la pagina legge
    questo oggetto e non rifa' nessun conto.
    """
    assert j["run_id"] == RUN_A
    assert j["adesso"] == "2026-07-09T00:00:00Z"
    assert j["degraded"] is False and j["reason"] is None
    assert set(j["conteggi"]) == {"intervieni", "continua_degradata",
                                  "continua", "senza_dati"}
    assert [v["valve_id"] for v in j["valvole"]] == [1, 2, 3]
    for v in j["valvole"]:
        extra = set(v) - PUNTO_KEYS - {"squadra"}
        assert not extra, extra
        assert PUNTO_KEYS <= set(v)
        assert v["azione"] in AZIONI
        assert set(v["conferma"]) == {"di_fila", "K", "chiamata"}
        assert v["conferma"]["K"] == decision.K_CONFERMA
        assert v["canale"] == "mean_filling_time_ms"
        assert v["classe"] == "flowmeter_dropout"
        if "squadra" in v:
            assert v["conferma"]["chiamata"] is True
            assert set(v["squadra"]) == {"arrivo", "ore"}


@requires_postgres
def test_parametri_sono_quelli_dichiarati(j):
    """I prezzi e le durate del conto, uno per uno.

    Sono i valori con cui sono state registrate le fixture di
    `work/pezzo7/dati/`: cambiarli in silenzio cambierebbe ogni verdetto.
    """
    p = j["parametri"]
    assert p["prezzo_valvola_eur"] == 1500.0
    assert p["vita_utile_h"] == 500.0
    assert p["fermo_non_pianificato_eur_h"] == 3700.0
    assert p["L_h"] == 24 and p["K"] == 2
    assert p["lattina_scartata_eur"] == 0.06
    # 0,47 x 3700 x 6/60 = 173,9 ; 3700 x 81/60 = 4995,0
    assert p["fermo_pianificato_eur_h"] == pytest.approx(173.9)
    assert p["salto_fermi_eur"] == pytest.approx(4995.0 - 173.9, abs=0.05)


# -- il conto, rifatto a mano -----------------------------------------------
@requires_postgres
def test_le_due_identita_del_confronto(j):
    """`D` e `R` ricostruite dagli ingredienti che la route dichiara.

    E' la prova che i numeri mostrati sono quelli usati per decidere, non una
    decorazione calcolata altrove.
    """
    p = j["parametri"]
    for v in j["valvole"]:
        atteso_r = p["prezzo_valvola_eur"] / p["vita_utile_h"] * v["q_es"]
        assert v["R"] == pytest.approx(atteso_r, abs=0.02), v["valve_id"]
        atteso_d = v["tasso_scarti_eur_h"] + v["delta_p"] * p["salto_fermi_eur"]
        assert v["D"] == pytest.approx(atteso_d, abs=0.02), v["valve_id"]


@requires_postgres
def test_l_azione_segue_il_confronto(j):
    """`intervieni` quando `D > R`, mai altrimenti.

    E' la regola, in una riga: la soglia non e' un parametro nascosto, e'
    l'incrocio delle due curve.
    """
    for v in j["valvole"]:
        if v["azione"] == "intervieni":
            assert v["D"] > v["R"], v["valve_id"]
        else:
            assert v["D"] <= v["R"], v["valve_id"]


@requires_postgres
def test_i_conteggi_sono_la_lista(j):
    """La testata conta esattamente le azioni della lista, e niente di piu'."""
    c = j["conteggi"]
    assert sum(c.values()) == len(j["valvole"]) == 3
    chiave = {"continua degradata": "continua_degradata"}
    for v in j["valvole"]:
        assert c[chiave.get(v["azione"], v["azione"])] > 0
    atteso = {"intervieni": 0, "continua_degradata": 0, "continua": 0,
              "senza_dati": 0}
    for v in j["valvole"]:
        atteso[chiave.get(v["azione"], v["azione"])] += 1
    assert c == atteso


# -- il segnale e la stima --------------------------------------------------
@requires_postgres
def test_le_valvole_sane_non_hanno_segnale(j):
    """Dentro la banda non c'e' niente da segnalare: nessuna stima, `D` a zero.

    Le due sane restano a 1900/1920 ms per sempre, cioe' sotto mu+3sigma =
    1940: non c'e' regime, non c'e' prognosi, non ci sono lattine buttate.
    """
    for vid in SANE:
        v = next(x for x in j["valvole"] if x["valve_id"] == vid)
        assert v["stima"]["esito"] == "nessun_segnale"
        assert v["stima"]["segnale"] is None
        assert v["azione"] == "continua"
        assert v["D"] == 0.0 and v["p1"] == 0.0 and v["delta_p"] == 0.0
        assert v["tasso_scarti_eur_h"] == 0.0
        assert v["conferma"] == {"di_fila": 0, "K": 2, "chiamata": False}
        assert "squadra" not in v


@requires_postgres
def test_la_valvola_che_deriva_ha_una_prognosi(j):
    """Segnale all'ora 24, retta stimata, banda ordinata lo <= hat <= hi.

    La valvola sale sopra 1940 dall'ora 24 e non rientra piu': il segnale e'
    quell'ora, non un'altra. La banda e' al 95% e i suoi tre estremi sono in
    ordine: una banda rovesciata sarebbe un conto sbagliato che sembra giusto.
    """
    v = next(x for x in j["valvole"] if x["valve_id"] == MALATA)
    s = v["stima"]
    assert s["esito"] == "stimato", s
    assert s["segnale"] == "2026-07-07T00:00:00Z"
    assert s["ore_di_osservazione"] == pytest.approx(48.0)
    assert s["n"] == 49                       # ore 24..72 comprese
    assert s["crollo_lo"] <= s["crollo_hat"] <= s["crollo_hi"]
    assert s["ore_a_crollo_lo"] <= s["ore_a_crollo_hat"] <= s["ore_a_crollo_hi"]
    assert 0.0 <= v["p1"] <= 1.0 and 0.0 <= v["delta_p"] <= 1.0
    assert v["tasso_scarti_eur_h"] > 0.0
    assert v["azione"] != "continua"


# -- le ultime 24 ore -------------------------------------------------------
@requires_postgres
def test_storia_24h_finisce_ad_adesso(j):
    """24 voci orarie contigue, la piu' vecchia per prima, l'ultima e' `adesso`.

    E' la striscia che la pagina disegna sotto ogni valvola: se l'ultima voce
    non fosse l'ora del verdetto, la striscia e il numero grande
    racconterebbero due istanti diversi.
    """
    for v in j["valvole"]:
        st = v["storia_24h"]
        assert len(st) == 24
        ts = [datetime.fromisoformat(x["at"].replace("Z", "+00:00"))
              for x in st]
        assert all(b - a == timedelta(hours=1) for a, b in zip(ts, ts[1:]))
        assert st[-1]["at"] == j["adesso"]
        assert st[-1]["azione"] == v["azione"]
        assert st[-1]["D"] == v["D"] and st[-1]["R"] == v["R"]
        for x in st:
            assert x["azione"] in AZIONI


# -- il testo ---------------------------------------------------------------
@requires_postgres
def test_il_motivo_ha_gli_accenti_veri(client):
    """«però», «è», «già» scritti bene, mai traslitterati.

    Il laboratorio scrive in ASCII perche' stampa a terminale. Qui il testo
    esce in JSON, e la pagina non deve piu' ripararlo da sola.
    """
    j72 = _chiama(client, adesso=_ora(72))
    testi = [v["motivo"] for v in j72["valvole"]]
    assert all(t for t in testi)
    assert not any("pero'" in t or "e' gia'" in t for t in testi)
    degradate = [t for t in testi if "fuori banda" in t]
    for t in degradate:
        assert "però è già fuori banda" in t


# -- `adesso` ---------------------------------------------------------------
@requires_postgres
def test_senza_adesso_risponde_sull_ultima_ora(client):
    """L'ultima ora COMPLETA del run, non l'orologio di parete.

    E' la copertura del riepilogo (`_copertura_riepilogo`), la stessa che
    delimita `/valves/progression/series`: l'ultima ora con cicli resta fuori
    finche' non e' finita. Qui i cicli arrivano fino alle 23:59:30, quindi
    l'ultimo secchiello servibile e' quello delle 22:00.
    """
    j = _chiama(client)
    assert j["adesso"] == "2026-07-09T22:00:00Z"
    assert j["degraded"] is False
    assert sum(j["conteggi"].values()) == 3


@requires_postgres
def test_adesso_dentro_l_ora_si_arrotonda_al_secchiello(client):
    """Un istante a meta' ora risponde sul secchiello che lo contiene.

    Il riepilogo e' orario: un verdetto «alle 12:37» descriverebbe comunque
    l'ora delle 12:00, e dichiararlo e' meglio che lasciarlo intuire.
    """
    r = client.get("/valves/decision",
                   params={"run_id": RUN_A,
                           "adesso": "2026-07-09T00:37:00+00:00"})
    assert r.status_code == 200, r.text
    assert r.json()["adesso"] == "2026-07-09T00:00:00Z"


@requires_postgres
def test_adesso_fuori_dalla_corsa_degrada(client):
    """Un'ora che la corsa non copre e' 200 + motivo, mai un 500 e mai zeri."""
    j = _chiama(client, adesso="2030-01-01T00:00:00+00:00")
    assert j["degraded"] is True and j["valvole"] == []
    assert "sta fuori dalla corsa" in j["reason"]


@requires_postgres
def test_prima_delle_24_ore_nessuna_valvola_ha_una_riga(client):
    """La griglia parte 24 h dopo il primo secchiello: prima non c'e' verdetto.

    Il tasso di scarti e la quota di esercizio si leggono sulle 24 ore di
    orologio precedenti: senza quelle ore il conto non esiste, e la route lo
    dichiara con `senza_dati` invece di inventare uno zero.
    """
    j = _chiama(client, adesso=_ora(5))
    assert j["conteggi"]["senza_dati"] == 3
    assert all(v["stima"]["esito"] == "senza_dati" for v in j["valvole"])
    assert all(v["storia_24h"] == [] for v in j["valvole"])


# -- risoluzione del run ----------------------------------------------------
@requires_postgres
def test_run_default_dal_kv_current_run_id(client):
    """Senza `run_id` la route segue il KV `current_run_id` (RUN_A)."""
    r = client.get("/valves/decision", params={"adesso": _ora(72)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["run_id"] == RUN_A
    assert [v["valve_id"] for v in j["valvole"]] == [1, 2, 3]


@requires_postgres
def test_il_filtro_di_run_non_e_vacuo(client):
    """RUN_B ha una valvola sola: chiedendolo si vede quella, non tre."""
    j = _chiama(client, run_id=RUN_B, adesso=_ora(72))
    assert j["run_id"] == RUN_B
    assert [v["valve_id"] for v in j["valvole"]] == [1]


@requires_postgres
def test_run_ambiguo_degrada_mai_500(client, db):
    """Due run e nessun KV → 200 degradato con motivo, mai un 500."""
    s = Storage(db)
    with s.engine.begin() as conn:
        conn.execute(text("DELETE FROM machine_state WHERE key = :k"),
                     {"k": CURRENT_RUN_ID_KEY})
    try:
        r = client.get("/valves/decision")
        assert r.status_code == 200
        j = r.json()
        assert j["degraded"] is True and j["valvole"] == []
        assert j["run_id"] is None
        assert "run non determinato" in j["reason"]
    finally:
        s.set_machine_state(CURRENT_RUN_ID_KEY, RUN_A)


@requires_postgres
def test_run_inesistente_degrada_con_motivo(client):
    """Un run senza ore riassunte non e' un 404: e' 200 + motivo."""
    j = _chiama(client, run_id="run_che_non_esiste")
    assert j["degraded"] is True and j["valvole"] == []
    assert "nessuna ora riassunta" in j["reason"]


@requires_postgres
def test_senza_finestra_sana_degrada(client, db):
    """Senza finestra sana dichiarata il segnale non e' definibile.

    Stessa disciplina di `/valves/baseline`: quale finestra sia sana e' una
    decisione umana e l'API non la deduce. Meglio dirlo che stimare su una
    baseline inventata.
    """
    s = Storage(db)
    with s.engine.begin() as conn:
        conn.execute(text("DELETE FROM machine_state WHERE key = :k"),
                     {"k": "baseline_window"})
    try:
        j = _chiama(client, adesso=_ora(72))
        assert j["degraded"] is True and j["valvole"] == []
        assert "nessuna finestra sana dichiarata" in j["reason"]
    finally:
        s.set_machine_state("baseline_window", FINESTRA)


# -- determinismo -----------------------------------------------------------
@requires_postgres
def test_due_chiamate_uguali_danno_lo_stesso_oggetto(client):
    """Nessuna cache a chiave `id()`, nessun residuo fra richieste.

    E' il motivo per cui il calcolo e' stato riscritto invece di importare
    `work/policy-lab/banco.py`, che memoizza la baseline su `id(serie)`: gli
    `id()` si riusano dopo una deallocazione, e in un processo che serve piu'
    richieste quel memo puo' restituire la baseline della valvola sbagliata.
    """
    a = _chiama(client, adesso=_ora(72))
    _altro = _chiama(client, run_id=RUN_B, adesso=_ora(72))
    b = _chiama(client, adesso=_ora(72))
    assert a == b
