# Open questions

Updated: 2026-08-23 (terzo aggiornamento)

> **Come si legge questo file.** Il titolo di una sezione dice il suo stato, e va
> creduto solo se porta una data. Fino al 2026-08-23 molte sezioni sono rimaste
> intitolate «APERTA» per giorni dopo essere state chiuse da un altro lavoro:
> chi le leggeva rifaceva indagini gia' fatte. Sono state marcate una per una,
> verificando sul database e nel codice invece che sulle etichette.
>
> **Chi chiude una voce deve marcare la sezione, non solo scriverne una nuova
> piu' in basso.** Il testo originale resta com'e' — serve a capire perche' una
> cosa era stata considerata un problema — ma il titolo non deve mentire.



## CHIUSA il 2026-08-23 — M11 si chiude sull'allarme, e il nome va a schermo

> **DECISA — non riaprire questa scelta.** L'utente ha preso la strada
> raccomandata: M11 si chiude prendendo atto che l'allarme funziona, e il lavoro
> e' andato sulla resa. Le pagine MACCHINA e VALVOLE ora scrivono il nome del
> guasto, e dove il modello non concorda lo dichiarano. Vedi `DECISIONS.md` e
> `RECENT_WORK.md`, 2026-08-23. Il testo qui sotto resta come storia.

Il silenzio del modello sulla valvola 21 e' **capito fino in fondo** e la
correzione ovvia e' stata **misurata e scartata**. Il dettaglio completo, con i
numeri, sta in `RECENT_WORK.md`, sezione del 2026-08-23. In breve:

- La causa non e' il normalizzatore ma il set di addestramento: in spazio
  normalizzato il guasto della 21 (z 3,81) e' fuori dal dominio addestrato
  (z 5,65-24,9), pur avendo una severita' dentro l'intervallo.
- L'aumento del set con copie attenuate funziona sullo scenario a 60 giorni e
  **perde 5,8 punti di macro-F1 su `val`** (0,7704 -> 0,7122). Non si spedisce.
- Non e' riparabile spostando il confine: `opening_delay` e `restriction` stanno
  sullo stesso asse e differiscono solo in ampiezza. **Serve una feature che
  discrimini**, cioe' lavoro sullo schema ML-F1, che e' congelato.

**La domanda per l'utente non e' tecnica.** Oggi la valvola 21 e' gia' in allarme
e lo e' per il 93,0% della corsa: il manutentore viene avvisato. Quello che manca
e' il **nome** del guasto. E il nome manca comunque, per tutte e nove le valvole
in allarme, perche' la dashboard non legge mai `predicted_label` — la sua
superficie API in `comune/dati.js:44-56` non ha `score`. Quindi:

> Vale la pena aprire lavoro nuovo sulle feature per far dire al modello
> `opening_delay` sulla valvola 21, oppure M11 si chiude prendendo atto che
> l'allarme funziona, e si spende invece sulla resa — mettere un nome di guasto
> leggibile sugli allarmi, che oggi si leggono tutti «score_aggregation»?

La raccomandazione e' la seconda. Le ragioni stanno in `DECISIONS.md`.

## APERTA dal 2026-08-23 — la provenienza del modello non e' tracciabile

`_resolve_model_version` (`pipeline/inference.py:75-96`) non trova
`model_version` nel sidecar e ripiega su `manifest.yaml:code_version`. Un modello
riaddestrato senza toccare il manifest scrive predizioni **indistinguibili** da
quelle vecchie, e `load_score_history` (`pipeline/alert.py:620`) partiziona la
cronologia K/N solo per `run_id`: i due modelli si mescolerebbero dentro la stessa
corsa. Non e' un problema oggi, perche' nessun modello nuovo e' stato spedito. Lo
diventa il giorno in cui se ne spedisce uno. **Va bumpato il manifest prima, o
aggiunto `model_version` al sidecar.**

## Chiuse il 2026-08-20 — leggere qui prima delle sezioni piu' vecchie

- **«Un database, un run»**: scelta la terza via, la cura vera. `cycles` ha ora
  chiave `(run_id, valve_id, cycle_id)` e la migrazione e' applicata al database
  vero. Le sezioni piu' sotto che descrivono il problema restano come storia.
- **«La baseline sana non ha dove stare»**: caricato il run `storico_60d` accanto
  a quello guasto; i KV `baseline_window` e `baseline_cache` esistono e
  `/valves/baseline` non risponde piu' `degraded`.
- **Il tetto di 48 ore sulla serie**: chiuso dal riepilogo orario
  `cycle_rollup_hour`. La serie sui 60 giorni costa 0,6 s.

## Chiusa il 2026-08-21 — cronologia K/N al riavvio

La cronologia score-only non riparte più vuota. `InferenceConsumer` ricostruisce
le ultime N decisioni dalle prediction persistite prima di elaborare il lotto
corrente. Il loader esclude gli UUID esatti del lotto già scritto e usa un
ordine totale deterministico. Restano fuori da questa correzione il cooldown e
gli streak legacy, che continuano a vivere solo in memoria.

## Aperte dal 2026-08-20

*(Intestazione di raggruppamento. Al 2026-08-23 non ha piu' sotto di se' nessuna
voce aperta: sono state tutte chiuse o superate.)*

## CHIUSA il 2026-08-23: la copertura score-only costa ore di latenza

K=5/N=150 rileva tutti i 9 guasti iniettati senza falsi positivi. La valvola 21
resta attiva per il 93,0% del run con 8 aperture, contro il 70,8% e 33 aperture
di K=5/N=100. La ritaratura non risolve pero' la classificazione del modello e
non offre una latenza breve o uniforme.

