"""M8 MQTT parity check — collaudo E2E: raw via MQTT vs telemetria diretta.

Riferimenti: spec M8 §8 (T0/T1/T5) e §10 (AC-M8-1); issue M8-05.
Spec e issue sono documenti di lavoro locali, fuori dal repository.

La pipeline M8 pubblica l'envelope v1.0 su MQTT (edge Node-RED, M7 —
topic v1, QoS 1) e il consumer ``pipeline/ingest.py`` (issue M8-04) lo
valida (wire v1.0 -> iniezione ``ingest_ts`` -> stored v1.1, spec §5.3),
deduplica su ``event_id`` (spec §6.3) e scrive il raw Parquet partizionato
(spec §6.2). Questo script collauda la parità record-per-record tra il
percorso MQTT (consumer -> Parquet) e la telemetria diretta di riferimento
(oracle):

  - **T0** (spec §8): determinismo di ``build_tag_mapping`` (2 run reali,
    sha256 identico — edge/tag-mapping.yaml è l'UNICA fonte, spec §2.6);
    ``docker compose config`` valida (SKIP senza CLI docker);
    grep ``ns=2;s=`` in ``edge/flows/`` = 0 occorrenze (AC-M7-4 resta
    valido — nessun NodeId hard-coded nei flow, spec §7.1); schema
    ``envelope-v1.1.json`` caricabile e valido (draft 2020-12) con
    ``envelope-v1.json`` (v1.0) immutata (contratto: ``ingest_ts``
    ASSENTE in v1.0 / REQUIRED in v1.1, spec §2.8/§5).
  - **T1** (spec §8 T1; AC-M8-1 — parità core): N record wire v1.0
    deterministici (seed 42 — ADR-0016; factory: ``cycle_id`` monotono
    1..N, ``data`` identici). Per ogni record il percorso MQTT è simulato
    SENZA broker (``IngestConsumer.handle_payload`` diretto: la logica del
    consumer è isolata dall'infrastruttura Docker) e l'oracolo diretto è
    la lista wire; flush su ``tmp_path``; confronto: ``cycle_id`` identici
    in ordine, 0 duplicati, 0 perdite, ``data`` identici record-per-record
    (AC-M8-1). Se mosquitto è raggiungibile (socket connect
    ``localhost:1883``), un E2E reale OPZIONALE (paho-mqtt, publish QoS 1
    sul topic v1) verifica lo stesso percorso attraverso il broker —
    altrimenti SKIP (prerequisito d'ambiente: ``docker compose up -d`` da
    ``edge/``).
  - **T3** (spec §8 T3; AC-M8-3 — dedup): redelivery forzata — i primi
    ``N_DEDUP_DUP`` payload sono RIPUBBLICATI byte-identici (stesso
    ``event_id``, at-least-once QoS 1, spec §4.2): il consumer conta il
    duplicato (``duplicates``, spec §6.3) e il Parquet contiene **1 solo
    record per event_id** con la sequenza ``cycle_id`` totale INVARIATA
    (0 duplicati aggiuntivi). Unit complementari (dedup store, rebuild,
    stats) in ``pipeline/tests/test_ingest_unit.py`` — qui solo l'E2E
    (percorso simulato, stessa code path di ``on_message``).
  - **T5** (spec §8 T5): burst — advance multiplo simulato: sequenza di
    record con gap deterministici (blocchi intermedi non leggibili,
    limite documentato spec §4.3); il consumer NON duplica (dedup su
    ``event_id``, anche su redelivery QoS 1), NON inventa record nei gap:
    il gap-set osservato coincide col riferimento, il watermark (M8:
    ultimo ``cycle_id`` processato) converge all'ultimo id generato.

Uso:
  python edge/tests/mqtt_parity_check.py              # T0 + T1 + T3 + T5 (T1-broker: SKIP se broker giù)
  python edge/tests/mqtt_parity_check.py --t1         # solo T1 (core simulato)
  python edge/tests/mqtt_parity_check.py --t1-broker  # solo E2E reale (richiede mosquitto su)
  python edge/tests/mqtt_parity_check.py --t3         # solo T3 (dedup redelivery, AC-M8-3)
  python edge/tests/mqtt_parity_check.py --t5 --n 50
  python -m pytest edge/tests/mqtt_parity_check.py -v

Dipendenze: jsonschema (validatore v1.0/v1.1 di pipeline/validator.py),
polars (lettura Parquet del consumer), pytest (esecuzione),
paho-mqtt==2.1.0 (solo T1-broker, opzionale), PyYAML + CLI docker
(solo T0-compose, opzionale). Vedi edge/requirements.txt (pin M8).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
import polars as pl
from jsonschema import Draft202012Validator, ValidationError

# ---------------------------------------------------------------------------
# Percorsi (tutto relativo a questo file: edge/tests/mqtt_parity_check.py)
# ---------------------------------------------------------------------------
_EDGE_DIR = Path(__file__).resolve().parents[1]
SCHEMA_V1_PATH = _EDGE_DIR / "schemas" / "envelope-v1.json"        # v1.0 (M7, immutata)
SCHEMA_V11_PATH = _EDGE_DIR / "schemas" / "envelope-v1.1.json"     # v1.1 (M8)
COMPOSE_PATH = _EDGE_DIR / "docker-compose.yml"
FLOWS_DIR = _EDGE_DIR / "flows"
MAPPING_YAML_PATH = _EDGE_DIR / "tag-mapping.yaml"                 # unica fonte (§2.6)
BUILD_SCRIPT_PATH = _EDGE_DIR / "scripts" / "build_tag_mapping.py"

# Il consumer (pipeline/ingest.py) e il validatore (pipeline/validator.py)
# sono usati come LIBRERIA (nessuna modifica). La root del repo entra in
# sys.path per l'esecuzione standalone e pytest.
_REPO_ROOT = _EDGE_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from pipeline.ingest import IngestConsumer  # noqa: E402
from pipeline.validator import (  # noqa: E402
    inject_ingest_ts, validate_stored, validate_wire,
)

# paho-mqtt (pinnato M8, ADR-0019) — richiesto SOLO dal T1-broker opzionale.
try:  # pragma: no cover
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover
    mqtt = None
    _PAHO_IMPORT_ERROR = exc
else:
    _PAHO_IMPORT_ERROR = None

# ---------------------------------------------------------------------------
# Costanti di collaudo (spec §8: N congelato in calibration, es. 100; seed 42)
# ---------------------------------------------------------------------------
N_CYCLES = 100          # T1 core: record del run di parità (AC-M8-1)
N_BURST = 40            # T5 burst: record emessi (come M7 T5)
N_BROKER = 20           # T1-broker (E2E reale opzionale): campione ridotto
N_DEDUP = 10            # T3 dedup: record univoci pubblicati una volta
N_DEDUP_DUP = 3         # T3 dedup: redelivery forzate (stesso event_id)
SEED = 42               # ADR-0016: determinismo stepped, seed fisso
TOPIC = "plant/filler01/telemetry/valve"   # topic v1 (ADR 0018, spec §4.1)
BROKER_HOST = "localhost"
BROKER_PORT = 1883

# Stato stazionario della valvola: DATA IDENTICI per ogni record (spec §8 T1),
# coerenti con la fixture canonica di M7 (parity_check.py HAPPY_PATH).
DATA_TEMPLATE: dict[str, Any] = {
    "filling_time_ms": 2480,
    "tail_time_ms": 421,
    "tail_pulse": 244,
    "pulse_count": 2256,
    "target": 2500,
    "delta_pulse": 244,
    "filling_step_out": 18,
    "filling_ok": True,
    "fill_quality_ok": True,
    "sequence_ok": True,
    "sample_valid": True,
    "diagnostic_status": "NORMAL",
}
DATA_KEYS: list[str] = list(DATA_TEMPLATE)

# Ancora fissa per timestamps deterministici (stessa partizione data:
# event_ts derivato da cycle_id, spec §6.2 — partizione da event_ts UTC).
_TS_ANCHOR = datetime(2026, 8, 12, 8, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Factory wire v1.0 deterministica (spec §8 T1: seed 42)
# ---------------------------------------------------------------------------
def _iso_utc(ts: datetime) -> str:
    """ISO8601 UTC con suffisso Z (convenzione envelope, ms)."""
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _det_uuid(cycle_id: int, seed: int) -> str:
    """uuid4 deterministico per (seed, cycle_id) — dedup testabile (spec §6.3).

    Seed int esplicito (Python 3.14 non accetta tuple come seed in
    ``random.Random``): combinazione deterministica seed*1_000_003+cycle_id.
    """
    rng = random.Random(seed * 1_000_003 + int(cycle_id))
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def make_wire_record(cycle_id: int, seed: int = SEED) -> dict[str, Any]:
    """Envelope wire v1.0 deterministico (factory spec §8 T1).

    ``cycle_id`` monotono, ``data`` IDENTICI per ogni record (stato
    stazionario), timestamps derivati da ``cycle_id`` (stessa partizione
    data, ordinati per ciclo), ``event_id`` uuid4 deterministico. Il
    record NON contiene ``ingest_ts`` (campo riservato in v1.0, spec §4.3).
    """
    event_ts = _TS_ANCHOR + timedelta(milliseconds=int(cycle_id) * 100)
    source_ts = event_ts - timedelta(milliseconds=20)
    return {
        "schema_version": "1.0",
        "event_id": _det_uuid(cycle_id, seed),
        "event_type": "valve_cycle",
        "event_ts": _iso_utc(event_ts),
        "source_ts": _iso_utc(source_ts),
        "machine_id": "filler01",
        "cycle_id": int(cycle_id),
        "valve_id": 1,
        "data": dict(DATA_TEMPLATE),
        "quality": {"valid": True, "completeness": "complete"},
    }


def generate_wire_stream(n: int, seed: int = SEED) -> list[dict[str, Any]]:
    """N record wire deterministici con ``cycle_id`` 1..N (T1 core)."""
    return [make_wire_record(cid, seed) for cid in range(1, n + 1)]


def burst_cycle_ids(n: int, seed: int = SEED) -> list[int]:
    """Sequenza ``cycle_id`` con gap deterministici (T5: burst simulato).

    Ogni 5 record la sequenza salta 2-4 id: blocchi intermedi non leggibili
    con advance multiplo (finestra di lettura larga, limite spec §4.3).
    """
    rng = random.Random(seed)
    ids: list[int] = []
    nxt = 1
    for i in range(n):
        ids.append(nxt)
        nxt += 1
        if (i + 1) % 5 == 0:
            nxt += rng.randint(2, 4)
    return ids


def generate_burst_stream(n: int, seed: int = SEED) -> tuple[list[dict[str, Any]], list[int]]:
    """Record wire con gap + gap-set di riferimento ``(records, gaps)``."""
    ids = burst_cycle_ids(n, seed)
    wires = [make_wire_record(cid, seed) for cid in ids]
    gaps = sorted(set(range(1, ids[-1] + 1)) - set(ids))
    return wires, gaps


# ---------------------------------------------------------------------------
# Parità (spec §8 T1: confronto su cycle_id e data, come M7 _parity_identity)
# ---------------------------------------------------------------------------
def _wire_parity(wire: dict[str, Any]) -> tuple[Any, ...]:
    """Identità di parità lato wire: (cycle_id, valve_id, data, quality).

    ``event_id``/``event_ts``/``source_ts`` esclusi per costruzione (ogni
    istanza del percorso genera i propri — convenzione M7 §T1): la parità
    richiesta è su ``cycle_id``, ``valve_id``, ``data`` e ``quality``.
    """
    return (
        int(wire["cycle_id"]), int(wire["valve_id"]),
        tuple(sorted((k, wire["data"][k]) for k in DATA_KEYS)),
        bool(wire["quality"]["valid"]), wire["quality"]["completeness"],
    )


def _stored_row_parity(row: dict[str, Any]) -> tuple[Any, ...]:
    """Identità di parità su una riga appiattita del Parquet (spec §6.2)."""
    return (
        int(row["cycle_id"]), int(row["valve_id"]),
        tuple(sorted((k, row[f"data.{k}"]) for k in DATA_KEYS)),
        bool(row["quality.valid"]), row["quality.completeness"],
    )


def read_stored_records(out_dir: str | Path) -> list[dict[str, Any]]:
    """Legge le righe stored (v1.1) dai Parquet partizionati, in ordine di
    append (ordine di file preservato da polars; una partizione per run)."""
    files = sorted(Path(out_dir).glob("**/valve_cycles.parquet"))
    if not files:
        return []
    return pl.read_parquet(files).to_dicts()


def _parity_problems(wires: list[dict[str, Any]], stored: list[dict[str, Any]],
                     stats: dict[str, Any], label: str) -> list[str]:
    """Check di parità MQTT-vs-oracle: numeri, ordine, duplicati, perdite,
    data, event_id, bump v1.1. Ritorna la lista dei problemi (vuota = ok)."""
    problems: list[str] = []
    expected = len(wires)
    for key in ("received", "written", "duplicates", "json_invalid", "schema_invalid"):
        if stats.get(key, 0) != (0 if key != "received" and key != "written" else expected):
            problems.append(f"{label}: stats[{key}]={stats.get(key)} atteso "
                            f"{expected if key in ('received', 'written') else 0}")
    if len(stored) != expected:
        problems.append(f"{label}: {len(stored)} record su Parquet, attesi "
                        f"{expected} (perdite o duplicati)")
        return problems
    stored_ids = [int(r["cycle_id"]) for r in stored]
    wire_ids = [int(w["cycle_id"]) for w in wires]
    if stored_ids != wire_ids:
        for i, (a, b) in enumerate(zip(stored_ids, wire_ids)):
            if a != b:
                problems.append(f"{label}: cycle_id divergente al record {i} "
                                f"(stored {a} vs wire {b})")
                break
        else:
            problems.append(f"{label}: lunghezze uguali ma cycle_id divergenti")
    if len(set(stored_ids)) != len(stored_ids):
        problems.append(f"{label}: {len(stored_ids) - len(set(stored_ids))} "
                        f"duplicati di cycle_id su Parquet")
    for i, (row, wire) in enumerate(zip(stored, wires)):
        if _stored_row_parity(row) != _wire_parity(wire):
            problems.append(f"{label}: data/quality divergenti al record {i} "
                            f"(cycle_id {row['cycle_id']})")
            break
        if row["event_id"] != wire["event_id"]:
            problems.append(f"{label}: event_id alterato al record {i}")
            break
        if row.get("schema_version") != "1.1" or not row.get("ingest_ts"):
            problems.append(f"{label}: record {i} non v1.1 "
                            f"(schema_version={row.get('schema_version')!r}, "
                            f"ingest_ts={row.get('ingest_ts')!r})")
            break
    return problems


# ---------------------------------------------------------------------------
# Harness percorso MQTT simulato (SENZA broker — spec §8 T1)
# ---------------------------------------------------------------------------
def run_consumer_simulated(wires: list[dict[str, Any]], out_dir: str | Path,
                           flush_records: int | None = None,
                           ) -> tuple[IngestConsumer, list[dict[str, Any]],
                                      dict[str, Any], list[str]]:
    """Percorso MQTT simulato: feed diretto a ``IngestConsumer.handle_payload``.

    Ogni record wire è consegnato come payload JSON (``json.dumps``) —
    identico a ciò che il consumer riceverebbe dal broker — e l'oracolo
    diretto (lista wire) resta il riferimento. Flush esplicito; ritorna
    (consumer, record stored dal Parquet, stats, esiti handle_payload).
    """
    if flush_records is None:
        flush_records = len(wires)
    consumer = IngestConsumer(
        broker="<simulato>", topic=TOPIC, out=str(out_dir),
        flush_records=flush_records, flush_seconds=60.0,
        client_id="plcsim-parity-sim")
    outcomes: list[str] = []
    for wire in wires:
        outcomes.append(consumer.handle_payload(
            json.dumps(wire, separators=(",", ":"))))
    consumer.flush()
    return consumer, read_stored_records(out_dir), consumer.stats_snapshot(), outcomes


@contextlib.contextmanager
def _tmp_out(out_dir: str | Path | None) -> Iterator[Path]:
    """Dir raw di lavoro: ``out_dir`` se dato (pytest), altrimenti temp."""
    if out_dir is not None:
        yield Path(out_dir)
        return
    tmp = tempfile.TemporaryDirectory(prefix="m8-parity-")
    try:
        yield Path(tmp.name)
    finally:
        tmp.cleanup()


# ---------------------------------------------------------------------------
# T0 — determinismo + compose + grep + schema (spec §8)
# ---------------------------------------------------------------------------
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_t0_determinism() -> tuple[str, str]:
    """T0a: build_tag_mapping deterministico (2 run reali, sha256 identico)."""
    if not (MAPPING_YAML_PATH.exists() and BUILD_SCRIPT_PATH.exists()):
        return "FAIL", (f"artefatti mancanti: {MAPPING_YAML_PATH} / "
                        f"{BUILD_SCRIPT_PATH}")

    def _run_once(out_path: Path) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT_PATH), str(MAPPING_YAML_PATH),
             str(out_path)], capture_output=True, text=True, timeout=120)
        digest = _sha256(out_path.read_bytes()) if out_path.exists() else ""
        return proc.returncode, digest

    with tempfile.TemporaryDirectory(prefix="m8-t0map-") as tmp:
        rc1, h1 = _run_once(Path(tmp) / "tag-mapping.js")
        rc2, h2 = _run_once(Path(tmp) / "tag-mapping.js")
    if rc1 == 0 and rc2 == 0 and h1 == h2:
        return "PASS", (f"build_tag_mapping.py deterministico (2 run su "
                        f"tag-mapping.yaml, sha256={h1})")
    return "FAIL", f"rc1={rc1} rc2={rc2} sha256_1={h1} sha256_2={h2}"


def _docker_cli() -> str | None:
    """CLI docker: PATH, o percorso noto dell'ambiente (spec §2 pin)."""
    import shutil
    found = shutil.which("docker")
    if found:
        return found
    # Docker Desktop su Windows non sempre mette la sua CLI nel PATH del
    # processo: si guarda anche l'installazione per-utente, ricavata da
    # LOCALAPPDATA invece che da un percorso cablato.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        documented = Path(local) / "Programs/DockerDesktop/resources/bin/docker.exe"
        if documented.exists():
            return str(documented)
    return None


