"""Backfill offline di `machine_state_history` da un run congelato.

Entry point: ``python -m pipeline.state_history_backfill``.

Problema chiuso: l'UNICO writer di `machine_state_history` è il consumer MQTT
(`pipeline/ingest.py`, topic `plant/filler01/state`) attraverso
`Storage.log_machine_state_history` / `close_machine_state_history`. Su un run
congelato in `work/` quel percorso non è mai passato, e senza transizioni OMAC
`GET /machine/oee` non può calcolare l'availability (degraded con motivo
esplicito: "nessuna transizione OMAC nella finestra"). Questo modulo ricava le
transizioni dagli eventi `STATE:` del run e le persiste con gli stessi metodi
di Storage — nessuna scrittura SQL propria, nessuno schema nuovo.

Semantica: identica a `state_history(run)` del generatore di fixture usato
dall'oracolo della dashboard (script locale, fuori dal repository), che è la
definizione già funzionante:

- si prendono SOLO gli eventi con `event` che inizia per `STATE:`, ordinati
  per `ts_beg`; l'etichetta è la parte dopo i due punti;
- `state_code` = `plcsim.realtime.OMAC_CODES[label]` (importato, non
  ricopiato: Running=1, Stopping=2, Stopped=3, Idle=4, Resetting=5,
  Starting=11);
- `entered_ts` = `ts_beg` dell'evento;
- `exited_ts` = `entered_ts` della transizione SUCCESSIVA; l'ultima resta
  aperta (`NULL`), esattamente come lo stato corrente scritto dal writer live.

`source`: dichiara la provenienza reale — `backfill:events.parquet:STATE:<run>`.
NON finge di essere il percorso MQTT: chi legge la riga deve poter distinguere
un backfill offline da una transizione osservata dal vivo.

Verità nascosta: si legge SOLO `events.parquet`, e di quel file SOLO gli
eventi `STATE:` (mai `FAULT_START`/`FAULT_RAMP`, mai `ground_truth.parquet`
o `fault_timeline.parquet`).

Idempotenza: `machine_state_history` è append-only e senza chiave naturale —
un secondo run DUPLICHEREBBE le transizioni. Il modulo quindi RIFIUTA di
scrivere se la tabella contiene già righe, a meno di `--replace` (che le
cancella prima). Mai un merge silenzioso.

CLI::

    python -m pipeline.state_history_backfill --run work/<run> [--db-url <url>] [--replace]

Exit codes: 0 = ok; 2 = run/eventi assenti o nessun evento STATE:;
3 = tabella già popolata senza `--replace`.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import func, select

from pipeline.storage import Storage, make_engine
from plcsim.realtime import OMAC_CODES  # unica definizione dei codici OMAC

logger = logging.getLogger("pipeline.state_history_backfill")

DATABASE_URL_DEFAULT = "postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim"


class StateBackfillError(Exception):
    """Errore di dominio (messaggio già operativo)."""


def read_state_transitions(run_dir: Path) -> list[dict[str, Any]]:
    """Transizioni OMAC dagli eventi `STATE:` — semantica di
    `state_history` nel generatore di fixture locale."""
    path = run_dir / "events.parquet"
    if not path.exists():
        raise StateBackfillError(f"eventi assenti: {path}")
    ev = (pl.scan_parquet(path)
          .filter(pl.col("event").str.starts_with("STATE:"))
          .select(["ts_beg", "event"])
          .sort("ts_beg")
          .collect())
    recs = ev.rows()
    if not recs:
        raise StateBackfillError(f"{path}: nessun evento STATE: (niente da scrivere)")
    source = f"backfill:events.parquet:STATE:{run_dir.name}"
    rows: list[dict[str, Any]] = []
    for i, (ts, e) in enumerate(recs):
        label = e.split(":", 1)[1]
        if label not in OMAC_CODES:
            raise StateBackfillError(
                f"stato OMAC sconosciuto {label!r} (attesi: {sorted(OMAC_CODES)})")
        rows.append({
            "state_code": OMAC_CODES[label],
            "state_label": label,
            "entered_ts": ts,
            "exited_ts": recs[i + 1][0] if i + 1 < len(recs) else None,
            "source": source,
        })
    return rows


def existing_rows(storage: Storage) -> int:
    with storage.engine.connect() as conn:
        return int(conn.execute(
            select(func.count()).select_from(storage.machine_state_history)).scalar_one())


def backfill(run_dir: Path, db_url: str = DATABASE_URL_DEFAULT,
             replace: bool = False) -> dict[str, Any]:
    rows = read_state_transitions(run_dir)
    storage = Storage(make_engine(db_url))
    storage.init()
    n = existing_rows(storage)
    if n and not replace:
        raise FileExistsError(
            f"machine_state_history contiene già {n} righe: append-only senza "
            f"chiave naturale, un secondo backfill duplicherebbe le transizioni. "
            f"Usa --replace per sostituirle.")
    if n and replace:
        with storage.engine.begin() as conn:
            conn.execute(storage.machine_state_history.delete())
        logger.info("rimosse %d righe preesistenti (--replace)", n)
    for row in rows:
        storage.log_machine_state_history(
            state_code=row["state_code"], state_label=row["state_label"],
            entered_ts=row["entered_ts"], source=row["source"])
        if row["exited_ts"] is not None:
            storage.close_machine_state_history(exited_ts=row["exited_ts"])
    return {"run": str(run_dir), "written": len(rows),
            "transitions": [(r["state_label"], r["entered_ts"].isoformat()) for r in rows]}


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.state_history_backfill",
        description="Backfill machine_state_history dagli eventi STATE: di un run.")
    ap.add_argument("--run", required=True, help="work/<run> (directory del run)")
    ap.add_argument("--db-url", default=DATABASE_URL_DEFAULT)
    ap.add_argument("--replace", action="store_true",
                    help="cancella le righe esistenti prima di scrivere")
    return ap


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_arg_parser().parse_args(argv)
    try:
        summary = backfill(Path(args.run), args.db_url, replace=args.replace)
    except StateBackfillError as exc:
        logger.error("%s", exc)
        return 2
    except FileExistsError as exc:
        logger.error("%s", exc)
        return 3
    for label, ts in summary["transitions"]:
        print(f"{ts}  {label}")
    print(f"scritte {summary['written']} transizioni")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
