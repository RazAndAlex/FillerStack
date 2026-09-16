"""Test di `pipeline/decision_rollup.py` — il verdetto precalcolato.

## Cosa decide se il precalcolo e' giusto

**Un solo criterio: il corpo letto dalla tabella e' identico a quello
calcolato.** Non «equivalente». Il precalcolo esiste per togliere secondi a
una pagina, e il modo in cui un'ottimizzazione fa danno e' spostare di un
capello un numero che nessuno riguarda piu'. Qui il confronto e' sull'oggetto
intero, campo per campo e in ogni ora della corsa di prova, non su un
campione di chiavi scelte a mano.

Le altre proprieta', ognuna un modo noto di rompersi:

- **contiguita'**: ogni ora fra `MIN(ora_ts)` e `MAX(ora_ts)` ha le sue righe,
  una per valvola. Un buco verrebbe letto come «zero intervieni, zero
  degradate», cioe' come «tutto calmo»: e' la bugia peggiore che questa
  tabella possa dire.
- **idempotenza**: rieseguire il riempimento non aggiunge righe e non cambia
  una cifra. La PK e' la chiave `ON CONFLICT DO UPDATE`.
- **freschezza**: se il riepilogo dei cicli si allunga, `fresco()` dice di no.
  Una risposta lenta e' un difetto, una vecchia e' una bugia.
- **`storia_24h` sono le ultime 24 RIGHE**, non le ultime 24 ore: con una
  corsa bucata le due cose danno storie di lunghezza diversa.
- **la rotta senza tabella risponde come prima**, e con la tabella risponde lo
  stesso corpo. Chi non ha precalcolato deve vedere la pagina di ieri, solo
  lenta.

I dati di prova sono quelli di `test_valves_decision.py`, riscritti qui in
breve: due valvole sane che alternano per sempre 1900/1920 ms e una che dopo
la finestra sana sale di 1 ms all'ora sopra i 1950, cioe' l'unica con un
segnale e una prognosi.

## Isolamento

DB dedicato per processo (`plcsim_test_prec_<random>`, prefisso registrato in
`conftest._PREFISSI_EFFIMERI`).
**Nessun test scrive sul database operazionale `plcsim`.**
"""
from __future__ import annotations

import json
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
from pipeline.decision_rollup import (
    DECISION_TABLE,
    DecisionRollup,
    DecisionRollupError,
    main as rollup_main,
)
from pipeline.storage import Storage, make_engine

from .conftest import drop_db_if_ephemeral


def _test_db_url() -> str:
    if "PLCSIM_PREC_TEST_DATABASE_URL" in os.environ:
        return os.environ["PLCSIM_PREC_TEST_DATABASE_URL"]
    url = (f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
           f"plcsim_test_prec_{secrets.token_hex(4)}")
    os.environ["PLCSIM_PREC_TEST_DATABASE_URL"] = url
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
RUN = "prec_test"
T0 = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)
ORE = 96
CICLI_ORA = 120
SANE = (1, 2)
MALATA = 3
FINESTRA = {"run_id": RUN, "start": T0.isoformat(),
            "end": (T0 + timedelta(hours=24)).isoformat()}


def _filling(valvola: int, h: int) -> int:
    if valvola != MALATA or h < 24:
        return 1900 if h % 2 == 0 else 1920
    return 1950 + (h - 24) + (3 if h % 2 else -3)


def _buoni(valvola: int, h: int) -> int:
    if valvola == MALATA and h >= 24:
        return 104
    return 108 if h % 2 == 0 else 110


