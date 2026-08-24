# SimulationClock: passo fisso 1 ms, scan PLC a cadenza fissa

Il SimulationClock è un event-loop a passo fisso configurabile (default 1 ms); il PLC virtuale esegue il suo scan a cadenza fissa (default 10 ms = 1 scan ogni 10 passi). Seed deterministico di default; la modalità bulk è la stessa logica con clock accelerato, la modalità real-time (pacing sul wall clock) è un'opzione successiva. Scelto contro l'event-driven perché fedele alla semantica del PLC reale (scan ciclico, contatori di impulsi letti a ogni scan) e perché rende deterministici timer e posizione encoder.

## Considered Options
- Event-driven (salto al prossimo evento) — rifiutato: più veloce in bulk ma il PLC non scansiona ciclicamente e la generazione impulsi è più fragile.
- Ibrido (passo fisso + coda eventi) — rifiutato: doppia meccanica temporale senza beneficio funzionale.

## Consequences
Il bulk è più lento; ottimizzazione possibile aumentando il passo nelle fasi vuote (es. IDLE macchina) senza cambiare semantica.
