"""Test unit — alert engine M10 (pipeline/alert.py).

Copre le regole della spec M10 §3 e il comportamento dei tre scenari demo
(healthy / fault / rientro) in isolamento deterministico, prima di toccare
Postgres o la pipeline reale.

Contratto pool (fix wave 2026-08-13, review m10-standards A1/A2):
- `AlertEvent` NON ha più il campo `gt_status` (collisione col glossario
  Ground Truth, campo morto);
- la chiusura emette il `from_status` REALE (stato precedente dell'engine),
  mai "sustained" fabbricato quando si chiude da "open".
- gli helper opzionali `alert.persist_events(events, storage, run_id)` e
  `alert.load_states(storage, config, run_id)` possono esistere: il test li importa
  lazy e tollera l'assenza (skip, mai FAIL).
"""
from __future__ import annotations

import os
import re
import secrets
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.alert import (
    AlertConfig,
    AlertEngine,
    AlertState,
    load_score_history,
    load_states,
    persist_events,
)


# ---------------------------------------------------------------------------
# DB di test (helper opzionali): nome UNICO PER PROCESSO
# ---------------------------------------------------------------------------
# I worker del fix wave girano in parallelo sullo stesso Postgres: un nome
# fisso condiviso produce corse DDL (residual risk F2/2.10 delle review).
# Ogni processo pytest si crea il proprio DB (`plcsim_test_fix_<random>`).
def _test_db_url() -> str:
    if "PLCSIM_TEST_DATABASE_URL" in os.environ:
        return os.environ["PLCSIM_TEST_DATABASE_URL"]
    url = (f"postgresql+psycopg://plcsim:plcsim@localhost:5432/"
           f"plcsim_test_fix_{secrets.token_hex(4)}")
    os.environ["PLCSIM_TEST_DATABASE_URL"] = url  # condivisa da tutto il processo
    return url


def _ensure_test_db(url: str) -> None:
    m = re.match(r"postgresql\+psycopg://([^/]+)/([A-Za-z0-9_]+)$", url)
    if not m:
        return
    try:
        from sqlalchemy import create_engine, text
        admin = create_engine(
            f"postgresql+psycopg://{m.group(1)}/postgres",
            connect_args={"connect_timeout": 3}, future=True)
        with admin.connect().execution_options(
                isolation_level="AUTOCOMMIT") as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": m.group(2)}).first()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{m.group(2)}"'))
        admin.dispose()
    except Exception:
        pass  # best-effort


_TEST_DB_URL = _test_db_url()
_ensure_test_db(_TEST_DB_URL)

# Run esplicito per le prove: da quando `predictions` ha un discriminante di
# run (2026-08-22) sia la scrittura sia la cronologia degli allarmi ne
# vogliono uno, e questi test non hanno un KV `current_run_id` da cui
# risolverlo.
RUN_DI_PROVA = "run_di_prova"


def _prediction_ts(wcid: int) -> str:
    base = datetime(2026, 8, 13, tzinfo=timezone.utc)
    return (base + timedelta(seconds=wcid)).isoformat().replace("+00:00", "Z")


def rec(valve_id, label, score, wcid, ts=None):
    return {
        "valve_id": valve_id,
        "predicted_label": label,
        "anomaly_score": score,
        "window_end_cycle_id": wcid,
        "prediction_ts": ts or _prediction_ts(wcid),
    }


def cfg(**kw):
    # I test M10 storici esercitano esplicitamente il comportamento legacy.
    # Il default operativo dell'engine usa invece K=5 in N=150 score-only.
    return AlertConfig(score_aggregation_window=0,
                       score_aggregation_required=0, **kw)


def labels(events):
    return [e.to_status for e in events]


def test_healthy_never_opens():
    eng = AlertEngine(cfg(persistence=2))
    evs = eng.process([rec(1, "healthy", 0.1, i) for i in range(10)])
    assert labels(evs) == []


def test_open_requires_persistence():
    # una sola finestra sopra soglia NON apre (persistence=2)
    eng = AlertEngine(cfg(threshold_open=0.5, hysteresis=0.1, persistence=2))
    evs = eng.process([
        rec(3, "restriction", 0.9, 1),
        rec(3, "restriction", 0.9, 2),
        rec(3, "restriction", 0.9, 3),
    ])
    # streak raggiunge persistence alla 2a finestra sopra soglia → open, poi sustain
    assert labels(evs) == ["open", "sustained"]


