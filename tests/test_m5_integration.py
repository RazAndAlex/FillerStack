"""Test di integrazione M5 (piano M5-v2 §3.2) — layer 7 analytics su run
compressi reali (0.02125 giorni ≈ 6-7 s ciascuno, template TPL di
tests/test_engine; pattern tests/test_fault_integration.py:108 con
`run_days(make_cfg(42), 0.02125, out=tmp_path, progress=False,
scenario=...)`).

Harness condiviso: fixture MODULE-scoped (una sola esecuzione per modulo,
6 run ≈ 45 s totali). Nessun run 1-day nei test, nessun output in work/
(tutto su tmp_path).

Metodologia e deviazioni documentate (decisione branch manager, steering
W2 2026-08-11 — i finding non sono conflitti di scope ma input per la
calibrazione W3, protocollo §1/§6):
- test_fp_rate_healthy_compressed: il piano richiedeva FP=0 per-segnale
  con baseline cross-seed (seed 777) e analisi su run sano seed 42.
  Misurato sui default candidati: 6 segnali a 0 (valve_alert, aggregato,
  σ-ratio, rate FT/TT, warning), MA XmR=2 finestre-riga e detector=1
  valvola (valve6) — deterministici. Sono FP INTRINSECI a scala 3σ dei
  control chart XmR (bound su 6 sole finestre di baseline + finestra
  parziale finale w5 da 62 cicli; escursioni marginali +0.54/+0.75 ms
  sopra UCL) e del sampling del detector D5 (~0.3%/valvola/KPI a 3σ,
  valve6 flaggata su pulsecount a z=1.15×soglia ≈ 3.46σ campionario).
  Il test usa quindi bound numerici documentati (XmR ≤ 3, detector ≤ 2);
  la soglia NUMERICA finale viene congelata da W3 in
  work/m5-frozen-criteria.json (AC-M5-2) e consumata dallo script di
  accettazione W4. Scan su 20 coppie cross-seed: XmR≥1 su TUTTE le coppie
  → nessuna coppia "pulita" esiste a scala compressa con i default.
- test_sigma_only_m3_class: il piano richiedeva "flagga tutte le valvole
  del gruppo/scope". Con la soglia CANDIDATA sigma_ratio_alert=1.5 su
  m3_demo (gruppo G2, valvole 12-17) il set flaggato è 5/6 di G2:
  valve14 (ratio 1.477-1.496 a severità piena, lift calibrazione M3
  min +48.5%) resta sotto soglia; 0 fuori-scope. Su m3_global: 33/35
  (valve8/20 saturano a 1.20-1.34 per il profilo anomalo driver_scale
  1.35, banda D7). Il test asserta la semantica GRUPPO del piano: in-scope
  flaggate (≥5/6 con il candidato, 6/6 sul segnale: max ratio ≥ 1.45 a
  severità piena), NESSUNA fuori-scope, sane 0; le proprietà valve8/20 e
  valve14 sono documentate come input per la calibrazione W3 di
  sigma_ratio_alert (candidato ~1.35: margine sulla banda sana ≤1.10 e
  sotto il lift G2 ≥1.49).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plcsim.analytics import (                    # noqa: E402
    AnalyticsConfig, Baseline, analyze, detect_faulted_valves, health, main,
)
from plcsim.run import run_days                    # noqa: E402
from plcsim.scenario import load_scenario          # noqa: E402
from tests.test_engine import make_cfg             # noqa: E402

# cfg di test: DEFAULT candidati di AnalyticsConfig (W3 può solo
# CONFERMARLI nelle soglie congelate; i test restano validi).
CFG = AnalyticsConfig()

# scenario demo m2_demo (seed 42, gradual start 100 ramp 200):
# 9 valvole guaste (eval-only per la definizione dell'atteso).
FAULTED_DEMO = {f"valve{v}" for v in (0, 1, 2, 3, 6, 7, 9, 10, 12)}
SANE_VALVES = {f"valve{i}" for i in range(35)} - FAULTED_DEMO

# scenario m2_single_faults (abrupt da cycle 50): un fault per tipo.
SINGLE_FAULTS = {"valve1", "valve5", "valve11"}

# gruppo G2 di m3_demo (pressure_instability group, valvole 12-17).
G2 = {f"valve{i}" for i in range(12, 18)}

SCENARIOS = {
    "m5_healthy": ("m5_healthy.yaml", 42),        # scenario_id 61, seed 42
    "m4_healthy": ("m4_healthy.yaml", 42),        # scenario_id 41, seed 42
    "m5_s777": ("m5_healthy.yaml", 777),          # seed ≠ baseline (M5-F2)
    "demo": ("m2_demo.yaml", 42),                 # scenario_id 42, seed 42
    "single": ("m2_single_faults.yaml", 42),      # scenario_id 2, seed 42
    "m3_demo": ("m3_demo.yaml", 42),              # scenario_id 51, seed 42
}

# Banda sana σ-ratio (fuori-scope, m3_demo): misurata max 1.047
# (m3_calibration: banda sana ≤ 0.2% su Δσ rel).
SANE_RATIO_MAX = 1.10
# Floor di separazione σ-ratio G2 (severità piena): misurato max-ratio per
# valvola 1.496-1.568 (m3_calibration: lift 6/6 ≥ +48.5%); floor 1.45.
G2_RATIO_MIN = 1.45


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
def csv_bytes(df: pl.DataFrame) -> bytes:
    """Serializzazione canonica per confronti byte-a-byte."""
    return df.write_csv().encode()


@pytest.fixture(scope="module")
def run_dirs(tmp_path_factory) -> dict[str, Path]:
    """I 6 run compressi canonici, eseguiti UNA volta per modulo (~45 s)."""
    root = tmp_path_factory.mktemp("m5_integration")
    out = {}
    for name, (fname, seed) in SCENARIOS.items():
        sc = load_scenario(ROOT / "scenarios" / fname)
        d = root / name
        run_days(make_cfg(seed), 0.02125, out=d, progress=False, scenario=sc)
        out[name] = d
    return out


def read_frames(out: Path) -> dict[str, pl.DataFrame]:
    return {n: pl.read_parquet(out / f"{n}.parquet")
            for n in ("valve_cycles", "events", "ground_truth")}


# --------------------------------------------------------------------------
# AC-M5-1 — bit-identità compressa M5 ≡ M4 (protocollo §4)
# --------------------------------------------------------------------------
def test_healthy_bit_identity_m5_m4(run_dirs):
    """Run compresso m5_healthy (id 61) vs m4_healthy (id 41), stesso seed
    42: valve_cycles/events/ground_truth IDENTICI a meno di scenario_id
    (righe, colonne, valori — assert model protocollo §4); per gli eventi
    confronto anche senza righe CMD:OPEN/CMD:CLOSE."""
    sc5 = load_scenario(ROOT / "scenarios" / "m5_healthy.yaml")
    assert sc5.scenario_id == 61
    assert sc5.name == "baseline sana M5"
    assert sc5.seed is None
    assert sc5.faults == []
    m5 = read_frames(run_dirs["m5_healthy"])
    m4 = read_frames(run_dirs["m4_healthy"])
    for artifact in ("valve_cycles", "events", "ground_truth"):
        a, b = m5[artifact], m4[artifact]
        assert a.columns == b.columns, artifact
        assert a.height == b.height, artifact
        assert a["scenario_id"].unique().to_list() == [61], artifact
        assert b["scenario_id"].unique().to_list() == [41], artifact
        assert csv_bytes(a.drop("scenario_id")) == \
            csv_bytes(b.drop("scenario_id")), artifact
    # eventi senza righe CMD (coerenza anche sul sottoinsieme)
    e5 = m5["events"].drop("scenario_id").filter(
        ~pl.col("event").is_in(["CMD:OPEN", "CMD:CLOSE"]))
    e4 = m4["events"].drop("scenario_id").filter(
        ~pl.col("event").is_in(["CMD:OPEN", "CMD:CLOSE"]))
    assert e5.height == e4.height
    assert csv_bytes(e5) == csv_bytes(e4)


# --------------------------------------------------------------------------
# AC-M5-3 — detector reale (port D5) esatto sul demo
# --------------------------------------------------------------------------
def test_detector_demo_exact(run_dirs):
    """Detector reale (plcsim.analytics.detect_faulted_valves, port D5 con
    mappa KPI estesa) su m2_demo compresso, baseline da run sano compresso
    stesso seed 42: flagga ESATTAMENTE le 9 valvole GT fault≠None (eval-only
    per l'atteso), nessuna sana. AC-M5-3 riformulato (fix M5-F6-ii): bande
    autorizzate valve8/20 SOLO se effettivamente flaggate da D5 — su m2 il
    detector NON le flagga: il set atteso è esattamente FAULTED_DEMO."""
    demo = read_frames(run_dirs["demo"])
    healthy = read_frames(run_dirs["m5_healthy"])
    baseline = Baseline.fit(healthy["valve_cycles"], CFG)
    flagged = detect_faulted_valves(demo["valve_cycles"], baseline, CFG)
    # atteso dalla GT (eval-only): tutte le valvole con fault_type != None
    expected = set(demo["ground_truth"]
                   .filter(pl.col("fault_type").is_not_null())
                   ["machine_code"].unique().to_list())
    assert expected == FAULTED_DEMO
    assert flagged == expected, sorted(flagged ^ expected)
    assert not (flagged & SANE_VALVES)
    # bande autorizzate valve8/20: nessun flag (D5 su m2 non le flagga)
    assert not flagged & {"valve8", "valve20"}


# --------------------------------------------------------------------------
# AC-M5-11 — semantica alert doppia: per-valvola (F1-b) autoritativa
# --------------------------------------------------------------------------
def test_alert_on_restriction(run_dirs):
    """m2_single_faults compresso (valve1 restriction 0.07, abrupt da cycle
    50): l'alert PER-VALVOLA (semantica F1-b, delta media finestra vs
    baseline per-valvola ≥ alert_pct_valve) FIRE su valve1 (finestre 1+ a
    ΔFT ≈ +5..10%); nessun flag su valvole non guaste dello scenario; sul
    run sano (stessa baseline) ZERO alert per-valvola.

    Nota (misurata, doc): con i default, anche valve5 (closing_delay 100 →
    ΔTT ≈ +33%) scatta — l'alert per-valvola copre i fault locali di tutti
    i tipi (F1-b); valve11 (opening_delay 80 → ΔFT +2.9..5.3%) resta sotto
    il 6% candidato. L'alert AGGREGATO top-10 (F1-a) NON scatta da solo sul
    demo né sul single (misurato 0 finestre) → (b) è AUTORITATIVA
    (AC-M5-11); entrambe le soglie vengono congelate da W3 nel JSON."""
    single = read_frames(run_dirs["single"])
    healthy = read_frames(run_dirs["m5_healthy"])
    baseline = Baseline.fit(healthy["valve_cycles"], CFG)
    hd, _ = health(analyze(single["valve_cycles"], baseline, CFG),
                   single["events"], baseline, CFG)
    fired = set(hd.filter(pl.col("valve_alert"))
                .unique(subset=["machine_code"])["machine_code"].to_list())
    assert "valve1" in fired
    # nessuna valvola sana: ogni flag è su una delle 3 guaste dello scenario
    assert fired <= SINGLE_FAULTS, fired
    # valve1: scatta dalla finestra 1 in poi (finestra 0 a cavallo di
    # start_cycle 50 → Δ medio sotto soglia)
    v1 = hd.filter(pl.col("machine_code") == "valve1")
    by_w = {r["window_id"]: bool(r["valve_alert"])
            for r in v1.select(["window_id", "valve_alert"])
            .iter_rows(named=True)}
    assert not by_w[0]
    assert all(by_w[w] for w in range(1, max(by_w) + 1)), by_w
    # run sano con la stessa baseline: zero alert per-valvola (FP=0)
    hh, _ = health(analyze(healthy["valve_cycles"], baseline, CFG),
                   healthy["events"], baseline, CFG)
    assert not hh["valve_alert"].any()


# --------------------------------------------------------------------------
# AC-M5-2 — FP rate per-segnale su run sano compresso (baseline seed 777)
# --------------------------------------------------------------------------
def test_fp_rate_healthy_compressed(run_dirs):
    """FP per-segnale su run sano compresso con baseline cross-seed NON
    degenere (fix M5-F2): baseline fit su m5_healthy compresso seed 777,
    analisi su m5_healthy compresso seed 42 (stesso scenario, seed
    diverso). Unità ESPLICITA = per-finestra (finestra-riga
    (window_id, machine_code); detector in valvole).

    SCELTA DOCUMENTATA (decisione branch manager 2026-08-11): 6 segnali
    sono 0 deterministici (valve_alert, aggregate_alert, sigma_alert,
    rate_ft_alert, rate_tt_alert, warning); XmR e detector hanno FP
    INTRINSECI a scala 3σ dei control chart / del sampling D5 e NON sono
    assertati a 0: XmR ≤ 3 finestre-riga (misurate 2: valve18/31 nella
    finestra parziale finale w5, +0.54/+0.75 ms sopra UCL) e detector ≤ 2
    valvole (misurata 1: valve6 su pulsecount, z=1.15×soglia ≈ 3.46σ
    campionario). I conteggi sono DETERMINISTICI (stessi seed); la soglia
    numerica finale viene congelata da W3 in work/m5-frozen-criteria.json
    (AC-M5-2) e consumata dallo script di accettazione W4."""
    h42 = read_frames(run_dirs["m5_healthy"])
    h777 = read_frames(run_dirs["m5_s777"])
    baseline = Baseline.fit(h777["valve_cycles"], CFG)
    feats = analyze(h42["valve_cycles"], baseline, CFG)
    hd, counts = health(feats, h42["events"], baseline, CFG)

    # 6 segnali robustamente a 0 (finestre-riga)
    for col in ("valve_alert", "aggregate_alert", "sigma_alert",
                "rate_ft_alert", "rate_tt_alert", "warning"):
        assert int(hd[col].sum()) == 0, col
    # XmR: bound documentato (intrinseco 3σ, misurato 2)
    assert int(hd["xmr_alert"].sum()) <= 3
    # detector: bound documentato (sampling D5, misurato 1)
    flagged = detect_faulted_valves(h42["valve_cycles"], baseline, CFG)
    assert len(flagged) <= 2, sorted(flagged)
    # CM: nessun evento alert-level — quota per-finestra entro la banda
    # sana (misurata max 0.1226, coda sana valve12 ~15% FT>2000) + coerenza
    # col conteggio diretto sulle colonne
    n_windows = hd["window_id"].n_unique()
    assert n_windows * len(SANE_VALVES | FAULTED_DEMO) == hd.height
    for wid, c in counts.items():
        assert c["quota_qualityok_suspect"] <= 0.15, wid
    direct = h42["valve_cycles"].filter(
        (pl.col("fill_quality_ok") == pl.lit(True))
        & (pl.col("diagnostic_status") == pl.lit("SUSPECT"))).height
    assert sum(c["n_qualityok_suspect"] for c in counts.values()) == direct
    # determinismo dei conteggi (stessi seed -> stessi numeri)
    feats2 = analyze(h42["valve_cycles"], baseline, CFG)
    hd2, counts2 = health(feats2, h42["events"], baseline, CFG)
    assert hd.select(["window_id", "machine_code", "xmr_alert"]) \
        .equals(hd2.select(["window_id", "machine_code", "xmr_alert"]))
    assert counts == counts2


# --------------------------------------------------------------------------
# AC-M5-10 — degrado solo-σ (classe M3): semantica GRUPPO su m3_demo
# --------------------------------------------------------------------------
def test_sigma_only_m3_class(run_dirs):
    """σ-ratio (σ_FT di finestra / σ_FT baseline ≥ sigma_ratio_alert) su
    m3_demo compresso (gruppo G2, valvole 12-17; pressure_instability
    severity 0.5, gradual start 100 ramp 200): flagga le valvole IN-SCOPE,
    NESSUNA fuori-scope; su run sano 0 flag (fix M5-F4, AC-M5-10).

    Semantica GRUPPO (decisione branch manager 2026-08-11) + deviazione
    documentata: con la soglia CANDIDATA default 1.5 il set flaggato è
    5/6 di G2 — valve14 (max ratio 1.496 a severità piena) resta sotto la
    soglia candidata; il SEGNALE σ↑ è presente su 6/6 G2 (max ratio per
    valvola ≥ 1.45 a severità piena, floor sotto il lift misurato
    1.496-1.568) e la banda sana fuori-scope è ≤ 1.10 (misurata max
    1.047). Su m3_global: 33/35 (valve8/20 saturano a 1.20-1.34 per il
    profilo anomalo driver_scale 1.35, banda D7) — proprietà documentate
    come input per la calibrazione W3 di sigma_ratio_alert (candidato
    ~1.35: margine sulla banda sana ≤1.10 e sotto il lift G2 ≥1.49)."""
    m3 = read_frames(run_dirs["m3_demo"])
    healthy = read_frames(run_dirs["m5_healthy"])
    baseline = Baseline.fit(healthy["valve_cycles"], CFG)
    hd, _ = health(analyze(m3["valve_cycles"], baseline, CFG),
                   m3["events"], baseline, CFG)
    flagged = set(hd.filter(pl.col("sigma_alert"))
                  .unique(subset=["machine_code"])["machine_code"].to_list())
    # FP side: nessuna valvola fuori-scope flaggata; in-scope ≥ 5/6
    assert not (flagged - G2), sorted(flagged - G2)
    assert len(flagged & G2) >= 5, sorted(flagged & G2)
    # segnale su 6/6 G2: max σ-ratio a severità piena (finestre 3..5,
    # cycle_id >= 300) ≥ floor; banda sana fuori-scope ≤ 1.10
    bstd = baseline.stats
    for mc in sorted(G2):
        v = hd.filter((pl.col("machine_code") == mc)
                      & (pl.col("window_id") >= 3))
        ratios = [r["ft_std"] / bstd[mc]["fillingtime"][1]
                  for r in v.select(["ft_std"]).iter_rows(named=True)]
        assert max(ratios) >= G2_RATIO_MIN, (mc, max(ratios))
    for mc in sorted({f"valve{i}" for i in range(35)} - G2):
        v = hd.filter(pl.col("machine_code") == mc)
        ratios = [r["ft_std"] / bstd[mc]["fillingtime"][1]
                  for r in v.select(["ft_std"]).iter_rows(named=True)]
        assert max(ratios) <= SANE_RATIO_MAX, (mc, max(ratios))
    # run sano (stessa baseline): 0 flag σ
    hh, _ = health(analyze(healthy["valve_cycles"], baseline, CFG),
                   healthy["events"], baseline, CFG)
    assert not hh["sigma_alert"].any()


# --------------------------------------------------------------------------
# AC-M5-6 — determinismo del pipeline analytics (pattern test_engine.py:182)
# --------------------------------------------------------------------------
def test_determinism_analytics_pipeline(run_dirs):
    """SHA-256 su features/health a parità di (input, baseline, cfg): due
    passate identiche → stessi digest; input diverso → digest diverso
    (pattern tests/test_engine.py:182-189, fix R2)."""
    demo = read_frames(run_dirs["demo"])
    healthy = read_frames(run_dirs["m5_healthy"])
    baseline = Baseline.fit(healthy["valve_cycles"], CFG)

    def fp(feats: pl.DataFrame, hdf: pl.DataFrame):
        return (hashlib.sha256(feats.write_csv().encode()).hexdigest(),
                hashlib.sha256(hdf.write_csv().encode()).hexdigest())

    f1 = analyze(demo["valve_cycles"], baseline, CFG)
    hd1, _ = health(f1, demo["events"], baseline, CFG)
    f2 = analyze(demo["valve_cycles"], baseline, CFG)
    hd2, _ = health(f2, demo["events"], baseline, CFG)
    assert fp(f1, hd1) == fp(f2, hd2)
    f3 = analyze(healthy["valve_cycles"], baseline, CFG)
    hd3, _ = health(f3, healthy["events"], baseline, CFG)
    assert fp(f1, hd1) != fp(f3, hd3)


# --------------------------------------------------------------------------
# AC-M5-6/9 — CLI riproducibile su run compresso
# --------------------------------------------------------------------------
def test_cli_reproducible_anchor(run_dirs, tmp_path):
    """CLI `python -m plcsim.analytics --fit-baseline / --cycles` su un run
    compresso: output su out_dir SEPARATA (mai accanto ai parquet di
    simulazione), hash dei parquet di input INVARIATO, fingerprint
    riproducibile (due passate identiche su features/health/summary)."""
    run_dir = run_dirs["m5_healthy"]
    baseline_path = tmp_path / "baseline.json"
    rc = main(["--fit-baseline",
               str(run_dir / "valve_cycles.parquet"),
               "--baseline", str(baseline_path)])
    assert rc == 0 and baseline_path.exists()

    def hashes() -> dict:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(run_dir.iterdir())
                if p.suffix == ".parquet" or p.name == "run_summary.json"}

    before = hashes()
    out = tmp_path / "out"
    rc = main(["--cycles", str(run_dir), "--baseline", str(baseline_path),
               "--out", str(out)])
    assert rc == 0
    for f in ("features.parquet", "health.parquet", "summary.json"):
        assert (out / f).exists(), f
    assert hashes() == before                     # input intoccabile
    out2 = tmp_path / "out2"
    assert main(["--cycles", str(run_dir), "--baseline", str(baseline_path),
                 "--out", str(out2)]) == 0
    for f in ("features.parquet", "health.parquet", "summary.json"):
        h1 = hashlib.sha256((out / f).read_bytes()).hexdigest()
        h2 = hashlib.sha256((out2 / f).read_bytes()).hexdigest()
        assert h1 == h2, f


# --------------------------------------------------------------------------
# ADR-0012 — GT mai nel percorso decisionale (riferimento rapido)
# --------------------------------------------------------------------------
def test_gt_never_leaks():
    """Riferimento rapido (già coperto in unit test_m5_analytics): il
    modulo analytics non contiene GT/fault_type nel percorso decisionale
    (anti-leakage ADR-0012)."""
    src = (ROOT / "plcsim" / "analytics.py").read_text(encoding="utf-8")
    assert "ground_truth" not in src
    assert "fault_type" not in src
