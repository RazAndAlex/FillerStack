"""Test W3 — ml_metrics (Track D): metriche pure per classe / macro /
valve-level / ritardo detection / FAR / confronto baseline.

Tutti i valori attesi sono CALCOLATI A MANO (fonte indipendente: specifica
AC-ML-3/AC-ML-4a, work/plan-ml-v2.md §8). Frame sintetici, nessun run di
simulazione (pre-marker).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plcsim.ml_metrics import (  # noqa: E402
    HEALTHY, binary_confusion, comparison_table, detection_delay, far_healthy,
    macro_precision, macro_recall, macro_recall_faults, min_class_recall,
    miss_composition, ml_vs_baseline_comparison, precision_recall_per_class,
    recall_per_class, valve_flags,
)

CLASSES = (HEALTHY, "restriction", "closing_delay", "opening_delay",
           "pressure_instability", "flowmeter_dropout", "flowmeter_glitch")

# Confusion matrix giocattolo 7×7 (righe = vera, colonne = predetta).
# Valori attesi calcolati a mano in fondo al modulo.
CM = [
    [90, 5, 0, 0, 0, 0, 0],    # healthy
    [4, 40, 1, 0, 0, 0, 0],    # restriction
    [2, 0, 35, 0, 0, 0, 0],    # closing_delay
    [0, 0, 0, 30, 0, 0, 0],    # opening_delay
    [10, 0, 0, 0, 20, 0, 0],   # pressure_instability
    [8, 0, 0, 0, 0, 12, 0],    # flowmeter_dropout
    [5, 0, 0, 0, 0, 0, 15],    # flowmeter_glitch
]


def _near(a: float, b: float) -> bool:
    return math.isclose(a, b, abs_tol=1e-9)


# --------------------------------------------------------------------------
# (a) Precision/recall per classe su confusion matrix (valori a mano)
# --------------------------------------------------------------------------


def test_precision_recall_per_class_hand_computed():
    pr = precision_recall_per_class(CM, CLASSES)
    # healthy: prec 90/119 (colonna healthy = 90+4+2+0+10+8+5), rec 90/95
    assert _near(pr[HEALTHY]["precision"], 90 / 119)
    assert _near(pr[HEALTHY]["recall"], 90 / 95)
    # restriction: prec 40/45, rec 40/45
    assert _near(pr["restriction"]["precision"], 40 / 45)
    assert _near(pr["restriction"]["recall"], 40 / 45)
    # closing_delay: prec 35/36, rec 35/37
    assert _near(pr["closing_delay"]["precision"], 35 / 36)
    assert _near(pr["closing_delay"]["recall"], 35 / 37)
    # opening_delay: 30/30, 30/30
    assert _near(pr["opening_delay"]["precision"], 1.0)
    assert _near(pr["opening_delay"]["recall"], 1.0)
    # pressure_instability: prec 20/20, rec 20/30
    assert _near(pr["pressure_instability"]["precision"], 1.0)
    assert _near(pr["pressure_instability"]["recall"], 20 / 30)
    # flowmeter_dropout: prec 12/12, rec 12/20
    assert _near(pr["flowmeter_dropout"]["precision"], 1.0)
    assert _near(pr["flowmeter_dropout"]["recall"], 12 / 20)
    # flowmeter_glitch: prec 15/15, rec 15/20
    assert _near(pr["flowmeter_glitch"]["precision"], 1.0)
    assert _near(pr["flowmeter_glitch"]["recall"], 15 / 20)


def test_per_class_zero_support_is_zero():
    # classe senza supporto: precision e recall 0.0 (mai NaN nei macro)
    cm = [[0, 2], [0, 8]]
    pr = precision_recall_per_class(cm, [HEALTHY, "restriction"])
    assert pr[HEALTHY] == {"precision": 0.0, "recall": 0.0}


# --------------------------------------------------------------------------
# (b) Macro-recall: 7 classi (headline) e 6 classi fault (informativo D-ML-3b)
# --------------------------------------------------------------------------


def test_macro_recall_all_and_faults_hand_computed():
    # macro 7 classi = media delle 7 recall
    expected_all = (90 / 95 + 40 / 45 + 35 / 37 + 1.0 + 20 / 30
                    + 12 / 20 + 15 / 20) / 7
    assert _near(macro_recall(CM, CLASSES), expected_all)
    # macro 6 classi fault = media delle 6 recall (senza healthy)
    expected_faults = (40 / 45 + 35 / 37 + 1.0 + 20 / 30 + 12 / 20 + 15 / 20) / 6
    assert _near(macro_recall_faults(CM, CLASSES), expected_faults)
    # macro su sole classi healthy → None (nessuna classe di fault)
    assert macro_recall_faults([[8]], [HEALTHY]) is None


# --------------------------------------------------------------------------
# (c) Recall min-classe: primaria = 7 classi; faults_only informativa
# --------------------------------------------------------------------------


def test_min_class_recall_primary_all_classes():
    res = min_class_recall(CM, CLASSES)
    # minimo su 7 classi = 12/20 (flowmeter_dropout)
    assert _near(res["all"], 12 / 20)
    assert _near(res["faults_only"], 12 / 20)


def test_min_class_recall_faults_only_differs():
    # healthy con recall bassa (0.2) vs fault con recall alta (0.9): la
    # primaria (7 classi) è trascinata da healthy, faults_only no (D-ML-3b)
    cm = [[2, 8], [1, 9]]  # riga healthy [2,8] rec 0.2; riga restriction [1,9]
    res = min_class_recall(cm, [HEALTHY, "restriction"])
    assert _near(res["all"], 0.2)
    assert _near(res["faults_only"], 0.9)


# --------------------------------------------------------------------------
# (d) Macro-precision
# --------------------------------------------------------------------------


def test_macro_precision_hand_computed():
    expected = (90 / 119 + 40 / 45 + 35 / 36 + 1.0 + 1.0 + 1.0 + 1.0) / 7
    assert _near(macro_precision(CM, CLASSES), expected)


# --------------------------------------------------------------------------
# (e) Aggregazione valve-level (θ%)
# --------------------------------------------------------------------------


def test_valve_flags_theta_percent():
    mcs = ["v1"] * 10 + ["v2"] * 10 + ["v3"] * 5
    preds = ([HEALTHY] * 7 + ["restriction"] * 3          # v1: 3/10 = 30%
             + [HEALTHY] * 2 + ["closing_delay"] * 8      # v2: 8/10 = 80%
             + [HEALTHY] * 5)                             # v3: 0/5
    assert valve_flags(mcs, preds, 30) == {"v1", "v2"}    # ≥ 30% (v3 no)
    assert valve_flags(mcs, preds, 40) == {"v2"}          # 30% < 40% → v1 no
    assert valve_flags(mcs, preds, 80) == {"v2"}          # 80% ≥ 80% sì
    assert valve_flags(mcs, preds, 90) == set()           # 80% < 90%


def test_valve_flags_rejects_bad_theta():
    with pytest.raises(ValueError, match="\\[0, 100\\]"):
        valve_flags(["v1"], [HEALTHY], 150.0)


# --------------------------------------------------------------------------
# (f) Ritardo di detection (onset GT noto, k consecutive)
# --------------------------------------------------------------------------


def test_detection_delay_hand_computed():
    gt = [HEALTHY] * 4 + ["restriction"] * 6   # onset_idx = 4
    # sostenuto da w=5 (w5,w6 corretti): ritardo 5-4 = 1
    pred = [HEALTHY] * 5 + ["restriction"] * 5
    assert detection_delay(gt, pred, onset_idx=4, fault_label="restriction") == 1
    # corretta già all'onset (w4,w5): ritardo 0
    pred0 = [HEALTHY] * 4 + ["restriction"] * 6
    assert detection_delay(gt, pred0, onset_idx=4, fault_label="restriction") == 0
    # mai rilevata
    pred_never = [HEALTHY] * 10
    assert detection_delay(gt, pred_never, onset_idx=4,
                           fault_label="restriction") is None
    # k=3: serve un tratto di 3 consecutive; w4 errata, w5 sana, w6-w8 ok → 2
    pred3 = [HEALTHY] * 4 + ["restriction", HEALTHY, "restriction",
                             "restriction", "restriction"] + [HEALTHY]
    assert detection_delay(gt, pred3, onset_idx=4,
                           fault_label="restriction", k=3) == 2


def test_detection_delay_validates_onset():
    with pytest.raises(ValueError, match="onset_idx"):
        detection_delay([HEALTHY] * 3, [HEALTHY] * 3, onset_idx=5,
                        fault_label="restriction")


# --------------------------------------------------------------------------
# (g) FAR su healthy (AC-ML-4c)
# --------------------------------------------------------------------------


def test_far_healthy_zero_and_nonzero():
    mcs = ["v1"] * 10 + ["v2"] * 10
    preds = ([HEALTHY] * 10 + [HEALTHY] * 10)
    res = far_healthy(mcs, preds, 50)
    assert res["flagged"] == set() and res["far"] == 0.0
    assert res["n_valves"] == 2
    preds2 = ([HEALTHY] * 6 + ["restriction"] * 4  # v1: 40% non-healthy
              + [HEALTHY] * 10)
    res2 = far_healthy(mcs, preds2, 50)
    assert res2["flagged"] == set() and res2["far"] == 0.0  # 40% < 50%
    res3 = far_healthy(mcs, preds2, 40)
    assert res3["flagged"] == {"v1"} and res3["far"] == 0.5


# --------------------------------------------------------------------------
# (i) Raw TP/FP/FN del detector + composizione dei miss per tipo fault
# --------------------------------------------------------------------------


def test_binary_confusion_hand_computed():
    gt = {("v1", "r1"): True, ("v2", "r1"): True, ("v3", "r1"): False}
    flags = {("v1", "r1"), ("v3", "r1")}
    assert binary_confusion(gt, flags) == {"tp": 1, "fp": 1, "fn": 1, "tn": 0}


def test_miss_composition_by_fault_type():
    types = {("v1", "r1"): "restriction", ("v2", "r1"): "closing_delay",
             ("v4", "r1"): "restriction"}
    flags = {("v1", "r1")}
    assert miss_composition(types, flags) == {"closing_delay": 1,
                                              "restriction": 1}


# --------------------------------------------------------------------------
# (j) recall_B0 per classe
# --------------------------------------------------------------------------


def test_recall_per_class_detector():
    types = {("v1", "r1"): "restriction", ("v2", "r1"): "restriction",
             ("v3", "r1"): "closing_delay"}
    flags = {("v1", "r1")}
    rec = recall_per_class(types, flags)
    assert rec == {"closing_delay": 0.0, "restriction": 0.5}


# --------------------------------------------------------------------------
# (h) Tabella confronto (valvola, run) ML vs baseline a disuguaglianze
# --------------------------------------------------------------------------


def test_comparison_table_columns_and_order():
    entries = [("v2", "r1", True), ("v1", "r1", True), ("v4", "r1", False),
               ("v3", "r1", True)]
    ml = {("v1", "r1"), ("v2", "r1"), ("v4", "r1")}
    b0 = {("v1", "r1")}
    t = comparison_table(entries, ml, b0)
    assert t.columns == ["machine_code", "run_name", "faulted",
                         "ml_flag", "b0_flag"]
    assert t.height == 4
    # ordine deterministico per (machine_code, run_name)
    assert t["machine_code"].to_list() == ["v1", "v2", "v3", "v4"]
    row_v3 = t.filter(pl.col("machine_code") == "v3").row(0)
    assert row_v3[2] is True and row_v3[3] is False and row_v3[4] is False


def test_ml_vs_baseline_inequalities_hand_computed():
    entries = [("v1", "r1", True), ("v2", "r1", True), ("v3", "r1", True),
               ("v4", "r1", False)]
    ml = {("v1", "r1"), ("v2", "r1"), ("v4", "r1")}
    b0 = {("v1", "r1")}
    out = ml_vs_baseline_comparison(entries, ml, b0, precision_tol=0.05)
    # ML: tp 2 (v1,v2), fp 1 (v4), fn 1 (v3) → recall 2/3, precision 2/3
    assert out["tp_ml"] == 2 and out["fp_ml"] == 1 and out["fn_ml"] == 1
    assert _near(out["recall_ml"], 2 / 3)
    assert _near(out["precision_ml"], 2 / 3)
    # B0: tp 1 (v1), fp 0, fn 2 (v2,v3) → recall 1/3, precision 1.0
    assert out["tp_b0"] == 1 and out["fp_b0"] == 0 and out["fn_b0"] == 2
    assert _near(out["recall_b0"], 1 / 3)
    assert _near(out["precision_b0"], 1.0)
    # disuguaglianze D-ML-5
    assert out["recall_ml_ge_b0"] is True    # 2/3 >= 1/3
    assert out["precision_ml_ge_b0_tol"] is False  # 2/3 < 1.0 − 0.05


def test_ml_vs_baseline_undefined_metrics():
    # nessuna valvola faulted → recall non definita → None e disuguaglianza
    # False (mai NaN, mai violazione fittizia)
    entries = [("v1", "r1", False)]
    out = ml_vs_baseline_comparison(entries, set(), set())
    assert out["recall_ml"] is None and out["recall_b0"] is None
    assert out["recall_ml_ge_b0"] is False
    assert out["precision_ml_ge_b0_tol"] is False
