"""Layer 3 — Sensori virtuali.

Converte grandezze fisiche in segnali osservabili dal PLC:
  - Flowmeter:  1 impulso = 0,1 ml (generazione in Plant, iniezione guasti qui)
  - Encoder:    posizione giostra in conteggi, step della griglia (77 ms)
  - Presenza:   lattina nello slot (in M1: presente quando la macchina è Running)
"""
from __future__ import annotations

import numpy as np

from .clock import SimulationClock
from .config import SimConfig
from .plant import Plant


FLOWMETER_BACKSTOP_MS = 1000   # finestra spuri post-close (piano M4 §6 R3)


class Flowmeter:
    """Flussimetro virtuale: iniezione guasti sul canale impulsi (M4).

    Il plant genera gli impulsi fisici (1 impulso = 0,1 ml) in Plant.step; il
    PLC li legge in PLC.scan. La Flowmeter si inserisce nel mezzo — nel run
    reale la chiama il wrapper installato dal FaultEngine (onda W2) DOPO
    plant.step(t_ms) e PRIMA di plc.scan(t_ms) — e modifica SOLO il canale
    osservabile plant.last_pulses:
      - dropout: azzera gli impulsi dello scan (s = frazione di scan persi)
      - glitch:  aggiunge 1 impulso spurio (s = tasso di scan spuri)
    La fisica (volume_carry) non è mai toccata: il mismatch volume/impulsi
    è la firma del guasto. Lo stato meccanico del plant (_mask_closed,
    _close_start, _open_start) è letto ma mai scritto.

    CONTRATTO W2 — maschere via setter: le maschere si cambiano SOLO con
    set_dropout(v, s) / set_glitch(v, s) (s > 0 attiva, s == 0 disattiva). I
    setter mantengono il flag `_active` (guardia a costo zero) e la lista
    `_active_valves` (ordinata per indice) che pilotano apply(). Una
    scrittura diretta sugli array non aggiorna la guardia: il percorso sano
    non disegna nulla finché nessun setter alza una maschera (fondamento
    bit-identità AC-M4-5). Il numero di draw per scan = len(_active_valves)
    (uno per valvola attiva, in ordine di lista) -> deterministico per
    (seed, scenario) anche con piu' valvole attive (n piccolo).

    Stream RNG (ADR-0013, piano M4 A-M2-3): `default_rng(SeedSequence(
    cfg.seed).spawn(3)[2])` su un SeedSequence FRESCO — per costruzione
    spawn(3)[:2] ≡ spawn(2), quindi la baseline sana non cambia (vedi
    test_stream_separation, tests/test_scenario.py).

    Record emesso: simula il SilenceTimer del PLC lato plant (senza toccare
    plc.py). Il flag `_record_emitted` si alza al primo scan post-close senza
    impulsi con gap >= cfg.silence_ms (fine coda = CycleClosed) e si azzera
    quando la valvola torna pre-close (_close_start == -1, begin_open/abort).
    Con il flag attivo gli spuri tornano liberi: sono late pulses, invisibili
    al PLC fuori da FILLING/TAIL. Il backstop limita gli spuri post-close a
    FLOWMETER_BACKSTOP_MS: protegge la coda dal livelock (il SilenceTimer
    non viene spazzato all'infinito); nel sano non è mai raggiunto (TT max
    reale 422 ms).

    NOTA best-effort: quando nessuna maschera è attiva la guardia esce senza
    aggiornare lo stato interno (zero draw, zero modifiche). Se un guasto si
    riattiva a ciclo già avviato, _prev_pulse può essere stantio (gap
    sovrastimato) e _cycle_cnt può derivare di ±k dopo un abort — l'onda W2
    impone le maschere ai confini di ciclo, dove lo stato è coerente (il
    ciclo_id resta un identificativo best-effort). Un flag _record_emitted
    rimasto attivo a fine guasto si auto-azzera al primo scan pre-close del
    ciclo riattivato.
    """

    def __init__(self, cfg: SimConfig, plant: Plant):
        self.cfg = cfg
        self.plant = plant
        n = len(cfg.valves)
        # maschere di severità per-valvola (0 = valvola sana; s ∈ (0, 1])
        self.dropout = np.zeros(n, dtype=np.float64)
        self.glitch = np.zeros(n, dtype=np.float64)
        # stream sensori: chiave 2 di spawn(3) su SeedSequence FRESCO
        self._stream = np.random.default_rng(
            np.random.SeedSequence(cfg.seed).spawn(3)[2])
        # stato interno (aggiornato solo per le valvole attive)
        self._prev_pulse = np.zeros(n, dtype=np.int64)
        self._record_emitted = np.zeros(n, dtype=bool)
        self._was_open = np.zeros(n, dtype=bool)
        self._cycle_cnt = np.zeros(n, dtype=np.int64)   # FILLING-entry (best-effort)
        self.late_count = np.zeros(n, dtype=np.int64)
        # guardia a costo zero + lista valvole attive (mantenute dai setter)
        self._active = False
        self._active_valves: list[int] = []

    def set_dropout(self, valve: int, s: float) -> None:
        """Attiva (s > 0) o disattiva (s == 0) la maschera dropout (W2)."""
        self.dropout[valve] = s
        self._refresh_active()

    def set_glitch(self, valve: int, s: float) -> None:
        """Attiva (s > 0) o disattiva (s == 0) la maschera glitch (W2)."""
        self.glitch[valve] = s
        self._refresh_active()

    def _refresh_active(self) -> None:
        """Ricalcola la lista delle valvole attive (ordinata per indice)."""
        self._active_valves = [v for v in range(len(self.dropout))
                               if self.dropout[v] > 0 or self.glitch[v] > 0]
        self._active = bool(self._active_valves)

    def apply(self, t_ms: int) -> list:
        """Per-scan: iniezione dropout/glitch sul canale impulsi (M4).

        Chiamata dal wrapper dell'engine DOPO plant.step(t_ms) e PRIMA di
        plc.scan(t_ms). Ritorna gli eventi late dello scan:
        [(t_ms, machine_code, "LATE_PULSE", nota, cycle_id)].
        """
        if not self._active:
            return []          # percorso sano: zero draw, zero modifiche
        plant = self.plant
        cfg = self.cfg
        events = []
        for v in self._active_valves:
            # 1 draw per valvola attiva per scan (ordine di lista stabile ->
            # numero di draw deterministico per (seed, scenario))
            r = self._stream.random()
            mask_closed = plant._mask_closed[v]
            post_close = plant._close_start[v] >= 0
            # 3) DROPOUT: azzera gli impulsi dello scan (s = frazione scan persi)
            if self.dropout[v] > 0 and r < self.dropout[v] and not mask_closed:
                plant.last_pulses[v] = 0
            # 4) GLITCH: +1 impulso spurio (s = tasso scan spuri). Gate:
            #    spuri liberi pre-close; post-close solo nella finestra
            #    backstop (protegge TAIL dal livelock del SilenceTimer) o
            #    dopo il record emesso (late pulses, invisibili al PLC fuori
            #    FILLING/TAIL).
            if self.glitch[v] > 0 and r < self.glitch[v] and not mask_closed:
                allowed = (not post_close
                           or t_ms - plant._close_start[v]
                              < FLOWMETER_BACKSTOP_MS
                           or self._record_emitted[v])
                if allowed:
                    plant.last_pulses[v] += 1
            # 5) RECORD EMESSO: simula il SilenceTimer del PLC lato plant — la
            #    fine coda (CycleClosed) è il primo scan post-close senza
            #    impulsi con gap >= cfg.silence_ms. Reset: valvola pre-close
            #    (_close_start == -1, begin_open/abort). Il gate del glitch
            #    sopra usa il valore PRE-scan del flag.
            had = plant.last_pulses[v] > 0
            gap = t_ms - self._prev_pulse[v]
            if post_close:
                if not had and gap >= cfg.silence_ms:
                    self._record_emitted[v] = True
            else:
                self._record_emitted[v] = False
            # 6) LATE PULSES: impulsi post-close oltre la finestra di coda.
            #    is_late = record emesso | (gap > silence, strict >): un
            #    impulso a gap ESATTAMENTE silence_ms è ancora un impulso di
            #    coda per il PLC (il record viene emesso solo allo scan senza
            #    impulsi) — NON late. prev_pulse == 0 esclude gli scan senza
            #    storico (best-effort).
            if had and post_close:
                is_late = (self._record_emitted[v]
                           or (self._prev_pulse[v] > 0
                               and gap > cfg.silence_ms))
                if is_late:
                    k = int(plant.last_pulses[v])
                    self.late_count[v] += k
                    events.append((t_ms, cfg.valves[v].machine_code,
                                   "LATE_PULSE", f"n_pulses={k}",
                                   int(self._cycle_cnt[v])))
            # 7) tracking ciclo: prev_pulse + contatore FILLING-entry (fine
            #    apply; il gap del prossimo scan usa il valore aggiornato)
            if had:
                self._prev_pulse[v] = t_ms
            opened = plant._open_start[v] >= 0
            if opened:
                if not self._was_open[v]:
                    self._cycle_cnt[v] += 1
                    self._was_open[v] = True
            else:
                self._was_open[v] = False
        return events


class Encoder:
    """Posizione della giostra e griglia step (vincolo geometrico, ADR-0008)."""

    def __init__(self, cfg: SimConfig, clock: SimulationClock):
        self.cfg = cfg
        self.clock = clock
        self.cpr = cfg.plant.encoder_counts_per_rot

    @property
    def position(self) -> int:
        """Conteggi encoder nella rotazione corrente (0..cpr-1)."""
        rot = int(round(self.cfg.recipe.rotation_ms))
        return (self.clock.now_ms % rot) * self.cpr // rot

    def step_from(self, start_ms: int) -> int:
        """Numero di step della griglia trascorsi da start_ms (77 ms/step)."""
        return (self.clock.now_ms - start_ms) // int(self.cfg.recipe.step_ms)

    def step_at(self, t_ms: int, start_ms: int) -> int:
        return (t_ms - start_ms) // int(self.cfg.recipe.step_ms)


class Presence:
    """Presenza lattina nello slot. M1: True quando la macchina è Running."""

    def __init__(self, cfg: SimConfig):
        self.cfg = cfg

    def can_present(self, running: bool) -> bool:
        return running
