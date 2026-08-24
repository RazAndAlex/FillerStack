"""Layer 4 — PLC virtuale (ADR-0009, ADR-0011).

Il PLC NON conosce la ground truth: vede solo impulsi, encoder, timer.
- MachineController: stato macchina minimo (Idle/Starting/Running/Stopping/Stopped)
- ValveController:  state machine a 9 stati + SAFE_DEPRESSURIZATION, 4 timer
- scan a cadenza fissa (default 10 ms)

Timer distinti (proposta §16):
  elapsed  : tempo realmente trascorso nella fase
  process  : timer di controllo (M1: == elapsed; congelabile in futuro)
  safety   : limite assoluto, mai congelato (FILLING)
  silence  : fine coda (nessun impulso per silence_ms)

Chiusura FILLING (GATE-DECISIONS D1, sostituisce "target OR cam OR
time_limit"): target | limite encoder | safety timeout. Il limite a 2000 ms
NON chiude più: è solo soglia diagnostica (filling_overtime + SUSPECT in
validation.complete_cycle).

OTTIMIZZAZIONI PRESTAZIONALI (M1 — target: 1 giorno simulato in <= 3 min):
- PLC.scan() vettorizzato: lo stato caldo delle 35 valvole vive in array numpy
  (stato, contatori di impulsi, timestamp di comando, scadenze `due`).
  Per ogni scan si calcolano con operazioni su array: accumulo impulsi in
  FILLING/TAIL, candidati di chiusura (D1: target OR limite encoder =
  zona geometrica esaurita, derivato da carousel.zone_end), fine coda per
  silenzio e scadenze temporali degli stati a timer.
- Le transizioni (≈1 per scan in media) sono gestite in scalare da
  _transition(): ogni valvola cambia stato al più una volta per scan, come
  nella macchina di riferimento (ValveController.step, mantenuta sotto come
  semantica di riferimento leggibile).
- ValveController resta il contenitore del record di ciclo ed è sincronizzato
  a ogni transizione: validation.complete_cycle() e telemetry.on_events()
  sono invariati.
- Interlock di fermo macchina (proposta §15: "macchina non Running ⇒ nessuna
  valvola in FILLING"): al fronte Running→non-Running i cicli in corso
  (FLUSHING..TAIL) sono abortiti in DEAD_ZONE senza record (lattina scartata,
  nessun ciclo chiuso — come nel dato reale) e la valvola si chiude.
Semantica invariata: scan ogni 10 ms, una transizione per scan per valvola,
draw RNG solo a comando di chiusura (plant), stesso seed => stessi output.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .clock import SimulationClock
from .config import SimConfig
from .plant import Plant


# --------------------------------------------------------------------------
# Stati valvola
# --------------------------------------------------------------------------
IDLE, FLUSHING, PRESSURIZING, FILLING, TAIL, VALIDATE, PAUSE, SNIFT, DEAD_ZONE, SAFE = range(10)
STATE_NAMES = {IDLE: "IDLE", FLUSHING: "FLUSHING", PRESSURIZING: "PRESSURIZING",
               FILLING: "FILLING", TAIL: "TAIL", VALIDATE: "VALIDATE_FILL",
               PAUSE: "PAUSE", SNIFT: "SNIFT", DEAD_ZONE: "DEAD_ZONE",
               SAFE: "SAFE_DEPRESSURIZATION"}

_FAR = np.iinfo(np.int64).max // 2   # "mai": scadenza per stati senza timer


# --------------------------------------------------------------------------
# Macchina (stato minimo, ADR-0006)
# --------------------------------------------------------------------------
class MachineController:
    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self._tpl = list(cfg.machine.day_hours)
        # residuo del giorno (24 h) sul primo stato (Idle)
        tot = sum(h for _, h in self._tpl)
        self._tpl[0] = (self._tpl[0][0], self._tpl[0][1] + (24.0 - tot))
        self._day = 0
        self._idx = 0
        self._entered_ms = 0
        self._dur = self._compute_duration()

    def _compute_duration(self) -> int:
        # round() invece di int(): le ore sono float (es. 8,2h -> 29519999,99...)
        # e il troncamento farebbe atterrare il jump 1 ms prima della
        # transizione -> loop infinito (bug trovato in fase M1).
        return int(round(self._tpl[self._idx][1] * 3600.0 * 1000.0))

    def step(self, t_ms: int) -> None:
        if t_ms - self._entered_ms >= self._dur:
            self._idx = (self._idx + 1) % len(self._tpl)
            if self._idx == 0:
                self._day += 1
            self._entered_ms = t_ms
            self._dur = self._compute_duration()

    @property
    def entered_ms(self) -> int:
        return self._entered_ms

    @property
    def duration_ms(self) -> int:
        # cache: ricalcolata solo alle transizioni (vedi _compute_duration)
        return self._dur

    @property
    def status(self) -> str:
        return self._tpl[self._idx][0]

    @property
    def running(self) -> bool:
        return self.status == "Running"

    @property
    def speed(self) -> int:
        base = self.cfg.machine.speed_by_status[self.status]
        return base


# --------------------------------------------------------------------------
# Valvola (contenitore del ciclo + semantica di riferimento scalare)
# --------------------------------------------------------------------------
@dataclass
class ValveController:
    cfg: SimConfig
    clock: SimulationClock
    plant: Plant
    valve_index: int
    rng: np.random.Generator

    state: int = IDLE
    entered_ms: int = 0
    cycle_id: int = 0
    # tempi del ciclo
    cycle_start_ms: int = 0
    open_cmd_ms: int = 0
    close_cmd_ms: int = 0
    last_pulse_ms: int = 0
    flow_stop_ms: int = 0
    # contatori
    pulses_total: int = 0
    pulses_at_close: int = 0
    tail_pulses: int = 0
    # flag di ciclo (quartetto, ADR-0011)
    fill_quality_ok: bool = True
    sequence_ok: bool = True
    sample_valid: bool = True
    diagnostic_status: str = "NORMAL"
    fault_flags: list = field(default_factory=list)
    close_reason: str = ""
    position_limit: bool = False        # D1: chiusura per limite encoder (zona esaurita)
    filling_overtime: bool = False      # D1: diagnostica (FT > 2000 ms), NON una chiusura
    events: list = field(default_factory=list)   # (ms, nome, payload) per event log

    # -- timer -------------------------------------------------------------
    def elapsed(self, t_ms: int) -> int:
        return t_ms - self.entered_ms

    def fill_elapsed(self, t_ms: int) -> int:
        return t_ms - self.open_cmd_ms

    def silence_elapsed(self, t_ms: int) -> int:
        return t_ms - self.last_pulse_ms

    # -- transizioni ---------------------------------------------------------
    def _set(self, state: int, t_ms: int, note: str = "") -> None:
        self.state = state
        self.entered_ms = t_ms
        self.events.append((t_ms, STATE_NAMES[state], note))

    def _enter_cycle(self, t_ms: int) -> None:
        self.cycle_id += 1
        self.cycle_start_ms = t_ms
        self.pulses_total = 0
        self.pulses_at_close = 0
        self.tail_pulses = 0
        self.fill_quality_ok = True
        self.sequence_ok = True
        self.sample_valid = True
        self.diagnostic_status = "NORMAL"
        self.fault_flags = []
        self.close_reason = ""
        self.position_limit = False
        self.filling_overtime = False
        self._set(FLUSHING, t_ms)

    def _close(self, t_ms: int, reason: str) -> None:
        self.close_cmd_ms = t_ms
        self.pulses_at_close = self.pulses_total
        self.close_reason = reason
        self.position_limit = reason == "encoder_limit"
        self.fill_quality_ok = not self.position_limit   # D1: encoder => qualità FALSE
        self.plant.mech[self.valve_index].begin_close(t_ms, self.rng)
        self.last_pulse_ms = t_ms
        self._set(TAIL, t_ms, reason)

    def _enter_safe(self, t_ms: int) -> None:
        """Errore critico in FILLING: depressurizzazione di sicurezza (ADR-0009).

        Il ciclo è rifiutato (sequence_ok/sample_valid False, record marcato);
        la valvola è depressurizzata (flusso spento)."""
        self.close_cmd_ms = t_ms
        self.pulses_at_close = self.pulses_total
        self.close_reason = "safety_timeout"
        self.sequence_ok = False
        self.sample_valid = False
        self.plant.mech[self.valve_index].abort()
        self.last_pulse_ms = t_ms
        self.flow_stop_ms = t_ms
        self._set(SAFE, t_ms, "safety_timeout")

    # -- scan (semantica di riferimento scalare, allineata a D1) ------------
    def step(self, t_ms: int, machine_running: bool, pulses: int) -> None:
        """Macchina a stati scalare di riferimento (una transizione per scan).

        Il motore usato in produzione è PLC.scan() (vettorizzato); questa
        versione documenta la semantica stato per stato, allineata a
        GATE-DECISIONS D1 (target | encoder_limit | safety_timeout).
        """
        cfg = self.cfg
        carousel = self.plant.carousel
        vi = self.valve_index

        if self.state == IDLE:
            if not machine_running:
                return
            ws = carousel.valve_window_start(t_ms, vi)
            if ws <= t_ms < carousel.zone_end(ws):
                self._enter_cycle(t_ms)

        elif self.state == FLUSHING:
            if self.elapsed(t_ms) >= cfg.flush_ms:
                self._set(PRESSURIZING, t_ms)

        elif self.state == PRESSURIZING:
            if self.elapsed(t_ms) >= cfg.pressurize_ms:
                self.open_cmd_ms = t_ms
                self.plant.mech[self.valve_index].begin_open(t_ms)
                self._set(FILLING, t_ms)

        elif self.state == FILLING:
            if pulses > 0:
                self.pulses_total += pulses
            # D1 (GATE-DECISIONS): target | encoder_limit | safety_timeout.
            # Il 2000 ms non chiude più: è soglia diagnostica (validation).
            enc_ms = self.plant.carousel.fill_window_ms(
                cfg.flush_ms, cfg.pressurize_ms)
            if self.pulses_total >= cfg.recipe.target_pulses:
                self._close(t_ms, "target")
            elif self.fill_elapsed(t_ms) >= enc_ms:
                # zona geometrica esaurita: posizione oltre la camma
                self._close(t_ms, "encoder_limit")
            elif self.fill_elapsed(t_ms) >= cfg.recipe.fill_time_limit_ms \
                    + cfg.fill_safety_margin_ms:
                # SafetyTimeout: backstop assoluto (ADR-0009); in sano non
                # scatta (il limite encoder a ~2127 ms chiude prima)
                self._enter_safe(t_ms)

        elif self.state == TAIL:
            if pulses > 0:
                self.tail_pulses += pulses
                self.last_pulse_ms = t_ms
            elif self.silence_elapsed(t_ms) >= cfg.silence_ms:
                self.flow_stop_ms = self.last_pulse_ms
                self._set(VALIDATE, t_ms)

        elif self.state == VALIDATE:
            pass  # attende che il validation layer cristallizzi il record

        elif self.state == PAUSE:
            if self.elapsed(t_ms) >= cfg.pause_ms:
                self._set(SNIFT, t_ms)

        elif self.state == SNIFT:
            if self.elapsed(t_ms) >= cfg.snift_ms:
                self._set(DEAD_ZONE, t_ms)

        elif self.state == DEAD_ZONE:
            # esce alla finestra successiva a quella del ciclo appena chiuso
            nxt = carousel.next_window_start(self.cycle_start_ms, vi)
            if t_ms >= nxt:
                self._set(IDLE, t_ms)

        elif self.state == SAFE:
            # depressurizzazione completata -> torna alla zona morta
            if self.elapsed(t_ms) >= 500:
                self._set(DEAD_ZONE, t_ms)

    # -- record di ciclo ------------------------------------------------------
    @property
    def filling_time_ms(self) -> int:
        return self.close_cmd_ms - self.open_cmd_ms

    @property
    def tail_time_ms(self) -> int:
        return self.flow_stop_ms - self.close_cmd_ms

    @property
    def step_out(self) -> int:
        return min(self.cfg.recipe.step_count,
                   (self.close_cmd_ms - self.open_cmd_ms)
                   // int(self.cfg.recipe.step_ms))

    def take_events(self) -> list:
        ev, self.events = self.events, []
        return ev


# --------------------------------------------------------------------------
# PLC (motore di scan vettorizzato)
# --------------------------------------------------------------------------
class PLC:
    def __init__(self, cfg: SimConfig, clock: SimulationClock, plant: Plant,
                 rng: np.random.Generator, on_cycle=None,
                 collect_events: bool = False):
        self.cfg = cfg
        self.clock = clock
        self.plant = plant
        self.rng = rng
        self.machine = MachineController(cfg)
        self.valves = [ValveController(cfg, clock, plant, i, rng)
                       for i in range(len(cfg.valves))]
        self.on_cycle = on_cycle          # callback(record) -> telemetria
        self.collect_events = collect_events
        self._prev_running = False
        self.states_seen: set[str] = set()

        n = len(cfg.valves)
        r = cfg.recipe
        # geometria giostra pre-calcolata
        self._rot = int(r.rotation_ms)
        self._zone = int(round(r.active_valves * r.rotation_ms / 35.0))
        self._phase = np.array(
            [int(round(i * r.rotation_ms / 35.0)) for i in range(n)],
            dtype=np.int64)
        # ricetta / timer
        self._target = r.target_pulses
        # D1: limite encoder = zona geometrica esaurita (derivato da
        # carousel.zone_end e dagli offset di fase del ciclo, ~2127 ms)
        self._encoder_limit = plant.carousel.fill_window_ms(
            cfg.flush_ms, cfg.pressurize_ms)
        self._silence = cfg.silence_ms
        self._safety_limit = r.fill_time_limit_ms + cfg.fill_safety_margin_ms
        self._flush = cfg.flush_ms
        self._press = cfg.pressurize_ms
        self._pause = cfg.pause_ms
        self._snift = cfg.snift_ms
        # stato caldo per-valvola (array vettoriali)
        self._state = np.zeros(n, dtype=np.int8)           # IDLE
        self._entered = np.zeros(n, dtype=np.int64)
        self._due = np.zeros(n, dtype=np.int64)            # prossima scadenza
        self._open_cmd = np.zeros(n, dtype=np.int64)
        self._close_cmd = np.zeros(n, dtype=np.int64)
        self._last_pulse = np.zeros(n, dtype=np.int64)
        self._flow_stop = np.zeros(n, dtype=np.int64)
        self._cycle_start = np.zeros(n, dtype=np.int64)
        self._cycle_id = np.zeros(n, dtype=np.int64)
        self._pt = np.zeros(n, dtype=np.int64)             # impulsi del ciclo
        self._pac = np.zeros(n, dtype=np.int64)            # impulsi alla chiusura
        self._tp = np.zeros(n, dtype=np.int64)             # impulsi di coda
        self._close_reason = [""] * n
        self._position_limit = np.zeros(n, dtype=bool)   # D1: chiusura encoder
        # maschere di stato mantenute alle transizioni (evitano confronti per scan)
        self._is_filling = np.zeros(n, dtype=bool)
        self._is_tail = np.zeros(n, dtype=bool)
        # buffer di lavoro per scan()
        self._b1 = np.zeros(n, dtype=bool)
        self._b2 = np.zeros(n, dtype=bool)
        self._b3 = np.zeros(n, dtype=bool)
        self._b4 = np.zeros(n, dtype=bool)
        self._ti = np.zeros(n, dtype=np.int64)
        self._zeros = np.zeros(n, dtype=np.int64)
        # coda eventi valvola (ms, valve_index, nome, nota, cycle_id):
        # drenata da telemetry.on_events; attiva solo con collect_events
        self._events_q: list[tuple] = []

    @property
    def has_events(self) -> bool:
        return bool(self._events_q)

    def take_events(self) -> list:
        """Eventi valvola accumulati dall'ultimo drenaggio (event log)."""
        ev, self._events_q = self._events_q, []
        return ev

    # -- transizioni (scalari, una per scan per valvola) ----------------------
    def _set_state(self, v: int, state: int, t_ms: int, note: str = "") -> None:
        self._state[v] = state
        self._entered[v] = t_ms
        self._is_filling[v] = state == FILLING
        self._is_tail[v] = state == TAIL
        self.states_seen.add(STATE_NAMES[state])
        vc = self.valves[v]
        vc.state = state
        vc.entered_ms = t_ms
        if self.collect_events:
            self._events_q.append(
                (t_ms, v, STATE_NAMES[state], note, int(self._cycle_id[v])))

    def _enter_cycle(self, v: int, t_ms: int) -> None:
        self._cycle_id[v] += 1
        self._cycle_start[v] = t_ms
        self._pt[v] = 0
        self._pac[v] = 0
        self._tp[v] = 0
        self._close_reason[v] = ""
        self._position_limit[v] = False
        vc = self.valves[v]
        vc.cycle_id = int(self._cycle_id[v])
        vc.cycle_start_ms = t_ms
        vc.pulses_total = 0
        vc.pulses_at_close = 0
        vc.tail_pulses = 0
        vc.fill_quality_ok = True
        vc.sequence_ok = True
        vc.sample_valid = True
        vc.diagnostic_status = "NORMAL"
        vc.fault_flags = []
        vc.close_reason = ""
        vc.position_limit = False
        vc.filling_overtime = False
        self._set_state(v, FLUSHING, t_ms)
        self._due[v] = t_ms + self._flush

    def _close(self, v: int, t_ms: int) -> None:
        """Chiusura FILLING (GATE-DECISIONS D1): target (normale) oppure
        limite encoder (zona geometrica esaurita -> PositionLimit TRUE e
        FillQualityOK FALSE). Il SafetyTimeout è gestito prima, in
        _transition -> _enter_safe (backstop assoluto)."""
        if self._pt[v] >= self._target:
            reason = "target"
        else:
            reason = "encoder_limit"
            self._position_limit[v] = True
            self.valves[v].fill_quality_ok = False
        self._close_cmd[v] = t_ms
        self._pac[v] = self._pt[v]
        self._close_reason[v] = reason
        self.plant.mech[v].begin_close(t_ms, self.rng)
        self._last_pulse[v] = t_ms
        self._set_state(v, TAIL, t_ms, reason)
        self._due[v] = _FAR

    def _sync_vc(self, v: int) -> ValveController:
        """Copia lo stato di ciclo dagli array al contenitore scalare
        (usato da validation.complete_cycle e telemetry)."""
        vc = self.valves[v]
        vc.open_cmd_ms = int(self._open_cmd[v])
        vc.close_cmd_ms = int(self._close_cmd[v])
        vc.last_pulse_ms = int(self._last_pulse[v])
        vc.flow_stop_ms = int(self._flow_stop[v])
        vc.pulses_total = int(self._pt[v])
        vc.pulses_at_close = int(self._pac[v])
        vc.tail_pulses = int(self._tp[v])
        vc.close_reason = self._close_reason[v]
        vc.position_limit = bool(self._position_limit[v])
        return vc

    def _finalize(self, v: int, t_ms: int) -> None:
        """VALIDATE_FILL: cristallizza il record e passa a PAUSE."""
        from .validation import complete_cycle   # import pigro (ciclo plc<->validation)
        self._flow_stop[v] = self._last_pulse[v]
        self._set_state(v, VALIDATE, t_ms)
        rec = complete_cycle(self._sync_vc(v), t_ms)
        self._set_state(v, PAUSE, t_ms)
        self._due[v] = t_ms + self._pause
        if self.on_cycle is not None:
            self.on_cycle(rec)

    def _enter_safe(self, v: int, t_ms: int) -> None:
        """Errore critico in FILLING: SAFE_DEPRESSURIZATION (ADR-0009).

        Backstop assoluto: in M1 sano non scatta mai (il limite encoder a
        ~2127 ms chiude prima del SafetyTimeout a 2500 ms); il percorso esiste
        per errori critici e fasi future (rallentamento). Il ciclo è rifiutato:
        il record resta marcato (sequence_ok=False, SUSPECT) e la valvola è
        depressurizzata (flusso spento).
        """
        from .validation import complete_cycle   # import pigro
        self._close_cmd[v] = t_ms
        self._pac[v] = self._pt[v]
        self._close_reason[v] = "safety_timeout"
        self.plant.mech[v].abort()               # depressurizzazione
        self._last_pulse[v] = t_ms
        self._flow_stop[v] = t_ms
        rec = complete_cycle(self._sync_vc(v), t_ms)
        self._set_state(v, SAFE, t_ms, "safety_timeout")
        self._due[v] = t_ms + 500                # depressurizzazione
        if self.on_cycle is not None:
            self.on_cycle(rec)

    def _abort_active(self, t_ms: int) -> None:
        """Fermo macchina: abortisce i cicli in corso (interlock §15).

        Le valvole in FLUSHING..TAIL hanno una lattina in lavorazione: il
        ciclo è scartato (nessun record) e la valvola si chiude. PAUSE/SNIFT
        (record già emesso) completano naturalmente alla ripresa.
        """
        st = self._state
        active = (st == FLUSHING) | (st == PRESSURIZING) | (st == FILLING) \
            | (st == TAIL)
        for v in np.nonzero(active)[0]:
            vi = int(v)
            self.plant.mech[vi].abort()
            ws = (int(self._cycle_start[vi]) // self._rot) * self._rot \
                + int(self._phase[vi])
            self._due[vi] = ws + self._rot
            self._set_state(vi, DEAD_ZONE, t_ms, "abort_stop")

    def _transition(self, v: int, t_ms: int) -> None:
        st = self._state[v]
        if st == IDLE:
            if not self.machine.running:
                return            # riprova al prossimo scan (due già maturata)
            ws = (t_ms // self._rot) * self._rot + int(self._phase[v])
            if ws <= t_ms < ws + self._zone:
                self._enter_cycle(v, t_ms)
            else:
                # prossima occasione: questa finestra o quella successiva
                self._due[v] = ws if t_ms < ws else ws + self._rot
        elif st == FLUSHING:
            self._set_state(v, PRESSURIZING, t_ms)
            self._due[v] = t_ms + self._press
        elif st == PRESSURIZING:
            self._open_cmd[v] = t_ms
            self.plant.mech[v].begin_open(t_ms)
            self._set_state(v, FILLING, t_ms)
            self._due[v] = _FAR
        elif st == FILLING:
            # D1: prima il SafetyTimeout (backstop assoluto), poi _close
            # (target | encoder_limit — con i parametri attuali l'encoder a
            # ~2127 ms scatta sempre prima dei 2500 ms del SafetyTimeout)
            if (t_ms - self._open_cmd[v]) >= self._safety_limit:
                self._enter_safe(v, t_ms)
            else:
                self._close(v, t_ms)
        elif st == TAIL:
            self._finalize(v, t_ms)
        elif st == PAUSE:
            self._set_state(v, SNIFT, t_ms)
            self._due[v] = t_ms + self._snift
        elif st == SNIFT:
            self._set_state(v, DEAD_ZONE, t_ms)
            ws = (int(self._cycle_start[v]) // self._rot) * self._rot \
                + int(self._phase[v])
            self._due[v] = ws + self._rot
        elif st == DEAD_ZONE:
            self._set_state(v, IDLE, t_ms)
            self._due[v] = t_ms   # controllo zona al prossimo scan
        elif st == SAFE:
            self._set_state(v, DEAD_ZONE, t_ms)
            ws = (int(self._cycle_start[v]) // self._rot) * self._rot \
                + int(self._phase[v])
            self._due[v] = ws + self._rot

    # -- scan ciclico (vettorizzato) ------------------------------------------
    def scan(self, t_ms: int) -> None:
        """Scan ciclico: aggiorna macchina e valvole, legge i sensori."""
        self.machine.step(t_ms)
        running = self.machine.running
        if running != self._prev_running:
            if self._prev_running:  # la macchina si ferma: abort + linea svuotata
                self._abort_active(t_ms)
                self.plant.drain_all()
            self._prev_running = running

        pulses = self.plant.last_pulses if running else self._zeros
        pt = self._pt
        b1, b2, b3, b4, ti = self._b1, self._b2, self._b3, self._b4, self._ti

        # accumulo impulsi (FILLING) e coda (TAIL)
        np.add(pt, pulses, out=pt, where=self._is_filling)
        np.greater(pulses, 0, out=b2)                      # impulsi questo scan
        np.logical_and(self._is_tail, b2, out=b3)
        np.add(self._tp, pulses, out=self._tp, where=b3)
        np.copyto(self._last_pulse, t_ms, where=b3)

        # candidati di chiusura FILLING (GATE-DECISIONS D1): target oppure
        # limite encoder (zona geometrica esaurita: fill_elapsed >=
        # zone_ms - flush_ms - pressurize_ms ~ 2127 ms, derivato da
        # carousel.zone_end). Il vecchio time_limit a 2000 ms è solo soglia
        # diagnostica (filling_overtime in validation), NON chiude più.
        np.greater_equal(pt, self._target, out=b1)
        np.subtract(t_ms, self._open_cmd, out=ti)
        np.greater_equal(ti, self._encoder_limit, out=b3)
        b1 |= b3
        b1 &= self._is_filling

        # candidati SAFE_DEPRESSURIZATION: FILLING oltre il SafetyTimeout
        # (backstop assoluto; con il limite encoder a ~2127 ms scatta solo in
        # condizioni forzate/edge, ADR-0009)
        np.greater_equal(ti, self._safety_limit, out=b4)
        b4 &= self._is_filling

        # candidati fine coda: TAIL senza impulsi e silenzio scaduto
        np.subtract(t_ms, self._last_pulse, out=ti)
        np.greater_equal(ti, self._silence, out=b3)
        b3 &= self._is_tail
        np.logical_not(b2, out=b2)
        b3 &= b2

        # candidati a timer (FLUSHING/PRESSURIZING/PAUSE/SNIFT/DEAD_ZONE/IDLE/SAFE)
        np.greater_equal(t_ms, self._due, out=b2)

        b1 |= b3
        b1 |= b4
        b1 |= b2
        for v in np.nonzero(b1)[0]:
            self._transition(int(v), t_ms)
