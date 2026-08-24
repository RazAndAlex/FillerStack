"""Test D-W2 — layer ML dataset (piano §4 test 1-3, schema congelato ML-F1).

Copre: windowing giocattolo (medie/std/min/max/slope a mano + DROP coda +
window_idx), label join GT (fault a fine finestra / pre-onset healthy),
in_ramp da timeline, split-by-run, schema ESATTAMENTE 43 feature + dtypes,
determinismo (hash sul contenuto), anti-leakage (GT-permutation bit-identità
sulle sole feature, eventi engine whitelist, nessuna colonna GT in output),
z-score per-valvola (fit da healthy_train, σ=0 → z=0, niente refit su
val/test), validazione manifest (seed separati, unicità (scenario, seed),
split sconosciuto) + manifest reale (skipif).

TUTTI i test usano frame polars sintetici in-memory: nessun run di
simulazione, eseguibili anytime.
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plcsim.ml_dataset import (          # noqa: E402
    CLOSE_REASON_WHITELIST,
    FEATURE_COLUMNS,
    FLAG_COLUMNS,
    KPI_COLUMNS,
    N_CYCLES,
    NON_FEATURE_COLUMNS,
    PROVENANCE_COLUMNS,
    SPLITS,
    RunInput,
    build_dataset,
    compute_window_features,
    fit_normalizer,
    flag_in_ramp,
    frame_hash,
    join_labels,
    normalizer_from_manifest,
    normalizer_to_manifest,
    transform_zscore,
    validate_manifest,
    window_cycles,
)

REPO = Path(__file__).resolve().parent.parent
MANIFEST_REALE = REPO / "work" / "ml_dataset" / "manifest.yaml"

_CR_REASONS = ("target", "encoder_limit", "safety_timeout", "tail_timeout",
               "other")   # "other" fuori whitelist: NON deve contribuire


# --------------------------------------------------------------------------
# Builder sintetici (frame polars in-memory)
# --------------------------------------------------------------------------

def make_cycles(n_valves: int = 2, n_cycles: int = 120) -> pl.DataFrame:
    """valve_cycles toy deterministico:
    fillingtime = 1000+c, tailtime = 50 (costante), tailpulse = 2c,
    pulsecount = c², deltapulse = c, filling_step_out = 10 (costante)."""
    rows = []
    for v in range(n_valves):
        for c in range(1, n_cycles + 1):
            rows.append({
                "machine_code": f"valve{v}", "cycle_id": c,
                "fillingtime": 1000 + c, "tailtime": 50, "tailpulse": 2 * c,
                "pulsecount": c * c, "deltapulse": c, "filling_step_out": 10,
                "fillingok": c % 25 != 0, "fill_quality_ok": True,
                "sequence_ok": True, "sample_valid": c % 2 == 0,
                "position_limit": False, "filling_overtime": c % 100 == 0,
                "diagnostic_status": "SUSPECT" if c % 40 == 0 else "NORMAL",
                "close_reason": _CR_REASONS[c % 5],
            })
    return pl.DataFrame(rows)


def make_gt(n_valves: int, n_cycles: int, active: dict) -> pl.DataFrame:
    """ground_truth toy: una riga per ciclo; fault_type solo dove `active`
    (dict (valve_index, cycle_id) -> fault_type); schema come telemetry.py
    (fault_type String con null per i cicli sani)."""
    rows = []
    for v in range(n_valves):
        for c in range(1, n_cycles + 1):
            rows.append({
                "cycle_id": c, "machine_code": f"valve{v}",
                "ts_beg": c * 1000, "fault_type": active.get((v, c)),
                "severity": 0.5 if (v, c) in active else 0.0,
                "valve_id": v, "scenario_id": 60,
            })
    return pl.DataFrame(rows, schema_overrides={"fault_type": pl.String})


def make_events(rows) -> pl.DataFrame:
    """events toy: [(ts_beg, machine_code, event, note, cycle_id, scenario_id)]."""
    return pl.DataFrame({
        "ts_beg": [r[0] for r in rows], "machine_code": [r[1] for r in rows],
        "event": [r[2] for r in rows], "note": [r[3] for r in rows],
        "cycle_id": [r[4] for r in rows], "scenario_id": [r[5] for r in rows],
    })


def make_timeline(start_cycle: int, ramp_cycles: int | None,
                  valve_id: int = 0) -> pl.DataFrame:
    """fault_timeline toy (colonne FAULT_TIMELINE_COLUMNS, telemetry.py)."""
    return pl.DataFrame({
        "scenario_id": [63], "fault_id": [0], "fault_type": ["restriction"],
        "valve_id": [valve_id], "severity": [0.5],
        "onset_mode": ["gradual" if ramp_cycles else "abrupt"],
        "start_cycle": [start_cycle], "end_cycle": [None],
        "ramp_cycles": [ramp_cycles], "start_ts": [0], "end_ts": [None],
    })


def make_run(name: str, split: str, scenario_id: int, n_valves: int = 2,
             n_cycles: int = 120, active: dict | None = None,
             events: pl.DataFrame | None = None,
             timeline: pl.DataFrame | None = None) -> RunInput:
    return RunInput(name=name, split=split, scenario_id=scenario_id,
                    cycles=make_cycles(n_valves=n_valves, n_cycles=n_cycles),
                    events=events, gt=make_gt(n_valves, n_cycles, active or {}),
                    timeline=timeline)


def expected_features(rows: list[dict], n: int) -> dict:
    """Attese calcolate a mano (numpy, implementazione indipendente da
    polars) per le finestre piene: {(machine_code, window_idx): {feature: v}}."""
    wins: dict[tuple, list] = {}
    for r in rows:
        wins.setdefault((r["machine_code"], (r["cycle_id"] - 1) // n),
                        []).append(r)
    exp = {}
    x = np.arange(n, dtype=float)
    for (mc, w), cyc in wins.items():
        if len(cyc) != n:
            continue          # finestra parziale di coda: DROP (§5)
        cyc.sort(key=lambda r: r["cycle_id"])
        d: dict[str, float] = {}
        for k in KPI_COLUMNS:
            y = np.array([r[k] for r in cyc], dtype=float)
            d[f"mean_{k}"] = float(y.mean())
            d[f"std_{k}"] = float(y.std(ddof=1))
            d[f"min_{k}"] = float(y.min())
            d[f"max_{k}"] = float(y.max())
            d[f"slope_{k}"] = float(
                np.sum((x - x.mean()) * (y - y.mean()))
                / np.sum((x - x.mean()) ** 2))
        for f in FLAG_COLUMNS:
            d[f"{f}_rate"] = float(np.mean([r[f] for r in cyc]))
        d["diagnostic_suspect_rate"] = float(
            np.mean([r["diagnostic_status"] == "SUSPECT" for r in cyc]))
        for reason in CLOSE_REASON_WHITELIST:
            d[f"close_reason_{reason}_rate"] = float(
                np.mean([r["close_reason"] == reason for r in cyc]))
        exp[(mc, w)] = d
    return exp


# --------------------------------------------------------------------------
# 1. Windowing giocattolo: stats a mano, DROP coda, window_idx
# --------------------------------------------------------------------------

def test_windowing_stats_toy():
    n, n_cycles, n_valves = 50, 120, 2
    cycles = make_cycles(n_valves=n_valves, n_cycles=n_cycles)
    feats = compute_window_features(cycles, n=n)
    expected = expected_features(cycles.to_dicts(), n)
    # DROP coda: 120 cicli → finestre piene 0 e 1 (1..100); 101..120 scartati
    assert feats.height == n_valves * 2
    assert sorted(feats["window_idx"].unique().to_list()) == [0, 1]
    for v in range(n_valves):
        w = feats.filter(pl.col("machine_code") == f"valve{v}")
        assert w["window_idx"].to_list() == [0, 1]
        assert w["last_cycle_id"].to_list() == [50, 100]
    # window_idx = floor((cycle_id − 1) / N)
    widx = window_cycles(cycles, n=n)
    assert widx.filter(pl.col("cycle_id") == 51)["window_idx"].to_list() \
        == [1, 1]                 # una per valvola
    assert widx.filter(pl.col("cycle_id") == 120).height == 0   # coda DROP
    # uguaglianza con le attese a mano (tutte le 30 statistiche + rate)
    assert feats.height == len(expected)
    for (mc, w), exp in expected.items():
        row = feats.filter((pl.col("machine_code") == mc)
                           & (pl.col("window_idx") == w))
        assert row.height == 1
        for col, val in exp.items():
            assert row[col].to_list()[0] == pytest.approx(val, rel=1e-9,
                                                          abs=1e-9), (mc, w, col)
    # valori esatti a mano (valve0, finestra 0)
    r = feats.filter((pl.col("machine_code") == "valve0")
                     & (pl.col("window_idx") == 0))
    assert r["mean_fillingtime"].to_list()[0] == pytest.approx(1025.5)
    assert r["slope_fillingtime"].to_list()[0] == pytest.approx(1.0)
    assert r["min_fillingtime"].to_list()[0] == pytest.approx(1001.0)
    assert r["max_fillingtime"].to_list()[0] == pytest.approx(1050.0)
    assert r["mean_tailtime"].to_list()[0] == pytest.approx(50.0)
    assert r["std_tailtime"].to_list()[0] == pytest.approx(0.0)
    assert r["slope_tailpulse"].to_list()[0] == pytest.approx(2.0)
    assert r["fillingok_rate"].to_list()[0] == pytest.approx(48 / 50)
    assert r["sample_valid_rate"].to_list()[0] == pytest.approx(0.5)
    assert r["diagnostic_suspect_rate"].to_list()[0] == pytest.approx(1 / 50)
    # close_reason whitelist: 10/50 ciascuno; "other" (10 cicli) non conta
    for reason in CLOSE_REASON_WHITELIST:
        assert r[f"close_reason_{reason}_rate"].to_list()[0] \
            == pytest.approx(0.2)


def test_windowing_late_pulse():
    n, n_cycles = 50, 100
    cycles = make_cycles(n_valves=1, n_cycles=n_cycles)
    events = make_events([
        (0, "valve0", "LATE_PULSE", "n_pulses=3", 10, 60),
        (0, "valve0", "LATE_PULSE", "n_pulses=5", 60, 60),
        (0, "valve0", "LATE_PULSE", "n_pulses=2", 99, 60),   # coda → nessuna finestra
    ])
    feats = compute_window_features(cycles, events=events, n=n)
    assert feats.schema["late_pulse_count"] == pl.Int64
    assert feats.schema["late_pulse_rate"] == pl.Float64
    v0 = feats.filter(pl.col("machine_code") == "valve0")
    assert v0.filter(pl.col("window_idx") == 0)["late_pulse_count"].to_list() \
        == [1]
    assert v0.filter(pl.col("window_idx") == 1)["late_pulse_count"].to_list() \
        == [2]                    # eventi a cycle_id 60 e 99 (finestra 51..100)
    assert v0.filter(pl.col("window_idx") == 1)["late_pulse_rate"].to_list() \
        == [2 / 50]
    # senza eventi → 0 / 0.0
    feats0 = compute_window_features(cycles, events=None, n=n)
    assert feats0["late_pulse_count"].to_list() == [0, 0]


# --------------------------------------------------------------------------
# 2. Label join: fault a fine finestra; pre-onset → healthy
# --------------------------------------------------------------------------

def test_label_join():
    n, n_cycles, n_valves = 10, 100, 2
    cycles = make_cycles(n_valves=n_valves, n_cycles=n_cycles)
    active = {}
    for c in range(81, n_cycles + 1):      # valve0 guasta da c=81
        active[(0, c)] = "restriction"
    for c in range(30, 61):                # valve1 guasta in [30, 60]
        active[(1, c)] = "closing_delay"
    gt = make_gt(n_valves, n_cycles, active)
    lab = join_labels(compute_window_features(cycles, n=n), gt)

    def get(mc, w):
        rows = lab.filter((pl.col("machine_code") == mc)
                          & (pl.col("window_idx") == w))["label"].to_list()
        assert len(rows) == 1
        return rows[0]

    # valve0: finestre 0..7 pre-onset (ultimo ciclo <= 80) → healthy; 8,9 → restriction
    for w in range(8):
        assert get("valve0", w) == "healthy"
    assert get("valve0", 8) == "restriction"
    assert get("valve0", 9) == "restriction"
    # valve1: finestre 0,1 pre-onset; 2..5 fault; 6+ healthy (fine a 60)
    assert get("valve1", 0) == "healthy"
    assert get("valve1", 1) == "healthy"
    for w in range(2, 6):
        assert get("valve1", w) == "closing_delay"
    for w in range(6, 10):
        assert get("valve1", w) == "healthy"
    # dal join esce SOLO label in più (nessuna colonna GT)
    assert set(lab.columns) - set(
        compute_window_features(cycles, n=n).columns) == {"label"}


def test_label_join_gt_mancante():
    cycles = make_cycles(n_valves=1, n_cycles=100)
    feats = compute_window_features(cycles, n=50)
    gt = make_gt(1, 100, {})
    gt = gt.filter((pl.col("cycle_id") != 50) | (pl.col("machine_code")
                                                 != "valve0"))
    with pytest.raises(ValueError, match="mancante"):
        join_labels(feats, gt)


def test_label_join_gt_non_1a1():
    cycles = make_cycles(n_valves=1, n_cycles=100)
    feats = compute_window_features(cycles, n=50)
    gt = make_gt(1, 100, {})
    dup = gt.filter((pl.col("cycle_id") == 50) & (pl.col("machine_code")
                                                  == "valve0"))
    with pytest.raises(ValueError, match="1:1"):
        join_labels(feats, pl.concat([gt, dup]))


# --------------------------------------------------------------------------
# 3. in_ramp da fault_timeline (rampa / cavalca onset / steady)
# --------------------------------------------------------------------------

def test_in_ramp():
    n, n_cycles = 10, 100
    feats = compute_window_features(make_cycles(n_valves=1, n_cycles=n_cycles),
                                    n=n)
    # gradual: start=30, ramp=20 → cicli rampa [30, 49]
    got = flag_in_ramp(feats, make_timeline(30, 20), n=n)["in_ramp"].to_list()
    assert got == [False, False, True, True, True, False,
                   False, False, False, False]
    # abrupt (ramp_cycles None), start=80 → solo la finestra che cavalca (71..80)
    got2 = flag_in_ramp(feats, make_timeline(80, None), n=n)["in_ramp"].to_list()
    assert got2 == [False] * 7 + [True, False, False]
    # nessuna timeline → tutto False
    assert flag_in_ramp(feats, None, n=n)["in_ramp"].to_list() == [False] * 10


# --------------------------------------------------------------------------
# 4. Split-by-run (manifest sintetico)
# --------------------------------------------------------------------------

def _write_scenario(dirpath: Path, sid: int, name: str, faults=()) -> Path:
    p = dirpath / f"s{sid}.yaml"
    p.write_text(yaml.safe_dump({
        "scenario_id": sid, "name": name, "seed": None, "faults": list(faults),
    }, sort_keys=False), encoding="utf-8")
    return p


def _write_manifest(dirpath: Path, runs, n_cycles: int = 50,
                    runs_as_list: bool = False) -> Path:
    data = {"schema_version": 0, "window": {"n_cycles": n_cycles},
            "runs": list(runs) if runs_as_list else dict(runs)}
    p = dirpath / "manifest.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


def test_split_by_run():
    d = Path(__import__("tempfile").mkdtemp())
    try:
        s41 = _write_scenario(d, 41, "healthy")
        s60 = _write_scenario(d, 60, "train_a",
                              [{"fault_type": "restriction", "scope": "local",
                                "valve_id": 0, "severity": 0.5,
                                "onset": {"mode": "abrupt",
                                          "start_cycle": 100}}])
        runs = {
            "healthy_train": {"scenario": s41.name, "scenario_id": 41,
                              "seed": 101, "split": "train"},
            "faults_train_a": {"scenario": s60.name, "scenario_id": 60,
                               "seed": 1001, "split": "train"},
            "healthy_val": {"scenario": s41.name, "scenario_id": 41,
                            "seed": 303, "split": "val"},
        }
        info = validate_manifest(_write_manifest(d, runs))
        # nessun run in 2 split
        per_run = {name: {r["split"] for r in info["runs"].values()
                          if r["name"] == name}
                   for name in info["runs"]}
        assert all(len(s) == 1 for s in per_run.values())
        # split disgiunti per run
        for s1, s2 in itertools.combinations(SPLITS, 2):
            assert not (set(info["splits"][s1]) & set(info["splits"][s2]))
        # stesso nome in 2 split (forma lista) → errore esplicito
        bad = [
            {"name": "x", "scenario": s41.name, "scenario_id": 41,
             "seed": 1, "split": "train"},
            {"name": "x", "scenario": s41.name, "scenario_id": 41,
             "seed": 2, "split": "val"},
        ]
        with pytest.raises(ValueError, match="duplicato"):
            validate_manifest(_write_manifest(d, bad, runs_as_list=True))
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# 5. Schema esatto: ESATTAMENTE le 43 colonne dello schema + provenance
# --------------------------------------------------------------------------

def test_schema_esatto_43():
    assert len(FEATURE_COLUMNS) == 43
    cycles = make_cycles(n_valves=2, n_cycles=120)
    feats = compute_window_features(cycles)
    assert feats.columns == ["machine_code", "window_idx", "last_cycle_id",
                             *FEATURE_COLUMNS]
    schema = feats.schema
    for c in FEATURE_COLUMNS:
        expected = pl.Int64 if c == "late_pulse_count" else pl.Float64
        assert schema[c] == expected, f"dtype errato per {c}"


def test_schema_esatto_dataset_completo():
    runs = [make_run("healthy_train", "train", 41),
            make_run("faults_val", "val", 63, active={(0, 90): "restriction"})]
    ds = build_dataset(runs, zscore=False)
    assert ds.columns == [*NON_FEATURE_COLUMNS, *FEATURE_COLUMNS]
    assert len(ds.columns) == len(PROVENANCE_COLUMNS) + 2 + 43
    st = ds.schema
    assert st["run_name"] == pl.String
    assert st["scenario_id"] == pl.Int64
    assert st["machine_code"] == pl.String
    assert st["window_idx"] == pl.Int64
    assert st["split"] == pl.String
    assert st["label"] == pl.String
    assert st["in_ramp"] == pl.Boolean


# --------------------------------------------------------------------------
# 6. Determinismo: stesso input → stesso hash di contenuto (2 run extractor)
# --------------------------------------------------------------------------

def test_determinismo():
    runs = [make_run("healthy_train", "train", 41),
            make_run("faults_train_a", "train", 60, n_cycles=100,
                     active={(0, 90): "restriction", (1, 95): "closing_delay"})]
    ds1 = build_dataset(runs)
    ds2 = build_dataset(runs)
    assert frame_hash(ds1) == frame_hash(ds2)
    assert frame_hash(ds1, FEATURE_COLUMNS) == frame_hash(ds2, FEATURE_COLUMNS)
    # ordine righe dell'input diverso → stesso output (sort deterministico)
    runs_rev = [RunInput(name=r.name, split=r.split,
                         scenario_id=r.scenario_id,
                         cycles=r.cycles.sort(["machine_code", "cycle_id"],
                                              descending=True),
                         events=r.events, gt=r.gt, timeline=r.timeline)
                for r in runs]
    ds3 = build_dataset(runs_rev)
    assert frame_hash(ds3) == frame_hash(ds1)


# --------------------------------------------------------------------------
# 7. ANTI-LEAKAGE (test funzionali forti, AC-ML-1a)
# --------------------------------------------------------------------------

def test_antileak_gt_permutation():
    """Permutare fault_type/severity nella GT → feature bit-identiche
    (hash uguale sulle SOLA 43 colonne feature); cambiano solo le label."""
    active_a = {(0, c): "restriction" for c in range(10, 101)}
    active_a.update({(1, c): "closing_delay" for c in range(60, 101)})
    runs = [make_run("healthy_train", "train", 41),
            make_run("faults_train_a", "train", 60, active=active_a),
            make_run("faults_val", "val", 63, active={(0, 20): "opening_delay"})]
    ds_a = build_dataset(runs)                       # zscore=True di default

    # GT permutata: shuffle di fault_type e severity tra tutti i cicli guasti
    rng = np.random.default_rng(0)
    guasti = [r for r in runs if r.gt is not None]
    shuffled = []
    for r in guasti:
        g = r.gt.to_dicts()
        faults = [row for row in g if row["fault_type"] is not None]
        types = [f["fault_type"] for f in faults]
        sevs = [f["severity"] for f in faults]
        rng.shuffle(types)
        rng.shuffle(sevs)
        for f, t, s in zip(faults, types, sevs):
            f["fault_type"] = t
            f["severity"] = s
        shuffled.append(pl.DataFrame(g,
                                     schema_overrides={"fault_type": pl.String}))
    runs_b = [RunInput(name=r.name, split=r.split, scenario_id=r.scenario_id,
                       cycles=r.cycles, events=r.events, gt=shuffled[i],
                       timeline=r.timeline)
              for i, r in enumerate(guasti)]
    ds_b = build_dataset(runs_b)
    # bit-identità sulle SOLO 43 colonne feature (label/in_ramp escluse)
    assert frame_hash(ds_a, FEATURE_COLUMNS) == frame_hash(ds_b, FEATURE_COLUMNS)
    # le label invece cambiano
    assert not ds_a["label"].equals(ds_b["label"])


def test_antileak_eventi_engine():
    """FAULT_START (nota rivelatrice), FAULT_RAMP, CMD:OPEN/CMD:CLOSE nel
    frame events → nessun effetto sulle feature (whitelist per tipo)."""
    late = [(0, "valve0", "LATE_PULSE", "n_pulses=3", 10, 60),
            (0, "valve1", "LATE_PULSE", "n_pulses=2", 77, 60)]
    engine = [
        (0, "valve0", "FAULT_START",
         "restriction severity=0.5 start_cycle=100", 10, 60),
        (0, "valve0", "FAULT_RAMP", "ramp severity=0.25", 20, 60),
        (0, "valve0", "CMD:OPEN", "", 30, 60),
        (0, "valve0", "CMD:CLOSE", "target", 31, 60),
    ]
    runs_clean = [make_run("healthy_train", "train", 41,
                           events=make_events(late))]
    runs_engine = [make_run("healthy_train", "train", 41,
                            events=make_events(late + engine))]
    # zscore=False: il confronto bit-identità è sulla matrice feature grezza
    # (la normalizzazione è ortogonale alla whitelist eventi)
    ds_clean = build_dataset(runs_clean, zscore=False)
    ds_engine = build_dataset(runs_engine, zscore=False)
    assert frame_hash(ds_clean, FEATURE_COLUMNS) \
        == frame_hash(ds_engine, FEATURE_COLUMNS)
    # controllo positivo: un LATE_PULSE in più DEVE cambiare le feature
    runs_extra = [make_run("healthy_train", "train", 41,
                           events=make_events(late + [(0, "valve0",
                                                       "LATE_PULSE",
                                                       "n_pulses=1", 11, 60)]))]
    ds_extra = build_dataset(runs_extra, zscore=False)
    assert frame_hash(ds_clean, FEATURE_COLUMNS) \
        != frame_hash(ds_extra, FEATURE_COLUMNS)
    assert ds_extra.filter((pl.col("machine_code") == "valve0")
                           & (pl.col("window_idx") == 0))["late_pulse_count"] \
        .to_list() == [2]


def test_antileak_nessuna_colonna_gt():
    runs = [make_run("healthy_train", "train", 41),
            make_run("faults_val", "val", 63, active={(0, 90): "restriction"})]
    ds = build_dataset(runs, zscore=False)
    # colonne GT presenti nel join intermedio → assenti dall'output feature
    # (machine_code/scenario_id sono provenance legittime, telemetry.py GT_COLUMNS)
    gt_cols = {"cycle_id", "ts_beg", "fault_type", "severity", "valve_id"}
    assert not (gt_cols & set(ds.columns))


# --------------------------------------------------------------------------
# 8. z-score: per-valvola da healthy_train; σ=0 → z=0; val/test no refit
# --------------------------------------------------------------------------

def test_zscore_fit_transform():
    feats = pl.DataFrame({
        "machine_code": ["valve0"] * 4 + ["valve1"] * 4,
        "fillingtime": [100., 110., 120., 130., 5., 5., 5., 5.],
    })
    stats = fit_normalizer(feats, ["fillingtime"])
    mu, sigma = stats["valve0"]["fillingtime"]
    assert mu == pytest.approx(115.0)
    assert sigma == pytest.approx(np.std([100., 110., 120., 130.], ddof=1))
    assert stats["valve1"]["fillingtime"][1] == 0.0       # σ=0 → guardia
    z = transform_zscore(feats, stats, ["fillingtime"])
    z0 = z.filter(pl.col("machine_code") == "valve0")["fillingtime"].to_list()
    assert z0 == pytest.approx([(x - mu) / sigma
                                for x in (100., 110., 120., 130.)])
    assert z.filter(pl.col("machine_code") == "valve1")["fillingtime"].to_list() \
        == [0.0] * 4
    # val/test usano le statistiche di train: nessun refit
    val = pl.DataFrame({"machine_code": ["valve0", "valve1"],
                        "fillingtime": [115.0, 42.0]})
    zv = transform_zscore(val, stats, ["fillingtime"])
    assert zv["fillingtime"].to_list()[0] == pytest.approx(0.0)  # 115 = μ train
    assert zv["fillingtime"].to_list()[1] == 0.0                 # σ=0 → z=0
    # un fit sui soli val darebbe statistiche diverse → non è quanto usato
    stats_val = fit_normalizer(
        pl.DataFrame({"machine_code": ["valve0", "valve0"],
                      "fillingtime": [10.0, 20.0]}), ["fillingtime"])
    assert stats_val["valve0"]["fillingtime"][0] == pytest.approx(15.0)
    assert stats_val["valve0"]["fillingtime"][0] \
        != stats["valve0"]["fillingtime"][0]
    # valvola senza statistiche → errore esplicito (mai refit implicito)
    with pytest.raises(ValueError, match="valvole senza statistiche"):
        transform_zscore(pl.DataFrame({"machine_code": ["valve9"],
                                       "fillingtime": [1.0]}), stats,
                         ["fillingtime"])


def test_zscore_end_to_end():
    """build_dataset: le feature di val/test usano μ/σ del solo healthy_train."""
    runs = [make_run("healthy_train", "train", 41, n_cycles=100),
            make_run("healthy_val", "val", 41, n_cycles=100)]
    ds = build_dataset(runs)                     # zscore=True di default
    assert ds.height == 2 * (2 + 2)              # 2 valvole × 2 finestre × 2 run
    # nessun NaN/Inf dopo la normalizzazione (guardia σ=0)
    assert ds.height == ds.drop_nulls().height
    # attese manuali: μ/σ di mean_fillingtime dal SOLO healthy_train (cicli
    # grezzi, NON le feature già trasformate) — per-valvola
    for v in (0, 1):
        mc = f"valve{v}"
        ht_cycles = runs[0].cycles.filter(pl.col("machine_code") == mc)
        win_means = [
            ht_cycles.filter(((pl.col("cycle_id") - 1) // 50) == w)
            ["fillingtime"].mean()
            for w in range(2)]
        mu = float(np.mean(win_means))
        sigma = float(np.std(win_means, ddof=1))
        raw = np.mean([1000 + c for c in range(1, 51)])   # finestra 0 del val
        expected_z = (raw - mu) / sigma
        got = ds.filter((pl.col("run_name") == "healthy_val")
                        & (pl.col("machine_code") == mc)
                        & (pl.col("window_idx") == 0))["mean_fillingtime"] \
            .to_list()[0]
        assert got == pytest.approx(expected_z, rel=1e-9, abs=1e-9), mc


def test_normalizer_manifest_roundtrip():
    feats = pl.DataFrame({
        "machine_code": ["valve0"] * 3 + ["valve1"] * 3,
        "fillingtime": [100., 110., 120., 5., 5., 5.],
        "tailtime": [50., 50., 50., 60., 60., 60.],
    })
    stats = fit_normalizer(feats, ["fillingtime", "tailtime"])
    data = normalizer_to_manifest(stats)
    json.dumps(data)                              # JSON-serializzabile (§7)
    assert normalizer_from_manifest(data) == stats


# --------------------------------------------------------------------------
# 9. Validazione manifest: seed separati, unicità (scenario, seed), split
# --------------------------------------------------------------------------

def test_manifest_seed_non_distinti():
    d = Path(__import__("tempfile").mkdtemp())
    try:
        s41 = _write_scenario(d, 41, "healthy")
        s60 = _write_scenario(d, 60, "a")
        runs = {
            "r1": {"scenario": s41.name, "scenario_id": 41, "seed": 101,
                   "split": "train"},
            "r2": {"scenario": s60.name, "scenario_id": 60, "seed": 101,
                   "split": "val"},
        }
        with pytest.raises(ValueError, match="seed non distinti"):
            validate_manifest(_write_manifest(d, runs))
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_manifest_scenario_seed_unicita():
    d = Path(__import__("tempfile").mkdtemp())
    try:
        s41 = _write_scenario(d, 41, "healthy")
        # stesso (file, seed) → errore, anche con nomi diversi
        runs = {
            "a": {"scenario": s41.name, "scenario_id": 41, "seed": 101,
                  "split": "train"},
            "b": {"scenario": s41.name, "scenario_id": 41, "seed": 101,
                  "split": "val"},
        }
        with pytest.raises(ValueError, match="\\(scenario, seed\\) duplicato"):
            validate_manifest(_write_manifest(d, runs))
        # stesso FILE con seed diversi → OK (4 run riusano m4_healthy id 41)
        runs_ok = {
            "a": {"scenario": s41.name, "scenario_id": 41, "seed": 101,
                  "split": "train"},
            "b": {"scenario": s41.name, "scenario_id": 41, "seed": 202,
                  "split": "baseline"},
            "c": {"scenario": s41.name, "scenario_id": 41, "seed": 303,
                  "split": "val"},
            "d": {"scenario": s41.name, "scenario_id": 41, "seed": 404,
                  "split": "test"},
        }
        info = validate_manifest(_write_manifest(d, runs_ok))
        assert len(info["runs"]) == 4
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_manifest_split_sconosciuto_e_scenario_id():
    d = Path(__import__("tempfile").mkdtemp())
    try:
        s41 = _write_scenario(d, 41, "healthy")
        with pytest.raises(ValueError, match="sconosciuto"):
            validate_manifest(_write_manifest(d, {
                "a": {"scenario": s41.name, "scenario_id": 41, "seed": 101,
                      "split": "banana"}}))
        with pytest.raises(ValueError, match="scenario_id"):
            validate_manifest(_write_manifest(d, {
                "a": {"scenario": s41.name, "scenario_id": 99, "seed": 101,
                      "split": "train"}}))
        # seed YAML dichiarato ma diverso dal manifest → errore (il seed
        # effettivo del run sarebbe quello YAML, run.py)
        s60 = _write_scenario(d, 60, "a")
        raw = {"scenario_id": 60, "name": "a", "seed": 999, "faults": []}
        s60.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        with pytest.raises(ValueError, match="seed YAML"):
            validate_manifest(_write_manifest(d, {
                "a": {"scenario": s60.name, "scenario_id": 60, "seed": 101,
                      "split": "train"}}))
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_manifest_tabella_piano():
    """Tabella §3.1 del piano (9 run, 4 split, m4_healthy riusato 4×) —
    valida con percorsi relativi alla radice repo e con file tmp."""
    d = Path(__import__("tempfile").mkdtemp())
    try:
        m4 = _write_scenario(d, 41, "healthy")   # m4_healthy equivalente
        fa = _write_scenario(d, 60, "train_a",
                             [{"fault_type": "restriction", "scope": "local",
                               "valve_id": 0, "severity": 0.5,
                               "onset": {"mode": "abrupt",
                                         "start_cycle": 100}}])
        fb = _write_scenario(d, 62, "train_b",
                             [{"fault_type": "pressure_instability",
                               "scope": "group", "group_id": 0,
                               "severity": 0.5,
                               "onset": {"mode": "gradual",
                                         "start_cycle": 100,
                                         "ramp_cycles": 20}}])
        fv = _write_scenario(d, 63, "val",
                             [{"fault_type": "closing_delay",
                               "scope": "local", "valve_id": 1,
                               "severity": 5,
                               "onset": {"mode": "abrupt",
                                         "start_cycle": 5000}}])
        fa2 = _write_scenario(d, 64, "test_a",
                              [{"fault_type": "opening_delay",
                                "scope": "local", "valve_id": 2,
                                "severity": 5,
                                "onset": {"mode": "abrupt",
                                          "start_cycle": 12000}}])
        fb2 = _write_scenario(d, 65, "test_b",
                              [{"fault_type": "flowmeter_dropout",
                                "scope": "local", "valve_id": 3,
                                "severity": 0.5,
                                "onset": {"mode": "abrupt",
                                          "start_cycle": 100}}])
        tab = {
            "healthy_train":   {"scenario": m4.name,  "scenario_id": 41,
                                "seed": 101,  "split": "train"},
            "faults_train_a":  {"scenario": fa.name,  "scenario_id": 60,
                                "seed": 1001, "split": "train"},
            "faults_train_b":  {"scenario": fb.name,  "scenario_id": 62,
                                "seed": 1002, "split": "train"},
            "healthy_baseline": {"scenario": m4.name, "scenario_id": 41,
                                 "seed": 202,  "split": "baseline"},
            "healthy_val":     {"scenario": m4.name,  "scenario_id": 41,
                                "seed": 303,  "split": "val"},
            "faults_val":      {"scenario": fv.name,  "scenario_id": 63,
                                "seed": 3001, "split": "val"},
            "healthy_test":    {"scenario": m4.name,  "scenario_id": 41,
                                "seed": 404,  "split": "test"},
            "faults_test_a":   {"scenario": fa2.name, "scenario_id": 64,
                                "seed": 4001, "split": "test"},
            "faults_test_b":   {"scenario": fb2.name, "scenario_id": 65,
                                "seed": 4002, "split": "test"},
        }
        info = validate_manifest(_write_manifest(d, tab, n_cycles=50))
        assert info["n_cycles"] == 50
        assert len(info["runs"]) == 9
        assert info["splits"]["train"] == ["healthy_train", "faults_train_a",
                                           "faults_train_b"]
        assert info["splits"]["baseline"] == ["healthy_baseline"]
        assert info["splits"]["val"] == ["healthy_val", "faults_val"]
        assert info["splits"]["test"] == ["healthy_test", "faults_test_a",
                                          "faults_test_b"]
        # percorso relativo alla radice repo (m4_healthy reale, solo lettura)
        info2 = validate_manifest(_write_manifest(d, {
            "healthy_train": {"scenario": "scenarios/m4_healthy.yaml",
                              "scenario_id": 41, "seed": 101,
                              "split": "train"}}))
        assert info2["runs"]["healthy_train"]["scenario"].resolve() \
            == (REPO / "scenarios" / "m4_healthy.yaml").resolve()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(not MANIFEST_REALE.exists(),
                    reason="manifest reale creato da D-W4")
def test_manifest_reale():
    """Esercita validate_manifest sul manifest concreto (work/ml_dataset/
    manifest.yaml) quando D-W4 lo crea; prima è skip."""
    info = validate_manifest(MANIFEST_REALE)
    assert info["n_cycles"] == N_CYCLES          # N=50 congelato (§5)
    assert len(info["runs"]) == 9
    for s1, s2 in itertools.combinations(SPLITS, 2):
        assert not (set(info["splits"][s1]) & set(info["splits"][s2]))
