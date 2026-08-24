"""Test di integrazione M3 (piano m3-v2 §4.2) — pressure_instability gruppo/global.

Harness condiviso (identico a test_fault_integration.py): run compresso
(0.02125 giorni, template TPL di tests/test_engine) via
run_days(make_cfg(42), 0.02125, out=tmp_path, progress=False, scenario=...);
i 4 run canonici (m3_healthy, m3_demo, m3_global, m2_healthy) sono fixture
MODULE-scoped (una sola esecuzione, ~7-8 s ciascuno). Nessun output in
work/: tutto su tmp_path.

Finestre NORMATIVE calibrate in work/m3_calibration.md (W2, run compressi):
- severita demo = 0.5 (group G2, gradual start 100 ramp 200): lift sigma_FT
  a severita piena +48.5%..+52.9% su 6/6 valvole di G2; direzione ↑ 6/6;
  theta = 0.30 (min misurato 1.485 -> margine 1.14x); deriva media FT del
  gruppo <= +0.5% (documentata, NON criterio); encoder 0%; FT max <= 2130.
- banda sana: |Δsigma_FT| <= +0.2% (27/27 entro ±10%), |ΔFT_mean| <= 0.03%
  (29/29 entro ±1%), fillingok 78.7 ± 3 p.p., close_reason target >= 98.5%.
- separazione >= 2x banda sana: 48.5% vs 0.2% (~240x).
- detector (tutti i cicli): soglia 3·sigma_h/sqrt(n-1) ≈ 8.7-9.1 ms;
  Δsigma G2 ≈ +23-25 ms -> ratio 2.66-2.74; fuori gruppo ~0.07 ms (0 false
  positivi). D3: sigma_piena (>=300) > sigma_rampa (100..299) su 6/6;
  stazionarieta [460,562] vs [300,459] entro ±10% (misurato ±3.9%).
- finestre AC-4 (gruppo, severita piena): |ΔTT_mean| <= 12 ms (misurato
  6.9), |ΔTP_mean| <= 8 (misurato 5.6), |ΔPC_mean| <= 2 (misurato 0.55).
- global (0.5, abrupt start 100): 35/35 valvole con Δsigma > soglia.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plcsim.run import run_days                       # noqa: E402
from plcsim.scenario import FaultEngine, load_scenario  # noqa: E402
from tests.test_engine import make_cfg                # noqa: E402
from tests.test_fault_integration import (            # noqa: E402
    SANE_BANDS, assert_sane_statistical_bands,
    csv_bytes, full_severity_ids, read_frames, stats_of,
)
from tests.test_scenario import _make_plant, _rec     # noqa: E402

# scenario demo: gruppo G2 = cfg.groups[2] = [12..17]
G2 = list(range(12, 18))
G2_MC = {f"valve{v}" for v in G2}
SEV = 0.5           # severita demo/global (calibrata W2)
THETA = 0.30        # soglia lift sigma nei test (min misurato 1.485)
START = 100         # start_cycle demo/global
RAMP = 200          # ramp_cycles demo -> severita piena dal ciclo 299
SANE_29 = {f"valve{v}" for v in range(35) if v not in G2}
# valve8/20 escluse dal conteggio della banda sigma (profilo anomalo
# driver_scale 1.35, D7/M1) ma verificate esplicitamente entro banda
SIGMA_EXCLUDED = {8, 20}

SCENARIOS = {
    "healthy": "m3_healthy.yaml",
    "demo": "m3_demo.yaml",
    "global": "m3_global.yaml",
    "m2healthy": "m2_healthy.yaml",
}


@pytest.fixture(scope="module")
def run_dirs(tmp_path_factory) -> dict[str, Path]:
    """I 4 run compressi canonici, eseguiti UNA volta per modulo."""
    root = tmp_path_factory.mktemp("m3_integration")
    out = {}
    for name, fname in SCENARIOS.items():
        sc = load_scenario(ROOT / "scenarios" / fname)
        d = root / name
        run_days(make_cfg(42), 0.02125, out=d, progress=False, scenario=sc)
        out[name] = d
    return out


def fingerprint(out: Path) -> str:
    """SHA-256 di (cycles+events+GT) serializzati in csv (D1)."""
    frames = read_frames(out)
    payload = b"".join(csv_bytes(frames[k])
                       for k in ("valve_cycles", "events", "ground_truth"))
    return hashlib.sha256(payload).hexdigest()


def _sigma_h(healthy_cycles: pl.DataFrame, mc: str) -> float:
    """std(ddof=1) del fillingtime della valvola nel run sano (baseline)."""
    return healthy_cycles.filter(pl.col("machine_code") == mc)["fillingtime"].std()


# --------------------------------------------------------------------------
# AC-5 — invariante critico: healthy M3 bit-identico a healthy M2
# --------------------------------------------------------------------------
def test_healthy_equiv_m2(run_dirs):
    """AC-5: run compresso m3_healthy ≡ run compresso m2_healthy.

    valve_cycles, events e ground_truth IDENTICI a meno della colonna
    scenario_id (50 vs 1) — comprese le righe CMD:OPEN/CMD:CLOSE (entrambi i
    run passano dall'engine M2/M3 che le emette in modo identico). E'
    l'invariante critico: il percorso sano M3 è bit-esatto rispetto a M2
    (x1.0 esatto in IEEE su _amp_mult, piano §1.1).
    """
    h3 = read_frames(run_dirs["healthy"])
    h2 = read_frames(run_dirs["m2healthy"])
    assert (h3["valve_cycles"]["scenario_id"] == 50).all()
    assert (h2["valve_cycles"]["scenario_id"] == 1).all()
    for k in ("valve_cycles", "events", "ground_truth"):
        a = h3[k].drop("scenario_id")
        b = h2[k].drop("scenario_id")
        assert csv_bytes(a) == csv_bytes(b), k
    # anche il sottoinsieme senza CMD coincide (schema task W2)
    a = h3["events"].drop("scenario_id").filter(
        ~pl.col("event").is_in(["CMD:OPEN", "CMD:CLOSE"]))
    b = h2["events"].drop("scenario_id").filter(
        ~pl.col("event").is_in(["CMD:OPEN", "CMD:CLOSE"]))
    assert csv_bytes(a) == csv_bytes(b)


# --------------------------------------------------------------------------
# AC-1/AC-4 — catena evento → GT → KPI (demo)
# --------------------------------------------------------------------------
def test_group_chain(run_dirs):
    """AC-1/AC-4: catena completa del fault di gruppo.

    (a) eventi: FAULT_START 1× con note '... group_id=2'; FAULT_RAMP per i
    cicli di rampa di OGNI membro; CMD:OPEN/CMD:CLOSE con cycle_id in GT
    (join 1:1) e invariante CMD:CLOSE.note == cycles.close_reason ==
    TAIL.note; (b) GT: valve12..17 con pressure_instability dal ciclo 100
    (None/0.0 prima; severity = 1° passo rampa 0.5/200 al ciclo 100, piena
    0.5 dal ciclo 299); (c) join 1:1 GT↔cycles su (machine_code, cycle_id,
    scenario_id) senza orfani.
    """
    frames = read_frames(run_dirs["demo"])
    cyc, ev, gt = (frames["valve_cycles"], frames["events"],
                   frames["ground_truth"])
    # (a) FAULT_START una volta, sul primo ciclo affetto, note corrette
    fs = ev.filter(pl.col("event") == "FAULT_START")
    assert fs.height == 1, fs.height
    r = fs.row(0, named=True)
    assert r["note"] == f"pressure_instability severity={SEV} " \
                        f"start_cycle={START} group_id=2", r
    assert r["cycle_id"] == START
    # FAULT_RAMP: cicli 100..299 di ogni membro (200 × 6; il ciclo 299 ha
    # gia' severita piena ma riceve ancora l'evento ramp: c < start+ramp)
    fr = ev.filter(pl.col("event") == "FAULT_RAMP")
    assert fr.height == 6 * RAMP, fr.height
    assert (fr["note"].str.starts_with("ramp severity=")).all()
    # (b) GT per i membri: confini esatti
    for v in G2:
        mc = f"valve{v}"
        g = gt.filter(pl.col("machine_code") == mc)
        pre = g.filter(pl.col("cycle_id") < START)
        assert (pre["fault_type"].is_null()).all() and \
            (pre["severity"] == 0.0).all(), mc
        aff = g.filter(pl.col("cycle_id") >= START)
        assert aff.height > 0
        assert (aff["fault_type"] == "pressure_instability").all(), mc
        r100 = g.filter(pl.col("cycle_id") == START).row(0, named=True)
        assert r100["severity"] == pytest.approx(SEV / RAMP), mc  # 1° passo
        assert (g.filter(pl.col("cycle_id") >= START + RAMP - 1)["severity"]
                == SEV).all(), mc
    # (c) join 1:1 GT<->cycles su (machine_code, cycle_id, scenario_id)
    keys_c = set(zip(cyc["machine_code"], cyc["cycle_id"], cyc["scenario_id"]))
    keys_g = set(zip(gt["machine_code"], gt["cycle_id"], gt["scenario_id"]))
    assert keys_c == keys_g
    # invariante CMD:CLOSE.note == cycles.close_reason == TAIL.note
    cr = {(r["machine_code"], r["cycle_id"]): r["close_reason"]
          for r in cyc.iter_rows(named=True)}
    for mc in G2_MC:
        cc = ev.filter((pl.col("event") == "CMD:CLOSE")
                       & (pl.col("machine_code") == mc))
        tail = ev.filter((pl.col("event") == "TAIL")
                         & (pl.col("machine_code") == mc))
        assert cc.height == tail.height > 0, mc
        for row in cc.iter_rows(named=True):
            assert row["note"] == cr[(mc, row["cycle_id"])], row
        for row in tail.iter_rows(named=True):
            assert row["note"] == cr[(mc, row["cycle_id"])], row


# --------------------------------------------------------------------------
# AC-2 — lift di sigma_FT del gruppo (severita piena) + separazione
# --------------------------------------------------------------------------
def test_group_sigma_lift(run_dirs):
    """AC-2: sui cicli a SEVERITÀ PIENA (filtro GT severity == 0.5, ciclo
    >= 299, n = 263), sigma_FT di valve12..17 >= (1+theta)·sigma_sana
    stessa-seed su >= 5/6 valvole (theta = 0.30 da calibrazione; misurato
    +48.5%..+52.9% -> 6/6); direzione ↑ su 6/6; separazione >= 2x banda sana
    (banda = max |Δsigma| relativo fuori gruppo nello stesso run).
    """
    frames = read_frames(run_dirs["demo"])
    h = read_frames(run_dirs["healthy"])
    cyc, gt = frames["valve_cycles"], frames["ground_truth"]
    h_cyc = h["valve_cycles"]
    # banda sana nello stesso run: max |Δsigma| relativo fuori gruppo
    sane_band = 0.0
    for v in range(35):
        if v in G2:
            continue
        s = cyc.filter(pl.col("machine_code") == f"valve{v}")["fillingtime"].std()
        b = _sigma_h(h_cyc, f"valve{v}")
        sane_band = max(sane_band, abs((s - b) / b))
    lifts = {}
    n_ok = 0
    for v in G2:
        mc = f"valve{v}"
        ids = full_severity_ids(gt, v, SEV)
        assert len(ids) == 264, (v, len(ids))   # cicli pieni 299..562
        s_full = cyc.filter((pl.col("machine_code") == mc)
                            & pl.col("cycle_id").is_in(ids))["fillingtime"].std()
        s_h = _sigma_h(h_cyc, mc)
        lifts[v] = s_full / s_h - 1.0
        assert s_full > s_h, f"{mc}: direzione non ↑ ({lifts[v]:+.3f})"
        if lifts[v] >= THETA:
            n_ok += 1
    assert n_ok >= 5, f"lift >= theta su {n_ok}/6: {lifts}"
    # separazione >= 2x banda sana
    min_lift = min(lifts.values())
    assert min_lift >= 2.0 * sane_band, \
        f"min_lift={min_lift:.3f} < 2*banda={2 * sane_band:.3f}"


# --------------------------------------------------------------------------
# AC-3 — valvole fuori gruppo sane
# --------------------------------------------------------------------------
def test_out_of_group_sane(run_dirs):
    """AC-3: le 29 valvole fuori gruppo restano sane.

    |ΔFT_mean| <= 1% (29/29, misurato <= 0.03%); |Δsigma_FT| <= 10% su
    27/27 (valve8/20 escluse dal conteggio, D7) e valve8/20 ESPLICITAMENTE
    entro banda (assenza di contaminazione nonostante il profilo anomalo,
    misurato <= 0.2%); fillingok pooled 78.7 ± 3 p.p.; close_reason target
    >= 98%; bande medie per-KPI (M2 appendice E) su tutte le 29 sane.
    """
    frames = read_frames(run_dirs["demo"])
    h = read_frames(run_dirs["healthy"])
    cyc, h_cyc = frames["valve_cycles"], h["valve_cycles"]
    n_dft_ok = n_sigma_ok = 0
    for mc in sorted(SANE_29):
        v = int(mc.removeprefix("valve"))
        s = cyc.filter(pl.col("machine_code") == mc)
        b = h_cyc.filter(pl.col("machine_code") == mc)
        dft = abs((s["fillingtime"].mean() - b["fillingtime"].mean())
                  / b["fillingtime"].mean())
        assert dft <= 0.01, f"{mc}: |ΔFT|={dft:.4f} > 1%"
        n_dft_ok += 1
        ds = abs((s["fillingtime"].std() - b["fillingtime"].std())
                 / b["fillingtime"].std())
        assert ds <= 0.10, f"{mc}: |Δσ|={ds:.4f} > 10%"
        if v not in SIGMA_EXCLUDED:
            n_sigma_ok += 1
    assert n_dft_ok == 29 and n_sigma_ok == 27
    # bande medie per-KPI (appendice E) su tutte le sane incluse 8/20
    assert_sane_statistical_bands(cyc, h_cyc, SANE_29)
    # quote pooled
    fok = cyc["fillingok"].mean()
    assert 0.757 <= fok <= 0.817, f"fillingok={fok:.4f}"
    tgt = (cyc["close_reason"] == "target").mean()
    assert tgt >= 0.98, f"target={tgt:.4f}"


# --------------------------------------------------------------------------
# AC-4 — parametri meccanici intatti (fault solo sul serbatoio)
# --------------------------------------------------------------------------
def test_group_mechanics_untouched(run_dirs):
    """AC-4: nessun parametro meccanico alterato.

    ΔTT/ΔTP/ΔPC del gruppo (severita piena) entro le finestre calibrate W2
    (|ΔTT| <= 12 ms, |ΔTP| <= 8, |ΔPC| <= 2 — misurato 6.9/5.6/0.55); il
    fault scrive SOLO plant._amp_mult: _restriction resta 1.0 e i delay
    meccanici restano 0.0 su tutte le valvole.
    """
    frames = read_frames(run_dirs["demo"])
    h = read_frames(run_dirs["healthy"])
    cyc, gt, h_cyc = (frames["valve_cycles"], frames["ground_truth"],
                      h["valve_cycles"])
    for v in G2:
        mc = f"valve{v}"
        ids = full_severity_ids(gt, v, SEV)
        s = stats_of(cyc.filter((pl.col("machine_code") == mc)
                                & pl.col("cycle_id").is_in(ids)))
        b = stats_of(h_cyc.filter(pl.col("machine_code") == mc))
        assert abs(s["tt"] - b["tt"]) <= 12, f"{mc}: ΔTT={s['tt']-b['tt']:.2f}"
        assert abs(s["tp"] - b["tp"]) <= 8, f"{mc}: ΔTP={s['tp']-b['tp']:.2f}"
        assert abs(s["pc"] - b["pc"]) <= 2, f"{mc}: ΔPC={s['pc']-b['pc']:.2f}"
    # non-iniezione sui canali meccanici (engine sul scenario canonico)
    cfg, plant = _make_plant()
    eng = FaultEngine(plant, load_scenario(ROOT / "scenarios" / "m3_demo.yaml"),
                      cfg)
    assert (eng._applied_restriction == 1.0).all()
    for v in range(35):
        assert plant.mech[v].close_delay_ms == 0 and \
            plant.mech[v].open_delay_ms == 0, v
    for v in G2:
        eng.on_cycle(_rec(v, START + RAMP - 1))   # iniezione per il ciclo 300
        assert plant._amp_mult[v] == 1.0 + SEV, v
    assert plant._amp_mult[0] == 1.0              # fuori gruppo


# --------------------------------------------------------------------------
# AC-7/D5 — detector segnali-only (test-only)
# --------------------------------------------------------------------------
def detect_group_flag(cycles: pl.DataFrame, baseline: pl.DataFrame) -> set[str]:
    """Detector dai SOLI segnali di valve_cycles (mai GT): flag per-valvola
    se Δσ_FT > 3·σ_h/√(n−1), con Δσ_FT = σ(run guasto) − σ(baseline) su
    TUTTI i cicli (n = cicli nel run guasto) e σ_h = std(ddof=1) del
    fillingtime della baseline stessa-seed. SE(Δσ di due std indipendenti)
    = σ_h/√(n−1): il √2 è già assorbito nella SE della singola std (piano
    §4.4 — niente √2 qui, a differenza delle medie, appendice F M2).

    IMPORTANTE (D5, vincolo B1 M2): la baseline stesso-seed è SOLO
    validazione in-M2/M3 (test-only) — NON ML-deployable: il layer
    analytics post-M3 deve usare seed SEPARATI train/baseline (es. train
    seed A, baseline seed B, validazione seed C).
    """
    flagged: set[str] = set()
    for mc in cycles["machine_code"].unique().to_list():
        df = cycles.filter(pl.col("machine_code") == mc)
        bf = baseline.filter(pl.col("machine_code") == mc)
        n = df.height
        sigma_h = bf["fillingtime"].std()
        if sigma_h is None or n < 2:
            continue
        thr = 3.0 * sigma_h / (n - 1) ** 0.5
        d_sigma = df["fillingtime"].std() - sigma_h
        if d_sigma > thr:
            flagged.add(mc)
    return flagged


def test_detector_flags_exactly_group(run_dirs):
    """AC-7: il detector flagga ESATTAMENTE le 6 valvole di G2 (valve12..17),
    0 falsi positivi fuori gruppo; allarme gruppo se >= 3/6 membri flaggati
    (6/6 attesi). Autocontrollo: baseline vs baseline -> 0 flag.
    """
    demo = read_frames(run_dirs["demo"])["valve_cycles"]
    healthy = read_frames(run_dirs["healthy"])["valve_cycles"]
    flagged = detect_group_flag(demo, healthy)
    assert flagged == G2_MC, flagged
    assert not (flagged & SANE_29)
    assert len(flagged & G2_MC) >= 3          # allarme gruppo
    assert detect_group_flag(healthy, healthy) == set()
    # margine: tutti i membri sopra la soglia con ratio >= 2 (calibrato 2.66)
    for mc in sorted(G2_MC):
        df = demo.filter(pl.col("machine_code") == mc)
        bf = healthy.filter(pl.col("machine_code") == mc)
        n = df.height
        thr = 3.0 * bf["fillingtime"].std() / (n - 1) ** 0.5
        ratio = (df["fillingtime"].std() - bf["fillingtime"].std()) / thr
        assert ratio >= 2.0, (mc, ratio)


# --------------------------------------------------------------------------
# D3 — rampa: sigma crescente, stazionario dopo
# --------------------------------------------------------------------------
def test_ramp_increases_sigma(run_dirs):
    """D3: su G2, sigma_FT pre-onset ([1,99]) ≈ baseline (entro ±10%),
    sigma in rampa ([100,299]) < sigma a severita piena ([300,562]) su 6/6;
    stazionarieta dopo la rampa: sigma([460,562]) ≈ sigma([300,459]) entro
    ±10% (tolleranza da calibrazione, misurato ±3.9%)."""
    demo = read_frames(run_dirs["demo"])["valve_cycles"]
    healthy = read_frames(run_dirs["healthy"])["valve_cycles"]
    for v in G2:
        mc = f"valve{v}"
        d = demo.filter(pl.col("machine_code") == mc)
        pre = d.filter(pl.col("cycle_id") < START)
        ramp = d.filter((pl.col("cycle_id") >= START)
                        & (pl.col("cycle_id") < START + RAMP))
        full = d.filter(pl.col("cycle_id") >= START + RAMP)
        s_pre, s_ramp, s_full = (pre["fillingtime"].std(),
                                 ramp["fillingtime"].std(),
                                 full["fillingtime"].std())
        s_h = _sigma_h(healthy, mc)
        assert abs(s_pre - s_h) / s_h <= 0.10, (mc, s_pre, s_h)
        assert s_full > s_ramp, (mc, s_full, s_ramp)
        a = d.filter((pl.col("cycle_id") >= START + RAMP)
                     & (pl.col("cycle_id") < 460))
        b = d.filter(pl.col("cycle_id") >= 460)
        assert abs(b["fillingtime"].std() - a["fillingtime"].std()) / \
            a["fillingtime"].std() <= 0.10, (mc, a["fillingtime"].std(),
                                             b["fillingtime"].std())


# --------------------------------------------------------------------------
# R6 — scope global
# --------------------------------------------------------------------------
def test_global_scope_lift(run_dirs):
    """R6/global: Δsigma_FT > soglia 3·sigma_h/sqrt(n-1) su >= 30/35 valvole
    (misurato 35/35); sanity bounds rispettati (FT <= 2130, step <= 26,
    TT max <= 600, fillingok 78.7 ± 3 p.p., target >= 98%)."""
    g = read_frames(run_dirs["global"])
    h = read_frames(run_dirs["healthy"])
    g_cyc, h_cyc = g["valve_cycles"], h["valve_cycles"]
    flagged = detect_group_flag(g_cyc, h_cyc)
    assert len(flagged) >= 30, len(flagged)
    assert g_cyc["fillingtime"].max() <= 2130
    assert g_cyc["filling_step_out"].max() <= 26
    assert g_cyc["tailtime"].max() <= 600
    fok = g_cyc["fillingok"].mean()
    assert 0.757 <= fok <= 0.817, fok
    assert (g_cyc["close_reason"] == "target").mean() >= 0.98
    fs = g["events"].filter(pl.col("event") == "FAULT_START")
    assert fs.height == 1
    assert "scope=global" in fs["note"][0]


# --------------------------------------------------------------------------
# D1/D2 — determinismo e fingerprint
# --------------------------------------------------------------------------
def test_determinism_fingerprint(run_dirs, tmp_path_factory):
    """AC-6: fingerprint(seed 42, m3_demo) ripetuto -> IDENTICO (D1); demo
    vs healthy: fingerprint diversi, le 6 valvole di G2 bit-diverse; le 29
    sane entro le bande statistiche (SANE_BANDS).

    SCOPERTA W2 (documentata in work/m3_calibration.md): a differenza del
    demo M2 (fault che spostano le MEDIE -> ri-sequenziamento ampio del RNG
    PLC condiviso), il fault M3 tocca solo la VARIANZA: nel run compresso
    21/29 valvole sane risultano bit-IDENTICHE al sano (il ri-sequenziamento
    tocca solo le valvole adiacenti in fase carosello: 7,8,9,10,11,18,19,21).
    Il criterio resta le bande statistiche (mai bit-identicità richiesta per
    le sane); qui NON si asserisce la non-bit-identità delle sane.
    """
    out2 = tmp_path_factory.mktemp("m3_demo2") / "out"
    run_days(make_cfg(42), 0.02125, out=out2, progress=False,
             scenario=load_scenario(ROOT / "scenarios" / "m3_demo.yaml"))
    assert fingerprint(out2) == fingerprint(run_dirs["demo"])
    demo = read_frames(run_dirs["demo"])
    healthy = read_frames(run_dirs["healthy"])
    assert fingerprint(run_dirs["demo"]) != fingerprint(run_dirs["healthy"])
    # G2: fisicamente diverse (bit-level) dal run sano
    for v in G2:
        mc = f"valve{v}"
        a = demo["valve_cycles"].filter(pl.col("machine_code") == mc)
        b = healthy["valve_cycles"].filter(pl.col("machine_code") == mc)
        assert csv_bytes(a.drop("scenario_id")) != csv_bytes(b.drop("scenario_id")), mc
    # sane: entro le bande statistiche (bit-identità NON richiesta)
    assert_sane_statistical_bands(demo["valve_cycles"],
                                  healthy["valve_cycles"], SANE_29)


# --------------------------------------------------------------------------
# D1/M2 bounds
# --------------------------------------------------------------------------
def test_bounds_m3(run_dirs):
    """D1: FT <= 2130 e filling_step_out <= 26 su tutti i run M3; nessun
    SAFE_DEPRESSURIZATION né close_reason safety_timeout; TT <= 600: quota
    >= 99% sui cicli G2 del demo e max <= 600 nel run sano compresso."""
    demo = read_frames(run_dirs["demo"])["valve_cycles"]
    g2 = demo.filter(pl.col("machine_code").is_in(list(G2_MC)))
    assert (g2["tailtime"] <= 600).mean() >= 0.99
    for name in ("healthy", "demo", "global"):
        cyc = read_frames(run_dirs[name])["valve_cycles"]
        assert cyc["fillingtime"].max() <= 2130, name
        assert cyc["filling_step_out"].max() <= 26, name
        assert cyc["tailtime"].max() <= 600, name
    healthy = read_frames(run_dirs["healthy"])["valve_cycles"]
    assert healthy["tailtime"].max() <= 600


# --------------------------------------------------------------------------
# QA-F7 — start_cycle=1 su un gruppo (G0)
# --------------------------------------------------------------------------
def test_start_cycle_one_group(run_dirs, tmp_path):
    """QA-F7 integrato: group G0=[0..5] abrupt start_cycle=1 -> iniezione
    PRE-APPLICATA alla costruzione: GT ciclo 1 valorizzata (severity 0.5),
    lift di sigma_FT presente su >= 5/6 membri (tutti i cicli a severita
    piena, n = 562), FAULT_START 1× con group_id=0, 0 SAFE."""
    y = tmp_path / "start1_g0.yaml"
    y.write_text("""scenario_id: 61
name: "scratch start_cycle=1 gruppo G0"
seed: null
faults:
  - fault_type: pressure_instability
    scope: group
    group_id: 0
    severity: 0.5
    onset:
      mode: abrupt
      start_cycle: 1
""", encoding="utf-8")
    out = tmp_path / "out"
    run_days(make_cfg(42), 0.02125, out=out, progress=False,
             scenario=load_scenario(y))
    frames = read_frames(out)
    gt, cyc = frames["ground_truth"], frames["valve_cycles"]
    ev = frames["events"]
    for v in range(6):
        mc = f"valve{v}"
        r1 = gt.filter((pl.col("machine_code") == mc)
                       & (pl.col("cycle_id") == 1)).row(0, named=True)
        assert r1["fault_type"] == "pressure_instability"
        assert r1["severity"] == 0.5
        assert (gt.filter(pl.col("machine_code") == mc)["fault_type"]
                == "pressure_instability").all()
    h = read_frames(run_dirs["healthy"])["valve_cycles"]
    n_ok = 0
    for v in range(6):
        mc = f"valve{v}"
        s = cyc.filter(pl.col("machine_code") == mc)["fillingtime"].std()
        s_h = _sigma_h(h, mc)
        if s >= (1.0 + THETA) * s_h:
            n_ok += 1
    assert n_ok >= 5, n_ok
    fs = ev.filter(pl.col("event") == "FAULT_START")
    assert fs.height == 1
    assert fs["note"][0] == ("pressure_instability severity=0.5 "
                             "start_cycle=1 group_id=0")
    assert ev.filter(pl.col("event") == "SAFE_DEPRESSURIZATION").height == 0
    assert (cyc["close_reason"] == "safety_timeout").sum() == 0


# --------------------------------------------------------------------------
# Join / summary / timeline
# --------------------------------------------------------------------------
def test_scenario_id_join(run_dirs):
    """H8: ogni evento VALVOLA con cycle_id > 0 si aggancia a un cycle
    record OPPURE è allow-listed da DEAD_ZONE 'abort_stop'; MACHINE
    esclusi; nessun orfano; join 1:1 GT↔cycles (pattern M2)."""
    for name in ("healthy", "demo", "global"):
        cyc, ev, gt = (read_frames(run_dirs[name])["valve_cycles"],
                       read_frames(run_dirs[name])["events"],
                       read_frames(run_dirs[name])["ground_truth"])
        keys_c = set(zip(cyc["machine_code"], cyc["cycle_id"],
                         cyc["scenario_id"]))
        keys_g = set(zip(gt["machine_code"], gt["cycle_id"],
                         gt["scenario_id"]))
        assert keys_c == keys_g, name
        evv = ev.filter((pl.col("machine_code") != "MACHINE")
                        & (pl.col("cycle_id") > 0))
        abort = ev.filter((pl.col("event") == "DEAD_ZONE")
                          & (pl.col("note") == "abort_stop"))
        abort_ids = set(zip(abort["machine_code"], abort["cycle_id"]))
        orfani = 0
        for r in evv.iter_rows(named=True):
            if (r["machine_code"], r["cycle_id"], r["scenario_id"]) in keys_c:
                continue
            if (r["machine_code"], r["cycle_id"]) in abort_ids:
                continue
            orfani += 1
        assert orfani == 0, f"{name}: {orfani} eventi orfani"


def test_run_summary_scenario(run_dirs):
    """run_summary.json: scenario_id/scenario_name corretti (50/51/52)."""
    expected = {"healthy": (50, "baseline sana M3 (equiv M2/M1)"),
                "demo": (51, "instabilita pressione gruppo G2 (valvole 12-17)"),
                "global": (52, "instabilita pressione scope global")}
    for name in expected:
        s = json.loads((run_dirs[name] / "run_summary.json")
                       .read_text(encoding="utf-8"))
        assert s["scenario_id"] == expected[name][0], name
        assert s["scenario_name"] == expected[name][1], name


def test_timeline_rows(run_dirs):
    """Timeline: demo -> 6 righe (valve12..17, fault_id 0, end null,
    start_ts per-valvola valorizzato); global -> 35 righe; healthy -> 0."""
    tl = pl.read_parquet(run_dirs["demo"] / "fault_timeline.parquet")
    assert tl.height == 6
    assert tl["valve_id"].to_list() == G2
    assert (tl["fault_id"] == 0).all()
    assert tl["end_cycle"].is_null().all() and tl["end_ts"].is_null().all()
    assert tl["start_ts"].is_not_null().all()   # tutti i membri processati
    assert (tl["fault_type"] == "pressure_instability").all()
    assert (tl["severity"] == SEV).all()
    tlg = pl.read_parquet(run_dirs["global"] / "fault_timeline.parquet")
    assert tlg.height == 35
    assert tlg["valve_id"].to_list() == list(range(35))
    assert (tlg["end_cycle"].is_null().all() and tlg["end_ts"].is_null().all())
    # healthy: nessun fault -> nessuna riga timeline (stream non scritto,
    # comportamento identico a M2)
    assert not (run_dirs["healthy"] / "fault_timeline.parquet").exists()


# --------------------------------------------------------------------------
# H11 — nessun SAFE nei run M3
# --------------------------------------------------------------------------
def test_no_safe(run_dirs):
    """H11: nessun evento SAFE_DEPRESSURIZATION né close_reason
    safety_timeout nei run healthy/demo/global (l'encoder ~2127 ms scatta
    prima del SafetyTimeout 2500 ms -> FT clampato < 2500)."""
    for name in ("healthy", "demo", "global"):
        frames = read_frames(run_dirs[name])
        assert frames["events"].filter(
            pl.col("event") == "SAFE_DEPRESSURIZATION").height == 0, name
        assert (frames["valve_cycles"]["close_reason"]
                == "safety_timeout").sum() == 0, name
