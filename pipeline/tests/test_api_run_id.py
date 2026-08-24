"""Test del filtro di run nell'API (`cycles.run_id`, 2026-08-19).

## Perché esiste

`cycle_id` riparte da 1 a ogni run del simulatore e i run **si sovrappongono
nel tempo di parete**: né la coppia `(valve_id, cycle_id)` né un filtro su
`event_ts` distinguono due run nello stesso database. Una query senza filtro di
run non produce un errore — produce **un numero plausibile e sbagliato**:

- `_count_cycles` somma i cicli dei due run caduti nella stessa finestra
  (Performance e Quality di macchina fuori scala);
- `DISTINCT ON (valve_id) ORDER BY cycle_id DESC` restituisce il ciclo del run
  **più lungo**, non del più recente;
- e soprattutto le finestre analitiche (`LAG`, `ROW_NUMBER`) scorrono
  **attraverso la giunzione** fra i due run: nasce un moving range
  `|x[i] - x[i-1]|` che non corrisponde ad alcuna transizione fisica, `MRbar`
  si gonfia, `UCL/LCL = media ± 2.66·MRbar` si allargano e la baseline diventa
  più **permissiva** — cioè smette di segnalare guasti veri.

## La prova di non-contaminazione

Ogni test qui ha la stessa forma: si misura con **un solo run** in tabella, si
inserisce un secondo run con la **stessa finestra temporale** e valori
volutamente estremi, si rimisura e si pretende **lo stesso numero**. Il secondo
run è costruito perché la contaminazione, se ci fosse, sarebbe impossibile da
non vedere (MRbar passerebbe da 20 a migliaia).

`test_il_filtro_non_e_vacuo` chiude il cerchio: chiedendo esplicitamente il
secondo run i numeri **devono** cambiare — altrimenti il test di
non-contaminazione sarebbe verde anche con un filtro che non filtra nulla.

Isolamento: DB effimero per processo (prefisso `plcsim_test_run_`, guardia in
`conftest.py`). Senza PostgreSQL raggiungibile i test si skippano. Non si
scrive MAI nel database operazionale `plcsim`.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from pipeline.cycles_storage import CURRENT_RUN_ID_KEY, CyclesStorage
from pipeline.storage import Storage, make_engine

from .conftest import drop_db_if_ephemeral

_TEST_DB_URL = os.environ.get(
    "PLCSIM_RUNID_TEST_DB_URL",
    f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
    f"plcsim_test_run_{secrets.token_hex(4)}")

RUN_A = "run_a"
RUN_B = "run_b"

# Serie verificabile a mano: valvola k alterna 1900/1920 ms → media 1910,
# moving range costante 20 ⇒ MRbar = 20, UCL = 1910 + 53.2.
_FT_A = [1900, 1920]
# Run B: valori estremi e nella STESSA finestra temporale, con gli STESSI
# cycle_id. Se contaminasse, MRbar salirebbe di tre ordini di grandezza.
_FT_B = [100, 9000]

_START = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
_END = _START + timedelta(hours=8)
_N = 100
_VALVOLE = (1, 2, 3)


def _admin_url() -> str:
    return "postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim"


def _pg_available() -> bool:
    try:
        return Storage(make_engine(_admin_url())).ping()
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _pg_available(), reason="PostgreSQL non raggiungibile")


def _ensure_db() -> None:
    name = _TEST_DB_URL.rsplit("/", 1)[-1]
    eng = make_engine(_admin_url())
    with eng.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        if not conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"),
                            {"n": name}).first():
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def _pulizia_db_effimero():
    yield
    drop_db_if_ephemeral(_TEST_DB_URL)


def _righe(run_id: str, ft: list[int]) -> list[dict]:
    """Cicli di un run — stessa finestra e stessi cycle_id per ogni run."""
    rows = []
    for valve_id in _VALVOLE:
        for i in range(_N):
            rows.append({
                "run_id": run_id,
                "machine_id": "V3",
                "valve_id": valve_id,
                "cycle_id": i + 1,
                "event_ts": _START + timedelta(seconds=i * 10),
                "source_ts": None, "ingest_ts": None,
                "fill_quality_ok": i % 10 != 0,       # rate atteso 0.9
                "diagnostic_status": "SUSPECT" if i % 4 == 0 else "NORMAL",
                "close_reason": None,
                "filling_time_ms": ft[i % 2],
                "tail_time_ms": 300, "tail_pulse": 220, "pulse_count": 2500,
                "target": 2500, "delta_pulse": 0, "filling_step_out": 24,
                "filling_ok": True, "sequence_ok": True, "sample_valid": True,
                "position_limit": False, "filling_overtime": False,
            })
    return rows


@pytest.fixture
def ctx():
    """DB pulito con lo schema REALE di `cycles` (run_id, PK a 3 colonne).

    Espone `(client, storage, cycles_storage)`. Parte con il solo RUN_A e la
    chiave KV `current_run_id` valorizzata.
    """
    prev = os.environ.get("PLCSIM_DATABASE_URL")
    _ensure_db()
    os.environ["PLCSIM_DATABASE_URL"] = _TEST_DB_URL
    import pipeline.api as api
    api._store = None
    s = Storage(make_engine(_TEST_DB_URL))
    s.metadata.drop_all(s.engine, checkfirst=True)
    s.init()
    cs = CyclesStorage(s.engine)
    cs.drop_all()
    cs.init()
    cs.bulk_insert(_righe(RUN_A, _FT_A))
    s.set_machine_state(CURRENT_RUN_ID_KEY, RUN_A)
    try:
        yield TestClient(api.app), s, cs
    finally:
        api._store = None
        if prev is None:
            os.environ.pop("PLCSIM_DATABASE_URL", None)
        else:
            os.environ["PLCSIM_DATABASE_URL"] = prev


def _aggiungi_run_b(cs: CyclesStorage) -> None:
    assert cs.bulk_insert(_righe(RUN_B, _FT_B)) == len(_VALVOLE) * _N


def _baseline(client, **extra):
    p = {"start": _START.isoformat(), "end": _END.isoformat(), **extra}
    r = client.get("/valves/baseline", params=p)
    assert r.status_code == 200
    return r.json()


def _oee(client, **extra):
    r = client.get("/machine/oee",
                   params={"window": "day", "at": _END.isoformat(), **extra})
    assert r.status_code == 200
    return r.json()


# === non-contaminazione =====================================================

@requires_postgres
def test_baseline_mrbar_invariata_con_un_secondo_run(ctx):
    """`MRbar` con due run in tabella == `MRbar` con un run solo.

    È il test centrale: se `LAG` scorresse fra i due run, il salto
    1920→100 alla giunzione porterebbe MRbar da 20 a ~1000 e allargherebbe
    UCL/LCL, rendendo la baseline più permissiva.
    """
    client, s, cs = ctx
    prima = _baseline(client)
    ft_prima = prima["valves"]["1"]["filling_time_ms"]
    assert ft_prima["mrbar"] == pytest.approx(20.0)
    assert ft_prima["mean"] == pytest.approx(1910.0)
    assert ft_prima["ucl"] == pytest.approx(1910.0 + 2.66 * 20.0)

    _aggiungi_run_b(cs)
    dopo = _baseline(client)
    assert dopo["valves"] == prima["valves"]
    assert dopo["window"]["run_id"] == RUN_A
    assert dopo["n_cicli_per_valvola"] == prima["n_cicli_per_valvola"]


@requires_postgres
def test_sigma_media_46_invariata_con_un_secondo_run(ctx):
    """Anche `ROW_NUMBER` (blocchi da 46) deve partizionare per run."""
    client, s, cs = ctx
    prima = _baseline(client)["valves"]["1"]["filling_time_ms"]
    _aggiungi_run_b(cs)
    dopo = _baseline(client)["valves"]["1"]["filling_time_ms"]
    assert dopo["sigma_media_46_n_blocchi"] == prima["sigma_media_46_n_blocchi"]
    assert dopo["sigma_media_46"] == prima["sigma_media_46"]


@requires_postgres
def test_conteggi_e_oee_invariati_con_un_secondo_run(ctx):
    """`_count_cycles` (e quindi Quality/Performance) non somma i due run."""
    client, s, cs = ctx
    prima = _oee(client)
    assert prima["source"]["cycles_rows"] == len(_VALVOLE) * _N
    assert prima["source"]["run_id"] == RUN_A
    _aggiungi_run_b(cs)
    dopo = _oee(client)
    assert dopo["source"]["cycles_rows"] == prima["source"]["cycles_rows"]
    assert dopo["quality"] == prima["quality"]
    assert dopo["quality_detail"]["good"] == prima["quality_detail"]["good"]
    assert dopo["quality_detail"]["total"] == prima["quality_detail"]["total"]


@requires_postgres
def test_serie_kpi_e_last_kpi_invariati_con_un_secondo_run(ctx):
    """Serie per ciclo e ultimo KPI restano quelli del run corrente."""
    client, s, cs = ctx
    prima = client.get("/valves/1/kpi", params={"limit": 5}).json()
    prima_v = client.get("/valves").json()["valves"]["1"]["last_kpi"]
    _aggiungi_run_b(cs)
    dopo = client.get("/valves/1/kpi", params={"limit": 5}).json()
    assert dopo["series"] == prima["series"]
    assert dopo["run_id"] == RUN_A
    assert all(r["filling_time_ms"] in _FT_A for r in dopo["series"])
    dopo_v = client.get("/valves").json()
    assert dopo_v["valves"]["1"]["last_kpi"] == prima_v
    assert dopo_v["run_id"] == RUN_A


@requires_postgres
def test_ancora_della_serie_oee_e_del_run(ctx):
    """`_first_cycle_ts` è l'ancora della camminata: deve essere del run."""
    client, s, cs = ctx
    _aggiungi_run_b(cs)
    with s.engine.begin() as conn:
        conn.execute(text("UPDATE cycles SET event_ts = event_ts "
                          "- interval '10 days' WHERE run_id = :r"),
                     {"r": RUN_B})
    meta = client.get("/machine/oee/series",
                      params={"at": _END.isoformat(), "windows": "day"}).json()["__meta"]
    assert meta["run_id"] == RUN_A
    assert meta["primo_ciclo_reale"] == _START.isoformat()


