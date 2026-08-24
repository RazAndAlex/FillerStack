# Stack: Python puro (numpy + polars), event-loop a mano

Il simulatore V3 è in Python con numpy + polars e un event-loop di simulazione a passo fisso scritto a mano: nessun framework di simulazione (simpy) e nessuna accelerazione esterna. Scelta per continuità col V2 (riuso di `kpi_params_clean.csv`, `step_params.csv` e dei criteri di fedeltà già validati), ecosistema ML pronto per i layer 7-8, e controllo diretto su determinismo e seed. La pipeline IIoT (OPC UA, Node-RED, MQTT) è infra esterna e linguaggio-agnostica, quindi non vincola.

## Considered Options
- Python + simpy/numba — rifiutato: meno controllo sul determinismo, meno didattico sul loop di simulazione.
- TypeScript/Node — rifiutato: perde continuità col V2 e l'ecosistema ML è più debole.

## Consequences
Le prestazioni bulk dipendono dalla qualità dell'event-loop Python; la granularità del passo è configurabile e ottimizzabile in seguito (fasi vuote a passo più largo).
