"""Layer ML — metriche di valutazione (D-W3, Track D).

Funzioni PURE su arrays/DataFrame (nessuno stato, nessun RNG, deterministiche):
contratto `work/plan-ml-v2.md` §5.2/§8 (AC-ML-3, AC-ML-4a/b/c) + decisioni
RATIFICATE D-ML-3/D-ML-5 (2026-08-11). Tutte le metriche sono calcolabili su
frame sintetici (test anytime, pre-marker).

Convenzioni di reporting (vincolanti):
- Unità finestra: label = "healthy" + i 6 fault type
  (work/ml-feature-schema.md §3; FAULT_TYPES scenario.py:31-33).
- **recall min-classe**: PRIMARIA = minimo sulle 7 classi (healthy + 6 fault,
  headline AC-ML-3 "recall min-classe ≥ 0,60"); riportata ANCHE la minima sulle
  SOLE 6 classi di fault (`faults_only`, binding informativo D-ML-3b/F6: il
  recall healthy ~0,95 inflaziona macro e minimi su 7 classi).
- macro-recall riportata su 7 classi (headline AC-ML-3) E sulle sole 6 classi
  fault (`macro_recall_faults`, binding informativo D-ML-3b).
- Per-class score = 0.0 quando la classe non ha supporto (convenzione
  zero_division sklearn) — mai NaN nei macro.
- Livello (valvola, run): chiave = (machine_code, run_name). Il confronto
  baseline ML vs detector 3σ (B0) è a DISUGUAGLIANZE (D-ML-5):
  recall_ML ≥ recall_B0 e precision_ML ≥ precision_B0 − 0,05 (tolleranza
  `precision_tol`, scelta di calibrazione da dichiarare al freeze).
"""
from __future__ import annotations

from typing import AbstractSet, Any, Mapping, Optional, Sequence

import numpy as np
import polars as pl

HEALTHY = "healthy"
FAULT_TYPES = ("restriction", "closing_delay", "opening_delay",
               "pressure_instability", "flowmeter_dropout", "flowmeter_glitch")
LABELS = (HEALTHY,) + FAULT_TYPES


def _as_cm(cm: Sequence, classes: Sequence[str]) -> np.ndarray:
    arr = np.asarray(cm, dtype=float)
    if arr.ndim != 2 or arr.shape[0] != arr.shape[1]:
        raise ValueError("cm deve essere una matrice quadrata")
    if arr.shape[0] != len(classes):
        raise ValueError("cm e classes devono avere la stessa cardinalità "
                         "(righe/colonne = true/pred, stesso ordine di classes)")
    return arr


def precision_recall_per_class(cm: Sequence, classes: Sequence[str]) -> dict:
    """(a) Precision/recall PER CLASSE a livello finestra su confusion matrix.

    cm[i, j] = finestre con classe vera classes[i] e predetta classes[j].
    precision = TP/(TP+FP) = cm[i,i]/somma colonna i; recall = TP/(TP+FN) =
    cm[i,i]/somma riga i. Supporto 0 → 0.0 (mai NaN).
    """
    arr = _as_cm(cm, classes)
    out: dict[str, dict[str, float]] = {}
    for i, c in enumerate(classes):
        tp = arr[i, i]
        fp = arr[:, i].sum() - tp
        fn = arr[i, :].sum() - tp
        out[c] = {
            "precision": float(tp / (tp + fp)) if (tp + fp) > 0 else 0.0,
            "recall": float(tp / (tp + fn)) if (tp + fn) > 0 else 0.0,
        }
    return out


def _recalls(cm: Sequence, classes: Sequence[str]) -> dict[str, float]:
    return {c: s["recall"] for c, s in precision_recall_per_class(cm, classes).items()}


def _precisions(cm: Sequence, classes: Sequence[str]) -> dict[str, float]:
    return {c: s["precision"] for c, s in precision_recall_per_class(cm, classes).items()}


def macro_recall(cm: Sequence, classes: Sequence[str]) -> float:
    """(b) Macro-recall su TUTTE le classi passate (headline: 7 = healthy + 6 fault)."""
    rec = _recalls(cm, classes)
    return float(np.mean([rec[c] for c in classes]))


def macro_recall_faults(cm: Sequence, classes: Sequence[str],
                        healthy: str = HEALTHY) -> Optional[float]:
    """(b) Macro-recall sulle SOLE classi di fault (binding informativo D-ML-3b).

    None se classes non contiene nessuna classe di fault.
    """
    faults = [c for c in classes if c != healthy]
    if not faults:
        return None
    rec = _recalls(cm, classes)
    return float(np.mean([rec[c] for c in faults]))


