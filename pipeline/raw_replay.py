"""Replay offline di un run bulk del simulatore nel raw canonico dell'ingest.

Entry point: ``python -m pipeline.raw_replay``.

Problema chiuso: la catena a valle (`pipeline/cycles_backfill.py`,
`pipeline/features.load_raw_valve_cycles`, `pipeline/inference.py`) legge
ESCLUSIVAMENTE il raw partizionato prodotto dal consumer MQTT
(`data/raw/machine=<machine_id>/date=<event_ts UTC>/valve_cycles.parquet`,
`pipeline/ingest.py` §6.2). Quel percorso richiede broker + edge attivi: su
un run congelato in `work/` (`valve_cycles.parquet` bulk di
`plcsim/telemetry.py`) non esiste alcun modo di popolarlo. Questo modulo è
l'anello mancante: trasforma UN run bulk nel raw canonico, senza toccare
l'ingest, senza broker e senza rieseguire il simulatore.

Perché offline e non realtime: le fixture che fanno da oracolo alla dashboard
sono state generate da run specifici già congelati; un nuovo run realtime
produrrebbe dati diversi e il confronto campo-per-campo perderebbe senso.
Il percorso live (realtime -> OPC UA -> Node-RED -> MQTT -> ingest) resta
valido e NON viene modificato da questo modulo.

Riuso, non reimplementazione: il layout (`FLATTENED_COLUMNS`), i tipi
(`COLUMN_TYPES`), la regola di partizionamento (`partition_of`) e
l'appiattimento (`flatten_record`, via `records_to_df`) sono IMPORTATI da
`pipeline/ingest.py`. Se il layout canonico cambia, questo modulo lo segue
automaticamente: non esiste una seconda definizione da tenere allineata.

Politica di onestà (nessuna fabbricazione) — la sorgente bulk ha meno campi
dell'envelope MQTT, e i campi mancanti restano NULL:

    colonna raw canonica     origine
    -----------------------  ---------------------------------------------
    schema_version           costante SCHEMA_VERSION: descrive il set di
                             colonne che QUESTO file contiene (v1.3, i 15
                             campi data.* post-M9), non un envelope
                             realmente transitato
    event_id                 NULL — identità del messaggio MQTT: nessun
                             messaggio è mai esistito, non si inventa un id
    event_ts                 `ts_beg` del run bulk (ISO UTC) — è anche la
                             chiave di partizionamento (spec §6.2)
    source_ts                NULL — orologio dell'edge, mai esistito
    ingest_ts                NULL — istante di ingestione, mai avvenuta
    machine_id               `--machine-id` ESPLICITO (il bulk non ce l'ha:
                             mai dedurre l'identità della macchina)
    cycle_id                 `cycle_id`
    valve_id                 `machine_code` "valveN" (0-based) -> N+1 (1-35),
                             la stessa mappa già documentata e usata da
                             `pipeline/cycles_backfill._bulk_valve_id` e,
                             all'inverso, da `pipeline/features.py`
    data.*                   colonne bulk di `plcsim/telemetry.py` (rename
                             1:1, nessuna semantica ricostruita)
    quality.valid            NULL — giudizio del validator dell'envelope
    quality.completeness     NULL — idem

Nessuna verità nascosta entra qui: si legge SOLO `valve_cycles.parquet` del
run (mai `ground_truth.parquet`, `fault_timeline.parquet`, né gli eventi di
guasto).

Determinismo: trasformazioni pure, nessun RNG; output ordinato per
(valve_id, cycle_id) dentro ogni partizione. Scrittura atomica (file temp +
``os.replace``), come l'ingest.

CLI::

    python -m pipeline.raw_replay --source work/<run>/valve_cycles.parquet \\
        --machine-id filler01 [--out data/raw] [--overwrite]

Exit codes: 0 = ok; 2 = sorgente assente/illeggibile/layout non bulk;
3 = partizione già esistente senza `--overwrite` (mai sovrascrittura
silenziosa di storico raw).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import polars as pl

from pipeline.ingest import (  # layout canonico: UNICA definizione
    COLUMN_TYPES,
    FILE_NAME,
    FLATTENED_COLUMNS,
    partition_of,
    records_to_df,
)

logger = logging.getLogger("pipeline.raw_replay")

# Versione envelope del SET DI COLONNE scritto (v1.3 = 15 campi data.*,
# post-M9). Non è la dichiarazione di un envelope realmente transitato.
SCHEMA_VERSION = "1.3"

DEFAULT_OUT = "data/raw"
CHUNK_ROWS = 50_000  # ~600k righe/run: si materializza a blocchi, non tutto

# data.<campo> <- colonna bulk (plcsim/telemetry.py). Rename 1:1.
DATA_FROM_BULK: dict[str, str] = {
    "filling_time_ms": "fillingtime",
    "tail_time_ms": "tailtime",
    "tail_pulse": "tailpulse",
    "pulse_count": "pulsecount",
    "target": "target",
    "delta_pulse": "deltapulse",
    "filling_step_out": "filling_step_out",
    "filling_ok": "fillingok",
    "fill_quality_ok": "fill_quality_ok",
    "sequence_ok": "sequence_ok",
    "sample_valid": "sample_valid",
    "position_limit": "position_limit",
    "filling_overtime": "filling_overtime",
    "diagnostic_status": "diagnostic_status",
    "close_reason": "close_reason",
}

_MACHINE_CODE_RE = re.compile(r"^valve(\d+)$")


class RawReplayError(Exception):
    """Errore di dominio del replay (messaggio già operativo)."""


def valve_id_of(machine_code: str) -> int:
    """"valveN" (0-based, telemetry) -> valve_id N+1 (contratto 1-35).

    Stessa mappa di `pipeline/cycles_backfill._bulk_valve_id`; valore fuori
    contratto -> errore CHIARO (mai riga silenziosamente scartata).
    """
    m = _MACHINE_CODE_RE.fullmatch(str(machine_code))
    if not m:
        raise RawReplayError(
            f"machine_code non riconosciuto: {machine_code!r} (atteso 'valveN')")
    vid = int(m.group(1)) + 1
    if not 1 <= vid <= 35:
        raise RawReplayError(
            f"machine_code {machine_code!r} -> valve_id {vid} fuori contratto 1-35")
    return vid


def _iso(ts: Any) -> str:
    """Datetime UTC -> stringa ISO parsabile da `partition_of` e da
    `cycles_backfill._normalize_timestamps` (`str.to_datetime`)."""
    if ts is None:
        raise RawReplayError("ts_beg nullo: event_ts è la chiave di partizione, "
                             "non può essere NULL (mai fabbricato)")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat()
    return str(ts)


def bulk_row_to_record(row: dict[str, Any], machine_id: str) -> dict[str, Any]:
    """Riga bulk -> record stored v1.3 nidificato (input di `flatten_record`)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": None,
        "event_ts": _iso(row["ts_beg"]),
        "source_ts": None,
        "ingest_ts": None,
        "machine_id": machine_id,
        "cycle_id": int(row["cycle_id"]),
        "valve_id": valve_id_of(row["machine_code"]),
        "data": {k: row.get(src) for k, src in DATA_FROM_BULK.items()},
        "quality": {"valid": None, "completeness": None},
    }


