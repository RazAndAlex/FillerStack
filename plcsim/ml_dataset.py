"""Layer ML (D-W2) — estrazione feature, label join, split, normalizzazione.

Contratto normativo: `work/ml-feature-schema.md` (CONGELATO, ML-F1) —
whitelist CHIUSA di ESATTAMENTE 43 feature (§2), colonne provenance/label/
in_ramp (§3), whitelist eventi (§4), politica finestre N=50 con DROP della
coda (§5), z-score per-valvola da healthy_train (§6). Nessuna colonna di
`ground_truth`/`fault_timeline` entra nella matrice feature; nessuna
chiave/posizione come feature.

Unità del dataset: una riga per (run, valvola, finestra di N cicli).

API pubblica (componibile, tutta su frame polars in-memory):
  window_cycles(cycles, n)            → finestre piene per valvola (+window_idx)
  compute_window_features(cycles, events, n) → 43 feature + last_cycle_id
  join_labels(features, gt)           → label (GT dell'ultimo ciclo; pre-onset
                                        → "healthy")
  flag_in_ramp(features, timeline, n) → in_ramp da fault_timeline
  fit_normalizer / transform_zscore / normalizer_to_manifest /
  normalizer_from_manifest            → z-score per-valvola (§6)
  dump_manifest(data, path)           → scrittura manifest CANONICA
                                        (byte-stabile, T1)
  frame_hash(df, columns)             → sha256 sul CONTENUTO del frame
  build_dataset(runs, ...)            → dataset completo (provenance + label +
                                        in_ramp + feature), sort deterministico
  validate_manifest(path)             → validazione manifest v0 (riusabile da
                                        D-W4); struttura attesa sotto.

Struttura manifest v0 ATTESA (schema v0 OWNED D-W1, `work/ml_dataset/
manifest.schema.md` — normativo dal contratto 2026-08-11; vedere
validate_manifest):

    version: 0             # schema W1 (D-W1): in alternativa a schema_version
    N: 50                  # schema W1: in alternativa a window.n_cycles
    runs:
      - name: healthy_train   # lista di {name: ...}
        scenario_file: scenarios/m4_healthy.yaml   # relativo alla radice repo
        scenario_id: 41      # DEVE combaciare con lo scenario YAML
        seed: 101            # seed effettivo del run (per m4_healthy il
                             #   seed YAML è null → il seed manifest è quello
                             #   effettivo; se il YAML dichiara un seed, deve
                             #   combaciare con il manifest)
        split: train         # train | val | test | baseline
        out_dir: ...         # opzionale (provenienza D-W4)

DEVIAZIONE DI COORDINAMENTO D-W4 (documentata in work/ml-w4-pipeline.md):
validate_manifest accetta ENTRAMBE le forme — chiave `scenario_file`
(schema W1, `work/ml_dataset/manifest.schema.md`) o `scenario` (forma
assunta da questo modulo quando lo schema W1 non esisteva ancora), e
`N` top-level (W1) o `window.n_cycles` / `n_cycles`. Il manifest reale
(D-W4) usa la forma W1.

Normalizzazione: μ/σ per (valvola, feature numerica) stimate SOLO sui cicli
del run healthy_train (manifest); guardia σ=0 → z=0; statistiche esportabili
con normalizer_to_manifest (congelate nel manifest da D-W4) e applicate
immutate a val/test.

Eventi: la whitelist (§4) è applicata per TIPO prima di ogni aggregazione;
solo LATE_PULSE alimenta feature in v0 (late_pulse_count/rate).
"""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from .plc import STATE_NAMES as _PLC_STATE_NAMES  # stati valvola (whitelist §4)
from .scenario import load_scenario

# --------------------------------------------------------------------------
# Costanti congelate (work/ml-feature-schema.md)
# --------------------------------------------------------------------------

N_CYCLES = 50                      # finestra (§5, fissata al freeze del manifest)

# KPI Int64 di origine (telemetry.py:21-27) — statistiche §2.1
KPI_COLUMNS = ("fillingtime", "tailtime", "tailpulse", "pulsecount",
               "deltapulse", "filling_step_out")
_STATS = ("mean", "std", "min", "max", "slope")

# Rate su finestra §2.2 (colonne Boolean di valve_cycles)
FLAG_COLUMNS = ("fillingok", "fill_quality_ok", "sequence_ok", "sample_valid",
                "position_limit", "filling_overtime")
