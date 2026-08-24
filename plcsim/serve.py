"""Entry point M6 — simulatore con server OPC UA embedded + pacing loop.

Nuovo punto di ingresso della milestone M6 (spec `.scratch/m6/spec.md`,
ADR-0016): il simulatore V3 diventa raggiungibile via OPC UA
(`opc.tcp://localhost:4840`, porta configurabile) con clock
realtime/accelerato/stepped e comandi scrivibili (causa-effetto). Il core
congelato NON viene importato qui (a parte `SimConfig` da .config, che è
importabile per contratto): serve.py importa SOLO .config, .scenario,
.realtime e .opcua_server — nessun import di run.py/telemetry.py, entry
point indipendente (la logica del loop vive già in RealtimeSim).

Modalità di clock (spec §6):
  - realtime (default): pacing wall-clock 1× (scan 10 ms = 10 ms reali).
  - accelerated F: F× reale (`--factor`, es. 100 = 100× reale).
  - stepped: avanzamento SOLO su comando — REPL minimale su stdin
    (advance N / status / quit); il server OPC UA gira comunque nel suo
    thread, così un operatore può connettersi e avanzare a mano.

Senza `--scenario`: run healthy senza engine — le write ForceFault via OPC
UA sono accettate ma senza effetto (warning loggato dal server).

Esempi:
  python -m plcsim.serve                                  # realtime 1×, porta 4840
  python -m plcsim.serve --mode accelerated --factor 100  # soak 100× reale
  python -m plcsim.serve --scenario scenarios/m2_demo.yaml
  python -m plcsim.serve --mode stepped --port 48551     # REPL su stdin
  echo -e "status\\nadvance 2\\nstatus\\nquit" | python -m plcsim.serve \\
      --mode stepped --port 48551                        # scriptabile (EOF chiude)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import SimConfig
from .opcua_server import OpcuaServer
from .realtime import RealtimeSim
from .scenario import load_scenario

__all__ = ("main", "build_parser")

# ore → ms di simulazione (orizzonte assoluto per run_until)
_H_TO_MS = 3600.0 * 1000.0


def _positive_float(value: str) -> float:
    """type argparse: float strettamente positivo (--factor)."""
    try:
        f = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} non è un numero valido") from None
    if f <= 0:
        raise argparse.ArgumentTypeError(
            f"deve essere > 0 (ricevuto {value!r})")
    return f


def build_parser() -> argparse.ArgumentParser:
    """Parser CLI (help in italiano)."""
    ap = argparse.ArgumentParser(
        prog="python -m plcsim.serve",
        description=("Simulatore PLC Sim V con server OPC UA embedded "
                     "(M6, ADR-0016). Endpoint di default: "
                     "opc.tcp://localhost:4840."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "esempi:\n"
            "  python -m plcsim.serve\n"
            "  python -m plcsim.serve --mode accelerated --factor 100\n"
            "  python -m plcsim.serve --scenario scenarios/m2_demo.yaml\n"
            "  python -m plcsim.serve --mode stepped --port 48551\n"
            "  echo -e \"status\\nadvance 2\\nstatus\\nquit\" | "
            "python -m plcsim.serve --mode stepped --port 48551\n"),
    )
    ap.add_argument("--port", type=int, default=4840, metavar="PORT",
                    help="porta TCP del server OPC UA (default 4840). "
                         "Se occupata: errore chiaro in avvio (exit 1).")
    ap.add_argument("--host", default="localhost", metavar="HOST",
                    help="host di bind dell'endpoint OPC UA (default "
                         "'localhost' — invariato rispetto al passato). "
                         "Es. '0.0.0.0' per raggiungere il server da altri "
                         "host/container Docker (vedi collaudo M7, "
                         ".scratch/m6/issues/02-host-bind.md).")
    ap.add_argument("--mode", choices=("realtime", "accelerated", "stepped"),
                    default="realtime",
                    help="modalità di clock (spec §6): realtime = pacing "
                         "wall-clock 1× (default); accelerated = F× reale; "
                         "stepped = avanzamento SOLO via comandi REPL su "
                         "stdin (unica modalità dei test automatici).")
    ap.add_argument("--factor", type=_positive_float, default=1.0,
                    metavar="F",
                    help="fattore di accelerazione (solo modalità "
                         "accelerated; es. 100 = 100× reale). In modalità "
                         "realtime è ignorato (pacing 1×). Errore se <= 0.")
    ap.add_argument("--scenario", default=None, metavar="YAML",
                    help="percorso scenario YAML (load_scenario). File "
                         "mancante o non valido → errore chiaro (exit 1). "
                         "SENZA scenario: run healthy senza engine — le "
                         "write ForceFault sono accettate ma senza effetto "
                         "(warning loggato dal server).")
    ap.add_argument("--seed", type=int, default=42, metavar="SEED",
                    help="seed del run (default 42). Se lo scenario ha un "
                         "seed non nullo, quello sovrascrive --seed "
                         "(ADR-0013, stessa semantica di plcsim.run).")
    ap.add_argument("--exposed-valves", default="1", metavar="LISTA",
                    help="valvole esposte nel namespace OPC UA (indici del "
                         "CONTRATTO 1-35, lista separata da virgola; "
                         "default '1' = Valve01). Es.: '1,2,3'.")
    ap.add_argument("--speed-target", type=float, default=15500.0,
                    metavar="CPH",
                    help="velocità target della ricetta in cph (default "
                         "15500 = Maxima, CONTEXT.md) → tag "
                         "Machine.SpeedTarget.")
    ap.add_argument("--end-hours", type=float, default=None, metavar="H",
                    help="orizzonte di simulazione in ORE (float, es. 24): "
                         "run_until(end_ms = H·3600·1000). Senza: run senza "
                         "orizzonte finché Ctrl-C. Solo modalità "
                         "realtime/accelerated (in stepped è ignorato).")
    ap.add_argument("--publish-ms", type=float, default=10.0, metavar="MS",
                    help="periodo del task di pubblicazione del server in "
                         "ms (avanzato; default 10.0). Deve essere > 0.")
    return ap


def _parse_exposed_valves(raw: str) -> list:
    """--exposed-valves: lista separata da virgola → indici contratto 1-35.

    Il range [1, 35] è validato da TagSnapshot (unica fonte del contratto).
    """
    parts = [p.strip() for p in str(raw).split(",")]
    if not parts or not any(parts):
        raise ValueError("--exposed-valves vuota: attesa lista separata da "
                         "virgola di indici 1-35 (es. '1,2,3')")
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            raise ValueError(
                f"--exposed-valves: {p!r} non è un intero valido "
                f"(attesi indici 1-35)") from None
    return out


def _load_scenario(path: str):
    """Carica lo scenario con messaggi d'errore chiari (exit 1 a monte)."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"scenario non trovato: {p} (--scenario) — usa un percorso "
            f"valido, es. scenarios/m5_healthy.yaml")
    try:
        return load_scenario(p)
    except ValueError as exc:
        raise ValueError(f"scenario non valido ({p}): {exc}") from exc


