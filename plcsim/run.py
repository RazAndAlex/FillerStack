"""CLI del simulatore V3.

Esempi:
  python -m plcsim.run --days 1 --out work/sim_out
  python -m plcsim.run --selfcheck
  python -m plcsim.run --days 5 --seed 42
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .clock import SimulationClock
from .config import SimConfig
from .plant import Plant
from .plc import PLC
from .scenario import FaultEngine, load_scenario
from .telemetry import Telemetry

ROOT = Path(__file__).resolve().parent.parent


def build_sim(cfg: SimConfig, telemetry: Telemetry,
              start: datetime | None = None) -> tuple[SimulationClock, Plant, PLC]:
    clock = SimulationClock() if start is None else SimulationClock(start=start)
    seeds = np.random.SeedSequence(cfg.seed).spawn(2)   # ADR-0013: stream separati
    plant = Plant(cfg, clock, np.random.default_rng(seeds[0]))
    plc = PLC(cfg, clock, plant, np.random.default_rng(seeds[1]),
              on_cycle=telemetry.on_cycle, collect_events=True)
    return clock, plant, plc


def _run_loop(cfg: SimConfig, telemetry: Telemetry, clock: SimulationClock,
              plant: Plant, plc: PLC, end_ms: int,
              progress: bool = True, engine: FaultEngine | None = None) -> float:
    """Loop bulk: clock accelerato, stessa logica (ADR-0005).

    Per ogni scan (10 ms): plant.step() integra la fisica sull'intervallo
    appena trascorso, poi plc.scan() legge gli impulsi e fa avanzare le
    macchine a stati. Le fasi macchina vuote (non Running) sono saltate alla
    transizione successiva (ADR-0005: ottimizzazione senza cambio di
    semantica). Ritorna il tempo wall-clock impiegato.
    """
    scan = cfg.scan_ms
    machine = plc.machine
    t0 = time.perf_counter()
    t = 0
    prev_status = None
    next_report = 4 * 3600 * 1000
    last_drain = 0
    stall = 0
    while t < end_ms:
        prev_t = t
        if machine.running:
            t += scan
            clock.jump_to(t)
            plant.step(t)
            plc.scan(t)
        else:
            # fase macchina vuota: salto alla transizione successiva
            t = min(machine.entered_ms + machine.duration_ms, end_ms)
            clock.jump_to(t)
            plc.scan(t)
        if t == prev_t:  # guardia anti-stallo (ADR-0005)
            stall += 1
            if stall > 2:
                raise RuntimeError(
                    f"stallo del clock a t={t} ms, stato={machine.status}")
        else:
            stall = 0
        if machine.status != prev_status:
            telemetry.on_machine(machine.status, t)
            prev_status = machine.status
        # event log valvola + engine (ADR-0012/M2): drenaggio periodico
        if (plc.has_events or (engine is not None and engine.has_events)) \
                and t - last_drain >= 1000:
            telemetry.on_events(plc, t)
            if engine is not None:
                telemetry.on_engine_events(engine)
            last_drain = t
        if progress and t >= next_report:
            next_report += 4 * 3600 * 1000
            el = time.perf_counter() - t0
            print(f"  t={t/3.6e6:6.2f} h  stato={machine.status:<9} "
                  f"cicli={telemetry.n_cycles:,}  ({el:.0f}s)", flush=True)
    if plc.has_events:      # drenaggio finale
        telemetry.on_events(plc, t)
    if engine is not None and engine.has_events:
        telemetry.on_engine_events(engine)
    return time.perf_counter() - t0


def run_days(cfg: SimConfig, days: int, out: Path | str,
             progress: bool = True, scenario=None,
             start: datetime | None = None) -> dict:
    """Bulk: clock accelerato, stessa logica (ADR-0005).

    scenario (Scenario M2 | None): se presente, l'engine viene costruito e
    agganciato a plant+telemetry PRIMA del loop; la GT è quella reale e gli
    eventi engine (FAULT_*, CMD:*) confluiscono in events.parquet.
    """
    orologio_telemetria = (SimulationClock() if start is None
                           else SimulationClock(start=start))
    telemetry = Telemetry(orologio_telemetria, out,
                          scenario_id=scenario.scenario_id if scenario else 0)
    clock, plant, plc = build_sim(cfg, telemetry, start=start)
    engine = None
    if scenario is not None:
        engine = FaultEngine(plant, scenario, cfg)
        telemetry.set_engine(engine)
    end_ms = int(days * 24 * 3600 * 1000)
    elapsed = _run_loop(cfg, telemetry, clock, plant, plc, end_ms, progress,
                        engine)
    paths = telemetry.write()
    # metadati del run (sidecar per report/analisi; nessuna interfaccia toccata)
    summary = {
        "days": days, "seed": cfg.seed, "n_cycles": telemetry.n_cycles,
        "elapsed_s": round(elapsed, 2),
        "states_seen": sorted(plc.states_seen),
        "machine_states": [s for s, _ in cfg.machine.day_hours],
        "start": clock.start.isoformat(),
        "scenario_id": scenario.scenario_id if scenario else None,
        "scenario_name": scenario.name if scenario else None,
    }
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "run_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    if progress:
        print(f"  fine: {telemetry.n_cycles:,} cicli in {elapsed:.0f}s "
                  f"({end_ms/1000/max(elapsed, 1e-9):,.0f}x realtime)")
        print(f"  stati valvola visti: {summary['states_seen']}")
    return paths


def selfcheck(cfg: SimConfig, seconds: float = 72000.0) -> None:
    """Verifica rapida: ~20 h di macchina (coprono Idle + Running), sanity
    sulle medie e sugli stati raggiunti."""
    print("== self-check V3 ==")
    print(f"  valvole: {len(cfg.valves)}  gruppi: {len(cfg.groups)}")
    v0 = cfg.valves[0]
    print(f"  valve0: flow_base={v0.flow_base_mls:.1f} ml/s "
          f"tau_close={v0.tau_close_s*1000:.0f} ms k_ramp={v0.k_ramp:.2f}")
    telemetry = Telemetry(SimulationClock(), ROOT / "work" / "selfcheck")
    clock, plant, plc = build_sim(cfg, telemetry)
    end = int(seconds * 1000)
    el = _run_loop(cfg, telemetry, clock, plant, plc, end, progress=True)
    print(f"  cicli in {seconds/3600:.2f}h: {telemetry.n_cycles:,}  "
          f"({el:.0f}s wall, {seconds/max(el, 1e-9):,.0f}x realtime)")
    df = telemetry.collect_cycles()
    if df is not None and df.height:
        for k in ["fillingtime", "tailtime", "tailpulse", "pulsecount"]:
            print(f"  {k}: mean {df[k].mean():.1f}  std {df[k].std():.1f}  "
                  f"[{df[k].min():.0f}..{df[k].max():.0f}]")
        print(f"  step_out max: {df['filling_step_out'].max()} "
              f"(atteso <=26), fillingok: {df['fillingok'].mean():.2%}")
        per_valve = df.group_by("machine_code").len().sort("machine_code")
        print(f"  cicli/valvola: min {per_valve['len'].min()}  "
              f"max {per_valve['len'].max()} (nessuno skip sano atteso)")
    print("  stati valvola visti:", sorted(plc.states_seen))
    print("SELF-CHECK OK")


def _iso_utc(v: str) -> datetime:
    """ISO8601 → datetime UTC. Un valore senza fuso e' letto come UTC."""
    d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _ancoraggio(start: str | None, end: str | None,
                days: int) -> datetime | None:
    """L'istante di partenza del run, o None per il default storico.

    `--end` esiste perche' la domanda vera non e' «quando comincia» ma «quando
    finisce»: uno storico serve a fare da passato, quindi deve arrivare fino a
    adesso. La partenza si ricava all'indietro da `--days`.
    """
    if end is not None:
        fine = (datetime.now(timezone.utc) if end == "now" else _iso_utc(end))
        return fine - timedelta(days=days)
    if start is not None:
        return _iso_utc(start)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Simulatore V3 — Rotary Filler causale")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(ROOT / "work" / "sim_out"))
    ap.add_argument("--scenario", default=None,
                    help="percorso YAML scenario M2 (fault + ground truth)")
    ap.add_argument("--start", default=None,
                    help="istante di partenza del run, ISO8601 UTC "
                         "(default: 2026-06-01T00:00:00Z, l'ancoraggio storico "
                         "del progetto). Mutuamente esclusivo con --end")
    ap.add_argument("--end", default=None,
                    help="istante di FINE del run, ISO8601 UTC oppure 'now': "
                         "la partenza viene calcolata all'indietro da --days. "
                         "Serve a generare uno storico che finisce adesso, "
                         "cosi' il live puo' proseguire da li'")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.start and args.end:
        ap.error("--start e --end sono mutuamente esclusivi: "
                 "l'ancoraggio del run e' uno solo")
    start = _ancoraggio(args.start, args.end, args.days)

    cfg = SimConfig.build(seed=args.seed)
    scenario = None
    if args.scenario:
        scenario = load_scenario(args.scenario)
        # ADR-0013: il seed dello scenario sovrascrive --seed del run
        if scenario.seed is not None:
            cfg = SimConfig.build(seed=scenario.seed)
    if args.selfcheck:
        selfcheck(cfg)
        return 0
    out = run_days(cfg, args.days, args.out, progress=not args.quiet,
                   scenario=scenario, start=start)
    print("output:")
    for k, p in out.items():
        print(f"  {k}: {p} ({p.stat().st_size/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