DIAGNOSTIC_SUSPECT = "SUSPECT"

# Rate close_reason §2.3 — whitelist CHIUSA (nessun bucket "other")
CLOSE_REASON_WHITELIST = ("target", "encoder_limit", "safety_timeout",
                          "tail_timeout")

# Whitelist eventi §4 — tipi ammessi come fonte feature (filtrati per tipo
# PRIMA dell'aggregazione; FAULT_START/FAULT_RAMP/CMD:* e qualsiasi altro
# tipo NON whitelisted vengono scartati per costruzione).
_EVENT_ALLOWED_TYPES = frozenset({"LATE_PULSE"})                 # sensors.py:163
_EVENT_ALLOWED_VALVE_STATES = frozenset(_PLC_STATE_NAMES.values())  # plc.py:55-58
_EVENT_ALLOWED_PREFIX = "STATE:"          # stato macchina (telemetry.on_machine)

# Le 43 feature ESATTE nello stesso ordine dello schema congelato (§2)
FEATURE_COLUMNS = (
    tuple(f"{s}_{k}" for k in KPI_COLUMNS for s in _STATS)
    + tuple(f"{f}_rate" for f in FLAG_COLUMNS)
    + ("diagnostic_suspect_rate",)
    + tuple(f"close_reason_{r}_rate" for r in CLOSE_REASON_WHITELIST)
    + ("late_pulse_count", "late_pulse_rate")
)
assert len(FEATURE_COLUMNS) == 43, "schema congelato: ESATTAMENTE 43 feature"

# Colonne non-feature (§3) — mai feature, mai fit
PROVENANCE_COLUMNS = ("run_name", "scenario_id", "machine_code", "window_idx",
                      "split")
NON_FEATURE_COLUMNS = PROVENANCE_COLUMNS + ("label", "in_ramp")

SPLITS = ("train", "val", "test", "baseline")

_FEATURE_DTYPE = {c: (pl.Int64 if c == "late_pulse_count" else pl.Float64)
                  for c in FEATURE_COLUMNS}


# --------------------------------------------------------------------------
# Windowing (§5): N=50 cicli consecutivi per valvola, DROP coda parziale,
# window_idx = floor((cycle_id − 1) / N)
# --------------------------------------------------------------------------