def _print_status(sim: RealtimeSim) -> None:
    """Stato macchina + contatori snapshot (comando `status`)."""
    m = sim.machine
    tags = sim.snapshot.machine_tags
    print(f"  t={sim.t_ms / _H_TO_MS:8.4f} h  stato={m.status:<9}  "
          f"running={m.running}  SpeedActual={tags['SpeedActual']:.1f}  "
          f"bottiglie={tags['BottleCounter']}  "
          f"cicli_esposti={tags['CycleCounter']}", flush=True)


def _repl_stepped(sim: RealtimeSim) -> None:
    """REPL minimale della modalità stepped (stdin).

    Comandi: `advance [N]` (N iterazioni di loop, default 1), `status`,
    `quit`/EOF. Il server OPC UA gira nel suo thread (avviato prima): un
    operatore può connettersi e avanzare a mano. Se stdin non è una tty
    (pipe), il REPL legge fino a EOF e poi chiude — comportamento
    scriptabile.
    """
    print("  REPL stepped — comandi: advance [N] | status | quit", flush=True)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        if cmd == "advance":
            n = 1
            if len(parts) > 1:
                try:
                    n = int(parts[1])
                except ValueError:
                    print(f"  advance: N non valido ({parts[1]!r}) — atteso "
                          f"intero", flush=True)
                    continue
            if n < 0:
                print(f"  advance: N deve essere >= 0 (ricevuto {n})",
                      flush=True)
                continue
            t_ms, status = sim.advance(n)
            print(f"  t={t_ms / _H_TO_MS:8.4f} h  stato={status:<9}  "
                  f"(dopo {n} iterazioni)", flush=True)
        elif cmd == "status":
            _print_status(sim)
        elif cmd in ("quit", "exit", "q"):
            break
        else:
            print(f"  comando sconosciuto: {parts[0]!r} (attesi: "
                  f"advance [N], status, quit)", flush=True)
    print("  fine REPL (quit/EOF): chiusura", flush=True)


