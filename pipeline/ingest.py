"""Consumer MQTT della pipeline dati (issue M8-04, spec M8 §3/§5.3/§6) — M8.

Entry point: ``python -m pipeline.ingest`` (CLI spec §6.4). Sottoscrive i
topic v1 in QoS 1 con sessione persistente, valida l'envelope
(wire v1.0/v1.2 -> iniezione ``ingest_ts`` -> stored v1.1/v1.3, spec §5.3),
deduplica su ``event_id`` (exactly-once logico, §6.3) e scrive storico raw
Parquet partizionato in
``data/raw/machine=<machine_id>/date=<event_ts UTC>/valve_cycles.parquet``
con flush atomico (temp + ``os.replace``, §6.2).

Catena di validazione (pipeline/validator.py, issue M8-02 — spec §5.3)::

    wire v1.0/v1.2 valid? ──no──▶ scarta + schema_invalid (log sample)
          │ sì (schema_version ∈ {1.0, 1.2} && ingest_ts assente)
          ▼
    inject_ingest_ts(payload, now_utc)  (ingest_ts + bump: 1.0→1.1, 1.2→1.3)
          ▼
    stored v1.1/v1.3 valid? ──no──▶ BUG (guardia): scarta, MAI scrivere
          │                           un record stored-invalido (spec §8 T6)
          ▼
    dedup su event_id -> Parquet partizionato (flush atomico)

Contratto e confini (spec §2, §11):
- il consumer NON scrive sul broker, NON tocca OPC UA, NON espone la ground
  truth del simulatore (invariante §2.4) e il dominio dati è machine-agnostico
  (topic ``plant/filler01/...``, nessun riferimento al simulatore — §2.5);
- MQTT: QoS 1, ``clean_session=False`` + client id fisso ``plcsim-ingest-v1``
  (sessione persistente: il broker accoda i messaggi durante il down del
  consumer, §6.1); reconnect automatico con backoff esponenziale 1s->60s +
  jitter (§6.1); PUBACK emesso dalla libreria DOPO il ritorno di
  ``on_message`` (``_handle_publish`` di paho: ack dopo il callback).

Ordine critico ack/flush (interpretazione documentata, da calibrare):
il flush a soglia (``--flush-records``) avviene in modo sincrono DENTRO il
callback del messaggio che completa il batch: il PUBACK di quel messaggio
(emesso dalla libreria dopo il callback) segue quindi la scrittura atomica.
I messaggi precedenti dello stesso batch vengono ackati dopo il buffering
(validazione + aggiornamento dedup set sincroni nel callback). La finestra
di perdita su crash-hard (SIGKILL) è quindi limitata al batch in memoria
(≤ ``--flush-records`` record, o la finestra ``--flush-seconds``) — il
broker non li reinvia (già ackati). Chiusura ordinata (SIGINT/SIGTERM):
flush finale + stats, nessuna perdita. Il fallimento di scrittura NON ferma
il consumer: i record restano in memoria e vengono ritentati al flush
successivo (DLQ fuori scope obbligatorio M8, §6.1).

Dedup (spec §6.3): ``set[str]`` in memoria + rebuild all'avvio da
``data/raw/**/valve_cycles.parquet`` (colonna ``event_id``); se la rebuild
fallisce -> WARNING + set vuoto (documentato). Opzione ``--dedup-store
<path>`` (OFF di default): JSONL append-only ``{"event_id": "..."}`` per
riga, aggiornato ad ogni flush DOPO la scrittura atomica del Parquet (ordine
anti-perdita: se il processo muore tra le due operazioni, la rebuild da
Parquet copre gli id mancanti nello store). Limite documentato: set in RAM
O(N); volumi POC (≤ 945k msg/giorno fleet, spec §9) ampiamente sotto soglia.

Stats (spec §6.1/§6.4): ``received, written, duplicates, json_invalid,
schema_invalid, reconnect_count, parquet_written`` — log periodico (60 s),
a chiusura e, se ``--stats-json``, scrittura JSON atomica (temp +
``os.replace``) a ogni tick periodico e alla chiusura (numeri del report di
accettazione letti da script, protocollo §5).

Dipendenze: paho-mqtt==2.1.0 (pin M8, ADR-0019), polars==1.43.2 (già
pinnato), jsonschema (edge/requirements.txt, M7).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl
from jsonschema import ValidationError

from pipeline.validator import inject_ingest_ts, validate_stored, validate_wire
from pipeline.prediction_schema import now_utc_iso  # unica implementazione (FINDING M9-7)

try:  # paho-mqtt (pinnato M8) — presente in runtime e nei test unitari
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover — errore di ambiente, non di runtime
    raise ImportError(
        "paho-mqtt non installato: `pip install -r requirements.txt` "
        "(paho-mqtt==2.1.0, pin M8 ADR-0019)") from exc

# ---------------------------------------------------------------------------
# Costanti (spec §6.2, §6.4)
# ---------------------------------------------------------------------------
DEFAULT_BROKER = "mosquitto"
DEFAULT_PORT = 1883
DEFAULT_TOPIC = "plant/filler01/telemetry/valve"
# Topic stato macchina (spec oee-backend §C2 / M8 §4): il PLC reale (o il
# bridge Node-RED) pubblica qui le transizioni OMAC — il consumer le
# persiste su machine_state_history per l'OEE Home L0.
DEFAULT_TOPIC_STATE = "plant/filler01/state"
DEFAULT_OUT = "data/raw"
DEFAULT_FLUSH_RECORDS = 100
DEFAULT_FLUSH_SECONDS = 5.0
DEFAULT_CLIENT_ID = "plcsim-ingest-v1"
DEFAULT_QOS = 1
STATS_LOG_SECONDS = 60.0
FILE_NAME = "valve_cycles.parquet"

# Colonne appiattite (spec §6.2) — ordine stabile e unico per ogni file.
_DATA_INT_FIELDS = (
    "filling_time_ms", "tail_time_ms", "tail_pulse", "pulse_count", "target",
    "delta_pulse", "filling_step_out",
)
_DATA_BOOL_FIELDS = ("filling_ok", "fill_quality_ok", "sequence_ok", "sample_valid",
                     "position_limit", "filling_overtime")
FLATTENED_COLUMNS: tuple[str, ...] = (
    "schema_version", "event_id", "event_ts", "source_ts", "ingest_ts",
    "machine_id", "cycle_id", "valve_id",
    *(f"data.{k}" for k in _DATA_INT_FIELDS),
    *(f"data.{k}" for k in _DATA_BOOL_FIELDS),
    "data.diagnostic_status",
    "data.close_reason",
    "quality.valid", "quality.completeness",
)

# Tipi polars coerenti con lo schema JSON (spec §6.2: Int64 per cycle_id,
# bool per i flag, null ammessi per la policy partial T6).
COLUMN_TYPES: dict[str, pl.DataType] = {
    "schema_version": pl.String,
    "event_id": pl.String,
    "event_ts": pl.String,
    "source_ts": pl.String,
    "ingest_ts": pl.String,
    "machine_id": pl.String,
    "cycle_id": pl.Int64,
    "valve_id": pl.Int64,
    **{f"data.{k}": pl.Int64 for k in _DATA_INT_FIELDS},
    **{f"data.{k}": pl.Boolean for k in _DATA_BOOL_FIELDS},
    "data.diagnostic_status": pl.String,
    "data.close_reason": pl.String,
    "quality.valid": pl.Boolean,
    "quality.completeness": pl.String,
}

STATS_KEYS = (
    "received", "written", "duplicates", "json_invalid",
    "schema_invalid", "reconnect_count", "parquet_written",
    "state_received", "state_invalid", "state_written",
)


# ---------------------------------------------------------------------------
# Helper puri (testabili senza broker)
# ---------------------------------------------------------------------------
def partition_of(record: dict[str, Any]) -> tuple[str, str]:
    """Partizione hive ``(machine_id, date)``.

    La data UTC deriva da ``event_ts`` (orologio edge), NON da ``ingest_ts``:
    la partizione è deterministica e indipendente dalla latenza di ingestione
    (spec §6.2). ``event_ts`` è già validato (format date-time, wire v1.0/v1.2);
    un valore non parsabile alza ``ValueError`` (guardia del chiamante).
    """
    ts = record["event_ts"]
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00").replace("z", "+00:00"))
    return record["machine_id"], dt.date().isoformat()


def flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    """Appiattisce un record stored v1.1/v1.3 nelle colonne della spec §6.2."""
    flat: dict[str, Any] = {}
    for key in FLATTENED_COLUMNS:
        if key.startswith("data."):
            flat[key] = record["data"].get(key[len("data."):])
        elif key.startswith("quality."):
            flat[key] = record["quality"].get(key[len("quality."):])
        else:
            flat[key] = record[key]
    return flat


def records_to_df(records: list[dict[str, Any]]) -> pl.DataFrame:
    """DataFrame polars dalle colonne appiattite (ordine/schema fissi)."""
    flattened = [flatten_record(r) for r in records]
    columns = {name: [row[name] for row in flattened] for name in FLATTENED_COLUMNS}
    return pl.DataFrame(columns, schema=COLUMN_TYPES)


def build_arg_parser() -> argparse.ArgumentParser:
    """CLI del consumer (spec §6.4)."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.ingest",
        description="Consumer MQTT M8: validazione envelope wire v1.0/v1.2 -> "
                    "stored v1.1/v1.3, dedup su event_id, raw Parquet "
                    "partizionato (spec §6).",
    )
    parser.add_argument("--broker", default=DEFAULT_BROKER,
                        help=f"host del broker MQTT (default: {DEFAULT_BROKER})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"porta del broker (default: {DEFAULT_PORT})")
    parser.add_argument("--topic", default=DEFAULT_TOPIC,
                        help=f"topic v1 (default: {DEFAULT_TOPIC})")
    parser.add_argument("--topic-state", default=DEFAULT_TOPIC_STATE,
                        help=f"topic stato macchina OMAC (default: {DEFAULT_TOPIC_STATE})")
    parser.add_argument("--qos", type=int, default=DEFAULT_QOS, choices=(0, 1, 2),
                        help=f"QoS di subscribe (default: {DEFAULT_QOS})")
    parser.add_argument("--out", type=Path, default=Path(DEFAULT_OUT),
                        help=f"root del raw partizionato (default: {DEFAULT_OUT})")
    parser.add_argument("--flush-records", type=int, default=DEFAULT_FLUSH_RECORDS,
                        help=f"record in memoria prima del flush (default: {DEFAULT_FLUSH_RECORDS})")
    parser.add_argument("--flush-seconds", type=float, default=DEFAULT_FLUSH_SECONDS,
                        help=f"secondi massimi prima del flush (default: {DEFAULT_FLUSH_SECONDS})")
    parser.add_argument("--dedup-store", type=Path, default=None,
                        help="JSONL append-only degli event_id flushati (OFF di default)")
    parser.add_argument("--stats-json", type=Path, default=None,
                        help="path del JSON atomico delle stats (scritto a chiusura e periodicamente)")
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID,
                        help=f"client id MQTT fisso, sessione persistente (default: {DEFAULT_CLIENT_ID})")
    return parser


