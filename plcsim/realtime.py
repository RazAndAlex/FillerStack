"""Layer realtime M6 — pacing loop + layer comandi macchina + snapshot tag.

Moduli nuovi per l'ADR-0016 (server OPC UA embedded): il simulatore diventa
raggiungibile a clock reale/accelerato/stepped, con comandi macchina
scrivibili (causa-effetto verificabile nella simulazione) e snapshot di tag
per il bridge del server. Il core congelato (plc.py, validation.py,
config.py, plant.py, run.py) NON viene toccato: nessuna modifica ai file,
nessun monkey-patch dei metodi — solo stato dell'istanza dove esplicitamente
previsto (D2, layer comandi). La modalità realtime è **fuori fingerprint di
determinismo** (ADR-0016): il fingerprint resta definito SOLO sul bulk
`python -m plcsim.run`; i test automatici M6 usano la modalità stepped,
riproducibile (stesso seed + stessa sequenza advance + stessi comandi ⇒
stessa evoluzione di stato).

Loop: stessa logica di `run._run_loop` (ADR-0005) — scan a 10 ms, skip
delle fasi macchina vuote alla transizione successiva, guardia anti-stallo.
La logica è COPIATA, non importata: realtime.py non importa run.py né
telemetry.py (nessun import circolare; M6 non usa la telemetria). Unica
eccezione prevista: se c'è un comando in coda, l'iterazione applica il
comando e fa UN passo scan singolo invece dello skip, per rendere
osservabile la transizione di stato (Stopping/Starting/Resetting).

Layer comandi (D2, MachineCommandLayer): il MachineController non ha API
pubblica di comando (core congelato, stato macchina minimo ADR-0006),
quindi il layer sovrascrive il template dell'istanza (`_tpl`, `_idx`,
`_entered_ms`, `_dur` — solo stato dell'istanza). Dopo il primo comando
l'override resta attivo: il template giornata
(Idle→Starting→Running→Stopping→Stopped) è sospeso finché non arriva un
altro comando. Mappature:

    CmdStop  → [Stopping 500 ms, Stopped durata lunga]
    CmdStart → [Starting 500 ms, Running durata lunga]
    CmdReset → [Resetting 500 ms, Idle durata lunga]

Verificato sperimentalmente: l'override `_tpl/_idx/_entered_ms/_dur`
funziona (Stopping→Stopped alla scadenza; `machine.step()` avanza nel
template override) e la transizione Running→non-Running abortisce i cicli
in corso con drain del plant (interlock già in plc.scan: comportamento bulk
identico). `speed_by_status` non contiene "Resetting": la velocità dello
snapshot usa `.get(status, 0.0)` (config congelata non modificata).

Pacing (run_until): modalità realtime/accelerated con sleep per scan
(scan_ms/factor reali; se il calcolo sfora non dorme — best effort, drift
misurato in pacing_stats). pacing_stats distingue gli scan singoli (drift_s)
dagli skip del template giornata (n_skips/skipped_ms): sim_ms include gli
skip, drift_s NO (solo scan singoli). Lo skip delle fasi vuote è consentito
SOLO
finché la macchina segue il template giornata (nessun comando mai
applicato): appena un comando viene applicato (override attivo) il salto è
disabilitato e la macchina procede scan-by-scan. Così (a) la demo realtime
parte con Idle/Starting saltate istantaneamente (come il bulk) e poi pacera
Running; (b) dopo CmdStop la macchina resta Stopped a pacing reale — niente
fast-forward all'orizzonte, niente riavvio schedulato dal template; (c) in
stepped le transizioni dei comandi sono osservabili (Stopping per 50 scan =
500 ms, poi Stopped). advance() mantiene il salto del template giornata
(orizzonte illimitato) per la riproducibilità dei test.
"""
from __future__ import annotations

import copy
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Sequence

from numpy.random import SeedSequence, default_rng

from .clock import SimulationClock
from .config import SimConfig
from .plant import Plant
from .plc import PLC
from .scenario import FaultEngine, Scenario
from .validation import CycleRecord

