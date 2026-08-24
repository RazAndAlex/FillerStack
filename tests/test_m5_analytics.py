"""Test W1 (M5) — analytics core (layer 7, READ-ONLY sopra la telemetria).

TDD su frame SINTETICI autocontenuti (fonte primaria): nessun run di
simulazione, nessun RNG, funzioni pure polars (<60s). Copre il contratto
M5-F1..F7 (work/reviews/plan-m5/converged-feedback.md): baseline congelata,
top-10 con tie-break deterministico, XmR esatto, soglie fisse diagnostiche +
alert rate-based (F3), sigma-ratio (F4), warning numerico (F5), detector D5
portato (F6), MachineStable da events, determinismo SHA-256, nessuna
scrittura su input, GT mai nel percorso decisionale (ADR-0012).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
import sys
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plcsim.analytics import (                  # noqa: E402
    AnalyticsConfig, Baseline, analyze, detect_faulted_valves, health, main,
)
from tests.test_fault_integration import detect_faulted_valves as d5_port  # noqa: E402

T0 = dt.datetime(2026, 6, 1, 8, 0, 0, tzinfo=dt.timezone.utc)

# cfg di test: stabile dopo 1 min (finestre sintetiche ~16 min)
CFG = AnalyticsConfig(stable_min_minutes=1)
# cfg per il test MachineStable con la regola 30 min reale
CFG_30 = AnalyticsConfig(stable_min_minutes=30)


# --------------------------------------------------------------------------
# Harness sintetico (deterministico, nessun RNG: LCG aritmetico puro)
# --------------------------------------------------------------------------
def _uni(i: int) -> float:
    """Uniforme pseudo-casuale deterministica in [0,1) (aritmetica pura)."""
    return ((i * 1103515245 + 12345) % 2147483648) / 2147483648.0


def make_cycles(valves, n_windows, window_cycles, ft_cfg, tt_cfg=None,
                pc_cfg=None, ts_step_s: float = 3.2, t0=None,
                same_noise: bool = False, wid_end_min=None) -> pl.DataFrame:
    """Frame cicli sintetico con flag coerenti alla semantica V3 (D1):
    filling_overtime = FT>2000, sequence_ok = TT<=600,
    diagnostic_status = SUSPECT se (overtime o !sequence_ok)."""
    if t0 is None:
        t0 = T0
    if tt_cfg is None:
        tt_cfg = {mc: (300.0, 44.0) for mc in valves}
    if pc_cfg is None:
        pc_cfg = {mc: (2500.0, 7.0) for mc in valves}
    rows = []
    cid = 0
    for wid in range(n_windows):
        for i in range(window_cycles):
            for vi, mc in enumerate(valves):
                mean, scale = ft_cfg[mc]
                idx = (0 if same_noise else vi) * 1_000_000 + wid * 1000 + i
                u = _uni(idx)
                ft = mean + scale * (u - 0.5)
                ttm, tts = tt_cfg[mc]
                tt = ttm + tts * (u - 0.5)
                pcm, pcs = pc_cfg[mc]
                pc = pcm + pcs * (u - 0.5)
                tp = 30.0 + 5.0 * (_uni(idx + 3) - 0.5)
                if wid_end_min:
                    span = wid_end_min[wid] - (wid_end_min[wid - 1]
                                               if wid > 0 else 0.0)
                    ts = t0 + dt.timedelta(
                        minutes=(wid_end_min[wid - 1] if wid > 0 else 0.0)
                        + (i + 1) * span / window_cycles)
                else:
                    ts = t0 + dt.timedelta(
                        seconds=(wid * window_cycles + i) * ts_step_s)
                overtime = ft > 2000.0
                seq_ok = tt <= 600.0
                diag = "SUSPECT" if (overtime or not seq_ok) else "NORMAL"
                rows.append({
                    "machine_code": mc, "ts_beg": ts,
                    "fillingtime": ft, "tailtime": tt, "tailpulse": tp,
                    "pulsecount": pc, "target": 2500.0,
                    "deltapulse": 2500.0 - pc, "filling_step_out": 26,
                    "fillingok": True, "fill_quality_ok": True,
                    "sequence_ok": seq_ok, "sample_valid": True,
                    "diagnostic_status": diag, "close_reason": "target",
                    "position_limit": False, "filling_overtime": overtime,
                    "cycle_id": cid, "scenario_id": 1,
                })
                cid += 1
    return pl.DataFrame(rows)


def make_const_cycles(valves, n_windows, window_cycles, ft: float) -> pl.DataFrame:
    """FT costante per valvola (sigma esattamente 0.0: tie-break pulito)."""
    rows = []
    cid = 0
    for wid in range(n_windows):
        for i in range(window_cycles):
            for mc in valves:
                rows.append({
                    "machine_code": mc,
                    "ts_beg": T0 + dt.timedelta(
                        seconds=(wid * window_cycles + i) * 3.2),
                    "fillingtime": float(ft), "tailtime": 300.0,
                    "tailpulse": 30.0, "pulsecount": 2500.0, "target": 2500.0,
                    "deltapulse": 0.0, "filling_step_out": 26,
                    "fillingok": True, "fill_quality_ok": True,
                    "sequence_ok": True, "sample_valid": True,
                    "diagnostic_status": "NORMAL", "close_reason": "target",
                    "position_limit": False, "filling_overtime": False,
                    "cycle_id": cid, "scenario_id": 1,
                })
                cid += 1
    return pl.DataFrame(rows)


def stable_events(t0=None) -> pl.DataFrame:
    """Timeline macchina stabile: Running ben prima della prima finestra."""
    t = t0 if t0 is not None else T0 - dt.timedelta(minutes=1)
    return pl.DataFrame([
        {"ts_beg": t - dt.timedelta(minutes=2), "machine_code": "MACHINE",
         "event": "STATE:Starting", "note": "", "cycle_id": 0,
         "scenario_id": 1},
        {"ts_beg": t, "machine_code": "MACHINE", "event": "STATE:Running",
         "note": "", "cycle_id": 0, "scenario_id": 1},
    ])


VALVES_5 = ["valve0", "valve1", "valve2", "valve3", "valve4"]
VALVES_7 = ["valve0", "valve1", "valve2", "valve3", "valve4",
            "valve8", "valve12"]
VALVES_15 = [f"valve{i}" for i in range(15)]


def _healthy_5(windows: int = 6) -> pl.DataFrame:
    return make_cycles(VALVES_5, windows, 100,
                       {mc: (1950.0, 40.0) for mc in VALVES_5})


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------
def test_baseline_fit_healthy():
    """Fit su frame sano sintetico -> stats in banda, zero alert."""
    frames = _healthy_5()
    b = Baseline.fit(frames, CFG)
    assert b.top10 == VALVES_5
    for mc in VALVES_5:
        m, s = b.stats[mc]["fillingtime"]
        assert m == pytest.approx(1950.0, rel=0.01), mc
        assert 8.5 <= s <= 15.0, (mc, s)          # ~40/sqrt(12)=11.55
        assert b.stats[mc]["tailtime"][0] == pytest.approx(300.0, rel=0.02)
        assert b.stats[mc]["pulsecount"][0] == pytest.approx(2500.0, rel=0.01)
    assert b.ft_rate["valve0"] == 0.0
    assert b.tt_rate["valve0"] == 0.0
    # zero alert su se stessa (fit -> analyze -> health)
    h, counts = health(analyze(frames, b, CFG), stable_events(), b, CFG)
    assert h["machine_healthy"].all()
    for col in ("valve_alert", "xmr_alert", "sigma_alert", "rate_ft_alert",
                "rate_tt_alert", "warning", "aggregate_alert"):
        assert not h[col].any(), col
    assert counts[0]["n_cycles"] == len(VALVES_5) * 100


def test_baseline_not_self_updating():
    """Fit -> analyze/health/detect su frame degradato: baseline INVARIATA."""
    healthy = _healthy_5()
    b = Baseline.fit(healthy, CFG)
    snap = json.dumps(b.to_dict(), sort_keys=True)
    degraded = make_cycles(
        VALVES_5, 6, 100,
        {**{mc: (1950.0, 40.0) for mc in VALVES_5},
         "valve2": (2106.0, 40.0)})
    feats = analyze(degraded, b, CFG)
    health(feats, stable_events(), b, CFG)
    detect_faulted_valves(degraded, b, CFG)
    assert json.dumps(b.to_dict(), sort_keys=True) == snap
    # i riferimenti restano quelli del fit sano
    assert b.stats["valve2"]["fillingtime"][0] == pytest.approx(1950.0,
                                                                rel=0.01)


def test_baseline_save_load_roundtrip(tmp_path):
    """Serializzazione JSON riproducibile: save/load identici (repr round-trip)."""
    b = Baseline.fit(_healthy_5(), CFG)
    p = tmp_path / "baseline.json"
    b.save(p)
    b2 = Baseline.load(p)
    assert b2.to_dict() == b.to_dict()
    assert b2.window_cycles == b.window_cycles


# --------------------------------------------------------------------------
# Top-10 (tie-break deterministico: sigma FT asc, poi machine_code)
# --------------------------------------------------------------------------
def test_top10_selection():
    """Le 10 valvole con sigma FT minima (valve0..9 vs valve10..14)."""
    ft = {mc: (1950.0, 6.93) for mc in VALVES_15[:10]}       # sigma ~2
    ft.update({mc: (1950.0, 346.4) for mc in VALVES_15[10:]})  # sigma ~100
    b = Baseline.fit(make_cycles(VALVES_15, 6, 100, ft), CFG)
    assert b.top10 == VALVES_15[:10]
    assert len(b.top10) == 10


def test_top10_tie_break():
    """Sigma identiche -> tie-break per machine_code (ordinamento
    deterministico): con FT costante tutte le sigma sono 0.0 esatte."""
    tie = make_const_cycles(VALVES_15, 6, 100, 1950.0)
    b = Baseline.fit(tie, CFG)
    assert {b.stats[mc]["fillingtime"][1] for mc in VALVES_15} == {0.0}
    expected = sorted(VALVES_15)[:10]
    assert b.top10 == expected
    # deterministico: re-fit -> stesso set
    assert Baseline.fit(tie, CFG).top10 == expected


# --------------------------------------------------------------------------
# Features (analyze)
# --------------------------------------------------------------------------
def test_condition_monitoring_qualityok_suspect():
    """Caso chiave CONTEXT.md: FillQualityOK=TRUE + SUSPECT (coda sana).

    Conteggio cicli + quota per finestra, coerenti col conteggio diretto."""
    ft = {mc: (1950.0, 40.0) for mc in VALVES_7}
    ft["valve12"] = (1950.0, 150.0)      # coda sana: FT>2000 ~15%
    frames = make_cycles(VALVES_7, 6, 100, ft)
    b = Baseline.fit(frames, CFG)
    feats = analyze(frames, b, CFG)
    assert feats["qualityok_suspect"].dtype == pl.Boolean
    direct = frames.filter(
        (pl.col("fill_quality_ok") == pl.lit(True))
        & (pl.col("diagnostic_status") == pl.lit("SUSPECT"))).height
    assert int(feats["qualityok_suspect"].sum()) == direct > 0
    # per-finestra, dal frame grezzo (indipendente dal pipeline)
    raw = frames.sort(["machine_code", "ts_beg"]).with_columns(
        (pl.int_range(pl.len()).over("machine_code") // 100)
        .alias("window_id"))
    direct_w = raw.group_by(["window_id", "machine_code"]).agg(
        ((pl.col("fill_quality_ok") == pl.lit(True))
         & (pl.col("diagnostic_status") == pl.lit("SUSPECT")))
        .sum().alias("nq"),
        pl.len().alias("n"))
    h, _ = health(feats, stable_events(), b, CFG)
    for r in direct_w.iter_rows(named=True):
        row = h.filter((pl.col("window_id") == r["window_id"])
                       & (pl.col("machine_code") == r["machine_code"]))
        assert row["n_qualityok_suspect"][0] == r["nq"]
        assert row["quota_qualityok_suspect"][0] == pytest.approx(
            r["nq"] / r["n"], abs=1e-9)


def test_xmr_limits_exact():
    """UCL/LCL = xbar +- 2.66*MRbar su serie nota (valori esatti) ed
    escursione rilevata (medie di finestra, NON cicli grezzi)."""

    def const_win(means):
        rows, cid = [], 0
        for wi, m in enumerate(means):
            for i in range(100):
                rows.append({
                    "machine_code": "valve0",
                    "ts_beg": T0 + dt.timedelta(seconds=(wi * 100 + i) * 3.2),
                    "fillingtime": float(m), "tailtime": 300.0,
                    "tailpulse": 30.0, "pulsecount": 2500.0, "target": 2500.0,
                    "deltapulse": 0.0, "filling_step_out": 26,
                    "fillingok": True, "fill_quality_ok": True,
                    "sequence_ok": True, "sample_valid": True,
                    "diagnostic_status": "NORMAL", "close_reason": "target",
                    "position_limit": False, "filling_overtime": False,
                    "cycle_id": cid, "scenario_id": 1,
                })
                cid += 1
        return pl.DataFrame(rows)

    b = Baseline.fit(const_win([100.0, 120.0, 140.0]), CFG)
    xbar, mr = b.xmr["valve0"]
    assert xbar == pytest.approx(120.0, abs=1e-9)
    assert mr == pytest.approx(20.0, abs=1e-9)
    run = const_win([100.0, 120.0, 140.0, 200.0, 200.0])
    feats = analyze(run, b, CFG)
    assert feats["xmr_ucl"].unique().to_list() == pytest.approx(
        [120.0 + 2.66 * 20.0], abs=1e-9)
    assert feats["xmr_lcl"].unique().to_list() == pytest.approx(
        [120.0 - 2.66 * 20.0], abs=1e-9)
    exc = feats.group_by("window_id").agg(
        pl.col("xmr_excursion").all().alias("all"),
        pl.col("xmr_excursion").any().alias("any")).sort("window_id")
    by_wid = {r["window_id"]: (r["all"], r["any"])
              for r in exc.iter_rows(named=True)}
    assert by_wid[4] == (True, True)      # finestra a 200: escursione piena
    assert by_wid[0] == (False, False)
    assert by_wid[1] == (False, False)
    assert by_wid[2] == (False, False)
    h, _ = health(feats, stable_events(), b, CFG)
    xa = h.select(["window_id", "xmr_alert"]).unique().sort("window_id")
    assert {r["window_id"]: r["xmr_alert"] for r in xa.iter_rows(named=True)} \
        == {0: False, 1: False, 2: False, 3: True, 4: True}


def test_fixed_thresholds_ft_tt():
    """Conta FT>2000/TT>600 coerente col conteggio diretto sui flag
    diagnostici del frame (coerenza, non doppia logica)."""
    ft = {mc: (1950.0, 40.0) for mc in VALVES_7}
    ft["valve12"] = (1950.0, 150.0)       # coda FT>2000
    tt = {mc: (300.0, 44.0) for mc in VALVES_7}
    tt["valve3"] = (590.0, 100.0)         # coda TT>600
    frames = make_cycles(VALVES_7, 6, 100, ft, tt_cfg=tt)
    b = Baseline.fit(frames, CFG)
    feats = analyze(frames, b, CFG)
    n_ft = int(feats["ft_over_2000"].sum())
    n_tt = int(feats["tt_over_600"].sum())
    assert n_ft == int((frames["fillingtime"] > 2000).sum())
    assert n_ft == int((frames["filling_overtime"] == True).sum())
    assert n_tt == int((frames["tailtime"] > 600).sum())
    assert n_tt == int((frames["sequence_ok"] == False).sum())
    assert n_ft > 0 and n_tt > 0


# --------------------------------------------------------------------------
# Health: alert
# --------------------------------------------------------------------------
def test_alert_aggregate_top10_pct():
    """F1-a: alert aggregato quando la media top-10 > baseline*(1+pct)."""
    ft = {mc: (1950.0, 6.93) for mc in VALVES_15[:10]}
    ft.update({mc: (1950.0, 346.4) for mc in VALVES_15[10:]})
    healthy = make_cycles(VALVES_15, 6, 100, ft)
    b = Baseline.fit(healthy, CFG)
    assert b.top10 == VALVES_15[:10]
    degraded = make_cycles(
        VALVES_15, 6, 100,
        {**{mc: (2106.0, 6.93) for mc in VALVES_15[:10]},
         **{mc: (1950.0, 346.4) for mc in VALVES_15[10:]}})
    hd, _ = health(analyze(degraded, b, CFG), stable_events(), b, CFG)
    hh, _ = health(analyze(healthy, b, CFG), stable_events(), b, CFG)
    assert hd["aggregate_alert"].all()
    assert not hh["aggregate_alert"].any()
    assert not hd["machine_healthy"].any()
    assert hh["machine_healthy"].all()


def test_alert_per_valve_local():
    """F1-b: shift +8% su UNA valvola -> alert per-valvola, NESSUN aggregato
    (deriva top-10 diluita ~10x)."""
    healthy = _healthy_5()
    ft = {mc: (1950.0, 40.0) for mc in VALVES_5}
    ft["valve2"] = (2106.0, 40.0)
    degraded = make_cycles(VALVES_5, 6, 100, ft)
    b = Baseline.fit(healthy, CFG)
    hd, _ = health(analyze(degraded, b, CFG), stable_events(), b, CFG)
    flagged = set(hd.filter(pl.col("valve_alert"))
                  .unique(subset=["machine_code"])["machine_code"].to_list())
    assert flagged == {"valve2"}
    assert not hd["aggregate_alert"].any()
    hh, _ = health(analyze(healthy, b, CFG), stable_events(), b, CFG)
    assert not hh["valve_alert"].any()


def test_rate_based_ft_alert_healthy_tail():
    """F3: coda sana FT>2000 (~15%) NON allarma (alert rate-based); solo
    sopra rate sano + margine scatta l'alert (le soglie fisse restano
    diagnostiche per-ciclo)."""
    ft = {mc: (1950.0, 40.0) for mc in VALVES_7}
    ft["valve8"] = (2034.0, 120.0)        # profilo valve8/20: coda lunga
    ft["valve12"] = (1950.0, 150.0)       # coda sana ~15% FT>2000
    healthy = make_cycles(VALVES_7, 8, 100, ft)
    b = Baseline.fit(healthy, CFG)
    assert 0.10 <= b.ft_rate["valve12"] <= 0.25       # rate sano ~15%
    # frame sano: flag diagnostici presenti, ZERO alert rate-based
    hh, _ = health(analyze(healthy, b, CFG), stable_events(), b, CFG)
    assert not hh["rate_ft_alert"].any()
    assert not hh["rate_tt_alert"].any()
    assert hh["machine_healthy"].all()
    feats = analyze(healthy, b, CFG)
    assert int(feats["ft_over_2000"].sum()) > 0       # diagnostica per-ciclo
    # degrado: rate FT>2000 sopra sano+SE-margine -> alert rate-based (la
    # media resta sotto la soglia per-valvola: l'alert NON viene dal shift
    # medio; le valvole sane restano sotto il margine effettivo)
    ft_d = {**ft, "valve12": (1990.0, 150.0)}         # rate ~0.43 > 0.28
    degraded = make_cycles(VALVES_7, 8, 100, ft_d)
    hd, _ = health(analyze(degraded, b, CFG), stable_events(), b, CFG)
    v12 = hd.filter(pl.col("machine_code") == "valve12")
    assert v12["rate_ft_alert"].all()
    assert not v12["valve_alert"].any()
    others = hd.filter(pl.col("machine_code") != "valve12")
    assert not others["rate_ft_alert"].any()


def test_sigma_ratio_flags_only_sigma():
    """F4 (classe M3): sigma FT x1.8 a media invariata -> sigma-alert sulla
    valvola affetta, sane NO."""
    ft = {mc: (1000.0, 17.32) for mc in VALVES_5}     # sigma ~5
    healthy = make_cycles(VALVES_5, 6, 100, ft)
    b = Baseline.fit(healthy, CFG)
    ft_d = {**ft, "valve2": (1000.0, 31.18)}          # sigma ~9 (x1.8)
    degraded = make_cycles(VALVES_5, 6, 100, ft_d)
    hd, _ = health(analyze(degraded, b, CFG), stable_events(), b, CFG)
    flagged = set(hd.filter(pl.col("sigma_alert"))
                  .unique(subset=["machine_code"])["machine_code"].to_list())
    assert flagged == {"valve2"}
    v2 = hd.filter(pl.col("machine_code") == "valve2")
    assert v2["sigma_alert"].all()
    assert not v2["valve_alert"].any()                # media invariata


def test_trend_warning_numeric():
    """F5: warning numerico — offset >= warn_pct sostenuto su trend_window
    finestre consecutive; 0 su sano."""
    cfgw = AnalyticsConfig(window_cycles=20, trend_window=20,
                           stable_min_minutes=1)
    healthy = make_cycles(VALVES_5, 22, 20,
                          {mc: (1000.0, 17.32) for mc in VALVES_5})
    b = Baseline.fit(healthy, cfgw)
    warn_frame = make_cycles(VALVES_5, 22, 20,
                             {mc: (1050.0, 17.32) for mc in VALVES_5})
    hw, _ = health(analyze(warn_frame, b, cfgw), stable_events(), b, cfgw)
    per_w = hw.group_by("window_id").agg(
        pl.col("warning").all().alias("all")).sort("window_id")
    by_wid = {r["window_id"]: r["all"] for r in per_w.iter_rows(named=True)}
    assert not any(by_wid[w] for w in range(19))      # prima di 20 finestre
    assert all(by_wid[w] for w in range(19, 22))      # dalla 20a in poi
    hh, _ = health(analyze(healthy, b, cfgw), stable_events(), b, cfgw)
    assert not hh["warning"].any()
    # la valvola non scatta per-valvola: offset 5% < 6% (warning != alert)
    assert not hh["valve_alert"].any()


# --------------------------------------------------------------------------
# Health: MachineStable (events STATE:*, machine_code MACHINE)
# --------------------------------------------------------------------------
def test_machine_stable_from_events():
    """Timeline STATE:* -> MachineStable = Running da >= stable_min_minutes
    senza transizioni Starting/Stopping/Stopped nella finestra."""
    t0 = T0
    events = pl.DataFrame([
        {"ts_beg": t0 - dt.timedelta(minutes=5), "machine_code": "MACHINE",
         "event": "STATE:Starting", "note": "", "cycle_id": 0,
         "scenario_id": 1},
        {"ts_beg": t0 - dt.timedelta(minutes=4), "machine_code": "MACHINE",
         "event": "STATE:Running", "note": "", "cycle_id": 0,
         "scenario_id": 1},
        {"ts_beg": t0 + dt.timedelta(minutes=42), "machine_code": "MACHINE",
         "event": "STATE:Stopping", "note": "", "cycle_id": 0,
         "scenario_id": 1},
    ])
    # finestre che terminano a 10 / 40 / 45 min: attesi [F, T, F]
    frames = make_cycles(["valve0"], 3, 100, {"valve0": (1950.0, 40.0)},
                         t0=t0, wid_end_min=[10.0, 40.0, 45.0])
    b = Baseline.fit(frames, CFG_30)
    h, _ = health(analyze(frames, b, CFG_30), events, b, CFG_30)
    rows = h.select(["window_id", "machine_stable", "machine_healthy"]) \
        .unique().sort("window_id")
    out = {r["window_id"]: (r["machine_stable"], r["machine_healthy"])
           for r in rows.iter_rows(named=True)}
    assert out[0] == (False, False)     # Running da 14 min < 30
    assert out[1] == (True, True)       # Running da 44 min >= 30
    assert out[2] == (False, False)     # Stopping nella finestra
    # senza eventi MACHINE -> mai stabile
    empty_events = pl.DataFrame(schema={
        "machine_code": pl.String, "event": pl.String,
        "ts_beg": pl.Datetime(time_unit="us", time_zone="UTC"),
    })
    h2, _ = health(analyze(frames, b, CFG_30), empty_events, b, CFG_30)
    assert not h2["machine_stable"].any()


# --------------------------------------------------------------------------
# Detector (port D5, F6)
# --------------------------------------------------------------------------
def test_detector_port_equivalence():
    """Port D5 (tests/test_fault_integration.py) su fixture IDENTICHE ->
    stesso set flaggato; mappa estesa flowmeter_glitch -> pulsecount."""
    ft_h = {mc: (1000.0, 17.32) for mc in VALVES_5}
    tt_h = {mc: (300.0, 5.0) for mc in VALVES_5}
    pc_h = {mc: (2500.0, 3.0) for mc in VALVES_5}
    healthy = make_cycles(VALVES_5, 3, 100, ft_h, tt_cfg=tt_h, pc_cfg=pc_h)
    ft_f = dict(ft_h)
    ft_f.update({"valve1": (1150.0, 17.32), "valve3": (1100.0, 17.32)})
    tt_f = dict(tt_h)
    tt_f["valve2"] = (360.0, 5.0)       # closing_delay -> tailtime
    faulted = make_cycles(VALVES_5, 3, 100, ft_f, tt_cfg=tt_f, pc_cfg=pc_h)
    b = Baseline.fit(healthy, CFG)
    primary = {"restriction": "fillingtime",
               "closing_delay": "tailtime",
               "opening_delay": "fillingtime"}
    mine = detect_faulted_valves(faulted, b, CFG)
    theirs = d5_port(faulted, healthy, primary)
    assert mine == theirs == {"valve1", "valve2", "valve3"}
    # mappa estesa: flowmeter_glitch -> pulsecount (D5 test-only non la copre)
    ft_g = dict(ft_h)
    ft_g["valve4"] = (1000.0, 17.32)
    pc_g = dict(pc_h)
    pc_g["valve4"] = (2875.0, 3.0)      # PC +15%
    glitch = make_cycles(VALVES_5, 3, 100, ft_g, tt_cfg=tt_h, pc_cfg=pc_g)
    assert detect_faulted_valves(glitch, b, CFG) == {"valve4"}
    assert d5_port(glitch, healthy, primary) == set()


# --------------------------------------------------------------------------
# Determinismo / isolamento / GT (ADR-0012)
# --------------------------------------------------------------------------
def test_determinism_fingerprint():
    """SHA-256 su features/health a parita' di (input, baseline, cfg)."""
    frames = _healthy_5()
    b = Baseline.fit(frames, CFG)
    ev = stable_events()

    def fp(feats, hdf):
        h1 = hashlib.sha256(feats.write_csv().encode()).hexdigest()
        h2 = hashlib.sha256(hdf.write_csv().encode()).hexdigest()
        return h1, h2

    f1 = analyze(frames, b, CFG)
    hd1, _ = health(f1, ev, b, CFG)
    f2 = analyze(frames, b, CFG)
    hd2, _ = health(f2, ev, b, CFG)
    assert fp(f1, hd1) == fp(f2, hd2)
    shifted = make_cycles(VALVES_5, 6, 100,
                          {**{mc: (1950.0, 40.0) for mc in VALVES_5},
                           "valve2": (2106.0, 40.0)})
    f3 = analyze(shifted, b, CFG)
    hd3, _ = health(f3, ev, b, CFG)
    assert fp(f1, hd1) != fp(f3, hd3)