Misurata sullo scenario solo come verita' diagnostica, la prima apertura arriva
dopo circa 1 h 15 min sulla valvola 30, 5 h 53 min sulla 21, 9 h 36 min sulla 8
e fra 12 h 43 min e 15 h 12 min sulle valvole 13-18. Il massimo peggioramento
rispetto a N=100 e' 0,4 s. La lettura della cronologia N=150 ha una mediana warm
di 9,811 ms, contro 7,625 ms di N=100.

Per M11 resta da decidere se questa sensibilita' sia sufficiente o se serva un
aggregatore con un vincolo di latenza esplicito. La decisione deve restare
separata da retraining e label ML.

**Chiusa dall'utente il 2026-08-23**: *«quella sensibilita' basta»*. K=5/N=150
resta la taratura definitiva e non serve un aggregatore con vincolo di latenza.
Il criterio accettato e' la copertura, non la prontezza. Le latenze qui sopra
restano scritte perche' sono il prezzo dichiarato di quella scelta, non un
difetto da correggere. Vedi `DECISIONS.md`, 2026-08-23.

Di M11 resta solo la classificazione del modello (il silenzio sulla valvola 21).
Chi la riapre non deve rimettere in discussione K e N.

## Meta' chiusa il 2026-08-22: runtime predefinito non autosufficiente

> **Attenzione: solo una meta' di questa voce e' ancora vera.**
> L'ambiente non autosufficiente **non e' piu' un problema da risolvere**: il
> 2026-08-22 l'utente ha deciso che il progetto resta su questa macchina e che
> non si fa lavoro di blocco delle versioni. Il limite e' dichiarato, non aperto.
>
> Resta vero invece lo `StarletteDeprecationWarning`, verificato il 2026-08-23:
> e' il quarto dei quattro avvisi della suite e viene da
> `fastapi/testclient.py`, cioe' da una libreria e non dal nostro codice.
> Toglierlo vuol dire installare `httpx2`, cioe' modificare l'ambiente — e
> questo si scontra con la decisione qui sopra. Va lasciato finche' qualcuno non
> riapre il tema dell'ambiente.

La suite richiesta `python -m pytest pipeline/tests -q` ha dato **298 passed, 1
warning in 177,52 s (0:02:57)** con Python 3.14. Il run ha usato i
`site-packages` utente e l'archivio uv gia' presenti tramite `PYTHONPATH`, senza
installare pacchetti. Il runtime predefinito resta pero' non autosufficiente:
gli ambienti Python di sistema e bundled non espongono `pytest` o `polars`.

Resta anche un `StarletteDeprecationWarning` in `fastapi/testclient.py` per l'uso
di httpx con `starlette.testclient`. Va valutato l'adeguamento suggerito a
httpx2 insieme al ripristino di un ambiente bloccato e riproducibile.

## Chiuso il 2026-08-21 — i «due silenzi del modello» erano uno solo

