"""Layer 7 — Analytics (ADR-0012): baseline, features, health, detector.

Modulo READ-ONLY sopra la telemetria (layer 6): non scrive MAI nei parquet di
simulazione e la GT non entra nel percorso decisionale (solo eval/test).

Contratto M5 (work/reviews/plan-m5/converged-feedback.md, fix M5-F1..F7):
  - M5-F1: alert AGGREGATO top-10 (deriva macchina) E variante PER-VALVOLA
    (fault locali); soglie numeriche in AnalyticsConfig (candidate, calibrate
    in W3 — il worker NON le cambia).
  - M5-F3: le soglie fisse FT<=2000/TT<=600 restano DIAGNOSTICHE per-ciclo
    (feature), l'alert e' RATE-based (rate di finestra > rate sano baseline +
    margine) per non allarmare sulla coda sana (valve12 ~15% FT>2000).
  - M5-F4: degrado solo-sigma (classe M3) flaggato dal sigma-ratio
    (sigma di finestra / sigma baseline) >= sigma_ratio_alert.
  - M5-F5: warning numerico: offset medio di finestra vs baseline >= warn_pct
    sostenuto su trend_window finestre consecutive (regola numerica, niente
    "da definire a valle").
  - M5-F6: detector D5 portato con baseline CONGELATA; il fattore sqrt(2)
    del test-only stesso-seed e' conservativo quando n_analyzed != n_baseline
    (bias documentato nella docstring di detect_faulted_valves).

Funzioni pure polars, nessun RNG, deterministiche (SHA-256 verificato in
tests/test_m5_analytics.py). Serializzazione baseline: JSON sidecar
riproducibile (schema_version, sort_keys, nessun arrotondamento: il repr
float di Python round-trip esatto).
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import polars as pl

# KPI coperti dalle statistiche della baseline (mean/std per-valvola).
KPI_MEANS = ("fillingtime", "tailtime", "pulsecount")

# Mappa KPI primario per tipo di guasto (port D5 esteso, mai usata con GT):
# restriction -> FT, closing_delay -> TT, opening_delay -> FT,
# flowmeter_dropout -> FT, flowmeter_glitch -> PC.
DETECTOR_KPI_MAP = {
    "restriction": "fillingtime",
    "closing_delay": "tailtime",
    "opening_delay": "fillingtime",
    "flowmeter_dropout": "fillingtime",
    "flowmeter_glitch": "pulsecount",
}

_UNSTABLE_STATES = ("Starting", "Stopping", "Stopped")


@dataclass(frozen=True)
class AnalyticsConfig:
    """Soglie e parametri dell'analytics — valori CANDIDATI (calibrati in W3).

    Il worker NON cambia questi default: sono override-abili via costruttore
    (i test usano finestre piu' piccole per restare sotto il minuto).
    """

    window_cycles: int = 100        # cicli per finestra (rolling + health)
    top_n: int = 10                 # valvole piu' stabili (sigma FT minima)
    alert_pct_agg: float = 0.06     # alert aggregato top-10 (+6%, deriva macchina)
    alert_pct_valve: float = 0.06   # alert per-valvola (+6%, fault locali)
    detector_k: float = 3.0         # z del detector D5
    xmr_k: float = 2.66             # UCL/LCL XmR = xbar +- 2.66*MRbar
    ft_limit_ms: int = 2000         # soglia diagnostica FT (NON chiusura)
    tt_limit_ms: int = 600          # soglia diagnostica TT
    sigma_ratio_alert: float = 1.5  # sigma finestra / sigma baseline
    warn_pct: float = 0.03          # offset minimo per warning
    trend_window: int = 20          # finestre consecutive per warning
    stable_min_minutes: int = 30    # Running continuo per MachineStable
    ft_rate_margin: float = 0.05    # margine rate FT>2000 sopra il sano
    tt_rate_margin: float = 0.001   # margine rate TT>600 sopra il sano


class Baseline:
    """Baseline statistica sana, CONGELATA alla costruzione.

    analyze/health/detect_faulted_valves non la mutano MAI (test dedicato
    test_baseline_not_self_updating): il confronto di un degrado usa i
    parametri fissati al momento del fit.

    Serializzazione: JSON riproducibile (to_dict/save, sort_keys, nessun
    arrotondamento dei float).
    """

    SCHEMA_VERSION = 1

    def __init__(self, stats, top10, xmr, ft_rate, tt_rate, n_cycles,
                 window_cycles: int):
        self._stats = {mc: {k: (float(m), float(s)) for k, (m, s) in d.items()}
                       for mc, d in stats.items()}
        self._top10 = tuple(sorted(top10))
        self._xmr = {mc: (float(x), float(mr)) for mc, (x, mr) in xmr.items()}
        self._ft_rate = {mc: float(r) for mc, r in ft_rate.items()}
        self._tt_rate = {mc: float(r) for mc, r in tt_rate.items()}
        self._n_cycles = {mc: int(n) for mc, n in n_cycles.items()}
        self.window_cycles = int(window_cycles)

    # -- accessori (copie: l'oggetto resta congelato) -----------------------
    @property
    def stats(self) -> dict:
        """{machine_code: {kpi: (mean, std)}} — copia difensiva."""
        return {mc: dict(d) for mc, d in self._stats.items()}

    @property
    def top10(self) -> list:
        """Top-N valvole piu' stabili (sigma FT minime, tie-break deterministico)."""
        return list(self._top10)

    @property
    def xmr(self) -> dict:
        """{machine_code: (xbar, MRbar)} sulle medie di finestra FT."""
        return dict(self._xmr)

    @property
    def ft_rate(self) -> dict:
        """Rate sano FT>2000 per-valvola (media sulle finestre della baseline)."""
        return dict(self._ft_rate)

    @property
    def tt_rate(self) -> dict:
        """Rate sano TT>600 per-valvola (media sulle finestre della baseline)."""
        return dict(self._tt_rate)

    @property
    def n_cycles(self) -> dict:
        return dict(self._n_cycles)

    # -- fit ----------------------------------------------------------------
    @classmethod
    def fit(cls, cycles_df: pl.DataFrame, cfg: AnalyticsConfig) -> "Baseline":
        """Fit delle statistiche sane per-valvola dal frame di cicli.

        - stats: mean/std di fillingtime/tailtime/pulsecount per-valvola;
        - top-N: valvole con sigma FT minima (tie-break: sigma asc, poi
          machine_code);
        - xmr: xbar e MRbar delle medie di finestra (window_cycles cicli)
          di fillingtime per-valvola (mitigazione bimodalita' R5: medie di
          finestra, NON cicli grezzi);
        - ft_rate/tt_rate: rate sani per-finestra (FT>2000, TT>600) medi
          sulle finestre della baseline.
        """
        df = _prepare_cycles(cycles_df)
        valves = sorted(df["machine_code"].unique().to_list())
        stats: dict = {}
        for mc in valves:
            sub = df.filter(pl.col("machine_code") == mc)
            stats[mc] = {}
            for kpi in KPI_MEANS:
                mean_v = sub[kpi].mean()
                std_v = sub[kpi].std()
                stats[mc][kpi] = (
                    float(mean_v) if mean_v is not None else 0.0,
                    float(std_v) if std_v is not None else 0.0,
                )
        ranked = sorted(valves,
                        key=lambda mc: (stats[mc]["fillingtime"][1], mc))
        top10 = ranked[: cfg.top_n]
        xmr = {mc: _window_xmr(df.filter(pl.col("machine_code") == mc),
                               cfg.window_cycles)
               for mc in valves}
        ft_rate, tt_rate = {}, {}
        for mc in valves:
            fr, tr = _window_rates(
                df.filter(pl.col("machine_code") == mc), cfg)
            ft_rate[mc], tt_rate[mc] = fr, tr
        n_cycles = {mc: int(df.filter(pl.col("machine_code") == mc).height)
                    for mc in valves}
        return cls(stats, top10, xmr, ft_rate, tt_rate, n_cycles,
                   cfg.window_cycles)

    # -- serializzazione riproducibile --------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "window_cycles": self.window_cycles,
            "stats": {mc: {k: [v[0], v[1]] for k, v in d.items()}
                      for mc, d in self._stats.items()},
            "top10": list(self._top10),
            "xmr": {mc: [x, m] for mc, (x, m) in self._xmr.items()},
            "ft_rate": dict(self._ft_rate),
            "tt_rate": dict(self._tt_rate),
            "n_cycles": dict(self._n_cycles),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Baseline":
        stats = {mc: {k: tuple(v) for k, v in d.items()}
                 for mc, d in payload["stats"].items()}
        xmr = {mc: tuple(v) for mc, v in payload["xmr"].items()}
        return cls(stats, payload["top10"], xmr, payload["ft_rate"],
                   payload["tt_rate"], payload["n_cycles"],
                   payload.get("window_cycles", 100))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False),
            encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Baseline":
        return cls.from_dict(
            json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# helpers interni (polars puri, deterministici)
# --------------------------------------------------------------------------
def _prepare_cycles(cycles_df: pl.DataFrame) -> pl.DataFrame:
    """Colonne KPI/quartetto tipizzate e ordinate (machine_code, ts_beg)."""
    cols = ["machine_code", "ts_beg", "fillingtime", "tailtime", "tailpulse",
            "pulsecount", "fill_quality_ok", "diagnostic_status",
            "filling_overtime", "sequence_ok", "cycle_id", "scenario_id",
            "target"]
    out = cycles_df.select(cols).with_columns([
        pl.col("fillingtime").cast(pl.Float64),
        pl.col("tailtime").cast(pl.Float64),
        pl.col("tailpulse").cast(pl.Float64),
        pl.col("pulsecount").cast(pl.Float64),
        pl.col("fill_quality_ok").cast(pl.Boolean),
        pl.col("diagnostic_status").cast(pl.String),
    ])
    return out.sort(["machine_code", "ts_beg"])


def _window_xmr(sub: pl.DataFrame, w: int) -> tuple[float, float]:
    """(xbar, MRbar) delle medie di finestra FT della baseline."""
    if sub.height == 0:
        return (0.0, 0.0)
    means = (sub.with_columns((pl.int_range(pl.len()) // w).alias("_wid"))
             .group_by("_wid")
             .agg(pl.col("fillingtime").mean().alias("m"))
             .sort("_wid")["m"])
    if means.is_empty():
        return (0.0, 0.0)
    xbar = float(means.mean())
    if means.len() < 2:
        return (xbar, 0.0)
    mr = float(means.diff().abs().drop_nulls().mean())
    return (xbar, mr)


def _window_rates(sub: pl.DataFrame, cfg: AnalyticsConfig) -> tuple[float, float]:
    """Rate sani per-finestra (FT>2000, TT>600) mediati sulle finestre."""
    w = cfg.window_cycles
    agg = (sub.with_columns((pl.int_range(pl.len()) // w).alias("_wid"))
           .group_by("_wid")
           .agg((pl.col("fillingtime") > cfg.ft_limit_ms).mean().alias("fr"),
                (pl.col("tailtime") > cfg.tt_limit_ms).mean().alias("tr"))
           .sort("_wid"))
    if agg.height == 0:
        return (0.0, 0.0)
    return (float(agg["fr"].mean()), float(agg["tr"].mean()))


def _frame_ts_dt(df: pl.DataFrame) -> pl.DataFrame:
    """Normalizza ts_beg a datetime (fallback int ms per i frame sintetici)."""
    dtype = df["ts_beg"].dtype
    if dtype == pl.Datetime:
        return df
    if dtype in (pl.Int8, pl.Int16, pl.Int32, pl.Int64,
                 pl.UInt32, pl.UInt64):
        return df.with_columns(
            pl.from_epoch(pl.col("ts_beg"), time_unit="ms")
            .dt.replace_time_zone("UTC").alias("ts_beg"))
    raise TypeError(f"ts_beg non supportato: {dtype}")


def _baseline_params(baseline: Baseline, cfg: AnalyticsConfig) -> pl.DataFrame:
    """Tabella per-valvola dei riferimenti baseline (join in analyze/health)."""
    rows = []
    for mc, st in baseline.stats.items():
        xm = baseline.xmr[mc]
        rows.append({
            "machine_code": mc,
            "b_ft_mean": st["fillingtime"][0],
            "b_ft_std": st["fillingtime"][1],
            "b_tt_mean": st["tailtime"][0],
            "b_tt_std": st["tailtime"][1],
            "b_pc_mean": st["pulsecount"][0],
            "b_pc_std": st["pulsecount"][1],
            "b_ft_rate": baseline.ft_rate[mc],
            "b_tt_rate": baseline.tt_rate[mc],
            "xmr_ucl": xm[0] + cfg.xmr_k * xm[1],
            "xmr_lcl": xm[0] - cfg.xmr_k * xm[1],
        })
    return pl.DataFrame(rows)


# --------------------------------------------------------------------------
# API pubblica
# --------------------------------------------------------------------------
def analyze(cycles_df: pl.DataFrame, baseline: Baseline,
            cfg: AnalyticsConfig) -> pl.DataFrame:
    """Features per-ciclo (layer 7, READ-ONLY, deterministico).

    - rolling mean/std di fillingtime/tailtime/tailpulse/pulsecount su
      window_cycles (per-valvola, ordine temporale);
    - z-score vs baseline (rolling mean, per-valvola);
    - flag diagnostici per-ciclo: ft_over_2000 (FT>ft_limit_ms),
      tt_over_600, qualityok_suspect (fill_quality_ok=True AND
      diagnostic_status=="SUSPECT" — il segnale del condition monitoring,
      CONTEXT.md);
    - XmR: UCL/LCL per-valvola = xbar +- 2.66*MRbar (medie di finestra della
      baseline, mitigazione bimodalita' R5) + flag xmr_excursion;
    - sigma_ratio: rolling sigma FT / sigma baseline.
    """
    df = _prepare_cycles(cycles_df)
    w = cfg.window_cycles
    f = df.with_columns([
        pl.int_range(pl.len()).over("machine_code").alias("_pos"),
        pl.col("fillingtime").rolling_mean(window_size=w)
        .over("machine_code").alias("ft_roll_mean"),
        pl.col("fillingtime").rolling_std(window_size=w)
        .over("machine_code").alias("ft_roll_std"),
        pl.col("tailtime").rolling_mean(window_size=w)
        .over("machine_code").alias("tt_roll_mean"),
        pl.col("tailtime").rolling_std(window_size=w)
        .over("machine_code").alias("tt_roll_std"),
        pl.col("tailpulse").rolling_mean(window_size=w)
        .over("machine_code").alias("tp_roll_mean"),
        pl.col("tailpulse").rolling_std(window_size=w)
        .over("machine_code").alias("tp_roll_std"),
        pl.col("pulsecount").rolling_mean(window_size=w)
        .over("machine_code").alias("pc_roll_mean"),
        pl.col("pulsecount").rolling_std(window_size=w)
        .over("machine_code").alias("pc_roll_std"),
    ])
    f = f.join(_baseline_params(baseline, cfg), on="machine_code", how="left")
    f = f.with_columns([
        ((pl.col("ft_roll_mean") - pl.col("b_ft_mean"))
         / pl.col("b_ft_std")).alias("ft_z"),
        ((pl.col("tt_roll_mean") - pl.col("b_tt_mean"))
         / pl.col("b_tt_std")).alias("tt_z"),
        ((pl.col("pc_roll_mean") - pl.col("b_pc_mean"))
         / pl.col("b_pc_std")).alias("pc_z"),
        ((pl.col("ft_roll_mean") > pl.col("xmr_ucl"))
         | (pl.col("ft_roll_mean") < pl.col("xmr_lcl")))
        .fill_null(False).alias("xmr_excursion"),
        (pl.col("ft_roll_std") / pl.col("b_ft_std")).alias("sigma_ratio"),
        (pl.col("fillingtime") > cfg.ft_limit_ms).alias("ft_over_2000"),
        (pl.col("tailtime") > cfg.tt_limit_ms).alias("tt_over_600"),
        ((pl.col("fill_quality_ok") == pl.lit(True))
         & (pl.col("diagnostic_status") == pl.lit("SUSPECT")))
        .alias("qualityok_suspect"),
        (pl.col("_pos") // w).alias("window_id"),
    ])
    keep = [
        "machine_code", "ts_beg", "cycle_id", "scenario_id",
        "fillingtime", "tailtime", "tailpulse", "pulsecount",
        "fill_quality_ok", "diagnostic_status", "filling_overtime",
        "sequence_ok",
        "ft_roll_mean", "ft_roll_std", "tt_roll_mean", "tt_roll_std",
        "tp_roll_mean", "tp_roll_std", "pc_roll_mean", "pc_roll_std",
        "ft_z", "tt_z", "pc_z", "xmr_ucl", "xmr_lcl", "xmr_excursion",
        "sigma_ratio", "ft_over_2000", "tt_over_600", "qualityok_suspect",
        "window_id",
    ]
    return f.select(keep)


def health(features_df: pl.DataFrame, events_df: pl.DataFrame,
           baseline: Baseline, cfg: AnalyticsConfig) -> tuple:
    """Summary per-finestra: MachineStable/MachineHealthy, warning, alert.

    Ritorna (health_df, counts):
    - health_df: una riga per (window_id, machine_code) con i flag di
      finestra (MachineStable/MachineHealthy, alert, warning, quote);
    - counts: dict window_id -> conteggi/quote machine-level per il
      condition monitoring (n_cycles, n_qualityok_suspect,
      quota_qualityok_suspect, n_ft_over, n_tt_over).

    MachineStable (da events STATE:* con machine_code=="MACHINE"): lo stato
    corrente a fine finestra e' Running da >= stable_min_minutes senza
    transizioni a Starting/Stopping/Stopped nella finestra.
    MachineHealthy = MachineStable AND nessun alert nella finestra.

    Alert:
    (a) AGGREGATO top-10: media top-10 (medie di finestra) >
        baseline_top10 * (1 + alert_pct_agg) — deriva macchina (F1);
    (b) PER-VALVOLA: media di finestra FT/TT/PC > baseline * (1 +
        alert_pct_valve) — fault locali (F1);
    (c) XmR escursione: media di finestra FT fuori [LCL, UCL];
    (d) sigma-ratio: sigma di finestra FT >= baseline sigma * sigma_ratio_alert
        (F4, classe M3);
    (e) RATE-based FT/TT: rate di finestra FT>2000 (TT>600) > rate sano
        baseline + margine EFFETTIVO. Il margine effettivo e'
        max(margine congelato, detector_k * SE_binomiale) con
        SE = sqrt(p*(1-p)/window_cycles) sul rate sano p: il rumore di
        campionamento del rate di finestra e' binomiale (~1.2-1.4 sigma
        del margine congelato sulle valvole a rate sano alto, es. valve8/20
        ~0.58) — il floor 3 sigma (stessa convenzione del progetto: D5,
        XmR) rende FP=0 su healthy robusto (calibrazione W3 "con SE/margine",
        M5-F3). Le soglie fisse restano diagnostiche per-ciclo.
    """
    f = _frame_ts_dt(features_df)
    g = f.group_by(["window_id", "machine_code"]).agg(
        pl.col("fillingtime").mean().alias("ft_mean"),
        pl.col("fillingtime").std().alias("ft_std"),
        pl.col("tailtime").mean().alias("tt_mean"),
        pl.col("pulsecount").mean().alias("pc_mean"),
        (pl.col("fillingtime") > cfg.ft_limit_ms).mean().alias("ft_rate"),
        (pl.col("tailtime") > cfg.tt_limit_ms).mean().alias("tt_rate"),
        (pl.col("fillingtime") > cfg.ft_limit_ms).sum().alias("n_ft_over"),
        (pl.col("tailtime") > cfg.tt_limit_ms).sum().alias("n_tt_over"),
        pl.col("qualityok_suspect").sum().alias("n_qualityok_suspect"),
        pl.len().alias("n_cycles"),
    )
    g = g.join(_baseline_params(baseline, cfg), on="machine_code", how="left")
    g = g.with_columns([
        ((pl.col("ft_mean") > pl.col("b_ft_mean") * (1 + cfg.alert_pct_valve))
         | (pl.col("tt_mean") > pl.col("b_tt_mean") * (1 + cfg.alert_pct_valve))
         | (pl.col("pc_mean") > pl.col("b_pc_mean") * (1 + cfg.alert_pct_valve)))
        .fill_null(False).alias("valve_alert"),
        ((pl.col("ft_mean") > pl.col("xmr_ucl"))
         | (pl.col("ft_mean") < pl.col("xmr_lcl")))
        .fill_null(False).alias("xmr_alert"),
        (pl.col("ft_std") >= pl.col("b_ft_std") * cfg.sigma_ratio_alert)
        .fill_null(False).alias("sigma_alert"),
        (pl.col("ft_rate") > pl.col("b_ft_rate")
         + pl.max_horizontal(
             pl.lit(cfg.ft_rate_margin),
             pl.lit(cfg.detector_k)
             * (pl.col("b_ft_rate") * (1 - pl.col("b_ft_rate"))
                / pl.lit(cfg.window_cycles)).sqrt()))
        .fill_null(False).alias("rate_ft_alert"),
        (pl.col("tt_rate") > pl.col("b_tt_rate")
         + pl.max_horizontal(
             pl.lit(cfg.tt_rate_margin),
             pl.lit(cfg.detector_k)
             * (pl.col("b_tt_rate") * (1 - pl.col("b_tt_rate"))
                / pl.lit(cfg.window_cycles)).sqrt()))
        .fill_null(False).alias("rate_tt_alert"),
        ((pl.col("ft_mean") - pl.col("b_ft_mean")) / pl.col("b_ft_mean"))
        .alias("offset_ft"),
        (pl.col("n_qualityok_suspect") / pl.col("n_cycles"))
        .alias("quota_qualityok_suspect"),
    ])
    # warning (F5): offset >= warn_pct sostenuto su trend_window finestre
    # consecutive (regola numerica per-valvola).
    g = g.sort(["machine_code", "window_id"])
    g = g.with_columns(
        (pl.col("offset_ft") >= cfg.warn_pct).fill_null(False).alias("_above"))
    g = g.with_columns(
        (pl.col("_above").not_().cast(pl.UInt32).cum_sum()
         .over("machine_code")).alias("_seg"))
    g = g.with_columns(
        (pl.int_range(pl.len()).over(["machine_code", "_seg"]) + 1)
        .alias("_streak"))
    g = g.with_columns(
        ((pl.col("_streak") >= cfg.trend_window) & pl.col("_above"))
        .alias("warning"))
    # alert aggregato top-10 (F1-a); media top-10 per finestra calcolata in
    # ordine FISSO (python, righe gia' ordinate per (machine_code, window_id)
    # dallo streak): la riduzione group_by/mean di polars e' order-arbitraria
    # e darebbe ULPs non deterministici sul fingerprint SHA-256.
    top10_mean = (sum(baseline.stats[mc]["fillingtime"][0]
                      for mc in baseline.top10) / len(baseline.top10)
                  if baseline.top10 else 0.0)
    top10_rows = (g.filter(pl.col("machine_code").is_in(baseline.top10))
                  .select(["window_id", "ft_mean"]).to_dicts())
    per_w: dict = {}
    for r in top10_rows:
        per_w.setdefault(int(r["window_id"]), []).append(float(r["ft_mean"]))
    agg = pl.DataFrame({
        "window_id": sorted(per_w),
        "top10_mean": [sum(vals) / len(vals)
                       for _, vals in sorted(per_w.items())],
    })
    g = g.join(agg, on="window_id", how="left")
    g = g.with_columns(
        (pl.col("top10_mean") > top10_mean * (1 + cfg.alert_pct_agg))
        .fill_null(False).alias("aggregate_alert"))
    # bounds di finestra + stabilita' macchina (events STATE:*)
    wb = f.group_by("window_id").agg(
        pl.col("ts_beg").min().alias("window_start"),
        pl.col("ts_beg").max().alias("window_end"))
    g = g.join(wb, on="window_id", how="left")
    g = g.join(_machine_stability(events_df, wb, cfg), on="window_id",
               how="left")
    g = g.with_columns(
        ((pl.col("machine_stable").fill_null(False))
         & ~(pl.col("aggregate_alert") | pl.col("valve_alert")
             | pl.col("xmr_alert") | pl.col("sigma_alert")
             | pl.col("rate_ft_alert") | pl.col("rate_tt_alert")))
        .alias("machine_healthy"))
    # conteggi/quote machine-level per il condition monitoring
    cnt = (g.group_by("window_id")
           .agg(pl.col("n_cycles").sum().alias("n_cycles"),
                pl.col("n_qualityok_suspect").sum()
                .alias("n_qualityok_suspect"),
                pl.col("n_ft_over").sum().alias("n_ft_over"),
                pl.col("n_tt_over").sum().alias("n_tt_over")))
    counts = {}
    for r in cnt.sort("window_id").iter_rows(named=True):
        wid = int(r["window_id"])
        n = int(r["n_cycles"])
        nq = int(r["n_qualityok_suspect"])
        counts[wid] = {
            "n_cycles": n,
            "n_qualityok_suspect": nq,
            "quota_qualityok_suspect": (nq / n) if n else 0.0,
            "n_ft_over": int(r["n_ft_over"]),
            "n_tt_over": int(r["n_tt_over"]),
        }
    cols = [
        "window_id", "machine_code", "window_start", "window_end",
        "machine_stable", "machine_healthy", "aggregate_alert",
        "valve_alert", "xmr_alert", "sigma_alert", "rate_ft_alert",
        "rate_tt_alert", "warning", "offset_ft", "top10_mean",
        "ft_mean", "ft_std", "ft_rate", "tt_rate",
        "n_cycles", "n_qualityok_suspect", "quota_qualityok_suspect",
        "n_ft_over", "n_tt_over",
    ]
    return g.select(cols).sort(["window_id", "machine_code"]), counts


def detect_faulted_valves(cycles_df: pl.DataFrame, baseline: Baseline,
                          cfg: AnalyticsConfig) -> set:
    """Port D5 (detector segnali-only, mai GT) con baseline congelata.

    Mappa KPI estesa: restriction->fillingtime, closing_delay->tailtime,
    opening_delay->fillingtime, flowmeter_dropout->fillingtime,
    flowmeter_glitch->pulsecount. Flag la valvola se
        |d_mean| > k * sigma * sqrt(2) / sqrt(n)
    con d_mean = media(run analizzato) - media(baseline) per-valvola,
    sigma = std della baseline (congelata), n = cicli del run analizzato;
    OR sui KPI primari (k = cfg.detector_k).

    NOTA sqrt(2)/sqrt(n) (M5-F6): nel D5 test-only le due medie (run guasto
    e run sano, stesso seed) sono stime rumorose con sigma_mean =
    sigma*sqrt(2/n). Con baseline CONGELATA la media baseline e' nota
    (sigma ~ 0) e il fattore esatto sarebbe k*sigma/sqrt(n): mantenere
    sqrt(2) rende la soglia ~41% piu' alta (sqrt(2) ~ 1.414) — conservative
    su n piccolo (meno falsi positivi, sensibilita' ridotta ~1/sqrt(2)),
    bias documentato.
    """
    df = _prepare_cycles(cycles_df)
    kpis = sorted({v for v in DETECTOR_KPI_MAP.values()})
    flagged: set = set()
    for mc in sorted(df["machine_code"].unique().to_list()):
        sub = df.filter(pl.col("machine_code") == mc)
        n = sub.height
        st = baseline.stats.get(mc)
        if n == 0 or st is None:
            continue
        for kpi in kpis:
            sigma = st[kpi][1]
            if sigma is None or sigma <= 0.0:
                continue
            d_mean = sub[kpi].mean() - st[kpi][0]
            if abs(d_mean) > (cfg.detector_k * sigma * (2.0 ** 0.5)
                              / (n ** 0.5)):
                flagged.add(mc)
                break
    return flagged


def _machine_stability(events_df: pl.DataFrame, wb: pl.DataFrame,
                       cfg: AnalyticsConfig) -> pl.DataFrame:
    """Righe STATE:* (machine_code=="MACHINE") -> stabile per finestra.

    Stabile se a fine finestra lo stato corrente e' Running da >=
    stable_min_minutes, senza transizioni a Starting/Stopping/Stopped
    nella finestra.
    """
    ev = events_df.filter(
        (pl.col("machine_code") == "MACHINE")
        & pl.col("event").str.starts_with("STATE:"))
    if ev.is_empty():
        return pl.DataFrame({
            "window_id": wb["window_id"],
            "machine_stable": [False] * wb.height,
        })
    ev = ev.with_columns(
        pl.col("event").str.strip_prefix("STATE:").alias("status"))
    ev = _frame_ts_dt(ev)
    timeline = ev.sort("ts_beg").select(["ts_beg", "status"]).to_dicts()
    t0 = timeline[0]["ts_beg"]
    rows = []
    for r in wb.sort("window_id").iter_rows(named=True):
        w_start, w_end = r["window_start"], r["window_end"]
        past = [e for e in timeline if e["ts_beg"] <= w_end]
        if not past:
            rows.append((int(r["window_id"]), False))
            continue
        cur = past[-1]["status"]
        bad = [e for e in timeline
               if w_start <= e["ts_beg"] <= w_end
               and e["status"] in _UNSTABLE_STATES]
        if cur != "Running" or bad:
            rows.append((int(r["window_id"]), False))
            continue
        nr = [e for e in past if e["status"] != "Running"]
        ref = nr[-1]["ts_beg"] if nr else t0
        minutes = (w_end - ref).total_seconds() / 60.0
        rows.append((int(r["window_id"]), minutes >= cfg.stable_min_minutes))
    return pl.DataFrame({"window_id": [r[0] for r in rows],
                         "machine_stable": [r[1] for r in rows]})


def _summary_dict(health_df: pl.DataFrame, counts: dict) -> dict:
    """Summary JSON per finestra (CLI): flag machine-level + valvole flaggate."""
    summ = (health_df.group_by("window_id")
            .agg(pl.col("window_start").min().alias("window_start"),
                 pl.col("window_end").max().alias("window_end"),
                 pl.col("machine_stable").any().alias("machine_stable"),
                 pl.col("machine_healthy").any().alias("machine_healthy"),
                 pl.col("aggregate_alert").any().alias("aggregate_alert"),
                 pl.col("machine_code").filter(pl.col("valve_alert"))
                 .alias("valve_alerts"),
                 pl.col("machine_code").filter(pl.col("xmr_alert"))
                 .alias("xmr_valves"),
                 pl.col("machine_code").filter(pl.col("sigma_alert"))
                 .alias("sigma_valves"),
                 pl.col("machine_code").filter(pl.col("rate_ft_alert"))
                 .alias("rate_ft_valves"),
                 pl.col("machine_code").filter(pl.col("rate_tt_alert"))
                 .alias("rate_tt_valves"),
                 pl.col("machine_code").filter(pl.col("warning"))
                 .alias("warning_valves"))
            .sort("window_id"))
    windows = []
    for r in summ.iter_rows(named=True):
        windows.append({
            "window_id": int(r["window_id"]),
            "window_start": (r["window_start"].isoformat()
                             if r["window_start"] is not None else None),
            "window_end": (r["window_end"].isoformat()
                           if r["window_end"] is not None else None),
            "machine_stable": bool(r["machine_stable"]),
            "machine_healthy": bool(r["machine_healthy"]),
            "aggregate_alert": bool(r["aggregate_alert"]),
            "valve_alerts": sorted(r["valve_alerts"]),
            "xmr_valves": sorted(r["xmr_valves"]),
            "sigma_valves": sorted(r["sigma_valves"]),
            "rate_ft_valves": sorted(r["rate_ft_valves"]),
            "rate_tt_valves": sorted(r["rate_tt_valves"]),
            "warning_valves": sorted(r["warning_valves"]),
        })
    return {"windows": windows, "counts": counts}


# --------------------------------------------------------------------------
# CLI (unico punto di scrittura: out_dir / baseline path, MAI run_dir)
# --------------------------------------------------------------------------
def _run_analytics_cli(run_dir: str | Path, baseline_path: str | Path,
                       out_dir: str | Path) -> None:
    run_dir = Path(run_dir)
    baseline = Baseline.load(baseline_path)
    cfg = AnalyticsConfig(window_cycles=baseline.window_cycles)
    cycles = pl.read_parquet(run_dir / "valve_cycles.parquet")
    ev_path = run_dir / "events.parquet"
    if ev_path.exists():
        events = pl.read_parquet(ev_path)
    else:
        events = pl.DataFrame(schema={
            "machine_code": pl.String, "event": pl.String,
            "ts_beg": pl.Datetime(time_unit="us", time_zone="UTC"),
        })
    features = analyze(cycles, baseline, cfg)
    health_df, counts = health(features, events, baseline, cfg)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    features.write_parquet(out_dir / "features.parquet")
    health_df.write_parquet(out_dir / "health.parquet")
    (out_dir / "summary.json").write_text(
        json.dumps(_summary_dict(health_df, counts), sort_keys=True,
                   ensure_ascii=False, indent=2),
        encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m plcsim.analytics",
        description="Layer 7 analytics: features/health/detector READ-ONLY "
                    "sopra la telemetria (MAI scrive in run_dir).")
    parser.add_argument("--cycles", metavar="RUN_DIR", default=None,
                        help="run_dir con valve_cycles.parquet + "
                             "events.parquet (analizza e scrive in --out)")
    parser.add_argument("--fit-baseline", metavar="CYCLES_PARQUET",
                        default=None,
                        help="fit della baseline dal cycles parquet e save "
                             "in --baseline")
    parser.add_argument("--baseline", required=True,
                        help="path del JSON baseline (load o save)")
    parser.add_argument("--out", metavar="OUT_DIR", default=None,
                        help="out_dir SEPARATO per features/health/summary")
    args = parser.parse_args(argv)
    if args.fit_baseline:
        baseline = Baseline.fit(pl.read_parquet(args.fit_baseline),
                                AnalyticsConfig())
        baseline.save(args.baseline)
        return 0
    if args.cycles:
        if not args.out:
            parser.error("--out richiesto con --cycles")
        _run_analytics_cli(args.cycles, args.baseline, args.out)
        return 0
    parser.error("specificare --cycles o --fit-baseline")


if __name__ == "__main__":
    raise SystemExit(main())