# Mappa OMAC (spec M6 §4.1 / CONTEXT.md): stato macchina -> codice
OMAC_CODES = {
    "Running": 1, "Stopping": 2, "Stopped": 3, "Idle": 4,
    "Resetting": 5, "Starting": 11,
}

# Nomi comandi accettati dal layer (D2)
COMMAND_NAMES = frozenset({"stop", "start", "reset"})

# Mappatura CycleRecord -> tag ValveNN.* (spec M6 §4.2)
_VALVE_TAG_FROM_REC = (
    ("FillingTime_ms", "fillingtime"),
    ("TailTime_ms", "tailtime"),
    ("TailPulse", "tailpulse"),
    ("PulseCount", "pulsecount"),
    ("Target", "target"),
    ("DeltaPulse", "deltapulse"),
    ("FillingStepOut", "filling_step_out"),
    ("FillingOK", "fillingok"),
    ("FillQualityOK", "fill_quality_ok"),
    ("SequenceOK", "sequence_ok"),
    ("SampleValid", "sample_valid"),
    ("DiagnosticStatus", "diagnostic_status"),
    # M9 (ADR-0020, issue M9-01): 3 tag per le feature ML mancanti nel
    # contratto M6 — output deterministici del PLC (validation.py), NON GT.
    ("CloseReason", "close_reason"),
    ("PositionLimit", "position_limit"),
    ("FillingOvertime", "filling_overtime"),
    ("LastCycleId", "cycle_id"),
)

__all__ = ("RealtimeSim", "TagSnapshot", "MachineCommandLayer", "OMAC_CODES",
           "COMMAND_NAMES", "StorageBridge")


def _valve_index(machine_code: str) -> int:
    """Indice valvola interno 0-based dal machine_code ('valve0'..'valve34').

    -1 se il codice non identifica una valvola (non dovrebbe accadere:
    il CycleRecord nasce sempre da una valvola del simulatore).
    """
    if machine_code.startswith("valve"):
        try:
            return int(machine_code[len("valve"):])
        except ValueError:
            return -1
    return -1