def min_class_recall(cm: Sequence, classes: Sequence[str],
                     healthy: str = HEALTHY) -> dict:
    """(c) Recall min-classe, con specifica di quale delle due è PRIMARIA.

    PRIMARIA (headline AC-ML-3 "recall min-classe ≥ 0,60"): minimo su TUTTE le
    7 classi (healthy + 6 fault) — chiave `all`. RIPORTATA ANCHE la minima
    sulle SOLE 6 classi di fault — chiave `faults_only` (binding informativo
    D-ML-3b/F6). `faults_only` = None se nessuna classe di fault presente.
    """
    rec = _recalls(cm, classes)
    faults = [c for c in classes if c != healthy]
    return {
        "all": float(min(rec[c] for c in classes)),
        "faults_only": (float(min(rec[c] for c in faults)) if faults else None),
    }


def macro_precision(cm: Sequence, classes: Sequence[str]) -> float:
    """(d) Macro-precision (media delle precision per classe, AC-ML-3)."""
    prec = _precisions(cm, classes)
    return float(np.mean([prec[c] for c in classes]))


def valve_flags(machine_codes: Sequence[str], pred_labels: Sequence[str],
                theta: float) -> set:
    """(e) Aggregazione valve-level: valvola flaggata se ≥ θ% delle sue
    finestre è classificata non-healthy (θ in percentuale, [0, 100]).

    frazione = (# finestre con pred != healthy) / (# finestre della valvola);
    flag se frazione >= θ/100. Valvola senza finestre: mai flaggata.
    """
    if not 0.0 <= theta <= 100.0:
        raise ValueError(f"θ deve essere in [0, 100], ottenuto {theta!r}")
    counts: dict[str, list[int]] = {}
    for mc, lab in zip(machine_codes, pred_labels):
        entry = counts.setdefault(mc, [0, 0])
        entry[1] += 1
        if lab != HEALTHY:
            entry[0] += 1
    return {mc for mc, (nh, tot) in counts.items()
            if tot > 0 and nh / tot >= theta / 100.0}


def detection_delay(gt_labels: Sequence[str], pred_labels: Sequence[str],
                    onset_idx: int, fault_label: str, k: int = 2) -> Optional[int]:
    """(f) Ritardo di detection: finestre tra onset reale (GT) e prima finestra
    classificata correttamente in modo SOSTENUTO (k=2 consecutive, k parametro).

    La prima finestra "sostenuta" è w ≥ onset_idx tale che per k finestre
    consecutive w..w+k−1 valga gt == fault_label E pred == fault_label
    (classificazione corretta = coincide con la GT di ogni finestra del
    tratto). Ritardo = w − onset_idx (0 se corretta già alla finestra di
    onset). None se mai rilevata in modo sostenuto (o run finisce prima di k
    finestre). Validazioni: lunghezze uguali, onset_idx in range, k ≥ 1,
    gt[onset_idx] == fault_label.
    """
    gt = list(gt_labels)
    pred = list(pred_labels)
    n = len(gt)
    if len(pred) != n:
        raise ValueError("gt_labels e pred_labels devono avere la stessa "
                         "lunghezza")
    if not 0 <= onset_idx < n:
        raise ValueError(f"onset_idx fuori range: {onset_idx} (n={n})")
    if k < 1:
        raise ValueError(f"k deve essere >= 1, ottenuto {k!r}")
    if gt[onset_idx] != fault_label:
        raise ValueError("onset_idx deve puntare alla prima finestra con GT == "
                         f"fault_label ({fault_label!r}), trovata "
                         f"{gt[onset_idx]!r}")
    for w in range(onset_idx, n - k + 1):
        if all(gt[j] == fault_label and pred[j] == fault_label
               for j in range(w, w + k)):
            return w - onset_idx
    return None


def far_healthy(machine_codes: Sequence[str], pred_labels: Sequence[str],
                theta: float) -> dict:
    """(g) FAR su healthy: valvole sane flaggate dal modello (AC-ML-4c).

    Input: finestre di un run SOLO healthy. Ritorna flagged (set valvole
    flaggate con la regola valve-level θ%), n_valves e far = flagged/n_valves
    (0.0 se nessuna valvola).
    """
    flagged = valve_flags(machine_codes, pred_labels, theta)
    n = len({mc for mc in machine_codes})
    return {"flagged": flagged, "n_valves": n,
            "far": len(flagged) / n if n else 0.0}