def _run_paced(sim: RealtimeSim, end_ms) -> None:
    """Modalità realtime/accelerated: pacing wall-clock nel thread MAIN."""
    if end_ms is not None:
        print(f"  run fino a t={end_ms / _H_TO_MS:.4f} h di simulazione "
              f"(Ctrl-C per interrompere)", flush=True)
    else:
        print("  run senza orizzonte: Ctrl-C per fermare", flush=True)
    stats = sim.run_until(end_ms)
    print("  fine orizzonte raggiunto", flush=True)
    if stats:
        print(f"  pacing: {stats['n_scans']} scan, "
              f"{stats['sim_ms'] / _H_TO_MS:.4f} h sim in "
              f"{stats['wall_s']:.2f} s wall (drift "
              f"{stats['drift_s'] * 1000.0:.1f} ms, factor "
              f"{stats['factor']})", flush=True)


def _run(args) -> None:
    """Esegue il run (costruzione + loop). Può alzare ValueError/OSError/
    RuntimeError (gestiti in main → exit 1) e KeyboardInterrupt (→ 130)."""
    # -- scenario + seed (ADR-0013, stessa semantica di run.py) -------------
    scenario = None
    if args.scenario:
        scenario = _load_scenario(args.scenario)
    seed = args.seed
    cfg = SimConfig.build(seed=seed)
    if scenario is not None and scenario.seed is not None:
        seed = scenario.seed          # lo scenario sovrascrive --seed
        cfg = SimConfig.build(seed=seed)

    # -- validazioni incrociate + aggiustamenti di modalità -----------------
    mode = args.mode
    factor = args.factor
    if mode == "realtime" and factor != 1.0:
        print(f"  nota: --factor {factor} ignorato in modalità realtime "
              f"(pacing 1×)", flush=True)
        factor = 1.0
    if mode == "stepped" and args.end_hours is not None:
        print(f"  nota: --end-hours ignorato in modalità stepped "
              f"(avanzamento solo via comandi REPL)", flush=True)
    if args.end_hours is not None and args.end_hours <= 0:
        raise ValueError(f"--end-hours deve essere > 0 (ricevuto "
                         f"{args.end_hours!r})")
    exposed = _parse_exposed_valves(args.exposed_valves)

    # -- wiring (spec §3): cfg → RealtimeSim → OpcuaServer ------------------
    sim = RealtimeSim(cfg, seed=seed, mode=mode, factor=factor,
                      exposed_valves=exposed,
                      speed_target_cph=args.speed_target, scenario=scenario)
    server = OpcuaServer(sim, port=args.port, publish_ms=args.publish_ms,
                         host=args.host)

    # -- messaggi di avvio ---------------------------------------------------
    print("PLC Sim V — serve (M6: server OPC UA embedded + pacing loop)",
          flush=True)
    print(f"  endpoint : {server.endpoint}", flush=True)
    print(f"  modalità : {mode}"
          + (f" ×{factor}" if mode == "accelerated" else ""), flush=True)
    print(f"  seed     : {seed}", flush=True)
    scenario_desc = scenario.name if scenario else \
        "nessuno (healthy: nessun engine, ForceFault senza effetto)"
    print(f"  scenario : {scenario_desc}", flush=True)
    print(f"  esposte  : valvole {exposed}", flush=True)
    print(f"  ricetta  : SpeedTarget={args.speed_target} cph", flush=True)
    if args.end_hours is not None and mode != "stepped":
        print(f"  orizzonte: {args.end_hours} h di simulazione", flush=True)

    try:
        server.start()
        print(f"  server OPC UA attivo su {server.endpoint} (pronto)",
              flush=True)
        _print_status(sim)            # stato iniziale macchina
        if mode == "stepped":
            _repl_stepped(sim)
        else:
            end_ms = None
            if args.end_hours is not None:
                end_ms = int(args.end_hours * _H_TO_MS)
            _run_paced(sim, end_ms)
    finally:
        server.stop()
        print("  server OPC UA fermato", flush=True)


def main(argv=None) -> int:
    """Entry point riutilizzabile (pattern run.py): main(argv) -> int.

    Exit codes: 0 = chiusura pulita (orizzonte raggiunto, quit/EOF,
    Ctrl-C gestito); 1 = errore (scenario mancante/non valido, porta
    occupata, argomenti non validi); 130 = KeyboardInterrupt fuori dai
    loop (es. durante l'avvio del server).
    """
    args = build_parser().parse_args(argv)
    try:
        _run(args)
    except KeyboardInterrupt:
        print("\ninterrotto (Ctrl-C): chiusura pulita", flush=True)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"errore: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
