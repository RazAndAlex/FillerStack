"""Test della baseline MEMORIZZATA (KV `baseline_cache`).

Perché esiste: la finestra sana è DICHIARATA e CONGELATA, quindi la baseline è
un fatto che si calcola una volta sola. Sui dati reali il calcolo costa ~136 s
su 6,6 M cicli e la dashboard lo pagava a ogni caricamento di pagina.

Il contratto verificato qui è la CHIAVE DI VALIDITÀ, non la velocità:
- calcolata la prima volta (`cached: false`), riservita dopo (`cached: true`)
  con numeri IDENTICI campo per campo;
- una finestra diversa NON riusa nulla: ricalcola e serve i numeri della
  finestra chiesta;
- un KV memorizzato con una chiave che non combacia viene IGNORATO — è la
  garanzia che l'API non serva mai i numeri di un'altra finestra;
- `?refresh=1` ricalcola e riscrive anche a chiave valida.

Isolamento: DB di test privato per processo, come gli altri moduli. Senza
PostgreSQL raggiungibile i test si skippano. Non si scrive mai nel DB `plcsim`.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData,
                        PrimaryKeyConstraint, String, Table, text)

from pipeline.storage import Storage, make_engine

from .conftest import drop_db_if_ephemeral

_TEST_DB_URL = os.environ.get(
    "PLCSIM_BASELINE_CACHE_TEST_DB_URL",
    f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
    f"plcsim_test_baseline_cache_{secrets.token_hex(4)}")


def _admin_url() -> str:
    return "postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim"


def _pg_available() -> bool:
    try:
        return Storage(make_engine(_admin_url())).ping()
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL non raggiungibile (avvia `docker compose up -d postgres`)")


def _ensure_db() -> None:
    name = _TEST_DB_URL.rsplit("/", 1)[-1]
    eng = make_engine(_admin_url())
    with eng.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"),
            {"n": name}).first()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def _pulizia_db_effimero():
    yield
    drop_db_if_ephemeral(_TEST_DB_URL)


def _cycles_metadata() -> MetaData:
    m = MetaData()
    Table("cycles", m,
          Column("valve_id", Integer, nullable=False),
          Column("cycle_id", Integer, nullable=False),
          Column("event_ts", DateTime(timezone=True), nullable=True),
          Column("fill_quality_ok", Boolean, nullable=True),
          Column("diagnostic_status", String, nullable=True),
          Column("filling_time_ms", Integer, nullable=True),
          Column("tail_time_ms", Integer, nullable=True),
          Column("tail_pulse", Integer, nullable=True),
          Column("pulse_count", Integer, nullable=True),
          Column("delta_pulse", Integer, nullable=True),
          Column("filling_step_out", Integer, nullable=True),
          PrimaryKeyConstraint("valve_id", "cycle_id"))
    return m


# Due finestre CONTIGUE con numeri diversi: la prima ora alterna 1900/1920 ms
# (media 1910), la seconda 1000/1020 (media 1010). Servire i numeri della
# finestra sbagliata è quindi visibile a occhio, non una differenza di terza
# cifra decimale.
_START = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
_MEZZO = _START + timedelta(hours=1)
_END = _START + timedelta(hours=2)
_N = 120                        # 60 cicli per finestra, 10 s l'uno


def _seed(engine) -> None:
    m = _cycles_metadata()
    m.drop_all(engine, checkfirst=True)
    m.create_all(engine, checkfirst=True)
    rows = []
    cid = 0
    for valve_id in range(1, 36):
        for i in range(_N):
            cid += 1
            prima = i < _N // 2
            rows.append({
                "valve_id": valve_id, "cycle_id": cid,
                "event_ts": _START + timedelta(seconds=i * 60),
                "fill_quality_ok": True,
                "diagnostic_status": "NORMAL",
                "filling_time_ms": (1900 if prima else 1000) + (i % 2) * 20,
                "tail_time_ms": 300, "tail_pulse": 220,
                "pulse_count": 2500, "delta_pulse": 0, "filling_step_out": 24,
            })
    with engine.begin() as conn:
        conn.execute(m.tables["cycles"].insert(), rows)


@pytest.fixture
def ambiente():
    """Client + Storage sullo stesso DB di test, con le tabelle KV create.

    `Storage.init()` serve: senza `machine_state` la memorizzazione si spegne
    da sola (per progetto) e i test non misurerebbero nulla.
    """
    prev = os.environ.get("PLCSIM_DATABASE_URL")
    _ensure_db()
    os.environ["PLCSIM_DATABASE_URL"] = _TEST_DB_URL
    import pipeline.api as api
    api._store = None
    eng = make_engine(_TEST_DB_URL)
    st = Storage(eng)
    st.init()
    _seed(eng)
    # Nessun residuo fra un test e l'altro: ogni test parte senza memorizzato.
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM machine_state WHERE key = 'baseline_cache'"))
    # La finestra DICHIARATA è la prima ora: è quella che si memorizza.
    st.set_machine_state("baseline_window",
                         {"start": _START.isoformat(), "end": _MEZZO.isoformat()})
    try:
        yield TestClient(api.app), st
    finally:
        api._store = None
        eng.dispose()
        if prev is None:
            os.environ.pop("PLCSIM_DATABASE_URL", None)
        else:
            os.environ["PLCSIM_DATABASE_URL"] = prev


def _get(client, **extra):
    """La finestra dichiarata (KV), come la chiede la dashboard."""
    r = client.get("/valves/baseline", params=extra)
    assert r.status_code == 200
    return r.json()


def _get_esplicita(client, start, end, **extra):
    """Una finestra passata a mano: calcolata sempre, non memorizzata."""
    r = client.get("/valves/baseline", params={
        "start": start.isoformat(), "end": end.isoformat(), **extra})
    assert r.status_code == 200
    return r.json()


def _senza_meta(b: dict) -> dict:
    return {k: v for k, v in b.items() if k not in ("cached", "computed_at")}


@requires_postgres
def test_prima_calcolata_poi_memorizzata_e_identica(ambiente):
    """Seconda chiamata sulla stessa finestra: memorizzata e IDENTICA."""
    client, _ = ambiente
    primo = _get(client)
    assert primo["cached"] is False
    assert primo["computed_at"] is not None
    secondo = _get(client)
    assert secondo["cached"] is True
    # Identità campo per campo: 35 valvole × tutti i KPI, non solo un campione.
    assert _senza_meta(secondo) == _senza_meta(primo)
    assert len(secondo["valves"]) == 35
    assert secondo["valves"]["1"]["filling_time_ms"]["mean"] == 1910.0


@requires_postgres
def test_finestra_diversa_non_riusa_il_memorizzato(ambiente):
    """Chiave diversa → si ricalcola, e i numeri sono quelli chiesti."""
    client, _ = ambiente
    primo = _get(client)
    assert primo["valves"]["1"]["filling_time_ms"]["mean"] == 1910.0
    secondo = _get_esplicita(client, _MEZZO, _END)
    assert secondo["cached"] is False
    assert secondo["valves"]["1"]["filling_time_ms"]["mean"] == 1010.0
    assert secondo["window"]["start"] != primo["window"]["start"]


@requires_postgres
def test_chiave_che_non_combacia_e_ignorata(ambiente):
    """Un KV memorizzato per un'ALTRA finestra non viene mai servito.

    È la garanzia centrale: il payload piantato qui contiene numeri
    riconoscibilmente falsi. Se comparissero nella risposta, l'API starebbe
    servendo i numeri di una finestra diversa da quella richiesta.
    """
    client, st = ambiente
    falso = {"valve_id": 1, "n": 1,
             "filling_time_ms": {"mean": -999.0, "std": 0.0}}
    st.set_machine_state("baseline_cache", {
        "key": {"run_id": None,
                "start": (_START - timedelta(days=30)).isoformat(),
                "end": (_MEZZO - timedelta(days=30)).isoformat()},
        "computed_at": "2000-01-01T00:00:00+00:00",
        "payload": {"valves": {"1": falso}, "degraded": False,
                    "reason": "PAYLOAD DI UN'ALTRA FINESTRA"},
    })
    b = _get(client)
    assert b["cached"] is False
    assert b["reason"] != "PAYLOAD DI UN'ALTRA FINESTRA"
    assert b["valves"]["1"]["filling_time_ms"]["mean"] == 1910.0
    assert len(b["valves"]) == 35


@requires_postgres
def test_refresh_ricalcola_anche_a_chiave_valida(ambiente):
    """`?refresh=1` è la rigenerazione esplicita: ricalcola e riscrive."""
    client, st = ambiente
    primo = _get(client)
    assert _get(client)["cached"] is True   # memorizzata
    rigenerata = _get(client, refresh="true")
    assert rigenerata["cached"] is False
    assert _senza_meta(rigenerata) == _senza_meta(primo)
    assert rigenerata["computed_at"] > primo["computed_at"]
    # Riscritta davvero: il KV porta il timestamp nuovo.
    kv = st.get_machine_state("baseline_cache")
    assert kv["computed_at"] == rigenerata["computed_at"]
    assert kv["key"]["start"] == primo["window"]["start"]
    assert kv["key"]["end"] == primo["window"]["end"]


@requires_postgres
def test_il_kv_dichiara_la_chiave_che_ha_prodotto_i_numeri(ambiente):
    """Il memorizzato porta con sé run + finestra, non una chiave qualsiasi."""
    client, st = ambiente
    b = _get(client)
    kv = st.get_machine_state("baseline_cache")
    assert kv["key"] == {"run_id": b["window"]["run_id"],
                         "start": b["window"]["start"],
                         "end": b["window"]["end"]}


@requires_postgres
def test_una_finestra_ad_hoc_non_sfratta_il_riferimento_congelato(ambiente):
    """Lo slot memorizzato è uno e appartiene alla finestra DICHIARATA.

    Senza questa regola una singola query esplorativa lascerebbe la dashboard
    a ricalcolare 136 s a ogni pagina per il resto della giornata.
    """
    client, st = ambiente
    dichiarata = _get(client)
    prima = st.get_machine_state("baseline_cache")
    assert prima["key"]["end"] == dichiarata["window"]["end"]

    adhoc = _get_esplicita(client, _MEZZO, _END)
    assert adhoc["cached"] is False
    assert adhoc["computed_at"] is None        # calcolata, non memorizzata
    assert st.get_machine_state("baseline_cache") == prima   # slot intatto

    ancora = _get(client)
    assert ancora["cached"] is True
    assert _senza_meta(ancora) == _senza_meta(dichiarata)


@requires_postgres
def test_senza_finestra_non_memorizza_nulla(ambiente):
    """Nessuna finestra dichiarata → degrado, e nessun KV scritto."""
    client, st = ambiente
    with st.engine.begin() as conn:
        conn.execute(
            text("DELETE FROM machine_state WHERE key = 'baseline_window'"))
    b = client.get("/valves/baseline").json()
    assert b["valves"] is None and b["degraded"] is True
    assert b["cached"] is False
    assert st.get_machine_state("baseline_cache") is None
