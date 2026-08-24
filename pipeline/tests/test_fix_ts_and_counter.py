"""Regressioni per i due difetti A corretti il 2026-08-19.

1. `prediction_ts` deve essere il tempo del DATO — l'`event_ts` del ciclo che
   chiude la finestra — non l'orologio di parete (`now()`).
   Misura del difetto: 12.060/12.060 righe con ts entro 55,7 s di esecuzione
   contro 15 h 20 m di dati reali, scostamento 78 giorni
   (`.scratch/backend-2026-08-19/CONFRONTO-API-FIXTURE.md` §2.2).

2. La chiusura di un alert NON incrementa `n_cycles_above` (una chiusura
   avviene sotto soglia); il sustain SÌ (ibidem §5.2).

Nessun test qui tocca un database.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.alert import AlertEvent, _full_state_payload  # noqa: E402
from pipeline.inference import InferenceConsumer  # noqa: E402


# ---------------------------------------------------------------------------
# Difetto 1 — prediction_ts sull'asse-dato
# ---------------------------------------------------------------------------
_LABELS = ["healthy", "restriction", "closing_delay", "opening_delay",
           "pressure_instability", "flowmeter_dropout", "flowmeter_glitch"]

_DATA_EPOCH = "2026-06-01T08:21:00.800000+00:00"


class _FakeModel:
    """Modello deterministico: sempre `healthy`, nessun refit, nessun IO."""

    classes_ = _LABELS

    def predict(self, X):
        return ["healthy"] * len(X)

    def predict_proba(self, X):
        row = [0.7] + [0.05] * 6
        return [row for _ in range(len(X))]


def _consumer() -> InferenceConsumer:
    """InferenceConsumer senza passare da __init__ (niente modello su disco)."""
    inf = InferenceConsumer.__new__(InferenceConsumer)
    inf.model = _FakeModel()
    inf.classes_ = _LABELS
    inf.model_version = "test-fix-ts"
    inf.feature_schema_version = "ML-F1"
    inf.healthy_label = "healthy"
    return inf


def _features(rows: list[tuple[str, int, int]]) -> pl.DataFrame:
    """Frame minimo compatibile con predict_frame (43 feature a zero)."""
    from pipeline.features import FEATURE_COLUMNS
    data = {
        "machine_code": [r[0] for r in rows],
        "window_idx": [r[1] for r in rows],
        "last_cycle_id": [r[2] for r in rows],
    }
    for c in FEATURE_COLUMNS:
        data[c] = [0.0] * len(rows)
    return pl.DataFrame(data)


def _cycles(rows: list[tuple[str, int, str | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "machine_code": [r[0] for r in rows],
            "cycle_id": [r[1] for r in rows],
            "event_ts": [r[2] for r in rows],
        },
        schema={"machine_code": pl.String, "cycle_id": pl.Int64,
                "event_ts": pl.String},
    )


def test_prediction_ts_is_event_ts_of_window_end_cycle():
    """prediction_ts == event_ts del ciclo `window_end_cycle_id`, non now()."""
    inf = _consumer()
    ts_a = "2026-06-01T08:21:00.800000+00:00"
    ts_b = "2026-06-01T09:30:11.250000+00:00"
    feats = _features([("valve0", 0, 50), ("valve0", 1, 100)])
    cycles = _cycles([("valve0", 50, ts_a), ("valve0", 100, ts_b),
                      ("valve1", 50, "2026-06-01T20:00:00+00:00")])

    recs = inf.predict_frame(feats, cycles=cycles)

    assert [r["prediction_ts"] for r in recs] == [ts_a, ts_b]
    # e non è l'orologio di parete: lo scostamento da now() è di mesi
    now = datetime.now(timezone.utc)
    for r in recs:
        age = now - datetime.fromisoformat(r["prediction_ts"])
        assert age > timedelta(days=1), (
            f"prediction_ts {r['prediction_ts']} è vicino a now(): "
            f"il difetto dell'orologio di parete è tornato")


def test_prediction_ts_matches_the_cycle_that_closes_the_window():
    """La chiave è (machine_code, cycle_id): valvole diverse, tempi diversi."""
    inf = _consumer()
    feats = _features([("valve0", 0, 7), ("valve1", 0, 7)])
    cycles = _cycles([
        ("valve0", 7, "2026-06-01T08:00:00+00:00"),
        ("valve1", 7, "2026-06-01T18:45:30+00:00"),
    ])
    recs = inf.predict_frame(feats, cycles=cycles)
    by_valve = {r["valve_id"]: r["prediction_ts"] for r in recs}
    # machine_code valveN -> valve_id N+1 (prediction_schema)
    assert by_valve[1] == "2026-06-01T08:00:00+00:00"
    assert by_valve[2] == "2026-06-01T18:45:30+00:00"


def test_missing_event_ts_skips_the_record_instead_of_inventing_now():
    """event_ts null/assente → record SALTATO, mai un timestamp di ripiego.

    `prediction_ts` è in `required` di prediction-v1.json, quindi «lasciare il
    campo assente» non è realizzabile: la scelta documentata è saltare.
    """
    inf = _consumer()
    feats = _features([("valve0", 0, 10),   # event_ts null
                       ("valve0", 1, 20),   # ciclo assente dalla mappa
                       ("valve0", 2, 30)])  # buono
    cycles = _cycles([("valve0", 10, None),
                      ("valve0", 30, "2026-06-01T12:00:00+00:00")])

    with pytest.warns(RuntimeWarning, match="event_ts assente"):
        recs = inf.predict_frame(feats, cycles=cycles)

    assert len(recs) == 1
    assert recs[0]["window_end_cycle_id"] == 30
    assert recs[0]["prediction_ts"] == "2026-06-01T12:00:00+00:00"


def test_cycles_without_event_ts_column_is_rejected_loudly():
    inf = _consumer()
    feats = _features([("valve0", 0, 10)])
    bad = pl.DataFrame({"machine_code": ["valve0"], "cycle_id": [10]})
    with pytest.raises(ValueError, match="event_ts"):
        inf.predict_frame(feats, cycles=bad)


# ---------------------------------------------------------------------------
# Difetto 2 — n_cycles_above
# ---------------------------------------------------------------------------
def _event(to_status: str, score: float) -> AlertEvent:
    return AlertEvent(
        valve_id=13, fault_type="restriction",
        from_status="open", to_status=to_status,
        anomaly_score=score, threshold_open=0.5, threshold_close=0.4,
        window_end_cycle_id=999, prediction_ts=_DATA_EPOCH,
    )


def test_closing_an_alert_does_not_increment_n_cycles_above():
    prev = {"opened_ts": _DATA_EPOCH, "opened_at_cycle_id": 100,
            "last_seen_ts": _DATA_EPOCH, "max_score_seen": 0.9,
            "n_cycles_above": 7}
    row = _full_state_payload(_event("closed", 0.31), prev)
    assert row["status"] == "closed"
    assert row["n_cycles_above"] == 7  # conservato: si chiude SOTTO soglia


def test_sustaining_an_alert_does_increment_n_cycles_above():
    prev = {"opened_ts": _DATA_EPOCH, "opened_at_cycle_id": 100,
            "last_seen_ts": _DATA_EPOCH, "max_score_seen": 0.9,
            "n_cycles_above": 7}
    row = _full_state_payload(_event("sustained", 0.95), prev)
    assert row["status"] == "sustained"
    assert row["n_cycles_above"] == 8
    assert row["max_score_seen"] == pytest.approx(0.95)


def test_open_then_close_counts_one_cycle_above():
    """Il caso misurato: fixture 1 / db 2 sui 4 alert chiusi."""
    row = _full_state_payload(_event("open", 0.8), None)
    assert row["n_cycles_above"] == 1
    row = _full_state_payload(_event("closed", 0.2), row)
    assert row["n_cycles_above"] == 1


def test_closed_with_no_previous_row_stays_zero():
    row = _full_state_payload(_event("closed", 0.2), None)
    assert row["n_cycles_above"] == 0