# === il filtro non è vacuo ==================================================

@requires_postgres
def test_il_filtro_non_e_vacuo(ctx):
    """Chiedendo esplicitamente RUN_B i numeri DEVONO cambiare.

    Senza questo, un filtro che non filtra nulla passerebbe i test di
    non-contaminazione.
    """
    client, s, cs = ctx
    _aggiungi_run_b(cs)
    a = _baseline(client, run_id=RUN_A)["valves"]["1"]["filling_time_ms"]
    b = _baseline(client, run_id=RUN_B)["valves"]["1"]["filling_time_ms"]
    assert a["mrbar"] == pytest.approx(20.0)
    assert b["mrbar"] == pytest.approx(8900.0)      # |9000-100|, costante
    assert b["mean"] == pytest.approx(4550.0)
    serie = client.get("/valves/1/kpi",
                       params={"limit": 5, "run_id": RUN_B}).json()
    assert serie["run_id"] == RUN_B
    assert all(r["filling_time_ms"] in _FT_B for r in serie["series"])


# === run ambiguo ============================================================

def _rendi_ambiguo(s: Storage, cs: CyclesStorage) -> None:
    _aggiungi_run_b(cs)
    with s.engine.begin() as conn:
        conn.execute(text("DELETE FROM machine_state WHERE key = :k"),
                     {"k": CURRENT_RUN_ID_KEY})