def window_cycles(cycles: pl.DataFrame, n: int = N_CYCLES) -> pl.DataFrame:
    """Assegna window_idx ai cicli e scarta le finestre parziali (< n cicli).

    Input: frame valve_cycles con `machine_code` e `cycle_id` (1-based).
    Output: sole righe delle finestre PIENE, con colonna `window_idx`
    (Int64) = floor((cycle_id − 1) / n), ordinate per (machine_code, cycle_id).
    """
    for col in ("machine_code", "cycle_id"):
        if col not in cycles.columns:
            raise ValueError(f"valve_cycles: colonna mancante: {col!r}")
    if cycles.height == 0:
        return cycles.with_columns(
            pl.lit(0).cast(pl.Int64).alias("window_idx")).slice(0, 0)
    dup = (cycles.group_by(["machine_code", "cycle_id"]).len()
           .filter(pl.col("len") > 1))
    if dup.height:
        raise ValueError("valve_cycles: (machine_code, cycle_id) duplicati — "
                         f"{dup.height} chiavi doppie")
    df = cycles.with_columns(
        ((pl.col("cycle_id") - 1) // n).alias("window_idx"))
    full = (df.group_by(["machine_code", "window_idx"]).len()
            .filter(pl.col("len") == n)
            .select(["machine_code", "window_idx"]))
    df = df.join(full, on=["machine_code", "window_idx"])
    return df.sort(["machine_code", "cycle_id"])


def empty_features_frame() -> pl.DataFrame:
    """Frame feature VUOTO con lo schema esatto (utile a test e path vuoti)."""
    return pl.DataFrame(schema={
        "machine_code": pl.String, "window_idx": pl.Int64,
        "last_cycle_id": pl.Int64, **_FEATURE_DTYPE})


# --------------------------------------------------------------------------
# Feature extractor (§2): ESATTAMENTE 43 colonne, nomi e dtypes dello schema
# --------------------------------------------------------------------------

def _slope_expr(kpi: str) -> pl.Expr:
    """Pendenza ai minimi quadrati: x = 0..n−1 (ordine di cycle_id),
    y = valore del KPI; slope = Σ((x−x̄)(y−ȳ)) / Σ((x−x̄)²).
    In forma chiusa: Σx = n(n−1)/2, Σ(x−x̄)² = n(n²−1)/12."""
    n = pl.len().cast(pl.Float64)
    xbar = (n - 1) / 2
    var_x = n * (n * n - 1) / 12
    sum_xy = pl.col(kpi).dot(pl.int_range(pl.len()).cast(pl.Float64))
    slope = (sum_xy - n * xbar * pl.col(kpi).mean()) / var_x
    return (pl.when(pl.len() >= 2).then(slope).otherwise(None)
            .alias(f"slope_{kpi}"))


def _whitelist_filter(events: pl.DataFrame) -> pl.DataFrame:
    """Filtra gli eventi per TIPO (whitelist §4) PRIMA di ogni aggregazione.

    Ammessi: LATE_PULSE, transizioni di stato valvola del PLC (plc.py
    STATE_NAMES), STATE:* della macchina. Tutto il resto (FAULT_START,
    FAULT_RAMP, CMD:OPEN, CMD:CLOSE, tipi sconosciuti) è scartato qui.
    """
    for col in ("machine_code", "event", "cycle_id"):
        if col not in events.columns:
            raise ValueError(f"events: colonna mancante: {col!r}")
    ev = pl.col("event")
    mask = (
        ev.is_in(sorted(_EVENT_ALLOWED_TYPES | _EVENT_ALLOWED_VALVE_STATES))
        | ev.str.starts_with(_EVENT_ALLOWED_PREFIX)
    )
    return events.filter(mask)


def _late_pulse_features(features: pl.DataFrame, events: pl.DataFrame | None,
                         n: int) -> pl.DataFrame:
    """Colonne §2.4: late_pulse_count (Int64) e late_pulse_rate da LATE_PULSE
    con cycle_id nella finestra, dopo il filtro whitelist per tipo."""
    if events is None:
        return features.with_columns([
            pl.lit(0).cast(pl.Int64).alias("late_pulse_count"),
            pl.lit(0.0).alias("late_pulse_rate"),
        ])
    allowed = _whitelist_filter(events)
    late = (allowed.filter(pl.col("event") == "LATE_PULSE")
            .select(["machine_code", "cycle_id"]))
    wins = features.select(["machine_code", "window_idx"]).with_columns([
        (pl.col("window_idx") * n + 1).alias("lo"),
        ((pl.col("window_idx") + 1) * n).alias("hi"),
    ])
    cnt = (late.join(wins, on="machine_code", how="inner")
           .filter(pl.col("cycle_id").is_between(pl.col("lo"), pl.col("hi")))
           .group_by(["machine_code", "window_idx"]).len()
           .rename({"len": "late_pulse_count"}))
    out = features.join(cnt, on=["machine_code", "window_idx"], how="left")
    out = out.with_columns(
        pl.col("late_pulse_count").fill_null(0).cast(pl.Int64))
    return out.with_columns(
        (pl.col("late_pulse_count").cast(pl.Float64) / n)
        .alias("late_pulse_rate"))


def compute_window_features(cycles: pl.DataFrame,
                            events: pl.DataFrame | None = None,
                            n: int = N_CYCLES) -> pl.DataFrame:
    """43 feature per (machine_code, window_idx) dal frame valve_cycles.

    Output: [machine_code, window_idx, last_cycle_id] + ESATTAMENTE le 43
    colonne FEATURE_COLUMNS (ordine dello schema; late_pulse_count Int64,
    tutte le altre Float64). `last_cycle_id` è la chiave interna per il
    label join (§5) e NON compare nell'output dataset finale.
    """
    need = ("machine_code", "cycle_id", *KPI_COLUMNS, *FLAG_COLUMNS,
            "diagnostic_status", "close_reason")
    missing = [c for c in need if c not in cycles.columns]
    if missing:
        raise ValueError(f"valve_cycles: colonne mancanti: {missing}")
    w = window_cycles(cycles, n)
    if w.height == 0:
        return empty_features_frame()
    w = w.sort(["machine_code", "window_idx", "cycle_id"])
    # KPI Int64 → Float64: min/max/std/mean/slope restano Float64 (schema §2.1)
    w = w.with_columns([pl.col(k).cast(pl.Float64) for k in KPI_COLUMNS])
    agg = w.group_by(["machine_code", "window_idx"]).agg([
        *[pl.col(k).mean().alias(f"mean_{k}") for k in KPI_COLUMNS],
        *[pl.col(k).std().alias(f"std_{k}") for k in KPI_COLUMNS],
        *[pl.col(k).min().alias(f"min_{k}") for k in KPI_COLUMNS],
        *[pl.col(k).max().alias(f"max_{k}") for k in KPI_COLUMNS],
        *[_slope_expr(k) for k in KPI_COLUMNS],
        *[pl.col(f).mean().alias(f"{f}_rate") for f in FLAG_COLUMNS],
        (pl.col("diagnostic_status") == DIAGNOSTIC_SUSPECT).mean()
        .alias("diagnostic_suspect_rate"),
        *[(pl.col("close_reason") == r).mean()
          .alias(f"close_reason_{r}_rate") for r in CLOSE_REASON_WHITELIST],
        pl.col("cycle_id").max().alias("last_cycle_id"),
    ]).sort(["machine_code", "window_idx"])
    out = _late_pulse_features(agg, events, n)
    return out.select(["machine_code", "window_idx", "last_cycle_id",
                       *FEATURE_COLUMNS])


# --------------------------------------------------------------------------
# Label join (§5): fault_type della GT dell'ULTIMO ciclo della finestra;
# cicli pre-onset (fault_type null) → "healthy". GT (machine_code, cycle_id)
# deve essere 1:1 e completa per l'ultimo ciclo di ogni finestra.
# --------------------------------------------------------------------------

def join_labels(features: pl.DataFrame, gt: pl.DataFrame) -> pl.DataFrame:
    """Aggiunge `label` (String): GT dell'ultimo ciclo della finestra,
    fault_type null → "healthy". Nessuna altra colonna GT esce dal join."""
    for col in ("machine_code", "last_cycle_id"):
        if col not in features.columns:
            raise ValueError(f"features: colonna mancante: {col!r}")
    for col in ("machine_code", "cycle_id", "fault_type"):
        if col not in gt.columns:
            raise ValueError(f"ground_truth: colonna mancante: {col!r}")
    dup = gt.group_by(["machine_code", "cycle_id"]).len() \
            .filter(pl.col("len") > 1)
    if dup.height:
        raise ValueError("ground_truth: (machine_code, cycle_id) duplicati — "
                         "il label join richiede GT 1:1")
    keys = (gt.select(["machine_code", "cycle_id"]).unique()
            .with_columns(pl.lit(True).alias("_gt_present")))
    joined = features.join(keys,
                           left_on=["machine_code", "last_cycle_id"],
                           right_on=["machine_code", "cycle_id"], how="left")
    missing = joined.filter(pl.col("_gt_present").is_null())
    if missing.height:
        raise ValueError(
            f"ground_truth: riga mancante per l'ultimo ciclo di "
            f"{missing.height} finestre — join label incompleto")
    gt_lbl = gt.select(["machine_code", "cycle_id", "fault_type"]) \
               .unique(subset=["machine_code", "cycle_id"])
    out = (joined.drop("_gt_present")
           .join(gt_lbl, left_on=["machine_code", "last_cycle_id"],
                 right_on=["machine_code", "cycle_id"], how="left")
           .with_columns(pl.col("fault_type").fill_null("healthy")
                         .alias("label"))
           .drop("fault_type"))
    return out


# --------------------------------------------------------------------------
# in_ramp (§5): da fault_timeline (start_cycle/ramp_cycles) — TRUE se la
# finestra contiene cicli in rampa (start_cycle ≤ c < start_cycle+ramp_cycles)
# OPPURE cavalca l'onset (cicli < start_cycle e cicli ≥ start_cycle).
# Solo per report informativo steady-state: MAI feature, MAI nel test
# bit-identità GT-permutation.
# --------------------------------------------------------------------------

def flag_in_ramp(features: pl.DataFrame,
                 timeline: pl.DataFrame | None = None,
                 n: int = N_CYCLES) -> pl.DataFrame:
    """Aggiunge `in_ramp` (Boolean). `timeline` è il fault_timeline del
    run (una riga per fault×valvola affetta); None/vuoto → tutto False."""
    if "in_ramp" in features.columns:
        features = features.drop("in_ramp")
    if timeline is None or timeline.height == 0:
        return features.with_columns(pl.lit(False).alias("in_ramp"))
    for col in ("valve_id", "start_cycle"):
        if col not in timeline.columns:
            raise ValueError(f"fault_timeline: colonna mancante: {col!r}")
    tl = (timeline.select(["valve_id", "start_cycle", "ramp_cycles"])
          .with_columns((pl.lit("valve")
                         + pl.col("valve_id").cast(pl.String))
                        .alias("machine_code"))
          .drop("valve_id"))
    wins = features.select(["machine_code", "window_idx"]).unique() \
                   .with_columns([
                       (pl.col("window_idx") * n + 1).alias("lo"),
                       ((pl.col("window_idx") + 1) * n).alias("hi"),
                   ])
    pairs = wins.join(tl, on="machine_code", how="inner")
    in_rampa = (pl.col("ramp_cycles").is_not_null()
                & (pl.col("lo") <= pl.col("start_cycle")
                   + pl.col("ramp_cycles") - 1)
                & (pl.col("start_cycle") <= pl.col("hi")))
    cavalca_onset = (pl.col("lo") < pl.col("start_cycle")) \
        & (pl.col("start_cycle") <= pl.col("hi"))
    flagged = (pairs.filter(in_rampa | cavalca_onset)
               .select(["machine_code", "window_idx"]).unique()
               .with_columns(pl.lit(True).alias("in_ramp")))
    return (features.join(flagged, on=["machine_code", "window_idx"],
                          how="left")
            .with_columns(pl.col("in_ramp").fill_null(False)
                          .cast(pl.Boolean)))


# --------------------------------------------------------------------------
# Normalizzazione (§6): z-score per-valvola, per feature numerica, con μ/σ
# stimati SOLO dal run healthy_train; σ=0 → z=0; fit e transform separati
# (le statistiche si congelano nel manifest — normalizer_to_manifest).
# --------------------------------------------------------------------------

def fit_normalizer(features: pl.DataFrame,
                   feature_columns=None) -> dict[str, dict[str, tuple]]:
    """μ/σ per (machine_code, feature) sulle feature del frame (healthy_train).

    Ritorna {machine_code: {feature: (mu, sigma)}} — mu/sigma Python float o
    None (feature completamente null nella valvola → guardia σ=0 a transform).
    """
    feature_columns = tuple(feature_columns or FEATURE_COLUMNS)
    if "machine_code" not in features.columns:
        raise ValueError("fit_normalizer: colonna 'machine_code' mancante")
    if features.height == 0:
        return {}
    stats_df = features.group_by("machine_code").agg([
        pl.col(c).mean().alias(f"{c}__mean")
        for c in feature_columns] + [
        pl.col(c).std().alias(f"{c}__std")
        for c in feature_columns])
    stats: dict[str, dict[str, tuple]] = {}
    for row in stats_df.iter_rows(named=True):
        mc = row["machine_code"]
        stats[mc] = {c: (row[f"{c}__mean"], row[f"{c}__std"])
                     for c in feature_columns}
    return stats


def transform_zscore(features: pl.DataFrame, stats: dict,
                     feature_columns=None) -> pl.DataFrame:
    """Applica le statistiche di fit (healthy_train) a ogni (valvola, feature).

    Guardia σ=0 (o statistiche assenti/null) → z=0 (nessun NaN/Inf).
    Le valvole senza statistiche → errore esplicito (fit mancante, mai
    refit implicito su val/test)."""
    feature_columns = tuple(feature_columns or FEATURE_COLUMNS)
    mcs = sorted(set(features["machine_code"].to_list()))
    missing = [mc for mc in mcs if mc not in stats]
    if missing:
        raise ValueError(
            f"z-score: valvole senza statistiche di normalizzazione "
            f"(fit healthy_train mancante): {missing}")
    parts = []
    for mc in mcs:
        grp = features.filter(pl.col("machine_code") == mc)
        for c in feature_columns:
            mu, sigma = stats[mc].get(c, (None, None))
            if mu is None or sigma is None or sigma == 0.0:
                grp = grp.with_columns(pl.lit(0.0).alias(c))
            else:
                grp = grp.with_columns(((pl.col(c) - mu) / sigma).alias(c))
        parts.append(grp)
    return pl.concat(parts)


def normalizer_to_manifest(stats: dict) -> dict:
    """Statistiche in forma JSON-serializzabile per il manifest (§7).

    Le chiavi machine_code sono emesse in ordine ORDINATO (deterministico,
    T1: difesa in profondità accanto a dump_manifest); normalizer_from_manifest
    è insensibile all'ordine → nessuna rottura backward.
    """
    return {mc: {c: [mu, sigma] for c, (mu, sigma) in s.items()}
            for mc, s in sorted(stats.items())}


def normalizer_from_manifest(data: dict) -> dict:
    """Ricarica le statistiche dal manifest (stessa forma di fit_normalizer)."""
    return {mc: {c: tuple(v) for c, v in s.items()} for mc, s in data.items()}


# --------------------------------------------------------------------------
# Determinismo (AC-ML-2): hash sul CONTENUTO del frame (righe materializzate
# ordinate), mai su byte di file. Sort stabile su (run_name, machine_code,
# window_idx) prima della scrittura.
# --------------------------------------------------------------------------

def frame_hash(df: pl.DataFrame, columns=None) -> str:
    """sha256 del contenuto del frame: righe ordinate per TUTTE le colonne
    selezionate e serializzate in CSV (deterministico a parità di valori e
    versione polars). `columns` limita l'hash a un sottoinsieme."""
    frame = df.select(columns) if columns is not None else df
    frame = frame.sort(frame.columns)
    return hashlib.sha256(frame.write_csv().encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Orchestratore: dataset completo da run in-memory (D-W4 legge i parquet dal
# manifest e costruisce i RunInput). Split-by-run: ogni run ha UN solo split
# (assert esplicito); nessun run in due split.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RunInput:
    """Input di un run: frame polars in-memory (mai run di simulazione qui)."""
    name: str
    split: str
    scenario_id: int
    cycles: pl.DataFrame
    events: pl.DataFrame | None = None
    gt: pl.DataFrame | None = None
    timeline: pl.DataFrame | None = None


def build_dataset(runs, n_cycles: int = N_CYCLES, zscore: bool = True,
                  fit_run: str = "healthy_train") -> pl.DataFrame:
    """Dataset completo: provenance + label + in_ramp + 43 feature.

    - per run: compute_window_features → join_labels → flag_in_ramp, poi
      provenance (run_name, scenario_id, split);
    - z-score: fit sul run `fit_run` (healthy_train) e transform su TUTTI i
      run (val/test mai refit);
    - determinismo: sort stabile su (run_name, machine_code, window_idx);
    - split-by-run: un run compare in un solo split (errori espliciti).
    """
    runs = list(runs)
    names = [r.name for r in runs]
    if len(names) != len(set(names)):
        raise ValueError("build_dataset: nomi run duplicati")
    if len(set((r.name, r.split) for r in runs)) != len(names):
        raise ValueError("build_dataset: un run non può stare in 2 split")
    frames = []
    for r in runs:
        if r.split not in SPLITS:
            raise ValueError(f"build_dataset: split sconosciuto {r.split!r} "
                             f"per run {r.name!r} (attesi {SPLITS})")
        if r.gt is None:
            raise ValueError(f"build_dataset: run {r.name!r}: ground_truth "
                             "richiesta per il label join")
        feats = compute_window_features(r.cycles, r.events, n=n_cycles)
        feats = join_labels(feats, r.gt)
        feats = flag_in_ramp(feats, r.timeline, n=n_cycles)
        feats = feats.with_columns([
            pl.lit(r.name).alias("run_name"),
            pl.lit(r.scenario_id).cast(pl.Int64).alias("scenario_id"),
            pl.lit(r.split).alias("split"),
        ])
        frames.append(feats)
    out = pl.concat(frames)
    if zscore:
        fit = out.filter(pl.col("run_name") == fit_run)
        if fit.height == 0:
            raise ValueError(f"build_dataset: run di fit {fit_run!r} assente "
                             "(zscore=True richiede il run healthy_train)")
        stats = fit_normalizer(fit)
        out = transform_zscore(out, stats)
    out = out.sort(["run_name", "machine_code", "window_idx"])
    return out.select([*NON_FEATURE_COLUMNS, *FEATURE_COLUMNS])


# --------------------------------------------------------------------------
# Scrittura manifest CANONICA (T1): byte-stabile a parità di contenuto.
# L'ordine delle chiavi di OGNI mappa è ridefinito in modo deterministico
# (ordinato); l'ordine delle LISTE è preservato (`runs` è semantico).
# generated_at NON viene rigenerato qui: il chiamante passa il dict già
# letto dal manifest esistente (nessun timestamp nuovo introdotto).
# --------------------------------------------------------------------------

def _canonical_sort_key(item):
    """Chiave di ordinamento per mappe YAML: (tipo, valore) — ordina senza
    crash anche chiavi di tipo misto (es. int e str nella stessa mappa)."""
    key = item[0]
    return (type(key).__name__, key)


def _canonicalize(value):
    """Ridefinisce ricorsivamente l'ordine delle chiavi di tutte le mappe
    (ordine ordinato deterministico); le liste restano invariate (ordine
    semantico). Le foglie (str/int/float/bool/None) passano intatte."""
    if isinstance(value, dict):
        return {k: _canonicalize(v)
                for k, v in sorted(value.items(), key=_canonical_sort_key)}
    if isinstance(value, list):
        return [_canonicalize(v) for v in value]
    return value


def dump_manifest(data: dict, path) -> None:
    """Scrittura canonica e byte-stabile del manifest v0 (T1).

    - ordina ricorsivamente le chiavi di ogni mappa (deterministico,
      indipendente dall'ordine di inserimento — ad es. l'ordine di
      iterazione di polars group_by in fit_normalizer);
    - preserva l'ordine delle liste (`runs` è semantico);
    - emette YAML in blocco con allow_unicode=True e default_flow_style=False
      (stessa resa di prima): a parità di CONTENUTO, i byte scritti sono
      identici tra riscritture successive.

    Nessuna chiave/struttura nuova: cambia solo l'ORDINE delle chiavi
    emesse (i consumatori leggono con yaml.safe_load, insensibile
    all'ordine). `path` può essere str o Path.
    """
    import yaml
    canonical = _canonicalize(data)
    p = Path(path)
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(canonical, fh, sort_keys=False,
                       default_flow_style=False, allow_unicode=True)


# --------------------------------------------------------------------------
# Validazione manifest v0 (schema OWNED D-W1; struttura attesa in docstring
# del modulo). Riusabile da D-W4 (pipeline build-features). Nessuna
# modifica a file: solo lettura + errori espliciti.
# --------------------------------------------------------------------------

def _resolve_scenario_path(manifest_path: Path, raw) -> Path:
    """Percorso scenario: assoluto → tale; relativo → prima rispetto alla
    directory del manifest, poi rispetto alla radice repo."""
    cand = Path(raw)
    if cand.is_absolute():
        return cand
    p1 = manifest_path.parent / cand
    if p1.exists():
        return p1
    repo_root = Path(__file__).resolve().parent.parent
    p2 = repo_root / cand
    if p2.exists():
        return p2
    return p1


def validate_manifest(path) -> dict:
    """Valida il manifest v0 e ritorna le info normalizzate per la pipeline.

    Controlli (errori espliciti con messaggio):
      1. run: split ∈ {train,val,test,baseline}; seed intero presente;
      2. scenario YAML esiste e scenario_id YAML == scenario_id manifest;
         seed YAML (se dichiarato) == seed manifest (il seed effettivo del
         run è quello YAML se presente, run.py);
      3. seed pairwise distinti TRA TUTTI i run + insiemi di seed per split
         pairwise disgiunti (AC-ML-1c);
      4. unicità per (file scenario, seed) — NON per scenario_id grezzo
         (4 run riusano m4_healthy.yaml id 41 con seed diversi);
      5. split-by-run: nessun run in 2 split; nomi run unici.

    Ritorna {"n_cycles": int, "runs": {name: {...}}, "splits": {...}}.
    """
    try:
        import yaml
    except ImportError:
        raise ValueError("pyyaml non installato: pip install pyyaml") from None
    p = Path(path)
    if not p.exists():
        raise ValueError(f"manifest non trovato: {p}")
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "runs" not in data:
        raise ValueError("manifest: chiave 'runs' mancante (schema v0)")
    win = data.get("window")
    # W1 (schema manifest.schema.md §2): top-level `N`; W2 (assunzione
    # pre-schema): window.n_cycles / n_cycles — accettate entrambe (DEVIAZIONE
    # DI COORDINAMENTO D-W4, vedi docstring modulo).
    n_cycles = (win.get("n_cycles") if isinstance(win, dict) else None) \
        or data.get("n_cycles") or data.get("N") or N_CYCLES
    if isinstance(n_cycles, bool) or not isinstance(n_cycles, int) \
            or n_cycles <= 0:
        raise ValueError(f"manifest: window.n_cycles non valido: {n_cycles!r}")

    raw_runs = data["runs"]
    if isinstance(raw_runs, dict):
        entries = []
        for name, entry in raw_runs.items():
            if not isinstance(entry, dict):
                raise ValueError(f"manifest: run {name!r} non è una mappa")
            e = dict(entry)
            e.setdefault("name", name)
            entries.append(e)
    elif isinstance(raw_runs, list):
        entries = list(raw_runs)
    else:
        raise ValueError("manifest: 'runs' deve essere una mappa "
                         "nome->entry o una lista di entry")

    runs: dict[str, dict] = {}
    for e in entries:
        if not isinstance(e, dict) or "name" not in e:
            raise ValueError("manifest: ogni run richiede la chiave 'name'")
        name = e["name"]
        if name in runs:
            raise ValueError(f"manifest: nome run duplicato: {name!r}")
        split = e.get("split")
        if split not in SPLITS:
            raise ValueError(f"manifest: run {name!r}: split {split!r} "
                             f"sconosciuto (attesi {SPLITS})")
        scenario_raw = e.get("scenario") or e.get("scenario_file")
        if not scenario_raw:
            raise ValueError(f"manifest: run {name!r} senza 'scenario'")
        scen = _resolve_scenario_path(p, scenario_raw)
        if not scen.exists():
            raise ValueError(f"manifest: scenario non trovato: {scen}")
        scenario = load_scenario(scen)
        manifest_sid = e.get("scenario_id")
        if manifest_sid != scenario.scenario_id:
            raise ValueError(
                f"manifest: run {name!r}: scenario_id {manifest_sid!r} != "
                f"scenario YAML {scenario.scenario_id!r} ({scen.name})")
        seed = e.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError(f"manifest: run {name!r}: seed deve essere un "
                             f"intero, trovato {seed!r}")
        if scenario.seed is not None and scenario.seed != seed:
            raise ValueError(
                f"manifest: run {name!r}: seed YAML {scenario.seed} != seed "
                f"manifest {seed} — il seed effettivo del run sarebbe "
                f"{scenario.seed}")
        runs[name] = {
            "name": name, "scenario": scen, "scenario_id": scenario.scenario_id,
            "seed": seed, "split": split,
            "out_dir": Path(e["out_dir"]) if e.get("out_dir") else None,
        }

    # unicità (file scenario, seed) — lo scenario_id può ripetersi
    seen: dict[tuple, str] = {}
    for name, r in runs.items():
        key = (str(r["scenario"]), r["seed"])
        if key in seen:
            raise ValueError(f"manifest: (scenario, seed) duplicato: "
                             f"{r['scenario'].name} seed={r['seed']} "
                             f"(run {seen[key]!r} e {name!r})")
        seen[key] = name
    # seed pairwise distinti tra tutti i run
    by_seed: dict[int, list[str]] = {}
    for name, r in runs.items():
        by_seed.setdefault(r["seed"], []).append(name)
    collisions = {s: ns for s, ns in by_seed.items() if len(ns) > 1}
    if collisions:
        raise ValueError(f"manifest: seed non distinti a coppie: {collisions}")
    # insiemi di seed per split pairwise disgiunti (AC-ML-1c)
    split_seeds = {s: set() for s in SPLITS}
    for r in runs.values():
        split_seeds[r["split"]].add(r["seed"])
    for s1, s2 in itertools.combinations(SPLITS, 2):
        overlap = split_seeds[s1] & split_seeds[s2]
        if overlap:
            raise ValueError(f"manifest: split {s1} e {s2} condividono seed "
                             f"{sorted(overlap)}")
    return {
        "n_cycles": int(n_cycles),
        "runs": runs,
        "splits": {s: [r["name"] for r in runs.values() if r["split"] == s]
                   for s in SPLITS},
    }


__all__ = [
    "N_CYCLES", "KPI_COLUMNS", "FLAG_COLUMNS", "CLOSE_REASON_WHITELIST",
    "FEATURE_COLUMNS", "PROVENANCE_COLUMNS", "NON_FEATURE_COLUMNS", "SPLITS",
    "window_cycles", "compute_window_features", "empty_features_frame",
    "join_labels", "flag_in_ramp", "fit_normalizer", "transform_zscore",
    "normalizer_to_manifest", "normalizer_from_manifest", "frame_hash",
    "dump_manifest", "RunInput", "build_dataset", "validate_manifest",
]
