"""Layer 6 — Telemetria (ADR-0012): 3 output separati.

1. valve_cycles.parquet : schema compatibile V2 + quartetto (per la baseline)
2. events.parquet       : transizioni di stato / comandi (tracciabilità fault)
3. ground_truth.parquet : fault per ciclo + fault_timeline (mai mescolata)

In M1 (sano) la ground truth è emessa con fault_type=null per stabilità dello
schema; il fault engine (M2) la popolerà davvero.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import polars as pl

from .clock import SimulationClock
from .plc import PLC
from .validation import CycleRecord

CYCLE_COLUMNS = [
    "machine_code", "ts_beg", "fillingtime", "tailtime", "tailpulse",
    "pulsecount", "target", "deltapulse", "filling_step_out", "fillingok",
    "fill_quality_ok", "sequence_ok", "sample_valid", "diagnostic_status",
    "close_reason", "position_limit", "filling_overtime",
    "cycle_id", "scenario_id",
]

EVENT_COLUMNS = ["ts_beg", "machine_code", "event", "note", "cycle_id",
                 "scenario_id"]

GT_COLUMNS = ["cycle_id", "machine_code", "ts_beg", "fault_type", "severity",
              "valve_id", "scenario_id"]

FAULT_TIMELINE_COLUMNS = ["scenario_id", "fault_id", "fault_type", "valve_id",
                          "severity", "onset_mode", "start_cycle", "end_cycle",
                          "ramp_cycles", "start_ts", "end_ts"]


class Telemetry:
    """Collettore dei 3 stream; scrittura parquet a file singoli.

    Ottimizzazione prestazionale: i record sono scaricati su disco in part
    parquet ogni FLUSH_EVERY cicli (run multi-giorno = milioni di dict:
    tenerli tutti in RAM costa GB); write() concatena le part e il residuo
    in un unico file per stream, con schema invariato.
    """

    FLUSH_EVERY = 200_000        # cicli in buffer prima dello scarico su disco
    FLUSH_EVENTS = 500_000       # eventi in buffer prima dello scarico

    def __init__(self, clock: SimulationClock, out_dir: Path | str,
                 scenario_id: int = 0):
        self.clock = clock
        self.out_dir = Path(out_dir)
        self.scenario_id = scenario_id
        self.cycles: list[dict] = []
        self.events: list[dict] = []
        self.gt: list[dict] = []
        self.fault_timeline: list[dict] = []
        self.n_cycles = 0
        self._engine = None
        self._parts: dict[str, list[Path]] = {
            "cycles": [], "events": [], "gt": [], "fault_timeline": []}
        self._flush_n = 0

    def set_engine(self, engine) -> None:
        """Aggancia il fault engine (M2): la GT diventa quella reale."""
        self._engine = engine

    # -- collettori ----------------------------------------------------------
    def on_cycle(self, rec: CycleRecord) -> None:
        d = asdict(rec)
        d["ts_beg"] = self.clock.ts_at(rec.ts_beg)
        d["scenario_id"] = self.scenario_id
        self.cycles.append({k: d[k] for k in CYCLE_COLUMNS})
        if self._engine is not None:
            # ground truth reale dal fault engine (M2): ts_beg arriva in ms
            # virtuali (int), convertito qui come per i cycle record
            row = self._engine.on_cycle(rec)
            row["ts_beg"] = self.clock.ts_at(row["ts_beg"])
            self.gt.append(row)
        else:
            # ground truth: in M1 nessun fault (schema stabile per M2)
            self.gt.append({
                "cycle_id": rec.cycle_id, "machine_code": rec.machine_code,
                "ts_beg": d["ts_beg"], "fault_type": None, "severity": 0.0,
                "valve_id": int(rec.machine_code.replace("valve", "")),
                "scenario_id": self.scenario_id,
            })
        self.n_cycles += 1
        if len(self.cycles) >= self.FLUSH_EVERY:
            self._flush()

    def on_events(self, plc: PLC, t_ms: int) -> None:
        """Drena gli eventi valvola accumulati dal PLC dall'ultima chiamata.

        Nel buffer ts_beg resta in ms virtuali (int): la conversione a
        datetime è fatta in scrittura in modo vettoriale (polars), per non
        pagare l'aritmetica datetime per evento nel loop caldo.
        """
        q = plc.take_events()
        if not q:
            return
        valves = plc.cfg.valves
        for (ms, vi, name, note, cid) in q:
            self.events.append({
                "ts_beg": ms,
                "machine_code": valves[vi].machine_code,
                "event": name, "note": note, "cycle_id": cid,
                "scenario_id": self.scenario_id,
            })
        if len(self.events) >= self.FLUSH_EVENTS:
            self._flush()

    def on_engine_events(self, engine) -> None:
        """Drena gli eventi del fault engine (FAULT_*, CMD:*) nel buffer eventi.

        Stessa ottimizzazione del PLC: ts_beg resta in ms virtuali (int), la
        conversione a datetime avviene in _frame (vettoriale, in scrittura).
        """
        q = engine.take_events()
        if not q:
            return
        for (ms, machine_code, name, note, cid) in q:
            self.events.append({
                "ts_beg": ms,
                "machine_code": machine_code,
                "event": name, "note": note, "cycle_id": cid,
                "scenario_id": self.scenario_id,
            })
        if len(self.events) >= self.FLUSH_EVENTS:
            self._flush()

    def on_machine(self, status: str, t_ms: int) -> None:
        self.events.append({
            "ts_beg": t_ms, "machine_code": "MACHINE",
            "event": f"STATE:{status}", "note": "", "cycle_id": 0,
            "scenario_id": self.scenario_id,
        })

    # -- flush / scrittura ---------------------------------------------------
    _STREAMS = (("cycles", CYCLE_COLUMNS, "valve_cycles.parquet"),
                ("events", EVENT_COLUMNS, "events.parquet"),
                ("gt", GT_COLUMNS, "ground_truth.parquet"),
                ("fault_timeline", FAULT_TIMELINE_COLUMNS,
                 "fault_timeline.parquet"))

    def _frame(self, name: str, cols: list[str], buf: list[dict]) -> pl.DataFrame:
        """DataFrame da buffer; converte ts ms -> datetime in scrittura
        (vettoriale: il buffer tiene i ms per non pagare datetime per evento)."""
        if name == "gt":
            # fault_type: le prime righe di un run guasto sono tutte None
            # (cicli sani pre-onset / valvole sane) → polars tipizzerebbe la
            # colonna come Null e il primo flush fallirebbe appena arriva una
            # stringa: forziamo String (None → null, stringhe ok)
            df = pl.DataFrame(buf, schema=cols,
                              schema_overrides={"fault_type": pl.String})
        else:
            df = pl.DataFrame(buf, schema=cols)
        if name == "events":
            df = df.with_columns(
                (pl.lit(self.clock.start)
                 + pl.col("ts_beg").cast(pl.Duration(time_unit="ms")))
                .cast(pl.Datetime(time_unit="us", time_zone="UTC"))
                .alias("ts_beg"))
        elif name == "fault_timeline":
            # start_ts/end_ts in ms virtuali (int); end_ts è null per i fault
            # permanenti (M2) — la conversione è null-safe (null → null)
            df = df.with_columns(
                (pl.lit(self.clock.start)
                 + pl.col("start_ts").cast(pl.Int64)
                 .cast(pl.Duration(time_unit="ms")))
                .cast(pl.Datetime(time_unit="us", time_zone="UTC"))
                .alias("start_ts"))
            df = df.with_columns(
                (pl.lit(self.clock.start)
                 + pl.col("end_ts").cast(pl.Int64)
                 .cast(pl.Duration(time_unit="ms")))
                .cast(pl.Datetime(time_unit="us", time_zone="UTC"))
                .alias("end_ts"))
        return df

    def _flush(self) -> None:
        """Scarica i buffer pieni in part parquet (memoria limitata)."""
        part_dir = self.out_dir / "_parts"
        for name, cols, _ in self._STREAMS:
            buf = getattr(self, name)
            if not buf:
                continue
            part_dir.mkdir(parents=True, exist_ok=True)
            p = part_dir / f"{name}_{self._flush_n}.parquet"
            self._frame(name, cols, buf).write_parquet(p)
            self._parts[name].append(p)
            buf.clear()
        self._flush_n += 1

    def _collect(self, name: str, cols: list[str]) -> pl.DataFrame | None:
        """Stream completo (part su disco + residuo in memoria)."""
        frames = [pl.read_parquet(p) for p in self._parts[name]]
        buf = getattr(self, name)
        if buf:
            frames.append(self._frame(name, cols, buf))
        if not frames:
            return None
        return frames[0] if len(frames) == 1 else pl.concat(frames)

    def collect_cycles(self) -> pl.DataFrame | None:
        """Tutti i cycle record come DataFrame (per selfcheck/analisi)."""
        return self._collect("cycles", CYCLE_COLUMNS)

    def write(self) -> dict[str, Path]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        part_dir = self.out_dir / "_parts"
        # fault_timeline: popolato a scrittura dall'engine agganciato (una riga
        # per fault dichiarato); senza engine o senza fault → stream non scritto
        if self._engine is not None:
            rows = self._engine.timeline()
            if rows:
                self.fault_timeline = rows
        # Scarica prima il residuo in memoria, cosi' tutto il flusso vive su
        # disco come part; poi scrive il file finale in STREAMING.
        # Perche' non concatenare in memoria: un run di 60 giorni produce ~36
        # milioni di cicli e un volume di eventi molto maggiore. Rileggere
        # tutte le part per concatenarle esaurirebbe la RAM alla FINE di una
        # generazione di ore — il momento peggiore in cui fallire.
        self._flush()
        for name, cols, fname in self._STREAMS:
            parti = self._parts[name]
            p = self.out_dir / fname
            if not parti:
                continue
            if len(parti) == 1:
                pl.read_parquet(parti[0]).write_parquet(p)
            else:
                pl.scan_parquet(parti).sink_parquet(p)
            out[name] = p
        # pulizia delle part intermedie
        if part_dir.exists():
            for p in part_dir.glob("*.parquet"):
                p.unlink()
            part_dir.rmdir()
        self._parts = {"cycles": [], "events": [], "gt": [], "fault_timeline": []}
        return out
