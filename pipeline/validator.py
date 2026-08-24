"""Validatore condiviso envelope (issue M8-02) — wire v1.0/v1.2 vs stored v1.1/v1.3.

Il consumer M8 (``pipeline/ingest.py``, issue M8-04) usa questo modulo per
la catena di validazione della spec §5.3:

    wire v1.0/v1.2 valid? ──no──▶ scarta + contatore schema_invalid (log sample)
          │ sì (schema_version ∈ {1.0, 1.2} && ingest_ts assente)
          ▼
    inject_ingest_ts(payload, now_utc)  (ingest_ts + bump: 1.0→1.1, 1.2→1.3)
          │
          ▼
    stored v1.1/v1.3 valid? ──no──▶ BUG (guardia): scarta, mai scrivere un
          │                       record invalido
          ▼
    dedup + scrittura Parquet

- ``validate_wire``:  envelope **v1.0** (edge/schemas/envelope-v1.json) e
  **v1.2** (edge/schemas/envelope-v1.2.json, contratto tag esteso M9,
  ADR-0020/issue M9-01) — dispatch su ``schema_version``; un record con
  ``ingest_ts`` è RIFIUTATO (campo riservato: lo schema wire non lo
  dichiara e additionalProperties:false lo vieta);
- ``validate_stored``: envelope **v1.1** (edge/schemas/envelope-v1.1.json) e
  **v1.3** (edge/schemas/envelope-v1.3.json, contratto tag esteso M9) —
  ``ingest_ts`` REQUIRED; ``recipe_id`` rifiutato (additionalProperties:false;
  nessuna versione corrente lo dichiara);
- ``inject_ingest_ts``: copia, inietta ``ingest_ts`` ISO8601 UTC (Z) e bump
  ``schema_version`` (v1.0→v1.1, v1.2→v1.3) — puro, testabile senza broker
  (spec §8 T6).

Gli errori sono ``jsonschema.ValidationError``: format (uuid, date-time)
come ASSERTION via ``FormatChecker`` (draft 2020-12) — stesso pattern di
``edge/tests/parity_check.py`` (M7 T2).

NOTA AMBIENTE (verificata 2026-08-12, jsonschema 4.26.0): il checker
``date-time`` di jsonschema è registrato SOLO se il pacchetto opzionale
``rfc3339-validator`` è installato (``with suppress(ImportError)`` in
``jsonschema/_format.py``) — in questo ambiente non lo è, quindi un
``FormatChecker()`` nudo renderebbe ``format: date-time`` una no-op
silenziosa. Questo modulo registra quindi un checker ``date-time``
esplicito, stdlib-only (regex RFC 3339 + ``datetime.fromisoformat``): la
verifica è una vera assertion a prescindere dai backend opzionali.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from pipeline.prediction_schema import _check_date_time  # unica implementazione (FINDING M9-7)

# ---------------------------------------------------------------------------
# Percorsi (tutto relativo a questo file: pipeline/validator.py)
# ---------------------------------------------------------------------------
_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "edge" / "schemas"
SCHEMA_WIRE_PATH = _SCHEMAS_DIR / "envelope-v1.json"      # v1.0 (M7, immutata)
SCHEMA_WIRE_V12_PATH = _SCHEMAS_DIR / "envelope-v1.2.json"  # v1.2 (M9, tag esteso)
SCHEMA_STORED_PATH = _SCHEMAS_DIR / "envelope-v1.1.json"  # v1.1 (M8, immutata)
SCHEMA_STORED_V13_PATH = _SCHEMAS_DIR / "envelope-v1.3.json"  # v1.3 (M9, tag esteso)

_UTC_Z = "Z"                       # convenzione envelope (event_ts, parity_check)
_TIMESPEC = "milliseconds"         # stessa precisione degli altri timestamp

# ``_check_date_time`` è definito in pipeline/prediction_schema.py (unica
# implementazione, dedup FINDING M9-7) e importato in cima al modulo; la
# registrazione esplicita del checker date-time è in ``_build_validator``
# (vedi NOTA AMBIENTE nel docstring del modulo).


def _load_schema(path: Path) -> dict[str, Any]:
    """Carica uno schema JSON e verifica la definizione stessa.

    ``Draft202012Validator.check_schema``: una definizione non valida è un
    errore di sviluppo, non di runtime (si alza al caricamento).
    """
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


@lru_cache(maxsize=1)
def load_schema_wire() -> dict[str, Any]:
    """Envelope v1.0 (wire: edge → MQTT, spec §4.3). Cache: migliaia di record."""
    return _load_schema(SCHEMA_WIRE_PATH)


@lru_cache(maxsize=1)
def load_schema_wire_v12() -> dict[str, Any]:
    """Envelope v1.2 (wire, contratto tag esteso M9, ADR-0020/issue M9-01)."""
    return _load_schema(SCHEMA_WIRE_V12_PATH)


@lru_cache(maxsize=1)
def load_schema_stored() -> dict[str, Any]:
    """Envelope v1.1 (stored: consumer → Parquet, spec §5)."""
    return _load_schema(SCHEMA_STORED_PATH)


@lru_cache(maxsize=1)
def load_schema_stored_v13() -> dict[str, Any]:
    """Envelope v1.3 (stored, contratto tag esteso M9)."""
    return _load_schema(SCHEMA_STORED_V13_PATH)


def _build_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Validator con FormatChecker: uuid/date-time sono ASSERTION (draft 2020-12).

    ``date-time`` è registrato ESPLICITAMENTE (vedi NOTA AMBIENTE nel
    docstring del modulo): senza, il format sarebbe una no-op silenziosa.
    """
    checker = FormatChecker()
    checker.checkers["date-time"] = (_check_date_time, (ValueError,))
    return Draft202012Validator(schema, format_checker=checker)


