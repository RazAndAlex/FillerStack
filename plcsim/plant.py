"""Layer 2 — Processo fisico simulato (ADR-0010).

Serbatoio condiviso debole: pressione comune a variazione lenta (oscillazione
con nonlinearità) + fase per-valvola; portata moltiplicativa:

    flow = flow_base_v · p_local(t) · open_factor(t) · noise(t)

Il volume viene integrato e consegnato al flowmeter (layer 3) che lo converte
in impulsi. La giostra avanza a velocità costante (rotazione 3,2 s, cadenza
reale); le grandezze fisiche interne appartengono alla ground truth, non al PLC.

OTTIMIZZAZIONI PRESTAZIONALI (M1 — target: 1 giorno simulato in <= 3 min):
- step() vettorizzato con numpy su tutte le 35 valvole: per ogni scan una sola
  np.sin (pressione locale), un solo draw rng.standard_normal(size=35) per il
  rumore di portata, e operazioni su array per fattori di apertura/chiusura,
  portata e quantizzazione degli impulsi — al posto di 35 chiamate Python per
  scan (math.sin + rng.normal + arithmetic per-valvola).
- Buffer di lavoro preallocati: nessuna allocazione nel loop caldo (tranne il
  vettore del rumore, vincolato dall'API dell'RNG).
- Stato meccanico in array numpy (open_start / close_start / ramp_end /
  inv_ramp); ValveMechanics resta come proxy per-valvola con la stessa
  interfaccia usata dal PLC (begin_open / begin_close) più abort() per il
  fermo macchina; open_factor / flow_rate scalari restano per test e debug.
Semantica invariata: integrazione al passo di scan (10 ms), stesso modello
fisico, draw RNG deterministici (1 vettoriale per scan + 2 scalari a ogni
comando di chiusura) => stesso seed => stessi output.

NOTA FISICA (fix di coerenza con la calibrazione documentata): il flussimetro
non vede portata < flow_cutoff_mls (c = minimo rilevabile). Le formule di
calibrazione di tau_close e k_ramp (config.py, work/CALIBRATION-NOTES.md)
assumono proprio questo cutoff; senza, il TT medio simulato risulta ~8% alto.
"""
from __future__ import annotations

import math

import numpy as np

from .clock import SimulationClock
from .config import PlantConstants, SimConfig, ValveCalibration

_CLOSED = -1  # sentinella: valvola chiusa / nessuna rampa in corso


class TankPressure:
    """Pressione di serbatoio condivisa: oscillazione lenta + saturazione.

    p(t) = 1 + amp·shape(sin(ωt + ψ)) con shape = (s + a·s³)/(1+a):
    appiattisce i picchi (dwell ai livelli estremi) senza rompere la causalità.
    Ogni valvola vede il fattore con una fase propria (offset fisico).
    """

    def __init__(self, cfg: SimConfig, rng: np.random.Generator):
        self.cfg = cfg
        self.rng = rng
        c = cfg.plant
        self.omega = 2.0 * math.pi / (c.driver_period_rot * cfg.recipe.rotation_ms)
        self.amp = c.driver_amp
        self.shape_a = c.driver_shape
        self.psi = rng.uniform(0, 2 * math.pi)

    def factor_at(self, t_ms: int, phase_rad: float) -> float:
        """Fattore scalare (interfaccia di comodo per test/debug)."""
        s = math.sin(self.omega * t_ms + phase_rad + self.psi)
        shaped = (s + self.shape_a * s * s * s) / (1.0 + self.shape_a)
        return 1.0 + self.amp * shaped


