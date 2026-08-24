"""Test del supervisor live: sequenza esplicita e stop sul backfill."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.live_supervisor import (
    SupervisorConfig,
    build_arg_parser,
    run,
    run_once,
)


def test_supervisor_runs_backfill_then_inference_with_explicit_date(tmp_path: Path):
    calls = []

    def runner(command, check=False):
        calls.append((command, check))
        return SimpleNamespace(returncode=0)

    config = SupervisorConfig(
        run_id="live-2026-08-21",
        date="2026-08-21",
        raw_dir=tmp_path,
        db_url="postgresql://example/db",
        once=True,
    )

    assert run_once(config, runner=runner) == 0
    assert len(calls) == 2
    assert all(check is False for _, check in calls)
    assert calls[0][0][0:3] == [config.python_executable, "-m",
                                 "pipeline.cycles_backfill"]
    assert calls[1][0][0:3] == [config.python_executable, "-m",
                                 "pipeline.inference"]
    for command in (calls[0][0], calls[1][0]):
        assert "--dates" in command
        assert command[command.index("--dates") + 1] == "2026-08-21"
        assert "2026-06-01" not in command
        # ENTRAMBI i comandi portano lo stesso run. Senza `--run-id`
        # l'inference ricadeva sul KV `current_run_id` — il run storico — e
        # non produceva nulla, perché quel run ha già occupato tutti i
        # window_end_cycle_id (misurato il 2026-08-22).
        assert "--run-id" in command
        assert command[command.index("--run-id") + 1] == "live-2026-08-21"
    assert calls[0][0].index("--run-id") < calls[0][0].index("--dates")


def test_supervisor_does_not_infer_when_backfill_fails(tmp_path: Path):
    calls = []

    def runner(command, check=False):
        calls.append(command)
        return SimpleNamespace(returncode=17)

    config = SupervisorConfig(run_id="run", date="2026-08-21",
                              raw_dir=tmp_path, once=True)
    assert run_once(config, runner=runner) == 17
    assert len(calls) == 1
    assert calls[0][2] == "pipeline.cycles_backfill"


def test_supervisor_cli_requires_run_id_and_date():
    parser = build_arg_parser()
    with pytest.raises(SystemExit) as missing_run:
        parser.parse_args(["--date", "2026-08-21", "--once"])
    assert missing_run.value.code == 2
    with pytest.raises(SystemExit) as missing_date:
        parser.parse_args(["--run-id", "run", "--once"])
    assert missing_date.value.code == 2


def test_supervisor_once_does_not_wait_or_repeat(tmp_path: Path):
    calls = []

    def runner(command, check=False):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    def waiter(_seconds):
        raise AssertionError("--once non deve attendere")

    config = SupervisorConfig(
        run_id="live-run",
        date="2026-08-21",
        raw_dir=tmp_path,
        interval_seconds=0.01,
        once=True,
    )
    assert run(config, runner=runner, waiter=waiter) == 0
    assert len(calls) == 2


def test_supervisor_wait_is_after_immediate_run_and_interruptible(tmp_path: Path):
    calls = []
    waits = []

    def runner(command, check=False):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    def waiter(seconds):
        waits.append(seconds)
        # Let one additional heartbeat run, then request an orderly stop.
        return len(waits) >= 2

    config = SupervisorConfig(
        run_id="live-run",
        date="2026-08-21",
        raw_dir=tmp_path,
        interval_seconds=0.25,
    )
    assert run(config, runner=runner, waiter=waiter) == 0
    assert len(calls) == 4  # backfill+inference for each of two heartbeats
    assert waits == [0.25, 0.25]


def test_supervisor_cli_rejects_invalid_date_and_interval():
    parser = build_arg_parser()
    with pytest.raises(SystemExit) as bad_date:
        parser.parse_args(["--run-id", "run", "--date", "2026-02-30"])
    assert bad_date.value.code == 2
    with pytest.raises(SystemExit) as bad_interval:
        parser.parse_args([
            "--run-id", "run", "--date", "2026-08-21",
            "--interval-seconds", "0",
        ])
    assert bad_interval.value.code == 2
