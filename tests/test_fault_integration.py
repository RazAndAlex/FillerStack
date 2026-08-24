"""Test di integrazione M2 (piano M2 §5.4) — catena evento → GT → KPI.

Harness condiviso: run compresso (0.02125 giorni, template TPL di
tests/test_engine) via run_days(cfg, 0.02125, out=tmp_path, progress=False,
scenario=...); i 4 run canonici sono fixture MODULE-scoped (una sola
esecuzione, ≈7 s ciascuno). Nessun output in work/: tutto su tmp_path.

Nota di metodologia (decisione gate W4): le finestre §1.4 sono normative a
SEVERITÀ PIENA. Lo scenario demo è gradual (start_cycle 100, ramp_cycles
200): sul run compresso solo i cicli con cycle_id >= 300 sono a severità
piena — i Δ calcolati su tutti i cicli risulterebbero diluiti dalla rampa.
test_demo_windows filtra quindi i cicli a severità piena (cycle_id in GT con
severity == severità piena del fault); le finestre §1.4 restano quelle
normative e così reggono (verificato: single-fault abrupt passano, fisica
coerente).
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

from plcsim.run import run_days            # noqa: E402
from plcsim.scenario import load_scenario  # noqa: E402
from tests.test_engine import make_cfg     # noqa: E402

# scenario demo: valvola -> (tipo fault, severità piena)
DEMO_FAULTS = {
    "valve0": ("restriction", 0.12),
    "valve1": ("opening_delay", 120.0),
    "valve2": ("restriction", 0.04),
    "valve3": ("closing_delay", 50.0),
    "valve6": ("closing_delay", 100.0),
    "valve7": ("closing_delay", 150.0),
    "valve9": ("opening_delay", 40.0),
    "valve10": ("opening_delay", 80.0),
    "valve12": ("restriction", 0.07),
}
FAULTED_DEMO = set(DEMO_FAULTS)
SANE_VALVES = {f"valve{i}" for i in range(35)} - FAULTED_DEMO

# scenario single-faults: valvola -> (tipo, severità)
SINGLE_FAULTS = {
    "valve1": ("restriction", 0.07),
    "valve5": ("closing_delay", 100.0),
    "valve11": ("opening_delay", 80.0),
}
START_CYCLE_SINGLE = 50   # m2_single_faults
START_CYCLE_DEMO = 100    # m2_demo (gradual, ramp 200 -> piena da 300)

SCENARIOS = {
    "healthy": "m2_healthy.yaml",
    "single": "m2_single_faults.yaml",
    "demo": "m2_demo.yaml",
    "valve820": "m2_valve820.yaml",
}


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
def read_frames(out: Path) -> dict[str, pl.DataFrame]:
    return {name: pl.read_parquet(out / f"{name}.parquet")
            for name in ("valve_cycles", "events", "ground_truth")}


def stats_of(df: pl.DataFrame) -> dict:
    """Medie/quote KPI per-valvola (frame già filtrato sulla valvola)."""
    return {
        "n": df.height,
        "ft": df["fillingtime"].mean(),
        "tt": df["tailtime"].mean(),
        "tp": df["tailpulse"].mean(),
        "pc": df["pulsecount"].mean(),
        "delta": df["deltapulse"].mean(),
        "step26": (df["filling_step_out"] == 26).mean(),
        "encoder": (df["close_reason"] == "encoder_limit").mean(),
        "overtime": (df["filling_overtime"] == True).mean(),
        "fillingok": (df["fillingok"] == True).mean(),
        "tt600": (df["tailtime"] <= 600).mean(),
        "poslim": (df["position_limit"] == True).mean(),
        "ft_max": df["fillingtime"].max(),
        "step_max": df["filling_step_out"].max(),
        "tt_max": df["tailtime"].max(),
    }


def csv_bytes(df: pl.DataFrame) -> bytes:
    """Serializzazione canonica per confronti byte-a-byte."""
    return df.write_csv().encode()


@pytest.fixture(scope="module")
def run_dirs(tmp_path_factory) -> dict[str, Path]:
    """I 4 run compressi canonici, eseguiti UNA volta per modulo."""
    root = tmp_path_factory.mktemp("m2_integration")
    out = {}
    for name, fname in SCENARIOS.items():
        sc = load_scenario(ROOT / "scenarios" / fname)
        d = root / name
        run_days(make_cfg(42), 0.02125, out=d, progress=False, scenario=sc)
        out[name] = d
    return out


@pytest.fixture(scope="module")
def extreme_run(tmp_path_factory) -> Path:
    """Run scratch a severità estrema (restriction 0.5 su valve5)."""
    d = tmp_path_factory.mktemp("m2_extreme")
    y = d / "extreme.yaml"
    y.write_text("""scenario_id: 8
name: "scratch severita estrema"
seed: null
faults:
  - fault_type: restriction
    scope: local
    valve_id: 5
    severity: 0.5
    onset:
      mode: abrupt
      start_cycle: 1