def test_sustain_then_close_hysteresis():
    eng = AlertEngine(cfg(threshold_open=0.5, hysteresis=0.2, persistence=2))
    evs = eng.process([
        rec(5, "flowmeter_dropout", 0.9, 1),
        rec(5, "flowmeter_dropout", 0.9, 2),   # open qui
        rec(5, "flowmeter_dropout", 0.8, 3),   # sustain
        rec(5, "flowmeter_dropout", 0.4, 4),   # 0.4 > threshold_close(0.3)? NO, 0.4 > 0.3 → isteresi, no close
        rec(5, "flowmeter_dropout", 0.2, 5),   # 0.2 <= 0.3 → close
    ])
    assert labels(evs) == ["open", "sustained", "closed"]


def test_hysteresis_band_no_transition():
    # score in (threshold_close, threshold_open) non transiziona
    eng = AlertEngine(cfg(threshold_open=0.5, hysteresis=0.3, persistence=1))
    evs = eng.process([
        rec(2, "pressure_instability", 0.9, 1),  # open
        rec(2, "pressure_instability", 0.35, 2),  # 0.2 < 0.35 < 0.5 → isteresi
    ])
    assert labels(evs) == ["open"]


def test_dedup_single_open_per_valve_fault():
    eng = AlertEngine(cfg(persistence=1))
    evs = eng.process([
        rec(8, "restriction", 0.9, 1),
        rec(8, "restriction", 0.9, 2),
        rec(8, "restriction", 0.9, 3),
    ])
    # solo una open, poi sustain (dedup: nessuna seconda open)
    assert evs[0].to_status == "open"
    assert labels(evs) == ["open", "sustained", "sustained"]


def test_cooldown_blocks_reopen():
    eng = AlertEngine(cfg(threshold_open=0.5, hysteresis=0.1, persistence=1,
                          cooldown_seconds=60.0))
    evs = eng.process([
        rec(11, "closing_delay", 0.9, 1),   # open (persistence=1)
        rec(11, "closing_delay", 0.2, 2),   # close
        # rientro immediato (stesso secondo) → dentro cooldown, nessuna open
        rec(11, "closing_delay", 0.9, 3),
    ])
    assert labels(evs) == ["open", "closed"]


def test_fault_type_maps_from_label_and_others_unaffected():
    eng = AlertEngine(cfg(persistence=1))
    evs = eng.process([
        rec(1, "flowmeter_glitch", 0.9, 1),   # valve 1 apre
        rec(2, "healthy", 0.1, 1),            # valve 2 healthy, no alert
        rec(3, "opening_delay", 0.9, 1),      # valve 3 apre
    ])
    # valve 1 e 3 hanno alert, valve 2 no
    opened_valves = sorted({e.valve_id for e in evs if e.to_status == "open"})
    assert opened_valves == [1, 3]
    assert all(e.fault_type != "healthy" for e in evs)


def test_healthy_label_closes_open_alert():
    eng = AlertEngine(cfg(persistence=1))
    evs = eng.process([
        rec(4, "restriction", 0.9, 1),   # open
        rec(4, "healthy", 0.05, 2),       # healthy + score basso → close
    ])
    assert labels(evs) == ["open", "closed"]


def test_healthy_close_from_open_emits_real_from_status():
    """Chiusura da "open": from_status DEVE essere "open" (il valore REALE).

    Regressione bug A1 (review m10-standards): `_close_all_for_valve`
    emetteva `from_status="sustained"` fabbricato anche quando lo stato
    precedente reale era "open" — un falso nel log di tracciabilità
    (ADR-0021: ogni transizione è tracciabile).
    """
    eng = AlertEngine(cfg(persistence=1))
    evs = eng.process([
        rec(4, "restriction", 0.9, 1),   # open (persistence=1)
        rec(4, "healthy", 0.05, 2),      # healthy + score basso → close
    ])
    assert [e.to_status for e in evs] == ["open", "closed"]
    assert evs[1].from_status == "open", \
        f"from_status fabbricato: atteso 'open', visto {evs[1].from_status!r}"


def test_healthy_close_from_sustained_emits_real_from_status():
    """Chiusura da "sustained": from_status == "sustained" (reale, coerente)."""
    eng = AlertEngine(cfg(persistence=1))
    evs = eng.process([
        rec(4, "restriction", 0.9, 1),   # open
        rec(4, "restriction", 0.9, 2),   # sustain
        rec(4, "healthy", 0.05, 3),      # close
    ])
    assert [e.to_status for e in evs] == ["open", "sustained", "closed"]
    assert evs[2].from_status == "sustained"


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        AlertConfig(threshold_open=0.0)
    with pytest.raises(ValueError):
        AlertConfig(threshold_open=0.5, hysteresis=0.5)  # >= threshold_open
    with pytest.raises(ValueError):
        AlertConfig(persistence=0)
    with pytest.raises(ValueError):
        AlertConfig(score_aggregation_window=20, score_aggregation_required=0)
    with pytest.raises(ValueError):
        AlertConfig(score_aggregation_window=5, score_aggregation_required=6)
    with pytest.raises(ValueError):
        AlertConfig(score_aggregation_window=True, score_aggregation_required=True)


