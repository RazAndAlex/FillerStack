# Quartetto di flag; fillingok emerge dalla logica (≈100% sano)

Il PLC virtuale produce quattro giudizi separati per ciclo: FillQualityOK (volume entro tolleranza), SequenceOK (sequenza completata), SampleValid (record affidabile per analytics) e DiagnosticStatus (comportamento valvola normale — NORMAL/SUSPECT). `fillingok` (colonna di compatibilità V2) emerge dalla logica: nel V3 sano ≈100% (macchina sana = cicli buoni). L'artefatto del simulatore di riferimento (fillingok TRUE solo 28,5%, finding D6) non viene riprodotto: non ha significato causale e la barra di fedeltà (ADR-0004) non lo include. Divergenza documentata nel report di fedeltà.

## Considered Options
- Quartetto + fillingok campionato a 28,5% — rifiutato: fedele all'artefatto ma senza significato causale.
- Solo FillingOk — rifiutato: si perde il caso 'sano ma SUSPECT', cuore del futuro condition monitoring.

## Consequences
Lo schema cycle records aggiunge le colonne del quartetto accanto a quelle V2; il test di accettazione non confronta il tasso di fillingok col dato reale.
