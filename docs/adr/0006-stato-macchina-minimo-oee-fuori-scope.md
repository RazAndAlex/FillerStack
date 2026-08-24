# Stato macchina minimo nel V3; OEE/consumption fuori scope

Il PLC virtuale simula uno stato macchina minimo (Idle → Starting → Running → Stopping → Stopped) che governa l'attività delle valvole (gating, avvio/stop). La timeline OEE e i consumption non sono generati dal V3 in questa fase: quando serviranno si riusa il generatore V2 già validato. Il V2 produceva 3 dataset da un giorno-tipo stereotipato; nel V3 il cuore causale sono i cicli valvola, e la struttura OEE è ortogonale alla causalità.

## Considered Options
- OMAC completo con OEE/consumi generati dal V3 — rinviato: più lavoro, stesso output già validato nel V2.
- Nessuno stato macchina — rifiutato: senza gating start/stop i cicli valvola non hanno contesto operativo.

## Consequences
Lo schema telemetria del V3 non include OEE/consumption per ora; il mapping stati OMAC completo (8 stati + transizioni) resta documentato nel glossario per la fase IIoT.