def test_default_score_aggregation_opens_only_after_five_scores_in_one_hundred_fifty():
    """K=5/N=150 usa solo lo score: label intermittenti non sono un filtro."""
    eng = AlertEngine()
    records = [rec(21, "healthy", 0.1, idx) for idx in range(1, 17)]
    records.extend([
        rec(21, "healthy", 0.9, 17),
        rec(21, "restriction", 0.9, 18),
        rec(21, "flowmeter_glitch", 0.9, 19),
        rec(21, "opening_delay", 0.9, 20),
    ])

    assert labels(eng.process(records)) == []
    events = eng.process([rec(21, "healthy", 0.9, 21)])

    assert labels(events) == ["open"]
    assert events[0].valve_id == 21


def test_score_aggregation_does_not_open_at_four_scores_in_one_hundred_fifty():
    """Il bordo K=4/N=150 non qualifica l'apertura."""
    eng = AlertEngine()
    records = [rec(30, "healthy", 0.1, idx) for idx in range(1, 17)]
    records.extend(rec(30, "restriction", 0.9, idx) for idx in range(17, 21))

    assert labels(eng.process(records)) == []


def test_default_window_keeps_five_scores_for_one_hundred_fifty_predictions():
    """La quinta evidenza esce dal default N=150 solo alla 151a posizione."""
    eng = AlertEngine()
    assert eng.config.score_aggregation_required == 5
    assert eng.config.score_aggregation_window == 150
    ts = "2026-08-13T00:00:00Z"

    opened = eng.process([
        rec(21, "healthy", 0.9, idx, ts=ts) for idx in range(1, 6)
    ])
    assert labels(opened) == ["open"]

    retained = eng.process([
        rec(21, "healthy", 0.1, idx, ts=ts) for idx in range(6, 151)
    ])
    assert labels(retained) == []

    closed = eng.process([rec(21, "healthy", 0.1, 151, ts=ts)])
    assert labels(closed) == ["closed"]


def test_score_aggregation_uses_one_alert_lineage_when_labels_change():
    """Dopo la qualificazione, label diverse non possono aprire duplicati."""
    eng = AlertEngine()
    qualifying = [
        rec(13, label, 0.9, idx)
        for idx, label in enumerate(
            ("restriction", "flowmeter_glitch", "healthy", "opening_delay", "healthy"),
            start=1)
    ]
    follow_up = [
        rec(13, "closing_delay", 0.9, 6),
        rec(13, "pressure_instability", 0.9, 7),
    ]

    events = eng.process(qualifying + follow_up)

    assert labels(events).count("open") == 1
    assert {event.fault_type for event in events} == {"score_aggregation"}


def test_restart_with_score_history_keeps_qualified_alert_open():
    """Il seed K/N mantiene aperto un alert che resta qualificato al restart."""
    config = AlertConfig(score_aggregation_window=3,
                         score_aggregation_required=2)
    before_restart = AlertEngine(config)
    assert labels(before_restart.process([
        rec(21, "healthy", 0.9, 98),
        rec(21, "healthy", 0.9, 99),
    ])) == ["open"]

    restarted = AlertEngine(config)
    restarted.states = before_restart.states
    restarted._score_history = {21: deque([True, True], maxlen=3)}

    assert labels(restarted.process([rec(21, "healthy", 0.1, 100)])) == []
    assert restarted.states[(21, "score_aggregation")].status == "open"


def test_restart_without_score_history_closes_same_alert():
    """Controllo negativo: senza seed il restart conserva il comportamento vuoto."""
    config = AlertConfig(score_aggregation_window=3,
                         score_aggregation_required=2)
    before_restart = AlertEngine(config)
    before_restart.process([
        rec(21, "healthy", 0.9, 98),
        rec(21, "healthy", 0.9, 99),
    ])

    restarted = AlertEngine(config)
    restarted.states = before_restart.states

    assert labels(restarted.process([rec(21, "healthy", 0.1, 100)])) == ["closed"]


def test_load_score_history_skips_database_when_score_aggregation_disabled():
    """K/N disabilitato non apre neppure una connessione per il seed."""
    class NoDatabaseAccess:
        @property
        def engine(self):
            raise AssertionError("il seed disabilitato non deve interrogare il DB")

    disabled = AlertConfig(score_aggregation_window=0,
                           score_aggregation_required=0)
    assert load_score_history(NoDatabaseAccess(), disabled) == {}