class ValveMechanics:
    """Proxy per-valvola sullo stato meccanico vettoriale del Plant.

    Dinamica della valvola: rampa di apertura (ritardo tau_open, flusso
    lineare 0→1) e rampa di chiusura (lineare, durata tau_close·jitter + snap
    di fine corsa, flusso scalato da k_ramp — GATE-DECISIONS D2).
    L'interfaccia pubblica (begin_open/begin_close) è invariata.
    """

    __slots__ = ("plant", "i", "cfg", "v", "tau_open_ms", "tau_close_ms",
                 "k_ramp", "settle_ms", "open_delay_ms", "close_delay_ms")

    def __init__(self, plant: "Plant", index: int, cfg: SimConfig,
                 v: ValveCalibration):
        self.plant = plant
        self.i = index
        self.cfg = cfg
        self.v = v
        self.tau_open_ms = cfg.plant.tau_open_ms
        self.tau_close_ms = v.tau_close_s * 1000.0
        self.k_ramp = v.k_ramp
        self.settle_ms = 0.0
        # ritardi di iniezione M2 (ADR-0013, contratto §1.2/§1.3): default
        # 0.0 = percorso sano (+0.0 esatto in IEEE -> bit-identicità M1)
        self.open_delay_ms = 0.0
        self.close_delay_ms = 0.0

    def begin_open(self, t_ms: int) -> None:
        """Comando di apertura: rampa 0->1 in tau_open, onset shiftabile.

        M2 (ADR-0013, contratto §1.3): con open_delay_ms > 0 la rampa parte
        a t + int(open_delay_ms) — troncatura int documentata: deterministico
        anche per delay frazionari (rampa gradual). Le maschere si aprono
        SUBITO: il flusso resta 0 nel tratto di ritardo perché in step() la
        rampa f = (t_ev - _open_start)·_inv_tau_open è clip a 0.
        """
        p = self.plant
        p._open_start[self.i] = t_ms + int(self.open_delay_ms)
        p._close_start[self.i] = _CLOSED
        p._mask_closed[self.i] = False
        p._mask_closing[self.i] = False

    def begin_close(self, t_ms: int, rng: np.random.Generator) -> None:
        """Comando di chiusura: rampa con jitter + snap di fine corsa (D2).

        GATE-DECISIONS D2: la rampa lineare è estesa da uno snap di fine
        corsa, snap_ms = |N(0, settle_jitter_ms)| (σ 33 ms default): il flusso
        continua il decadimento lineare fino a tau_close·jitter + snap_ms.
        La variabilità della dinamica di chiusura diventa variabilità del
        tempo dell'ultimo impulso (σ_TT) — niente rumore additivo su TT nel
        validation layer. Stesso numero di draw RNG di prima (jitter, snap)
        => stesso seed => stessi output.
        """
        p = self.plant
        j = 1.0 + rng.normal(0.0, p.cfg.plant.ramp_jitter)
        snap_ms = abs(rng.normal(0.0, p.cfg.plant.settle_jitter_ms))
        self.settle_ms = snap_ms
        # la durata della rampa vive negli array del Plant (loop vettoriale)
        ramp = max(10.0, self.tau_close_ms * j) + snap_ms
        p._close_start[self.i] = t_ms
        # M2 (ADR-0013, contratto §1.2): close_delay_ms estende la rampa di
        # d ms — ramp INVARIATA (stessi draw jitter/snap, zero draw
        # aggiuntivi); nel tratto [t, t+d] fc è clip a 1 -> flusso pieno·k_ramp
        p._ramp_end[self.i] = t_ms + self.close_delay_ms + ramp
        p._inv_ramp[self.i] = 1.0 / ramp
        p._mask_closing[self.i] = True

    def abort(self) -> None:
        """Fermo macchina: la valvola si chiude subito (ciclo abortito)."""
        p = self.plant
        p._open_start[self.i] = _CLOSED
        p._close_start[self.i] = _CLOSED
        p._mask_closed[self.i] = True
        p._mask_closing[self.i] = False

    # -- lettura scalare (test/debug) ---------------------------------------
    def open_factor(self, t_ms: int) -> float:
        """Fattore di apertura [0,1] (rampa di chiusura NON scalata k_ramp).

        Clip esplicita a [0,1]: con open_delay/close_delay > 0 il fattore
        grezzo resterebbe negativo nel tratto di ritardo (apertura) o > 1
        nel tratto a flusso pieno (chiusura) — il clip è coerente con quello
        vettoriale di step() (np.clip su f/fc). Con delay 0 è no-op nei punti
        d'uso del percorso M1 (rampa: fattore < 1).
        """
        p = self.plant
        i = self.i
        if p._open_start[i] < 0:
            return 0.0
        if p._close_start[i] < 0:
            return min(1.0, max(0.0, (t_ms - p._open_start[i]) / self.tau_open_ms))
        return min(1.0, max(0.0, (p._ramp_end[i] - t_ms) * p._inv_ramp[i]))

    def flow_rate(self, t_ms: int, p_local: float, noise: float,
                  flow_base: float) -> float:
        """Portata istantanea [ml/s] attraverso il flussimetro."""
        f = self.open_factor(t_ms)
        if f <= 0.0:
            return 0.0
        if self.plant._close_start[self.i] >= 0:
            f *= self.k_ramp
        return flow_base * p_local * f * max(0.0, 1.0 + noise)