@requires_postgres
def test_run_ambiguo_degrada_mai_500(ctx):
    """Due run e nessuno indicato → 200 degradato ovunque, mai un 500.

    Coerente con `/valves/baseline` senza finestra dichiarata e con
    `/machine/oee` senza cicli: la forma di degrado del progetto è 200 +
    `degraded: true` + reason leggibile, non un errore.
    """
    client, s, cs = ctx
    _rendi_ambiguo(s, cs)

    b = client.get("/valves/baseline",
                   params={"start": _START.isoformat(), "end": _END.isoformat()})
    assert b.status_code == 200
    assert b.json()["valves"] is None and b.json()["degraded"] is True
    assert "run non determinato" in b.json()["reason"]

    o = client.get("/machine/oee",
                   params={"window": "day", "at": _END.isoformat()})
    assert o.status_code == 200
    src = o.json()["source"]
    assert src["degraded"] is True and src["run_id"] is None
    assert "run non determinato" in src["reason"]
    assert o.json()["quality"] is None      # mai un numero su due run

    k = client.get("/valves/1/kpi")
    assert k.status_code == 200
    assert k.json()["series"] == [] and k.json()["degraded"] is True

    v = client.get("/valves")
    assert v.status_code == 200
    assert v.json()["kpi_degraded"] is True
    assert all(x["last_kpi"] is None for x in v.json()["valves"].values())

    ser = client.get("/machine/oee/series",
                     params={"at": _END.isoformat(), "windows": "day"})
    assert ser.status_code == 200
    assert "run non determinato" in ser.json()["__meta"]["run_reason"]


