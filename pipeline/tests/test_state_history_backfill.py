"""Test del backfill machine_state_history (pipeline/state_history_backfill.py).

Copertura della parte pura (nessun DB, nessuna scrittura su `plcsim`):
- selezione: SOLO gli eventi `STATE:`, ordinati per ts_beg — gli eventi di
  valvola e, in particolare, gli eventi di guasto (verità nascosta) non
  entrano MAI;
- `state_code` da `plcsim.realtime.OMAC_CODES`; etichetta sconosciuta →
  errore chiaro;
- `exited_ts` = entered_ts della transizione successiva, l'ultima resta
  aperta (None) — semantica identica a `state_history` nel generatore di
  fixture locale;
- `source` dichiara la provenienza backfill (non finge il percorso MQTT);
- eventi assenti / nessun STATE: → StateBackfillError.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.state_history_backfill import (  # noqa: E402
    StateBackfillError,
    read_state_transitions,
)


def _events(run_dir: Path, rows: list[tuple[str, str]]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "ts_beg": [datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
                   for t, _ in rows],
        "machine_code": ["MACHINE"] * len(rows),
        "event": [e for _, e in rows],
        "note": [""] * len(rows),
        "cycle_id": [0] * len(rows),
        "scenario_id": [1] * len(rows),
    }).write_parquet(run_dir / "events.parquet")
    return run_dir


def test_semantica_transizioni(tmp_path):
    run = _events(tmp_path / "run", [
        ("2026-06-01T08:21:00", "STATE:Running"),
        ("2026-06-01T08:12:00", "STATE:Starting"),   # fuori ordine: va ordinato
        ("2026-06-01T09:00:00", "FLUSHING"),         # non STATE: → escluso
        ("2026-06-01T10:00:00", "FAULT_START"),      # verità nascosta → escluso
        ("2026-06-01T23:42:00", "STATE:Stopping"),
    ])
    rows = read_state_transitions(run)
    assert [r["state_label"] for r in rows] == ["Starting", "Running", "Stopping"]
    assert [r["state_code"] for r in rows] == [11, 1, 2]
    assert rows[0]["exited_ts"] == rows[1]["entered_ts"]
    assert rows[1]["exited_ts"] == rows[2]["entered_ts"]
    assert rows[-1]["exited_ts"] is None              # stato corrente, aperto
    assert all(r["source"].startswith("backfill:events.parquet:STATE:")
               for r in rows)


def test_stato_sconosciuto(tmp_path):
    run = _events(tmp_path / "r2", [("2026-06-01T08:00:00", "STATE:Boh")])
    with pytest.raises(StateBackfillError):
        read_state_transitions(run)


def test_sorgenti_mancanti(tmp_path):
    with pytest.raises(StateBackfillError):
        read_state_transitions(tmp_path / "inesistente")
    run = _events(tmp_path / "r3", [("2026-06-01T08:00:00", "FLUSHING")])
    with pytest.raises(StateBackfillError):
        read_state_transitions(run)