class TagSnapshot:
    """Snapshot thread-safe dei tag del contratto M6 (spec §4, namespace v1).

    L'oggetto root "Filler01" è gestito dal server: qui vivono i tag
    Machine.* e ValveNN.*. Ogni update di un tag appende (tag_path, valore)
    al log dei cambiamenti: `drain_changes()` lo svuota per il bridge del
    server (push immediato). Il log serve alla modalità realtime: il pulse
    DataReady dura un solo scan (10 ms reali) e il poll del server
    (10-50 ms) potrebbe perderlo; drenando il log a ogni tick il server
    pubblica subito i cambi.

    `exposed_valves` usa gli indici del CONTRATTO (1-35, come FaultValve
    della spec): internamente le valvole del simulatore sono 0-based
    (cfg.valves, machine_code 'valve0'..'valve34') — conversione = indice
    contratto - 1. Default: [1] (Valve01 = valve0 interna).
    """

    def __init__(self, exposed_valves: Optional[Sequence[int]] = None,
                 speed_target_cph: float = 15500.0, n_valves: int = 35):
        if exposed_valves is None:
            exposed_valves = [1]
        self.exposed_valves = sorted({int(v) for v in exposed_valves})
        for v in self.exposed_valves:
            if not 1 <= v <= n_valves:
                raise ValueError(
                    f"valvola esposta {v} fuori range [1, {n_valves}] "
                    "(indici del contratto)")
        self._exposed_internal = {v - 1 for v in self.exposed_valves}
        self._speed_target_cph = float(speed_target_cph)
        self._lock = threading.Lock()
        # Cap esplicito (review-A M6-A2): senza consumer (uso standalone/
        # soak) il log crescerebbe senza limite (~153 B/entry → ~185 MB/
        # giorno sim). maxlen=200_000 ≈ 30 MB: oltre il cap si perdono i
        # pulse PIÙ VECCHI — accettato (spec §5 ammette già pulse persi).
        # Contratto di drenaggio: il server drena ogni publish_ms; in
        # standalone il cap limita la memoria.
        self._changes: deque[tuple[str, object]] = deque(maxlen=200_000)
        self._tags: dict[str, dict] = {
            "Machine": {
                "Running": False, "State": 4, "SpeedActual": 0.0,
                "SpeedTarget": self._speed_target_cph,
                "BottleCounter": 0, "CycleCounter": 0, "DataReady": False,
            },
        }
        for v in self.exposed_valves:
            self._tags[f"Valve{v:02d}"] = {
                "FillingTime_ms": 0, "TailTime_ms": 0, "TailPulse": 0,
                "PulseCount": 0, "Target": 0, "DeltaPulse": 0,
                "FillingStepOut": 0, "FillingOK": False, "FillQualityOK": False,
                "SequenceOK": False, "SampleValid": False,
                "DiagnosticStatus": "NORMAL",
                # M9 (issue M9-01): CloseReason/PositionLimit/FillingOvertime.
                # CloseReason: None (null) e NON "" — lo schema envelope
                # v1.2/v1.3 ha enum {target|encoder_limit|safety_timeout|
                # tail_timeout} + null: la stringa vuota NON è nello enum e
                # produrrebbe un record schema_invalid (FINDING M9-11).
                "CloseReason": None, "PositionLimit": False,
                "FillingOvertime": False,
                "LastCycleId": 0,
            }

    # -- mutazioni ---------------------------------------------------------
    def _set_locked(self, group: str, tag: str, value) -> None:
        """Aggiorna un tag e appende (path, valore) SOLO se cambia davvero.

        Da chiamare con il lock già acquisito (lock non rientrante).
        """
        old = self._tags[group][tag]
        if old != value:
            self._tags[group][tag] = value
            self._changes.append((f"{group}.{tag}", value))

    def _set(self, group: str, tag: str, value) -> None:
        with self._lock:
            self._set_locked(group, tag, value)

    def on_cycle(self, rec: CycleRecord) -> None:
        """Hook al confine di ciclo (chiamato dal PLC durante lo scan).

        Ogni ciclo chiuso (tutte le valvole) incrementa BottleCounter; se il
        ciclo appartiene a una valvola esposta aggiorna anche ValveNN.*,
        incrementa CycleCounter e pone DataReady=TRUE (pulse di 1 scan,
        spec §5).
        """
        with self._lock:
            self._tags["Machine"]["BottleCounter"] += 1
            self._changes.append(
                ("Machine.BottleCounter", self._tags["Machine"]["BottleCounter"]))
        vi = _valve_index(rec.machine_code)
        if vi in self._exposed_internal:
            group = f"Valve{vi + 1:02d}"
            with self._lock:
                g = self._tags[group]
                for tag, attr in _VALVE_TAG_FROM_REC:
                    val = getattr(rec, attr)
                    if g[tag] != val:
                        g[tag] = val
                        self._changes.append((f"{group}.{tag}", val))
                self._tags["Machine"]["CycleCounter"] += 1
                self._changes.append(
                    ("Machine.CycleCounter",
                     self._tags["Machine"]["CycleCounter"]))
                self._set_locked("Machine", "DataReady", True)

    def begin_scan(self) -> None:
        """Inizio iterazione: DataReady=FALSE (il pulse dura 1 scan, spec §5)."""
        self._set("Machine", "DataReady", False)

    def update_machine(self, running: bool, state_code: int,
                       speed_actual: float) -> None:
        """Aggiorna i tag macchina dal MachineController (fine iterazione)."""
        self._set("Machine", "Running", bool(running))
        self._set("Machine", "State", int(state_code))
        self._set("Machine", "SpeedActual", float(speed_actual))

    # -- letture -----------------------------------------------------------
    def read(self) -> dict:
        """Copia profonda thread-safe di tutti i tag (dict annidato)."""
        with self._lock:
            return copy.deepcopy(self._tags)

    @property
    def machine_tags(self) -> dict:
        """Copia thread-safe dei tag Machine.*."""
        with self._lock:
            return dict(self._tags["Machine"])

    def drain_changes(self) -> list:
        """Svuota il log dei cambiamenti: list[(tag_path, nuovo_valore)].

        Ogni update di tag (machine, ValveNN.*, CycleCounter, BottleCounter,
        DataReady) appende (path, valore) al log; il drain restituisce le
        coppie accumulate dall'ultima chiamata e svuota il log.
        """
        with self._lock:
            out = list(self._changes)
            self._changes.clear()
        return out