# ---------------------------------------------------------------------------
# Helper opzionali del pool: alert.persist_events / alert.load_states
# ---------------------------------------------------------------------------
# Contratto pool (fix wave 2026-08-13): i due helper possono esistere in
# pipeline/alert.py. Lazy-import + tolleranza assenza: se non ci sono → skip
# (mai FAIL); se ci sono ma il roundtrip è rotto → FAIL (segnale reale).
def _pg_available() -> bool:
    try:
        from pipeline.storage import Storage, make_engine
        return Storage(make_engine(_TEST_DB_URL)).ping()
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL non raggiungibile (avvia `docker compose up -d postgres`)")


@requires_postgres
def test_persist_helpers_roundtrip_when_present():
    """persist_events/load_states (se presenti): engine → storage → stato.

    - helper assenti in pipeline.alert → skip (contratto opzionale);
    - helper presenti → le transizioni devono finire su storage (read-back
      reale: UNA riga per lineage) e load_states deve ricostruire stati
      AlertState coerenti.
    """
    from pipeline import alert as alert_mod
    from pipeline.storage import Storage, make_engine
    from sqlalchemy import select

    if not (hasattr(alert_mod, "persist_events")
            and hasattr(alert_mod, "load_states")):
        pytest.skip("alert.persist_events/load_states non presenti "
                    "(contratto opzionale del pool)")

    url = _TEST_DB_URL
    s = Storage(make_engine(url))
    # checkfirst=True: tollera i residui di DDL concorrente (F2/2.10)
    s.metadata.drop_all(s.engine, checkfirst=True)
    s.init()

    cfg = AlertConfig(threshold_open=0.5, hysteresis=0.1, persistence=1,
                      score_aggregation_window=0,
                      score_aggregation_required=0)
    eng = AlertEngine(cfg)
    events = eng.process([
        rec(2, "restriction", 0.9, 1),   # open
        rec(2, "restriction", 0.9, 2),   # sustained
        rec(2, "restriction", 0.2, 3),   # closed
    ])
    assert [e.to_status for e in events] == ["open", "sustained", "closed"]
    alert_mod.persist_events(events, s, RUN_DI_PROVA)

    with s.engine.connect() as conn:
        rows = conn.execute(select(s.alerts)).fetchall()
    assert len(rows) == 1, \
        "persist_events deve mantenere UNA riga per (run, valvola, guasto)"
    assert rows[0].status == "closed"
    assert rows[0].run_id == RUN_DI_PROVA

    states = alert_mod.load_states(s, cfg, RUN_DI_PROVA)
    assert states, "load_states ha ritornato una mappa vuota"
    vals = list(states.values())
    assert all(isinstance(v, AlertState) for v in vals), \
        f"load_states: valori non AlertState: {vals}"
    assert any(v.status == "closed" for v in vals), \
        f"load_states non ricostruisce lo stato chiuso: {vals}"


@requires_postgres
def test_load_score_history_restores_active_alert_after_restart():
    """Il riavvio conserva un alert qualificato e chiude senza il seed."""
    from pipeline.storage import Storage, make_engine

    s = Storage(make_engine(_TEST_DB_URL))
    s.metadata.drop_all(s.engine, checkfirst=True)
    s.init()

    def stored_prediction(wcid: int, score: float) -> dict:
        return {
            "prediction_id": str(uuid.uuid4()),
            "model_version": "test-model",
            "feature_schema_version": "ML-F1",
            "prediction_ts": _prediction_ts(wcid),
            "machine_id": "filler01",
            "valve_id": 21,
            "window_idx": wcid,
            "window_end_cycle_id": wcid,
            "predicted_label": "healthy",
            "anomaly_score": score,
            "probabilities": {"healthy": 1.0},
            "feature_fingerprint": "a" * 64,
        }

    config = AlertConfig(score_aggregation_window=3,
                         score_aggregation_required=2)
    for wcid, score in ((98, 0.1), (99, 0.9), (100, 0.9), (101, 0.1)):
        assert s.insert_prediction(stored_prediction(wcid, score), RUN_DI_PROVA)

    before_restart = AlertEngine(config)
    opened = before_restart.process([
        rec(21, "healthy", 0.1, 98),
        rec(21, "healthy", 0.9, 99),
        rec(21, "healthy", 0.9, 100),
    ])
    assert labels(opened) == ["open"]
    persist_events(opened, s, RUN_DI_PROVA)

    restarted = AlertEngine(config)
    restarted.states = load_states(s, config, RUN_DI_PROVA)
    restarted._score_history = load_score_history(
        s, config, before_window_end_cycle_ids={21: 101},
        run_id=RUN_DI_PROVA)
    assert labels(restarted.process([rec(21, "healthy", 0.1, 101)])) == []

    without_seed = AlertEngine(config)
    without_seed.states = load_states(s, config, RUN_DI_PROVA)
    assert labels(without_seed.process([rec(21, "healthy", 0.1, 101)])) == [
        "closed"]


