# Step Out emergente dalla geometria (emenda ADR-0004)

Il Filling Step Out non è più campionato dalla distribuzione empirica per-valvola×stato del V2: nel V3 deriva dalla posizione encoder al momento della chiusura (slot della giostra, vincolo geometrico della camma). La zona pericolosa 25-26 emerge dal limite fisico (FT limit 2000 ms ≈ 26 slot × 77 ms), non da una distribuzione iniettata. Criterio di accettazione emendato nell'ADR-0004: il pattern della zona pericolosa (valvole normali mai a step 26, valve8/20 ~70% a 26) e il margine a camma coerenti sostituiscono la marginale empirica esatta per-valvola×stato.

## Considered Options
- Campionamento empirico V2 (`step_params.csv`) — rifiutato: riproduce la marginale ma rompe la causalità (lo step non è più una conseguenza della posizione).
- Marginale esatta per-valvola×stato — rifiutato: contraddice il principio causale.

## Consequences
`step_params.csv` non viene copiato nel nuovo repo (ADR-0003 prevedeva la copia: non serve più). La calibrazione dello step è un problema geometrico (velocità giostra ↔ ms/slot ↔ arco di riempimento).