class MachineCommandLayer:
    """Layer comandi macchina (D2): override del template dell'istanza.

    Il MachineController non ha API pubblica di comando (core congelato,
    ADR-0006): il layer sostituisce lo stato dell'istanza (`_tpl`, `_idx`,
    `_entered_ms`, `_dur`) con il template override del comando — nessun
    monkey-patch di metodi. Dopo il primo comando l'override resta attivo:
    il template giornata è sospeso finché non arriva un altro comando.

    Mappature (fase transitoria 500 ms, fase finale "lunga" 999999 h):

        stop  → [Stopping 500 ms, Stopped lunga]
        start → [Starting 500 ms, Running lunga]
        reset → [Resetting 500 ms, Idle lunga]

    Applicabilità: stop solo da Running/Starting; start solo da
    Stopped/Idle; reset solo da Stopped. Altrimenti no-op, ma il comando è
    comunque consumato (pulse esaurito, spec §11 Q2: auto-reset lato
    server).
    """

    TRANSITION_MS = 500
    LONG_HOURS = 999999.0

    # (stato, ore): la durata in ms è derivata da
    # MachineController._compute_duration (ore*3600*1000, round); la prima
    # fase è comunque impostata direttamente a TRANSITION_MS.
    OVERRIDES = {
        "stop": [("Stopping", TRANSITION_MS / 3600.0 / 1000.0),
                 ("Stopped", LONG_HOURS)],
        "start": [("Starting", TRANSITION_MS / 3600.0 / 1000.0),
                  ("Running", LONG_HOURS)],
        "reset": [("Resetting", TRANSITION_MS / 3600.0 / 1000.0),
                  ("Idle", LONG_HOURS)],
    }
    ALLOWED = {
        "stop": frozenset({"Running", "Starting"}),
        "start": frozenset({"Stopped", "Idle"}),
        "reset": frozenset({"Stopped"}),
    }

    def __init__(self, machine):
        self.machine = machine
        # diagnostica: ultimo comando processato ed esito applicabilità
        self.last_command: Optional[dict] = None
        self.last_command_applied: Optional[bool] = None

    def apply(self, cmd: dict, t_ms: int) -> bool:
        """Applica il comando sovrascrivendo il template della macchina.

        Ritorna True se applicato, False se no-op (stato non ammesso).
        Il comando è consumato in entrambi i casi (chiamato dal loop).
        """
        name = cmd["name"]
        allowed = self.ALLOWED.get(name, frozenset())
        applied = self.machine.status in allowed
        if applied:
            self.machine._tpl = list(self.OVERRIDES[name])
            self.machine._idx = 0
            self.machine._entered_ms = t_ms
            self.machine._dur = self.TRANSITION_MS
        self.last_command = dict(cmd)
        self.last_command_applied = applied
        return applied