def run_t0_compose() -> tuple[str, str]:
    """T0b: docker compose config valida (SKIP senza CLI docker)."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        return "SKIP", f"PyYAML non installato (T0-compose non eseguito): {exc}"
    try:
        data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return "FAIL", f"docker-compose.yml non parsabile: {exc}"
    services = (data or {}).get("services", {})
    missing = [s for s in ("nodered", "mosquitto") if s not in services]
    if missing:
        return "FAIL", f"servizi mancanti nel compose: {missing}"
    cli = _docker_cli()
    if cli is None:
        return "SKIP", (f"CLI docker non trovato — YAML valido e servizi "
                        f"nodered+mosquitto presenti; `docker compose config` "
                        f"non eseguito (prerequisito d'ambiente)")
    proc = subprocess.run([cli, "compose", "config", "-q"],
                          cwd=str(_EDGE_DIR), capture_output=True, text=True,
                          timeout=120)
    if proc.returncode == 0:
        return "PASS", (f"docker compose config valida "
                        f"({len(services)} servizi: nodered, mosquitto)")
    return "FAIL", (f"docker compose config: rc={proc.returncode} "
                    f"{proc.stderr.strip()[:300]}")


def run_t0_grep_node_ids() -> tuple[str, str]:
    """T0c: grep ``ns=2;s=`` in edge/flows/ = 0 (AC-M7-4 resta valido)."""
    hits: list[tuple[str, int]] = []
    for path in sorted(FLOWS_DIR.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        n = text.count("ns=2;s=")
        if n:
            hits.append((str(path.relative_to(_EDGE_DIR)), n))
    if hits:
        return "FAIL", (f"{sum(n for _, n in hits)} occorrenze ns=2;s= in "
                        f"edge/flows/: {hits}")
    return "PASS", ("0 occorrenze ns=2;s= in edge/flows/ — nessun NodeId "
                    "hard-coded nei flow (AC-M7-4)")


def run_t0_schema() -> tuple[str, str]:
    """T0d: schema v1.1 caricabile e valido; v1.0 immutata (contratto §5)."""
    problems: list[str] = []
    try:
        v1 = json.loads(SCHEMA_V1_PATH.read_text(encoding="utf-8"))
        v11 = json.loads(SCHEMA_V11_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(v1)
        Draft202012Validator.check_schema(v11)
    except Exception as exc:
        return "FAIL", f"schema non caricabile/valido: {exc}"

    # v1.0 immutata (spec §2.8/§5.2): niente ingest_ts (proprietà/required),
    # const "1.0", required invariato (contratto M7).
    v1_required = ["schema_version", "event_id", "event_type", "event_ts",
                   "source_ts", "machine_id", "cycle_id", "valve_id",
                   "data", "quality"]
    if "ingest_ts" in v1.get("properties", {}) or "ingest_ts" in v1.get("required", []):
        problems.append("v1.0: ingest_ts presente tra properties/required "
                        "(campo riservato — v1.0 immutata violata)")
    if v1.get("properties", {}).get("schema_version", {}).get("const") != "1.0":
        problems.append("v1.0: schema_version const atteso '1.0'")
    if v1.get("required") != v1_required:
        problems.append(f"v1.0: required divergente dal contratto M7 "
                        f"({v1.get('required')})")

    # v1.1 (spec §5.1/§5.2): ingest_ts REQUIRED, const "1.1",
    # additionalProperties false, recipe_id ASSENTE (futuro v1.2).
    if "ingest_ts" not in v11.get("required", []):
        problems.append("v1.1: ingest_ts non REQUIRED")
    if v11.get("properties", {}).get("schema_version", {}).get("const") != "1.1":
        problems.append("v1.1: schema_version const atteso '1.1'")
    if v11.get("additionalProperties") is not False:
        problems.append("v1.1: additionalProperties atteso false")
    if "recipe_id" in v11.get("properties", {}):
        problems.append("v1.1: recipe_id presente (atteso assente fino a v1.2)")

    # Comportamento (validatori REALI di pipeline/validator.py — usati dal
    # consumer): wire v1.0 ok; wire con ingest_ts RIFIUTATO; stored v1.1 ok
    # dopo iniezione; recipe_id RIFIUTATO (additionalProperties:false).
    wire = make_wire_record(1, SEED)
    try:
        validate_wire(wire)
    except ValidationError as exc:
        problems.append(f"wire v1.0 rifiutato: {exc.message}")
    wire_bad = {**wire, "ingest_ts": "2026-08-12T08:00:00.000Z"}
    try:
        validate_wire(wire_bad)
        problems.append("wire con ingest_ts ACCETTATO (v1.0 deve rifiutarlo)")
    except ValidationError:
        pass
    stored = inject_ingest_ts(wire, "2026-08-12T08:00:00.500Z")
    try:
        validate_stored(stored)
    except ValidationError as exc:
        problems.append(f"stored v1.1 rifiutato: {exc.message}")
    try:
        validate_stored({**stored, "recipe_id": "maxima"})
        problems.append("recipe_id ACCETTATO in v1.1 (additionalProperties:false)")
    except ValidationError:
        pass

    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", ("envelope-v1.1.json caricabile e valido (draft 2020-12); "
                    "envelope-v1.json immutata (no ingest_ts, const 1.0, "
                    "required invariato); wire v1.0 ok, wire con ingest_ts "
                    "rifiutato, stored v1.1 ok, recipe_id rifiutato")


def run_t0() -> int:
    """T0 completo (spec §8): stampa 4 ESITO; ritorna il numero di FAIL."""
    checks = [
        ("T0-determinismo", run_t0_determinism()),
        ("T0-compose", run_t0_compose()),
        ("T0-grep-ns2s", run_t0_grep_node_ids()),
        ("T0-schema", run_t0_schema()),
    ]
    fails = 0
    for label, (esito, evidenza) in checks:
        print(f"ESITO ({label}): {esito} - {evidenza}")
        fails += 1 if esito == "FAIL" else 0
    return fails


# ---------------------------------------------------------------------------
# T1 — parità core (spec §8 T1; AC-M8-1)
# ---------------------------------------------------------------------------
def run_t1(n: int = N_CYCLES, seed: int = SEED,
           out_dir: str | Path | None = None) -> tuple[str, str]:
    """T1 (core): parità record-per-record MQTT simulato vs oracle diretto.

    N record wire deterministici (seed 42): il percorso MQTT è simulato
    (``IngestConsumer.handle_payload`` SENZA broker, tmp dir) e l'oracolo
    diretto è la lista wire; confronto su ``cycle_id`` (ordine), duplicati,
    perdite e ``data`` record-per-record (AC-M8-1).
    """
    wires = generate_wire_stream(n, seed)
    problems: list[str] = []
    # prerequisito: factory deterministica (stesso seed => stessa sequenza)
    if generate_wire_stream(n, seed) != wires:
        problems.append("factory NON deterministica (due run divergenti)")

    with _tmp_out(out_dir) as raw_dir:
        consumer, stored, stats, outcomes = run_consumer_simulated(wires, raw_dir)
        if any(o != "buffered" for o in outcomes[:-1]) or outcomes[-1] != "written":
            problems.append(f"esiti handle_payload inattesi: attesi "
                            f"{n - 1} 'buffered' + 1 'written', ottenuti {outcomes}")
        problems += _parity_problems(wires, stored, stats,
                                     "MQTT simulato vs oracle diretto")

    if problems:
        print(f"ESITO (AC-M8-1): FAIL - {len(problems)} controlli falliti")
        for p in problems:
            print(f"    - {p}")
        return "FAIL", "; ".join(problems[:4])
    evidenza = (f"{n}/{n} cycle_id identici, 0 duplicati, 0 perdite, data "
                f"identici record-per-record (MQTT simulato -> consumer -> "
                f"Parquet vs oracle diretto)")
    print(f"ESITO (AC-M8-1): PASS - {evidenza}")
    return "PASS", evidenza


def broker_reachable(host: str = BROKER_HOST, port: int = BROKER_PORT,
                     timeout: float = 1.0) -> bool:
    """Probe socket: mosquitto su (localhost:1883)?."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_t1_broker_e2e(n: int = N_BROKER, out_dir: str | Path | None = None,
                      host: str = BROKER_HOST, port: int = BROKER_PORT,
                      timeout_s: float = 60.0) -> tuple[str, str]:
    """T1 E2E reale (OPZIONALE, spec §8): publish paho QoS 1 -> consumer.

    Richiede un broker mosquitto raggiungibile su ``host:port`` (SKIP
    altrimenti — prerequisito d'ambiente: ``docker compose up -d`` da
    ``edge/``). Consumer reale (IngestConsumer + client paho) e publisher
    di test sul topic v1; id client unici per run (sessione pulita).
    """
    if mqtt is None:  # pragma: no cover
        return "SKIP", f"paho-mqtt non installato: {_PAHO_IMPORT_ERROR}"
    if not broker_reachable(host, port):
        return "SKIP", (f"broker non raggiungibile su {host}:{port} — "
                        f"prerequisito d'ambiente (docker compose up -d da "
                        f"edge/); E2E reale non eseguito")
    wires = generate_wire_stream(n, SEED)
    problems: list[str] = []
    with _tmp_out(out_dir) as raw_dir:
        uid = f"{os.getpid()}-{time.time_ns()}"
        consumer = IngestConsumer(
            broker=host, port=port, topic=TOPIC, out=str(raw_dir),
            flush_records=len(wires), flush_seconds=2.0,
            client_id=f"plcsim-parity-e2e-{uid}")
        client = consumer._create_client()
        client.on_connect = consumer._on_connect
        client.on_disconnect = consumer._on_disconnect
        client.on_message = consumer._on_message
        client.connect(host, port, keepalive=60)
        client.loop_start()
        pub = None
        try:
            time.sleep(1.5)  # CONNACK + SUBACK (broker locale)
            pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                              client_id=f"plcsim-parity-pub-{uid}",
                              clean_session=True)
            pub.connect(host, port, keepalive=30)
            pub.loop_start()
            for wire in wires:
                try:
                    info = pub.publish(
                        TOPIC, json.dumps(wire, separators=(",", ":")), qos=1)
                    info.wait_for_publish(timeout=10.0)
                except (ValueError, RuntimeError) as exc:
                    # paho 2.x: wait_for_publish None a successo, alza su errore
                    problems.append(f"broker E2E: publish fallito per "
                                    f"cycle_id={wire['cycle_id']}: {exc}")
            deadline = time.monotonic() + timeout_s
            while (time.monotonic() < deadline
                   and consumer.stats["received"] < len(wires)):
                time.sleep(0.1)
            if consumer.stats["received"] < len(wires):
                problems.append(f"broker E2E: ricevuti "
                                f"{consumer.stats['received']}/{len(wires)} "
                                f"entro {timeout_s:.0f}s")
            consumer.flush()
        finally:
            if pub is not None:
                pub.loop_stop()
                pub.disconnect()
            client.loop_stop()
            client.disconnect()
        stored = read_stored_records(raw_dir)
        problems += _parity_problems(wires, stored, consumer.stats_snapshot(),
                                     "MQTT reale (paho QoS1) vs oracle")

    if problems:
        print(f"ESITO (T1-broker): FAIL - {len(problems)} controlli falliti")
        for p in problems:
            print(f"    - {p}")
        return "FAIL", "; ".join(problems[:4])
    evidenza = (f"broker reale {host}:{port}: {n}/{n} cycle_id identici "
                f"(publish paho QoS1 -> consumer -> Parquet vs oracle "
                f"diretto), 0 duplicati, 0 perdite, data identici "
                f"record-per-record")
    print(f"ESITO (T1-broker): PASS - {evidenza}")
    return "PASS", evidenza