@lru_cache(maxsize=1)
def _validator_wire() -> Draft202012Validator:
    return _build_validator(load_schema_wire())


@lru_cache(maxsize=1)
def _validator_wire_v12() -> Draft202012Validator:
    return _build_validator(load_schema_wire_v12())


@lru_cache(maxsize=1)
def _validator_stored() -> Draft202012Validator:
    return _build_validator(load_schema_stored())


@lru_cache(maxsize=1)
def _validator_stored_v13() -> Draft202012Validator:
    return _build_validator(load_schema_stored_v13())


def _wire_validator_for(schema_version: str) -> Draft202012Validator:
    """Validator wire per versione (version-aware, retro-compatibile)."""
    if schema_version == "1.0":
        return _validator_wire()
    if schema_version == "1.2":
        return _validator_wire_v12()
    raise ValidationError(
        f"schema_version wire non supportata: {schema_version!r} "
        f"(attese: 1.0, 1.2)")


def _stored_validator_for(schema_version: str) -> Draft202012Validator:
    """Validator stored per versione (version-aware, retro-compatibile)."""
    if schema_version == "1.1":
        return _validator_stored()
    if schema_version == "1.3":
        return _validator_stored_v13()
    raise ValidationError(
        f"schema_version stored non supportata: {schema_version!r} "
        f"(attese: 1.1, 1.3)")


def validate_wire(payload: dict[str, Any]) -> None:
    """Valida un payload MQTT contro lo schema wire corretto (version-aware).

    Dispatch su ``schema_version`` ("1.0" o "1.2" — spec M7/M9). Solleva
    ``jsonschema.ValidationError`` se NON conforme. Un record con
    ``ingest_ts`` presente è RIFIUTATO (campo riservato: lo schema wire non
    lo dichiara e additionalProperties:false lo vieta — spec §4.3/§5.2): il
    timestamp di ingestione è iniettato SOLO dal consumer, mai dall'edge
    (§58). Chi chiama decide la politica (contatore schema_invalid + log).
    """
    sv = payload.get("schema_version") if isinstance(payload, dict) else None
    if sv not in ("1.0", "1.2"):
        raise ValidationError(
            f"schema_version wire mancante o non supportata: {sv!r} "
            f"(attese: 1.0, 1.2)")
    _wire_validator_for(sv).validate(payload)


def validate_stored(record: dict[str, Any]) -> None:
    """Valida un record contro lo schema stored corretto (version-aware).

    Dispatch su ``schema_version`` ("1.1" o "1.3" — ingest_ts REQUIRED in
    entrambe). Solleva ``jsonschema.ValidationError`` se NON conforme.
    ``recipe_id`` è rifiutato (additionalProperties:false — nessuna versione
    corrente lo dichiara). Il writer non deve MAI scrivere un record che
    fallisce qui (guardia, spec §5.3).
    """
    sv = record.get("schema_version") if isinstance(record, dict) else None
    if sv not in ("1.1", "1.3"):
        raise ValidationError(
            f"schema_version stored mancante o non supportata: {sv!r} "
            f"(attese: 1.1, 1.3)")
    _stored_validator_for(sv).validate(record)


def inject_ingest_ts(payload: dict[str, Any], ts: datetime | str) -> dict[str, Any]:
    """Wire → stored: inietta ``ingest_ts`` e bumpa ``schema_version``.

    Version-aware: wire v1.0 → stored v1.1, wire v1.2 → stored v1.3
    (catena coerente: stored = wire + ingest_ts, minor+1).

    Ritorna una COPIA (l'input non è modificato). ``ts``: ``datetime`` aware
    (un naive è interpretato come UTC) oppure stringa ISO8601 già formata;
    l'output è ISO8601 UTC con suffisso ``Z`` (convenzione envelope). Il
    record risultante valida contro lo schema stored corrispondente.

    Se il payload contiene GIÀ ``ingest_ts`` solleva ``ValueError``: il wire
    con ``ingest_ts`` è rifiutato a monte da ``validate_wire`` e non deve MAI
    essere iniettato due volte (spec §5.3 "mai iniettato due volte").
    """
    if "ingest_ts" in payload:
        raise ValueError(
            "payload già contenente ingest_ts: il wire lo vieta "
            "(validate_wire a monte) — mai iniettare due volte (spec §5.3)")
    wire_version = payload.get("schema_version")
    bump = {"1.0": "1.1", "1.2": "1.3"}.get(wire_version)
    if bump is None:
        raise ValueError(
            f"inject_ingest_ts: schema_version wire non supportata: "
            f"{wire_version!r} (attese: 1.0, 1.2)")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)  # naive = UTC per contratto
        ts_str = ts.astimezone(timezone.utc).isoformat(
            timespec=_TIMESPEC).replace("+00:00", _UTC_Z)
    else:
        ts_str = ts
    record = copy.deepcopy(payload)
    record["ingest_ts"] = ts_str
    record["schema_version"] = bump
    return record
