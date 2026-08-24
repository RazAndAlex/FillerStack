"""Layer ML — wrapper classificatore (D-W3, Track D).

Contratto: `work/ml-feature-schema.md` (schema feature CONGELATO — 43 feature,
ML-F1) + `work/plan-ml-v2.md` §1.4/§7/§14 (D-ML-1 RATIFICATO 2026-08-11).

Modello PRIMARIO: regressione logistica MULTINOMIALE deterministica
(`LogisticRegression(solver="lbfgs", class_weight="balanced", random_state=42,
max_iter=1000)`). lbfgs in scikit-learn supporta SOLO la multinomiale: la
decisione D-ML-1 `multi_class="multinomial"` è resa esplicita solo se il
parametro esiste nella versione installata (vedi DEVIAZIONE-1) — con lbfgs il
modello risultante è identico nei due casi.

Escalation (NON attivo di default — SOLO su evidenza di validation, D-ML-1):
`HistGradientBoostingClassifier` a random_state fisso e, se accettato dalla
versione installata, `n_jobs=1` (determinismo byte-identico AC-ML-2,
practitioner F4; vedi DEVIAZIONE-2).

Determinismo: stesso input (stessi dati, stesso ordine di righe) → stesse
predizioni (lbfgs deterministico a parità di dati/ordine righe; random_state
fisso; il save/load è un roundtrip esatto via joblib, test dedicato).

Nessun fallback numpy (vietato dal contratto: D-ML-1, solo contingenza da
root-veto). Nessuna normalizzazione qui: lo z-score per-valvola è applicato a
monte da `ml_dataset` (work/ml-feature-schema.md §6) — questo wrapper riceve la
matrice feature già normalizzata (ESATTAMENTE 43 colonne).

DEVIAZIONI IMPLEMENTATIVE (protocollo §5.3 — riportate in work/ml-w3-model.md):
  1. `multi_class="multinomial"` — NON accettato da sklearn 1.9.0 dell'env
     (TypeError: unexpected keyword argument; parametro rimosso in sklearn>=1.7
     perché lbfgs supporta SOLO la multinomiale). Omissione CONDIZIONALE via
     ispezione firma (`_accepts`); modello matematicamente identico.
  2. `n_jobs=1` su HistGradientBoostingClassifier — parametro ASSENTE in
     sklearn 1.9.0 (ispezione firma). Omesso condizionalmente; il fit HistGBM
     moderno è single-threaded (niente istogramma threadato) e random_state è
     fisso → determinismo AC-ML-2 preservato.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Optional, Sequence

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

# Vocabolario label per finestra (work/ml-feature-schema.md §3): "healthy" +
# i 6 fault type di FAULT_TYPES (scenario.py:31-33). Costante locale con
# riferimento al contratto (stile analytics.py: mappe locali referenziate).
HEALTHY = "healthy"
FAULT_TYPES = ("restriction", "closing_delay", "opening_delay",
               "pressure_instability", "flowmeter_dropout", "flowmeter_glitch")
LABELS = (HEALTHY,) + FAULT_TYPES

# Whitelist CHIUSA di ESATTAMENTE 43 feature (work/ml-feature-schema.md §2).
FEATURE_COUNT = 43

_SIDECAR_SUFFIX = ".json"


def _accepts(cls: type, param: str) -> bool:
    """True se il costruttore della classe sklearn accetta `param` (env-safe).

    Permette di esprimere la decisione D-ML-1 letteralmente (multi_class,
    n_jobs) dove l'API della versione installata lo consente e di omettere il
    parametro dove è stato rimosso (DEVIAZIONE-1/-2), senza cambiare il modello.
    """
    return param in inspect.signature(cls.__init__).parameters


class MLModel:
    """Wrapper deterministico del classificatore (logistic multinomiale lbfgs).

    API: fit(X, y) / predict(X) / predict_proba(X) / save(path) / load(path).
    X: matrice (n_windows, 43) di feature già z-score-normalizzate a monte;
    y: label per finestra ∈ LABELS (stringhe). Dopo il fit, `classes_` (np.ndarray)
    contiene le classi note al fit (in ordine sklearn).
    """

    def __init__(self, kind: str = "logistic", random_state: int = 42,
                 max_iter: int = 1000, class_weight: Optional[str] = "balanced",
                 model_version: Optional[str] = None,
                 feature_schema_version: Optional[str] = None):
        if kind not in ("logistic", "histgbm"):
            raise ValueError(f"kind sconosciuto: {kind!r} (attesi: 'logistic', "
                             f"'histgbm')")
        self.kind = kind
        self.random_state = random_state
        self.max_iter = max_iter
        self.class_weight = class_weight
        # M9 (ADR-0020): tracciabilità del modello — model_version (da code_version
        # del manifest o tag esplicito) e feature_schema_version (riferito a
        # work/ml-feature-schema.md ML-F1). None = non versionato (modelli pre-M9).
        self.model_version = model_version
        self.feature_schema_version = feature_schema_version
        if kind == "logistic":
            kwargs: dict[str, Any] = dict(
                solver="lbfgs", class_weight=class_weight,
                random_state=random_state, max_iter=max_iter,
            )
            if _accepts(LogisticRegression, "multi_class"):
                kwargs["multi_class"] = "multinomial"  # DEVIAZIONE-1 (env-safe)
            self._clf = LogisticRegression(**kwargs)
        else:
            kwargs = dict(random_state=random_state, max_iter=max_iter)
            if _accepts(HistGradientBoostingClassifier, "n_jobs"):
                kwargs["n_jobs"] = 1  # DEVIAZIONE-2 (env-safe)
            self._clf = HistGradientBoostingClassifier(**kwargs)
        self.classes_: Optional[np.ndarray] = None
        self.is_fitted = False

    @property
    def estimator(self) -> Any:
        """Estimator sklearn sottostante (per introspezione pipeline/report)."""
        return self._clf

    def fit(self, X: Sequence, y: Sequence) -> "MLModel":
        """Fit sul dataset (finestre × 43 feature, label per finestra).

        Guardie contratto: X deve avere ESATTAMENTE 43 colonne (schema
        congelato) e y solo label del vocabolario LABELS — errore esplicito.
        """
        X = np.asarray(X, dtype=float)
        y = np.asarray(y)
        if X.ndim != 2 or X.shape[1] != FEATURE_COUNT:
            raise ValueError(
                f"X deve avere esattamente {FEATURE_COUNT} colonne (schema "
                f"feature congelato, ML-F1); ottenute "
                f"{X.shape[1] if X.ndim == 2 else X.ndim}.")
        unknown = sorted({str(label) for label in y} - set(LABELS))
        if unknown:
            raise ValueError(
                f"label sconosciute al vocabolario: {unknown} (attese: "
                f"healthy + 6 fault type — work/ml-feature-schema.md §3).")
        self._clf.fit(X, y)
        self.classes_ = np.asarray(self._clf.classes_)
        self.is_fitted = True
        return self

    def predict_proba(self, X: Sequence) -> np.ndarray:
        """Probabilità per finestra; colonne allineate a `classes_`."""
        return np.asarray(self._clf.predict_proba(np.asarray(X, dtype=float)))

    def predict(self, X: Sequence) -> np.ndarray:
        """Classe predetta per finestra (stringhe del vocabolario LABELS)."""
        return np.asarray(self._clf.predict(np.asarray(X, dtype=float)))

    def save(self, path: Any) -> None:
        """Serializza estimator (joblib) + sidecar JSON riproducibile.

        Sidecar: `path + ".json"` con schema_version, kind, iperparametri e
        classi note al fit (sort_keys, nessun arrotondamento — stile
        analytics.py). Roundtrip save/load → predizioni identiche (test).
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self._clf, path)
        meta = {
            "schema_version": 1,
            "kind": self.kind,
            "random_state": self.random_state,
            "max_iter": self.max_iter,
            "class_weight": self.class_weight,
            "is_fitted": self.is_fitted,
            # M9 (ADR-0020): tracciabilità (opzionale, None se non impostata)
            "model_version": self.model_version,
            "feature_schema_version": self.feature_schema_version,
            "classes": list(self.classes_) if self.classes_ is not None else None,
        }
        Path(str(path) + _SIDECAR_SUFFIX).write_text(
            json.dumps(meta, sort_keys=True, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Any) -> "MLModel":
        """Ricarica modello salvato con save(); sidecar obbligatorio."""
        path = Path(path)
        sidecar = Path(str(path) + _SIDECAR_SUFFIX)
        if not sidecar.exists():
            raise FileNotFoundError(
                f"sidecar mancante: {sidecar} (save() scrive sidecar JSON "
                f"obbligatorio per la provenienza).")
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        if meta.get("schema_version") != 1:
            raise ValueError(f"schema_version sidecar sconosciuto: "
                             f"{meta.get('schema_version')!r}")
        model = cls(kind=meta["kind"], random_state=meta["random_state"],
                    max_iter=meta["max_iter"],
                    class_weight=meta.get("class_weight", "balanced"))
        model._clf = joblib.load(path)
        model.is_fitted = meta["is_fitted"]
        model.model_version = meta.get("model_version")
        model.feature_schema_version = meta.get("feature_schema_version")
        model.classes_ = (np.asarray(meta["classes"])
                          if meta["classes"] is not None else None)
        return model