def _cicli() -> list[dict]:
    rows = []
    passo = timedelta(hours=1) / CICLI_ORA
    for v in (*SANE, MALATA):
        for h in range(ORE):
            ft, buoni = _filling(v, h), _buoni(v, h)
            base = T0 + timedelta(hours=h)
            for i in range(CICLI_ORA):
                rows.append({
                    "run_id": RUN, "machine_id": "filler01",
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
    cs.bulk_insert(_cicli())
    r = CycleRollup(engine)
    r.drop_all()
    r.init()
    r.fill(RUN)
    s = Storage(engine)
    s.init()
    s.set_machine_state(CURRENT_RUN_ID_KEY, RUN)
    s.set_machine_state("baseline_window", FINESTRA)
    d = DecisionRollup(engine)
    d.drop_all()
    yield engine
    cs.drop_all()
    r.drop_all()
    d.drop_all()


@pytest.fixture(scope="module")
def pieno(db):
    """La tabella riempita una volta sola, piu' gli ingredienti del confronto."""
    from pipeline import api
    d = DecisionRollup(db)
    d.init()
    d.fill(RUN)
    st = Storage(db)
    cov_lo, cov_hi, _p = api._copertura_riepilogo(st, RUN)
    serie = api._serie_per_decisione(st, RUN, cov_lo, cov_hi + timedelta(hours=1))
    fs, fe, _s, _k = api._baseline_window(st, None, None)
    finestra = {"start": api._iso(fs), "end": api._iso(fe)}
    return d, serie, finestra, cov_hi


@pytest.fixture(scope="module")
def client(db):
    from pipeline import api
    prima = api._store
    api._store = Storage(db)
    yield TestClient(api.app)
    api._store = prima


def _canonico(corpo) -> str:
    return json.dumps(corpo, sort_keys=True, default=str)


# -- il criterio: identita' del corpo ---------------------------------------

@requires_postgres
def test_ogni_ora_della_corsa_da_lo_stesso_corpo(pieno):
    """Il corpo letto e quello calcolato coincidono in TUTTE le ore.

    Non un campione: la corsa di prova e' corta apposta perche' il confronto
    possa essere esaustivo. Un'ottimizzazione che sposta una cifra la sposta
    quasi sempre in un solo punto della corsa.
    """
    d, serie, finestra, _cov_hi = pieno
    lo, hi = d.coverage(RUN)
    t = lo
    while t <= hi:
        atteso = decision.decisione(serie, finestra, RUN, t.isoformat())
        assert _canonico(d.decisione(RUN, t)) == _canonico(atteso), t
        t += timedelta(hours=1)


@requires_postgres
def test_la_linea_letta_e_quella_calcolata(pieno):
    d, serie, finestra, cov_hi = pieno
    atteso = decision.linea(serie, finestra, RUN, cov_hi.isoformat())
    assert _canonico(d.linea(RUN)) == _canonico(atteso)


@requires_postgres
def test_la_valvola_malata_ha_una_prognosi(pieno):
    """La corsa di prova deve contenere davvero una stima, non solo zeri.

    Senza questo il test precedente potrebbe passare confrontando due volte
    «nessun segnale»: il blocco `stima` e' il pezzo piu' fragile della riga e
    deve essere esercitato.
    """
    d, _serie, _finestra, cov_hi = pieno
    corpo = d.decisione(RUN, cov_hi)
    malata = [v for v in corpo["valvole"] if v["valve_id"] == MALATA][0]
    assert malata["stima"]["esito"] != "nessun_segnale"
    assert malata["stima"]["segnale"] is not None
    # l'ordine delle chiavi torna quello di `stima_piena`, non quello di JSONB
    assert list(malata["stima"])[0] == "esito"


# -- contiguita', idempotenza, freschezza -----------------------------------

@requires_postgres
def test_copertura_contigua_e_una_riga_per_valvola(pieno, db):
    d, _serie, _finestra, _cov_hi = pieno
    lo, hi = d.coverage(RUN)
    atteso_ore = int((hi - lo).total_seconds() // 3600) + 1
    with db.connect() as conn:
        righe = conn.execute(text(
            f"SELECT ora_ts, COUNT(*) FROM {DECISION_TABLE} "
            "WHERE run_id = :r GROUP BY ora_ts ORDER BY ora_ts"),
            {"r": RUN}).all()
    assert len(righe) == atteso_ore
    assert {n for _t, n in righe} == {len((*SANE, MALATA))}
    # nessun salto: le ore distano esattamente un'ora
    istanti = [t for t, _n in righe]
    assert all((b - a) == timedelta(hours=1)
               for a, b in zip(istanti, istanti[1:]))


@requires_postgres
def test_riempire_due_volte_non_cambia_niente(pieno):
    d, _serie, _finestra, cov_hi = pieno
    prima_righe = d.rows_for(RUN)
    prima_corpo = _canonico(d.decisione(RUN, cov_hi))
    d.fill(RUN)
    assert d.rows_for(RUN) == prima_righe
    assert _canonico(d.decisione(RUN, cov_hi)) == prima_corpo


@requires_postgres
def test_fresco_dice_no_se_i_cicli_si_allungano(pieno):
    d, _serie, finestra, cov_hi = pieno
    assert d.fresco(RUN, cov_hi, finestra) is True
    assert d.fresco(RUN, cov_hi + timedelta(hours=1), finestra) is False
    assert d.fresco("run_che_non_esiste", cov_hi, finestra) is False


@requires_postgres
def test_fresco_dice_no_se_cambia_una_costante_o_la_finestra(pieno):
    """La freschezza non e' solo copertura: e' anche l'impronta dei parametri.

    Prima dell'impronta questa tabella si dichiarava fresca dopo un cambio di
    `PREZZO_VALVOLA_EUR` o di finestra sana, e le rotte servivano i numeri
    vecchi senza dirlo.
    """
    d, _serie, finestra, cov_hi = pieno
    assert d.fresco(RUN, cov_hi, finestra) is True
    prima = decision.PREZZO_VALVOLA_EUR
    try:
        decision.PREZZO_VALVOLA_EUR = prima + 100.0
        assert d.fresco(RUN, cov_hi, finestra) is False
    finally:
        decision.PREZZO_VALVOLA_EUR = prima
    assert d.fresco(RUN, cov_hi, finestra) is True
    altra = dict(finestra, end=str(finestra["end"]).replace("T00", "T01"))
    assert altra != finestra
    assert d.fresco(RUN, cov_hi, altra) is False


@requires_postgres
def test_la_rotta_ricalcola_quando_cambia_un_prezzo(pieno, client):
    """Il punto che questo lavoro esiste per chiudere.

    La tabella resta com'e'; si cambia `PREZZO_VALVOLA_EUR` in memoria e si
    chiede l'ultima ora alla rotta. Il verdetto deve venire dal **calcolo**,
    con i numeri nuovi: `R` scala con il prezzo della valvola, quindi un
    prezzo diverso deve muovere `R` e i `parametri` del corpo.
    """
    d, _serie, _finestra, cov_hi = pieno
    p = {"run_id": RUN, "adesso": cov_hi.isoformat()}
    vecchio = client.get("/valves/decision", params=p).json()
    righe_prima = d.rows_for(RUN)

    prima = decision.PREZZO_VALVOLA_EUR
    try:
        decision.PREZZO_VALVOLA_EUR = prima + 100.0
        nuovo = client.get("/valves/decision", params=p).json()
    finally:
        decision.PREZZO_VALVOLA_EUR = prima

    assert nuovo["parametri"]["prezzo_valvola_eur"] == prima + 100.0
    assert vecchio["parametri"]["prezzo_valvola_eur"] == prima
    r_vecchi = {v["valve_id"]: v["R"] for v in vecchio["valvole"]}
    r_nuovi = {v["valve_id"]: v["R"] for v in nuovo["valvole"]}
    assert r_nuovi != r_vecchi
    # la tabella non e' stata toccata, e tornata la costante torna il corpo
    assert d.rows_for(RUN) == righe_prima
    di_nuovo = client.get("/valves/decision", params=p).json()
    assert _canonico(di_nuovo) == _canonico(vecchio)


@requires_postgres
def test_storia_sono_le_ultime_24_righe(pieno):
    """La storia e' lunga 24 dovunque la griglia abbia gia' 24 righe."""
    d, _serie, _finestra, cov_hi = pieno
    lo, _hi = d.coverage(RUN)
    corto = d.decisione(RUN, lo + timedelta(hours=3))
    assert [len(v["storia_24h"]) for v in corto["valvole"]] == [4, 4, 4]
    pieno_ = d.decisione(RUN, cov_hi)
    assert [len(v["storia_24h"]) for v in pieno_["valvole"]] == [24, 24, 24]


# -- le rotte ---------------------------------------------------------------

@requires_postgres
def test_le_rotte_rispondono_lo_stesso_corpo_con_e_senza_tabella(client, db):
    """Chi non ha precalcolato vede la pagina di ieri, solo lenta.

    Si misura scartando la tabella, leggendo le due rotte, rimettendola e
    rileggendole: i due corpi devono coincidere. E' la garanzia che il
    precalcolo non abbia cambiato la risposta di nascosto.
    """
    d = DecisionRollup(db)
    d.drop_all()
    senza_v = client.get("/valves/decision", params={"run_id": RUN}).json()
    senza_t = client.get("/valves/decision/timeline",
                         params={"run_id": RUN}).json()
    assert senza_v["degraded"] is False
    d.init()
    d.fill(RUN)
    con_v = client.get("/valves/decision", params={"run_id": RUN}).json()
    con_t = client.get("/valves/decision/timeline",
                       params={"run_id": RUN}).json()
    assert _canonico(con_v) == _canonico(senza_v)
    assert _canonico(con_t) == _canonico(senza_t)


@requires_postgres
def test_ora_fuori_dal_precalcolo_ricalcola_invece_di_mentire(client, pieno):
    """Prima delle 24 h la griglia non esiste: deve dirlo il calcolo.

    La tabella non copre quelle ore, e rispondere «senza dati» leggendo una
    tabella che semplicemente non arriva fin li' sarebbe inventare un fatto.
    """
    r = client.get("/valves/decision",
                   params={"run_id": RUN, "adesso": T0.isoformat()})
    corpo = r.json()
    assert r.status_code == 200
    assert corpo["degraded"] is False
    assert corpo["conteggi"]["senza_dati"] == len((*SANE, MALATA))


# -- CLI --------------------------------------------------------------------

@requires_postgres
def test_cli_status_non_scrive(pieno, capsys):
    d, _serie, _finestra, _cov_hi = pieno
    prima = d.rows_for(RUN)
    assert rollup_main(["--run-id", RUN, "--db-url", _TEST_DB_URL,
                        "--status"]) == 0
    fuori = capsys.readouterr().out
    assert str(prima) in fuori
    assert d.rows_for(RUN) == prima


@requires_postgres
def test_cli_run_inesistente_esce_2(db):
    assert rollup_main(["--run-id", "mai_visto",
                        "--db-url", _TEST_DB_URL]) == 2


@requires_postgres
def test_ingredienti_alza_un_errore_di_dominio(db):
    from pipeline.decision_rollup import ingredienti
    with pytest.raises(DecisionRollupError):
        ingredienti(db, "mai_visto")


# Questo test non ha bisogno del database: guarda solo il modulo della politica.
def test_ogni_costante_della_politica_sta_nell_impronta():
    """Nessuna costante entra nella politica senza entrare nell'impronta.

    L'impronta e' una tupla di nomi scritta a mano. Chi domani aggiunge una
    costante al calcolo e si dimentica di elencarla ricrea esattamente il
    difetto che l'impronta e' nata per chiudere: la tabella resta formalmente
    fresca e le rotte servono i numeri vecchi senza dirlo.

    Le tre escluse sono dichiarate qui, una per una, con il motivo. Aggiungere
    un nome a `FUORI` e' una decisione visibile in revisione. Dimenticarsene
    invece non lo era, ed e' il caso che questo test rende impossibile.
    """
    from pipeline import decision

    FUORI = {
        # Quanto e' grande il memo della t di Student. Cambia il tempo di
        # risposta e non puo' cambiare un numero del verdetto.
        "CACHE_T_CDF",
        "CACHE_T_QUANTILE",
        # L'elenco stesso.
        "PARAMETRI_IMPRONTA",
    }

    nel_modulo = {n for n in dir(decision)
                  if n.isupper() and not n.startswith("_")}
    attese = nel_modulo - FUORI
    presenti = set(decision.PARAMETRI_IMPRONTA)

    assert attese - presenti == set(), (
        "costanti della politica fuori dall'impronta: %s. Aggiungerle a "
        "PARAMETRI_IMPRONTA, oppure a FUORI qui sopra con il motivo."
        % sorted(attese - presenti))
    assert presenti - nel_modulo == set(), (
        "PARAMETRI_IMPRONTA nomina costanti che nel modulo non esistono "
        "piu': %s" % sorted(presenti - nel_modulo))