class Carousel:
    """Giostra: posizione angolare globale e finestra di ciclo per valvola.

    Rotazione costante (3,2 s). Ogni valvola ha un offset angolare; la sua
    finestra attiva (zona utile, 26 slot) inizia ogni rotazione in un istante
    fisso. Il vincolo di camma: il riempimento deve chiudersi entro
    zone_start + fill_limit (26 step × 77 ms).
    """

    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.rot_ms = cfg.recipe.rotation_ms
        self.spacing_ms = self.rot_ms / 35.0          # passo tra valvole
        self.zone_ms = cfg.recipe.active_valves * self.spacing_ms  # ~2377 ms

    def valve_window_start(self, t_ms: int, valve_index: int) -> int:
        """Inizio della finestra attiva della valvola nella rotazione corrente."""
        base = (t_ms // int(self.rot_ms)) * int(self.rot_ms)
        return base + int(round(valve_index * self.spacing_ms))

    def next_window_start(self, t_ms: int, valve_index: int) -> int:
        """Inizio della finestra attiva successiva a quella che contiene t_ms."""
        return self.valve_window_start(t_ms, valve_index) + int(self.rot_ms)

    def zone_end(self, window_start: int) -> int:
        return window_start + int(round(self.zone_ms))

    def cam_limit(self, window_start: int) -> int:
        """Limite geometrico della camma: fill_limit dopo l'ingresso in zona.

        Riserva per la fase di rallentamento (ProcessControlTimer congelabile,
        ADR futura): in M1 a velocità costante il check attivo di geometria è
        il limite encoder di D1 (fill_window_ms, vedi sotto) — la camma marca
        la pericolosità (step 26), non chiude.
        """
        return window_start + self.cfg.recipe.fill_time_limit_ms

    def fill_window_ms(self, flush_ms: int, pressurize_ms: int) -> int:
        """Finestra di riempimento utile (GATE-DECISIONS D1).

        Zona geometrica (zone_end della valvola) meno gli offset di fase del
        ciclo (FLUSHING + PRESSURIZING): è il limite encoder visto da
        fill_start — l'uscita dalla zona, ~2127 ms con i parametri attuali
        (26 slot × 3200/35 − 200 − 50).
        """
        return int(round(self.zone_ms)) - flush_ms - pressurize_ms

    def in_zone(self, t_ms: int, valve_index: int) -> bool:
        ws = self.valve_window_start(t_ms, valve_index)
        return ws <= t_ms < self.zone_end(ws)


class Plant:
    """Stato fisico complessivo: pressione + meccaniche valvola + volume.

    Gli array interni (35 elementi) sono lo stato caldo del loop vettoriale;
    `mech` espone i proxy per-valvola usati dal PLC per i comandi a evento.
    """

    def __init__(self, cfg: SimConfig, clock: SimulationClock,
                 rng: np.random.Generator):
        self.cfg = cfg
        self.clock = clock
        self.rng = rng
        self.tank = TankPressure(cfg, rng)
        self.carousel = Carousel(cfg)

        n = len(cfg.valves)
        self._n = n
        c = cfg.plant
        # parametri per-valvola
        self._flow_base = np.array([v.flow_base_mls for v in cfg.valves])
        self._k_ramp = np.array([v.k_ramp for v in cfg.valves])
        # iniezione fault M2 (ADR-0013, contratto §1.1): fattore di
        # restriction per-valvola, default 1.0 = percorso sano (x1.0 esatto
        # in IEEE -> bit-identicità col percorso M1, base di AC-5/AC-6/AC-9)
        self._restriction = np.ones(n)
        # M3 (pressure_instability): moltiplicatore per-valvola dell'ampiezza
        # del driver lento (ADR-0010); default 1.0 = percorso sano (x1.0
        # esatto in IEEE -> bit-identicità col percorso M2/M1)
        self._amp_mult = np.ones(n)
        phases = np.array([v.phase_rad for v in cfg.valves])
        self._phase_psi = phases + self.tank.psi
        # costanti pre-calcolate
        self._inv_tau_open = 1.0 / c.tau_open_ms
        self._omega = self.tank.omega
        self._shape_a = c.driver_shape
        self._shape_gain = c.driver_amp / (1.0 + c.driver_shape)
        self._dt_s = cfg.scan_ms / 1000.0
        self._half_scan = cfg.scan_ms // 2   # valutazione a punto medio
        # stato meccanico (array caldi)
        self._open_start = np.full(n, _CLOSED, dtype=np.int64)
        self._close_start = np.full(n, _CLOSED, dtype=np.int64)
        self._ramp_end = np.zeros(n, dtype=np.float64)
        self._inv_ramp = np.zeros(n, dtype=np.float64)
        # buffer di lavoro (niente allocazioni nel loop)
        self._f = np.zeros(n, dtype=np.float64)    # fattore di apertura
        self._fc = np.zeros(n, dtype=np.float64)   # fattore in chiusura
        self._s = np.zeros(n, dtype=np.float64)    # arg/seno driver
        self._sh = np.zeros(n, dtype=np.float64)   # pressione locale
        self._m = np.zeros(n, dtype=np.float64)    # portata misurata / scratch
        self._z = np.zeros(n, dtype=np.float64)    # rumore (buffer per out=)
        self._mask = np.zeros(n, dtype=bool)       # cutoff flussimetro
        # maschere di stato meccanico, mantenute dai comandi a evento
        # (begin_open/begin_close/abort): evitano confronti per scan
        self._mask_closed = np.ones(n, dtype=bool)
        self._mask_closing = np.zeros(n, dtype=bool)
        # stato osservabile
        self.volume_carry = np.zeros(n, dtype=np.float64)
        self.flow_now = np.zeros(n, dtype=np.float64)
        self.last_pulses = np.zeros(n, dtype=np.int64)

        self.mech = [ValveMechanics(self, i, cfg, v)
                     for i, v in enumerate(cfg.valves)]

    def step(self, t_ms: int) -> None:
        """Integrazione a ogni scan del PLC (10 ms), vettorizzata sulle valvole.

        La portata è valutata al PUNTO MEDIO dell'intervallo [t-10, t]
        (t_mid = t - 5 ms): la regola del rettangolo a destra sottostima
        sistematicamente la rampa di chiusura (~-q0·k·dt/2 ≈ -7 impulsi su TP
        e ~-5 ms su TT), rompendo la coerenza con le formule di calibrazione
        (continue) di config.py. Col punto medio l'integrale delle rampe
        (lineari) è esatto; la semantica di scan (10 ms) è invariata.
        """
        t_ev = t_ms - self._half_scan
        c = self.cfg.plant
        f = self._f
        # rampa di apertura 0→1 in tau_open (valvole chiuse: mascherato dopo)
        np.subtract(t_ev, self._open_start, out=f, casting="unsafe")
        f *= self._inv_tau_open
        np.clip(f, 0.0, 1.0, out=f)
        np.copyto(f, 0.0, where=self._mask_closed)
        # rampa di chiusura 1→0 scalata da k_ramp
        fc = self._fc
        np.subtract(self._ramp_end, t_ev, out=fc)
        fc *= self._inv_ramp
        np.clip(fc, 0.0, 1.0, out=fc)
        fc *= self._k_ramp
        np.copyto(f, fc, where=self._mask_closing)
        # pressione locale: driver lento condiviso con fase per-valvola
        s = self._s
        np.multiply(t_ev, self._omega, out=s)
        s += self._phase_psi
        np.sin(s, out=s)
        sh = self._sh
        np.multiply(s, s, out=sh)
        sh *= s
        sh *= self._shape_a
        sh += s
        sh *= self._shape_gain
        sh *= self._amp_mult           # M3: amp_v = amp·mult (x1.0 sano = no-op)
        sh += 1.0                      # p_local = 1 + amp·shaped
        # rumore di portata: un unico draw vettoriale per scan (buffer fisso)
        z = self._z
        self.rng.standard_normal(self._n, out=z)
        z *= c.flow_noise
        z += 1.0
        np.clip(z, 0.0, None, out=z)
        # portata fisica [ml/s] (ground truth) e misurata (cutoff flussimetro)
        q = self.flow_now
        np.multiply(self._flow_base, f, out=q)
        q *= sh
        q *= z
        # iniezione M2 (ADR-0013, contratto §1.1): restriction per-valvola —
        # moltiplicatore puro DOPO il rumore e PRIMA del cutoff del
        # flussimetro; nessuna allocazione, nessun draw RNG nel loop caldo
        q *= self._restriction
        np.greater_equal(q, c.flow_cutoff_mls, out=self._mask)
        m = self._m
        np.multiply(q, self._mask, out=m)
        m *= self._dt_s
        self.volume_carry += m
        # impulsi da 0,1 ml maturati dallo scan precedente
        np.divide(self.volume_carry, 0.1, out=m)
        np.trunc(m, out=m)
        np.copyto(self.last_pulses, m, casting="unsafe")
        np.multiply(self.last_pulses, 0.1, out=m)
        self.volume_carry -= m

    def pulses_since_last_scan(self, index: int) -> int:
        """Impulsi flowmeter (0,1 ml) dell'ultimo scan integrato.

        La quantizzazione è ora fatta in step() (vettoriale): questo metodo
        espone solo il conteggio per-valvola (nessuna mutazione di stato).
        """
        return int(self.last_pulses[index])

    def drain_all(self) -> None:
        """Fermo macchina: la linea si svuota (il prodotto non arriva alla
        lattina e il flussimetro smette di contare)."""
        self.volume_carry[:] = 0.0
        self.last_pulses[:] = 0
