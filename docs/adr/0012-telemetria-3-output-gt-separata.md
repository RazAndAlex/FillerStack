# Telemetria: 3 output separati; ground truth mai mescolata

Il V3 emette: (1) cycle records per valvola con schema compatibile col V2 (machine_code, ts_beg, fillingtime, tailtime, tailpulse, pulsecount, target, deltapulse, filling_step_out, fillingok) + nuovi flag (SequenceOK, SampleValid, DiagnosticStatus, LatePulseCount); (2) event log separato (transizioni di stato, comandi, impulsi aggregati per scan) per tracciabilità dei fault; (3) ground truth in file separato (per ciclo: fault_type, severity, valve; timeline fault con onset), mai mescolata alla telemetria. La separazione della ground truth è il confine anti-leakage per il futuro ML (proposta §29).

## Considered Options
- Solo cycle records + ground truth — rifiutato: senza event log i fault non sono tracciabili ('dove è causato' richiede di vedere le transizioni).
- Solo cycle records — rifiutato: senza ground truth non esiste detection time né evaluation.

## Consequences
Tre artefatti distinti in `work/`; gli strumenti di fedeltà V2 si applicano al solo primo output.
