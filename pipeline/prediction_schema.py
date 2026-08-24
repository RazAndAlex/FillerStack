"""Schema prediction v1 (M9, ADR-0020) — `pipeline/prediction_schema.py`.

Un record per (valvola, finestra, predizione), con provenienza completa del
modello (model_version + feature_schema_version, tracciabilità §47/§80). Lo
`schema_version` del record prediction è `"1.0"` (NELLO SPAZIO DELLE PREDICTION,
indipendente dall'envelope dei dati raw).

Campi (spec M9 §5):

    schema_version         const "1.0" (schema prediction)
    prediction_id          uuid v4  (base dedup/audit)
    model_version          string   (da sidecar model.joblib.json, obbligatorio)
    feature_schema_version string   (riferito a work/ml-feature-schema.md)
    prediction_ts          date-time UTC (momento dell'inferenza)
    machine_id             const "filler01"
    valve_id               int 1-35 (contratto)
    window_idx             int >= 0
    window_end_cycle_id    int      (ultimo ciclo della finestra)
    predicted_label        enum 7 classi (healthy + 6 fault type)
    anomaly_score          float [0,1] = 1 − P(healthy)
    probabilities          object {label: prob} per le classi note al fit
    feature_fingerprint    string sha256 dei 43 feature (tracciabilità input)

Separazione predizione/decisione (contesto §67-§68): `anomaly_score` è un
valore, NON un alert; isteresi/cooldown/persistenza della decisione restano M10.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from plcsim.ml_model import LABELS  # ("healthy",) + 6 fault type

# ---------------------------------------------------------------------------
# Percorsi
# ---------------------------------------------------------------------------
_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "edge" / "schemas" \
    / "prediction-v1.json"

# Vocabolario label (allineato a ml_model.LABELS / work/ml-feature-schema.md §3)
PREDICTION_LABELS = tuple(LABELS)

# Colonne della finestra feature necessarie alla tracciabilità (machine_code +
# window_idx + last_cycle_id): le 43 feature FEATURE_COLUMNS sono il vettore.
WINDOW_KEY_COLUMNS = ("machine_code", "window_idx", "last_cycle_id")


def now_utc_iso() -> str:
    """ISO8601 UTC con suffisso Z (convenzione envelope, ms).

    Unica implementazione del modulo (dedup FINDING M9-7):
    ``pipeline/ingest.py`` la importa da qui.
    """
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _load_prediction_schema() -> dict[str, Any]:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _validator() -> Draft202012Validator:
    """Validator con FormatChecker esplicito per uuid/date-time (stesso pattern
    di pipeline/validator.py: il format date-time è no-op senza checker locale)."""
    checker = FormatChecker()
    checker.checkers["date-time"] = (_check_date_time, (ValueError,))
    return Draft202012Validator(_load_prediction_schema(), format_checker=checker)


def _check_date_time(instance: object) -> bool:
    """Check date-time RFC 3339 (stdlib, senza dipendenze opzionali).

    Unica implementazione del checker (dedup FINDING M9-7):
    ``pipeline/validator.py`` la importa da qui.
    """
    import re
    from datetime import datetime as _dt
    if not isinstance(instance, str):
        return True
    m = re.match(
        r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$",
        instance)
    if not m:
        return False
    try:
        _dt.fromisoformat(instance.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_prediction(record: dict[str, Any]) -> None:
    """Valida un prediction record contro prediction-v1.json (FormatChecker)."""
    _validator().validate(record)


def build_prediction(
    *,
    machine_code: str,
    window_idx: int,
    window_end_cycle_id: int,
    predicted_label: str,
    probabilities: Mapping[str, float],
    feature_vector: Sequence[float],
    model_version: str,
    feature_schema_version: str,
    prediction_id: str | None = None,
    prediction_ts: str | None = None,
    healthy_label: str = "healthy",
) -> dict[str, Any]:
    """Costruisce un prediction record v1 conforme (spec M9 §5).

    - `machine_code`: "valve{N}" 0-based → convertito in valve_id 1-35;
    - `predicted_label`: classe predetta (∈ PREDICTION_LABELS);
    - `probabilities`: {label: prob} per le classi note al fit;
    - `anomaly_score` = 1 − P(healthy);
    - `feature_fingerprint` = sha256 dei 43 feature (hex, ordinati come dato).

    Le probabilità sono normalizzate? NO: si assumono già probabilità valide
    da `predict_proba` (somma 1). `anomaly_score` è clampato a [0,1] per
    robustezza (errori di arrotondamento float).
    """
    # valve_id dal machine_code "valve{N}": contratto 1-35 (interno 0-based)
    if not machine_code.startswith("valve"):
        raise ValueError(f"machine_code non valvola: {machine_code!r}")
    valve_id = int(machine_code[len("valve"):]) + 1
    if not 1 <= valve_id <= 35:
        raise ValueError(f"valve_id fuori range from {machine_code!r}")

    prob_healthy = float(probabilities.get(healthy_label, 0.0))
    anomaly = max(0.0, min(1.0, 1.0 - prob_healthy))

    if predicted_label not in PREDICTION_LABELS:
        raise ValueError(f"predicted_label fuori vocabolario: "
                         f"{predicted_label!r} (attesi {PREDICTION_LABELS})")

    fp = hashlib.sha256((",".join(repr(float(v)) for v in feature_vector))
                        .encode("utf-8")).hexdigest()

    record = {
        "schema_version": "1.0",
        "prediction_id": prediction_id or str(uuid4()),
        "model_version": model_version,
        "feature_schema_version": feature_schema_version,
        "prediction_ts": prediction_ts or now_utc_iso(),
        "machine_id": "filler01",
        "valve_id": valve_id,
        "window_idx": int(window_idx),
        "window_end_cycle_id": int(window_end_cycle_id),
        "predicted_label": predicted_label,
        "anomaly_score": round(anomaly, 12),
        "probabilities": {k: float(v) for k, v in probabilities.items()},
        "feature_fingerprint": fp,
    }
    validate_prediction(record)  # guardia: mai emettere record non conforme
    return record


__all__ = [
    "PREDICTION_LABELS", "WINDOW_KEY_COLUMNS", "validate_prediction",
    "build_prediction", "now_utc_iso",
]
