# Open questions

Updated: 2026-08-20


## Chiuse il 2026-08-20 — leggere qui prima delle sezioni piu' vecchie

- **«Un database, un run»**: scelta la terza via, la cura vera. `cycles` ha ora
  chiave `(run_id, valve_id, cycle_id)` e la migrazione e' applicata al database
  vero. Le sezioni piu' sotto che descrivono il problema restano come storia.
- **«La baseline sana non ha dove stare»**: caricato il run `storico_60d` accanto
  a quello guasto; i KV `baseline_window` e `baseline_cache` esistono e
  `/valves/baseline` non risponde piu' `degraded`.
- **Il tetto di 48 ore sulla serie**: chiuso dal riepilogo orario
  `cycle_rollup_hour`. La serie sui 60 giorni costa 0,6 s.

## Aperte dal 2026-08-20

## Il confronto con la propria base e' cieco su cio' che e' sempre stato storto (2026-08-20)

Misurato sulla finestra sana di `storico_60d` (21 giu - 2 lug), valvola per
valvola: **la 21 e la 9 stanno a qualita' 0,601 e filling time 2.023 ms**, mentre
tutte le altre trentatre' stanno fra 0,769 e 0,826 con filling fra 1.835 e 1.970.

Quelle due valvole sono anomale **fin dal primo giorno della run**, e l'anomalia
e' entrata nella loro stessa base. Confrontate con se' stesse risultano a posto.

Riguarda l'impostazione «ogni valvola contro la propria base», che regge la
pagina VALVOLE e il pannello di dettaglio della pagina TEMPO. Quel confronto
trova bene cio' che **cambia** ed e' cieco su cio' che e' **costante e sbagliato**.

Servono le due letture insieme: lo scostamento dalla propria base, che esiste, e
la posizione rispetto alle altre trentaquattro, che oggi non c'e' da nessuna
parte. La seconda cambia la struttura di una pagina gia' approvata, quindi la
decisione e' dell'utente e non e' stata presa.

Nota: il valore anomalo della 21 e della 9 non e' un difetto dei dati. E'
probabilmente il fenomeno emergente degli slot 25-26 gia' registrato in
ADR-0008, ma il collegamento non e' stato verificato.

## Il quarto guasto e' piccolo e comune, non nascosto in un'altra grandezza (2026-08-20)

**Correzione di una lettura sbagliata fatta in giornata.** Era stato scritto che
la valvola 13 mostra il guasto di pressione sul tail time, 350 ms contro i 302 di
una valvola sana. E' falso: i 350 ms sono il valore normale della 13 dal primo
giorno della run, costante per due mesi. Ogni valvola ha il proprio tail time di
fabbrica - la 16 sta a 273 ms, la 1 a 302, la 13 a 350. Era una differenza **fra
valvole** letta come una differenza **nel tempo**.

Misurato poi nel modo giusto, confrontando la stessa valvola prima e dopo il
ciclo di inizio del guasto (`start_cycle` 656222, gruppo 2, valvole 13-18):
l'instabilita' di pressione vale **+1,7 ms di filling time** e nient'altro. Tail
time, tail pulse e conteggio impulsi non si muovono. Sulla valvola 1, fuori dal
gruppo, la stessa differenza vale 0,0 ms.

Il segnale e' reale ma e' lo **0,09% della grandezza**. Quindi il guasto non e'
invisibile perche' guardiamo la grandezza sbagliata: e' invisibile perche' e'
piccolo e comune a sei valvole. Per vederlo serve un confronto fra popolazioni,
cioe' il gruppo 2 contro le altre. Non e' un pannello di dettaglio ed e' lavoro
a parte.

Questo chiude, con una risposta diversa da quella attesa, il punto aperto «il
quarto guasto non lascia traccia sulla qualita'».


**Il percorso live esiste solo a meta'.** Il ponte OPC UA e' montato su **una
valvola su 35** e non c'e' alcun processo continuo che porti i dati dal disco al
database. L'utente ha approvato la direzione «storico generato + percorso live
sopra»: la seconda meta' non e' mai stata progettata. Quando si accendera',
servira' anche portare `run_id` alle tabelle derivate (previsioni, allarmi,
transizioni), che oggi ne sono prive per decisione presa e registrata.

**La prestazione non porta informazione.** E' costante a 0,997-1,000 in ogni
periodo perche' il simulatore non modella la perdita di velocita'. Non e' un
difetto da correggere in fretta: renderla informativa significa modellare la
perdita di velocita' nel simulatore, che e' lavoro nuovo. Nel frattempo si mostra
com'e' e non si trucca.