""", encoding="utf-8")
    out = d / "out"
    run_days(make_cfg(42), 0.02125, out=out, progress=False,
             scenario=load_scenario(y))
    return out


def full_severity_ids(gt: pl.DataFrame, valve_id: int, sev_full: float) -> list:
    """Cycle_id della valvola a severità PIENA (dalla GT, esatto)."""
    return gt.filter((pl.col("valve_id") == valve_id)
                     & (pl.col("severity") == sev_full))["cycle_id"].to_list()


# --------------------------------------------------------------------------
# Catene fault → KPI (m2_single_faults)
# --------------------------------------------------------------------------
def _chain_events_and_gt(frames, mc: str, ftype: str, sev, start: int) -> None:
    """(a) eventi + (b) GT: parte comune delle 3 catene."""
    cyc, ev, gt = frames["valve_cycles"], frames["events"], frames["ground_truth"]
    ev_v = ev.filter(pl.col("machine_code") == mc)
    # FAULT_START: una volta, sul primo ciclo affetto, note corrette
    fs = ev_v.filter(pl.col("event") == "FAULT_START")
    assert fs.height == 1, fs.height
    r = fs.row(0, named=True)
    assert r["cycle_id"] == start, r
    assert r["note"] == f"{ftype} severity={sev} start_cycle={start}", r
    # CMD:OPEN/CMD:CLOSE con cycle_id presenti in GT (join 1:1)
    cc = ev_v.filter(pl.col("event") == "CMD:CLOSE")
    co = ev_v.filter(pl.col("event") == "CMD:OPEN")
    gt_ids = set(gt.filter(pl.col("machine_code") == mc)["cycle_id"].to_list())
    assert cc.height == co.height == len(gt_ids)
    assert set(cc["cycle_id"].to_list()) == gt_ids
    # invariante: CMD:CLOSE.note == cycles.close_reason == TAIL.note
    cr = dict(zip(cyc.filter(pl.col("machine_code") == mc)["cycle_id"],
                  cyc.filter(pl.col("machine_code") == mc)["close_reason"]))
    tail = ev_v.filter(pl.col("event") == "TAIL")
    assert tail.height == cc.height
    for row in cc.iter_rows(named=True):
        assert row["note"] == cr[row["cycle_id"]], row
    for row in tail.iter_rows(named=True):
        assert row["note"] == cr[row["cycle_id"]], row
    # GT: fault_type/severity sui cicli >= start, None prima
    g = gt.filter(pl.col("machine_code") == mc)
    pre = g.filter(pl.col("cycle_id") < start)
    assert (pre["fault_type"].is_null()).all() and (pre["severity"] == 0.0).all()
    aff = g.filter(pl.col("cycle_id") >= start)
    assert aff.height > 0
    assert (aff["fault_type"] == ftype).all()
    assert (aff["severity"] == sev).all()


def test_fault_chain_restriction(run_dirs):
    """AC-1: restriction 0.07 su valve1 (m2_single_faults) → catena completa.

    FAULT_START + CMD + GT sui cicli giusti; ΔFT ∈ [+5%, +10%] vs run sano
    stesso seed; quota encoder_limit ≤ 40%.
    """
    frames = read_frames(run_dirs["single"])
    h = read_frames(run_dirs["healthy"])
    _chain_events_and_gt(frames, "valve1", "restriction", 0.07,
                         START_CYCLE_SINGLE)
    s = stats_of(frames["valve_cycles"].filter(
        pl.col("machine_code") == "valve1"))
    h1 = stats_of(h["valve_cycles"].filter(pl.col("machine_code") == "valve1"))
    dft = (s["ft"] - h1["ft"]) / h1["ft"]
    assert 0.05 <= dft <= 0.10, f"ΔFT={dft:.4f}"
    assert s["encoder"] <= 0.40, f"encoder={s['encoder']:.3f}"


def test_fault_chain_closing_delay(run_dirs):
    """AC-2: closing_delay 100 ms su valve5 → ΔTT/ΔTP/ΔFT nelle finestre.

    ΔTP: modello LINEARE (gate W4): ΔTP ≈ q0·k·d/0.1 (flusso pieno per d ms
    — il ritardo sposta la rampa, non la integra) → finestra [+70, +155]
    per d=100 (atteso ≈113; la formula quadratica del piano v1 era errata).
    """
    frames = read_frames(run_dirs["single"])
    h = read_frames(run_dirs["healthy"])
    _chain_events_and_gt(frames, "valve5", "closing_delay", 100.0,
                         START_CYCLE_SINGLE)
    s = stats_of(frames["valve_cycles"].filter(
        pl.col("machine_code") == "valve5"))
    h5 = stats_of(h["valve_cycles"].filter(pl.col("machine_code") == "valve5"))
    assert 70 <= s["tt"] - h5["tt"] <= 130, f"ΔTT={s['tt'] - h5['tt']:.1f}"
    assert 70 <= s["tp"] - h5["tp"] <= 155, f"ΔTP={s['tp'] - h5['tp']:.1f}"
    assert -0.01 <= (s["ft"] - h5["ft"]) / h5["ft"] <= 0.01, "ΔFT"


def test_fault_chain_opening_delay(run_dirs):
    """AC-3: opening_delay 80 ms su valve11 → ΔFT/ΔPC/ΔTT/ΔTP/encoder."""
    frames = read_frames(run_dirs["single"])
    h = read_frames(run_dirs["healthy"])
    _chain_events_and_gt(frames, "valve11", "opening_delay", 80.0,
                         START_CYCLE_SINGLE)
    s = stats_of(frames["valve_cycles"].filter(
        pl.col("machine_code") == "valve11"))
    h11 = stats_of(h["valve_cycles"].filter(pl.col("machine_code") == "valve11"))
    assert 56 <= s["ft"] - h11["ft"] <= 104, f"ΔFT={s['ft'] - h11['ft']:.1f}"
    assert -4 <= s["pc"] - h11["pc"] <= 3, f"ΔPC={s['pc'] - h11['pc']:.1f}"
    assert -10 <= s["tt"] - h11["tt"] <= 10, f"ΔTT={s['tt'] - h11['tt']:.1f}"
    assert -5 <= s["tp"] - h11["tp"] <= 5, f"ΔTP={s['tp'] - h11['tp']:.1f}"
    assert s["encoder"] <= 0.08, f"encoder={s['encoder']:.3f}"


# --------------------------------------------------------------------------
# GT esatta
# --------------------------------------------------------------------------
def test_gt_covers_exact_cycles(run_dirs):
    """AC-4: ogni ciclo completato ha una riga GT (join 1:1 su
    machine_code+cycle_id+scenario_id) e i confini di onset sono esatti
    (ciclo start_cycle−1 → None; start_cycle → valorizzato)."""
    for name, d in run_dirs.items():
        cyc, gt = (read_frames(d)["valve_cycles"],
                   read_frames(d)["ground_truth"])
        keys_c = set(zip(cyc["machine_code"], cyc["cycle_id"],
                         cyc["scenario_id"]))
        keys_g = set(zip(gt["machine_code"], gt["cycle_id"],
                         gt["scenario_id"]))
        assert keys_c == keys_g, f"{name}: join 1:1 GT<->cycles"
    # confini esatti — single_faults (abrupt, start 50)
    gt = read_frames(run_dirs["single"])["ground_truth"]
    for mc, (ftype, sev) in SINGLE_FAULTS.items():
        g = gt.filter(pl.col("machine_code") == mc)
        pre = g.filter(pl.col("cycle_id") < START_CYCLE_SINGLE)
        assert (pre["fault_type"].is_null()).all(), mc
        aff = g.filter(pl.col("cycle_id") == START_CYCLE_SINGLE)
        assert aff.height == 1
        assert aff["fault_type"][0] == ftype and aff["severity"][0] == sev
        assert g["cycle_id"].min() == 1, mc  # contatore PLC, nessun drift
    # confini esatti — demo (gradual, start 100: severity > 0 dal ciclo 100)
    gt = read_frames(run_dirs["demo"])["ground_truth"]
    for mc in FAULTED_DEMO:
        g = gt.filter(pl.col("machine_code") == mc)
        pre = g.filter(pl.col("cycle_id") < START_CYCLE_DEMO)
        assert (pre["fault_type"].is_null()).all(), mc
        aff = g.filter(pl.col("cycle_id") == START_CYCLE_DEMO)
        assert aff.height == 1
        assert aff["fault_type"][0] == DEMO_FAULTS[mc][0]
        assert aff["severity"][0] > 0.0, mc


# --------------------------------------------------------------------------
# Sanity valvole sane / determinismo
# --------------------------------------------------------------------------
def _valve_bytes(df: pl.DataFrame, mc: str, drop_scenario: bool = True):
    d = df.filter(pl.col("machine_code") == mc)
    if drop_scenario:
        d = d.drop("scenario_id")
    return csv_bytes(d)


# Bande statistiche per le valvole SANE (demo vs healthy, stesso seed 42):
# autorizzate dal root — |ΔFT| ≤ 1 ms, |Δstep_out| ≤ 0.1, |ΔTT| ≤ 8 ms,
# |ΔTP| ≤ 4, |ΔPC| ≤ 1.5, |Δfok| ≤ 3 p.p. (misurate: 0.28 / 0.004 /
# 4.13 / 3.14 / 0.12 / 2.14 p.p.).
SANE_BANDS = {
    "fillingtime": 1.0,        # ms
    "filling_step_out": 0.1,
    "tailtime": 8.0,           # ms
    "tailpulse": 4.0,
    "pulsecount": 1.5,
    "fillingok": 0.03,         # 3 p.p.
}


def assert_sane_statistical_bands(demo: pl.DataFrame, healthy: pl.DataFrame,
                                  valves: set[str]) -> None:
    """Medie per-valvola delle valvole sane entro le bande autorizzate.

    NIENTE bit-identicità: l'RNG PLC è CONDIVISO tra le valvole — i fault
    cambiano la durata dei cicli delle valvole guastate → ri-sequenziamento
    dei draw jitter/snap anche sulle sane, più interazioni di confine
    (abort/DEAD_ZONE). Le medie restano statisticamente identiche.
    """
    for mc in sorted(valves):
        d = demo.filter(pl.col("machine_code") == mc)
        b = healthy.filter(pl.col("machine_code") == mc)
        assert d.height > 0 and b.height > 0, mc
        for kpi, band in SANE_BANDS.items():
            diff = abs(d[kpi].mean() - b[kpi].mean())
            assert diff <= band, f"{mc}: |Δ{kpi}|={diff:.4f} > {band}"


def test_sanity_healthy_valves(run_dirs):
    """AC-5(a): nel run demo le 26 valvole sane sono statisticamente
    identiche al run sano stesso seed (medie per-valvola entro le bande
    autorizzate: |ΔFT| ≤ 1 ms, |Δstep_out| ≤ 0.1, |ΔTT| ≤ 8 ms, |ΔTP| ≤ 4,
    |ΔPC| ≤ 1.5, |Δfok| ≤ 3 p.p.).

    RNG PLC condiviso → ri-sequenziamento dei draw; interazioni di confine
    (abort/DEAD_ZONE); le medie restano identiche in senso statistico.
    """
    demo = read_frames(run_dirs["demo"])["valve_cycles"]
    healthy = read_frames(run_dirs["healthy"])["valve_cycles"]
    assert_sane_statistical_bands(demo, healthy, SANE_VALVES)


def test_demo_windows(run_dirs):
    """AC-1/AC-2/AC-3 demo: TUTTE le 9 finestre della tabella §1.4, assertate
    sui cicli a SEVERITÀ PIENA della valvola guastata.

    Metodologia (decisione gate W4): le finestre §1.4 sono normative a
    severità piena; lo scenario demo è gradual (start_cycle 100, ramp_cycles
    200) → la rampa diluisce i Δ sul run compresso. I cicli a severità piena
    sono quelli con severity == severità piena nella GT (cycle_id >= 300).
    Δ = media(cicli pieni) − media(run sano stesso-seed, stessa valvola);
    le quote sono calcolate sui cicli pieni. Le finestre NON sono allargate.
    """
    frames = read_frames(run_dirs["demo"])
    h = read_frames(run_dirs["healthy"])
    cyc, gt = frames["valve_cycles"], frames["ground_truth"]
    for mc, (ftype, sev_full) in DEMO_FAULTS.items():
        valve_id = int(mc.removeprefix("valve"))
        ids = full_severity_ids(gt, valve_id, sev_full)
        assert len(ids) > 200, (mc, len(ids))   # ~264 cicli pieni
        s = stats_of(cyc.filter((pl.col("machine_code") == mc)
                                & pl.col("cycle_id").is_in(ids)))
        b = stats_of(h["valve_cycles"].filter(pl.col("machine_code") == mc))
        dft = (s["ft"] - b["ft"]) / b["ft"]
        dft_ms = s["ft"] - b["ft"]
        dtt = s["tt"] - b["tt"]
        dtp = s["tp"] - b["tp"]
        dpc = s["pc"] - b["pc"]
        dpc_rel = dpc / b["pc"]
        dtp_rel = (s["tp"] - b["tp"]) / b["tp"]
        ctx = f"{mc} ({ftype} {sev_full})"
        if ftype == "restriction":
            if sev_full == 0.04:
                assert 0.028 <= dft <= 0.055, f"{ctx} ΔFT={dft:.4f}"
                assert s["encoder"] <= 0.15, f"{ctx} enc={s['encoder']:.3f}"
                assert 0.35 <= s["step26"] <= 0.65, f"{ctx} s26={s['step26']:.3f}"
                assert 0.30 <= s["overtime"] <= 0.60, f"{ctx} ovt={s['overtime']:.3f}"
            elif sev_full == 0.07:
                assert 0.05 <= dft <= 0.10, f"{ctx} ΔFT={dft:.4f}"
                assert 0.10 <= s["encoder"] <= 0.40, f"{ctx} enc={s['encoder']:.3f}"
                assert 0.55 <= s["step26"] <= 0.85, f"{ctx} s26={s['step26']:.3f}"
                assert 0.65 <= s["overtime"] <= 0.90, f"{ctx} ovt={s['overtime']:.3f}"
                assert -0.10 <= dtp_rel <= -0.04, f"{ctx} ΔTP%={dtp_rel:.4f}"
            else:  # 0.12 (valve0) — saturazione encoder
                assert 2080 <= s["ft"] <= 2130, f"{ctx} FT={s['ft']:.1f}"
                assert 0.50 <= s["encoder"] <= 1.00, f"{ctx} enc={s['encoder']:.3f}"
                assert -0.05 <= dpc_rel <= -0.02, f"{ctx} ΔPC%={dpc_rel:.4f}"
                assert 60 <= s["delta"] <= 100, f"{ctx} delta={s['delta']:.1f}"
                assert 0.10 <= s["fillingok"] <= 0.40, f"{ctx} fok={s['fillingok']:.3f}"
                assert 0.90 <= s["step26"] <= 1.00, f"{ctx} s26={s['step26']:.3f}"
        elif ftype == "closing_delay":
            # ΔTP: finestre LINEARI (gate W4) — vedi test_fault_chain_closing_delay
            assert -0.01 <= dft <= 0.01, f"{ctx} ΔFT={dft:.4f}"
            if sev_full == 50.0:
                assert 35 <= dtt <= 65, f"{ctx} ΔTT={dtt:.1f}"
                assert 35 <= dtp <= 80, f"{ctx} ΔTP={dtp:.1f}"
            elif sev_full == 100.0:
                assert 70 <= dtt <= 130, f"{ctx} ΔTT={dtt:.1f}"
                assert 70 <= dtp <= 155, f"{ctx} ΔTP={dtp:.1f}"
            else:  # 150 (valve7)
                assert 105 <= dtt <= 195, f"{ctx} ΔTT={dtt:.1f}"
                assert s["tt"] <= 480, f"{ctx} TT={s['tt']:.1f}"
                assert 110 <= dtp <= 230, f"{ctx} ΔTP={dtp:.1f}"
                assert s["tt600"] >= 0.99, f"{ctx} tt600={s['tt600']:.4f}"
        else:  # opening_delay
            assert -4 <= dpc <= 3, f"{ctx} ΔPC={dpc:.1f}"
            assert -10 <= dtt <= 10, f"{ctx} ΔTT={dtt:.1f}"
            assert -5 <= dtp <= 5, f"{ctx} ΔTP={dtp:.1f}"
            if sev_full == 40.0:
                assert 28 <= dft_ms <= 52, f"{ctx} ΔFT={dft_ms:.1f}"
                assert 0.20 <= s["step26"] <= 0.45, f"{ctx} s26={s['step26']:.3f}"
                assert s["encoder"] <= 0.05, f"{ctx} enc={s['encoder']:.3f}"
            elif sev_full == 80.0:
                assert 56 <= dft_ms <= 104, f"{ctx} ΔFT={dft_ms:.1f}"
                assert 0.35 <= s["step26"] <= 0.65, f"{ctx} s26={s['step26']:.3f}"
                assert 0.30 <= s["overtime"] <= 0.60, f"{ctx} ovt={s['overtime']:.3f}"
                assert s["encoder"] <= 0.08, f"{ctx} enc={s['encoder']:.3f}"
            else:  # 120 (valve1)
                assert 84 <= dft_ms <= 156, f"{ctx} ΔFT={dft_ms:.1f}"
                assert s["step26"] >= 0.40, f"{ctx} s26={s['step26']:.3f}"
                assert s["overtime"] >= 0.40, f"{ctx} ovt={s['overtime']:.3f}"
                assert s["encoder"] <= 0.08, f"{ctx} enc={s['encoder']:.3f}"


def test_start_cycle_one_integration(tmp_path, run_dirs):
    """QA-F7 integrato: start_cycle=1 → iniezione attiva dal primo ciclo
    (GT ciclo 1 valorizzata, fisica già iniettata alla costruzione)."""
    y = tmp_path / "start1.yaml"
    y.write_text("""scenario_id: 7
