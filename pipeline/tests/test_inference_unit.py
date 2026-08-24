"""Unit test del consumer inference M9 (ADR-0020) — prediction record + versione + storage.

Copertura (spec M9 §7):
- T2: prediction record conforme a prediction-v1.json (build_prediction
  valida; mutazioni rifiutate: predicted_label fuori vocabolario, anomaly_score
  clamp, valve_id fuori range).
- T0: model_version + feature_schema_version obbligatori (build_prediction
  li propaga; inference li risolve da sidecar/manifest, mai None).
- persistenza storage (M10 ADR-0021): dedup su prediction_id (idempotente),
  watermark (window_end_cycle_id già presente → nessun duplicato logico).
- run() end-to-end su raw sintetico: raw → feature → prediction → storage.

Il backend predictions è PostgreSQL (M10). I test che toccano il DB sono
marcati `postgres` e skippati senza un server raggiungibile; i test sul
contratto di prediction (build_prediction/validate) girano sempre.

Il test end-to-end usa un DB di test PRIVATO (`plcsim_test_hermetic`, usato
solo da questo test) con drop_all in setup e teardown che rimuove le proprie
righe: nessuna dipendenza dall'ordine di esecuzione né da scrittori esterni
(fix F2 — prima: `plcsim_test` condiviso e sporcato da terzi → il test
falliva al secondo run).

Nessun Docker/broker per i test non-DB; modello reale Track D
(work/ml_dataset/model) se presente, altrimenti skip del test end-to-end.
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.prediction_schema import (  # noqa: E402
    PREDICTION_LABELS,
    build_prediction,
    validate_prediction,
)
from jsonschema import ValidationError  # noqa: E402


# ---------------------------------------------------------------------------
# T2 — prediction record
# ---------------------------------------------------------------------------
def _probs():
    return {"healthy": 0.8, "restriction": 0.05, "closing_delay": 0.05,
            "opening_delay": 0.02, "pressure_instability": 0.03,
            "flowmeter_dropout": 0.02, "flowmeter_glitch": 0.03}


def test_prediction_record_valid():
    rec = build_prediction(
        machine_code="valve0", window_idx=0, window_end_cycle_id=50,
        predicted_label="healthy", probabilities=_probs(),
        feature_vector=[0.0] * 43,
        model_version="v1", feature_schema_version="ML-F1")
    validate_prediction(rec)  # non alza
    assert rec["anomaly_score"] == pytest.approx(0.2, abs=1e-9)
    assert rec["valve_id"] == 1
    assert len(rec["feature_fingerprint"]) == 64


def test_prediction_rejects_bad_label():
    with pytest.raises(ValueError):
        build_prediction(
            machine_code="valve0", window_idx=0, window_end_cycle_id=50,
            predicted_label="not-a-label", probabilities=_probs(),
            feature_vector=[0.0] * 43,
            model_version="v1", feature_schema_version="ML-F1")


def test_prediction_rejects_bad_valve():
    with pytest.raises(ValueError):
        build_prediction(
            machine_code="valve99", window_idx=0, window_end_cycle_id=50,
            predicted_label="healthy", probabilities=_probs(),
            feature_vector=[0.0] * 43,
            model_version="v1", feature_schema_version="ML-F1")


def test_anomaly_score_clamped_to_unit():
    # P(healthy)=0 → anomaly 1; feature_vector vuoto ok nel build (fingerprint)
    rec = build_prediction(
        machine_code="valve0", window_idx=0, window_end_cycle_id=50,
        predicted_label="healthy", probabilities={"healthy": 0.0, "restriction": 1.0},
        feature_vector=[0.0] * 43,
        model_version="v1", feature_schema_version="ML-F1")
    assert rec["anomaly_score"] == 1.0


def test_score_history_exclusions_use_prediction_identity():
    from pipeline import inference

    assert inference._score_history_exclusions([
        {"prediction_id": "00000000-0000-0000-0000-000000000001"},
        {"prediction_id": "00000000-0000-0000-0000-000000000002"},
    ]) == [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    ]


def test_alert_seed_excludes_already_persisted_current_batch(monkeypatch):
    """Il wiring non deve mettere il lotto corrente nel seed e nel process."""
    from pipeline import inference
    from pipeline.alert import AlertConfig, AlertEngine

    config = AlertConfig(score_aggregation_window=3,
                         score_aggregation_required=2)
    persisted = {
        98: True,
        99: False,
        100: False,
        101: True,
        102: False,
    }
    observed: dict[str, object] = {}
    persisted_events = []

    current_ids = [
        "00000000-0000-0000-0000-000000000101",
        "00000000-0000-0000-0000-000000000102",
    ]

    def load_history(_storage, _config, *, excluded_prediction_ids,
                     run_id=None):
        observed["excluded"] = excluded_prediction_ids
        values = [persisted[wcid] for wcid in (98, 99, 100)]
        return {21: deque(values[-3:], maxlen=3)}

    def persist_events(events, _storage, _run_id=None):
        persisted_events.extend(events)
        return len(events)

    monkeypatch.setattr(
        inference,
        "_alert_helpers",
        lambda: (AlertEngine, lambda **_kwargs: config, persist_events,
                 lambda _storage, _config, _run_id=None: {}),
    )
    monkeypatch.setattr(inference, "_alert_score_history_loader",
                        lambda: load_history)

    consumer = object.__new__(inference.InferenceConsumer)
    consumer.healthy_label = "healthy"
    # run esplicito: la cronologia allarmi è per run, e questo harness non
    # ha un KV `current_run_id` da cui risolverlo.
    consumer._run_id = "run_di_prova"
    storage = object()
    consumer._storage = lambda: storage
    current_batch = [
        {"prediction_id": current_ids[0], "valve_id": 21,
         "predicted_label": "healthy", "anomaly_score": 0.9,
         "window_end_cycle_id": 101, "prediction_ts": "2026-08-13T00:01:41Z"},
        {"prediction_id": current_ids[1], "valve_id": 21,
         "predicted_label": "healthy", "anomaly_score": 0.1,
         "window_end_cycle_id": 102, "prediction_ts": "2026-08-13T00:01:42Z"},
    ]

    consumer._process_alert_transitions(current_batch)

    assert observed["excluded"] == current_ids
    assert persisted_events == []


def test_prediction_requires_model_version():
    # model_version None non è consentito dallo schema (minLength 1 string)
    from pipeline.prediction_schema import _validator
    rec = build_prediction(
        machine_code="valve0", window_idx=0, window_end_cycle_id=50,
        predicted_label="healthy", probabilities=_probs(),
        feature_vector=[0.0] * 43,
        model_version="v1", feature_schema_version="ML-F1")
    rec["model_version"] = ""
    with pytest.raises(ValidationError):
        validate_prediction(rec)


# ---------------------------------------------------------------------------
# T0 + persistenza — inference end-to-end (richiede modello Track D + Postgres)
# ---------------------------------------------------------------------------
def _pg_available() -> bool:
    try:
        from pipeline.storage import Storage, make_engine
        return Storage(make_engine()).ping()
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _pg_available(), reason="PostgreSQL non raggiungibile (avvia `docker compose up -d postgres`)")


# DB di test PRIVATO e HERMETICO del test end-to-end: nome dedicato
# (`plcsim_test_hermetic`), NON il DB condiviso `plcsim_test` (fix F2: un
# writer esterno intermittente sporcava `plcsim_test` → il test falliva a
# DB "sporco" e dipendeva dall'ordine di esecuzione).
_TEST_DB_URL = (
    "postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim_test_hermetic"
)

# Finestre attese: 200 cicli valve0 → 4 finestre da 50 (window_end 50/100/150/200)
_EXPECTED_WINDOWS = {50, 100, 150, 200}


def _ensure_test_database(url: str) -> None:
    """Self-service: crea il DB di test PRIVATO se non esiste ancora.

    `plcsim_test_hermetic` è usato SOLO da questo test: crearlo qui (via DB
    amministrativo `postgres`) rende il test indipendente dal setup manuale
    e dall'ordine di esecuzione della suite.
    """
    from sqlalchemy import create_engine, text
    dbname = url.rsplit("/", 1)[-1]
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    eng = create_engine(admin_url, connect_args={"connect_timeout": 3})
    try:
        with eng.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": dbname}).scalar()
        if not exists:
            with eng.connect().execution_options(
                    isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        eng.dispose()


@requires_postgres
def test_inference_run_end_to_end(tmp_path):
    model = Path("work/ml_dataset/model/model.joblib")
    if not model.exists():
        pytest.skip("modello Track D assente")
    from pipeline.inference import InferenceConsumer
    from pipeline.storage import Storage, make_engine
    from sqlalchemy import delete, func, select

    # raw sintetico v1.3 (200 cicli valve0 dal bulk healthy, se disponibile)
    bulk_path = Path("work/m4_healthy_1d/valve_cycles.parquet")
    if not bulk_path.exists():
        pytest.skip("bulk healthy run assente")
    bulk = pl.read_parquet(bulk_path)
    v0 = bulk.filter(pl.col("machine_code") == "valve0").sort("cycle_id").head(200)

    raw = pl.DataFrame({
        "schema_version": ["1.3"] * v0.height,
        "event_id": [f"00000000-0000-0000-0000-{i:012d}"
                     for i in range(v0.height)],
        # event_ts: colonna canonica del raw (`ingest.FLATTENED_COLUMNS`) —
        # è il tempo del DATO, e da esso deriva `prediction_ts`. Prima era
        # omessa da questa fixture: raw sintetico non conforme al layout reale.
        "event_ts": [t.isoformat() for t in v0["ts_beg"].to_list()],
        "machine_id": ["filler01"] * v0.height,
        "cycle_id": v0["cycle_id"].to_list(),
        "valve_id": [1] * v0.height,
        "data.filling_time_ms": v0["fillingtime"].to_list(),
        "data.tail_time_ms": v0["tailtime"].to_list(),
        "data.tail_pulse": v0["tailpulse"].to_list(),
        "data.pulse_count": v0["pulsecount"].to_list(),
        "data.target": v0["target"].to_list(),
        "data.delta_pulse": v0["deltapulse"].to_list(),
        "data.filling_step_out": v0["filling_step_out"].to_list(),
        "data.filling_ok": v0["fillingok"].to_list(),
        "data.fill_quality_ok": v0["fill_quality_ok"].to_list(),
        "data.sequence_ok": v0["sequence_ok"].to_list(),
        "data.sample_valid": v0["sample_valid"].to_list(),
        "data.diagnostic_status": v0["diagnostic_status"].to_list(),
        "data.close_reason": v0["close_reason"].to_list(),
        "data.position_limit": v0["position_limit"].to_list(),
        "data.filling_overtime": v0["filling_overtime"].to_list(),
        "quality.valid": [True] * v0.height,
        "quality.completeness": ["complete"] * v0.height,
    })
    rawdir = tmp_path / "raw"
    part = rawdir / "machine=filler01" / "date=2026-08-13"
    part.mkdir(parents=True, exist_ok=True)
    raw.write_parquet(part / "valve_cycles.parquet")

    # DB di test PRIVATO (usato SOLO da questo test: nessun altro file, nessun
    # scrittore esterno). _ensure_test_database crea il DB al primo uso;
    # drop_all in setup fa partire SEMPRE da zero — il test passa al secondo
    # run senza dipendere dall'ordine alfabetico né da residui (fix F2).
    _ensure_test_database(_TEST_DB_URL)
    st = Storage(make_engine(_TEST_DB_URL))
    st.drop_all()
    st.init()

    # run esplicito: il DB privato del test non ha un KV `current_run_id`, e
    # dal 2026-08-22 una prediction senza run non è scrivibile.
    run_id = "test_e2e_inference"
    predictions = st.predictions
    consumer = InferenceConsumer(raw_dir=rawdir, db_url=_TEST_DB_URL,
                                 run_id=run_id)
    try:
        n = consumer.run()
        assert n == 4  # 200 cicli = 4 finestre da 50

        with st.engine.connect() as conn:
            n_rows = conn.execute(select(func.count()).select_from(predictions)).scalar()
        assert n_rows == 4
        with st.engine.connect() as conn:
            mv = conn.execute(
                select(predictions.c.model_version).distinct()).scalar()
        assert mv and mv != "unknown"

        # prediction_ts sull'asse-DATO (event_ts del ciclo di fine finestra),
        # non sull'orologio di parete: i cicli sono del 2026-06-01.
        with st.engine.connect() as conn:
            ts_rows = conn.execute(
                select(predictions.c.prediction_ts)).scalars().all()
        assert len(ts_rows) == 4
        assert all(str(t).startswith("2026-06-01") for t in ts_rows), ts_rows

        # watermark/dedup: un secondo run NON duplica (window_end_cycle_id già
        # predetto → skip), nonostante prediction_id nuovo a ogni run (uuid4)
        n2 = consumer.run()
        assert n2 == 0  # nessuna finestra nuova (watermark: tutte già predette)
        with st.engine.connect() as conn:
            n_rows2 = conn.execute(select(func.count()).select_from(predictions)).scalar()
        assert n_rows2 == 4  # nessun duplicato
    finally:
        # teardown: il test rimuove le PROPRIE 4 righe (valve 1, finestre
        # 50/100/150/200) — il DB privato resta pulito per qualunque run
        with st.engine.begin() as conn:
            conn.execute(
                delete(predictions).where(
                    predictions.c.valve_id == 1,
                    predictions.c.window_end_cycle_id.in_(list(_EXPECTED_WINDOWS))))