**Il quarto guasto non lascia traccia sulla qualita'.** L'instabilita' di
pressione scritta sulle valvole 13-18 in `scenarios/storico_60d.yaml` non muove
la qualita': la valvola 13 resta a 0,80 per due mesi, mentre gli altri tre guasti
si vedono benissimo. Dice che quel guasto va cercato in una grandezza diversa. Non
e' stato approfondito.

**Il modello non ha riconosciuto il ritardo di apertura della valvola 21.** Emerso
nella sessione precedente, mai indagato. Sulla qualita' quel guasto si vede
(0,60 → 0,51): la domanda e' perche' la catena di inferenza no.

**La guardia di pulizia dei database di test ha un buco.** In
`pipeline/tests/conftest.py` il prefisso `plcsim_test_baseline_` non riconosce i
database `plcsim_test_baseline_cache_<hex>`, e ne sono rimasti 14 sul Postgres di
sviluppo. E' una voce in piu' in `_PREFISSI_EFFIMERI`.

## Un database, un run — RISOLTA 2026-08-20

`cycles` ha chiave `(valve_id, cycle_id)` senza data, e ogni run del simulatore
rinumera `cycle_id` per valvola a partire da 1. Caricare un secondo run nello
stesso database farebbe scartare in silenzio (ON CONFLICT DO NOTHING) quasi tutti
i suoi cicli, mescolando due run sotto la stessa identita'. Per questo nel DB c'e'
solo `work/m4_demo_dropout_1d` e `work/m2_healthy_5d` NON e' stato caricato.