**Il primo silenzio non esiste.** Le valvole 13-18 (instabilita' di pressione)
hanno punteggio **1,000** con l'etichetta giusta e un allarme attivo ciascuna. Il
modello le vede. A non vederle sono il **tempo di riempimento** e quindi la
qualita' e le due carte di CARTA, che su quella grandezza sono costruite. La nota
sulla pagina e' corretta ma dice meno del vero: le carte non la vedono,
l'inferenza si'.

**Il secondo e' reale, ed e' spiegato.** La valvola 21 ha `opening_delay` da 45 ms
dal ciclo 431.725 e il modello la dice `healthy` a **0,0029**, su una finestra che
finisce al ciclo 1.035.950 — seicentomila cicli dopo l'inizio del guasto, quindi
non e' dato vecchio.

Il segnale **c'e' nei dati grezzi**: sulle finestre da 50 cicli il minimo del
tempo di riempimento sale di **+44,4 ms**, cioe' quasi tutto il ritardo iniettato;
la media sale di +31,3 ms. Il controllo naturale e' la valvola 9, stessa base e
nessun guasto: +0,5 ms e +0,1 ms.

Si perde nella **normalizzazione z-score per valvola**. La 21 ha una base sana
larga — sigma di `min_fillingtime` a 24,2 ms contro ~5 ms delle valvole portatrici
dell'addestramento — e satura contro un tetto di 2.130 ms che rende
`max_fillingtime` costante, cosi' la guardia sigma=0 lo forza a z=0. Lo stesso
ritardo fisico che su una valvola di addestramento vale z=+7,9 sulla 21 vale
z=+1,95, e il logit resta negativo. Il peso del modello e' 2,13 sul **minimo** e
0,688 sulla media: la feature che il guasto muove di piu' e' quella normalizzata
peggio.

**Quante valvole sono cieche cosi': due su trentacinque.** Misurato sulla finestra
sana dichiarata, quota di cicli fermi al tetto della propria valvola: **21 al
24,96%** e **9 al 24,83%**, mediana **0,13%** sulle altre trentatre'. Sono le due
con media 2.023 ms e sigma 92. Il difetto e' confinato, ma la 9 oggi e' sana: se
si guastasse, il modello perderebbe anche lei.

**Il modello non e' del tutto cieco**: sulle 11.962 finestre dopo l'onset
l'anomalia media e' 0,089 contro 0,020 prima, e **699 finestre (5,8%)** superano
0,5, contro lo 0,24% della finestra sana. La classe indicata quasi mai e'
`opening_delay` (4 finestre su 11.962). La dashboard legge **l'ultima** finestra,
che sta sotto: il segnale sparso esiste e viene buttato via dalla lettura.

Ipotesi abbattute con misura, non con ragionamento: fuori dominio per severita'
(l'addestramento ha `opening_delay` a 40 e a 120, quindi 45 e' dentro); base sana
contaminata dal guasto (mu di `zstats.json` per la 21 e' 2.023,08 contro i 2.023,3
misurati: coincidono); z-stat presi dal run sbagliato; perdita nell'estrazione
(l'estrazione consegna z=+3,81 sulla media).

Resta non spiegato: il contributo positivo della media (+2,62) e' annullato da
`mean_pulsecount`/`mean_deltapulse` (−3,98) e `mean_filling_step_out` (−3,30), che
sulla 21 spingono verso `healthy`. Non e' stato indagato perche' quelle stesse
grandezze si muovano con rapporti diversi sulle valvole di addestramento.

Script della diagnosi in `.scratch/silenzio-21/`. Nessun file del consegnato
toccato.


## Chiuso il 2026-08-21 — le righe fantasma in alert-history

Le righe con `status: "closed"` e `closed_ts: null` erano **183 su 347** in tutte
e sei le fixture, non 21 su 27 come diceva la nota: quel numero era la sola
`b-guasto-singolo`. Non erano allarmi chiusi male. Erano coppie valvola/guasto
che il motore ha **valutato e mai aperto**: `opened_ts`, `last_seen_ts` e
`opened_at_cycle_id` erano nulli, `max_score_seen` a 0,0 e `n_cycles_above` a 0.
Portavano `closed` solo perche' e' il valore iniziale del dataclass `AlertState`.
`alert_rows()` iterava gli stati interni del motore invece degli eventi emessi.

Corretto nel generatore (`history_extract.py`, `predict_ef.py`) e rigenerato:
**347 righe -> 164**, `closed_ts` nullo **183 -> 0**, e gli **85 allarmi attivi
non si sono mossi di una riga** — ora combaciano esatti con `alerts.json` in
tutti e sei gli scenari. Il numero giusto era gia' scritto nel docstring di
`alerts_history()` in `pipeline/api.py`, che documentava proprio il caso delle 27
righe contro 6.

Nota: i 160 s che sembravano una finestra di chiusura non lo sono. Le chiusure
vere distano **50 cicli esatti** in 79 casi su 79 — una finestra ML, non un
`cooldown`.

## CHIUSO il 2026-08-22 — l'OEE gonfiato quando la finestra comincia prima dei dati

> **SUPERATA — non rifare questa indagine.**
> L'API dichiara la finestra parziale: `availability_detail.uncovered_s` e
> `source.window_partial` (`pipeline/api.py:915`). E' la strada che l'utente
> aveva scelto. Vedi `DECISIONS.md`, 2026-08-22.

`_compute_oee_window` (`pipeline/api.py:883`) somma in `planned_s` solo gli
intervalli coperti da `machine_state_history`. Un tratto di finestra senza righe
non entra ne' come fermo ne' come assente: il denominatore si accorcia e la
disponibilita' sale, con `degraded` a `false` e `reason` a `null`.

Misurato sull'API vera, storia che comincia il 2026-06-21T03:59:38, `window=day`:

| storia coperta | `planned_s` | disponibilita' | OEE |
|---|---|---|---|
| 1 ora | 3.600 | 0,850 | **0,666** |
| 6 ore | 21.600 | 0,975 | **0,766** |
| 25 ore | 86.400 | 0,640 | **0,503** |

**Tocca le pagine vive, non solo le fixture congelate.**
`GET /machine/oee/series?window=day` al bordo sinistro dello storico parte da
0,666 e scende verso 0,50 mentre la finestra si riempie: un peggioramento mai
avvenuto, sulla traiettoria che e' l'elemento primario di OEE e di TEMPO.

E' anche la causa della contraddizione fra le fixture: le tre di guasto vengono
da run di un giorno e mostrano 0,756 / 0,760 / 0,701 contro lo **0,504** della
sana. Le fixture replicano fedelmente l'API — il difetto e' a monte.

L'utente ha scelto di **dichiarare la finestra parziale**. In lavorazione.

## CHIUSO il 2026-08-23 — due difetti minori trovati per strada

> **SUPERATA — non rifare questa indagine.**
> Verificato eseguendo, non leggendo.
> 
> - **`validate.py` passa**: `OK: nessun fallimento` sui sei scenari. Il difetto
>   dei `GT_TOKENS` e' gia' corretto con `GT_DEROGHE`, che ammette i nomi di campo
>   legittimi `fault_type`, `by_fault_type`, `duration_seconds_by_fault_type`.
> - **Gli altri tre vivono solo dentro `.scratch/dashboard-v6/fixtures/`**, cioe' i
>   generatori delle fixture congelate. Quelle fixture sono superate per decisione
>   del 2026-08-22 e non si rigenerano: nessuna pagina viva le legge e nessun test
>   fa asserzioni sui loro valori. Correggerli non avrebbe effetto su niente.
> - La voce «nessuna pagina legge `alert-history.json`» **non ha oggetto**: quel
>   file esiste solo nelle fixture, mentre la route viva `/alerts/history` e' usata
>   da `comune/dati.js`.

- **`validate.py` non passa, e non passava gia' prima**: 67 fallimenti, tutti
  `token ground-truth 'fault' presente`. `GT_TOKENS` contiene la sottostringa
  `fault`, che pesca il nome di campo legittimo `fault_type` — la classe di
  guasto **predetta**, non la verita' nascosta. Finche' resta cosi', il gate non
  e' utilizzabile.
- **`history_extract_ef.py` e' codice morto e contraddittorio**: scrive storici
  vuoti con `status: "PIPELINE_MAI_ESEGUITA"` che `predict_ef.py` poi
  sovrascrive. Chi legge quel file crede che `e` e `f` non abbiano storico.
- **La stessa classe di difetto vive in `predict.py::alert_rows`**, innocua solo
  perche' il chiamante filtra per stato prima di scrivere. E' latente.
- **Nessuna pagina legge `alert-history.json` ne' `alert-pareto.json`.** Gli
  unici consumatori sono i generatori. La correzione e' reale nei dati e non ha
  effetto su niente di guardabile.


## Una deriva lunga settimane — RIPROPOSTA e RIFIUTATA il 2026-08-23

> **Non e' in coda, e non e' una domanda aperta.** Riproposta all'utente il
> 2026-08-23 come l'unica cosa in elenco che direbbe qualcosa di nuovo sul
> prodotto — misurare **quanto il modello anticipa** il degrado, che oggi non e'
> misurabile perche' ogni guasto viene rilevato in circa due ore. Risposta:
> *«per adesso quello della deriva no»*.
>
> Resta scritta qui perche' la misura e il ragionamento valgono ancora. Chi la
> riapre non deve rifare l'analisi, deve solo lanciare la run.

**Registrata su richiesta dell'utente come nota, non come lavoro pianificato.**

Il motore dei guasti sa gia' fare derive: `onset.mode = gradual` applica una
rampa lineare sulla gravita' distribuita su `ramp_cycles`, che e' un numero
libero (`plcsim/scenario.py`, `severity_at`). I tipi di guasto sono sei:
`restriction`, `closing_delay`, `opening_delay`, `pressure_instability`,
`flowmeter_dropout`, `flowmeter_glitch`.

In `storico_60d` le rampe piu' lunghe sono di **40.000 cicli**, cioe' circa due
giorni e mezzo (valvola 8). Il gruppo di pressione sale su 25.000. La valvola 30
parte tardi e sta ancora salendo a fine run. La 21 e' a gradino di proposito.

**Il modello rileva le rampe quasi subito.** Misurato ora per ora sulla valvola
8: tutto il 2 luglio fra 0,01 e 0,06 (rumore), poi il 3 luglio 0,161 alle 04:00,
0,817 alle 05:00, **1,000 alle 06:00**. Da rumore a saturazione in **due ore**,
dentro il primo 4% della rampa. La valvola 30 si comporta allo stesso modo il
12 agosto.

**Il punteggio poi satura e non dice piu' niente**: resta a 1,000 per sette
settimane. Dice *che* qualcosa non va, non *quanto* ne' *con che velocita'*. Per
il peggioramento servono le grandezze nominate.

Cosa comprerebbe una rampa su tre settimane: e' l'unico modo per misurare
**quanto il modello anticipa** il degrado della qualita'. Oggi ogni guasto viene
rilevato in due ore, quindi non c'e' spazio per misurare l'anticipo, e non si
puo' dire se il modello serva a prevenire o solo ad accorgersi.

Costo: una riga di scenario, poi una run nuova — circa tre ore di simulazione
piu' ricaricamento e ricalcolo delle previsioni. Strada gia' percorsa.

Limiti che restano comunque: `severity_at` e' monotona crescente e ogni valvola
ammette un solo fault. Non esistono guasti che si riparano ne' usure che vanno e
vengono.


## DECISA il 2026-08-20 — il confronto con la propria base e' cieco su cio' che e' sempre stato storto

> **SUPERATA — non rifare questa indagine.**
> La decisione era dell'utente ed **e' stata presa**: la lettura di popolazione
> esiste ed e' la striscia «LE 35 SULLA STESSA SCALA» nella pagina TEMPO, scelta
> fra tre varianti. `LESSICO.md` e' stato corretto apposta, perche' due regole
> scritte vietavano proprio quel riquadro: ora i meccanismi dichiarati sono due,
> contro la propria base storica e contro la mediana delle altre trentaquattro.

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

## NOTA, non una domanda — il quarto guasto e' piccolo e comune (2026-08-20)

> **Non c'e' niente da decidere qui.** E' la correzione di una lettura sbagliata,
> tenuta perche' l'errore e' facile da rifare: una differenza **fra valvole** era
> stata letta come una differenza **nel tempo**.

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

## Due pagine accettate sono inservibili sul database vero — RISOLTA 2026-08-21

Misurato sull'API vera (porta 8123), database `storico_60d`, 36,2 milioni di
cicli:

| route | tempo | chi la usa |
|---|---|---|
| `/machine/oee/series?window=day` | **27,0 s** | MACCHINA |
| `/valves` | **15,6 s** | MACCHINA, VALVOLE |
| `/machine/oee?window=day` | 0,33 s | tutte |
| `/valves/baseline` | 0,014 s | tutte |

Il lavoro del 2026-08-20 ha portato `cycle_rollup_hour` sotto le route nuove e
sotto `/machine/oee`, ma **non** sotto `/machine/oee/series` e `/valves`, che
sono rimaste al conteggio sui cicli grezzi.

Effetto a schermo, verificato in un browser vero a 1536x770: MACCHINA resta
**completamente vuota per una ventina di secondi** — cornici e titoli, nessun
dato — e poi si riempie tutta insieme. Nessun errore in console: non e' rotta,
e' lenta. TEMPO, che usa solo le route nuove, si disegna in circa sei secondi.

Vale la lezione gia' registrata: un aggiornamento lento si legge come fermo.
Venti secondi di riquadri vuoti non sono una rifinitura di prestazione, sono un
difetto di correttezza percepita su due pagine che l'utente ha accettato.

Nessuna delle due pagine si tocca per ripararlo: il difetto sta nel backend, e
si chiude portando le due route sul riepilogo orario come le altre.

### Chiusa lo stesso giorno

Nessuna delle due cause era quella che sembrava.

**`/valves` — 15,6 s a 0,10 s.** Il `DISTINCT ON` su `predictions` percorreva
723.110 righe per tenerne 35 (12,9 s da solo). Non era l'indice: quello giusto
c'era ed era quello che il piano usava. Sostituito da un `LATERAL` su
`generate_series(1, 35)`, la stessa forma applicata ieri a `cycles`. La
proprieta' «nessuna valvola sparisce» e' verificata da un test che cancella
tutte le prediction di una valvola.

**`/machine/oee/series` — 13,7 s a 0,69 s** con l'`at` che la pagina manda
davvero. La causa non era una query lenta ma il **numero di interrogazioni**:
739 per richiesta, di cui 8,0 s su 9,0 dentro l'attesa di rete. Tre cause, tutte
di ripetizione: il contatore era costruito per finestra invece che per
richiesta, e `shift` e `day` hanno lo stesso passo, quindi leggevano due volte
gli stessi bordi; la storia OMAC — 300 righe in tutto, 0,062 ms a lettura —
veniva riletta 358 volte per finestra; e ogni ora di bordo veniva letta due
volte invece di ricavarne la seconda meta' per differenza dal riepilogo.
Interrogazioni dopo: **12**, indipendenti dal numero di punti.

Verificato a schermo: MACCHINA si disegna per intero in tre secondi, contro la
ventina di prima. Le risposte sono identiche prima e dopo — confronto
sull'oggetto JSON intero, piu' 8 combinazioni di parametri della serie e 30 di
`/machine/oee`.

### Cosa resta aperto

- **`/machine/oee/series` senza `at` sta a 3,3 s** (da 27 s). La pagina manda
  sempre `at`, quindi non e' il percorso che l'utente vede, ma la route e'
  pubblica. Causa non isolata: il sospetto e' che le ore fra la fine della run e
  «adesso» cadano fuori dalla copertura del riepilogo e vengano lette da
  `cycles`. **Non misurato.**
- **Il pavimento con `at` e' ~0,7 s**, non i 0,09 s di `/machine/oee`. Restano
  ~180 mezz'ore di bordo lette da `cycles`, cioe' 1,75 milioni di righe di
  indice. E' irriducibile con un riepilogo a grana d'ora: il taglio delle
  finestre cade a `:29:35` e nessuna aggregazione oraria lo puo' rispondere.
  Chiuderlo vuol dire una tabella a grana di minuto, che e' una tabella nuova.

## COSTRUITA e accettata il 2026-08-22 — la carta di controllo giusta per questo segnale

> **SUPERATA — non rifare questa indagine.**
> E' la pagina CARTA (`/k1/`), accettata dall'utente sui dati veri. La carta sulla
> media mobile di 46 cicli e quella sul ciclo singolo stanno una sopra l'altra,
> come chiedeva il confronto.

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

## SUPERATA il 2026-08-22 — le sei fixture NON vanno rigenerate

> **SUPERATA — non rifare questa indagine.**
> Decisione dell'utente, presa dopo aver misurato chi le legge: nessuna delle
> cinque pagine accettate le tocca. Il difetto che contava (l'OEE gonfiato) e'
> stato corretto a monte, nell'API. Vedi `DECISIONS.md`, 2026-08-22.

1. Profili di fermata non confrontabili (motivo originale): le run di guasto non
   si fermano mai (disponibilita' 99%), la run sana si' (64%), da cui l'OEE
   ribaltato — gli scenari guasti leggono 0,756 contro 0,504 del sano.
2. **Due fixture si contraddicono**: `machine-oee-day.json` e
   `machine-oee-series.json` danno `performance_detail.theoretical` diverso sullo
   stesso istante e dallo stesso generatore. Ha ragione la seconda.
3. **`alert-history.json` contiene 21 righe fantasma** su 27, generate iterando
   lo stato interno di `AlertEngine` invece degli eventi emessi.

Misura del 2026-08-21 sul punto 2: la contraddizione non e' su due fixture ma su
**tre**, e sono esattamente i tre scenari di guasto. Su `machine-oee-day.json`
contro il punto della serie **allo stesso istante**:

| scenario | day | serie | scarto |
|---|---|---|---|
| a-sana | 604406,2 | 604406,2 | - |
| b-guasto-singolo | 604366,9 | 604378,9 | 12 |
| c-multi-valvola | 604366,9 | 604376,9 | 10 |
| d-deriva-diffusa | 604366,9 | 604376,9 | 10 |
| e-macchina-ferma | 604406,2 | 604406,2 | - |
| f-oee-degradato | 604406,2 | 604406,2 | - |

I tre scenari sani concordano. Nei tre guasti il `day` porta sempre lo stesso
valore congelato 604366,9 mentre la serie lo ricalcola: non e' arrotondamento.

## CHIUSA il 2026-08-22 — il percorso live non era mai stato esercitato

> **SUPERATA — non rifare questa indagine.**
> La catena gira da sola, provata con un guasto vero iniettato dal vivo sulla
> valvola 5: punteggio del modello 1,000 alla prima finestra, allarme aperto da
> solo al ciclo 700. Prove in
> `.scratch/percorso-live/PERCORSO-LIVE-CHIUSURA-20260822.md`.

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

## SUPERATA — Dashboard visual gate, v6 contract decision required (2026-08-18)

> **SUPERATA — non rifare questa indagine.**
> La v6 e' stata abbandonata. La dashboard ha **cinque pagine accettate**
> dall'utente fra il 19 e il 22 agosto: MACCHINA, VALVOLE, OEE, TEMPO, CARTA.
> La domanda strutturale sulle cinque schermate non esiste piu' nella forma in
> cui e' scritta qui.

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

## RIDOTTA il 2026-08-23 — Post-M10 calibration scope

> **SUPERATA — non rifare questa indagine.**
> Di M11 resta **una cosa sola**: la classificazione del modello, cioe' il
> silenzio sulla valvola 21. La taratura degli allarmi e' chiusa (K=5/N=150
> accettata il 2026-08-23) e non va rimessa in discussione.

It is not yet decided which deferred M11 work should come first: broader alert
calibration across multi-fault/severity cases, Tail Time/Tail Pulse physical
calibration, or operational exposure of valve-controller groups.

Why it matters: these items improve different kinds of diagnostic confidence and
should not expand M10 before its visual acceptance gate closes.

Known option: close M10 first, then prioritize from observed dashboard limitations
and demo evidence.

## SUPERATA — Dashboard v6, open after contract v2 (2026-08-18)

> **SUPERATA — non rifare questa indagine.**
> Riguarda un impianto abbandonato. I tre punti non sono stati riportati sulle
> cinque pagine accettate: chi volesse riaprirli deve rifarli contro il prodotto
> di oggi, non contro il contratto della v6.

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

## Misura live edge risolta dalla controprova, 2026-08-21

Il precedente tentativo 4 non aveva una misura realtime continua. La
controprova successiva ha riavviato soltanto Node-RED e, senza alcun deploy
post-restart, ha confermato mapping 567, subscription 567, 35 trigger e MQTT
connesso.

Nel tentativo live 4 sono arrivati e sono stati scritti 269 eventi, con zero
reject e backlog zero. Dopo circa 30 secondi il server OPC UA ha registrato
`publish cycle count(31) > lifetime count(30)` e `BadNoSubscription`. Il
contatore è rimasto fermo per più di due minuti. Non è quindi dimostrata una
finestra di 600 secondi. I 269 eventi coprono tutte le 35 valvole, con 7 o 8
eventi per valvola, ma solo nel preflight di 25,812 secondi e non nella
finestra continua richiesta.

La controprova ha poi osservato 18 min 51,598 s continui su tutte le 35
valvole, senza gap superiore a 10 s; la questione resta qui come storico del
tentativo fallito, non come blocco aperto. Blocchi B e C restano fuori ambito.

## CHIUSA il 2026-08-22 — la cronologia degli allarmi non distingueva i run

> **SUPERATA — non rifare questa indagine.**
> Verificato sullo schema il 2026-08-23: `predictions` ha la colonna `run_id` e
> gli indici `ix_predictions_run_valve_ts` e `ix_predictions_run_valve_wcid`.
> Anche `alerts` e `alert_transitions` hanno `run_id`, con la chiave unica passata
> a `(run_id, valve_id, fault_type)`. Le due vie descritte qui sotto non sono piu'
> una scelta: e' stata presa la seconda.

`predictions` non ha una colonna `run_id`, e `load_score_history`
(`pipeline/alert.py:612`) prende le ultime N=150 predizioni per valvola
ordinate per `prediction_ts DESC`, senza filtro di run. Un run live ha
`prediction_ts` piu' recenti di `storico_60d`: le sue predizioni entrano in
testa alla cronologia e spingono fuori quelle storiche.

Misurato sul database il 2026-08-21. Otto delle nove valvole in allarme stanno
a 150 su 150 e sono inattaccabili. La **valvola 21 sta a 7 su 150**, margine 2
sul K=5, con le posizioni sopra soglia agli indici
`[9, 20, 69, 83, 123, 125, 137]`. Un run live sano produce solo predizioni
sotto soglia: **dopo 70 finestre per valvola — 3.500 cicli, 3 h 15 min di
battito continuo — il conteggio della 21 scende a 4 e l'allarme si chiude.**

La valvola 21 e' l'unica per cui tutto il lavoro sull'aggregazione e' esistito,
e si spegnerebbe senza che nessun test fallisca.

Le due vie, e la scelta e' dell'utente perche' cambia cosa vede un tecnico:

1. **Accettare la mescolanza e dichiararla**, tenendo le corse live corte. Non
   costa niente adesso e lascia il difetto in piedi.
2. **Dare a `predictions` un discriminante di run**, come si e' fatto per
   `cycles` il 2026-08-19. E' una modifica di schema con scritture sul database
   e richiede autorizzazione.

Finche' non e' decisa: copia di sicurezza prima di ogni corsa di inference sul
live, corse sotto le tre ore, e conteggio della valvola 21 misurato prima e
dopo.

## SUPERATA il 2026-08-22 — Blocco B, cleanup ingest e misura post

> **SUPERATA — non rifare questa indagine.**
> Il percorso live e' stato chiuso in giornata. `STATE.md`, sezione «Stato al
> 2026-08-22, sera», supera esplicitamente i tre rapporti del Blocco B.

La finestra live è stata interrotta al checkpoint. Il PID dell'ingest non era
leggibile perché Win32_Process ha restituito `Accesso negato`; non è quindi
disponibile una conferma diretta della sua uscita, anche se le porte 4840 e
4841 erano senza listener dopo cleanup. Il restart Node-RED successivo ha
trovato il container in esecuzione ma `/health` ha risposto 404. La misura v21
post è invece completa e invariata a 7 su 150. Il rapporto registra le prove e
il confine: `.scratch/percorso-live/BLOCK-B-BATTITO-REPORT-20260822.md`.

## Blocco B — duplicati raw nella partizione live — poi CHIUSA il 2026-08-22

> **Gia' chiusa.** Chiusa piu' in basso, sezione «Blocco B — duplicati raw nella partizione live
> — CHIUSA 2026-08-22». Questa resta come descrizione del problema.

Il secondo tentativo ha isolato un blocker riproducibile. Il backfill
con `--dates 2026-08-21` trova 6.217 duplicati su `(valve_id, cycle_id)` e
termina con exit 2. La stessa data contiene quindi più run che riusano il
contatore ciclo. Il supervisor non può arrivare all'inference senza una
segregazione della fonte raw più stretta della data.

Non è autorizzato scegliere record o modificare schema in questo lavoro. Serve
una decisione di dominio su un identificatore di run nella raw path o su una
regola di selezione verificabile. Fino a quella decisione, Blocco B resta
`BLOCKED_FOR_REASONING / NEEDS-REVIEW`.

## Blocco B — avvio ingest con root isolata — poi CHIUSA il 2026-08-22

> **Gia' chiusa.** Chiusa piu' in basso, sezione «Blocco B — avvio ingest con root isolata —
> CHIUSA 2026-08-22». Questa resta come descrizione del problema.

La segregazione per root raw è necessaria per evitare i duplicati della
partizione condivisa. Nel terzo tentativo, però, `Start-Process` ha spezzato
il percorso con spazio passato a `pipeline.ingest --out`; l'ingest non ha
scritto dati e il supervisor non è stato avviato.

Serve una forma di invocazione Windows verificata che conservi il valore di
`--out` come un solo argomento. Il contratto dell'ultimo tentativo vieta un
quarto giro senza nuova autorizzazione.

## Blocco B — `predictions` senza `run_id` — poi CHIUSA il 2026-08-22

> **Gia' chiusa.** Chiusa piu' in basso, sezione «`predictions` senza `run_id` ferma il percorso
> live — CHIUSA 2026-08-22». La colonna `run_id` esiste, verificata sullo schema
> il 2026-08-23.

Misurato nella quarta corsa. `inference.py:280` salta una finestra quando la
coppia `(valve_id, window_end_cycle_id)` è già presente in `predictions`, e la
tabella non ha un discriminante di run. Le predizioni di `storico_60d`
occupano per la valvola 1 i `window_end_cycle_id` da 50 a **1.036.100** —
20.663 finestre, tutti i multipli di 50. Un run live riparte da `cycle_id` 1,
quindi **ogni sua finestra risulta già predetta e viene saltata**.

Non è un'erosione lenta: è un arresto. Il run live dovrebbe superare 1.036.100
cicli per valvola prima di produrre una sola predizione.

Questo supera la questione registrata il 2026-08-21 sulla cronologia allarmi e
ne cambia le opzioni. La via «accettare la mescolanza e tenere le corse corte»
non è più praticabile: non c'è niente da mescolare, perché non si produce
nulla. Resta la seconda via — dare a `predictions` un discriminante di run,
come si è fatto per `cycles` il 2026-08-19 — che è una modifica di schema e
richiede autorizzazione esplicita.

## Blocco B — duplicati raw nella partizione live — CHIUSA 2026-08-22

Non era una decisione di dominio. La partizione `date=2026-08-21` conteneva
cinque sessioni del simulatore accodate, ognuna con il contatore ciclo che
riparte da 1; dentro ciascuna sessione i duplicati sono zero, e i 7.497
duplicati nascevano solo dal mescolarle. Regola: **una sessione continua del
simulatore per partizione**. Con la partizione del 2026-08-22 nuova e una sola
sessione, il backfill è passato al primo battito.

Resta però un difetto di ordine: il controllo sui duplicati gira sull'intera
partizione prima del filtro del cursore incrementale, quindi su una partizione
sporca il backfill muore senza che l'incrementale abbia voce.

## Blocco B — avvio ingest con root isolata — CHIUSA 2026-08-22

Era un problema di virgolette, non di dominio: `Start-Process` spezzava `--out`
sullo spazio in «PLC Sim V». Un percorso relativo lo evita. Stessa classe di
errore su Docker: Git Bash riscrive `/tmp` in un percorso Windows e fa fallire
`pg_dump`; si disattiva con `MSYS_NO_PATHCONV=1`.

## `predictions` senza `run_id` ferma il percorso live — CHIUSA 2026-08-22

Risolta con la colonna `run_id` su `predictions` e il filtro di run su
watermark, cronologia allarmi e rotte di lettura. L'inference sul run live
produce: 140 record dove prima erano zero. Lo storico resta a 723.110 righe
attribuite a `storico_60d`, la valvola 21 a 7 su 150 e i nove allarmi
invariati.

## `alerts` non distingue i run — poi CHIUSA il 2026-08-22

> **Gia' chiusa.** Chiusa piu' in basso, sezione «`alerts` non distingue i run — CHIUSA
> 2026-08-22». `alerts` e `alert_transitions` hanno `run_id`, verificato sullo
> schema il 2026-08-23.

Emersa appena l'inference ha ripreso a produrre. `alerts` ha chiave
`(valve_id, fault_type)` senza run, quindi le righe sono condivise. Un run
non corrente eredita gli stati del run corrente ma ha cronologia punteggi
vuota: la prima finestra sotto soglia lo porta al ramo di chiusura.

Verificato sul database vero **senza scrivere**, costruendo il motore in
memoria: per la valvola 21 l'evento prodotto è `('sustained', 'closed')`. La
prima predizione live avrebbe spento l'allarme.

La riparazione non è simmetrica a quella di `predictions`, perché `alert_id`
è derivato: `alert_id_for(valve_id, fault_type)` è un `uuid5` che il codice
dichiara congelato. Includere il run significa ricalcolare l'identità di ogni
allarme storico e riallineare la chiave esterna di 64.180 transizioni. È una
modifica di identità e richiede autorizzazione esplicita.

Le due vie: completare il lavoro su `alerts` (allarmi per run, coerenti con
`cycles` e `predictions`, al costo degli `alert_id` storici), oppure tenere la
guardia e rimandare (il live produce punteggi ma non diagnosi).

**Guardia in vigore nel frattempo** (`inference._process_alert_transitions`):
se il run del consumer differisce dal KV `current_run_id`, le transizioni
alert sono saltate con un avviso esplicito e le prediction restano
persistite. Va rimossa insieme alla migrazione di `alerts`, non prima.

## `alerts` non distingue i run — CHIUSA 2026-08-22

Risolta. `alerts` e `alert_transitions` hanno una colonna `run_id`; la chiave
unica è passata da `(valve_id, fault_type)` a `(run_id, valve_id, fault_type)`;
`alert_id_for` include il run nella derivazione.

La migrazione ha riscritto l'identità delle 12 righe esistenti e la chiave
esterna delle 64.180 transizioni in una sola transazione, nell'ordine imposto
dalla FK. Verificato prima e dopo: 12 allarmi, 9 attivi, 64.180 transizioni,
**zero orfane**, e l'id derivato da `alert_id_for` coincide con quello scritto
in tabella. La valvola 21 resta a 7 su 150 con i nove allarmi invariati.

Il vincolo «alert_id congelato» non è stato violato: proteggeva la stabilità
dell'id lungo la vita di un allarme (open→sustained→closed→reopen), e dentro
un run quella proprietà è intatta. È cambiata una volta sola la base degli id
storici, possibile perché nessun codice fuori da `alert.py` e `storage.py` usa
un `alert_id` come chiave e nulla lo conserva fuori dal database.

La guardia a runtime in `inference._process_alert_transitions` è stata
rimossa: la separazione ora è nello schema.

## Un run live nuovo richiede una sessione broker pulita — REGOLA 2026-08-22

Non è una questione aperta ma una regola operativa, scoperta misurando. La
coda persistente del broker MQTT (QoS 1, `clean_session=False`, client id
fisso `plcsim-ingest-v1`) consegna all'ingest, alla riconnessione, i messaggi
della sessione precedente del simulatore. Quelle righe entrano nella
partizione del run nuovo con `cycle_id` che il run nuovo raggiungerà più
tardi: alla collisione il backfill si ferma con «righe duplicate su
(valve_id, cycle_id)».

La coda serve e non va tolta: protegge l'esercizio normale mentre l'ingest è
giù. La regola è che un run live **nuovo** usi un `--client-id` dedicato,
così la sua sessione parte senza arretrati.

Insieme alle altre due già note, la sequenza d'avvio di un run live è:
partizione raw nuova · Node-RED riavviato a server già in ascolto · sessione
broker pulita · `--run-id` esplicito e identico per backfill e inference.


## RINVIATE PER DECISIONE il 2026-08-24 — le due voci minori dell'IIoT

Chiusa la roadmap IIoT, restavano due voci che nessuno aveva ne' chiuso ne'
rinviato per iscritto. Restano aperte come fatti, ma **non sono lavoro previsto**:
chi le trova non deve trattarle come un debito da saldare prima di usare il
progetto.

1. **`SpeedActual` dichiara una velocita' 2,5 volte sotto la cadenza reale.**
   `speed_by_status["Running"] = 15110` alimenta il tag. Sistemarlo tocca il
   contratto tag OPC UA, che ha un test su `SpeedTarget 15500`. **Rinviata**: il
   tag non e' letto da nulla a valle. Node-RED trasporta, la pipeline non lo usa
   come feature, l'OEE legge il target dal KV verificato e non da questo tag. Il
   costo del fix e' un contratto da rinegoziare; il beneficio e' zero finche'
   nessuno legge quel tag. Si riapre il giorno in cui qualcosa a valle lo legge.

2. **La provenienza del modello non e' tracciabile.** `_resolve_model_version`
   (`pipeline/inference.py:75-96`) ripiega su `manifest.yaml:code_version`, e
   `load_score_history` (`pipeline/alert.py:620`) partiziona la cronologia K/N
   solo per `run_id`. **Rinviata, con una condizione di riapertura precisa**: non
   e' un problema finche' non si spedisce un modello nuovo, perche' con un solo
   modello in circolazione non c'e' niente da distinguere. Il giorno in cui se ne
   riaddestra uno, va bumpato il manifest **prima** di scrivere predizioni, oppure
   aggiunto `model_version` al sidecar. Questa e' la condizione, non un consiglio.

Nessuna delle due e' stata declassata perche' scomoda. Sono entrambe rinviate
perche' hanno un costo certo e un beneficio che oggi non esiste, e in tutti e due
i casi la condizione che le fa tornare a contare e' scritta qui sopra.