def load_bulk(source: Path) -> pl.DataFrame:
    """Legge il run bulk e verifica che sia davvero un layout bulk."""
    if not source.exists():
        raise RawReplayError(f"sorgente assente: {source}")
    df = pl.read_parquet(source)
    missing = [c for c in ("machine_code", "cycle_id", "ts_beg", "fillingtime")
               if c not in df.columns]
    if missing:
        raise RawReplayError(
            f"{source}: layout non bulk (colonne mancanti: {missing}; "
            f"viste: {sorted(df.columns)})")
    if df.height == 0:
        raise RawReplayError(f"{source}: sorgente vuota")
    return df


def _chunks(df: pl.DataFrame) -> Iterator[pl.DataFrame]:
    for off in range(0, df.height, CHUNK_ROWS):
        yield df.slice(off, CHUNK_ROWS)


def build_partitions(df: pl.DataFrame, machine_id: str) -> dict[tuple[str, str], pl.DataFrame]:
    """Run bulk -> {(machine_id, date): frame appiattito canonico}.

    L'appiattimento passa da `records_to_df` (quindi da `flatten_record` e
    `FLATTENED_COLUMNS`/`COLUMN_TYPES` di `pipeline/ingest.py`): il layout non
    è riscritto qui.
    """
    keep = ["machine_code", "cycle_id", "ts_beg", *DATA_FROM_BULK.values()]
    keep = [c for c in keep if c in df.columns]
    df = df.select(keep).sort(["machine_code", "cycle_id"])
    parts: dict[tuple[str, str], list[pl.DataFrame]] = {}
    for chunk in _chunks(df):
        records = [bulk_row_to_record(r, machine_id)
                   for r in chunk.iter_rows(named=True)]
        by_part: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for rec in records:
            by_part.setdefault(partition_of(rec), []).append(rec)
        for key, recs in by_part.items():
            parts.setdefault(key, []).append(records_to_df(recs))
    out: dict[tuple[str, str], pl.DataFrame] = {}
    for key, frames in parts.items():
        frame = pl.concat(frames, how="vertical")
        out[key] = frame.sort(["valve_id", "cycle_id"])
    return out


