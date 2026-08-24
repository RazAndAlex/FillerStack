# Giostra: 35 valvole (26 attive) con offset angolari e ValveGroupMap

La giostra è modellata per intero: 35 valvole con offset angolari, 26 attive e 9 in zona morta (sopra la camma). La `ValveGroupMap` è configurabile e raggruppa le valvole per controller (la tesi: un PLC ogni sei unità di riempimento). Il gruppo condiviso è lo scope naturale dei guasti di gruppo e spiega le anomalie a coppie identiche del V2 (valve8≡valve20, valve28≡valve30: stesso profilo di errore = stesso controller).

## Considered Options
- Solo 26 valvole attive — rifiutato: la geometria di step/posizione e lo step-out oltre slot 26 perdono le 9 posizioni morte.
- 35 valvole senza gruppi — rifiutato: i guasti di gruppo e le coppie identiche resterebbero senza spiegazione strutturale.

## Consequences
La posizione encoder è il vincolo geometrico del ciclo (zona utile, zona morta, margine a camma); il numero di valvole per gruppo non è hardcoded.
