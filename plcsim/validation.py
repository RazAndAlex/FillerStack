"""Layer 5 — Validazione del ciclo (ADR-0011).

In VALIDATE_FILL il PLC cristallizza il record di ciclo, mai modificato
retroattivamente. Quartetto di flag separati:

  FillQualityOK     : la lattina è entro la tolleranza di volume (±1 g)
  SequenceOK        : la sequenza macchina è stata completata correttamente
  SampleValid       : il record è affidabile per analytics/training
  DiagnosticStatus  : comportamento della valvola (NORMAL / SUSPECT)

fillingok (colonna di compatibilità V2) EMERGE dalla logica (ADR-0011):
nel V3 sano ≈ FillQualityOK, mai l'artefatto del simulatore di
riferimento (28,5%).
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import SimConfig
from .plc import ValveController


@dataclass
class CycleRecord:
    machine_code: str
    ts_beg: int                    # ms virtuali (inizio ciclo)
    fillingtime: int               # FT [ms]: comando apertura -> comando chiusura
    tailtime: int                  # TT [ms]: comando chiusura -> ultimo impulso
    tailpulse: int                 # TP: impulsi dopo il comando di chiusura
    pulsecount: int                # PC: impulsi al comando di chiusura
    target: int
    deltapulse: int                # target - pulsecount
    filling_step_out: int          # slot al completamento (geometria, ADR-0008)
    fillingok: bool
    fill_quality_ok: bool
    sequence_ok: bool
    sample_valid: bool
    diagnostic_status: str
    close_reason: str
    cycle_id: int
    position_limit: bool = False        # D1: chiusura per limite encoder
    filling_overtime: bool = False      # D1: FT > 2000 ms (diagnostica)
    scenario_id: int = 0


def complete_cycle(vc: ValveController, t_ms: int) -> CycleRecord:
    """Cristallizza il record del ciclo appena chiuso (chiamato dal PLC)."""
    cfg: SimConfig = vc.cfg
    recipe = cfg.recipe

    ft = vc.filling_time_ms
    tt = vc.tail_time_ms
    tp = vc.tail_pulses
    pc = vc.pulses_at_close
    delta = recipe.target_pulses - pc
    step = vc.step_out
    # D1: soglia diagnostica (il 2000 ms NON chiude più: è solo flag)
    overtime = ft > recipe.fill_time_limit_ms

    # --- quartetto (ADR-0011, semantica D1) ---
    # D1: encoder_limit => FillQualityOK=FALSE esplicito (PositionLimit);
    # SafetyTimeout => SequenceOK=FALSE; FT > 2000 => FillingOvertime + SUSPECT
    quality_ok = (not vc.position_limit) and abs(delta) <= recipe.tolerance_pulses
    seq_ok = vc.close_reason != "safety_timeout" \
        and tt <= recipe.tail_time_limit_ms
    sample_ok = seq_ok and pc > 0
    diag = "SUSPECT" if (not quality_ok or not seq_ok or overtime) else "NORMAL"

    rec = CycleRecord(
        machine_code=vc.plant.cfg.valves[vc.valve_index].machine_code,
        ts_beg=vc.cycle_start_ms,
        fillingtime=ft,
        tailtime=tt,
        tailpulse=tp,
        pulsecount=pc,
        target=recipe.target_pulses,
        deltapulse=delta,
        filling_step_out=step,
        fillingok=quality_ok,            # emerge dalla logica (ADR-0011)
        fill_quality_ok=quality_ok,
        sequence_ok=seq_ok,
        sample_valid=sample_ok,
        diagnostic_status=diag,
        close_reason=vc.close_reason or "target",
        cycle_id=vc.cycle_id,
        position_limit=vc.position_limit,
        filling_overtime=overtime,
    )
    return rec