Blocca a cascata: niente run sana -> niente baseline vera -> `/valves/baseline`
resta `degraded` (manca anche il KV `baseline_window`) -> il riferimento di
qualita' della pagina OEE non e' quello documentato (una baseline calcolata oggi
darebbe 0,7635 invece di 0,7868, perche' userebbe il run guasto).

Opzione raccomandata: **un database per scenario**, non una modifica dello schema.
Motivo: i run multipli sono un problema della *demo*, non dell'esercizio — una
macchina vera ha un solo flusso di cicli che avanza nel tempo. Cambiare la chiave
di `cycles` pagherebbe un contratto per un problema che in produzione non esiste.

## La carta di controllo giusta per questo segnale — decisa, non ancora costruita

Misurato: i limiti XmR di `/valves/baseline` non valgono sul singolo ciclo
(73-78% dei cicli sani fuori limite) perche' la macchina ha un'oscillazione
deterministica di periodo **46 cicli** (`driver_period_rot`), non perche' il
processo derivi. Il rimedio proposto in origine (`mean +- 3 sigma`) e' stato
**misurato e rifiutato**: non produce falsi positivi ma non discrimina.

La forma corretta e' una carta sulla **media mobile di 46 cicli** (~3,6 min):
comprime l'oscillazione del driver da 71,9 a 1,79 ms, 40x, lasciando visibile
solo lo scostamento vero della valvola.

Decisione dell'utente (2026-08-19): **si costruira' come seconda variante accanto
a quella attuale**, da confrontare sui dati veri, non su una descrizione. Non
implementarla prima di quel confronto. Ha senso solo dopo `HANDOFF-api-vera.md`,
perche' va giudicata su dati reali.

## Le sei fixture vanno rigenerate — tre motivi, non uno

1. Profili di fermata non confrontabili (motivo originale): le run di guasto non
   si fermano mai (disponibilita' 99%), la run sana si' (64%), da cui l'OEE
   ribaltato — gli scenari guasti leggono 0,756 contro 0,504 del sano.
2. **Due fixture si contraddicono**: `machine-oee-day.json` e
   `machine-oee-series.json` danno `performance_detail.theoretical` diverso sullo
   stesso istante e dallo stesso generatore. Ha ragione la seconda.
3. **`alert-history.json` contiene 21 righe fantasma** su 27, generate iterando
   lo stato interno di `AlertEngine` invece degli eventi emessi.

## Il percorso live non e' mai stato esercitato (2026-08-19)

Tutto il popolamento e' stato fatto in replay offline dai run congelati. La catena
`realtime -> OPC UA -> Node-RED -> MQTT -> ingest -> database` non e' mai stata
percorsa da capo a fondo, nemmeno una volta. La logica della catena e' dimostrata,
il percorso dell'edge no. Vale una run breve prima della demo allo stakeholder.

## Tre difetti trovati costruendo la dashboard — CHIUSI (2026-08-18)

1. **OEE fuori scala — CHIUSO.** `Speed_Target = 15500` e' lattine/ora a livello
   macchina (accertato: `plcsim/realtime.py:145`, `speed_by_status["Running"] =
   15110` nelle stesse unita', contratto tag OPC UA). Il difetto era che
   `plcsim/config.py:48` `rotation_ms = 3200` implica 39.375 lattine/ora,
   **2,540 volte** il valore di targa — il punto aperto che `docs/V3-DESIGN.md`
   sez. 188 registrava come "da riconciliare con la cadenza osservata".

   **Decisione dell'utente: NON toccare la geometria del simulatore.** Motivo:
   `rotation_ms` non e' una cadenza di timestamp, e' portante — da esso
   discendono `zone_ms` (2377 ms), il limite encoder che chiude la valvola
   (~2127 ms, che taglia la distribuzione di FT e produce l'1,3% di
   `close_reason = encoder_limit`), la coerenza fra `step_count 26 x step_ms 77`
   e la finestra utile che rende pericolosi gli slot 25-26 (fenomeno emergente,
   ADR-0008), e la frequenza di oscillazione del driver
   (`omega = 2*pi / (46 * rotation_ms)`, periodo da FFT dei dati V2). Cambiarlo
   avrebbe richiesto di ri-derivare il modello fisico, ricalibrare e riaddestrare
   il modello ML, oltre a rompere la bit-identita' bulk.

   **Fix applicato**: persistito il KV `speed_target` = 39.375 cph, DERIVATO
   dalla geometria a ogni esecuzione da `edge/scripts/provision_speed_target.py`
   (nessun secondo numero cablato). L'API ora legge un target verificato
   (`speed_target_source = "kv"`) e l'OEE torna calcolabile.

   **CONSEGUENZA DA SAPERE**: con il target corretto, Performance risulta
   0,999-1,000 in ogni scenario. Non e' un errore: **il simulatore non modella
   la perdita di velocita'** — la cadenza e' fissa, quindi Performance non porta
   informazione e l'OEE si riduce di fatto ad Availability x Quality. Misurato:
   A da 0,0 a 1,0, Q da 0,764 a 0,787 (Q risponde ai guasti), P costante.
   Se serve una Performance informativa va modellata la perdita di velocita' nel
   simulatore: e' lavoro nuovo, non un fix.

   **RESTA APERTO (minore)**: `speed_by_status["Running"] = 15110` alimenta il
   tag `SpeedActual`, che continua a dichiarare una velocita' 2,5 volte inferiore
   alla cadenza reale di generazione dei cicli. Sistemarlo tocca il contratto tag
   OPC UA, che ha un test su `SpeedTarget 15500`.

2. **Catena prediction -> alert vuota — CHIUSA.** Non era una regressione: non
   era mai stata eseguita sulle run in `work/`. Eseguita con il modello gia'
   addestrato: 9 valvole guaste su 9 rilevate, 0 falsi positivi sulle 26 sane,
   0 alert sulla run sana.

3. **Baseline non esposta — CHIUSA.** Aggiunta `GET /valves/baseline`
   (`pipeline/api.py`), verificata da `pipeline/tests/test_baseline.py`.

## Igiene ambiente — RISOLTA (2026-08-18)

I moduli di test creavano un database per processo pytest senza rimuoverlo: 49
database effimeri residui, 441 MB, e l'avvio del container Postgres arrivato a
~150 secondi per l'fsync della data directory.

- **Teardown aggiunto**: `pipeline/tests/conftest.py` espone
  `drop_db_if_ephemeral` con una guardia stretta (prefisso noto + otto cifre
  esadecimali); i tre moduli che creano un database effimero lo chiamano a fine
  sessione. Verificato: 52 database prima di una suite, 52 dopo.
- **Arretrato smaltito**: `edge/scripts/cleanup_test_databases.py` (dry-run di
  default, `--apply` per agire) ha rimosso 49 database, 0 falliti. Restano i
  cinque giusti: `plcsim`, `postgres`, e i tre di test a nome FISSO
  (`plcsim_test`, `plcsim_test_cycles`, `plcsim_test_hermetic`).
- **Effetto misurato**: da 484 MB a 43 MB; avvio di Postgres da ~150 secondi a
  **3 secondi**.

## Dashboard visual gate — v6 contract decision required (2026-08-18)

All previous dashboard builds (v1 P&ID, v2, v3 minimal, Signal Bench /
Trace & Trigger restart, v4 "Minimale Estremo") were deleted on 2026-08-18
by user decision, with their plans, contracts, reviews, and build evidence.
The visual gate of M10 remains OPEN. A new v6 prototype was subsequently built,
but its gate failed twice and the frozen contract forbids a third patch cycle.

The open question is now structural: keep/correct/discard the five-screen split.
Two independent signed reviews are complete:

- `review-01-gpt5.6-sol-high.md`: discard the five-screen navigation and restore
  the case diagnostic as the spine joining anomaly, impact, evidence and handoff.
- `review-02-gpt5.6-terra-high.md`: correct deeply around triage; do not imply a
  quality-to-valve causal bridge that the current routes cannot measure on the
  same OEE window.

Both also require separating baseline-normal from production-acceptable and
treating the existing blind-image gate as a preliminary falsification test, not
real technician acceptance. The next action is a user decision before any build.

Why it matters: another visual patch would violate the v6 contract and would not
resolve the missing decision model or the data-attribution boundary.

## v2 deferred items (v3) — SUPERSEDED

Deferred during the v2 contract: mechanical lens block reorder (`IMP,TRD,EVD,…`),
grouping the DEMO controls into a popover, full removal of the exception list
from L0, SpeedActual exposure (API-dependent), reworking the theme default
after the demo. None of them block v2 acceptance; the v3 redesign absorbed the
exception-list visibility into the disclosure pattern (list stays in the DOM).

**Superseded 2026-08-16**: v2/v3 are discarded with the dashboard restart; the
deferred list no longer applies to any active work.

## Post-M10 calibration scope

It is not yet decided which deferred M11 work should come first: broader alert
calibration across multi-fault/severity cases, Tail Time/Tail Pulse physical
calibration, or operational exposure of valve-controller groups.

Why it matters: these items improve different kinds of diagnostic confidence and
should not expand M10 before its visual acceptance gate closes.

Known option: close M10 first, then prioritize from observed dashboard limitations
and demo evidence.

## Dashboard v6 — open after contract v2 (2026-08-18)

Three points survive `contract-v2.md`; none blocks the build.

**The handoff card.** `PRODUCT.md` asks for a card that is generated, copyable and
exportable. Contract v1 subtracted it down to "one button that freezes valve + KPI +
timestamp". The subtraction is declared but was never confirmed by the user; one reviewer
flagged it as the subtraction most likely to be wrong. It must be settled before S3 is
built, not after.

**AA contrast has never been measured.** It has been inherited from the frozen tokens and
asserted, never checked with an instrument. §2.3 of contract v2 now prescribes measuring
it. Why it matters: the whole product is designed for 0.5-2 m reading distance in variable
light, so this is the one accessibility claim the design actually depends on.

**In a diffuse condition the case has no clickable destination.** §7 S2 forbids singling
out individuals when the deviation is common — correctly, because sending a technician to
dismantle one valve during a machine-wide drift is the diagnostic error the screen exists
to prevent. But it leaves the technician with nowhere to go in the most serious scenario
of the set. The resolution belongs in S2 (population view, common-condition checks), not
in the triage.

## La baseline sana non ha dove stare — RISOLTA 2026-08-20

La pagina VALVOLE confronta ogni valvola contro la **propria** banda ±3σ presa
da un run sano. Le fixture la calcolavano da `work/m2_healthy_5d`, 86.296 cicli
per valvola. Il database operativo contiene solo il run guasto
`work/m4_demo_dropout_1d`, e `cycles` ha chiave primaria `(valve_id, cycle_id)`
senza discriminante di run: un secondo run scarterebbe le proprie righe in
silenzio.

Le tre vie, misurate:

1. **Lasciarla mancante.** Onesto, già in piedi. Costo: la giostra è vuota.
2. **Dichiarare una finestra sana dentro il run attuale.** Non funziona: il
   primo allarme apre alle 08:34:28 e i cicli iniziano alle 08:21:00, quindi la
   parte sana vale 253 cicli per valvola. Una baseline su 253 cicli non è una
   baseline. Calcolata sull'intero run darebbe 0,7635 al posto di 0,7868 — cioè
   una baseline **sbagliata**, non mancante.
3. **Dare a `cycles` un discriminante di run e caricare `m2_healthy_5d`.** È la
   cura vera. È una modifica di schema e di ingest, con scritture sul database:
   richiede l'autorizzazione dell'utente e non appartiene al lavoro del proxy.

Decisione dell'utente attesa fra 1 e 3.