class StorageBridge:
    """Best-effort: transizioni OMAC + bottle_counter verso lo storage (OEE L0).

    Il V3 è la sorgente realtime del wire OEE Home (spec dashboard §7.2,
    oee-backend-spec §C1): a ogni CAMBIO di stato macchina chiude la
    transizione precedente e ne apre una nuova su
    `machine_state_history` (append-only, entered_ts = now UTC,
    source="realtime"); `on_cycle` aggiorna il KV `bottle_counter` su
    `machine_state`. Import LAZY di pipeline.storage e connessione al
    primo uso: se il modulo o il DB non sono disponibili il bridge si
    disabilita silenziosamente (errors += 1, mai raise) — la simulazione
    non deve MAI dipendere dallo storage.

    Attivo solo se `PLCSIM_DATABASE_URL` è impostata nell'ambiente o se
    `url` è passato esplicito: i test del simulatore (senza env) non
    subiscono alcuna connessione DB. Lo schema (create_all) resta compito
    esplicito della pipeline (init dei tool/tests), non del bridge.
    """

    def __init__(self, url: str | None = None):
        self._url = url or os.environ.get("PLCSIM_DATABASE_URL")
        self._storage = None
        self._last_code: Optional[int] = None
        self._active = self._url is not None
        self.errors = 0  # diagnostica (test/report)

    @property
    def active(self) -> bool:
        """True se il bridge può scrivere (URL configurato e nessun errore)."""
        return self._active

    def _ensure(self) -> None:
        """Connessione lazy al primo uso (best-effort, mai raise)."""
        if self._storage is not None or not self._active:
            return
        try:
            from pipeline.storage import Storage, make_engine
            self._storage = Storage(make_engine(self._url))
        except Exception:  # noqa: BLE001 — best-effort, mai rompere la sim
            self._storage = None
            self._active = False
            self.errors += 1

    def on_state(self, state_code: int, state_label: str,
                 source: str = "realtime") -> None:
        """Da chiamare a ogni scan: logga SOLO i cambi di stato (idempotente).

        Al cambio: chiude la transizione aperta (exited_ts = now) e apre la
        nuova (entered_ts = now). Mai raise.
        """
        if state_code == self._last_code:
            return
        self._ensure()
        if self._storage is None:
            return
        try:
            now = datetime.now(timezone.utc)
            if self._last_code is not None:
                self._storage.close_machine_state_history(now)
            self._storage.log_machine_state_history(
                state_code, state_label, entered_ts=now, source=source)
            self._last_code = state_code
        except Exception:  # noqa: BLE001 — best-effort
            self._storage = None
            self._active = False
            self.errors += 1

    def on_cycle(self, bottle_counter: int) -> None:
        """Persiste il contatore bottiglie corrente (KV, best-effort)."""
        self._ensure()
        if self._storage is None:
            return
        try:
            self._storage.set_bottle_counter(bottle_counter)
        except Exception:  # noqa: BLE001 — best-effort
            self._storage = None
            self._active = False
            self.errors += 1