# ---------------------------------------------------------------------------
# T3 — dedup su event_id, redelivery forzata (spec §8 T3; AC-M8-3)
# ---------------------------------------------------------------------------
def run_t3(n: int = N_DEDUP, n_dup: int = N_DEDUP_DUP, seed: int = SEED,
           out_dir: str | Path | None = None) -> tuple[str, str]:
    """T3: publish duplicato con lo stesso ``event_id`` (redelivery forzata).

    ``n`` record wire deterministici (seed 42; ``event_id`` univoco per
    ``cycle_id``, factory ``_det_uuid``) pubblicati una volta; poi i primi
    ``n_dup`` payload sono RIPUBBLICATI byte-identici (stesso ``event_id``
    — la redelivery at-least-once QoS 1 del broker, spec §4.2): il
    consumer li scarta (``duplicates`` CONTATO, spec §6.3 — il messaggio
    è comunque ACKato, nessun flush) e il Parquet deve contenere **1 solo
    record per event_id** con la sequenza ``cycle_id`` totale INVARIATA
    (0 duplicati aggiuntivi nella sequenza finale, AC-M8-3).

    Percorso simulato (``IngestConsumer.handle_payload`` — stessa code
    path del callback paho ``on_message``), coerente con T1/T5 di questo
    file; le unit complementari (dedup store, rebuild, stats) vivono in
    ``pipeline/tests/test_ingest_unit.py``.
    """
    wires = generate_wire_stream(n, seed)
    payloads = [json.dumps(w, separators=(",", ":")) for w in wires]
    problems: list[str] = []
    with _tmp_out(out_dir) as raw_dir:
        consumer, stored, stats, _ = run_consumer_simulated(
            wires, raw_dir, flush_records=5)
        # redelivery forzata: republish byte-identici (stesso event_id)
        outcomes_dup = [consumer.handle_payload(p) for p in payloads[:n_dup]]
        consumer.flush()
        stats_after = consumer.stats_snapshot()

        if any(o != "duplicate" for o in outcomes_dup):
            problems.append(f"redelivery non rilevata come duplicato: "
                            f"esiti {outcomes_dup} (attesi {n_dup} "
                            f"'duplicate')")
        if stats_after["duplicates"] != n_dup:
            problems.append(f"duplicates={stats_after['duplicates']} atteso "
                            f"{n_dup}")
        if stats_after["received"] != n + n_dup:
            problems.append(f"received={stats_after['received']} atteso "
                            f"{n + n_dup}")
        if stats_after["written"] != n:
            problems.append(f"written={stats_after['written']} atteso {n}")
        if len(stored) != n:
            problems.append(f"{len(stored)} record su Parquet attesi {n} "
                            f"(duplicati scritti?)")
        else:
            eids = [r["event_id"] for r in stored]
            if len(set(eids)) != n:
                problems.append(f"{len(eids) - len(set(eids))} event_id "
                                f"duplicati su Parquet (attesi 0)")
            stored_ids = [int(r["cycle_id"]) for r in stored]
            if stored_ids != [int(w["cycle_id"]) for w in wires]:
                problems.append("sequenza cycle_id finale diversa "
                                "dall'originale (0 duplicati aggiuntivi attesi)")
        # Parquet invariato dopo le redelivery (nessuna riscrittura)
        if len(read_stored_records(raw_dir)) != n:
            problems.append("redelivery: il duplicato è stato SCRITTO su "
                            "Parquet")

    if problems:
        print(f"ESITO (AC-M8-3): FAIL - {len(problems)} controlli falliti")
        for p in problems:
            print(f"    - {p}")
        return "FAIL", "; ".join(problems[:4])
    evidenza = (f"{n} record su Parquet (1 per event_id), {n_dup} redelivery "
                f"conteggiate (duplicates={n_dup}), sequenza cycle_id "
                f"invariata ({n}/{n}, 0 duplicati aggiuntivi), stats "
                f"coerenti (received={n + n_dup}, written={n})")
    print(f"ESITO (AC-M8-3): PASS - {evidenza}")
    return "PASS", evidenza