def test_no_write_on_input_parquet(tmp_path):
    """Hash dei parquet input PRIMA/DOPO analyze identici; scan sorgente:
    nessuna scrittura su path di simulazione (solo out_dir)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    frames = _healthy_5()
    events = stable_events()
    frames.write_parquet(run_dir / "valve_cycles.parquet")
    events.write_parquet(run_dir / "events.parquet")

    def hashes() -> dict:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(run_dir.iterdir())}

    before = hashes()
    b = Baseline.fit(frames, CFG)
    feats = analyze(frames, b, CFG)
    health(feats, events, b, CFG)
    detect_faulted_valves(frames, b, CFG)
    assert hashes() == before
    # scan sorgente: le sole write_parquet sono su out_dir; mai su run_dir
    src = (ROOT / "plcsim" / "analytics.py").read_text(encoding="utf-8")
    for ln in src.splitlines():
        if "write_parquet" in ln:
            assert "out_dir" in ln, ln
        if "run_dir" in ln:
            assert "write_parquet" not in ln, ln
            assert "write_text" not in ln, ln


def test_gt_never_in_decision_path():
    """ADR-0012: le firme analytics non ricevono GT; scan sorgente."""
    src = (ROOT / "plcsim" / "analytics.py").read_text(encoding="utf-8")
    assert "ground_truth" not in src
    assert "fault_type" not in src
    for fn in (analyze, health, detect_faulted_valves, Baseline.fit):
        for pname in inspect.signature(fn).parameters:
            low = pname.lower()
            assert "gt" not in low and "truth" not in low \
                and "fault" not in low, (fn.__name__, pname)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_cli_fit_analyze_no_write_run_dir(tmp_path):
    """--fit-baseline + --cycles: output su out_dir SEPARATA; run_dir
    intoccabile (hash identici)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    frames = _healthy_5()
    # Running da 60 min prima della prima finestra: stabile su TUTTE le
    # finestre anche con la regola default stable_min_minutes=30 del CLI
    t_running = T0 - dt.timedelta(minutes=60)
    events = pl.DataFrame([
        {"ts_beg": t_running - dt.timedelta(minutes=2),
         "machine_code": "MACHINE", "event": "STATE:Starting",
         "note": "", "cycle_id": 0, "scenario_id": 1},
        {"ts_beg": t_running, "machine_code": "MACHINE",
         "event": "STATE:Running", "note": "", "cycle_id": 0,
         "scenario_id": 1},
    ])
    frames.write_parquet(run_dir / "valve_cycles.parquet")
    events.write_parquet(run_dir / "events.parquet")

    def hashes() -> dict:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(run_dir.iterdir())}

    before = hashes()
    baseline_path = tmp_path / "baseline.json"
    out = tmp_path / "out"
    rc = main(["--fit-baseline",
               str(run_dir / "valve_cycles.parquet"),
               "--baseline", str(baseline_path)])
    assert rc == 0 and baseline_path.exists()
    rc = main(["--cycles", str(run_dir), "--baseline", str(baseline_path),
               "--out", str(out)])
    assert rc == 0
    assert (out / "features.parquet").exists()
    assert (out / "health.parquet").exists()
    assert (out / "summary.json").exists()
    assert hashes() == before
    assert out != run_dir
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert len(summary["windows"]) == 6
    assert len(summary["counts"]) == 6
    feats = pl.read_parquet(out / "features.parquet")
    assert feats.height == frames.height
    hdf = pl.read_parquet(out / "health.parquet")
    assert hdf["machine_healthy"].all()