def binary_confusion(gt_faulted: Mapping, flags: AbstractSet) -> dict:
    """(i) Raw TP/FP/FN/TN di un detector binario a livello (valvola, run).

    gt_faulted: {chiave: bool} (chiave = (machine_code, run_name));
    flags: set di chiavi flaggate. Dominio = unione delle chiavi dei due input
    (chiave non in gt_faulted → non faulted; non in flags → non flaggata).
    """
    keys = set(gt_faulted) | set(flags)
    tp = fp = fn = tn = 0
    for key in keys:
        faulted = bool(gt_faulted.get(key, False))
        flagged = key in flags
        if faulted and flagged:
            tp += 1
        elif not faulted and flagged:
            fp += 1
        elif faulted and not flagged:
            fn += 1
        else:
            tn += 1
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def miss_composition(fault_types_by_key: Mapping, flags: AbstractSet) -> dict:
    """(i) Composizione dei miss (FN) per tipo fault (AC-ML-4a).

    fault_types_by_key: {(machine_code, run_name): fault_type} per le valvole
    GUASTE; flags: chiavi flaggate dal detector. Miss = chiavi guaste non
    flaggate, contate per tipo fault (ordine alfabetico).
    """
    from collections import Counter
    misses = Counter(ft for key, ft in fault_types_by_key.items()
                     if key not in flags)
    return dict(sorted(misses.items()))


def recall_per_class(fault_types_by_key: Mapping, flags: AbstractSet) -> dict:
    """(j) recall_B0 per classe: per ogni fault type, recall su (valvola, run).

    recall_cls = (# chiavi di cls flaggate) / (# chiavi di cls). Classi senza
    alcuna (valvola, run) guasta: assenti dal risultato (non applicabili).
    """
    tot: dict[str, int] = {}
    hit: dict[str, int] = {}
    for key, ft in fault_types_by_key.items():
        tot[ft] = tot.get(ft, 0) + 1
        if key in flags:
            hit[ft] = hit.get(ft, 0) + 1
    return {ft: hit.get(ft, 0) / tot[ft] for ft in sorted(tot)}


def comparison_table(entries: Sequence, ml_flags: AbstractSet,
                     b0_flags: AbstractSet) -> pl.DataFrame:
    """(h) Tabella confronto per (valvola, run): ML vs baseline 3σ (B0).

    entries: sequenza di (machine_code, run_name, faulted_bool) — GT a livello
    (valvola, run). Riga per ogni entry, ordinata per (machine_code, run_name)
    (determinismo). Colonne: machine_code, run_name, faulted, ml_flag, b0_flag.
    """
    rows = []
    for mc, run, faulted in sorted(entries, key=lambda e: (e[0], e[1])):
        rows.append({
            "machine_code": mc, "run_name": run, "faulted": bool(faulted),
            "ml_flag": (mc, run) in ml_flags, "b0_flag": (mc, run) in b0_flags,
        })
    return pl.DataFrame(rows)


def ml_vs_baseline_comparison(entries: Sequence, ml_flags: AbstractSet,
                              b0_flags: AbstractSet,
                              precision_tol: float = 0.05) -> dict:
    """(h) Confronto ML vs B0 a disuguaglianze (D-ML-5, AC-ML-4a).

    Ritorna tabella (pl.DataFrame) + metriche aggregate binarie a livello
    (valvola, run): recall/precision di ML e B0, TP/FP/FN di entrambi e le
    DISUGUAGLIANZE `recall_ml_ge_b0` (recall_ML >= recall_B0) e
    `precision_ml_ge_b0_tol` (precision_ML >= precision_B0 − precision_tol,
    tol = 0,05 default). Metriche non definite (denominatore 0) → None e
    disuguaglianza False.
    """
    table = comparison_table(entries, ml_flags, b0_flags)
    gt = {(mc, run): faulted for mc, run, faulted in entries}
    conf_ml = binary_confusion(gt, ml_flags)
    conf_b0 = binary_confusion(gt, b0_flags)

    def _recall(conf: dict) -> Optional[float]:
        return (conf["tp"] / (conf["tp"] + conf["fn"])
                if conf["tp"] + conf["fn"] else None)

    def _precision(conf: dict) -> Optional[float]:
        return (conf["tp"] / (conf["tp"] + conf["fp"])
                if conf["tp"] + conf["fp"] else None)

    recall_ml, recall_b0 = _recall(conf_ml), _recall(conf_b0)
    precision_ml, precision_b0 = _precision(conf_ml), _precision(conf_b0)
    return {
        "table": table,
        "recall_ml": recall_ml, "recall_b0": recall_b0,
        "precision_ml": precision_ml, "precision_b0": precision_b0,
        "recall_ml_ge_b0": (recall_ml is not None and recall_b0 is not None
                            and recall_ml >= recall_b0),
        "precision_ml_ge_b0_tol": (precision_ml is not None
                                   and precision_b0 is not None
                                   and precision_ml >= precision_b0 - precision_tol),
        "tp_ml": conf_ml["tp"], "fp_ml": conf_ml["fp"], "fn_ml": conf_ml["fn"],
        "tp_b0": conf_b0["tp"], "fp_b0": conf_b0["fp"], "fn_b0": conf_b0["fn"],
    }