# ---------------------------------------------------------------------------
# T5 — burst via MQTT (spec §8 T5)
# ---------------------------------------------------------------------------
def run_t5(n: int = N_BURST, seed: int = SEED,
           out_dir: str | Path | None = None) -> tuple[str, str]:
    """T5: burst — advance multiplo simulato (gap deterministici).

    Sequenza di record con gap (blocchi intermedi non leggibili, limite
    spec §4.3): il consumer NON duplica (dedup su ``event_id`` — anche su
    redelivery QoS 1), NON inventa record nei gap (gap-set osservato ==
    riferimento) e il watermark (M8: ultimo ``cycle_id`` processato)
    converge all'ultimo id generato.
    """
    wires, expected_gaps = generate_burst_stream(n, seed)
    problems: list[str] = []
    if not expected_gaps:
        problems.append("burst non riprodotto: nessun gap nella sequenza "
                        "generata")
    if generate_burst_stream(n, seed) != (wires, expected_gaps):
        problems.append("factory burst NON deterministica (due run divergenti)")

    with _tmp_out(out_dir) as raw_dir:
        consumer, stored, stats, _ = run_consumer_simulated(wires, raw_dir)
        problems += _parity_problems(wires, stored, stats,
                                     "burst MQTT vs riferimento")
        stored_ids = [int(r["cycle_id"]) for r in stored]
        if stored_ids:
            # gap-set: cycle_id mancanti tra 1 e l'ultimo processato
            observed_gaps = sorted(set(range(1, max(stored_ids) + 1))
                                   - set(stored_ids))
            if observed_gaps != expected_gaps:
                problems.append(f"gap-set divergenti: osservato "
                                f"{observed_gaps} vs riferimento "
                                f"{expected_gaps}")
            # watermark convergente (M8): ultimo cycle_id processato ==
            # ultimo id generato (nessuna perdita in coda al burst)
            if max(stored_ids) != wires[-1]["cycle_id"]:
                problems.append(f"watermark non convergente: ultimo stored "
                                f"{max(stored_ids)} vs ultimo generato "
                                f"{wires[-1]['cycle_id']}")
        # redelivery QoS 1 (stesso event_id ripubblicato): il consumer NON
        # duplica — dedup su event_id (spec §6.3), duplicato CONTATO.
        outcome = consumer.handle_payload(
            json.dumps(wires[0], separators=(",", ":")))
        consumer.flush()
        stats_after = consumer.stats_snapshot()
        if outcome != "duplicate":
            problems.append(f"redelivery non rilevata come duplicato "
                            f"(esito={outcome!r})")
        if stats_after["duplicates"] != 1:
            problems.append(f"duplicates atteso 1 dopo redelivery, ottenuto "
                            f"{stats_after['duplicates']}")
        if len(read_stored_records(raw_dir)) != len(wires):
            problems.append(f"redelivery: il duplicato è stato SCRITTO su "
                            f"Parquet (attesi {len(wires)} record)")

    if problems:
        print(f"ESITO (T5): FAIL - {len(problems)} controlli falliti")
        for p in problems:
            print(f"    - {p}")
        return "FAIL", "; ".join(problems[:4])
    evidenza = (f"burst {n} record con {len(expected_gaps)} gap: gap-set "
                f"identici al riferimento, watermark convergente "
                f"({stored_ids[-1]}), 0 duplicati, redelivery QoS1 "
                f"rilevata e scartata (duplicates=1, Parquet invariato), "
                f"data identici record-per-record")
    print(f"ESITO (T5): PASS - {evidenza}")
    return "PASS", evidenza


