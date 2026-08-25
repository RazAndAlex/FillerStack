"""Configurazione e calibrazione — parametri fisici del V3.

I parametri V2 (`plcsim/valve_params.csv`) definiscono la baseline statistica
sana (ADR-0004). Il file e' dato del pacchetto, non un risultato di lavorazione:
sta accanto al codice che lo legge (ADR-0023). Da essi si derivano le
costanti fisiche per-valvola del V3 (problema inverso, ADR-0004):

  flow_base  : portata nominale [ml/s]            <- ft_mean, pc_mean
  tau_close  : durata rampa di chiusura [s]       <- tt_mean
  k_ramp     : fattore di flusso durante la rampa <- tp_mean

Modello di coda (interpretazione di calibrazione, documentata in docs/):
  - PC (pulsecount) = impulsi al comando di chiusura = target + overshoot di
    scan (~6,5 impulsi medi con scan 10 ms: quadratura, NON artefatto).
  - TP (tailpulse)  = impulsi contati durante la rampa di chiusura, flusso che
    NON raggiunge la lattina (linea/ricircolo): q(t) = q0·k·(1 - t/tau).
  - TT (tailtime)   = tempo da comando di chiusura all'ultimo impulso =
    S·(1 - c/q0) con S = tau_close·jitter + snap (GATE-DECISIONS D2: lo snap
    di fine corsa estende la rampa con flusso reale contato, E[snap] =
    settle_jitter·√(2/π), compensato in tau_close).
  - La soglia c = flusso minimo rilevabile dal flussimetro (cutoff).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARAMS = Path(__file__).resolve().parent / "valve_params.csv"


# --------------------------------------------------------------------------
# Costanti globali (da RE / spec V2 / primer)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Recipe:
    """Ricetta 250 ml (unico formato in giugno)."""

    target_pulses: int = 2500          # 1 impulso = 0,1 ml
    fill_time_limit_ms: int = 2000     # filling_time_limit reale
    tolerance_pulses: int = 10         # ±1 g = ±10 impulsi (primer)
    tail_time_limit_ms: int = 600      # soglia TT dashboard (RE)
    step_count: int = 26               # slot della camma (time-grid 77 ms)
    step_ms: float = 77.0              # ms per slot (2000/26, RE)
    active_valves: int = 26
    rotation_ms: float = 3200.0        # cadenza eventi reale (REPORT-DATI)


@dataclass(frozen=True)
class MachineTemplate:
    """Stato macchina minimo (ADR-0006): giorno-tipo da V2."""

    # stato -> (durata ore, ordine)
    day_hours: tuple = (
        ("Idle", 7.82),
        ("Starting", 0.15),
        ("Running", 15.35),
        ("Stopping", 0.13),
        ("Stopped", 0.17),
    )  # somma 23,62 h; il residuo (~0,38 h) va in Idle
    speed_by_status: dict = field(default_factory=lambda: {
        "Running": 15110, "Starting": 5015, "Stopping": 3031,
        "Stopped": 1, "Idle": 2,
    })
    speed_noise: float = 150.0         # σ sul Running (V2)


@dataclass(frozen=True)
class PlantConstants:
    """Costanti fisiche globali (calibrate con lo script di taratura locale).

    driver_amp e flow_noise sono valori BASE: in SimConfig.build() vengono
    materializzati come array per-valvola (35) applicando le mappe
    valve_driver_scale / valve_noise_scale. plant.py non cambia: le sue
    operazioni vettoriali (sh *= shape_gain, z *= flow_noise) broadcastano
    già sugli array.
    """

    tau_open_ms: float = 180.0         # ritardo elettrovalvola+pneumatica (primer 150-200)
    flow_cutoff_mls: float = 10.0      # c: minimo rilevabile dal flussimetro
    settle_jitter_ms: float = 55.0     # σ snap di fine corsa (D2) — calibrato (σ_TT)
    ramp_jitter: float = 0.095         # σ relativa della rampa — calibrato (σ_TT/σ_TP)
    driver_amp: float = 0.060          # ampiezza base driver condiviso (→ σ_FT ~71;
    # 0.0628 dava 74 ma supera la tolleranza ±5% del test di integrazione
    # fisica (p_local di valvola aperta nella finestra del test) — vedi
    # script di taratura; 70.8 resta entro ±10% del target per tutte le sane)
    driver_period_rot: float = 46.0    # periodo reale (FFT, spec V2)
    driver_shape: float = 0.6          # nonlinearità (appiattimento picchi)
    flow_noise: float = 0.032          # σ rumore di portata base
    encoder_counts_per_rot: int = 32000
    grid_phase_ms: float = 0.0         # fase globale griglia step (calibrazione)

    # Profilo anomalo valve8/20 (GATE-DECISIONS D1, V3-DESIGN §16): fattori
    # per-valvola sulla variabilità di portata, per σ_FT ~110 vs ~74.
    # valve_noise_scale: rumore di portata per-valvola (flow_noise·s). Il
    # rumore bianco per-scan si media sul riempimento (~190 scan → contributo
    # ~1% della σ_FT), quindi il fattore sull'ampiezza del driver lento
    # condiviso (ADR-0010) è il canale causale che muove davvero la σ_FT
    # per-valvola: amp_v = driver_amp·valve_driver_scale.get(v, 1.0).
    valve_noise_scale: dict[int, float] = field(default_factory=lambda: {
        8: 10.0, 20: 10.0,          # rumore di portata effettivo 0.32 (calibrato)
    })
    valve_driver_scale: dict[int, float] = field(default_factory=lambda: {
        8: 1.35, 20: 1.35,          # ampiezza driver per-valvola (calibrato)
    })

    # Correzioni empiriche per-valvola delle medie TT/TP (script di taratura,
    # fase 5): residui di quantizzazione del flussimetro (0,1 ml) e della
    # griglia di scan (10 ms) sulle code lunghe. tau_close_corr_ms in ms
    # (sommati a tau_close), k_ramp_corr moltiplicativo. Deterministiche:
    # stesso seed -> stessi output (verificato in pytest).
    tau_close_corr_ms: dict[str, float] = field(default_factory=lambda: {
        # residui di quantizzazione (script di taratura, fase 5)
        "valve0": 0.19, "valve1": -0.18, "valve2": -0.16, "valve3": -0.14,
        "valve4": -1.02, "valve5": 2.67, "valve6": -0.55, "valve7": 0.44,
        "valve8": -2.56, "valve9": -2.78, "valve10": -1.97, "valve11": 0.67,
        "valve12": 3.89, "valve13": 0.56, "valve14": -1.07, "valve15": -1.78,
        "valve16": -0.62, "valve17": 1.84, "valve18": 3.37, "valve19": 0.66,
        "valve20": -3.5, "valve21": 0.82, "valve22": -1.93, "valve23": 0.03,
        "valve24": 8.5, "valve25": -1.97, "valve26": -0.9, "valve27": -0.04,
        "valve28": 0.09, "valve29": -0.75, "valve30": 0.87, "valve31": 2.44,
        "valve32": -0.34, "valve33": -0.74, "valve34": 0.61,
    })
    k_ramp_corr: dict[str, float] = field(default_factory=lambda: {
        "valve0": 1.0003, "valve1": 1.0013, "valve2": 1.0, "valve3": 1.0043,
        "valve4": 1.0022, "valve5": 0.9919, "valve6": 0.9996, "valve7": 1.0001,
        "valve8": 1.0058, "valve9": 1.0045, "valve10": 0.999, "valve11": 0.9998,
        "valve12": 0.9887, "valve13": 1.0008, "valve14": 1.0006, "valve15": 1.0056,
        "valve16": 0.999, "valve17": 1.0012, "valve18": 0.9944, "valve19": 0.9993,
        "valve20": 1.0076, "valve21": 0.9993, "valve22": 1.0051, "valve23": 0.9994,
        "valve24": 0.9818, "valve25": 1.0016, "valve26": 1.0012, "valve27": 0.9987,
        "valve28": 1.0019, "valve29": 0.9991, "valve30": 0.9987, "valve31": 0.9977,
        "valve32": 0.9993, "valve33": 1.0023, "valve34": 0.9998,
    })


# --------------------------------------------------------------------------
# Parametri per-valvola
# --------------------------------------------------------------------------
@dataclass
class ValveCalibration:
    machine_code: str
    index: int
    ft_mean: float
    ft_std: float
    tt_mean: float
    tt_std: float
    tp_mean: float
    pc_mean: float
    # derivati fisici
    flow_base_mls: float = 0.0
    tau_close_s: float = 0.0
    k_ramp: float = 1.0
    phase_rad: float = 0.0
    zone_phase_ms: float = 0.0         # fase sub-slot per la griglia step

    @property
    def name(self) -> str:
        return f"valve{self.index}"


def _derive(
    ft_mean: float, tt_mean: float, tp_mean: float, pc_mean: float,
    tau_open_ms: float, cutoff: float, settle_jitter_ms: float,
) -> tuple[float, float, float]:
    """Deriva (flow_base, tau_close, k_ramp) dai target statistici V2.

    flow_base  = volume erogato / (ft_mean - tau_open/2)      [ml/s]
    tau_close  = tt_mean / (1 - cutoff/flow_base) - E[snap]   [s]
    k_ramp     = tp_target / tp_atteso(q0, S, cutoff)         [-]

    GATE-DECISIONS D2: la rampa effettiva è tau_close·jitter + snap,
    E[snap] = settle_jitter·√(2/π). tau_close compensa il valor medio dello
    snap (S = tau_close + E[snap] = tt_mean/(1-x) resta invariato → anche
    k_ramp resta quello pre-snap); la verifica numerica è nello script di taratura.
    """
    volume_ml = pc_mean * 0.1
    flow_base = volume_ml / ((ft_mean - tau_open_ms / 2.0) / 1000.0)
    x = cutoff / flow_base
    e_snap_s = settle_jitter_ms * (2.0 / np.pi) ** 0.5 / 1000.0
    tau_close = (tt_mean / 1000.0) / (1.0 - x) - e_snap_s if x < 0.95 \
        else 0.35 - e_snap_s
    tau_close = max(tau_close, 0.02)
    tp_expected = flow_base * (tau_close + e_snap_s) * (1.0 - x * x) / 2.0 / 0.1
    k_ramp = tp_mean / tp_expected if tp_expected > 0 else 1.0
    return flow_base, tau_close, k_ramp


def _apply_corr(v: "ValveCalibration", plant: PlantConstants) -> None:
    """Applica le correzioni empiriche per-valvola (mappe di config.py)."""
    v.tau_close_s += plant.tau_close_corr_ms.get(v.machine_code, 0.0) / 1000.0
    v.k_ramp *= plant.k_ramp_corr.get(v.machine_code, 1.0)


def load_valve_params(path: Path | str = DEFAULT_PARAMS,
                      plant: PlantConstants = PlantConstants()) -> list[ValveCalibration]:
    """Carica valve_params.csv e deriva le costanti fisiche per-valvola."""
    out: list[ValveCalibration] = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            code = r["machine_code"]
            vi = int(code.replace("valve", ""))
            fb, tc, kr = _derive(
                float(r["ft_mean"]), float(r["tt_mean"]), float(r["tp_mean"]),
                float(r["pc_mean"]), plant.tau_open_ms, plant.flow_cutoff_mls,
                plant.settle_jitter_ms,
            )
            # fase del driver condiviso per-valvola (fasi reali dal RE, spec V2)
            phase = (vi * 2.39996) % (2.0 * np.pi)
            # fase sub-slot della griglia step (geometria: spaziatura valvole
            # 360/35 mod arco slot 360/26*77/3200... vedi V3-DESIGN §16)
            slot_arc_deg = 360.0 / recipe_step_arc_deg()
            zone_phase = ((vi * (360.0 / 35.0)) % slot_arc_deg) / slot_arc_deg * 77.0
            out.append(ValveCalibration(
                machine_code=code, index=vi,
                ft_mean=float(r["ft_mean"]), ft_std=float(r["ft_std"]),
                tt_mean=float(r["tt_mean"]), tt_std=float(r["tt_std"]),
                tp_mean=float(r["tp_mean"]), pc_mean=float(r["pc_mean"]),
                flow_base_mls=fb, tau_close_s=tc, k_ramp=kr,
                phase_rad=phase, zone_phase_ms=zone_phase,
            ))
    out.sort(key=lambda v: v.index)
    for v in out:
        _apply_corr(v, plant)
    return out


def recipe_step_arc_deg() -> float:
    """Arco di uno step della griglia step (77 ms a 3,2 s/giro)."""
    return 77.0 / 3200.0 * 360.0


# --------------------------------------------------------------------------
# Gruppi valvole (ADR-0007): un PLC ogni sei unità (tesi)
# --------------------------------------------------------------------------
def default_groups(n_valves: int = 35, per_controller: int = 6) -> list[list[int]]:
    groups: list[list[int]] = []
    for g in range(0, n_valves, per_controller):
        groups.append(list(range(g, min(g + per_controller, n_valves))))
    return groups


# --------------------------------------------------------------------------
# Config complessiva
# --------------------------------------------------------------------------
@dataclass
class SimConfig:
    seed: int = 42
    recipe: Recipe = field(default_factory=Recipe)
    machine: MachineTemplate = field(default_factory=MachineTemplate)
    plant: PlantConstants = field(default_factory=PlantConstants)
    scan_ms: int = 10
    silence_ms: int = 150              # finestra "nessun impulso" = fine coda
    fill_safety_margin_ms: int = 500   # SafetyTimeout = fill_time_limit + margine
    pause_ms: int = 250                # PAUSE (primer 300-500 -> 250)
    snift_ms: int = 150                # SNIFT (primer 200-250 -> 150)
    flush_ms: int = 200                # FLUSHING (primer 0,2-0,3 s -> 200)
    pressurize_ms: int = 50            # PRESSURIZING
    valves: list[ValveCalibration] = field(default_factory=list)
    groups: list[list[int]] = field(default_factory=default_groups)

    @classmethod
    def build(cls, params_path: Path | str = DEFAULT_PARAMS,
              seed: int = 42, **overrides) -> "SimConfig":
        cfg = cls(seed=seed)
        base = {**cfg.plant.__dict__, **overrides}
        cfg.plant = _materialize_plant(base, params_path)
        cfg.valves = load_valve_params(params_path, cfg.plant)
        return cfg


def _materialize_plant(base: dict, params_path: Path | str) -> PlantConstants:
    """Materializza i canali per-valvola di plant (calibrazione M1).

    driver_amp e flow_noise diventano array per-valvola (35) applicando le
    mappe valve_driver_scale / valve_noise_scale (profilo anomalo valve8/20,
    GATE-DECISIONS D1). plant.py è invariato: le sue operazioni vettoriali
    (sh *= shape_gain, z *= flow_noise) broadcastano sugli array.
    """
    plant = PlantConstants(**base)
    n = len(load_valve_params(params_path, plant))
    s_map = plant.valve_noise_scale
    a_map = plant.valve_driver_scale
    noise = plant.flow_noise
    amp = plant.driver_amp
    if isinstance(noise, np.ndarray):
        noise_arr = noise
    else:
        noise_arr = np.array([noise * s_map.get(i, 1.0) for i in range(n)])
    if isinstance(amp, np.ndarray):
        amp_arr = amp
    else:
        amp_arr = np.array([amp * a_map.get(i, 1.0) for i in range(n)])
    return PlantConstants(**{**base, "flow_noise": noise_arr, "driver_amp": amp_arr})