@requires_postgres
def test_run_ambiguo_risolto_da_run_id_esplicito(ctx):
    """L'ambiguità si scioglie indicando il run, su ogni route."""
    client, s, cs = ctx
    _rendi_ambiguo(s, cs)
    b = _baseline(client, run_id=RUN_A)
    assert b["degraded"] is False or "valvole" in (b["reason"] or "")
    assert b["window"]["run_id"] == RUN_A
    assert b["valves"]["1"]["filling_time_ms"]["mrbar"] == pytest.approx(20.0)
    o = _oee(client, run_id=RUN_A)
    assert o["source"]["cycles_rows"] == len(_VALVOLE) * _N
    assert o["source"]["run_id"] == RUN_A


@requires_postgres
def test_baseline_window_kv_porta_il_run(ctx):
    """Il KV `baseline_window` è `{run_id, start, end}` e scioglie l'ambiguità.

    Il riferimento sano è un RUN: i run si sovrappongono nel tempo di parete,
    quindi start/end da soli non identificano nulla.
    """
    client, s, cs = ctx
    _rendi_ambiguo(s, cs)
    s.set_machine_state("baseline_window", {
        "run_id": RUN_B, "start": _START.isoformat(), "end": _END.isoformat()})
    b = client.get("/valves/baseline").json()
    assert b["window"]["source"] == "kv"
    assert b["window"]["run_id"] == RUN_B
    assert b["valves"]["1"]["filling_time_ms"]["mrbar"] == pytest.approx(8900.0)


@requires_postgres
def test_baseline_window_kv_vecchio_resta_valido(ctx):
    """Retrocompatibilità: un KV `{start, end}` senza run_id continua a
    funzionare, risolvendo il run da `current_run_id`."""
    client, s, cs = ctx
    _aggiungi_run_b(cs)                      # KV current_run_id resta = RUN_A
    s.set_machine_state("baseline_window", {
        "start": _START.isoformat(), "end": _END.isoformat()})
    b = client.get("/valves/baseline").json()
    assert b["window"]["source"] == "kv"
    assert b["window"]["run_id"] == RUN_A
    assert b["valves"]["1"]["filling_time_ms"]["mrbar"] == pytest.approx(20.0)