@requires_postgres
def test_load_score_history_restarts_without_events_or_current_batch():
    """Seed reale: ordine, nessun padding, nessuna write e niente doppio conteggio."""
    from pipeline.storage import Storage, make_engine
    from sqlalchemy import func, select

    s = Storage(make_engine(_TEST_DB_URL))
    s.metadata.drop_all(s.engine, checkfirst=True)
    s.init()

    def stored_prediction(wcid: int, score: float) -> dict:
        return {
            "prediction_id": str(uuid.uuid4()),
            "model_version": "test-model",
            "feature_schema_version": "ML-F1",
            "prediction_ts": _prediction_ts(wcid),
            "machine_id": "filler01",
            "valve_id": 21,
            "window_idx": wcid,
            "window_end_cycle_id": wcid,
            "predicted_label": "healthy",
            "anomaly_score": score,
            "probabilities": {"healthy": 1.0},
            "feature_fingerprint": "a" * 64,
        }

    # Le ultime due righe appartengono al lotto corrente e sono già persistite
    # quando inference arriva al seed.
    for wcid, score in ((98, 0.9), (99, 0.1), (100, 0.1), (101, 0.9), (102, 0.1)):
        assert s.insert_prediction(stored_prediction(wcid, score), RUN_DI_PROVA)

    config = AlertConfig(score_aggregation_window=3,
                         score_aggregation_required=2)
    with s.engine.connect() as conn:
        counts_before = (
            conn.execute(select(func.count()).select_from(s.alerts)).scalar(),
            conn.execute(select(func.count()).select_from(s.alert_transitions)).scalar(),
        )

    history = load_score_history(
        s, config, before_window_end_cycle_ids={21: 101},
        run_id=RUN_DI_PROVA)

    assert list(history) == [21]
    assert list(history[21]) == [True, False, False]
    assert history[21].maxlen == 3
    with s.engine.connect() as conn:
        counts_after = (
            conn.execute(select(func.count()).select_from(s.alerts)).scalar(),
            conn.execute(select(func.count()).select_from(s.alert_transitions)).scalar(),
        )
    assert counts_after == counts_before == (0, 0)

    restarted = AlertEngine(config)
    restarted._score_history = history
    # Se 101 entrasse anche nel seed, il doppio `True` aprirebbe l'alert in
    # questo stesso lotto. Con il limite esclusivo non esiste alcuna transizione.
    current_batch = [
        rec(21, "healthy", 0.9, 101),
        rec(21, "healthy", 0.1, 102),
    ]
    assert labels(restarted.process(current_batch)) == []


@requires_postgres
def test_load_score_history_is_deterministic_and_excludes_exact_batch_ids():
    """Duplicati di ciclo hanno un ordine totale e il lotto è escluso per UUID."""
    from pipeline.storage import Storage, make_engine

    s = Storage(make_engine(_TEST_DB_URL))
    s.metadata.drop_all(s.engine, checkfirst=True)
    s.init()

    ids = [f"00000000-0000-0000-0000-{value:012d}" for value in (1, 2, 3)]
    scores = (0.1, 0.9, 0.2)
    for prediction_id, score in zip(ids, scores):
        record = {
            "prediction_id": prediction_id,
            "model_version": "test-model",
            "feature_schema_version": "ML-F1",
            "prediction_ts": "2026-08-13T00:00:01Z",
            "machine_id": "filler01",
            "valve_id": 21,
            "window_idx": 1,
            "window_end_cycle_id": 1,
            "predicted_label": "healthy",
            "anomaly_score": score,
            "probabilities": {"healthy": 1.0},
            "feature_fingerprint": "a" * 64,
        }
        assert s.insert_prediction(record, RUN_DI_PROVA)

    config = AlertConfig(score_aggregation_window=2,
                         score_aggregation_required=1)
    history = load_score_history(s, config, run_id=RUN_DI_PROVA)
    assert list(history[21]) == [True, False]

    prior_only = load_score_history(
        s, config, excluded_prediction_ids=ids[1:], run_id=RUN_DI_PROVA)
    assert list(prior_only[21]) == [False]