name: "scratch start_cycle=1"
seed: null
faults:
  - fault_type: restriction
    scope: local
    valve_id: 6
    severity: 0.07
    onset:
      mode: abrupt
      start_cycle: 1
""", encoding="utf-8")
    out = tmp_path / "out"
    run_days(make_cfg(42), 0.02125, out=out, progress=False,
             scenario=load_scenario(y))
    frames = read_frames(out)
    g6 = frames["ground_truth"].filter(pl.col("machine_code") == "valve6")
    r1 = g6.filter(pl.col("cycle_id") == 1).row(0, named=True)
    assert r1["fault_type"] == "restriction" and r1["severity"] == 0.07
    assert (g6["fault_type"] == "restriction").all()
    s = stats_of(frames["valve_cycles"].filter(
        pl.col("machine_code") == "valve6"))
    b = stats_of(read_frames(run_dirs["healthy"])["valve_cycles"].filter(
        pl.col("machine_code") == "valve6"))
    assert s["ft"] - b["ft"] > 0, f"ΔFT={s['ft'] - b['ft']:.1f}"


def test_extreme_severity(extreme_run):
    """Inviluppo post-gate (§3): restriction 0.5 → encoder domina, MAI SAFE.

    quota encoder_limit ≥ 90% (misurato 100%), position_limit True,
    fill_quality_ok False, filling_overtime True, FT max = 2130 (clamp
    encoder, 2127 < safety 2500 → SAFE irraggiungibile)."""
    frames = read_frames(extreme_run)
    cyc = frames["valve_cycles"]
    v5 = cyc.filter(pl.col("machine_code") == "valve5")
    s = stats_of(v5)
    assert s["encoder"] >= 0.90, f"enc={s['encoder']:.3f}"
    enc = v5.filter(pl.col("close_reason") == "encoder_limit")
    assert (enc["position_limit"] == True).all()
    assert (enc["fill_quality_ok"] == False).all()
    assert (enc["filling_overtime"] == True).all()
    assert s["ft_max"] == 2130, s["ft_max"]
    ev = frames["events"]
    assert ev.filter(pl.col("event") == "SAFE_DEPRESSURIZATION").height == 0
    assert (cyc["close_reason"] == "safety_timeout").sum() == 0


def test_no_safe_in_m2_runs(run_dirs, extreme_run):
    """H11: nessun evento SAFE né close_reason safety_timeout nei run
    healthy/demo/single/valve820 e nel run a severità estrema (l'encoder
    ~2127 ms scatta prima del SafetyTimeout 2500 ms → FT clampato < 2500)."""
    for name, d in run_dirs.items():
        frames = read_frames(d)
        assert frames["events"].filter(
            pl.col("event") == "SAFE_DEPRESSURIZATION").height == 0, name
        assert (frames["valve_cycles"]["close_reason"]
                == "safety_timeout").sum() == 0, name
    frames = read_frames(extreme_run)
    assert frames["events"].filter(
        pl.col("event") == "SAFE_DEPRESSURIZATION").height == 0
    assert (frames["valve_cycles"]["close_reason"]
            == "safety_timeout").sum() == 0


def test_bounds(run_dirs):
    """D1: FT ≤ 2130 e filling_step_out ≤ 26 su tutti i run; TT ≤ 600:
    quota ≥ 99,0% sui cicli GUASTI del demo (valve7 d=150) e max ≤ 600 nel
    run sano compresso (coda reale §1.4)."""
    demo = read_frames(run_dirs["demo"])
    gt_demo = demo["ground_truth"]
    v7_faulted = gt_demo.filter((pl.col("machine_code") == "valve7")
                                & pl.col("fault_type").is_not_null())
    ids = set(v7_faulted["cycle_id"].to_list())
    v7 = demo["valve_cycles"].filter(
        (pl.col("machine_code") == "valve7")
        & pl.col("cycle_id").is_in(list(ids)))
    assert (v7["tailtime"] <= 600).mean() >= 0.99
    for name, d in run_dirs.items():
        cyc = read_frames(d)["valve_cycles"]
        assert cyc["fillingtime"].max() <= 2130, name
        assert cyc["filling_step_out"].max() <= 26, name
    healthy = read_frames(run_dirs["healthy"])["valve_cycles"]
    assert healthy["tailtime"].max() <= 600


def test_healthy_equiv_m1(tmp_path_factory, run_dirs):
    """AC-9: run compresso m2_healthy ≡ percorso M1 (stesso seed 42).

    cycles identici a meno di scenario_id; events identici a meno di
    scenario_id e delle righe CMD:OPEN/CMD:CLOSE (solo engine M2); GT
    identica a meno di scenario_id (fault_type None / severity 0.0 in
    entrambi)."""
    out_m1 = tmp_path_factory.mktemp("m2_m1") / "out"
    run_days(make_cfg(42), 0.02125, out=out_m1, progress=False, scenario=None)
    m1 = read_frames(out_m1)
    h = read_frames(run_dirs["healthy"])
    # cycles
    a = h["valve_cycles"].drop("scenario_id")
    b = m1["valve_cycles"].drop("scenario_id")
    assert csv_bytes(a) == csv_bytes(b)
    # events: healthy senza le righe CMD (solo engine)
    a = h["events"].drop("scenario_id").filter(
        ~pl.col("event").is_in(["CMD:OPEN", "CMD:CLOSE"]))
    b = m1["events"].drop("scenario_id")
    assert csv_bytes(a) == csv_bytes(b)
    # GT
    a = h["ground_truth"].drop("scenario_id")
    b = m1["ground_truth"].drop("scenario_id")
    assert csv_bytes(a) == csv_bytes(b)


def test_determinism_fingerprint(tmp_path_factory, run_dirs):
    """AC-6: SHA-256 di (cycles+events+GT) ripetuto → identico (determinismo
    esatto stesso-seed stesso-scenario); demo vs healthy: le 26 valvole sane
    NON sono bit-identiche (RNG PLC condiviso → ri-sequenziamento draw;
    interazioni di confine) ma le loro medie cadono nelle bande statistiche;
    il fingerprint globale dei due scenari differisce e le 9 guastate hanno
    cicli bit-diversi (regressione stesso-ambiente, QA-F11)."""
    def fingerprint(out: Path) -> str:
        frames = read_frames(out)
        payload = b"".join(
            csv_bytes(frames[k]) for k in ("valve_cycles", "events",
                                           "ground_truth"))
        return hashlib.sha256(payload).hexdigest()

    out2 = tmp_path_factory.mktemp("m2_demo2") / "out"
    run_days(make_cfg(42), 0.02125, out=out2, progress=False,
             scenario=load_scenario(ROOT / "scenarios" / "m2_demo.yaml"))
    assert fingerprint(out2) == fingerprint(run_dirs["demo"])
    # demo vs healthy: valvole sane entro le bande statistiche
    # (NO bit-identicità: RNG condiviso → ri-sequenziamento draw)
    demo = read_frames(run_dirs["demo"])
    healthy = read_frames(run_dirs["healthy"])
    assert_sane_statistical_bands(demo["valve_cycles"],
                                  healthy["valve_cycles"], SANE_VALVES)
    # differenze SOLO sulle guastate: il totale differisce, e le 9 guastate
    # hanno cicli fisicamente diversi (bit-level) dal run sano
    assert fingerprint(run_dirs["demo"]) != fingerprint(run_dirs["healthy"])
    for mc in sorted(FAULTED_DEMO):
        assert _valve_bytes(demo["valve_cycles"], mc) != \
            _valve_bytes(healthy["valve_cycles"], mc), mc


# --------------------------------------------------------------------------
# Detector segnali-only (D5, test-only)
# --------------------------------------------------------------------------
def detect_faulted_valves(cycles: pl.DataFrame, baseline: pl.DataFrame,
                          primary: dict) -> set[str]:
    """Detector dai SOLI segnali di valve_cycles (mai GT).

    primary: {fault_type: KPI primario} (restriction → fillingtime,
    closing_delay → tailtime, opening_delay → fillingtime). Flag se
    |Δmean| > 3·σ·√2/√n con Δmean = media(run guasto) − media(run sano)
    stesso-seed per-valvola: entrambe le medie sono stime rumorose con std
    σ/√n → σ_Δmean = σ·√2/√n (n = cicli nel run guasto, σ = std della
    baseline); l'OR sui KPI primari copre i 3 tipi di fault.

    IMPORTANTE (D5, disposizione MUSE): la baseline stesso-seed è SOLO una
    validazione in-M2 (test-only) — NON è ML-deployable (la stessa baseline
    non può servire da train e da ground-truth in produzione). Vincolo
    post-M2: il layer analytics deve usare seed SEPARATI train/baseline
    (es. train seed A, baseline seed B, validazione seed C).
    """
    kpis = sorted({kpi for kpi in primary.values()})
    flagged: set[str] = set()
    for mc in cycles["machine_code"].unique().to_list():
        df = cycles.filter(pl.col("machine_code") == mc)
        bf = baseline.filter(pl.col("machine_code") == mc)
        n = df.height
        for kpi in kpis:
            d_mean = df[kpi].mean() - bf[kpi].mean()
            sigma = bf[kpi].std()
            if sigma is None or n == 0:
                continue
            if abs(d_mean) > 3.0 * sigma * (2.0 ** 0.5) / (n ** 0.5):
                flagged.add(mc)
                break
    return flagged


def test_detector_flags_only_faulted(run_dirs):
    """AC-7: il detector flagga ESATTAMENTE le 9 valvole guastate del demo,
    nessuna sana (incluse valve8/20 sane-anomale).

    Soglia 3·σ_sano·√2/√n: σ_mean = σ_sano·√2/√n perché Δmean è la
    differenza di DUE medie rumorose (run guasto e run sano, stessa σ).
    """

    demo = read_frames(run_dirs["demo"])["valve_cycles"]
    healthy = read_frames(run_dirs["healthy"])["valve_cycles"]
    primary = {"restriction": "fillingtime",
               "closing_delay": "tailtime",
               "opening_delay": "fillingtime"}
    flagged = detect_faulted_valves(demo, healthy, primary)
    assert flagged == FAULTED_DEMO, flagged
    assert not (flagged & SANE_VALVES)
    # hard signals di conferma sulla valvola a severità severa (valve0):
    # mai da soli, ma coerenti con il flag primario
    s = stats_of(demo.filter(pl.col("machine_code") == "valve0"))
    b = stats_of(healthy.filter(pl.col("machine_code") == "valve0"))
    assert s["encoder"] > b["encoder"] + 0.01
    assert s["poslim"] > 0.0 and b["poslim"] == 0.0
    assert s["overtime"] > b["overtime"] + 0.05


# --------------------------------------------------------------------------
# Scenari aggiuntivi / schema / join
# --------------------------------------------------------------------------
def test_valve820_profile(run_dirs):
    """AC-8: valve4 con restriction 0.06 riproduce il profilo valve8/20
    (FT medio ~2034,5): FT ∈ [2005, 2065], step-26 ∈ [50%, 70%],
    encoder ≤ 35%, filling_overtime > 50%."""
    cyc = read_frames(run_dirs["valve820"])["valve_cycles"]
    s = stats_of(cyc.filter(pl.col("machine_code") == "valve4"))
    assert 2005 <= s["ft"] <= 2065, f"FT={s['ft']:.1f}"
    assert 0.50 <= s["step26"] <= 0.70, f"s26={s['step26']:.3f}"
    assert s["encoder"] <= 0.35, f"enc={s['encoder']:.3f}"
    assert s["overtime"] > 0.50, f"ovt={s['overtime']:.3f}"


def test_demo_severity_ordering(run_dirs):
    """Ordinamento per severità sui Δ (le baseline per-valvola differiscono:
    si confrontano i Δ, non le medie assolute — decisione gate W4)."""
    demo = read_frames(run_dirs["demo"])["valve_cycles"]
    healthy = read_frames(run_dirs["healthy"])["valve_cycles"]
    def delta(mc: str, kpi: str) -> float:
        a = demo.filter(pl.col("machine_code") == mc)[kpi].mean()
        b = healthy.filter(pl.col("machine_code") == mc)[kpi].mean()
        return a - b
    dft = {sev: delta(mc, "fillingtime") for mc, sev in
           (("valve2", 0.04), ("valve12", 0.07), ("valve0", 0.12))}
    assert dft[0.04] < dft[0.07] < dft[0.12], dft
    dtt = {sev: delta(mc, "tailtime") for mc, sev in
           (("valve3", 50), ("valve6", 100), ("valve7", 150))}
    assert dtt[50] < dtt[100] < dtt[150], dtt
    dfto = {sev: delta(mc, "fillingtime") for mc, sev in
            (("valve9", 40), ("valve10", 80), ("valve1", 120))}
    assert dfto[40] < dfto[80] < dfto[120], dfto


def test_engine_events_present(run_dirs):
    """AC-11: FAULT_START/FAULT_RAMP/CMD in events.parquet con
    cycle_id/scenario_id (demo, 9 fault gradual)."""
    ev = read_frames(run_dirs["demo"])["events"]
    assert {"cycle_id", "scenario_id"} <= set(ev.columns)
    assert (ev["scenario_id"] == 42).all()
    fs = ev.filter(pl.col("event") == "FAULT_START")
    assert fs.height == 9, fs.height
    fr = ev.filter(pl.col("event") == "FAULT_RAMP")
    assert fr.height > 0, "FAULT_RAMP assenti"
    assert (fr["note"].str.starts_with("ramp severity=")).all()
    assert (fr["cycle_id"] >= START_CYCLE_DEMO).all()
    for evt in ("CMD:OPEN", "CMD:CLOSE"):
        e = ev.filter(pl.col("event") == evt)
        assert e.height > 0 and e["cycle_id"].min() > 0, evt
    # timeline: 9 righe, fault permanenti (end null)
    tl = pl.read_parquet(run_dirs["demo"] / "fault_timeline.parquet")
    assert tl.height == 9
    assert tl["end_cycle"].is_null().all() and tl["end_ts"].is_null().all()


def test_scenario_id_join(run_dirs):
    """Join events↔GT↔cycles su (machine_code, cycle_id, scenario_id) con
    la semantica H8: ogni evento VALVOLA con cycle_id > 0 si aggancia a un
    cycle record OPPURE è allow-listed da DEAD_ZONE 'abort_stop' (stesso
    machine_code+cycle_id); MACHINE esclusi; nessun orfano residuo."""
    for name, d in run_dirs.items():
        cyc, ev, gt = (read_frames(d)["valve_cycles"],
                       read_frames(d)["events"],
                       read_frames(d)["ground_truth"])
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
            if (r["machine_code"], r["cycle_id"],
                    r["scenario_id"]) in keys_c:
                continue
            if (r["machine_code"], r["cycle_id"]) in abort_ids:
                continue
            orfani += 1
        assert orfani == 0, f"{name}: {orfani} eventi orfani"


def test_run_summary_scenario(run_dirs):
    """run_summary.json contiene scenario_id/scenario_name (AC-11)."""
    expected = {"healthy": (1, "baseline sana M1"),
                "single": (2, "un fault per tipo (mild-2)"),
                "demo": (42, "demo M2 — 3 fault meccanici, 3 severità"),
                "valve820": (3, "profilo valve8/20 come fault iniettato (valve4)")}
    for name, d in run_dirs.items():
        s = json.loads((d / "run_summary.json").read_text(encoding="utf-8"))
        assert s["scenario_id"] == expected[name][0], name
        assert s["scenario_name"] == expected[name][1], name


@pytest.mark.skipif(
    not (ROOT / "work" / "sim_out_5d" / "events.parquet").exists(),
    reason="parquet M1 assenti (work/sim_out_5d)")
def test_m1_events_readable():
    """AC-11: i parquet M1 (schema senza scenario_id) si leggono con polars
    (evoluzione schema documentata, colonna nuova e opzionale)."""
    df = pl.read_parquet(ROOT / "work" / "sim_out_5d" / "events.parquet")
    assert df.height > 0
    assert "scenario_id" not in df.columns
    assert {"ts_beg", "machine_code", "event", "note", "cycle_id"} \
        <= set(df.columns)