# ---------------------------------------------------------------------------
# CLI (standalone)
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python edge/tests/mqtt_parity_check.py",
        description=("M8 mqtt parity check (spec §8) — parità raw via MQTT "
                     "vs telemetria diretta: T0 (determinismo build_tag_mapping, "
                     "compose, grep ns=2;s=, schema v1.1/v1.0), T1 (parità "
                     "core simulata + E2E reale opzionale se broker su), T3 "
                     "(dedup redelivery, AC-M8-3), T5 (burst con gap). Nessuna "
                     "modifica a pipeline/edge."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("esempi:\n"
                "  python edge/tests/mqtt_parity_check.py\n"
                "  python edge/tests/mqtt_parity_check.py --t1\n"
                "  python edge/tests/mqtt_parity_check.py --t1-broker\n"
                "  python edge/tests/mqtt_parity_check.py --t5 --n 50\n"
                "  python -m pytest edge/tests/mqtt_parity_check.py -v\n"),
    )
    ap.add_argument("--t0", action="store_true", help="esegue T0 (determinismo, compose, grep, schema)")
    ap.add_argument("--t1", action="store_true", help="esegue T1 core (parità simulata, AC-M8-1)")
    ap.add_argument("--t1-broker", dest="t1_broker", action="store_true",
                    help="esegue T1 E2E reale (richiede mosquitto su localhost:1883)")
    ap.add_argument("--t3", action="store_true",
                    help="esegue T3 (dedup redelivery, AC-M8-3)")
    ap.add_argument("--t5", action="store_true", help="esegue T5 (burst con gap)")
    ap.add_argument("--all", action="store_true", help="esegue T0+T1+T1-broker+T5 (default)")
    ap.add_argument("--n", type=int, default=N_CYCLES, help=f"record T1 core (default {N_CYCLES})")
    ap.add_argument("--n-burst", dest="n_burst", type=int, default=N_BURST,
                    help=f"record T5 burst (default {N_BURST})")
    ap.add_argument("--n-broker", dest="n_broker", type=int, default=N_BROKER,
                    help=f"record T1-broker (default {N_BROKER})")
    ap.add_argument("--seed", type=int, default=SEED, help=f"seed (default {SEED})")
    ap.add_argument("--broker", default=BROKER_HOST,
                    help=f"host broker T1-broker (default {BROKER_HOST})")
    ap.add_argument("--port", type=int, default=BROKER_PORT,
                    help=f"porta broker T1-broker (default {BROKER_PORT})")
    ap.add_argument("--out", type=Path, default=None,
                    help="dir raw di lavoro (default: dir temporanea; "
                         "sottodir t1/broker/t5 per test distinti)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    default_run = not (args.t0 or args.t1 or args.t1_broker or args.t3
                       or args.t5 or args.all)
    out_root: Path | None = args.out
    rc = 0

    if args.t0 or args.all or default_run:
        rc |= 1 if run_t0() else 0
    if args.t1 or args.all or default_run:
        esito, _ = run_t1(args.n, args.seed,
                          out_dir=(out_root / "t1") if out_root else None)
        rc |= 0 if esito == "PASS" else 1
    if args.t1_broker or args.all or default_run:
        esito, _ = run_t1_broker_e2e(
            args.n_broker, out_dir=(out_root / "broker") if out_root else None,
            host=args.broker, port=args.port)
        rc |= 0 if esito == "PASS" else 1   # SKIP = prerequisito ambiente, ok
    if args.t3 or args.all or default_run:
        esito, _ = run_t3(out_dir=(out_root / "t3") if out_root else None)
        rc |= 0 if esito == "PASS" else 1
    if args.t5 or args.all or default_run:
        esito, _ = run_t5(args.n_burst, args.seed,
                          out_dir=(out_root / "t5") if out_root else None)
        rc |= 0 if esito == "PASS" else 1
    return rc


