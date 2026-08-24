"""Heartbeat sequenziale per il percorso live di backfill e inference.

Il supervisore è volutamente piccolo: ogni battito avvia prima il backfill
della sola partizione esplicita e avvia l'inference solo se quel comando
termina con successo.  L'idempotenza e il cursore di avanzamento restano nei
comandi sottostanti; un riavvio del supervisore può quindi ripetere il battito
senza introdurre uno stato operativo parallelo.

Uso tipico::

    python -m pipeline.live_supervisor \
        --run-id live-2026-08-21 --date 2026-08-21 \
        --interval-seconds 60

``--date`` è obbligatorio per impedire che il percorso live cada sullo scan
storico di ``data/raw``.  I comandi sono passati a ``subprocess.run`` come
liste di argomenti, mai attraverso una shell.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date as date_type
import logging
import math
from pathlib import Path
import subprocess
import sys
import threading
from typing import Callable, Sequence


logger = logging.getLogger("pipeline.live_supervisor")


def _date_argument(value: str) -> str:
    """Valida una partizione calendario nel formato ``YYYY-MM-DD``."""
    try:
        parsed = date_type.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"date non valida {value!r}: usare YYYY-MM-DD"
        ) from exc
    # fromisoformat accetta solo il formato ISO canonico per una stringa
    # (ma il confronto esplicito rende il contratto visibile e stabile).
    normalized = parsed.isoformat()
    if value != normalized:
        raise argparse.ArgumentTypeError(
            f"date non valida {value!r}: usare YYYY-MM-DD"
        )
    return normalized


def _positive_seconds(value: str) -> float:
    """Ritorna un intervallo finito strettamente positivo."""
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "interval-seconds deve essere un numero positivo"
        ) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError(
            "interval-seconds deve essere un numero positivo"
        )
    return seconds


@dataclass(frozen=True)
class SupervisorConfig:
    """Configurazione immutabile di un heartbeat live."""

    run_id: str
    date: str
    raw_dir: Path = Path("data/raw")
    db_url: str | None = None
    interval_seconds: float = 60.0
    once: bool = False
    python_executable: str = sys.executable

    def __post_init__(self) -> None:
        run_id = str(self.run_id).strip()
        if not run_id:
            raise ValueError("run_id mancante: passare --run-id <id>")
        object.__setattr__(self, "run_id", run_id)
        # Riusa la stessa validazione del parser anche per i chiamanti Python.
        object.__setattr__(self, "date", _date_argument(self.date))
        interval = float(self.interval_seconds)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("interval_seconds deve essere un numero positivo")
        object.__setattr__(self, "interval_seconds", interval)
        executable = str(self.python_executable).strip()
        if not executable:
            raise ValueError("python_executable non può essere vuoto")
        object.__setattr__(self, "python_executable", executable)
        object.__setattr__(self, "raw_dir", Path(self.raw_dir))

    def backfill_command(self) -> list[str]:
        """Comando delimitato per il writer ``cycles``."""
        command = [
            self.python_executable,
            "-m",
            "pipeline.cycles_backfill",
            "--run-id",
            self.run_id,
            "--dates",
            self.date,
            "--raw-dir",
            str(self.raw_dir),
        ]
        if self.db_url is not None:
            command.extend(("--db-url", self.db_url))
        return command

    def inference_command(self) -> list[str]:
        """Comando delimitato per l'inference della stessa partizione.

        ``--run-id`` è lo STESSO del backfill, e non è opzionale. Senza,
        l'inference ricade sul KV ``current_run_id`` — che nel percorso live
        è il run storico — e succedono due cose, entrambe sbagliate: il
        watermark diventa quello dello storico, che ha già occupato ogni
        `window_end_cycle_id`, quindi il run live non produce nulla; e se
        producesse, scriverebbe le proprie prediction sotto l'identità del
        run storico. Misurato il 2026-08-22: `prediction: 0 record prodotti`
        a ogni battito, con i cicli che intanto entravano regolarmente.
        """
        command = [
            self.python_executable,
            "-m",
            "pipeline.inference",
            "--dates",
            self.date,
            "--raw",
            str(self.raw_dir),
            "--run-id",
            self.run_id,
        ]
        if self.db_url is not None:
            command.extend(("--db-url", self.db_url))
        return command


Runner = Callable[..., subprocess.CompletedProcess[object]]
Waiter = Callable[[float], bool]


def _returncode(result: object) -> int:
    """Legge un return code anche da runner di test minimale."""
    value = getattr(result, "returncode", 0)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"runner ha restituito returncode non valido: {value!r}") from exc


def run_once(
    config: SupervisorConfig,
    *,
    runner: Runner = subprocess.run,
) -> int:
    """Esegue esattamente un battito: backfill, poi inference.

    Un errore del backfill interrompe il battito prima di invocare
    l'inference.  ``check=False`` mantiene il codice di uscita osservabile e
    permette al supervisore di terminare con lo stesso errore operativo.
    """
    backfill = config.backfill_command()
    logger.info("heartbeat: backfill %s", " ".join(backfill))
    backfill_result = runner(backfill, check=False)
    backfill_rc = _returncode(backfill_result)
    if backfill_rc != 0:
        logger.error("heartbeat fermato: backfill exit=%d", backfill_rc)
        return backfill_rc

    inference = config.inference_command()
    logger.info("heartbeat: inference %s", " ".join(inference))
    inference_result = runner(inference, check=False)
    inference_rc = _returncode(inference_result)
    if inference_rc != 0:
        logger.error("heartbeat fermato: inference exit=%d", inference_rc)
    return inference_rc


def _wait(seconds: float) -> bool:
    """Attende con un evento; ritorna ``False`` quando scade il battito."""
    return threading.Event().wait(seconds)


def run(
    config: SupervisorConfig,
    *,
    runner: Runner = subprocess.run,
    stop_event: threading.Event | None = None,
    waiter: Waiter | None = None,
) -> int:
    """Esegue heartbeat fino a ``--once``, errore o stop esplicito.

    La prima corsa è immediata.  Dopo una corsa riuscita il wait è
    interrompibile con ``stop_event`` (utile anche per un arresto ordinato) o
    con ``KeyboardInterrupt`` nella CLI.
    """
    wait_fn = waiter or _wait
    while True:
        returncode = run_once(config, runner=runner)
        if returncode != 0 or config.once:
            return returncode
        if stop_event is not None:
            if stop_event.wait(config.interval_seconds):
                return 0
        elif wait_fn(config.interval_seconds):
            return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.live_supervisor",
        description=(
            "Heartbeat live: backfill delimitato per data, poi inference "
            "sequenziale e riavviabile."
        ),
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="discriminante esplicito del run per il backfill",
    )
    parser.add_argument(
        "--date",
        required=True,
        type=_date_argument,
        help="partizione live esatta (YYYY-MM-DD), mai la root storica",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=Path("data/raw"),
        help="root raw condivisa da backfill (--raw-dir) e inference (--raw)",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="URL PostgreSQL esplicito (default: configurazione dei moduli)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=_positive_seconds,
        default=60.0,
        help="intervallo positivo tra battiti (default: 60)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="esegue un solo battito e termina",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = SupervisorConfig(
        run_id=args.run_id,
        date=args.date,
        raw_dir=args.raw_dir,
        db_url=args.db_url,
        interval_seconds=args.interval_seconds,
        once=args.once,
    )
    try:
        return run(config)
    except KeyboardInterrupt:
        logger.info("heartbeat interrotto dall'operatore")
        return 130


__all__ = [
    "SupervisorConfig",
    "build_arg_parser",
    "run_once",
    "run",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
