"""Test della route GET /valves/baseline (riferimento sano per valvola).

Perché esiste la route: lo scostamento di una valvola non è calcolabile in modo
onesto senza un riferimento. Il confronto fra valvole nello stesso istante non lo
sostituisce (alcune valvole sono strutturalmente diverse e risulterebbero
permanentemente anomale), e il confronto interno alla serie recente misura la
derivata del degrado, non il livello.

Contratto verificato qui:
- finestra sana DICHIARATA (query o KV), mai dedotta: senza dichiarazione la
  risposta è 200 con `valves: null` + `degraded: true`, MAI 404 e mai valori
  inventati;
- statistiche per valvola con MRbar (moving range) e limiti XmR
  `media ± 2.66·MRbar`, calcolati in SQL con LAG per valvola;
- `sigma = MRbar / 1.128` (stima XmR con d2 per n=2);
- valvole senza cicli nella finestra → `degraded` con l'elenco.

Isolamento: DB di test privato per processo, come gli altri moduli. Senza
PostgreSQL raggiungibile i test si skippano.
"""
from __future__ import annotations

import math
import os
import secrets
import statistics
from datetime import datetime, timedelta, timezone

import pytest

from .conftest import drop_db_if_ephemeral
from fastapi.testclient import TestClient
from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData,
                        PrimaryKeyConstraint, String, Table, text)

from pipeline.storage import Storage, make_engine

_TEST_DB_URL = os.environ.get(
    "PLCSIM_BASELINE_TEST_DB_URL",
    f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
    f"plcsim_test_baseline_{secrets.token_hex(4)}")


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
    """Crea il DB di test se manca (CREATE DATABASE fuori transazione)."""
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
    """Rimuove il database di test a fine sessione (guardia in conftest.py)."""
    yield
    drop_db_if_ephemeral(_TEST_DB_URL)


def _cycles_metadata() -> MetaData:
    """`cycles` test-locale: le colonne che la route baseline legge davvero."""
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


# Serie deterministica e VERIFICABILE A MANO: valvola 1 alterna 1900/1920 ms,
# quindi media 1910, moving range costante 20 → MRbar = 20 e UCL = 1910 + 53.2.
_FT = [1900, 1920]
_START = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
_END = _START + timedelta(hours=8)
_N = 100


def _seed(engine) -> None:
    m = _cycles_metadata()
    m.drop_all(engine, checkfirst=True)
    m.create_all(engine, checkfirst=True)
    rows = []
    cid = 0
    for valve_id in range(1, 35):          # 34 valvole: la 35 resta SENZA cicli
        for i in range(_N):
            cid += 1
            rows.append({
                "valve_id": valve_id, "cycle_id": cid,
                "event_ts": _START + timedelta(seconds=i * 10),
                "fill_quality_ok": i % 10 != 0,          # rate atteso 0.9
                "diagnostic_status": "SUSPECT" if i % 4 == 0 else "NORMAL",
                "filling_time_ms": _FT[i % 2],
                "tail_time_ms": 300, "tail_pulse": 220,
                "pulse_count": 2500, "delta_pulse": 0, "filling_step_out": 24,
            })
    with engine.begin() as conn:
        conn.execute(m.tables["cycles"].insert(), rows)


@pytest.fixture
def client():
    prev = os.environ.get("PLCSIM_DATABASE_URL")
    _ensure_db()
    os.environ["PLCSIM_DATABASE_URL"] = _TEST_DB_URL
    import pipeline.api as api
    api._store = None                       # storage lazy: forza il DB di test
    _seed(make_engine(_TEST_DB_URL))
    try:
        yield TestClient(api.app)
    finally:
        api._store = None
        if prev is None:
            os.environ.pop("PLCSIM_DATABASE_URL", None)
        else:
            os.environ["PLCSIM_DATABASE_URL"] = prev


@requires_postgres
def test_senza_finestra_degrada_e_non_inventa(client):
    """Nessuna finestra dichiarata → 200 degradato, mai 404, mai valori finti."""
    r = client.get("/valves/baseline")
    assert r.status_code == 200
    b = r.json()
    assert b["valves"] is None
    assert b["degraded"] is True
    assert "finestra" in b["reason"]
    assert b["window"]["source"] == "assente"


