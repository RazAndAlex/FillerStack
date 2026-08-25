# Caso studio IIoT, una pagina

`trentacinque-valvole.html` e' il caso studio anonimizzato della catena IIoT,
scritto il 2026-08-24 alla chiusura della roadmap.

La pagina e' pubblicata come artefatto privato dell'autore, e l'indirizzo non e'
raggiungibile da fuori. Per aggiornarla si modifica **questo file** e si
ripubblica allo stesso indirizzo: un percorso nuovo crea un artefatto nuovo.

## Cosa e' omesso, e perche'

Questo e' lavoro personale, non un incarico. La linea e il nome della macchina
che hanno ispirato il modello non compaiono. Resta il tipo di impianto, la forma
della catena e le sei decisioni, che sono la parte trasferibile e l'unica che
abbia senso mostrare a un estraneo.

## I numeri e da dove vengono

Nessun numero della pagina e' stimato. In ordine di apparizione:

| Numero | Fonte |
|---|---|
| 567 tag, 7 di macchina piu' 16 per valvola | `edge/tag-mapping.yaml`, intestazione |
| 36,2 milioni di cicli | `.project/STATE.md`, riepilogo del 2026-08-22 |
| 723.000 predizioni | idem |
| 567 test verdi su due corse | si rifa' con `pytest`; i verbali delle due corse restano fuori dal repository |
| 1957 impulsi di scarto, punteggio 1,000, allarme al ciclo 700 | documento di chiusura del percorso live, fuori dal repository |
| 9 guasti su 9, zero falsi positivi | `.project/STATE.md`, taratura K=5/N=150 |
| 11.913 righe su 11.913 chiavi dopo il riavvio | documento di chiusura del percorso live |

Se un numero cambia, si cambia qui e si ripubblica. Un caso studio con un numero
scaduto vale meno di uno senza numeri.
