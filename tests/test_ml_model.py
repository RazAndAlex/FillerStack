"""Test W3 — ml_model (Track D): wrapper logistic multinomiale deterministica.

Contratto D-ML-1 (RATIFICATO 2026-08-11, work/plan-ml-v2.md §1.4/§7/§14):
fit 2× stesso dataset → predizioni identiche (determinismo); save/load
roundtrip → predizioni identiche; class_weight="balanced" alza la recall della
classe rara su dataset sbilanciato sintetico; dataset linearmente separabile →
accuracy ≥ soglia sanità. Tutto su frame SINTETICI (nessun run di simulazione,
pre-marker). Dataset: 43 feature (schema congelato, ML-F1); label =
vocabolario LABELS.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plcsim.ml_model import (  # noqa: E402
    FEATURE_COUNT, FAULT_TYPES, HEALTHY, MLModel,
)

# --------------------------------------------------------------------------
# Dataset sintetici deterministici (LCG aritmetico puro, nessun RNG)
# --------------------------------------------------------------------------


def _uni(i: int) -> float:
    """Uniforme pseudo-casuale deterministica in [0,1) (aritmetica pura)."""
    return ((i * 1103515245 + 12345) % 2147483648) / 2147483648.0


def _make_dataset(n: int, seed: int, label2: str = "restriction",
                  gap: bool = True) -> tuple:
    """n finestre × 43 feature.

    x0 è il canale di segnale: gap=True → classi linearmente separabili
    (healthy x0∈[0.05,0.30], l'altra x0∈[0.70,0.95], gap [0.30,0.70]);
    gap=False → x0∈[0.35,0.65] per entrambe (overlap, non separabile).
    Le altre 42 feature sono rumore uniforme. Assegnazione classe da LCG.
    """
    X = np.zeros((n, FEATURE_COUNT))
    y = []
    for r in range(n):
        cls = 0 if _uni(seed + r) < 0.5 else 1
        if gap:
            lo, hi = (0.05, 0.30) if cls == 0 else (0.70, 0.95)
        else:
            lo, hi = 0.35, 0.65
        X[r, 0] = lo + (hi - lo) * _uni(seed + 1_000_000 + r)
        for c in range(1, FEATURE_COUNT):
            X[r, c] = _uni(seed + 3_000_000 + r * FEATURE_COUNT + c)
        y.append(HEALTHY if cls == 0 else label2)
    return X, np.asarray(y)


def _make_3class(n: int, seed: int) -> tuple:
    """3 classi separabili su x0: healthy / restriction / closing_delay."""
    bands = {HEALTHY: (0.05, 0.30), "restriction": (0.35, 0.60),
             "closing_delay": (0.65, 0.95)}
    X = np.zeros((n, FEATURE_COUNT))
    y = []
    for r in range(n):
        cls = ["healthy", "restriction", "closing_delay"][
            int(_uni(seed + r) * 3) % 3]
        lo, hi = bands[cls]
        X[r, 0] = lo + (hi - lo) * _uni(seed + 1_000_000 + r)
        for c in range(1, FEATURE_COUNT):
            X[r, c] = _uni(seed + 3_000_000 + r * FEATURE_COUNT + c)
        y.append(cls)
    return X, np.asarray(y)


def _recall_rare(y_true, y_pred, rare: str) -> float:
    m = y_true == rare
    return float((y_pred[m] == rare).mean())


# --------------------------------------------------------------------------
# Determinismo (fit 2× stesso dataset → predizioni identiche)
# --------------------------------------------------------------------------


def test_fit_twice_identical_predictions():
    """AC: stesso input → stesse predizioni (lbfgs deterministico)."""
    X, y = _make_3class(400, seed=11)
    m1 = MLModel().fit(X, y)
    m2 = MLModel().fit(X, y)
    assert np.array_equal(m1.predict(X), m2.predict(X))
    assert np.array_equal(m1.predict_proba(X), m2.predict_proba(X))
    assert list(m1.classes_) == ["closing_delay", HEALTHY, "restriction"]
    assert m1.classes_.dtype.kind == "U"  # label stringhe (schema §3)


# --------------------------------------------------------------------------
# Save/load roundtrip → predizioni identiche
# --------------------------------------------------------------------------


def test_save_load_roundtrip_identical(tmp_path):
    X, y = _make_3class(400, seed=11)
    m = MLModel().fit(X, y)
    before = m.predict(X)
    path = tmp_path / "model.joblib"
    m.save(path)
    m2 = MLModel.load(path)
    assert np.array_equal(m2.predict(X), before)
    assert np.array_equal(m2.predict_proba(X), m.predict_proba(X))
    assert list(m2.classes_) == list(m.classes_)
    assert m2.kind == m.kind == "logistic"
    # sidecar JSON riproducibile (stile analytics.py)
    sidecar = Path(str(path) + ".json")
    assert sidecar.exists() and '"schema_version": 1' in sidecar.read_text()


# --------------------------------------------------------------------------
# class_weight="balanced" alza la recall della classe rara
# --------------------------------------------------------------------------


def test_balanced_raises_rare_class_recall():
    """Dataset sbilanciato (95/5): recall classe rara con balanced > senza."""
    rng = np.random.RandomState(42)  # seed fisso → deterministico
    n = 2000
    cls = rng.choice([0, 1], size=n, p=[0.95, 0.05])
    X = np.zeros((n, FEATURE_COUNT))
    X[:, 0] = np.where(cls == 0, rng.normal(0.0, 1.0, n),
                       rng.normal(1.2, 1.0, n))
    X[:, 1:] = rng.normal(0.0, 1.0, (n, FEATURE_COUNT - 1))
    y = np.asarray([HEALTHY if c == 0 else "restriction" for c in cls])

    m_bal = MLModel().fit(X, y)          # default class_weight="balanced"
    m_none = MLModel(class_weight=None).fit(X, y)
    assert m_bal.class_weight == "balanced"
    rec_bal = _recall_rare(y, m_bal.predict(X), "restriction")
    rec_none = _recall_rare(y, m_none.predict(X), "restriction")
    assert rec_bal > rec_none, (rec_bal, rec_none)


# --------------------------------------------------------------------------
# Dataset linearmente separabile → accuracy ≥ soglia sanità
# --------------------------------------------------------------------------


def test_separable_dataset_high_accuracy():
    X, y = _make_dataset(600, seed=7, gap=True)
    m = MLModel().fit(X, y)
    acc = float((m.predict(X) == y).mean())
    assert acc >= 0.98, acc


# --------------------------------------------------------------------------
# Guardie contratto (schema 43 feature, vocabolario label)
# --------------------------------------------------------------------------


def test_fit_rejects_wrong_feature_count():
    X, y = _make_dataset(60, seed=3)
    with pytest.raises(ValueError, match="43"):
        MLModel().fit(X[:, :42], y)


def test_fit_rejects_unknown_labels():
    X, y = _make_dataset(60, seed=3, label2="bogus_fault")
    with pytest.raises(ValueError, match="label sconosciute"):
        MLModel().fit(X, y)


def test_rejects_unknown_kind():
    with pytest.raises(ValueError, match="kind sconosciuto"):
        MLModel(kind="numpy_fallback")  # vietato dal contratto (D-ML-1)


# --------------------------------------------------------------------------
# Escalation HistGBM: wrapper pronto, NON attivo di default (D-ML-1)
# --------------------------------------------------------------------------


def test_histgbm_wrapper_ready_and_deterministic():
    """HistGBM è escalation (solo su evidenza di validation): random_state
    fisso, n_jobs=1 se accettato dall'API installata (DEVIAZIONE-2)."""
    X, y = _make_dataset(300, seed=5, gap=True)
    m = MLModel(kind="histgbm").fit(X, y)
    p1, p2 = m.predict(X), m.predict(X)
    assert np.array_equal(p1, p2)
    acc = float((p1 == y).mean())
    assert acc >= 0.98, acc


def test_default_kind_is_logistic():
    m = MLModel()
    assert m.kind == "logistic"
    assert m.estimator.solver == "lbfgs"  # DEVIAZIONE-1: lbfgs è multinomial-only