# ---------------------------------------------------------------------------
# pytest (spec §8) — test_t0* = T0, test_t1* = T1, test_t5* = T5
# ---------------------------------------------------------------------------
def test_t0_build_tag_mapping_determinism() -> None:
    esito, evidenza = run_t0_determinism()
    assert esito == "PASS", evidenza


def test_t0_compose_config_valid() -> None:
    esito, evidenza = run_t0_compose()
    if esito == "SKIP":
        pytest.skip(evidenza)
    assert esito == "PASS", evidenza


def test_t0_no_hardcoded_node_ids_in_flows() -> None:
    esito, evidenza = run_t0_grep_node_ids()
    assert esito == "PASS", evidenza


def test_t0_schema_v11_valid_and_v10_frozen() -> None:
    esito, evidenza = run_t0_schema()
    assert esito == "PASS", evidenza


def test_t1_parity_mqtt_simulated(tmp_path: Path) -> None:
    esito, evidenza = run_t1(N_CYCLES, SEED, out_dir=tmp_path / "raw-t1")
    assert esito == "PASS", evidenza


def test_t1_parity_broker_e2e(tmp_path: Path) -> None:
    if not broker_reachable():
        pytest.skip("mosquitto non raggiungibile su localhost:1883 — "
                    "prerequisito d'ambiente (docker compose up -d da edge/)")
    esito, evidenza = run_t1_broker_e2e(N_BROKER, out_dir=tmp_path / "raw-e2e")
    assert esito == "PASS", evidenza


def test_t3_dedup_redelivery(tmp_path: Path) -> None:
    esito, evidenza = run_t3(N_DEDUP, N_DEDUP_DUP, SEED,
                             out_dir=tmp_path / "raw-t3")
    assert esito == "PASS", evidenza


def test_t5_burst_gap_parity(tmp_path: Path) -> None:
    esito, evidenza = run_t5(N_BURST, SEED, out_dir=tmp_path / "raw-t5")
    assert esito == "PASS", evidenza


if __name__ == "__main__":
    raise SystemExit(main())
