# Fedeltà: vincolo statistico ereditato dal V2 (non seed-esatto)

Il V3 sano deve soddisfare la stessa barra di accettazione del V2: medie per-valvola entro ±1%, σ entro ±10%, step_out marginali (incl. zona pericolosa 25-26) e bounds dei KPI rispettati, con accettazione statistica e non seed-esatta (eredita l'ADR-0003 del progetto Comprensione). I parametri V2 (humps per-valvola, step empirici) diventano i valori di calibrazione delle costanti fisiche del V3: se i KPI che *escono dal PLC* hanno la stessa firma statistica del V2, allora fisica+sensori+PLC sono corretti. Quando si inietta un fault, la distanza dalla baseline è quindi misurabile nel linguaggio dei dashboard di riferimento.

## Consequences
Non serve recuperare il seed dell'LCG del simulatore di riferimento; la calibrazione è un problema inverso (costanti fisiche → firma statistica), da automatizzare con un test di accettazione.

**Criterio emendato dall'ADR-0008**: la marginale empirica esatta di `filling_step_out` è sostituita dal pattern della zona pericolosa (valvole normali mai a step 26; valve8/20 ~70% a 26) e dal margine a camma coerente.