# ---------------------------------------------------------------------------
# Storage lazy (stato macchina → machine_state_history, OEE L0)
# ---------------------------------------------------------------------------
# Storage condiviso del processo: costruito al primo messaggio di stato.
# False = tentativo fallito (non ritentare: il consumer non deve MAI
# bloccarsi sul DB).
_INGEST_STORAGE: Any = None


def _ingest_storage() -> Any:
    """Storage operazionale lazy per lo stato macchina (best-effort).

    Prima chiamata: Storage + init() (create_all checkfirst — crea
    machine_state_history se manca, non tocca tabelle esistenti). Se il
    modulo/DB non è disponibile: False (mai più ritentato) e None al
    chiamante — il consumer continua a vivere (lo stato non persistito
    è perso per il DB, loggato).
    """
    global _INGEST_STORAGE
    if _INGEST_STORAGE is not None:
        return _INGEST_STORAGE if _INGEST_STORAGE is not False else None
    try:
        from pipeline.storage import Storage, make_engine
        st = Storage(make_engine())
        st.init()
        _INGEST_STORAGE = st
        return st
    except Exception:  # noqa: BLE001 — best-effort
        _INGEST_STORAGE = False
        return None


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------
class IngestConsumer:
    """Consumer MQTT: validazione + dedup + Parquet partizionato (spec §6).

    Threading: il loop di rete paho gira in un thread daemon
    (``loop_start``); ``on_message``/``on_disconnect``/``on_connect`` sono
    eseguiti lì. Un lock protegge buffer + dedup set (il flush può avvenire
    dal thread di rete a soglia record e dal thread principale a soglia
    tempo/chiusura). La riconnessione è gestita dalla libreria
    (``reconnect_on_failure`` di paho, backoff esponenziale 1s->60s configurato
    con ``reconnect_delay_set``); ``on_disconnect`` conta l'evento e
    reinsemina il backoff con jitter.
    """

    def __init__(
        self,
        broker: str = DEFAULT_BROKER,
        port: int = DEFAULT_PORT,
        topic: str = DEFAULT_TOPIC,
        topic_state: str = DEFAULT_TOPIC_STATE,
        out: str | Path = DEFAULT_OUT,
        flush_records: int = DEFAULT_FLUSH_RECORDS,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
        dedup_store: str | Path | None = None,
        stats_json: str | Path | None = None,
        client_id: str = DEFAULT_CLIENT_ID,
        qos: int = DEFAULT_QOS,
    ) -> None:
        if flush_records < 1:
            raise ValueError("flush_records deve essere >= 1")
        if flush_seconds <= 0:
            raise ValueError("flush_seconds deve essere > 0")
        if qos not in (0, 1, 2):
            raise ValueError("qos deve essere 0, 1 o 2")
        self.broker = broker
        self.port = port
        self.topic = topic
        self.topic_state = topic_state
        self.qos = qos
        self.out = Path(out)
        self.flush_records = flush_records
        self.flush_seconds = float(flush_seconds)
        self.dedup_store = Path(dedup_store) if dedup_store else None
        self.stats_json = Path(stats_json) if stats_json else None
        self.client_id = client_id

        self.logger = logging.getLogger("pipeline.ingest")
        self.stats: dict[str, int] = {key: 0 for key in STATS_KEYS}
        self.start_ts = now_utc_iso()

        # Dedup (spec §6.3): set in memoria + rebuild all'avvio.
        self.seen: set[str] = set()
        # Buffer per partizione (machine, date) -> record stored validi.
        self._buffer: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._shutting_down = False
        self._backoff_attempts = 0
        self._write_failures: dict[tuple[str, str], int] = {}
        self._last_flush_at = time.monotonic()
        self._last_stats_at = time.monotonic()
        self._api_v2 = hasattr(mqtt, "CallbackAPIVersion")

    # -- signali -----------------------------------------------------------
    def request_stop(self, signum: int | None = None, frame: Any = None) -> None:
        """Handler SIGINT/SIGTERM: chiusura ordinata (flush + stats)."""
        self.logger.info("segnale %s ricevuto — chiusura ordinata", signum)
        self._stop.set()

    # -- stats --------------------------------------------------------------
    def stats_snapshot(self) -> dict[str, Any]:
        return {key: self.stats[key] for key in STATS_KEYS}

    def write_stats_json(self, path: Path) -> None:
        """Stats JSON atomico (temp + os.replace) — numeri da script (protocollo §5)."""
        payload: dict[str, Any] = {**self.stats_snapshot(), "start_ts": self.start_ts}
        payload["end_ts"] = now_utc_iso()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _log_stats(self) -> None:
        self.logger.info("stats %s", json.dumps(self.stats_snapshot()))

    # -- dedup (spec §6.3) ---------------------------------------------------
    def rebuild_dedup(self) -> int:
        """Rebuild del dedup set all'avvio: dedup-store poi Parquet (unione).

        Se la rebuild da Parquet fallisce: WARNING + set solo da dedup-store
        (documentato in spec §6.3). Ritorna il numero di event_id nel set.
        """
        self.seen = set()
        n_store = 0
        if self.dedup_store is not None:
            n_store = self._load_dedup_store_file(self.dedup_store)
        n_parquet = 0
        if self.out.exists():
            files = sorted(self.out.glob(f"**/{FILE_NAME}"))
            if files:
                try:
                    parquet_ids = set(
                        pl.scan_parquet(files).select("event_id").collect()["event_id"].to_list())
                    self.seen.update(parquet_ids)
                    n_parquet = len(parquet_ids)
                except Exception as exc:  # noqa: BLE001 — WARNING + set vuoto (spec §6.3)
                    self.logger.warning(
                        "rebuild dedup da Parquet FALLITA (%s) — dedup set: "
                        "solo dedup-store; rischio duplicati su restart", exc)
        self.logger.info(
            "dedup set: %d event_id (store=%d, parquet=%d)",
            len(self.seen), n_store, n_parquet)
        return len(self.seen)

    def _load_dedup_store_file(self, path: Path) -> int:
        ids: set[str] = set()
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(json.loads(line)["event_id"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
        except OSError as exc:
            self.logger.warning("dedup-store non leggibile (%s) — proseguo senza", exc)
            return 0
        self.seen.update(ids)
        return len(ids)

    def _append_dedup_store(self, event_ids: list[str]) -> None:
        """Append JSONL del dedup-store (solo record scritti su Parquet).

        Chiamato DOPO la scrittura atomica del Parquet: se il processo muore
        tra le due operazioni, la rebuild da Parquet copre gli id mancanti.
        """
        if self.dedup_store is None:
            return
        self.dedup_store.parent.mkdir(parents=True, exist_ok=True)
        with open(self.dedup_store, "a", encoding="utf-8") as fh:
            for event_id in event_ids:
                fh.write(json.dumps({"event_id": event_id}, separators=(",", ":")) + "\n")

    # -- elaborazione messaggi ------------------------------------------------
    def handle_state_payload(self, payload: bytes | str) -> str:
        """Processa un messaggio di STATO macchina (topic plant/filler01/state).

        Payload (spec oee-backend §C2): {"state_code": int,
        "state_label" o "state": str, "ts"?: ISO8601, ...} — il topic è
        il contratto machine-agnostic (un PLC reale pubblica lo stesso
        topic). Su messaggio valido → `log_machine_state_history`
        (append-only). Esito: "state_written" | "state_invalid" |
        "state_storage_unavailable". Il fallimento di storage NON ferma
        il consumer (log warning); nessun ack differito: il messaggio è
        consumato comunque.
        """
        self.stats["state_received"] += 1
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            data = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.stats["state_invalid"] += 1
            self.logger.warning(
                "state_invalid: payload non JSON (%s) — sample=%r",
                exc, _truncate(payload if isinstance(payload, str)
                               else repr(payload)))
            return "state_invalid"
        if not isinstance(data, dict):
            self.stats["state_invalid"] += 1
            self.logger.warning("state_invalid: payload non-oggetto")
            return "state_invalid"
        state_code = data.get("state_code")
        state_label = data.get("state_label") or data.get("state")
        try:
            state_code = int(state_code)
        except (TypeError, ValueError):
            self.stats["state_invalid"] += 1
            self.logger.warning(
                "state_invalid: state_code assente/non numerico — sample=%r",
                _truncate(json.dumps(data, separators=(",", ":"))))
            return "state_invalid"
        entered_ts = None
        ts = data.get("ts")
        if ts is not None:
            try:
                entered_ts = datetime.fromisoformat(
                    str(ts).replace("Z", "+00:00").replace("z", "+00:00"))
            except ValueError:
                entered_ts = None  # ts non parsabile: now UTC in storage
        st = _ingest_storage()
        if st is None:
            self.logger.warning(
                "stato macchina %d non persistito: storage operazionale "
                "non disponibile (PLCSIM_DATABASE_URL) — messaggio perso "
                "per il DB (il consumer continua)", state_code)
            return "state_storage_unavailable"
        try:
            st.log_machine_state_history(
                state_code, state_label or str(state_code),
                entered_ts=entered_ts, source=f"mqtt:{self.topic_state}")
        except Exception as exc:  # noqa: BLE001 — mai fermare il consumer
            self.logger.warning(
                "stato %d non persistito (storage error): %s",
                state_code, exc)
            return "state_storage_unavailable"
        self.stats["state_written"] += 1
        return "state_written"

    def handle_payload(self, payload: bytes | str) -> str:
        """Processa un payload MQTT (usato da ``on_message`` e dai test).

        Esito: ``"written"`` (record scritto su Parquet in questo call),
        ``"buffered"``, ``"duplicate"``, ``"json_invalid"``,
        ``"schema_invalid"``. Il payload non valido viene ACKato comunque
        (nessun flush) — spec §6.3.
        """
        self.stats["received"] += 1
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            data = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.stats["json_invalid"] += 1
            sample = payload if isinstance(payload, str) else repr(payload)
            self.logger.warning(
                "json_invalid: payload non JSON (%s) — sample=%r",
                exc, _truncate(sample))
            return "json_invalid"

        try:
            validate_wire(data)  # version-aware: rifiuta ingest_ts presente e
            #                      versioni ∉ {1.0, 1.2}
        except ValidationError as exc:
            self.stats["schema_invalid"] += 1
            sv = data.get("schema_version") if isinstance(data, dict) else None
            self.logger.warning(
                "schema_invalid (wire %s): %s — sample=%r",
                sv, exc.message, _truncate(json.dumps(data, separators=(",", ":"))))
            return "schema_invalid"

        record = inject_ingest_ts(data, datetime.now(timezone.utc))
        try:
            validate_stored(record)  # guardia stored (spec §5.3, T6)
        except ValidationError as exc:
            self.stats["schema_invalid"] += 1
            self.logger.error(
                "schema_invalid (stored %s — GUARDIA): %s — record NON scritto",
                record.get("schema_version"), exc.message)
            return "schema_invalid"

        event_id = record["event_id"]
        with self._lock:
            if event_id in self.seen:
                self.stats["duplicates"] += 1
                self.logger.warning(
                    "duplicate event_id=%s (cycle_id=%s)", event_id, record.get("cycle_id"))
                return "duplicate"
            try:
                machine, date = partition_of(record)
            except (ValueError, TypeError, KeyError) as exc:
                self.stats["schema_invalid"] += 1
                self.logger.error(
                    "event_ts non parsabile per la partizione (guardia): %s — record scartato",
                    exc)
                return "schema_invalid"
            self.seen.add(event_id)
            self._buffer.setdefault((machine, date), []).append(record)
            if self._buffer_size_locked() >= self.flush_records:
                self._flush_locked()  # ack del messaggio segue (paho: dopo il callback)
                return "written"
        return "buffered"

    def _buffer_size_locked(self) -> int:
        return sum(len(records) for records in self._buffer.values())

    # -- flush atomico (spec §6.2) ---------------------------------------------
    def flush(self) -> dict[str, int] | None:
        """Flush sincrono del buffer (temp + os.replace). None se vuoto."""
        with self._lock:
            return self._flush_locked()

    def _flush_locked(self) -> dict[str, int] | None:
        if not self._buffer:
            return None
        buffer, self._buffer = self._buffer, {}
        written_total = 0
        parquet_written = 0
        for (machine, date), records in buffer.items():
            try:
                self._write_partition(machine, date, records)
                written_total += len(records)
                parquet_written += 1
                self._write_failures[(machine, date)] = 0
            except Exception as exc:  # noqa: BLE001 — il consumer non si ferma mai
                attempts = self._write_failures.get((machine, date), 0) + 1
                self._write_failures[(machine, date)] = attempts
                self.logger.error(
                    "scrittura parquet fallita (tentativo %d) per machine=%s date=%s — "
                    "%d record tenuti in memoria, retry al prossimo flush "
                    "(DLQ fuori scope M8): %s",
                    attempts, machine, date, len(records), exc)
                self._buffer.setdefault((machine, date), []).extend(records)
                continue
            try:
                self._append_dedup_store([r["event_id"] for r in records])
            except Exception as exc:  # noqa: BLE001 — non bloccante
                self.logger.warning(
                    "dedup-store append fallito (non bloccante; rebuild da Parquet copre): %s",
                    exc)
        self.stats["written"] += written_total
        self.stats["parquet_written"] += parquet_written
        if written_total:
            self.logger.info(
                "flush: %d record scritti in %d file parquet (totale scritti=%d)",
                written_total, parquet_written, self.stats["written"])
        return {"written": written_total, "parquet_written": parquet_written}

    def _write_partition(self, machine: str, date: str, records: list[dict[str, Any]]) -> None:
        """Riscrittura atomica del file giornaliero (temp + os.replace)."""
        part_dir = self.out / f"machine={machine}" / f"date={date}"
        part_dir.mkdir(parents=True, exist_ok=True)
        target = part_dir / FILE_NAME
        tmp = part_dir / f"{FILE_NAME}.tmp.{os.getpid()}"
        df_new = records_to_df(records)
        if target.exists():
            df_old = pl.read_parquet(target)
            df = pl.concat([df_old, df_new], how="vertical")
        else:
            df = df_new
        df.write_parquet(tmp, compression="snappy")
        os.replace(tmp, target)

    # -- MQTT (paho 2.x, callback api v2 con fallback v1) ------------------------
    def _create_client(self) -> mqtt.Client:
        """Client paho con client id fisso e sessione persistente.

        paho-mqtt 2.x richiede ``CallbackAPIVersion`` (VERSION2); con paho
        1.x (non pinnato) si ripiega sulla firma classica. ``clean_session=
        False`` richiede client id non vuoto (spec §6.1).
        """
        if self._api_v2:
            return mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=self.client_id, clean_session=False)
        return mqtt.Client(client_id=self.client_id, clean_session=False)

    def _connect_with_retry(self, client: mqtt.Client) -> None:
        """Connessione iniziale con retry (sopravvive a broker giù all'avvio).

        Backoff esponenziale 1s->60s + jitter (spec §6.1). Una volta
        connesso, il rientro di un eventuale drop è gestito dalla libreria
        (``reconnect_on_failure`` di paho, backoff 1s->60s configurato in
        ``run``).
        """
        while not self._stop.is_set():
            try:
                client.connect(self.broker, self.port, keepalive=60)
                self.logger.info("connesso a %s:%s (client_id=%s)", self.broker, self.port, self.client_id)
                return
            except OSError as exc:
                delay = self._backoff_delay()
                self.logger.warning(
                    "connect a %s:%s fallito (%s) — retry tra %.1fs", self.broker, self.port, exc, delay)
                self._stop.wait(delay)
        raise RuntimeError("consumer interrotto prima della connessione")

    def _backoff_delay(self) -> float:
        """Backoff esponenziale 1s->60s + jitter (spec §6.1)."""
        base = min(60.0, 1.0 * (2 ** self._backoff_attempts))
        self._backoff_attempts += 1
        return base + random.uniform(0.0, 1.0)

    # callback api v2: (client, userdata, connect_flags, reason_code, properties)
    # callback api v1: (client, userdata, flags, rc)
    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any,
                    reason_code: Any, properties: Any = None) -> None:
        if hasattr(reason_code, "is_failure"):  # v2 (ReasonCode)
            ok = not reason_code.is_failure
            rc_desc = str(reason_code)
        else:  # v1 (int)
            ok = reason_code == 0
            rc_desc = str(reason_code)
        if not ok:
            self.logger.warning("CONNACK rifiutato (rc=%s) — nuovo tentativo", rc_desc)
            return
        self._backoff_attempts = 0
        self.logger.info("CONNACK ok — subscribe %s e %s qos=%d",
                         self.topic, self.topic_state, self.qos)
        result, mid = client.subscribe(
            [(self.topic, self.qos), (self.topic_state, self.qos)])
        if result != mqtt.MQTT_ERR_SUCCESS:
            self.logger.warning("subscribe fallito (rc=%s) — il broker ripristina "
                                "la sessione persistente alla riconnessione", result)

    # v2: (client, userdata, disconnect_flags, reason_code, properties)
    # v1: (client, userdata, rc)
    def _on_disconnect(self, client: mqtt.Client, userdata: Any, *args: Any) -> None:
        self.stats["reconnect_count"] += 1
        reason = args[1] if (self._api_v2 and len(args) >= 3) else (args[0] if args else None)
        self.logger.warning(
            "disconnesso (reason=%s) — reconnect_count=%d", reason, self.stats["reconnect_count"])
        if self._shutting_down or self._stop.is_set():
            return
        # Backoff della libreria: reinsemino la base con jitter (1s->60s+jitter).
        try:
            client.reconnect_delay_set(1 + random.uniform(0.0, 1.0), 60)
        except Exception:  # noqa: BLE001 — mai far fallire il callback
            pass

    def _on_message(self, client: mqtt.Client, userdata: Any, message: Any) -> None:
        try:
            if message.topic == self.topic_state:
                self.handle_state_payload(message.payload)
            else:
                self.handle_payload(message.payload)
        except Exception as exc:  # noqa: BLE001 — il consumer non si ferma mai per un record
            self.logger.error("errore inatteso processando il messaggio (ignorato, ack comunque): %s", exc)

    # -- ciclo di vita ----------------------------------------------------------
    def run(self) -> int:
        """Lifecycle completo: rebuild dedup -> connect -> loop -> segnali -> flush finale."""
        self.rebuild_dedup()
        client = self._create_client()
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(1, 60)  # backoff libreria: 1s -> 60s (esponenziale)
        try:
            self._connect_with_retry(client)
            client.loop_start()
        except RuntimeError:
            self.logger.warning("consumer interrotto prima della connessione")
        try:
            while not self._stop.wait(1.0):
                self._tick()
        finally:
            self._shutdown(client)
        return 0

    def _tick(self) -> None:
        now = time.monotonic()
        if self._buffer_size_locked() and now - self._last_flush_at >= self.flush_seconds:
            self.flush()
            self._last_flush_at = now
        if now - self._last_stats_at >= STATS_LOG_SECONDS:
            self._last_stats_at = now
            self._log_stats()
            if self.stats_json is not None:
                try:
                    self.write_stats_json(self.stats_json)
                except Exception as exc:  # noqa: BLE001
                    self.logger.error("stats-json periodico fallito: %s", exc)

    def _shutdown(self, client: mqtt.Client) -> None:
        self._shutting_down = True
        try:
            client.loop_stop()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("loop_stop: %s", exc)
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.logger.info("chiusura ordinata: flush finale")
        try:
            self.flush()
        except Exception as exc:  # noqa: BLE001
            self.logger.error("flush finale fallito: %s", exc)
        self._log_stats()
        if self.stats_json is not None:
            try:
                self.write_stats_json(self.stats_json)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("stats-json finale fallito: %s", exc)
        self.logger.info("consumer terminato: %s", json.dumps(self.stats_snapshot()))


def _truncate(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    consumer = IngestConsumer(
        broker=args.broker, port=args.port, topic=args.topic, qos=args.qos,
        topic_state=args.topic_state,
        out=args.out, flush_records=args.flush_records,
        flush_seconds=args.flush_seconds, dedup_store=args.dedup_store,
        stats_json=args.stats_json, client_id=args.client_id)
    signal.signal(signal.SIGINT, consumer.request_stop)
    signal.signal(signal.SIGTERM, consumer.request_stop)
    return consumer.run()


if __name__ == "__main__":
    raise SystemExit(main())
