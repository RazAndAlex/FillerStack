# Serbatoio condiviso debole (pressione comune, coupling calibrato)

La pressione di serbatoio è una risorsa condivisa a variazione lenta (oscillazione) con pressione locale per-valvola; portata = nominale × fattore pressione × apertura × restrizione × variabilità. Il coupling condiviso si calibra debole per rispettare la baseline V2 (valvole quasi indipendenti). Scelta per due motivi: (1) l'instabilità di pressione colpisce naturalmente più valvole (guasto di gruppo senza iniezione per-valvola); (2) il driver lento condiviso può riprodurre la firma a due pile del FT reale (P=0,68), che in V3 non può venire da un generatore statistico.

## Considered Options
- Modello statico per-valvola (nessuna risorsa condivisa) — rifiutato: i guasti di gruppo andrebbero iniettati valvola per valvola e il driver lento dovrebbe essere replicato come rumore locale.
- Dinamica completa serbatoio (pressione che evolve col riempimento simultaneo) — rinviata: correlazioni forti, difficile da calibrare, rischia di allontanare la baseline sana dal V2.

## Consequences
La correlazione incrociata tra valvole sane è volutamente debole; la calibrazione deve verificarla (per-valvola means/σ come da ADR-0004, più controllo che la correlazione media resti bassa).