def write_partition(frame: pl.DataFrame, out_dir: Path, machine_id: str,
                    date: str, overwrite: bool) -> Path:
    """Scrittura atomica di una partizione (temp + os.replace, come l'ingest)."""
    part_dir = out_dir / f"machine={machine_id}" / f"date={date}"
    part_dir.mkdir(parents=True, exist_ok=True)
    target = part_dir / FILE_NAME
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"{target} esiste già — rifiuto di sovrascrivere storico raw "
            f"senza --overwrite (mai sovrascrittura silenziosa)")
    tmp = target.with_suffix(".parquet.tmp")
    frame.write_parquet(tmp)
    os.replace(tmp, target)
    return target


def replay(source: Path, machine_id: str, out_dir: Path,
           overwrite: bool = False) -> dict[str, Any]:
    df = load_bulk(source)
    parts = build_partitions(df, machine_id)
    written = []
    for (mid, date), frame in sorted(parts.items()):
        path = write_partition(frame, out_dir, mid, date, overwrite)
        written.append({"path": str(path), "rows": frame.height, "date": date})
        logger.info("scritta partizione %s (%d righe)", path, frame.height)
    return {
        "source": str(source),
        "source_rows": df.height,
        "machine_id": machine_id,
        "partitions": written,
        "rows_written": sum(w["rows"] for w in written),
        "columns": list(FLATTENED_COLUMNS),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.raw_replay",
        description="Replay offline di un run bulk nel raw canonico "
                    "data/raw/machine=<id>/date=<UTC>/valve_cycles.parquet.")
    ap.add_argument("--source", required=True,
                    help="work/<run>/valve_cycles.parquet (run bulk congelato)")
    ap.add_argument("--machine-id", required=True,
                    help="identità macchina (il bulk non ce l'ha: mai dedotta)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"root del raw (default: {DEFAULT_OUT})")
    ap.add_argument("--overwrite", action="store_true",
                    help="sovrascrivi le partizioni già presenti")
    return ap


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_arg_parser().parse_args(argv)
    try:
        summary = replay(Path(args.source), args.machine_id, Path(args.out),
                         overwrite=args.overwrite)
    except RawReplayError as exc:
        logger.error("%s", exc)
        return 2
    except FileExistsError as exc:
        logger.error("%s", exc)
        return 3
    for part in summary["partitions"]:
        print(f"{part['path']}: {part['rows']} righe")
    print(f"totale: {summary['rows_written']} righe da "
          f"{summary['source_rows']} righe sorgente")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
