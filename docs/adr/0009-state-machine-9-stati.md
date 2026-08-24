# State machine valvola completa a 9 stati

Ogni valvola esegue la sequenza IDLE → FLUSHING → PRESSURIZING → FILLING → TAIL → VALIDATE_FILL → PAUSE → SNIFT → DEAD_ZONE → IDLE, con SAFE_DEPRESSURIZATION separato per gli errori critici (timeout di sicurezza). Le durate delle fasi sono note dal RE (flussaggio 0,2-0,3 s; pausa 300-500 ms; snift 200-250 ms), quindi il costo è gestibile. La forma compatta (solo fill/tail) è stata scartata: le fasi pre/post-riempimento sono l'aggancio naturale dei guasti (es. pressurizzazione lenta) e la base della fedeltà al ciclo reale.

## Considered Options
- Compatta IDLE→FILLING→TAIL→VALIDATE→IDLE — rifiutata: i guasti delle fasi pre/post non avrebbero dove agganciarsi.
- Completa + CIP adesso — rinviata: modalità operativa intera senza benefici immediati per la validazione.

## Consequences
Ogni fase ha timer e condizioni di uscita propri; la durata delle fasi è configurabile per ricetta.
