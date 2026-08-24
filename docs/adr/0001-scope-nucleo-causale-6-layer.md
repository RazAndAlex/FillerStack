# Scope: nucleo causale a 6 layer (non analytics/ML/IIoT)

Il progetto costruisce il nucleo causale del simulatore V3 — layer 1-6: ground truth/fault engine, processo fisico, sensori virtuali, PLC virtuale, validazione KPI, telemetria. Analytics/ML e pipeline IIoT real-time (OPC UA → Node-RED → MQTT) sono progettati come layer agganciabili ma non implementati in questa fase: un modello addestrato su segnali la cui causalità non è ancora validata non avrebbe valore, e la domanda ML interessante (rilevare il degrado prima dello scarto) nasce solo a causalità verificata.

## Considered Options
- Layer 1-8 (anche ML subito) — rifiutato: rischio di addestrare su segnali non validati.
- Tutto + pipeline IIoT real-time — rifiutato: carico enorme su fondamenta non verificate.

## Consequences
L'architettura (interfacce tra layer, formato telemetria) va pensata fin da subito per agganciare analytics e ML senza ristrutturazioni.