@requires_postgres
def test_statistiche_e_limiti_xmr(client):
    """Media, MRbar, sigma e UCL/LCL sulla serie deterministica 1900/1920."""
    r = client.get("/valves/baseline",
                   params={"start": _START.isoformat(), "end": _END.isoformat()})
    assert r.status_code == 200
    b = r.json()
    assert b["window"]["source"] == "query"
    assert b["xmr_k"] == 2.66

    v1 = b["valves"]["1"]
    assert v1["n"] == _N
    ft = v1["filling_time_ms"]

    # media 1910 esatta; MRbar = 20 (moving range costante, 99 transizioni)
    assert ft["mean"] == pytest.approx(1910.0)
    assert ft["mrbar"] == pytest.approx(20.0)
    # sigma = MRbar / d2, d2 = 1.128 per n = 2
    assert ft["sigma"] == pytest.approx(20.0 / 1.128, rel=1e-3)
    # limiti XmR: media +- 2.66 * MRbar
    assert ft["ucl"] == pytest.approx(1910.0 + 2.66 * 20.0)
    assert ft["lcl"] == pytest.approx(1910.0 - 2.66 * 20.0)
    # sigma di popolazione della serie alternata = 10
    assert ft["std"] == pytest.approx(statistics.pstdev(_FT * (_N // 2)))

    # tassi: 90% qualita' buona, 25% SUSPECT
    assert v1["fill_quality_ok_rate"] == pytest.approx(0.9)
    assert v1["diagnostic_suspect_rate"] == pytest.approx(0.25)


@requires_postgres
def test_valvola_senza_cicli_e_dichiarata(client):
    """La valvola 35 non ha cicli: si dichiara nel reason, non si inventa."""
    r = client.get("/valves/baseline",
                   params={"start": _START.isoformat(), "end": _END.isoformat()})
    b = r.json()
    assert "35" not in b["valves"]
    assert b["degraded"] is True
    assert "35" in b["reason"]


@requires_postgres
def test_finestra_vuota_non_produce_baseline(client):
    """Finestra senza cicli → nessuna baseline, motivo esplicito."""
    fuori = _START + timedelta(days=400)
    r = client.get("/valves/baseline",
                   params={"start": fuori.isoformat(),
                           "end": (fuori + timedelta(hours=1)).isoformat()})
    b = r.json()
    assert b["valves"] is None
    assert b["degraded"] is True
    assert "nessun ciclo" in b["reason"]


@requires_postgres
def test_tutti_i_kpi_presenti(client):
    """Ogni KPI dichiarato in `kpi` esiste davvero in ogni valvola."""
    r = client.get("/valves/baseline",
                   params={"start": _START.isoformat(), "end": _END.isoformat()})
    b = r.json()
    for kpi in b["kpi"]:
        for v in b["valves"].values():
            assert kpi in v, f"{kpi} mancante su valvola {v['valve_id']}"
            for campo in ("mean", "std", "p50", "mrbar", "sigma", "ucl", "lcl"):
                assert campo in v[kpi]


@requires_postgres
def test_baseline_non_e_scambiata_per_valve_id(client):
    """`/valves/baseline` non deve finire nel matcher `/valves/{valve_id}`."""
    r = client.get("/valves/baseline")
    assert r.status_code == 200          # 422 significherebbe rotte in ordine errato


# === i numeri che rendono leggibili gli XmR (aggiunte 2026-08-19) ==========
# Il seed ha 100 cicli per valvola, alternati 1900/1920 ms: due blocchi PIENI
# da 46 cicli (23+23 valori per blocco) hanno entrambi media 1910 esatta,
# quindi la sd empirica delle medie a blocchi vale 0.0 — verificabile a mano.

@requires_postgres
def test_sigma_media_46_misurata_non_derivata(client):
    """`sigma_media_46` e' la sd EMPIRICA delle medie a blocchi di 46 cicli.

    Non e' `sigma_full / sqrt(46)`: la serie non e' iid (oscillazione del
    driver con periodo 46 cicli) e quella regola sbaglia di 23x a n=10
    (MISURE-b-c.md sez. b.6). Qui i due blocchi pieni hanno la stessa media
    esatta 1910, quindi la sd misurata e' 0.0 mentre la regola 1/sqrt(n)
    darebbe 10/sqrt(46) = 1.47: il test distingue le due strade.
    """
    b = client.get("/valves/baseline",
                   params={"start": _START.isoformat(),
                           "end": _END.isoformat()}).json()
    ft = b["valves"]["1"]["filling_time_ms"]
    assert ft["n_cicli_di_riferimento"] == 46
    assert ft["sigma_media_46_n_blocchi"] == 2      # 100 cicli -> 2 blocchi pieni
    assert ft["sigma_media_46_reason"] is None
    assert ft["sigma_media_46"] == pytest.approx(0.0)
    # la regola iid darebbe un numero diverso da zero: non e' quella usata
    assert ft["sigma_full"] / math.sqrt(46) == pytest.approx(10 / math.sqrt(46))
    # sigma_full = dispersione del SINGOLO ciclo (lo stesso numero di `std`,
    # sotto il nome che ne dichiara il significato)
    assert ft["sigma_full"] == ft["std"]


@requires_postgres
def test_numeri_xmr_su_tutti_i_kpi(client):
    """I tre numeri che rendono leggibili ucl/lcl esistono per ogni KPI."""
    b = client.get("/valves/baseline",
                   params={"start": _START.isoformat(),
                           "end": _END.isoformat()}).json()
    assert b["n_cicli_di_riferimento"] == 46
    # lo scopo e' DICHIARATO nella risposta, non solo nella docstring
    nota = b["xmr_note"].lower()
    assert "media" in nota and "46" in nota and "singolo ciclo" in nota
    assert "73-78%" in nota
    for kpi in b["kpi"]:
        for v in b["valves"].values():
            for campo in ("sigma_full", "n_cicli_di_riferimento",
                          "sigma_media_46", "sigma_media_46_n_blocchi",
                          "sigma_media_46_reason"):
                assert campo in v[kpi], f"{campo} mancante su {kpi}"


@requires_postgres
def test_sigma_media_46_null_con_motivo_se_i_blocchi_non_bastano(client):
    """Meno di due blocchi pieni: `null` + motivo, mai un numero plausibile.

    Finestra di 10 minuti = 60 cicli per valvola = un solo blocco pieno da 46:
    la dispersione di UNA media non e' misurabile, e la route lo dice.
    """
    fine = _START + timedelta(minutes=10)
    b = client.get("/valves/baseline",
                   params={"start": _START.isoformat(),
                           "end": fine.isoformat()}).json()
    ft = b["valves"]["1"]["filling_time_ms"]
    assert b["valves"]["1"]["n"] == 60
    assert ft["sigma_media_46"] is None
    assert ft["sigma_media_46_n_blocchi"] == 1
    assert "2 blocchi pieni" in ft["sigma_media_46_reason"]
    # gli altri numeri restano serviti: manca solo cio' che non e' misurabile
    assert ft["mean"] is not None and ft["ucl"] is not None


@requires_postgres
def test_n_cicli_per_valvola_e_mediana_dei_conteggi(client):
    """Il campo che la dashboard cita in chiaro esiste ed e' coerente.

    `a/pagina.js` scrive "base della valvola N su {n_cicli_per_valvola} cicli
    sani": senza il campo la pagina mostrava un trattino. Deve essere la
    mediana dei conteggi per valvola, con min/max accanto perche' la
    dispersione non resti nascosta.
    """
    r = client.get("/valves/baseline")
    assert r.status_code == 200
    d = r.json()
    for k in ("n_cicli_per_valvola", "n_cicli_per_valvola_min",
              "n_cicli_per_valvola_max"):
        assert k in d, k
    if d.get("valves"):
        conteggi = sorted(v["n"] for v in d["valves"].values())
        meta = len(conteggi) // 2
        attesa = (conteggi[meta] if len(conteggi) % 2
                  else (conteggi[meta - 1] + conteggi[meta]) // 2)
        assert d["n_cicli_per_valvola"] == attesa
        assert d["n_cicli_per_valvola_min"] == conteggi[0]
        assert d["n_cicli_per_valvola_max"] == conteggi[-1]
    else:
        # nessuna finestra dichiarata: mai un default, sempre None
        assert d["n_cicli_per_valvola"] is None
