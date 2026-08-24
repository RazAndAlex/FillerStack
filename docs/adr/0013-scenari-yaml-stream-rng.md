# Scenari YAML dichiarativi + stream RNG per componente

Gli scenari (fault, severità, onset, scope, valvole) sono file YAML dichiarativi, ispirati all'esempio della proposta (scenario_id=42, valve_id=12, fault_type=restriction, severity=0.35, fault_start_cycle=18000). Il master seed genera stream RNG separati per componente (SeedSequence numpy: fisica, sensori, PLC). Testabilità: stesso seed + scenario diverso ⇒ differenza attribuibile solo al fault ('dove è causato'). Scenari programmatici Python rinviabili: i casi standard YAML dimostreranno se servono.

## Considered Options
- Scenari programmatici Python — rinviato: più flessibili ma meno leggibili/dichiarativi.
- YAML + Python entrambi — rinviato: doppia API da mantenere.

## Consequences
Ogni scenario è un file versionabile; l'event log e la ground truth referenziano scenario_id/cycle, rendendo ogni anomalia riconducibile alla sua causa.
