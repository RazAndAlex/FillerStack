"""Layer ML — pipeline CLI end-to-end (D-W4, Track D).

CLI: `python -m plcsim.ml_pipeline <subcommand>`:

  validate --manifest <path>       validazione manifest (W2 + schema W1)
  build-features --manifest <path> estrazione feature + z-score + hashes
                                   + statistiche di normalizzazione nel manifest
  train --manifest <path>          fit logistic su train, metriche su validation
  eval --manifest <path>           UNA valutazione sul test (+ predizioni)
  report --manifest <path>         report confronto ML vs baseline 3σ (D-ML-5)

Contratto: `work/plan-ml-v2.md` (§1.2/§3.1/§4/§5.2/§7/§8/§14) +
`work/ml-feature-schema.md` (43 feature CONGELATE) + `work/ml_dataset/
manifest.schema.md` (schema manifest v0, NORMATIVO — D-W1). Riutilizza i
moduli D-W2 (`plcsim/ml_dataset.py`) e D-W3 (`plcsim/ml_model.py`,
`plcsim/ml_metrics.py`, `plcsim/ml_baseline.py`).

Convenzioni vincolanti (protocollo §5):
- I numeri di train/eval/report sono calcolati SOLO dai dati letti dallo
  script (niente copypaste nel report; determinismo AC-ML-2).
- hash dei parquet = hash del CONTENUTO del frame (frame_hash, D-W2),
  mai byte di file; determinismo = sort stabile su provenance
  (run_name, machine_code, window_idx).
- Subcomandi che richiedono dati non ancora generati (pre-marker, nessun
  run >60 s vietato): messaggio chiaro "dati assenti: atteso post-marker"
  ed exit 1 — mai un crash.
- θ è in PERCENTUALE [0,100] (convenzione D-W3); θ/k sono tarati su
  validation nel pilot D-W5 PRIMA del freeze: finché
  `work/ml-criteria-freeze.md` non esiste, θ di default = 50,0 marcato
  [PILOT — non congelato]. k = 2 (piano §5.2).
- NESSUN run di simulazione da questo modulo (layer read-only sui run).

Struttura output (tutto sotto work/ml_dataset/):
  features/{train,val,test}.parquet   una riga per (run, valvola, finestra)
  model/model.joblib (+ sidecar JSON)  modello serializzato
  model/zstats.json                    μ/σ per-valvola (dal manifest)
  model/classes_.json                  classi note al fit
  metrics_val.json / metrics_test.json metriche (script, niente copypaste)
  predictions_test.parquet             predizioni per-valvola per il confronto
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import polars as pl

from .ml_baseline import detect_faulted_valves
from .ml_dataset import (
    FEATURE_COLUMNS, NON_FEATURE_COLUMNS, RunInput, build_dataset,
    dump_manifest, fit_normalizer, frame_hash, normalizer_from_manifest,
    normalizer_to_manifest, transform_zscore, validate_manifest,
)
from .ml_metrics import (
    HEALTHY, detection_delay, far_healthy, macro_precision, macro_recall,
    macro_recall_faults, min_class_recall, miss_composition,
    ml_vs_baseline_comparison, precision_recall_per_class, recall_per_class,
    valve_flags,
)
from .ml_model import MLModel, LABELS

ROOT = Path(__file__).resolve().parent.parent
ML_DIR = ROOT / "work" / "ml_dataset"
FEATURES_DIR = ML_DIR / "features"
MODEL_DIR = ML_DIR / "model"
METRICS_VAL = ML_DIR / "metrics_val.json"
METRICS_TEST = ML_DIR / "metrics_test.json"
PREDICTIONS_TEST = ML_DIR / "predictions_test.parquet"
FREEZE_FILE = ROOT / "work" / "ml-criteria-freeze.md"
DEFAULT_MANIFEST = ML_DIR / "manifest.yaml"

# Valori [PILOT] (da congelare dopo D-W5, work/ml-calibration.md) — marcati
# esplicitamente nel report finché work/ml-criteria-freeze.md non esiste.
PILOT_THETA = 50.0          # % finestre non-healthy per flag valvola (piano §5.2)
PILOT_K = 2                 # finestre consecutive sostenute (piano §5.2, AC-ML-4b)
PILOT_DELAY_MAX = 5         # finestre: proposta iniziale ≤ 5 (AC-ML-4b)
PILOT_PRECISION_TOL = 0.05  # tolleranza precision ML ≥ B0 − tol (D-ML-5, AC-ML-4a)

# Criteri AC letti dal freeze (se esiste) o PILOT (marcatura NON CONGELATO).
# Formato atteso di work/ml-criteria-freeze.md (D-W5/manager): YAML con
# chiavi top-level theta, k, delay_max_windows, precision_tol,
# ac_ml_3: {macro_recall, min_class_recall, macro_precision},
# ac_ml_4c: {far_max}, ac_ml_6a: {target_s, alarm_pct, guardrail_x},
# ac_ml_6b: {gen_total_min, train_eval_min, guardrail_x}. Assenti → PILOT.
PILOT_CRITERIA = {
    "theta": PILOT_THETA,
    "k": PILOT_K,
    "delay_max_windows": PILOT_DELAY_MAX,
    "precision_tol": PILOT_PRECISION_TOL,
    "ac_ml_3": {"macro_recall": 0.75, "min_class_recall": 0.60,
                "macro_precision": 0.80},
    "ac_ml_4c": {"far_max": 0.0},
    "ac_ml_6a": {"target_s": 320.0, "alarm_pct": 0.20, "guardrail_x": 2.0},
    "ac_ml_6b": {"gen_total_min": 55.0, "train_eval_min": 10.0,
                 "guardrail_x": 2.0},
}


# ---------------------------------------------------------------------------
# Utility comuni
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: YAML di livello superiore deve essere una "
                         f"mappa")
    return data


def load_criteria() -> tuple[dict, bool]:
    """Criteri AC: dal freeze file se esiste (congelati), altrimenti PILOT.

    Ritorna (criteri, congelato: bool). I valori del freeze sovrascrivono i
    PILOT (merge superficiale su chiavi top-level + nested ac_ml_3/ac_ml_4c).
    """
    crit = json.loads(json.dumps(PILOT_CRITERIA))  # deep copy
    frozen = False
    if FREEZE_FILE.exists():
        data = _load_yaml(FREEZE_FILE)
        for k, v in data.items():
            if k in crit:
                if isinstance(v, dict) and isinstance(crit[k], dict):
                    crit[k].update({sk: sv for sk, sv in v.items()
                                    if sv is not None})
                elif v is not None:
                    crit[k] = v
        frozen = True
    return crit, frozen


def _resolve_out(run_info: dict) -> Path:
    """out_dir del manifest (relativo alla root repo, schema W1 §3) → Path."""
    out = run_info.get("out_dir")
    if out is None:
        return None
    p = Path(out)
    return p if p.is_absolute() else ROOT / p


def _missing_data(*paths: Path) -> list[str]:
    return [str(p) for p in paths if not Path(p).exists()]


def _abort_missing(what: str, missing: list[str]) -> int:
    print(f"dati assenti: atteso post-marker — {what} non ancora presenti:",
          file=sys.stderr)
    for m in missing:
        print(f"  - {m}", file=sys.stderr)
    return 1


def _validate_ok(manifest: Path) -> tuple[dict | None, list[str]]:
    """validate_manifest (W2) + controlli schema W1 (N==50, tail_policy)."""
    errors: list[str] = []
    info = None
    try:
        info = validate_manifest(manifest)
    except Exception as exc:  # validate_manifest solleva ValueError con messaggio
        errors.append(str(exc))
    try:
        data = _load_yaml(manifest)
    except Exception as exc:
        errors.append(f"manifest YAML non leggibile: {exc}")
        return None, errors
    if data.get("version") != 0:
        errors.append(f"schema W1: version deve essere 0, trovato "
                      f"{data.get('version')!r}")
    if data.get("N") != 50:
        errors.append(f"schema W1: N deve essere 50 (congelato D-ML-2), "
                      f"trovato {data.get('N')!r}")
    if data.get("tail_policy") != "drop":
        errors.append(f"schema W1: tail_policy deve essere 'drop' (politica "
                      f"coda run §3.2), trovato {data.get('tail_policy')!r}")
    if not data.get("feature_schema"):
        errors.append("schema W1: feature_schema mancante (riferimento "
                      "normativo ML-F1)")
    norm = data.get("normalization") or {}
    if norm.get("source_run") != "healthy_train":
        errors.append(f"schema W1: normalization.source_run deve essere "
                      f"'healthy_train', trovato {norm.get('source_run')!r}")
    if info is not None:
        if len(info["runs"]) != 9:
            errors.append(f"manifest reale: attesi 9 run (piano §3.1), "
                          f"trovati {len(info['runs'])}")
        if info["splits"]["baseline"] != ["healthy_baseline"]:
            errors.append(f"manifest: split baseline deve contenere solo "
                          f"healthy_baseline, trovato {info['splits']['baseline']}")
    return info, errors


def _confusion_metrics(y_true, y_pred, classes) -> dict:
    """Metriche per-classe/macro su (y_true, y_pred) via ml_metrics (D-W3)."""
    from sklearn.metrics import confusion_matrix
    classes = [str(c) for c in classes]
    cm = confusion_matrix(list(y_true), list(y_pred), labels=classes)
    per_class = precision_recall_per_class(cm, classes)
    return {
        "classes": classes,
        "confusion_matrix": [[int(v) for v in row] for row in cm],
        "per_class": per_class,
        "macro_recall": macro_recall(cm, classes),
        "macro_recall_faults": macro_recall_faults(cm, classes),
        "min_class_recall": min_class_recall(cm, classes),
        "macro_precision": macro_precision(cm, classes),
        "support": {c: int(sum(1 for t in y_true if t == c)) for c in classes},
        "n_windows": int(len(list(y_true))),
    }


def _fmt_metrics(m: dict) -> str:
    lines = [
        f"  macro_recall (7 classi)     = {m['macro_recall']:.4f}",
        f"  macro_recall fault (6)      = {m['macro_recall_faults']:.4f}",
        f"  min_class_recall (all)      = {m['min_class_recall']['all']:.4f}",
        f"  min_class_recall (faults)   = "
        f"{m['min_class_recall']['faults_only']:.4f}",
        f"  macro_precision             = {m['macro_precision']:.4f}",
        f"  n_windows                   = {m['n_windows']}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcomando: validate
# ---------------------------------------------------------------------------

def cmd_validate(args) -> int:
    manifest = Path(args.manifest)
    if not manifest.exists():
        print(f"ERRORE: manifest non trovato: {manifest}")
        return 1
    info, errors = _validate_ok(manifest)
    if errors:
        print(f"manifest NON valido ({len(errors)} errore/i):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"manifest valido: {manifest}")
    print(f"  N={info['n_cycles']} tail_policy=drop "
          f"version={_load_yaml(manifest).get('version')}")
    for split, names in info["splits"].items():
        print(f"  split {split:8s}: {', '.join(names)}")
    print("  seed pairwise distinti: OK (AC-ML-1c); split-by-run: OK "
          "(AC-ML-1b); unicità (scenario_file, seed): OK (V3)")
    return 0


# ---------------------------------------------------------------------------
# Subcomando: build-features
# ---------------------------------------------------------------------------

def _read_run_frames(info: dict, manifest_path: Path):
    """Legge i parquet dei 9 run dal manifest; fault_timeline facoltativo
    (AC-M4-9: assente nei run healthy). Ritorna (runs, hashes, n_cycles)."""
    runs: list[RunInput] = []
    hashes: dict[str, dict] = {}
    n_cycles: dict[str, int] = {}
    missing: list[tuple[str, str]] = []
    for name in info["runs"]:
        r = info["runs"][name]
        out = _resolve_out(r)
        if out is None:
            missing.append((name, "out_dir assente nel manifest"))
            continue
        files = {"valve_cycles": out / "valve_cycles.parquet",
                 "events": out / "events.parquet",
                 "ground_truth": out / "ground_truth.parquet"}
        absent = [k for k, p in files.items() if not p.exists()]
        if absent:
            for k in absent:
                missing.append((name, f"{k}.parquet mancante in {out}"))
            continue
        cycles = pl.read_parquet(files["valve_cycles"])
        events = pl.read_parquet(files["events"])
        gt = pl.read_parquet(files["ground_truth"])
        tl_path = out / "fault_timeline.parquet"
        timeline = pl.read_parquet(tl_path) if tl_path.exists() else None
        runs.append(RunInput(name=name, split=r["split"],
                             scenario_id=r["scenario_id"], cycles=cycles,
                             events=events, gt=gt, timeline=timeline))
        hashes[name] = {
            "valve_cycles": frame_hash(cycles),
            "events": frame_hash(events),
            "ground_truth": frame_hash(gt),
            "fault_timeline": (frame_hash(timeline) if timeline is not None
                               else None),
        }
        n_cycles[name] = int(cycles.height)
    return runs, hashes, n_cycles, missing


def cmd_build_features(args) -> int:
    manifest = Path(args.manifest)
    info, errors = _validate_ok(manifest)
    if errors:
        print(f"manifest NON valido — {len(errors)} errore/i:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    n = info["n_cycles"]
    runs, hashes, n_cycles, missing = _read_run_frames(info, manifest)
    if missing:
        print("build-features: dati dei run mancanti (atteso post-marker):",
              file=sys.stderr)
        for name, why in missing:
            print(f"  - {name}: {why}", file=sys.stderr)
        print("Nessuna modifica al manifest (nessuna scrittura parziale).",
              file=sys.stderr)
        return 1
    if "healthy_train" not in {r.name for r in runs}:
        print("build-features: run di normalizzazione 'healthy_train' assente",
              file=sys.stderr)
        return 1

    dataset = build_dataset(runs, n_cycles=n, zscore=False)
    fit = dataset.filter(pl.col("run_name") == "healthy_train")
    stats = fit_normalizer(fit)
    dataset = transform_zscore(dataset, stats)
    dataset = dataset.sort(["run_name", "machine_code", "window_idx"])
    dataset = dataset.select([*NON_FEATURE_COLUMNS, *FEATURE_COLUMNS])

    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    for split in ("train", "val", "test"):
        part = dataset.filter(pl.col("split") == split) \
                      .sort(["run_name", "machine_code", "window_idx"])
        part.write_parquet(FEATURES_DIR / f"{split}.parquet")

    feature_hash = frame_hash(dataset)
    print(f"feature estratti: {dataset.height:,} finestre "
          f"({FEATURES_DIR}/*.parquet)")
    print(f"feature_hash={feature_hash}   (AC-ML-2: riproducibile)")

    # popola hashes/n_cycles/statistiche di normalizzazione nel manifest
    data = _load_yaml(manifest)
    for entry in data["runs"]:
        name = entry["name"]
        entry["hashes"] = hashes[name]
        entry["n_cycles"] = n_cycles[name]
    data["normalization"] = {"source_run": "healthy_train",
                             "per_valve": normalizer_to_manifest(stats)}
    # scrittura CANONICA (T1): byte-stabile a parità di contenuto
    # (chiavi ordinate ricorsivamente; ordine liste preservato)
    dump_manifest(data, manifest)
    print(f"manifest aggiornato: {manifest} (hashes, n_cycles, "
          f"normalization.per_valve — {len(stats)} valvole)")
    return 0


# ---------------------------------------------------------------------------
# Subcomando: train
# ---------------------------------------------------------------------------

def _load_split_features(split: str) -> pl.DataFrame:
    return pl.read_parquet(FEATURES_DIR / f"{split}.parquet")


def cmd_train(args) -> int:
    manifest = Path(args.manifest)
    info, errors = _validate_ok(manifest)
    if errors:
        print(f"manifest NON valido — {len(errors)} errore/i:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    missing = _missing_data(FEATURES_DIR / "train.parquet",
                            FEATURES_DIR / "val.parquet")
    if missing:
        return _abort_missing("features train/val (build-features prima)", missing)

    import time
    t0 = time.time()
    train = _load_split_features("train")
    val = _load_split_features("val")
    X = train.select(FEATURE_COLUMNS).to_numpy()
    y = train["label"].to_list()
    model = MLModel(kind="logistic", random_state=42, max_iter=1000,
                    class_weight="balanced")
    model.fit(X, y)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model.save(MODEL_DIR / "model.joblib")

    # z-stats (μ/σ per-valvola congelate nel manifest) + classi note al fit
    data = _load_yaml(manifest)
    stats = normalizer_from_manifest(
        (data.get("normalization") or {}).get("per_valve") or {})
    (MODEL_DIR / "zstats.json").write_text(
        json.dumps(stats, sort_keys=True), encoding="utf-8")
    (MODEL_DIR / "classes_.json").write_text(
        json.dumps({"classes": [str(c) for c in model.classes_]},
                   sort_keys=True, indent=2), encoding="utf-8")

    y_val = val["label"].to_list()
    y_pred = [str(c) for c in model.predict(
        val.select(FEATURE_COLUMNS).to_numpy())]
    metrics = _confusion_metrics(y_val, y_pred, model.classes_)
    metrics["split"] = "val"
    metrics["elapsed_s"] = round(time.time() - t0, 3)
    METRICS_VAL.write_text(json.dumps(metrics, sort_keys=True, indent=2),
                           encoding="utf-8")
    print(f"modello salvato: {MODEL_DIR}/model.joblib (+ sidecar JSON)")
    print(f"metriche validation: {METRICS_VAL}")
    print(_fmt_metrics(metrics))
    return 0


# ---------------------------------------------------------------------------
# Subcomando: eval (UNA volta sul test)
# ---------------------------------------------------------------------------

def cmd_eval(args) -> int:
    manifest = Path(args.manifest)
    info, errors = _validate_ok(manifest)
    if errors:
        print(f"manifest NON valido — {len(errors)} errore/i:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    missing = _missing_data(FEATURES_DIR / "test.parquet",
                            MODEL_DIR / "model.joblib")
    if missing:
        return _abort_missing("features test o modello (train prima)", missing)

    import time
    t0 = time.time()
    test = _load_split_features("test")
    model = MLModel.load(MODEL_DIR / "model.joblib")
    y_test = test["label"].to_list()
    y_pred = [str(c) for c in model.predict(
        test.select(FEATURE_COLUMNS).to_numpy())]
    metrics = _confusion_metrics(y_test, y_pred, model.classes_)
    metrics["split"] = "test"
    metrics["elapsed_s"] = round(time.time() - t0, 3)
    METRICS_TEST.write_text(json.dumps(metrics, sort_keys=True, indent=2),
                            encoding="utf-8")

    preds = test.select(["run_name", "scenario_id", "machine_code",
                         "window_idx", "label", "in_ramp"]).with_columns(
        pl.Series("pred", y_pred))
    preds.write_parquet(PREDICTIONS_TEST)
    print(f"metriche test: {METRICS_TEST} (UNA valutazione, protocollo §1.3.6)")
    print(_fmt_metrics(metrics))
    print(f"predizioni per-valvola: {PREDICTIONS_TEST} "
          f"({preds.height:,} righe)")
    return 0


# ---------------------------------------------------------------------------
# Subcomando: report (confronto ML vs baseline 3σ, D-ML-5)
# ---------------------------------------------------------------------------

def _test_gt_faulted(info: dict) -> tuple[dict, dict]:
    """GT a livello (valvola, run) sui run di test dal fault_timeline.

    Ritorna (faulted: {(mc, run): fault_type}, onset: {(mc, run): (fault,
    start_cycle)}) — per il confronto e per il ritardo di detection.
    """
    faulted: dict = {}
    onset: dict = {}
    for name, r in info["runs"].items():
        if r["split"] != "test":
            continue
        out = _resolve_out(r)
        tl_path = out / "fault_timeline.parquet" if out else None
        if tl_path is None or not tl_path.exists():
            continue
        tl = pl.read_parquet(tl_path)
        for row in tl.iter_rows(named=True):
            mc = f"valve{row['valve_id']}"
            key = (mc, name)
            faulted[key] = str(row["fault_type"])
            onset[key] = (str(row["fault_type"]), int(row["start_cycle"]))
    return faulted, onset


def _ml_flags(preds: pl.DataFrame, theta: float) -> set:
    flags: set = set()
    for run_name in preds["run_name"].unique().to_list():
        part = preds.filter(pl.col("run_name") == run_name)
        flagged = valve_flags(part["machine_code"].to_list(),
                              part["pred"].to_list(), theta)
        flags |= {(mc, run_name) for mc in flagged}
    return flags


def _b0_flags(info: dict) -> tuple[set, set]:
    """Flags detector 3σ·√2/√n cross-seed (baseline healthy_baseline s202).

    Ritorna (mean_shift_flags, sigma_shift_flags) su (mc, run) per i run di
    test; il ramo σ-shift è SEPARATO (D-ML-5).
    """
    base_run = info["runs"].get("healthy_baseline")
    base_out = _resolve_out(base_run) if base_run else None
    if base_out is None or not (base_out / "valve_cycles.parquet").exists():
        raise FileNotFoundError(
            "report: run di riferimento healthy_baseline assente (atteso "
            "post-marker)")
    baseline = pl.read_parquet(base_out / "valve_cycles.parquet")
    mean_flags: set = set()
    sigma_flags: set = set()
    for name, r in info["runs"].items():
        if r["split"] != "test":
            continue
        out = _resolve_out(r)
        cyc = pl.read_parquet(out / "valve_cycles.parquet")
        res = detect_faulted_valves(cyc, baseline)
        mean_flags |= {(mc, name) for mc in res.flagged}
        sigma_flags |= {(mc, name) for mc in res.sigma_shift_flagged}
    return mean_flags, sigma_flags


def cmd_report(args) -> int:
    manifest = Path(args.manifest)
    info, errors = _validate_ok(manifest)
    if errors:
        print(f"manifest NON valido — {len(errors)} errore/i:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    required = [FEATURES_DIR / "test.parquet", PREDICTIONS_TEST,
                METRICS_TEST, MODEL_DIR / "model.joblib"]
    base_out = _resolve_out(info["runs"]["healthy_baseline"])
    required.append(base_out / "valve_cycles.parquet")
    for name, r in info["runs"].items():
        if r["split"] == "test":
            required.append(_resolve_out(r) / "valve_cycles.parquet")
            # fault_timeline: assente nei run healthy per costruzione
            # (AC-M4-9) — richiesto solo per i run con fault
            if name != "healthy_test":
                required.append(_resolve_out(r) / "fault_timeline.parquet")
    missing = _missing_data(*required)
    if missing:
        return _abort_missing("dati completi (build-features + train + eval "
                              "+ run di test/baseline)", missing)

    crit, frozen = load_criteria()
    theta = float(args.theta if args.theta is not None else crit["theta"])
    k = int(crit["k"])
    tol = float(crit["precision_tol"])
    marca = "CONGELATO (freeze)" if frozen else \
        "[PILOT — NON CONGELATO, attesa freeze D-W5]"

    test = _load_split_features("test")
    preds = pl.read_parquet(PREDICTIONS_TEST)
    joined = test.join(preds.select(["run_name", "scenario_id", "machine_code",
                                     "window_idx", "pred"]),
                       on=["run_name", "scenario_id", "machine_code",
                           "window_idx"], how="inner")
    y_true = joined["label"].to_list()
    y_pred = joined["pred"].to_list()

    print(f"== Report ML vs baseline 3σ (D-ML-5) — θ={theta:g}% k={k} "
          f"{marca} ==")
    print(f"fonte: {METRICS_TEST}, {PREDICTIONS_TEST}, run di test, "
          f"healthy_baseline s202 (ML-F6: confronto stesso seed del "
          f"detector)")

    # --- metriche per classe (solo da dati calcolati) ---
    metrics = _confusion_metrics(y_true, y_pred, LABELS)
    print(f"\nFinestra-level test (n={metrics['n_windows']}):")
    print(f"  macro_recall 7 classi     = {metrics['macro_recall']:.4f}")
    print(f"  macro_recall 6 fault      = {metrics['macro_recall_faults']:.4f}")
    print(f"  min_class_recall (all)    = "
          f"{metrics['min_class_recall']['all']:.4f}")
    print(f"  min_class_recall (faults) = "
          f"{metrics['min_class_recall']['faults_only']:.4f}")
    print(f"  macro_precision           = {metrics['macro_precision']:.4f}")
    for c in sorted(metrics["per_class"], key=lambda c: (c != HEALTHY, c)):
        p, r = metrics["per_class"][c]["precision"], metrics["per_class"][c]["recall"]
        print(f"    {c:22s} precision={p:.4f} recall={r:.4f} "
              f"support={metrics['support'][c]}")

    # --- livello (valvola, run): ML vs B0 ---
    ml_flags = _ml_flags(preds, theta)
    b0_flags, sigma_flags = _b0_flags(info)
    faulted, onset = _test_gt_faulted(info)
    all_keys = set(faulted) | set(ml_flags) | set(b0_flags) | set(sigma_flags)
    entries = [(mc, run, (mc, run) in faulted)
               for mc, run in sorted(all_keys)]
    comp = ml_vs_baseline_comparison(entries, ml_flags, b0_flags,
                                     precision_tol=tol)
    print(f"\nConfronto (valvola, run) su test ({len(entries)} valvole×run):")
    print(f"  recall_ML={comp['recall_ml']:.4f} ≥ recall_B0="
          f"{comp['recall_b0']:.4f} → "
          f"{'OK' if comp['recall_ml_ge_b0'] else 'NON OK'}")
    print(f"  precision_ML={comp['precision_ml']:.4f} ≥ precision_B0−"
          f"{tol:g}={comp['precision_b0'] - tol:.4f} → "
          f"{'OK' if comp['precision_ml_ge_b0_tol'] else 'NON OK'}")
    print(f"  raw ML: TP={comp['tp_ml']} FP={comp['fp_ml']} "
          f"FN={comp['fn_ml']} | raw B0: TP={comp['tp_b0']} "
          f"FP={comp['fp_b0']} FN={comp['fn_b0']}")
    print(f"  miss (FN) B0 per tipo fault: "
          f"{miss_composition(faulted, b0_flags) or 'nessuno'}")
    print(f"  recall_B0 per classe: "
          f"{recall_per_class(faulted, b0_flags) or 'n/a'}")
    print(f"  miss (FN) ML per tipo fault: "
          f"{miss_composition(faulted, ml_flags) or 'nessuno'}")
    print(f"  recall_ML per classe: "
          f"{recall_per_class(faulted, ml_flags) or 'n/a'}")

    # --- ramo σ-shift SEPARATO (D-ML-5) ---
    sigma_fp = sorted(sigma_flags - set(faulted))
    sigma_tp = sorted(sigma_flags & set(faulted))
    print(f"\nRamo σ-shift (pressure_instability, criterio M3) — SEPARATO:")
    print(f"  flaggate {len(sigma_flags)} valvole×run; "
          f"TP={len(sigma_tp)} FP={len(sigma_fp)} "
          f"(FP: {sigma_fp or 'nessuno'})")

    # --- FAR healthy (AC-ML-4c) ---
    h_preds = preds.filter(pl.col("run_name") == "healthy_test")
    ml_far = far_healthy(h_preds["machine_code"].to_list(),
                         h_preds["pred"].to_list(), theta)
    h_cyc = pl.read_parquet(_resolve_out(info["runs"]["healthy_test"])
                            / "valve_cycles.parquet")
    b_cyc = pl.read_parquet(base_out / "valve_cycles.parquet")
    b0_healthy = detect_faulted_valves(h_cyc, b_cyc)
    b0_far_valves = len(b0_healthy.flagged)
    print(f"\nFAR healthy_test (AC-ML-4c, θ={theta:g}%):")
    print(f"  ML: {len(ml_far['flagged'])}/{ml_far['n_valves']} valvole "
          f"flaggate (far={ml_far['far']:.3f}) {marca}")
    print(f"  B0: {b0_far_valves}/{len(set(h_cyc['machine_code']))} valvole "
          f"flaggate")

    # --- valve8/20: NOTA, mai FAIL (ML-F4) ---
    v820_ml = sorted(mc for mc, _ in ml_flags if mc in ("valve8", "valve20"))
    v820_b0 = sorted(mc for mc, _ in b0_flags if mc in ("valve8", "valve20"))
    print(f"\nvalve8/20 (ML-F4 — controlli sani-anomali, mai portatrici):")
    if v820_ml:
        print(f"  NOTA: ML flagga {v820_ml} → segnale onesto di "
              f"calibrazione da RIPORTARE (non FAIL automatico)")
    else:
        print(f"  ML: valve8/20 non flaggate (OK)")
    if v820_b0:
        print(f"  NOTA (B0): detector 3σ flagga {v820_b0}")

    # --- ritardo di detection (AC-ML-4b, informativo nel report) ---
    delays = []
    for (mc, run), (fault, start) in sorted(onset.items()):
        part = joined.filter((pl.col("machine_code") == mc)
                             & (pl.col("run_name") == run)) \
                     .sort("window_idx")
        gt_w = part["label"].to_list()
        pr_w = part["pred"].to_list()
        onset_idx = (start - 1) // info["n_cycles"]
        if onset_idx < len(gt_w) and gt_w[onset_idx] == fault:
            d = detection_delay(gt_w, pr_w, onset_idx, fault, k=k)
            delays.append((mc, run, fault, d))
    med = sorted(d for _, _, _, d in delays if d is not None)
    med_val = med[len(med) // 2] if med else None
    print(f"\nRitardo di detection (k={k} sostenute, AC-ML-4b):")
    for mc, run, fault, d in sorted(delays):
        print(f"  {mc:8s} {run:16s} {fault:20s} delay={d if d is not None else 'mai'}")
    print(f"  mediana={med_val if med_val is not None else 'n/a'} finestre "
          f"(proposta PILOT ≤ {crit['delay_max_windows']}) {marca}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m plcsim.ml_pipeline",
        description="Pipeline ML (Track D): validate | build-features | "
                    "train | eval | report")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="valida il manifest (W2 + W1)")
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("build-features",
                       help="estrazione feature + z-score + aggiorna manifest")
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.set_defaults(func=cmd_build_features)

    p = sub.add_parser("train", help="fit logistic su train + metriche val")
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.set_defaults(func=cmd_train)

    p = sub.add_parser("eval", help="UNA valutazione sul test")
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("report", help="report confronto ML vs baseline 3σ")
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.add_argument("--theta", type=float, default=None,
                   help="θ %% finestre non-healthy per flag valvola "
                        "(default: freeze o [PILOT] 50)")
    p.set_defaults(func=cmd_report)
    return ap


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # errore esplicito, mai crash silenzioso
        print(f"ERRORE: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