class RealtimeSim:
    """Simulatore M6 con clock reale/accelerato/stepped (D1).

    Costruisce SimulationClock + Plant + PLC con gli stessi stream RNG di
    run.build_sim (`SeedSequence(seed).spawn(2)`, ADR-0013), un FaultEngine
    opzionale (se scenario) e il TagSnapshot (on_cycle del PLC). Seed degli
    stream: se lo scenario dichiara seed non null, quello sovrascrive SEMPRE
    il seed/cfg (ADR-0013, stessa semantica di run.py — anche su un cfg
    esplicito: lo scenario vince). Il loop è
    la stessa logica di run._run_loop (ADR-0005) con un'eccezione: comando
    in coda → applica il comando e fa un passo scan singolo invece dello
    skip delle fasi vuote.

    Modalità:
      - stepped (default): `advance(n)` esegue n iterazioni di loop dal
        chiamante (nessun thread). Unica modalità dei test automatici M6.
      - realtime / accelerated: `run_until(end_ms)` con pacing wall-clock
        (sleep per scan = scan_ms/factor reali; se il calcolo sfora non
        dorme — best effort, drift misurato in pacing_stats).
        `run_until(None)` gira finché `stop()`.

    Comandi macchina (D2): `submit_command({"name": "stop"|"start"|"reset"})`
    — coda thread-safe consumata dal loop. `command_pending` segnala al
    bridge se ci sono comandi non ancora applicati.
    """

    def __init__(self, cfg: Optional[SimConfig] = None, seed: int = 42,
                 mode: str = "stepped", factor: float = 1.0,
                 exposed_valves: Optional[Sequence[int]] = None,
                 speed_target_cph: float = 15500.0,
                 scenario: Optional[Scenario] = None,
                 storage_bridge: Optional[StorageBridge] = None):
        if mode not in ("stepped", "realtime", "accelerated"):
            raise ValueError(f"mode sconosciuto: {mode!r} (attesi: "
                             f"stepped, realtime, accelerated)")
        if factor <= 0:
            raise ValueError(f"factor deve essere > 0 (ricevuto {factor!r})")
        self.mode = mode
        self.factor = float(factor)
        # ADR-0013: il seed dello scenario sovrascrive SEMPRE quello del run
        # (stessa semantica di run.py: cfg = SimConfig.build(seed=scenario.seed)
        # quando scenario.seed non è null) — anche se un cfg esplicito è
        # passato, lo scenario vince sul seed degli stream RNG (FIX-2,
        # review-A M6-A7).
        if scenario is not None and scenario.seed is not None:
            self._cfg = SimConfig.build(seed=scenario.seed)
        else:
            self._cfg = cfg if cfg is not None else SimConfig.build(seed=seed)
        # ADR-0013: stream RNG separati, come run.build_sim
        seeds = SeedSequence(self._cfg.seed).spawn(2)
        self._clock = SimulationClock()
        self._plant = Plant(self._cfg, self._clock,
                            default_rng(seeds[0]))
        self.snapshot = TagSnapshot(exposed_valves=exposed_valves,
                                    speed_target_cph=speed_target_cph,
                                    n_valves=len(self._cfg.valves))
        self._plc = PLC(self._cfg, self._clock, self._plant,
                        default_rng(seeds[1]),
                        on_cycle=self._on_cycle, collect_events=False)
        self.commands = MachineCommandLayer(self._plc.machine)
        # True da quando un comando è stato APPLICATO (non no-op): il salto
        # delle fasi vuote è disabilitato (la macchina segue l'override, non
        # più il template giornata) — vedi docstring di modulo e _iterate.
        self._override_active = False
        self._engine: Optional[FaultEngine] = None
        if scenario is not None:
            self._engine = FaultEngine(self._plant, scenario, self._cfg)
        self._cmd_queue: queue.Queue = queue.Queue()
        self._stop_evt = threading.Event()
        self._running = False
        self._stall = 0
        self._last_drain = 0
        # tracciabilità (D4): eventi engine scartati dal drenaggio periodico
        self.engine_events_drained = 0
        # statistiche di pacing dell'ultimo run_until (report performance)
        self.pacing_stats: Optional[dict] = None
        # Bridge storage (OEE L0): default auto da PLCSIM_DATABASE_URL (se
        # l'env non è impostata il bridge nasce disattivato — nessuna
        # connessione nei test del simulatore). Best-effort, mai raise.
        self.storage_bridge = (storage_bridge if storage_bridge is not None
                               else StorageBridge())

    # -- proprietà ---------------------------------------------------------
    @property
    def clock(self) -> SimulationClock:
        return self._clock

    @property
    def plant(self) -> Plant:
        return self._plant

    @property
    def plc(self) -> PLC:
        return self._plc

    @property
    def machine(self):
        return self._plc.machine

    @property
    def engine(self) -> Optional[FaultEngine]:
        return self._engine

    @property
    def cfg(self) -> SimConfig:
        return self._cfg

    @property
    def t_ms(self) -> int:
        return self._clock.now_ms

    @property
    def command_pending(self) -> bool:
        """True se ci sono comandi in coda non ancora applicati (bridge)."""
        return not self._cmd_queue.empty()

    # -- comandi -----------------------------------------------------------
    def submit_command(self, cmd: dict) -> None:
        """Accoda un comando macchina (thread-safe, usata dal bridge server).

        cmd: {"name": "stop"|"start"|"reset"}. Validato subito (fail fast
        sul chiamante); l'applicabilità allo stato corrente è valutata dal
        loop (no-op se non applicabile, comando comunque consumato).
        """
        if not isinstance(cmd, dict) or "name" not in cmd:
            raise ValueError(
                "comando non valido: atteso dict {'name': 'stop'|'start'|'reset'}")
        name = cmd["name"]
        if name not in COMMAND_NAMES:
            raise ValueError(f"comando sconosciuto: {name!r} (attesi: "
                             f"stop, start, reset)")
        self._cmd_queue.put(dict(cmd))

    def stop(self) -> None:
        """Ferma un run_until in corso (thread-safe)."""
        self._stop_evt.set()

    def _take_command(self) -> Optional[dict]:
        try:
            return self._cmd_queue.get_nowait()
        except queue.Empty:
            return None

    def _apply_command(self, cmd: dict, t_ms: int) -> bool:
        """Applica il comando (layer D2) e attiva l'override se applicato.

        Un comando applicato sospende il template giornata per sempre
        (override attivo): da quel momento il loop procede scan-by-scan
        anche nelle fasi non-Running, così la macchina resta nello stato
        finale del comando (es. Stopped) a pacing reale.
        """
        applied = self.commands.apply(cmd, t_ms)
        if applied:
            self._override_active = True
        return applied

    # -- engine (D4) -------------------------------------------------------
    def _on_cycle(self, rec: CycleRecord) -> None:
        """Chiamato dal PLC a ogni chiusura di ciclo.

        Stessa chiamata di telemetry: snapshot (tag) + engine (iniezione
        YAML a start_cycle>1). Il record non va a telemetria (M6 non la
        usa): il risultato di engine.on_cycle (riga GT) è scartato. Il
        bridge storage persiste il contatore bottiglie (best-effort).
        """
        self.snapshot.on_cycle(rec)
        if self._engine is not None:
            self._engine.on_cycle(rec)
        if self.storage_bridge is not None:
            self.storage_bridge.on_cycle(
                self.snapshot.machine_tags["BottleCounter"])

    def _drain_engine(self) -> None:
        """Drena gli eventi engine (scarto) con contatore di tracciabilità."""
        if self._engine is None:
            return
        ev = self._engine.take_events()
        self.engine_events_drained += len(ev)

    # -- loop --------------------------------------------------------------
    def _iterate(self, end_ms) -> int:
        """Una iterazione del loop (semantica _run_loop, ADR-0005).

        Ritorna i ms sim avanzati. Scan singolo quando: (a) la macchina è
        in Running; (b) un comando è stato appena applicato (transizione
        osservabile, eccezione D1); (c) l'override è attivo (un comando è
        già stato applicato in passato: la macchina segue l'override, non
        il template giornata, quindi niente salto delle fasi vuote). Lo
        skip alla transizione successiva resta solo sul template giornata
        (ADR-0005: ottimizzazione senza cambio di semantica). end_ms è
        l'orizzonte per il clamp del salto: float('inf') per advance
        (salto illimitato), valore finito per run_until(end_ms),
        None per run_until senza termine.
        """
        scan = self._cfg.scan_ms
        machine = self.plc.machine
        self.snapshot.begin_scan()          # DataReady=FALSE (pulse 1 scan)
        prev_t = self._clock.now_ms
        t = prev_t
        cmd = self._take_command()
        applied = cmd is not None
        if applied:
            self._apply_command(cmd, t)
        if machine.running or applied or self._override_active:
            t += scan
            self._clock.jump_to(t)
            self._plant.step(t)
            self._plc.scan(t)
        else:
            # fase macchina vuota (template giornata): salto alla
            # transizione successiva (ADR-0005). end_ms=None (run_until
            # senza orizzonte) = nessun clamp sul salto.
            jump_t = machine.entered_ms + machine.duration_ms
            if end_ms is not None:
                jump_t = min(jump_t, end_ms)
            t = jump_t
            self._clock.jump_to(t)
            self._plc.scan(t)
        if t == prev_t:  # guardia anti-stallo (ADR-0005)
            self._stall += 1
            if self._stall > 2:
                raise RuntimeError(
                    f"stallo del clock a t={t} ms, stato={machine.status}")
        else:
            self._stall = 0
        self._update_machine_tags()         # fine iterazione
        if self._engine is not None and t - self._last_drain >= 1000:
            self._drain_engine()
            self._last_drain = t
        return t - prev_t

    def _update_machine_tags(self) -> None:
        """Tag macchina dello snapshot dal MachineController (fine iterazione).

        SpeedActual con fallback .get(status, 0.0): la mappa di config
        (congelata) non contiene "Resetting" (D2).

        Hook storage (OEE L0): il bridge osserva lo stato a ogni scan e
        logga su machine_state_history SOLO le transizioni (idempotente,
        best-effort, mai raise).
        """
        machine = self.plc.machine
        status = machine.status
        self.snapshot.update_machine(
            running=machine.running,
            state_code=OMAC_CODES[status],
            speed_actual=float(
                self._cfg.machine.speed_by_status.get(status, 0.0)),
        )
        if self.storage_bridge is not None:
            self.storage_bridge.on_state(OMAC_CODES[status], status)

    # -- API ---------------------------------------------------------------
    def advance(self, n_scans: int = 1) -> tuple:
        """Modalità stepped: esegue n iterazioni di loop (niente thread).

        Il salto delle fasi vuote è mantenuto (orizzonte illimitato):
        stesso seed + stessa sequenza advance + stessi comandi ⇒ stessa
        evoluzione di stato (determinismo stepped). Ritorna
        (t_ms, machine_status).
        """
        if self._running:
            raise RuntimeError(
                "advance() non può girare durante run_until()")
        if n_scans < 0:
            raise ValueError(f"n_scans deve essere >= 0 (ricevuto {n_scans!r})")
        for _ in range(int(n_scans)):
            self._iterate(end_ms=float("inf"))
        if self._engine is not None:
            self._drain_engine()
        return self._clock.now_ms, self.plc.machine.status

    def run_until(self, end_ms: Optional[int] = None) -> dict:
        """Modalità realtime/accelerated: pacing wall-clock.

        Sleep per scan = scan_ms/factor reali (factor=1 → 10 ms reali per
        10 ms sim; accelerated F → 10/F ms). Se il calcolo sfora (macchina
        lenta) non dorme: best effort, drift misurato. end_ms=None → gira
        finché stop(). Aggiorna pacing_stats:
        {n_scans, n_skips, sim_ms, skipped_ms, wall_s, drift_s, factor}.
        Interpretazione (report di accettazione soak): sim_ms INCLUDE gli
        skip del template giornata (n_skips/skipped_ms li quantificano),
        drift_s NO — accumula solo sulle iterazioni di scan singolo
        (n_scans). La metrica di drift corretta è drift_s (e derivati
        drift_s/n_scans, wall_s − n_scans·target_per_scan), NON
        sim_ms/1000 − wall_s (dominata dallo skip).
        """
        if self._running:
            raise RuntimeError("run_until già in esecuzione (chiamare stop())")
        self._running = True
        self._stop_evt.clear()
        target_per_scan = self._cfg.scan_ms / self.factor / 1000.0
        t0 = time.perf_counter()
        sim0 = self._clock.now_ms
        stats = {"n_scans": 0, "n_skips": 0, "sim_ms": 0, "skipped_ms": 0,
                 "wall_s": 0.0, "drift_s": 0.0, "factor": float(self.factor)}
        try:
            while not self._stop_evt.is_set():
                if end_ms is not None and self._clock.now_ms >= end_ms:
                    break
                it_start = time.perf_counter()
                advanced = self._iterate(end_ms)
                if advanced == self._cfg.scan_ms:
                    stats["n_scans"] += 1
                    elapsed = time.perf_counter() - it_start
                    sleep_for = target_per_scan - elapsed
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                        elapsed = time.perf_counter() - it_start
                    stats["drift_s"] += elapsed - target_per_scan
                else:
                    # iterazione di skip (fase macchina vuota del template
                    # giornata): niente pacing, solo ms sim saltati
                    stats["n_skips"] += 1
                    stats["skipped_ms"] += advanced
        finally:
            if self._engine is not None:
                self._drain_engine()
            self._running = False
        stats["sim_ms"] = self._clock.now_ms - sim0
        stats["wall_s"] = time.perf_counter() - t0
        self.pacing_stats = stats
        return stats
