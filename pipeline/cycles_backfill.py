"""Backfill operazionale della tabella `cycles` (M10, ADR-0021) — popola il
data-plane KPI servito da `GET /valves/{id}/kpi`.

Problema chiuso: `pipeline/api.py` serve la serie KPI per ciclo dalla tabella
`cycles` (via `pipeline/cycles_storage.py`, modulo dello stesso pool), ma
nessun modulo la popola dai raw Parquet: senza questo backfill l'endpoint è
un data-plane morto (serie vuota per sempre). QUESTO modulo è l'UNICO writer
del percorso di popolamento: legge i raw per-valvola e bulk-inserta le righe
appiattite in `cycles` tramite `CyclesStorage.bulk_insert` (idempotente,
ON CONFLICT su (run_id, valve_id, cycle_id)).

Fonti raw supportate (rilevamento difensivo per-file, mai silenzioso):

1. **raw appiattito dell'ingest M8** — la fonte del feature service e
   dell'InferenceConsumer (`pipeline/features.load_raw_valve_cycles`,
   `pipeline/inference.RAW_DIR_DEFAULT = data/raw`):
   `data/raw/machine=<machine_id>/date=<UTC>/valve_cycles.parquet`, colonne
   envelope v1.1/v1.3 appiattite da `pipeline/ingest.py` (`machine_id`,
   `cycle_id`, `valve_id`, `event_ts`, `source_ts`, `ingest_ts`, `data.*`,
   `quality.*`);

2. **run bulk del simulatore** — `work/ml_dataset/runs/*/valve_cycles.parquet`
   (UNA run_dir per invocazione, identificata da `--run-id`: ogni run
   rinumera cycle_id per-valvola da 1, quindi la root `runs/` con più run
   collide sull'identità (valve_id, cycle_id) DENTRO lo stesso --run-id e
   viene rifiutata dal guard anti-dup, mai deduplicata silenziosamente.
   Run DIVERSI convivono nel DB perché `run_id` fa parte della chiave):
   colonne bulk di `plcsim/telemetry.py`
   (`machine_code` "valveN", `cycle_id`, `ts_beg`, `fillingtime`, `tailtime`, ...).

Mapping raw → 22 colonne operazionali (spec M10 §5 + oee-backend-spec §A, API):

    colonna operazionale        raw appiattito          run bulk
    --------------------------  ---------------------   ------------------
    run_id                      — (assente → --run-id)  — (assente → --run-id)
    machine_id                  machine_id              — (assente → --machine-id)
    cycle_id                    cycle_id                cycle_id
    valve_id                    valve_id (1-35)         "valveN" → N+1
    filling_time_ms             data.filling_time_ms    fillingtime
    tail_time_ms                data.tail_time_ms       tailtime
    tail_pulse                  data.tail_pulse         tailpulse
    pulse_count                 data.pulse_count        pulsecount
    target                      data.target             target
    delta_pulse                 data.delta_pulse        deltapulse
    filling_step_out            data.filling_step_out   filling_step_out
    filling_ok                  data.filling_ok         fillingok
    fill_quality_ok             data.fill_quality_ok    fill_quality_ok
    sequence_ok                 data.sequence_ok        sequence_ok
    sample_valid                data.sample_valid       sample_valid
    diagnostic_status           data.diagnostic_status  diagnostic_status
    close_reason                data.close_reason       close_reason
    position_limit              data.position_limit     position_limit
    filling_overtime            data.filling_overtime   filling_overtime
    event_ts                    event_ts (ISO string)   ts_beg
    source_ts                   source_ts (ISO string)  — (NULL)
    ingest_ts                   ingest_ts (ISO string)  — (NULL)

Politica di onestà (nessuna fabbricazione):

- `delta_pulse` è mappato AS-IS dalla fonte raw (`data.delta_pulse` /
  `deltapulse`): per CONTEXT.md vale `delta_pulse = target − pulse_count`
  (negativo = sovra-riempimento) e la fonte raw è la computazione del
  simulatore stesso (verificata identica al 100% delle righe dei run bulk).
  NON viene ricalcolato: ricalcolarlo rischierebbe skew col record raw e
  sarebbe comunque una riscrittura di semantica.
- campo SENZA fonte raw nel layout in uso → NULL, MAI valore inventato. In
  particolare: (a) il raw v1.1 pre-M9 (12 campi) non ha
  close_reason/position_limit/filling_overtime → NULL per quelle righe
  (NB: `pipeline/features.py` li azzera coi default "sani" per l'anti-skew
  ML — QUI no: il data-plane operazionale deve essere onesto, non
  skew-safe); (b) i run bulk non hanno `machine_id` → NULL nel mapping
  puro, ma il DDL di `cycles` lo vuole NOT NULL: il backfill richiede
  `--machine-id` ESPLICITO per quelle fonti (mai dedotto, vedi sotto);
  (c) `event_ts`/`source_ts`/`ingest_ts` assenti nella sorgente → NULL
  (OEE window su NULL → degraded, mai fabbricato). Bulk: `ts_beg` → `event_ts`,
  `source_ts`/`ingest_ts` → NULL.
- nessuna deduzione di identità: `valve_id` dei run bulk deriva da
  `machine_code` "valveN" con rinomina 1:1 (N+1, contratto valve_id 1-35
  usato anche da features.py all'inverso), non da altro.
- righe duplicate (valve_id, cycle_id) nella fonte → errore CHIARO prima
  dell'insert (mai scelta silenziosa di quale riga vince): stessa policy di
  `plcsim/ml_dataset.py`.

`run_id` (NOT NULL nel DDL, prima colonna della PK): discriminante di run.
NESSUNA fonte raw lo trasporta e NON viene MAI dedotto dal percorso (stessa
policy di `machine_id`): `--run-id <id>` è OBBLIGATORIO e l'assenza è un
errore chiaro PRIMA di toccare il DB. Senza di esso un secondo run verrebbe
scartato in silenzio da ON CONFLICT (cycle_id riparte da 1 a ogni run) e i
due run, sovrapposti nel tempo di parete, non sarebbero separabili nemmeno
filtrando `event_ts`.

Idempotenza: `CyclesStorage.bulk_insert` usa ON CONFLICT DO NOTHING su
(run_id, valve_id, cycle_id) — il re-run dell'intero raw è sicuro e riporta
`rows_inserted=0` per le righe già presenti.

Chunking (obbligatorio, NON ottimizzazione): PostgreSQL accetta al massimo
65.535 bind params per statement (protocollo wire) e `bulk_insert` compila
un unico INSERT multi-VALUES (RETURNING + ON CONFLICT bypassa
insertmanyvalues di SQLAlchemy): 22 colonne/riga ⇒ massimo ~2.978 righe per
statement. Questo modulo spezza il batch in chunk da
`BULK_INSERT_CHUNK_ROWS` (1.000 righe = 22.000 params, conservativo):
l'idempotenza resta per-chunk (ON CONFLICT), i volumi reali (≈17k cicli/
valvola/run, 600k+/run) richiedono il chunking per qualunque fonte.

Memoria (2026-08-19): il backfill NON materializza più l'intero run in RAM.
`cycles.to_dicts()` su un run sano da 5 giorni sono 3 milioni di dict (36
milioni a 60 giorni): `_iter_record_chunks` produce i record a blocchi di
`BULK_INSERT_CHUNK_ROWS` righe con `slice()` sul frame polars, quindi in RAM
sta al più un chunk di dict alla volta. Semantica invariata: stesso ordine,
stessa idempotenza, stesso `rows_inserted`.

`machine_id` (NOT NULL nel DDL di cycles_storage):
- layout appiattito → pass-through dal raw (envelope header);
- layout run bulk → la fonte NON ce l'ha: NON si deduce (mai indovinare
  l'identità macchina), si passa `--machine-id <id>` esplicitamente (es.
  `filler01`), altrimenti errore CHIARO prima di toccare il DB.

CLI::

    python -m pipeline.cycles_backfill --run-id <id> --raw-dir <path> [--db-url <url>] [--valve N] [--machine-id <id>]

`--run-id` è OBBLIGATORIO (mai dedotto dal percorso).

Exit codes: 0 = ok; 1 = errore operativo (DB non raggiungibile,
CyclesStorage non ancora installato); 2 = fonte raw assente/vuota/illeggibile
o formato non supportato.

Determinismo: trasformazioni polars pure, nessun RNG; output ordinato per
(run_id, valve_id, cycle_id).
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import polars as pl

logger = logging.getLogger("pipeline.cycles_backfill")

# ---------------------------------------------------------------------------
# Contratto operazionale (spec M10 §5 + oee-backend-spec §A) — 22 colonne, ordine canonico.
# ---------------------------------------------------------------------------
CYCLES_COLUMNS: tuple[str, ...] = (
    "run_id",
    "machine_id", "cycle_id", "valve_id",
    "filling_time_ms", "tail_time_ms", "tail_pulse",
    "pulse_count", "target", "delta_pulse", "filling_step_out",
    "filling_ok", "fill_quality_ok", "sequence_ok", "sample_valid",
    "diagnostic_status", "close_reason", "position_limit", "filling_overtime",
    "event_ts", "source_ts", "ingest_ts",
)

# raw appiattito (envelope v1.1/v1.3, pipeline/ingest.py): data.* → operativo
_FLAT_RENAME: dict[str, str] = {
    "data.filling_time_ms": "filling_time_ms",
    "data.tail_time_ms": "tail_time_ms",
    "data.tail_pulse": "tail_pulse",
    "data.pulse_count": "pulse_count",
    "data.target": "target",
    "data.delta_pulse": "delta_pulse",
    "data.filling_step_out": "filling_step_out",
    "data.filling_ok": "filling_ok",
    "data.fill_quality_ok": "fill_quality_ok",
    "data.sequence_ok": "sequence_ok",
    "data.sample_valid": "sample_valid",
    "data.diagnostic_status": "diagnostic_status",
    "data.close_reason": "close_reason",
    "data.position_limit": "position_limit",
    "data.filling_overtime": "filling_overtime",
}
_FLAT_KEYS = ("machine_id", "cycle_id", "valve_id", "event_ts", "source_ts", "ingest_ts")

# run bulk (plcsim/telemetry.py): colonne bulk → operativo
_BULK_RENAME: dict[str, str] = {
    "fillingtime": "filling_time_ms",
    "tailtime": "tail_time_ms",
    "tailpulse": "tail_pulse",
    "pulsecount": "pulse_count",
    "target": "target",
    "deltapulse": "delta_pulse",
    "filling_step_out": "filling_step_out",
    "fillingok": "filling_ok",
    "fill_quality_ok": "fill_quality_ok",
    "sequence_ok": "sequence_ok",
    "sample_valid": "sample_valid",
    "diagnostic_status": "diagnostic_status",
    "close_reason": "close_reason",
    "position_limit": "position_limit",
    "filling_overtime": "filling_overtime",
    "ts_beg": "event_ts",
}
_BULK_KEYS = ("machine_code", "cycle_id")

_MACHINE_CODE_RE = re.compile(r"^valve(\d+)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Timestamp columns (nuove per OEE windowabile)
_TS_COLS: tuple[str, ...] = ("event_ts", "source_ts", "ingest_ts")


# limite protocollo PostgreSQL: 65.535 bind params/statement; 22 colonne ⇒
# max ~2.978 righe; 1.000 righe/statement = 22.000 params (conservativo).
BULK_INSERT_CHUNK_ROWS = 1000

# Il cursore vive nella KV ``machine_state`` già esistente.  Una chiave per
# coppia (run, data) evita di far collidere due acquisizioni che riusano i
# cycle_id e, soprattutto, non usa la chiave operativa ``current_run_id``.
CURSOR_KEY_PREFIX = "cycles_backfill_cursor"


# ---------------------------------------------------------------------------
# Errori (chiari, mai crash silenziosi)
# ---------------------------------------------------------------------------
class BackfillError(Exception):
    """Errore di dominio del backfill (messaggio già operativo)."""


class NoRawDataError(BackfillError):
    """Fonte raw assente o vuota."""


class RawFormatError(BackfillError):
    """Formato raw non supportato o incoerente (mai deduzione silenziosa)."""


class CyclesStorageUnavailable(BackfillError):
    """`pipeline/cycles_storage.py` non ancora installato (integrazione differita)."""


# ---------------------------------------------------------------------------
# Mapping (pure polars, deterministico)
# ---------------------------------------------------------------------------
def _detect_layout(df: pl.DataFrame) -> str:
    """Rileva il layout del file: "flattened" (ingest M8) o "bulk" (run).

    Difensivo e mai silenzioso: layout sconosciuto → RawFormatError con la
    lista delle colonne viste (aiuta a diagnosticare fonti nuove).
    """
    cols = set(df.columns)
    if "data.filling_time_ms" in cols or (
            "machine_id" in cols and "cycle_id" in cols and "data.filling_ok" in cols):
        return "flattened"
    if "fillingtime" in cols and "machine_code" in cols and "cycle_id" in cols:
        return "bulk"
    raise RawFormatError(
        f"layout raw non riconosciuto (colonne: {sorted(cols)})")


def _frame_to_cycles(df: pl.DataFrame, rename: dict[str, str],
                     keep: Iterable[str]) -> pl.DataFrame:
    """Rinomina le colonne note e conserva le chiavi; il resto → _finalize.

    Solo rinomina 1:1 (nessuna semantica ricostruita): le colonne assenti
    nel raw NON vengono create qui — `_finalize` le lascia NULL (onestà).
    """
    out = df.rename({k: v for k, v in rename.items() if k in df.columns})
    keep_cols = [c for c in keep if c in out.columns]
    return out.select(keep_cols + [c for c in rename.values() if c in out.columns])


def _normalize_timestamps(frame: pl.DataFrame) -> pl.DataFrame:
    """Converte event_ts/source_ts/ingest_ts String (ISO) → Datetime UTC.

    - raw appiattito: event_ts/source_ts/ingest_ts sono String ISO (es.
      "2026-08-01T10:00:00Z" o "2026-08-01T10:00:00.123456+00:00") → timestamptz
      via str.to_datetime(time_zone="UTC") (deterministico, nessun RNG);
    - bulk: ts_beg → event_ts è già Datetime UTC → lascia stare (solo cast
      se necessario per coerenza di timezone);
    - assente → non toccare (la crea _finalize come null tipizzato);
    - null → resta null.
    """
    for col in _TS_COLS:
        if col not in frame.columns:
            continue
        dtype = frame[col].dtype
        if dtype == pl.String:
            frame = frame.with_columns(pl.col(col).str.to_datetime(time_zone="UTC").alias(col))
        elif dtype == pl.Datetime:
            # assicurati che sia UTC-aware (bulk già UTC, ma difensivo)
            tz = getattr(dtype, "time_zone", None)
            if tz != "UTC":
                # cast difensivo: se senza timezone, interpreta come UTC
                frame = frame.with_columns(pl.col(col).cast(pl.Datetime(time_zone="UTC")).alias(col))
        elif dtype == pl.Null:
            frame = frame.with_columns(pl.col(col).cast(pl.Datetime(time_zone="UTC")).alias(col))
        else:
            # altro tipo inatteso: prova conversione generica (mai fabbricare)
            try:
                frame = frame.with_columns(pl.col(col).cast(pl.Datetime(time_zone="UTC")).alias(col))
            except Exception:
                pass
    return frame


def _finalize(frame: pl.DataFrame) -> pl.DataFrame:
    """Allinea al contratto: ESATTAMENTE le 22 colonne, ordine canonico.

    Colonna mancante → NULL (nessuna fabbricazione, vedi docstring).
    Timestamp: se presente come String → convertito a Datetime UTC; se
    mancante → NULL tipizzato Datetime UTC (non generico Null) così i
    concat verticali tra file non divergono di tipo.
    """
    # normalizza timestamp prima di allineare (se sono stringhe)
    frame = _normalize_timestamps(frame)
    cols_expr = []
    for c in CYCLES_COLUMNS:
        if c in frame.columns:
            # se è una timestamp rimasta String (caso non coperto sopra), converti
            if c in _TS_COLS and frame[c].dtype == pl.String:
                cols_expr.append(pl.col(c).str.to_datetime(time_zone="UTC").alias(c))
            else:
                cols_expr.append(pl.col(c))
        else:
            if c in _TS_COLS:
                cols_expr.append(pl.lit(None, dtype=pl.Datetime(time_zone="UTC")).alias(c))
            else:
                cols_expr.append(pl.lit(None).alias(c))
    return frame.select(cols_expr)


def _bulk_valve_id(machine_code: pl.Series) -> pl.Series:
    """machine_code "valveN" (0-based, telemetry) → valve_id N+1 (1-35).

    Rinomina 1:1 dell'identità valvola, non una deduzione: è l'inverso
    esatto della mappa `valve{valve_id-1}` di pipeline/features.py. Valore
    fuori pattern → RawFormatError (mai indovinare).
    """
    def _parse(value: object) -> int:
        if isinstance(value, str):
            m = _MACHINE_CODE_RE.fullmatch(value)
            if m:
                return int(m.group(1)) + 1
        raise RawFormatError(
            f"machine_code fuori pattern 'valveN' (valore: {value!r}) — "
            "nessuna deduzione di identità valvola")
    return machine_code.map_elements(_parse, return_dtype=pl.Int64)


def _load_flattened_file(path: Path) -> pl.DataFrame:
    raw = pl.read_parquet(path)
    mapped = _frame_to_cycles(raw, _FLAT_RENAME, _FLAT_KEYS)
    # _finalize normalizza event_ts/source_ts/ingest_ts da String → Datetime UTC
    return _finalize(mapped)


def _load_bulk_file(path: Path) -> pl.DataFrame:
    raw = pl.read_parquet(path)
    mapped = _frame_to_cycles(raw, _BULK_RENAME, _BULK_KEYS)
    if "machine_code" not in mapped.columns:
        raise RawFormatError(f"{path}: file bulk senza machine_code")
    mapped = mapped.with_columns(_bulk_valve_id(mapped["machine_code"]).alias("valve_id"))
    mapped = mapped.drop("machine_code")
    # machine_id: i run bulk non hanno la colonna → resta NULL (onestà)
    # event_ts deriva da ts_beg (già Datetime UTC); source_ts/ingest_ts → NULL
    mapped = _normalize_timestamps(mapped)
    return _finalize(mapped)


def _normalize_dates(dates: Optional[Iterable[str] | str]) -> tuple[str, ...] | None:
    """Normalizza e valida le partizioni ``date=YYYY-MM-DD`` richieste."""
    if dates is None:
        return None
    if isinstance(dates, str):
        dates = (dates,)
    normalized: set[str] = set()
    for value in dates:
        text = str(value).strip()
        if not _DATE_RE.fullmatch(text):
            raise ValueError(
                f"date non valida {value!r}: usare YYYY-MM-DD")
        normalized.add(text)
    if not normalized:
        raise ValueError("dates non può essere vuoto")
    return tuple(sorted(normalized))


def _resolve_files(raw_dir: str | Path,
                   dates: Optional[Iterable[str] | str] = None) -> list[Path]:
    """File valve_cycles.parquet candidati (file diretto o scansione **).

    `**/valve_cycles.parquet` copre entrambi i layout: quello appiattito è
    `machine=*/date=*/valve_cycles.parquet` (sotto-radice di data/raw) e la
    run bulk può essere puntata direttamente o via `runs/`.

    Quando ``dates`` è esplicito, sono ammesse solo directory padre esatte
    ``date=YYYY-MM-DD``. Questo evita che il percorso live possa selezionare
    implicitamente altre partizioni (in particolare giugno).
    """
    wanted = _normalize_dates(dates)
    p = Path(raw_dir)
    if p.is_file():
        if wanted is None or p.parent.name in {f"date={d}" for d in wanted}:
            return [p.resolve()]
        return []
    if wanted is None:
        return sorted({f.resolve() for f in p.glob("**/valve_cycles.parquet")})

    files: set[Path] = set()
    # `**/date=<requested>/...` keeps the filesystem walk constrained to the
    # exact partition names supplied by the caller.
    for value in wanted:
        partition = f"date={value}"
        if p.name == partition:
            candidate = p / "valve_cycles.parquet"
            if candidate.is_file():
                files.add(candidate.resolve())
        files.update(
            f.resolve() for f in p.glob(f"**/{partition}/valve_cycles.parquet")
            if f.parent.name == partition
        )
    return sorted(files)


def load_cycles(raw_dir: str | Path,
                valve: Optional[int] = None,
                dates: Optional[Iterable[str] | str] = None) -> pl.DataFrame:
    """Legge i raw per-valvola e li mappa alle 22 colonne operazionali.

    `run_id` NON è trasportato da nessuna fonte raw: resta NULL qui e viene
    valorizzato da `backfill()` con il `--run-id` esplicito.

    - `raw_dir`: root del raw (layout 1: `data/raw`) O root/path dei run
      bulk (layout 2, es. `work/ml_dataset/runs` o una singola run_dir);
    - `valve`: filtro opzionale su valve_id 1-35 (contratto operazionale,
      NON 0-based).
    - `dates`: partizioni esplicite `YYYY-MM-DD`; quando presente seleziona
      esclusivamente directory con nome esatto `date=<data>`.

    Ritorna un frame con ESATTAMENTE `CYCLES_COLUMNS`, ordinato per
    (valve_id, cycle_id). Fonte assente/vuota → NoRawDataError; layout
    misto o sconosciuto → RawFormatError; duplicati (valve_id, cycle_id) →
    RawFormatError (mai scelta silenziosa di riga).
    """
    raw_dir = Path(raw_dir)
    normalized_dates = _normalize_dates(dates)
    # difensivo: `--raw-dir` può puntare DIRETTAMENTE a un valve_cycles.parquet
    # (es. una run_dir singola del simulatore) — comodo per operatori
    if raw_dir.is_file():
        if raw_dir.name != "valve_cycles.parquet":
            raise NoRawDataError(
                f"{raw_dir} non è un valve_cycles.parquet (puntare la root "
                "del raw, una run_dir o un file valve_cycles.parquet)")
        files = _resolve_files(raw_dir, dates=normalized_dates)
    else:
        if not raw_dir.is_dir():
            raise NoRawDataError(
                f"raw dir non trovata o non è una directory: {raw_dir}")
        files = _resolve_files(raw_dir, dates=normalized_dates)
        if not files:
            raise NoRawDataError(
                f"nessun valve_cycles.parquet sotto {raw_dir} — fonte raw vuota "
                "(layout atteso: machine=*/date=*/valve_cycles.parquet o run bulk)")

    frames: list[pl.DataFrame] = []
    layouts: set[str] = set()
    for path in files:
        frame = pl.read_parquet(path)
        layout = _detect_layout(frame)
        layouts.add(layout)
        mapped = (_load_flattened_file(path) if layout == "flattened"
                  else _load_bulk_file(path))
        frames.append(mapped)
    if len(layouts) > 1:
        raise RawFormatError(
            f"layout raw MISTI nella stessa fonte ({sorted(layouts)}) — "
            "riprocessare separatamente: nessun merge implicito")

    cycles = pl.concat(frames, how="vertical")
    if cycles.height == 0:
        raise NoRawDataError(f"fonte raw vuota (0 righe totali in {len(files)} file): {raw_dir}")

    # duplicati (valve_id, cycle_id) → errore chiaro (policy ml_dataset).
    # ATTENZIONE: la chiave va isolata. `cycles.is_duplicated()` sull'intero
    # frame confronta TUTTE le colonne, quindi due run diversi con la stessa
    # (valve_id, cycle_id) ma misure diverse — il caso reale — passavano il
    # guard e finivano al DB, dove `ON CONFLICT DO NOTHING` li scartava in
    # silenzio abbassando `rows_inserted` senza dire perche'. Verificato:
    # polars marca duplicata solo la riga identica su tutte le colonne.
    dup = cycles.select(["valve_id", "cycle_id"]).is_duplicated()
    if dup.any():
        n_dup = int(dup.sum())
        raise RawFormatError(
            f"{n_dup} righe duplicate su (valve_id, cycle_id) nella fonte — "
            "dato raw incoerente, nessuna riga scelta silenziosamente. "
            "Causa tipica: più run del simulatore sotto la stessa root (ogni "
            "run rinumera cycle_id per-valvola da 1) — puntare a UNA singola "
            "run_dir (es. work/ml_dataset/runs/faults_train_a), non alla root")

    cycles = cycles.sort(["run_id", "valve_id", "cycle_id"])
    if valve is not None:
        if not 1 <= valve <= 35:
            raise ValueError(
                f"valve {valve} fuori contratto 1-35 (valve_id operazionale)")
        cycles = cycles.filter(pl.col("valve_id") == valve)
    return cycles


# ---------------------------------------------------------------------------
# Backfill (CyclesStorage, integrazione differita se il modulo non c'è)
# ---------------------------------------------------------------------------
@dataclass
class BackfillResult:
    """Esito del backfill (numeri leggibili dagli script, protocollo §5)."""
    files: list[Path]
    rows_read: int
    rows_mapped: int
    rows_inserted: int


def _iter_record_chunks(cycles: "pl.DataFrame",
                        chunk_rows: int = BULK_INSERT_CHUNK_ROWS):
    """Record del frame a blocchi di `chunk_rows` — MAI l'intero run in RAM.

    `cycles.to_dicts()` materializzava 3 milioni di dict per un run da 5
    giorni (36 milioni a 60 giorni). `slice()` su polars è una vista
    zero-copy: si convertono in dict solo le righe del chunk corrente, che
    viene rilasciato appena inserito. Ordine e contenuto identici a
    `to_dicts()` letto a blocchi ⇒ idempotenza e `rows_inserted` invariati.
    """
    total = cycles.height
    for start in range(0, total, chunk_rows):
        yield cycles.slice(start, chunk_rows).to_dicts()


def _cursor_key(run_id: str, date: str) -> str:
    """Ritorna la chiave KV dedicata a un run e a una partizione data."""
    return f"{CURSOR_KEY_PREFIX}:{run_id}:{date}"


def _read_cursor(state: object, run_id: str, date: str) -> str | None:
    """Legge l'high-water mark di ARRIVO (``ingest_ts``) dalla KV.

    Ritorna una stringa ISO, oppure ``None`` quando il cursore è assente o in
    un formato che non si sa leggere — inclusa la forma ``per_valve``
    precedente (vedi ``_filter_with_cursor``). ``None`` significa "leggi
    tutto": le righe già presenti vengono comunque scartate dall'
    ``ON CONFLICT`` del writer, quindi un cursore mancante costa tempo, mai
    correttezza.
    """
    raw = state.get_machine_state(_cursor_key(run_id, date))
    if not isinstance(raw, dict):
        return None
    value = raw.get("max_ingest_ts")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _write_cursor(state: object, run_id: str, date: str,
                  max_ingest_ts: str | None) -> None:
    """Persisti il cursore dopo un ``bulk_insert`` concluso senza errori."""
    if not max_ingest_ts:
        return
    state.set_machine_state(
        _cursor_key(run_id, date),
        {"version": 2, "max_ingest_ts": max_ingest_ts},
    )


def _filter_with_cursor(cycles: "pl.DataFrame",
                        cursor: str | None) -> "pl.DataFrame":
    """Seleziona le righe ARRIVATE dall'ultimo giro in poi.

    Il confronto è su ``ingest_ts`` — l'istante in cui l'ingest ha scritto la
    riga — e NON sul ``cycle_id``.

    PERCHE' NON IL cycle_id (misurato il 2026-08-22 su una corsa live vera):
    l'high-water mark per valvola sul ``cycle_id`` dà per scontato che i
    cicli arrivino in ordine crescente, e non è vero. Alla ripartenza di
    Node-RED la prima lettura della subscription consegna il valore CORRENTE
    di ``LastCycleId``: per la valvola 1 è arrivato un `cycle_id` **274**
    prima della sequenza reale, che è poi ripartita da 4. Il cursore ha preso
    274 come soglia e ogni ciclo autentico successivo — 4, 5, 6, … — è
    risultato "già visto" ed è stato scartato. Per sempre, e in silenzio: il
    log diceva «backfill ok … 0 righe inserite», che si legge come "niente di
    nuovo" e non come "ho buttato via 643 righe".

    ``ingest_ts`` non ha quel problema perché è assegnato dall'ingest
    nell'ordine di scrittura: una riga che arriva tardi ha comunque un
    ``ingest_ts`` più grande, quindi passa il filtro. Il confronto è ``>=``,
    non ``>``: le righe di uno stesso flush condividono l'istante e con ``>``
    quelle sul bordo si perderebbero. La sovrapposizione di un flush la
    risolve l'``ON CONFLICT`` del writer, che è idempotente per contratto —
    si paga qualche riga riproposta e non si perde niente.

    Un cursore in formato ``per_valve`` (versione 1) viene letto come
    assente: si rilegge la partizione una volta e il writer deduplica.
    """
    if cycles.is_empty() or not cursor:
        return cycles
    if "ingest_ts" not in cycles.columns:
        # Sorgente senza ingest_ts (raw bulk storico): nessun cursore
        # applicabile, si rilegge tutto e deduplica il writer.
        return cycles
    try:
        soglia = datetime.fromisoformat(cursor)
    except (TypeError, ValueError):
        return cycles
    return cycles.filter(
        pl.col("ingest_ts").is_null() | (pl.col("ingest_ts") >= soglia))


def _insert_cycles(cycles: "pl.DataFrame", store: object,
                   state: object | None, run_id: str,
                   date: str | None) -> int:
    """Inserisce un frame e, per il live, avanza il cursore dopo ogni batch."""
    usa_cursore = state is not None and bool(date)
    cursor = _read_cursor(state, run_id, date) if usa_cursore else None
    pending = _filter_with_cursor(cycles, cursor) if usa_cursore else cycles
    # L'avanzamento si calcola UNA volta sul frame che stiamo per scrivere:
    # e' il massimo ingest_ts delle righe considerate, non del chunk, cosi'
    # il cursore non dipende da come il writer spezza i lotti.
    avanzamento: str | None = None
    if usa_cursore and not pending.is_empty() and "ingest_ts" in pending.columns:
        massimo = pending.get_column("ingest_ts").max()
        if massimo is not None:
            avanzamento = (massimo.isoformat()
                           if hasattr(massimo, "isoformat") else str(massimo))
    inserted = 0
    for chunk in _iter_record_chunks(pending, BULK_INSERT_CHUNK_ROWS):
        inserted += int(store.bulk_insert(chunk))
    # Il cursore si scrive *dopo* i bulk_insert: se il writer fallisce a
    # meta', il giro successivo rilegge da dove eravamo e il writer
    # deduplica. Nessuna riga confermata viene saltata al riavvio.
    if usa_cursore:
        _write_cursor(state, run_id, date, avanzamento)
    return inserted


def backfill(raw_dir: str | Path,
             run_id: str,
             db_url: Optional[str] = None,
             valve: Optional[int] = None,
             machine_id: Optional[str] = None,
             dates: Optional[Iterable[str] | str] = None) -> BackfillResult:
    """Backfill completo: raw → 22 colonne → `CyclesStorage.bulk_insert`.

    - `run_id`: OBBLIGATORIO, discriminante di run (prima colonna della PK).
      Nessuna fonte raw lo trasporta e non viene MAI dedotto dal percorso:
      mancante/vuoto → `BackfillError` PRIMA di toccare il DB, esattamente
      come `machine_id`;

    - import LAZY di `pipeline.cycles_storage` (modulo dello stesso pool,
      può non essere ancora installato): assente → CyclesStorageUnavailable
      con messaggio chiaro (integrazione differita, MAI fabbricare righe);
    - engine via `pipeline.storage.make_engine` (contratto ADR-0021, già
      landed): `db_url=None` → `PLCSIM_DATABASE_URL` o default POC;
    - `machine_id`: override esplicito per le fonti che non lo trasportano
      (run bulk) — il DDL di `cycles` lo ha NOT NULL; se dopo il mapping
      restano NULL su machine_id e NON è fornito → errore chiaro PRIMA del
      DB (mai dedurre l'identità macchina, mai inserire NULL su NOT NULL);
    - `init()` di CyclesStorage se presente (creazione tabella if-not-exists,
      idempotente): il backfill è self-sufficient per l'operatore;
    - `bulk_insert(records)` è l'UNICO meccanismo di dedup: niente
      check-then-insert (no TOCTOU) — ON CONFLICT (run_id, valve_id, cycle_id);
    - i record sono prodotti a blocchi (`_iter_record_chunks`): l'intero run
      non entra mai in RAM.
    """
    if run_id is None or not str(run_id).strip():
        raise BackfillError(
            "run_id mancante: passare --run-id <id> esplicitamente — il run "
            "non viene MAI dedotto dal percorso. Senza discriminante di run "
            "un secondo caricamento verrebbe scartato in silenzio da "
            "ON CONFLICT (cycle_id riparte da 1 a ogni run)")
    run_id = str(run_id).strip()
    normalized_dates = _normalize_dates(dates)

    # Nel live ogni partizione ha il proprio cursore.  Caricare le date una
    # alla volta evita che il progresso di una data possa filtrare quella di
    # un'altra (e consente cycle_id che ripartono a ogni partizione).
    date_frames: list[tuple[str | None, pl.DataFrame]] = []
    if normalized_dates is None:
        date_frames.append((None, load_cycles(raw_dir, valve=valve)))
        files = _resolve_files(raw_dir)
    else:
        files = []
        for date in normalized_dates:
            date_files = _resolve_files(raw_dir, dates=(date,))
            if not date_files:
                continue
            frame = load_cycles(raw_dir, valve=valve, dates=(date,))
            date_frames.append((date, frame))
            files.extend(date_files)
        if not date_frames:
            raise NoRawDataError(
                f"nessun valve_cycles.parquet nelle date richieste {normalized_dates}")
        files = sorted(set(files))

    prepared: list[tuple[str | None, pl.DataFrame]] = []
    for date, frame in date_frames:
        frame = frame.with_columns(pl.lit(run_id).alias("run_id"))
        if machine_id is not None:
            frame = frame.with_columns(pl.lit(machine_id).alias("machine_id"))
        null_machine = int(frame["machine_id"].null_count())
        if null_machine:
            raise BackfillError(
                f"{null_machine} righe senza machine_id (fonte senza la colonna, "
                "es. run bulk): passare --machine-id <id> esplicitamente — "
                "l'identità macchina non viene MAI dedotta")
        prepared.append((date, frame))
    rows_read = sum(int(frame.height) for _, frame in prepared)

    try:
        from pipeline.cycles_storage import CyclesStorage  # noqa: PLC0415
    except ImportError as exc:
        raise CyclesStorageUnavailable(
            "pipeline.cycles_storage non ancora installato: mapping raw "
            "verificato, insert differito (integrare quando il modulo "
            "arriva)") from exc

    from pipeline.storage import make_engine
    engine = make_engine(db_url)
    store = CyclesStorage(engine)
    init = getattr(store, "init", None)
    if callable(init):
        init()  # if-not-exists: self-sufficient, idempotente

    # Storage è usato solo per il percorso a date esplicite: i backfill bulk
    # storici non devono creare né leggere stato di avanzamento live.
    state = None
    if normalized_dates is not None:
        from pipeline.storage import Storage
        state = Storage(engine)

    rows_inserted = 0
    for date, frame in prepared:
        # 22 chiavi canoniche, valori None → SQL NULL
        rows_inserted += _insert_cycles(
            frame, store, state, run_id, date)
    return BackfillResult(
        files=files, rows_read=rows_read, rows_mapped=rows_read,
        rows_inserted=rows_inserted)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.cycles_backfill",
        description="Backfill della tabella `cycles` (data-plane KPI di "
                    "GET /valves/{id}/kpi) dai raw Parquet per-valvola.",
    )
    parser.add_argument("--run-id", required=True,
                        help="OBBLIGATORIO: discriminante di run (prima "
                             "colonna della PK). Mai dedotto dal percorso")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"),
                        help="root del raw (layout ingest: data/raw; layout "
                             "run bulk: work/ml_dataset/runs o una run_dir)")
    parser.add_argument("--db-url", default=None,
                        help="URL PostgreSQL (default: $PLCSIM_DATABASE_URL "
                             "o POC locale)")
    parser.add_argument("--valve", type=int, default=None,
                        help="solo la valvola N (valve_id operazionale 1-35)")
    parser.add_argument("--machine-id", default=None,
                        help="machine_id esplicito per fonti che non lo "
                             "trasportano (run bulk); mai dedotto")
    parser.add_argument("--dates", nargs="+", metavar="YYYY-MM-DD",
                        help="partizioni live esplicite; seleziona solo "
                             "directory date=YYYY-MM-DD (mai la root implicita)")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("run_id=%s raw_dir=%s dates=%s db_url=%s valve=%s machine_id=%s",
                args.run_id, args.raw_dir, args.dates or "<tutte>",
                args.db_url or "<default>", args.valve, args.machine_id)
    try:
        result = backfill(args.raw_dir, run_id=args.run_id,
                          db_url=args.db_url, valve=args.valve,
                          machine_id=args.machine_id, dates=args.dates)
    except BackfillError as exc:
        logger.error("%s", exc)
        return 2 if isinstance(exc, (NoRawDataError, RawFormatError)) else 1
    except Exception as exc:  # noqa: BLE001 — errore operativo, messaggio chiaro
        logger.error("backfill fallito: %s", exc)
        return 1
    logger.info(
        "backfill ok: %d file, %d righe lette, %d righe mappate, %d righe "
        "inserite (re-run idempotente: inserite=0 per righe già presenti)",
        len(result.files), result.rows_read, result.rows_mapped,
        result.rows_inserted)
    return 0


__all__ = [
    "CYCLES_COLUMNS", "BackfillResult", "backfill", "load_cycles",
    "NoRawDataError", "RawFormatError", "CyclesStorageUnavailable",
    "BackfillError", "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
