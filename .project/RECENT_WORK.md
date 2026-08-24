## 2026-08-21 (tardi) — L'aggregazione ritarata a 5 su 150, e chiusa

Ultimo dei quattro passaggi a Codex. `AlertConfig` passa da N=100 a **N=150**,
K resta 5.

### Perche'

Con 5 su 100 la valvola 21 stava **sul filo**: cronologia letta dal database,
**esattamente 5 finestre sopra soglia su 100**, cioe' il minimo per qualificarsi.
Una in meno e l'allarme si chiudeva. Simulazione: accesa il 70,8% del tempo, con
33 aperture — un lampeggio, non un allarme.

Allungare la memoria e' diventato sicuro **solo dopo** la ricostruzione della
cronologia all'avvio: prima, un N piu' grande allungava il buco dopo ogni
riavvio.

### Verificato da me sul database vero

| | prima (5/100) | dopo (5/150) |
|---|---|---|
| valvola 21, finestre sopra soglia | **5 su 100** (margine zero) | **7 su 150** (margine 2) |
| valvola 9, controllo sano | — | **2 su 150**, ben sotto K |
| valvola 21 accesa (simulazione) | 70,8% | **93,0%** |
| aperture | 33 | **8** |
| falsi positivi | 0 | **0** |
| ricostruzione cronologia | 3.500 righe | 5.250 righe, **10,9 ms** |
| suite | 298 | **298 passed**, rimisurata |

Prova di riavvio rifatta con la regola nuova: con la cronologia l'allarme della
21 resta `sustained`, senza si chiude. Nove valvole in allarme — 8, 13-18, 21,
30 — e la pagina VALVOLE le mostra a 1536 px, zero errori in console.

### Il ritardo non e' peggiorato

Era il numero che mancava per congelare la scelta. Misurato su tutte e nove le
guaste: il peggioramento massimo e' di **0,4 secondi** (valvola 8), e la valvola
21 **migliora di 35 minuti** — da 6 h 28 a 5 h 53. Allungare la memoria non ha
comprato stabilita' al prezzo della prontezza: l'ha comprata gratis.

Resta vero il fatto gia' registrato: quei ritardi di 1 h 15 - 15 h 12 sono del
**modello**, non della regola d'allarme.

### Due note

- Copia di sicurezza nuova in
  `.scratch/taratura-aggregazione/backup-2026-08-21-n150/`, con SHA256 e
  `pg_restore --list` verificato. Il replay ha cambiato **+1 allarme e +75
  transizioni**: lo storico passa da 11 a 12 righe.
- Codex ha dichiarato come blocco che «il runtime Python predefinito non e'
  autosufficiente senza PYTHONPATH». **Non si riproduce**: la suite gira qui con
  un semplice `python -m pytest pipeline/tests -q`. E' un artefatto del suo
  ambiente, non del progetto.

---

## 2026-08-21 (fine): Ritaratura score-only a K=5/N=150

La simulazione ha confrontato il nuovo default K=5/N=150 con K=5/N=100. Le due
regole hanno 0 falsi positivi e rilevano 9 guasti su 9. La valvola 21 resta
attiva per il 93,0% del run con 8 aperture, contro il 70,8% e 33 aperture di
N=100. `threshold_open` resta 0,5. Modello, feature extraction, inference e
schema ML-F1 non cambiano.

La misura dei ritardi sulle nove valvole mostra un peggioramento massimo di 0,4
s e un miglioramento di 2.080,69 s (-34 min 40,69 s) sulla valvola 21. `load_score_history` con
N=150 legge 5.250 righe per 35 valvole e ha una mediana warm di 9,811 ms, contro
7,625 ms per N=100.

Il replay con il nuovo default ha portato il database da 11 a 12 alert e da
64.105 a 64.180 transizioni. Il dump precedente al replay e' in
`.scratch/taratura-aggregazione/backup-2026-08-21-n150/alerts-pre-score-replay.dump`
con SHA-256
`4443A54C4931227D424B14372D702FB28CB89103E93AD683B19885D02C006045`. L'API
espone nove alert attivi sulle valvole 8, 13-18, 21 e 30.

Il controllo UI a 1536x770 passa nei temi chiaro e scuro. Non risultano overflow
orizzontali o verticali e la console non mostra errori.

La suite richiesta `python -m pytest pipeline/tests -q` ha dato **298 passed, 1
warning in 177,52 s (0:02:57)** con Python 3.14. Il run ha usato i
`site-packages` utente e l'archivio uv gia' presenti tramite `PYTHONPATH`, senza
installare pacchetti. Il runtime predefinito resta non autosufficiente perche'
non espone `pytest` o `polars`. Il costo della latenza score-only resta una
domanda aperta per M11.

---

## 2026-08-21 (notte) — Cronologia K/N ricostruita al riavvio

`load_score_history` carica le ultime N prediction delle 35 valvole, le ordina
con la chiave totale `prediction_ts`, `window_end_cycle_id`, `prediction_id` e
costruisce solo booleani `anomaly_score >= threshold_open`. Il seed non crea
eventi, non scrive e non aggiunge padding.

`InferenceConsumer` esegue il seed prima di `engine.process(records)`. Poiché
le prediction del lotto sono già persistite, passa i loro UUID esatti al
loader. Il lotto entra quindi una sola volta nella cronologia. L'identità non
dipende dal ciclo e resta corretta quando due run riusano gli stessi numeri.
K/N disabilitato evita il loader e la sua query.

`load_states` mantiene la sua API. Cooldown e streak legacy restano in memoria.
I test coprono riavvio positivo, assenza del seed, seed senza eventi, modalità
disabilitata, doppio conteggio del lotto corrente, esclusione selettiva per UUID
e parità di timestamp e ciclo. Verifica finale: 30 test mirati e 298 test della
suite `pipeline/tests` passano. La query reale carica 3.500 righe per 35 valvole
con mediana 7,906 ms e massimo 8,511 ms su dieci campioni. Dopo il riavvio API,
`/health` conferma database disponibile e `/valves` espone nove valvole attive,
compresa la 21 con allarme `score_aggregation` aperto. La pagina VALVOLE passa
il controllo a 1536x770 nei temi chiaro e scuro senza errori in console.

---

## 2026-08-21 (sera) — L'aggregazione del punteggio, implementata da Codex

Lavoro passato a un altro harness con due handoff su disco
(`.scratch/HANDOFF-codex-aggregazione-punteggio.md` e
`.scratch/HANDOFF-codex-2-taratura-aggregazione.md`). Implementazione di Codex,
verifica mia.

### Che cosa fa adesso il motore degli allarmi

`AlertConfig` ha due parametri nuovi: `score_aggregation_window = 100` e
`score_aggregation_required = 5`. Un allarme si apre quando **5 degli ultimi 100
punteggi** della stessa valvola stanno sopra 0,5, **ignorando la classe
prevista** — scelta obbligata, perche' sulla valvola 21 l'etichetta piu'
frequente sopra soglia era `flowmeter_glitch` e sulla 19, sana, era
`opening_delay`. La regola **sostituisce** il percorso per classe invece di
aggiungersi, e usa un `fault_type` tecnico stabile, `score_aggregation`.

### Il risultato, verificato da me sull'API vera

- **`GET /valves`: nove valvole con allarme attivo — 8, 13-18, 21, 30 — una sola
  riga ciascuna.** La valvola 21 c'e', ed e' la ragione per cui il lavoro
  esisteva.
- **Lo storico allarmi passa da 184 righe a 11** (8 sustained, 1 open, 2 closed).
  I ~180 allarmi di rumore per classe, che c'erano su tutte e trentacinque le
  valvole comprese le sane, sono spariti.
- Pagina VALVOLE a 1536 px: «9 valvole in allarme», la 21 marcata sulla corona,
  zero errori in console.
- Suite: **290 passed**, misurato da me.
- Copia di sicurezza presa prima del riprocessamento
  (`.scratch/taratura-aggregazione/backup-2026-08-21/`), e il replay e' a secco
  per default: la sostituzione richiede `--replace` piu' un `--backup` valido.

### La taratura: il primo tentativo non accendeva la 21

Codex aveva scelto K=5 su N=20. Simulato sui punteggi veri di tutte e
trentacinque le valvole: zero falsi positivi, ma la valvola 21 restava accesa lo
**0,7%** del tempo e **finiva spenta**, quindi invisibile. La dimensione che
discrimina non e' la severita' K ma la lunghezza della memoria N: il segnale
della 21 e' rado (5,9% delle finestre) e una memoria di venti finestre lo
dimentica. Con **5 su 100** resta accesa il 70,8% del tempo, con 33 aperture
invece di 76. **Zero falsi positivi a ogni impostazione provata**, fino a 2 su
20: le valvole sane non fanno mai grappolo.

Lo strumento di taratura resta in `.scratch/taratura-aggregazione/simula.py`.

### Il timore sul ritardo era infondato, e la misura lo dice

Temevo che N=100 rallentasse molto l'accensione. Ho misurato la regola **vecchia**
sugli stessi dati per confronto:

| valvola | regola vecchia | 5 su 100 |
|---|---|---|
| 8 | 9 h 14 | 9 h 36 |
| 13 | 13 h 18 | 13 h 02 |
| 15 | 16 h 06 | 15 h 12 |
| **21** | 9 h 05 | **6 h 28** |
| 30 | 1 h 18 | 1 h 15 |

Il ritardo e' sostanzialmente **invariato**, e sulla 21 e' persino minore. Non
c'e' nessun compromesso da mettere davanti all'utente. Cio' che e' cambiato non e'
la prontezza ma la **persistenza**: la regola vecchia apriva e richiudeva, la
nuova resta aperta.

**Fatto separato che ne emerge**: quei ritardi di nove-sedici ore appartengono al
**modello**, non alla regola d'allarme — tutte e due le regole aspettano quel
tempo perche' e' il punteggio a impiegarlo. Sull'instabilita' di pressione sono
circa tredici ore dall'iniezione al primo allarme. Non e' toccato da questo
lavoro.

### Due cose da sapere

- Nota successiva del 2026-08-21: il primo limite qui sotto è stato chiuso dalla
  ricostruzione della cronologia persistita descritta all'inizio del file.
- **La cronologia K/N vive solo in memoria e riparte vuota al riavvio del
  motore.** Dopo un riavvio servono fino a cento finestre prima che un allarme
  possa riaprirsi: sulla valvola 21 e' la differenza fra visibile e invisibile.
  E' il difetto aperto piu' importante di questo lavoro.
- `ESITO.md` di Codex intitola la matrice K/N «sull'intera storia»: non lo e'.
  Rieseguito lo script, legge al massimo 5.000 finestre per valvola dalla route.
  I numeri sono giusti, la loro base e' piu' stretta di come e' dichiarata. La
  tabella dei **ritardi**, invece, usa davvero i parquet interi.

---

## 2026-08-21 — Aggregazione score-only degli alert

`pipeline.alert.AlertConfig` adotta K=5/N=100 come default operativo per
valvola. Conta soltanto `anomaly_score >= 0.5` e non usa mai la label ML come
filtro. La lineage unica `score_aggregation` evita aperture duplicate quando le
label cambiano. Impostare K/N entrambi a `0` o `None` conserva la persistenza
legacy per label.

La simulazione sull'intera storia ha confrontato K/N 5/20, 4/20, 3/20, 2/20,
5/50, 4/50, 3/50, 6/100 e 5/100. Tutte le configurazioni hanno mantenuto zero
falsi positivi. Solo 3/50 e 5/100 hanno rilevato 9 guasti su 9 e incluso la
valvola 21; 5/100 ha dato la copertura migliore, 70,8%, ed e' diventato il
default. `threshold_open` resta 0,5.

Il replay ha letto 723.110 predizioni persistite e ricostruito in una sola
transazione 64.105 transizioni score-only. Prima della modifica e' stato creato
e verificato un dump PostgreSQL da 2.350.934 byte, SHA-256
`ECDA4E7F97EFE39F53F46695941E5F580BE8C96F4D0760BB78CAB1A0B4FF2402`. L'API
reale espone ora allarmi attivi sulle valvole 8, 13-18, 21 e 30; la 21 e' `open`
con `fault_type=score_aggregation`.

La latenza diagnostica misurata va da 1 h 15 min a 15 h 12 min. Ground truth e
scenario sono stati usati soltanto per questa misura offline. Non sono entrati
nel motore o nel replay. Modello, `pipeline/features.py`, `pipeline/inference.py`
e schema ML-F1 non sono cambiati.

Verifiche: **16 test mirati passati**, **290 test della suite `pipeline` passati**,
compilazione dei tre script di taratura riuscita, API controllata dal vivo e
dashboard `/v1/` controllata a 1536x770 nei temi chiaro e scuro, senza errori in
console. Script, matrice e rapporto sono in `.scratch/taratura-aggregazione/`.

Limite dichiarato in quel momento, poi chiuso il 2026-08-21: la cronologia K/N
era in memoria e al restart ripartiva vuota.

---

## 2026-08-21 — Il punto 2 del piano, e il difetto che stava a monte

### I due difetti delle fixture, misurati prima di toccarli

**Le righe fantasma non erano allarmi rotti: non erano allarmi.** 183 righe su
347 avevano `status: "closed"` con `closed_ts` nullo — e nullo anche `opened_ts`,
`last_seen_ts`, `opened_at_cycle_id`, con punteggio 0,0 e zero cicli sopra
soglia. Erano coppie valvola/guasto **valutate e mai aperte**: `closed` era solo
il valore iniziale del dataclass `AlertState`. `alert_rows()` iterava gli stati
interni del motore invece degli eventi emessi. Corretto nel generatore:
**347 righe -> 164**, `closed_ts` nullo **183 -> 0**, e gli 85 allarmi attivi
invariati, ora combacianti con `alerts.json` in tutti e sei gli scenari. Il
numero giusto era gia' scritto nel docstring di `alerts_history()`.

**La contraddizione dell'OEE non era delle fixture.** Le tre di guasto davano
0,756 / 0,760 / 0,701 contro lo 0,504 della sana: con un guasto la macchina
sembrava andare meglio. Il worker ha dimostrato che le fixture **replicano
fedelmente l'API** e si e' fermato senza scrivere un file — la scelta giusta. La
causa e' in `_compute_oee_window`: una finestra che comincia prima dei dati
accorcia il denominatore in silenzio. **Tocca le pagine vive**, non solo le
fixture: la serie dell'OEE al bordo sinistro dello storico parte da 0,666 e
scende verso 0,503, un peggioramento mai avvenuto.

### Changed

**`pipeline/api.py`** — la finestra parziale si dichiara. `availability_detail`
porta `window_s`, `uncovered_s`, `coverage`; `source` porta `window_partial`; e
`reason` porta il motivo in italiano con le ore mancanti. **Nessun numero e'
cambiato**: e' una dichiarazione, non una correzione di calcolo. `degraded`
mantiene il proprio significato, per scelta: allargarlo avrebbe fatto sparire
l'OEE dalle pagine all'inizio dello storico. Suite **286 passed**.

**`.scratch/dashboard-v7/oee/pagina.js`** — il segmento «Non pianificato» era
sbagliato **in tutti gli scenari**: `planned_s` somma gia' Idle, Stopping e
Stopped, quindi quel tratto e' sempre e solo dato mancante. Ora si chiama
**«Senza storia»**, e con lui tutto il vocabolario della pagina — cascata,
pannello disponibilita', suggerimenti, etichette per lettori di schermo.

**`.scratch/dashboard-v6/fixtures/validate.py`** — 67 fallimenti, tutti falsi. Il
controllo sulla verita' nascosta cercava la sottostringa `fault` nel testo grezzo
e pescava `fault_type` (la classe **predetta**), `by_fault_type` e la parola
`de-fault` nelle note di provenienza. Ora i nomi si controllano sulle **chiavi**
del JSON con tre deroghe dichiarate, e sul testo restano solo le sequenze senza
lettura innocente. Provato in tutti e due i versi: quattro casi legittimi
passano, nove fughe vere falliscono. Sulle sei fixture: **nessun fallimento**,
per la prima volta.

### La scelta dell'utente

Fra quattro opzioni ha scelto **dichiarare la finestra parziale**. Poi, davanti a
tre modi di trattare l'OEE al 76,4% mostrato sopra un riferimento del 50,4% —
numeri non confrontabili, 15,5 ore contro 24 — ha risposto **«va bene come e'
adesso»**, contro la mia raccomandazione di spegnere il riferimento. Il fatto era
gia' dichiarato altrove nella pagina, quindi la discrepanza resta leggibile e in
vista.

### Verificato da me, non solo riferito

Sonda dal vivo sulla 8123 sui tre casi di copertura; diff contro la copia di
sicurezza; conteggi delle righe e coerenza dei `__meta` ricalcolati; il gate
provato anche in negativo; le pagine guardate in un browser vero a 1536 px, tema
chiaro e scuro, zero errori in console. Controllato anche che il nuovo `reason`
non compaia dove prima non c'era: TEMPO lo mostra solo sui dati degradati, CARTA
legge un campo diverso su route che non passano di li'.

### Cosa resta aperto

Tre difetti latenti registrati in `OPEN_QUESTIONS.md` e non toccati:
`history_extract_ef.py` e' codice morto che scrive storici vuoti poi
sovrascritti; `predict.py::alert_rows` ha la stessa falla, innocua solo perche'
il chiamante filtra; e **nessuna pagina legge `alert-history.json`** — la
correzione e' reale nei dati e invisibile a schermo.

---

## 2026-08-21 — Il verdetto della striscia di CARTA passa dal riempimento al bordo

### Contesto

L'innesto del colore di k3 nella striscia di k1 era stato consegnato con **due
fasce piene da 11 px**, una per carta, sopra e sotto il numero della valvola.
L'utente l'ha bocciato guardandolo: *«i rettangoli un po' rendono il numero meno
chiaro. Non potresti fare simile alla selezionata? Cioe' il contorno o solo il
lato alto o basso in colore?»*. La cella e' alta 34 px: al numero — che e'
l'identita' della valvola — ne restavano 12.

### Il metodo, proposto dall'utente

Invece di rifare l'innesto nella pagina per ogni ipotesi, l'utente ha chiesto un
**artefatto di anteprime**: una piccola versione della barra dei numeri con
qualche numero, per mostrare le differenze fra le opzioni senza implementarle.

Pubblicato come artefatto: cinque forme una sotto l'altra, **celle a grandezza
reale** (42 x 34 px, la misura vera a 1536 px), palette e carattere presi da
`comune/lessico.css`, e gli **stessi otto stati incolonnati** in ogni riga —
non misurata, sana, appena fuori, solo la carta alta, il disaccordo della
valvola 21, due gravita' diverse, fuori scala, cella scelta.

- **A** l'attuale, per confronto · **B** due bordi da 4 px, alto e basso ·
  **C** contorno e lato basso · **D** una riga sola sotto, divisa a meta' ·
  **E** filetti sottili su fondo velato.

L'utente ha scelto **D**, e la motivazione e' registrata come vincolante:
*«mi piace di piu' il D, perche' il B sarebbe l'altro, ma guardare uno in alto e
un altro in basso li separa un po' troppo»*. B e D erano identiche in tutto
tranne la distanza fra i due segni.

### Changed

Due file di `k1/`, nient'altro.

- **`pagina.js`** — le due fasce sono sostituite da una riga alta **5 px** sotto
  il numero, divisa a meta': sinistra il ciclo singolo, destra la media di 46.
  Cella alta 34 px e riga della griglia da 104 px **invariate**.
- **`stile.css`** — i campioni della legenda seguono la forma nuova (mezza riga
  a sinistra, mezza a destra) invece del filetto sopra e sotto. Il campione
  ripete la cella, quindi la legenda non si decodifica.

### Il lessico, con due contraddizioni dichiarate

`LESSICO.md` da 480 a 499 righe. Le regole nuove sono intrecciate nelle sezioni
esistenti, non appese in coda, e due regole scritte sono state **corrette perche'
la pagina accettata le contraddiceva**:

1. «Una navigazione non si tinge» diventa «**finche' la scelta non e' essa stessa
   la domanda**». La striscia delle 35 e' l'eccezione dichiarata: popolazione
   chiusa, e la domanda e' *quale* valvola. Conta comunque come **una** regione
   tinta.
2. Nuova regola in §1: **il colore sta sul bordo, non addosso a cio' che si
   legge**.
3. Nuova regola in §3: **due valori da confrontare stanno accostati**, non ai due
   estremi della forma.
4. Nuova regola in §4: **il segno della scelta e il segno del verdetto non usano
   lo stesso canale** — la scelta resta il contorno `--ink`, il verdetto scende
   sul bordo basso.
5. Nel conteggio delle regioni tinte, CARTA e' registrata a **3** come massimo
   strutturale e a **2** sulla valvola 21.

### Verificato

Browser vero a 1536 px, dati veri sulla porta 8078, temi chiaro e scuro. Zero
errori in console. Geometria letta dal DOM, non dedotta: valvola 8 riga piena
(entrambe le carte al 100%), valvola 9 solo meta' destra (media di 46 allo
0,06%), **valvola 21 solo meta' destra** — il ciclo singolo e' a zero e non
disegna niente, quindi il disaccordo si vede dalla riga monca senza spiegazioni.
Cliccando su 21 le due carte si ridisegnano e la cella scelta resta riconoscibile
dal contorno d'inchiostro, che non va contro il colore.

Nota di lavoro: la prima verifica ha mostrato ancora le fasce vecchie perche' il
browser aveva `pagina.js` in cache. Il difetto era mio, non del codice — le
anteprime a schermo vanno confrontate con la geometria letta dal DOM prima di
concludere qualcosa.

---

## 2026-08-21 — La grammatica di TEMPO nel lessico condiviso

### Contesto

Punto 4 di `HANDOFF-tempo.md`, lasciato indietro il giorno prima: `LESSICO.md` e
`comune/lessico.css` descrivevano le prime tre pagine e non nominavano TEMPO. Va
fatto prima della carta di controllo, che e' una schermata strutturalmente nuova:
senza il lessico aggiornato le sue varianti reinventerebbero la lingua invece di
ereditarla.

### Changed

**`LESSICO.md`, da 226 a 380 righe.** Le regole di TEMPO sono intrecciate nelle
sezioni esistenti, non appese in coda: una lingua sola, non una lingua piu'
un'appendice. Ogni regola nuova porta il riferimento al codice che la prova
(`pc/pagina.js:233`). Nuova sezione 5, «Il tempo, e i confini del dato».

Quattro regole scritte sono state **corrette perche' la pagina accettata le
contraddiceva**, e la contraddizione e' dichiarata invece di essere nascosta:

1. «Il meccanismo e' uno solo» diventa **due meccanismi nominati**: contro la
   propria base storica, e contro la mediana delle altre trentaquattro. Il
   secondo esiste perche' il primo e' cieco sulle valvole 9 e 21, storte dal
   primo giorno.
2. «Il confronto fra valvole si fa contro la base della singola valvola, non
   contro la media macchina» vietava esattamente il riquadro di popolazione che
   l'utente ha scelto fra tre varianti. Ammesso, con la sua ragione scritta.
3. «Un grafico convenzionale per riquadro» diventa «**una convenzione** per
   riquadro»: piu' copie della stessa contano come una, ma solo se restano
   neutre.
4. «Classifiche di sospetti» resta vietato **come vista**: il posto in classifica
   puo' stare nel suggerimento, mai nell'ordine dell'asse.

Corretta anche la sezione 10: la validazione diceva 1920x1080, che e' la misura
sbagliata. Ora dice **1536x770 px CSS**, il viewport vero dell'utente.

**`comune/lessico.css`, da 229 a 284 righe.** Solo aggiunte. Nessuna riga
originale rimossa — verificato per confronto riga a riga — e nessuno dei nomi di
classe nuovi (`.per`, `.pan-per`, `.trascinabile`, `.cella-pop`, `.pop-scelta`)
compare in `a/`, `v1/`, `oee/`: le tre pagine accettate rendono identiche per
costruzione, non per speranza. Nuovo token `--barra-muto: #aab3bc`, che dava
nome a un letterale orfano usato due volte in `pc/stile.css`.

### Verificato

Browser vero a 1536x770, dati veri sulla porta 8078. TEMPO si disegna corretta e
porta le **tre** regioni tinte che il lessico le assegna: la traiettoria OEE, il
riquadro di popolazione, la fascia delle 35. MACCHINA verificata identica prima
e dopo la modifica al foglio condiviso, rimettendo l'originale e riguardando.

### Poi: le due route lente, chiuse lo stesso giorno

Il difetto trovato verificando il lessico e' stato messo davanti all'utente, che
ha scelto di chiuderlo prima di andare avanti col piano. Il lavoro e' stato
delegato con un pacchetto su disco che distingueva **cio' che era accertato da
cio' che non lo era**: il primo difetto aveva gia' un `EXPLAIN` e un precedente
da imitare, il secondo aveva solo due tracce, ed e' stato dato l'ordine di
misurarlo prima di riscriverlo. Le due tracce erano tutte e due sbagliate.

| route | prima | dopo |
|---|---|---|
| `GET /valves` | 15,6 s | **0,10 s** |
| `GET /machine/oee/series`, con l'`at` vero | 13,7 s | **0,69 s** |
| `GET /machine/oee/series`, senza `at` | 27,0 s | 3,3 s |

**`/valves`**: `DISTINCT ON` su `predictions` che percorreva 723.110 righe per
tenerne 35. Sostituito da un `LATERAL` su `generate_series(1, 35)`.

**`/machine/oee/series`**: la causa non era una query lenta ma **739
interrogazioni per richiesta**, di cui 8,0 s su 9,0 in attesa di rete. Tre
ripetizioni: contatore costruito per finestra invece che per richiesta, con
`shift` e `day` che rileggevano gli stessi bordi; storia OMAC riletta 358 volte
per finestra su una tabella di 300 righe; ogni ora di bordo letta due volte
invece di ricavarne la seconda meta' per differenza dal riepilogo, che sono
interi e non un'approssimazione. Interrogazioni dopo: **12**, indipendenti dal
numero di punti.

Verificato da me e non solo dichiarato: tempi rimisurati alla terza chiamata,
MACCHINA guardata in un browser vero a 1536x770 (si disegna per intero in tre
secondi, contro la ventina di prima, con i quattro guasti giusti nella fascia
degli allarmi), file toccati confrontati con quelli dichiarati, e la suite fatta
girare per intero: **539 passed**, uscita 0.

Toccati solo `pipeline/api.py`, `pipeline/tests/test_api.py`,
`pipeline/tests/test_oee.py`. Nessuna pagina della dashboard, nessun indice
nuovo nel database.

Resta aperto in `OPEN_QUESTIONS.md`: i 3,3 s della serie senza `at`, causa non
isolata, e il pavimento di 0,7 s con `at`, irriducibile senza un riepilogo a
grana di minuto.

### Trovato per strada

Due difetti registrati in `OPEN_QUESTIONS.md`, nessuno dei due riparato qui:

- **`/machine/oee/series` a 27,0 s e `/valves` a 15,6 s** sull'API vera. MACCHINA
  resta vuota una ventina di secondi e poi si riempie tutta insieme; nessun
  errore in console. Le due route non hanno ricevuto il riepilogo orario che il
  20 agosto ha sistemato le altre.
- La contraddizione fra le due fixture dell'OEE e' su **tre** scenari, non due, e
  sono i tre di guasto.

---

## 2026-08-20 — Sessantacinque giorni di storico, il riepilogo orario, e la pagina TEMPO scelta dall'utente

### Contesto

Le tre pagine accettate (`MACCHINA · VALVOLE · OEE`) sono state fatte girare
sull'API vera tramite un proxy (`.scratch/dashboard-v7/server_api.py`), e su
richiesta dell'utente il database e' stato popolato con un run storico di 60
giorni. Il suo primo commento davanti a quei dati:

> «avere i 60 giorni e' abbastanza inutile dati che non abbiamo un date picker.
> non posso vedere l'andamento dell'oee nel tempo, magari mese scorso.»

Aveva ragione su un fatto misurabile: `SERIES_SPAN_MAX` tagliava la serie a **48
ore**. Dei 60 giorni caricati, l'interfaccia ne mostrava due.

### Added

**`cycle_rollup_hour` — il riepilogo orario dei cicli** (`pipeline/cycle_rollup.py`).
Una riga per `(run_id, bucket_ts, valve_id)` con `total` e `good`: **34.090 righe
contro 36,2 milioni di cicli**. CLI con riempimento incrementale e idempotente
(`ON CONFLICT DO UPDATE`).

Il punto delicato, risolto: **le finestre dell'OEE non sono allineate all'ora**
(l'`at` corrente e' `19:29:35`). Sommare secchielli orari darebbe un numero
sbagliato ai bordi. La forma adottata e' **ore intere dal riepilogo + i due bordi
parziali letti direttamente da `cycles`**: ogni bordo e' al massimo un'ora, e il
risultato resta esatto. L'arrotondamento all'ora piu' vicina e' stato escluso per
contratto — sarebbe un numero plausibile e falso.

Verificata l'identita' numerica su dieci finestre (allineata, disallineata di
secondi, dentro un solo secchiello, a cavallo di due, con ore vuote, che sborda
inizio e fine del run, su un altro run), piu' un controllo indipendente del
coordinatore su una settimana scelta a caso: **4.229.570 / 3.270.119 dal
riepilogo, identici alla lettura diretta**.

**`GET /valves/quality/series`** — la qualita' per singola valvola nel tempo,
grane `hour|day|week`, `from`/`to` obbligatori e restituiti allineati ai bordi
effettivi. Non esisteva alcun modo di leggere quel dato: `/machine/oee/series`
porta solo i totali di macchina e `quality_detail.per_valve` resta `null` nella
serie. 20-40 ms sui 60 giorni interi.

**Finestre `week` e `month`** sull'OEE, `SERIES_SPAN_MAX` alzato a 60 giorni,
parametri espliciti `from`/`to` sulla serie.

**Tre varianti della navigazione nel tempo**, `ta/`, `tb/`, `tc/`, costruite col
metodo gia' adottato: pacchetto identico su disco (`PACCHETTO-tempo.md`), stesso
modello, stesso effort, unica variabile il principio di navigazione, consegnate
come indirizzi cliccabili. **L'utente ha scelto `tb/`** — la striscia dei due
mesi sempre in vista, con la finestra trascinabile.

### Fixed

**Il contatore a secchielli troncava in silenzio.** `_CycleCountsBucketed`
assumeva senza imporlo che i bordi della finestra cadessero su bordi di
secchiello; quando non era vero **troncava la finestra e restituiva un numero
piu' piccolo, senza dichiararlo**. Alimentava la serie che la pagina MACCHINA
gia' disegnava. Difetto preesistente, ora chiuso con una guardia sui due resti e
il ritorno alla lettura diretta.

**Il passo della serie troncava invece di diradarsi.** Sessanta giorni a passo
2 h davano 200 punti, cioe' gli ultimi 16,6 giorni: gli altri 43 sparivano senza
un segno. Ora il passo si moltiplica e `__meta.passo` dichiara quale ha risposto.

**Il proxy scavalcava il periodo scelto.** Iniettava `at` su `machine/oee` e
`machine/oee/series` sempre; ora salta l'iniezione quando la richiesta porta gia'
`from` o `to`, altrimenti un periodo scelto da un calendario verrebbe sostituito
dall'istante del proxy.

**`work_mem` di PostgreSQL era al default di 4 MB** e mandava su disco le query
pesanti (lavoro della notte precedente). Con la memoria di lavoro alzata e un
indice coprente dichiarato nello schema: baseline 138 s → 0,11 s, OEE giornaliero
142 s → 0,47 s, `/valves` 458 s → 4,3 s.

### Measured

| richiesta | prima | dopo |
|---|---|---|
| `/machine/oee?window=day` | 1,35 s | **0,20 s** |
| serie `day` su 48 h | 2,7 s | **0,37 s** |
| serie `day` sui 60 giorni | non esprimibile (tetto 48 h); 147 s forzando il vecchio meccanismo | **0,6 s** |
| `/valves/quality/series` sui 60 giorni | non esisteva | **20-40 ms** |
| aggregato giornaliero sui 60 giorni in SQL puro | 53,4 s | — |

Suite: da 215 a **513 test verdi**.

### Scoperto guardando i dati

**L'OEE di macchina sui 60 giorni e' quasi piatto**: da 0,504 a 0,473, con la
disponibilita' costante a 0,64 e la prestazione a 0,997-1,000 per costruzione. Il
movimento vero sta sotto la media delle 35 valvole, e i tre guasti scritti nello
scenario hanno **tre forme diverse**:

| valvola | giugno | luglio | agosto |
|---|---|---|---|
| 8 (restrizione) | 0,804 → 0,549 | **0,000** | 0,000 |
| 21 (ritardo apertura) | 0,60 | 0,60 → 0,51 | 0,51 |
| 30 (sensore di portata) | 0,80 | 0,80 | 0,80 → 0,39 → **0,000** |
| 1 (sana, per confronto) | 0,815 | 0,815 | 0,817 |

Il **quarto** guasto dello scenario — instabilita' di pressione sulle valvole
13-18 — sulla qualita' **non lascia traccia**: la valvola 13 resta a 0,80 per due
mesi. Non e' stato nascosto ne' compensato: dice che quel guasto va cercato
altrove che nella qualita', ed e' rimasto visibile nelle tre varianti.


## 2026-08-19 (3) — Backend: la catena operativa gira davvero, dati veri nel DB

### Changed

**Il database operazionale era vuoto perche' la catena di popolamento non era
mai stata eseguita.** Causa radice accertata: la directory `data/` non esisteva
affatto — il consumer di ingest (`pipeline/ingest.py`, solo-MQTT) non era mai
girato, quindi non c'era alcun raw Parquet a monte. Da li' a cascata:
`CyclesStorage.init()` mai chiamato su `plcsim` (ciclo di vita standalone) e
`Storage.init()` mai rieseguito dopo l'aggiunta di `machine_state_history` allo
schema. Nessuna migrazione rotta: passi mai eseguiti.

**Due moduli nuovi chiudono il buco.** `pipeline/raw_replay.py` scrive il raw
canonico in `data/raw` da un run bulk, importando `FLATTENED_COLUMNS`,
`COLUMN_TYPES`, `partition_of` e `records_to_df` da `ingest.py` invece di
riscriverli; `pipeline/state_history_backfill.py` ricava le transizioni OMAC
dagli eventi `STATE:` di `events.parquet`. Con `data/raw` popolato,
`cycles_backfill`, `features` e `inference` funzionano **senza modifiche**.

Rotta scelta: **popolamento offline dai run congelati in `work/`**, non un nuovo
run realtime. Motivo: le fixture-oracolo vengono da run specifici, e un run nuovo
avrebbe reso privo di significato il confronto campo per campo.

Stato di `plcsim` (da `work/m4_demo_dropout_1d`, scenario `b-guasto-singolo`):
`cycles` 603.664 · `predictions` 12.060 · `alerts` 6 · `alert_transitions` 334 ·
`machine_state_history` 5. L'ML arriva completo: 35 valvole su 35 con
`last_prediction`, la 13 a `flowmeter_dropout` score 1.0 con due alert
`sustained`, 33 sane.

### Fixed

**Tre difetti del backend, trovati confrontando l'API vera con le fixture.**

1. **`prediction_ts` era l'orologio di parete.** `predict_frame` non passava
   `prediction_ts` e `prediction_schema.py` ripiegava su `now_utc_iso()`: 12.060
   record entro 55 secondi di orologio di esecuzione contro 15 h di dati.
   `alert.py` ereditava il difetto su TUTTI i suoi timestamp. Conseguenza sulla
   dashboard accettata: `etaDato` fa `if (!(secondi >= 0)) return null`, quindi
   l'indicatore dell'eta' del dato **spariva** invece di leggere zero. Corretto
   datando ogni prediction con l'`event_ts` del ciclo che chiude la finestra;
   `event_ts` assente -> record saltato con warning, mai `now()`.
2. **`n_cycles_above` incrementato alla chiusura di un alert** (`alert.py`, ramo
   `_CLOSED`): una chiusura avviene per definizione SOTTO soglia. +1 sistematico
   su ogni alert chiuso. Il ramo `_SUSTAINED` era ed e' corretto.
3. **Formato ISO incoerente dentro la stessa risposta**: `kpi_series` tornava
   `datetime` grezzi (serializzati con `Z`) mentre le altre route usano `_iso()`
   (`+00:00`). Uniformato su `+00:00` al confine di presentazione, senza toccare
   il contratto di `cycles_storage`.

Dopo le correzioni, riverifica indipendente: **835 timestamp confrontati uno a
uno, 835/835 identici alla fixture**; 0 campi discordanti su 54 sui sei alert;
1508 timestamp con `+00:00` e zero con `Z`.

### Added

- **`GET /machine/oee/series`** — la dashboard accettata la chiamava e non
  esisteva. Costruita come ciclo su `_compute_oee_window`, quindi **un solo punto
  di verita'**; protetta da `test_serie_punto_identico_a_machine_oee`, che
  confronta il dizionario INTERO contro `/machine/oee` allo stesso `at`.
- **`GET /alerts/history`** — anch'essa chiamata e mancante. Serve la tabella
  `alerts` intera, e serve solo le righe persistite.
- **`window=hour`** sull'OEE. Non `15min`: sotto la mezz'ora la qualita' e' solo
  rumore binomiale (SNR 1,11 a 1 h contro 1,03 a 15 min).
- **Qualita' per valvola sulla finestra dell'OEE** (`quality_detail.worst_valve*`,
  e `per_valve` solo con `?per_valve=1` per non rompere l'identita' serie/punto).
  Misurato: la disaggregazione **non costa una scansione in piu'** (161,9 -> 122,6
  ms di media, perche' i totali di macchina sono ora la somma delle righe).
- **Numeri che rendono leggibili gli XmR** su `/valves/baseline`:
  `n_cicli_di_riferimento` = 46, `sigma_media_46` misurata empiricamente (mai
  derivata con radice di n), `sigma_full`, `xmr_note`. Piu'
  `n_cicli_per_valvola` (mediana, con min e max accanto), che `a/pagina.js` cita
  in chiaro e che mancava: la pagina mostrava un trattino.

### Measured

- **Non c'e' nessuna deriva lenta.** L'apertura fuori limite degli XmR (293/400
  su valvole sane) e' causata da un'**oscillazione deterministica di periodo 46
  cicli** (`driver_period_rot`, `plcsim/config.py`): FFT una riga sola a 46,0
  cicli = 49,7% della varianza, varianza a periodi oltre 400 cicli = 0,000,
  correlazione valvola 1 / valvola 13 = -0,894. Cambia il rimedio: contro una
  deriva si ricalcola la baseline su finestra, contro un'oscillazione no.
- **Il rimedio `mean +- 3 sigma` non discrimina**: 0 fuori-limite su 86.340x35
  cicli sani, ma anche 0,00% su una valvola con media spostata di 2,37 sigma.
  Separa su una run su quattro e inverte la classifica su `m6_global_diffusa_1d`.
- **La sigma della media non scala con radice di n** su questo segnale: a n=10 la
  regola `MRbar` sbaglia di 23x in senso anticonservativo. La nota in
  `BASELINE-PROPOSTA.md` che lo afferma e' quantitativamente sbagliata.
- **La qualita' di macchina non puo' mostrare un problema di valvola a nessuna
  finestra**: la valvola 13 a qualita' 0,000 per 15 ore muove la Q di macchina di
  0,0071. E' aritmetica (1/35 = 2,9%), non durata della finestra.
- **Due fixture si contraddicono**: `machine-oee-day.json` dice `theoretical`
  604366,9 e `machine-oee-series.json`, stesso istante e stesso generatore, dice
  604378,9 — che e' il valore dell'API. Ha ragione la fixture-serie
  (`generate.py:206` calcola dal `running_h` non arrotondato).
- **`alert-history.json` contiene 21 righe fantasma** (`n_cycles_above` 0,
  `max_score_seen` 0,0, timestamp nulli): lo script iterava lo stato interno del
  motore invece di cio' che il motore ha emesso. Le 6 righe vere coincidono col
  database, `alert_id` di lineage compreso.

### Verified

Suite completa: **433 test, tutti verdi** (da 396 a inizio sessione). `plcsim`
non e' mai stato scritto dai test. Confronto finale route per route contro le
fixture: **zero divergenze di categoria "problema del backend"**, 3 di categoria
"assunzione della fixture", 5 di perimetro.

### Next

`HANDOFF-api-vera.md` — far girare le tre pagine accettate sull'API vera tramite
un proxy, senza toccarle. E' l'unico passo che trasforma la dashboard da demo su
dati congelati a prodotto, e nessuno ha ancora mai guardato quelle pagine su dati
reali.

## 2026-08-19 (2) — Dashboard v7 COMPLETA: tre pagine, tutte accettate

### Changed

**La dashboard e' finita e accettata dall'utente.** Verdetto finale dopo averle
usate collegate fra loro: *"si mi piace e funzionano bene. oee mi va bene cosi
come e'"*. E' il primo esito positivo del progetto dopo sei versioni respinte.

Tre pagine, non quattro: `MACCHINA · VALVOLE · OEE`, in
`.scratch/dashboard-v7/` (`python .scratch/dashboard-v7/server.py`, porta 8077).

- **MACCHINA** (`/a/`) — come sta andando. Accettata per prima, dopo quattro giri
  di correzioni.
- **VALVOLE** (`/v1/`) — quale valvola apro. La **giostra**: le 35 valvole nelle
  loro posizioni fisiche sul carosello. Scelta fra tre varianti indipendenti.
- **OEE** (`/oee/`) — perche' l'OEE e' quello che e'. Una **cascata del tempo**:
  24 h che scendono a gradini fino alle ore che hanno prodotto pezzi buoni.

**La pagina VALVOLA e' stata cancellata dal piano.** Motivo misurato, non
stilistico: il dettaglio di una valvola contiene 400 cicli che coprono **22
minuti** su 24 ore, e non esistono altre route. Sarebbe stato il pannello
disegnato piu' grande. La domanda che l'ha fatta cadere e' dell'utente: *"se
clicco su una valvola posso vedere i suoi grafici, quindi la pagina dopo, chiamata
valvola, per cosa e'?"*. Era nel piano da sei versioni senza che nessuno la
verificasse.

### Il metodo, che e' la vera differenza rispetto alle sei versioni precedenti

Ogni pagina strutturalmente nuova e' stata **costruita in tre varianti
indipendenti** (stesso pacchetto su disco, nomi neutri, stesso tier, unica
variabile il principio organizzativo), consegnata come **URL cliccabili** e non
come immagini, e scelta dall'utente prima di qualunque altro lavoro. Poi corretta
a giri stretti sul suo feedback.

Dalla seconda pagina in poi la grammatica non e' piu' stata in discussione:
`LESSICO.md` + `comune/lessico.css` sono estratti dalla pagina approvata, e le
varianti successive li ereditano invece di reinventarli.

### Scoperte fatte misurando, che nessuna versione precedente aveva

- **L'OEE ribaltato ha una causa piu' profonda del previsto.** Non solo le run di
  guasto non si fermano mai: negli scenari `b/c/d` il tempo **pianificato** e'
  15,5 h su 24, quindi **8,5 h (il 35% della giornata) non entrano nel
  denominatore**. La pagina OEE lo mostra come un blocco che cambia colonna fra
  gli scenari.
- **`close_reason` distingue un guasto da una deviazione statistica.** La valvola
  13 di `b` chiude a `encoder_limit` 400 volte su 400 (la sana: `target` 400 su
  400): non raggiunge mai il target, il riempimento e' troncato dalla geometria
  della giostra. Da qui il suo tempo costante a 2130 ms, che non e' regolarita'
  ma troncamento.
- **In `d-deriva-diffusa` la mediana non si sposta** (|z| <= 0,21 su tutte e 35):
  cambia la **dispersione**, con il 10-13% dei cicli fuori banda su 30 valvole.
  Una dashboard che confronta solo medie non vede quello scenario.
- **Nessuna grandezza singola copre i sei scenari.** Tre costruttori indipendenti
  l'hanno misurato: lo scarto della media trova le nove valvole di `c` ma e' cieco
  su `d`; lo scarto di qualita' copre `d` ma trova due delle nove di `c`; le
  valvole 4, 7 e 8 deviano **solo** sul tempo di coda. Fondere in un indice unico
  avrebbe ricreato il punteggio di anomalia che l'utente ha respinto.
- **Il "26 valvole utili su 35" non distingue le valvole**: tutte e 35 riempiono.
  Il 26 e' il numero massimo di passi di rotazione (`filling_step_out` non supera
  mai 26; ogni `encoder_limit` cade li').
- **Il viewport reale dell'utente e' 1536x770 px CSS**, non 1920x1080: monitor
  1920x1080 con scalatura Windows al 125%. Tutte le verifiche precedenti erano
  fatte alla misura sbagliata. Da qui in avanti si valida a 1536x770.

### Decisioni prese in questa fase

- **Il colore solo dove c'e' gravita'**, con intensita' graduale sotto il
  riferimento e zona morta contro il rumore (prestazione e qualita', 0,5 punti).
- **Una macchina non in marcia non produce rosso.** Con queste route un fermo
  voluto e un guasto non sono distinguibili, quindi la tinta sarebbe un verdetto
  non ricavabile: il numero resta, il colore no. Applicato in OEE (disponibilita'
  di turno) e in MACCHINA (OEE di turno).
- **La discrepanza fra due indicatori onesti non va eliminata ma indirizzata.**
  L'utente ha scelto la candela proprio perche' puo' contraddire l'allarme: *"la
  candela mi fa fare domande… mi da' un'idea di cosa chiedermi"*. Il suggerimento
  mostra tutte le grandezze, cosi' il disaccordo restringe il campo invece di
  lasciare un dubbio.
- Corretto nel lessico condiviso un testo con contrasto **1,41:1** (illeggibile)
  sul fondo scuro del suggerimento: ora 4,99:1 e 6,48:1 in entrambi i temi.

### Remaining

**Sulla dashboard** (segnalati all'utente, non risolti):
- `d-deriva-diffusa` su VALVOLE ha **4** elementi colorati contro il massimo di 3.
- Il pannello valvola **ripete** in parte il suggerimento; andrebbe alleggerito
  lasciandogli cio' che solo lui mostra (la traiettoria nel tempo).
- Il riferimento del gauge OEE resta `?rif=sano`; l'alternativa `?rif=oggi` e'
  ancora raggiungibile e mai scelta.

**Sul backend** — quattro richieste emerse costruendo, tutte fuori dalla dashboard:
1. **Transizioni di stato con i loro istanti**: senza, la timeline della giornata
   non e' ricostruibile (oggi ci sono solo i totali per stato).
2. **I limiti XmR di `/valves/baseline` non valgono sul singolo ciclo**: una
   valvola sana risulta fuori limite 293 volte su 400. Da correggere o da
   documentare come limiti di media.
3. **Una finestra OEE piu' corta** (es. un'ora scorrevole): con `shift`/`day` le
   tre componenti nel tempo sono piatte per costruzione (punti a 2 h su medie a
   24 h condividono il 92% dei dati).
4. **Cicli storici per valvola oltre i 400 attuali**, se si vuole una pagina
   VALVOLA con una storia vera.

**Sui dati**: rigenerare le sei fixture su profili di fermata confrontabili, per
togliere l'OEE ribaltato alla radice invece che spiegarlo.

## 2026-08-19 — Dashboard v7: la pagina MACCHINA e' ACCETTATA dall'utente

### Changed

**Prima accettazione di una schermata in sette tentativi.** Verdetto testuale
dell'utente dopo averla usata nel browser: *"okay, la pagina di questa dashboard
adesso mi piace"*. Le sei versioni precedenti erano state tutte respinte al primo
contatto pratico.

**Fase 1 — dati completati prima di qualunque UI** (vincolo posto dall'utente):
- Serie temporale OEE per tutti e sei gli scenari (`machine-oee-series.json`),
  costruita camminando `at` all'indietro sulla route reale. Un worker v6 aveva
  dichiarato la serie infattibile e si sbagliava.
- Storico allarmi completo, transizioni vere e pareto per tipo di guasto e per
  valvola (`alert-history.json`, `alert-transitions.json`, `alert-pareto.json`).
- Predizioni ed alert estesi a `e-macchina-ferma` e `f-oee-degradato`, che non
  erano mai passati dalla pipeline: erano a 0/35 valvole con `last_prediction`.
- Corretti due OEE impossibili nelle fixture (192% e 176%): `align_oee.py` aveva
  riallineato il livello superiore lasciando `prev.oee` al vecchio target.
- Esposta la nona route `GET /valves/baseline` sul server delle fixture. Due
  costruttori su tre l'avevano cercata e non trovata: e' una dipendenza
  strutturale, non un dettaglio.

**Fase 2 — la pagina MACCHINA costruita tre volte e scelta dall'utente:**
- Server unico (`.scratch/dashboard-v7/server.py`, porta 8077) che rispecchia le
  route reali leggendo le fixture congelate, con switch scenario in querystring.
- Pacchetto di briefing scritto su disco (`PACCHETTO-comune.md`) invece che
  duplicato nei prompt, cosi' le tre varianti hanno ricevuto istruzioni
  letteralmente identiche e il confronto regge.
- Tre versioni con nomi neutri, stesso tier, unica variabile il principio
  organizzativo: A grammatica della piattaforma di supervisione esistente, B scatter valvole della tesi di riferimento, C layout BI
  convenzionale. Consegnate come tre URL cliccabili, non come screenshot.
- **L'utente ha scelto A**, poi tre giri di revisione guidati dal suo feedback.

### Le revisioni chieste dall'utente su A

1. Eta' del dato da quadrante a riga di testo, che si accende solo nel caso che
   conta: macchina in marcia e dati fermi. La soglia dei 15 minuti era inventata;
   sostituita con 5 minuti, derivati dallo sfasamento reale delle 35 valvole
   (89-157 s).
2. Barre OEE turno verticali (precedente a sinistra, corrente a destra), prese
   dalla versione B: *"segue il tempo come un grafico con una linea"*.
3. **Meno colore.** Passando da una pagina sobria a una colorata sullo scenario
   di deriva: *"e' come essere colpito"*. Da 11 elementi colorati a 3.
4. Dark mode.
5. Hover e affordance su ogni valvola, anche quelle sane; focus da tastiera.
6. Click su valvola -> pannello nella stessa pagina, senza costruire una seconda
   pagina.
7. Il gauge si colora quando e' sotto il riferimento, con intensita' graduale: la
   tolleranza esisteva nel codice ma non nell'immagine.
8. Carta di controllo rifatta: 400 marcatori di allarme sovrapposti coprivano la
   linea dei dati. La traiettoria non va mai coperta ne' schiacciata contro un
   bordo, nemmeno quando sta interamente fuori banda.
9. Hover informativo su tutti i grafici.

### Difetti dei dati trovati e lasciati visibili

- **L'OEE e' ribaltato**: gli scenari di guasto hanno OEE giorno *piu' alto* del
  sano (0,756 contro 0,504) perche' le run di guasto non si fermano mai
  (disponibilita' 99% contro 64%). Non e' un errore di calcolo: le sei fixture
  provengono da run con profili di fermata non confrontabili. **Noto all'utente,
  deliberatamente non aggirato.** Si risolve rigenerando gli scenari.
- **La serie OEE su finestra turno e' inutilizzabile** come andamento di salute:
  dente di sega da 0,79 a 0,000 ogni notte anche su macchina sana. Mai disegnata.
- **I limiti XmR di `/valves/baseline` non valgono sul singolo ciclo**: una valvola
  sana risultava fuori limite 293 volte su 400, perche' MRbar misura lo scarto fra
  cicli consecutivi (sigma ~9 ms) mentre la dispersione vera e' sette volte piu'
  larga (~70 ms). Da correggere lato backend.
- **La soglia di 600 ms sul tempo di coda non e' raggiungibile** su questi dati: il
  massimo osservato e' 451 ms. La soglia della tesi di riferimento non separa nulla qui.
- **La timeline degli stati non e' costruibile**: le route danno i totali per stato
  e il numero di transizioni, mai i loro istanti. Richiesta pulita per il backend.

### Perche' ha funzionato, dopo sei fallimenti

Il piano v7 e' stato eseguito alla lettera nella parte che non era mai stata
eseguita: **una pagina sola, costruita tre volte, consegnata come URL e non come
immagine, prima che esistesse qualunque seconda pagina.** L'utente ha corretto
la pagina quattro volte in poche ore invece di riceverla finita e respingerla.
Nessuna decisione strutturale e' stata presa su prosa o schizzi.

### Remaining

- Due decisioni aperte: la zona morta sulla prestazione, e quale riferimento usi
  il gauge OEE (`?rif=sano` contro `?rif=oggi`).
- Le pagine VALVOLE, VALVOLA e OEE non esistono: solo MACCHINA e' stata costruita.
- Rigenerare le fixture su profili di fermata confrontabili, per togliere di mezzo
  l'OEE ribaltato alla radice invece che nella presentazione.

## 2026-08-18 (9) — v6 rejected; root cause traced and folded into the rules

### Changed
- **v6 was rejected by the user** on first hands-on contact: *«la trovo incompresnsibile…
  perchè la prima pagina non è quella in cui vedo lo stato generale? non ci capisco niente
  di questa dashboard»*. Sixth consecutive rejection.
- Root cause traced from the raw session transcripts, not from the retrospectives:
  **v6 complied with almost every non-negotiable and failed anyway.** Six of the seven
  rules describe the artifact; the seventh describes the loop and is a prohibition, so
  nothing ever scheduled the user's first look. It landed ten hours in, when the user
  had to ask for it — *«come lo posso testare io?»* — and the rejection came 26 minutes
  later. The model opened 30 screenshots of the product; the user received one artifact
  and one URL.
- Four contributing causes recorded: proxies grew to fill the space where the user
  wasn't; a proxy (the blur test) was allowed to overrule a user decision (the carousel);
  consent given on ASCII sketches did not survive contact with the product; navigation
  was designed as a discovery and delivered as an instruction.
- **`CLAUDE.md` gained a "loop rules" block** — obligations with timing, not prohibitions:
  the user grades the first screen running before a second is built; structural decisions
  are shown as published alternatives, never described; an internal test never overrules a
  user decision; if it needs a reading guide it has failed; test orientation, not only
  legibility.
- **`HANDOFF-dashboard.md` gained §2b** (what v6 proved), failure mode **R6** (the user
  was never in the loop), two extra questions in the bar, four extra entries in "Do not",
  and a corrected delegation-cost section.
- Delegation cost guidance corrected against repriced data: `subagent_tokens` is the
  final context size, not cost; Sonnet was the *cheaper* arm on builds, not the expensive
  one; xhigh is cheap on short tasks; blind judging belongs at sonnet-low.
- Open question raised for the user, not yet answered: after six bespoke rejections and
  their own *«o posso farla con power bi?»*, whether a conventional BI layout should be
  attempt seven.

Evidence: `Claude Manager Vault/Research/dashboard-v6-root-cause.md`. Files snapshotted
before editing in `Archive/2026-08-18-pre-v6-lessons/`.

## 2026-08-18 (7) — Five reviews synthesised; two root decisions; contract v2

### Changed
- Synthesised all five independent contract reviews into
  `.scratch/dashboard-v6/review-contratto/SINTESI.md`. Consensus: no re-skin proposed
  by anyone; points A-D confirmed 5/5; the acceptance criterion judged honest 5/5
  (it failed twice and stopped the third patch cycle); its one defect, flagged by
  all five, is that expectations were sealed *after* the build.
- Discarded flash's visual channel wholesale on the user's instruction after two of
  two spot-checked visual claims proved false (a green that does not exist in the
  palette; a banner defect already fixed in `app.js:185-192`). flash delegates vision
  to `mimo` vision-workers. Its file-derived observations are retained.
- **Found the bridge between the two branches, already present in the routes and never
  used**: `GET /valves/baseline` returns `fill_quality_ok_rate` per valve (86,296 healthy
  cycles each), and `/valves/{id}/kpi` returns the same flag now. The difference is
  per-valve scrap excess — the same quantity that composes OEE Quality, decomposed.
- Established two facts from it: the machine's chronic 21.3% scrap is **concentrated**
  (v21 39.9%, v09 39.7%, the other 33 between 17.3% and 23.0%); and the largest valve's
  share of total excess classifies every scenario correctly (b 76.5% = one culprit,
  c 58.4% = two, d 7.1% = none, diffuse).
- Wrote `.scratch/dashboard-v6/DECISIONE-radici.md` — two model decisions: severity has
  two axes (prominence follows deviation from own declared baseline, globally, never
  distance from 100%); and the "case" object is a measure, not a screen.
- Sealed triage expectations **before** the build in `checks/attese-triage.md` with
  SHA-256, fixing the criterion defect all five reviews named.
- Built the triage screen twice from an identical frozen packet — worker-opus-low vs
  worker-opus-high, effort the only variable. Both converged independently on the same
  primary form (single bar: length = how much, segmentation = how concentrated). Both
  saw the carousel rejected by the blur test as a primary (3 and 5 mass centres on
  diffuse drift). Merged per user decision: low's framing (actionable subject + cans)
  with high's calibration (noise reference at 37 pt, frozen 3-sigma binomial threshold).
  Delivered `.scratch/dashboard-v6/triage/triage.html`, blur 6/6.
- Wrote `.scratch/dashboard-v6/PIANO-residuo.md` — full accounting of every review
  finding: 7 closed, 9 amendments needing only redaction, 5 criterion corrections,
  1 open user decision.
- Wrote **`.scratch/dashboard-v6/contract-v2.md`** (+ `.sha256`), superseding v1.
- Logged fan-out E in the Effort Ledger: high cost +77% tokens, +160% wall-clock over
  low, and bought method (calibration, frozen threshold), not composition.

### Decided
- **The carousel moves into VALVOLE as a physical localiser; the triage takes the place
  of the overview. Four screens, not five.** User decision, 2026-08-18, taken with the
  blur-test evidence in hand.

### Open
- Handoff card subtracted from `PRODUCT.md` — never confirmed by the user.
- AA contrast never measured numerically.
- In diffuse condition the case has no clickable destination.

## 2026-08-18 (6) — Two independent signed reviews of the v6 contract

### Changed
- Added Sol review `.scratch/dashboard-v6/review-contratto/review-01-gpt5.6-sol-high.md`
  (888 words, 11 images inspected, signed): discard the five-screen navigation as
  the product spine; retain its visual instruments inside a case-diagnostic flow.
- Added independent Terra review
  `.scratch/dashboard-v6/review-contratto/review-02-gpt5.6-terra-high.md`
  (897 words, 10 images inspected, signed): deep structural correction; the
  current routes cannot support the implied causal bridge from global quality
  loss to a valve over the same OEE window.
- Both reviews independently confirm the four gate findings, add the distinction
  between baseline-normal and production-acceptable, and classify the blind-image
  gate as an honest falsification mechanism but not technician acceptance.

### Remaining
- Compare the two reviews with the user and decide the dashboard architecture.
- Do not open another implementation/fix cycle before that decision.

## 2026-08-18 (5) — Gate visivo FALLITO due volte; si torna al contratto. Handoff per review indipendenti

### Esito
Il gate di accettazione della dashboard e' stato eseguito due volte con giudici ciechi
(6 al primo giro, uno per tier; 3 al ri-voto). **Fallito entrambe le volte.** Medie del
ri-voto: punto focale 3,5 · narrazione 3,8 · densita' 3,5 · fedelta' 3,0 · craft 4,0,
contro una soglia di 4. La cella obbligatoria (35 valvole su 35 in allarme) passa nella
sostanza — il giudice conclude «non penso a 35 valvole rotte» — ma non nel disegno.

Per la regola congelata nel contratto (§10.3) i due cicli di correzione sono esauriti:
**non si apre un terzo giro**. E' la regola che il progetto non ha mai rispettato nelle
cinque versioni precedenti, tutte patchate finche' il gate diventava verde.

### I quattro punti strutturali da decidere
1. Valvole e qualita'/OEE sono due rami che non si ricongiungono mai.
2. La gravita' e' invertita fra i due rami: una valvola urla in rosso, settantamila
   lattine scartate stanno in una barra grigia.
3. I grafici di dettaglio non hanno valori sull'asse e non sono calibrabili.
4. Il vocabolario non e' univoco: l'ambra ha due significati, i conteggi di valvole sono
   tre insiemi diversi chiamati allo stesso modo.

Piu' un fatto di dominio che complica tutto: la baseline sana ha ~21% di scarti, quindi
«tutte le valvole nella norma» e «21,3% di scarti» sono vere insieme e la dashboard non
le riconcilia.

### Prossimo passo — NON e' costruire
`.scratch/dashboard-v6/HANDOFF-review-contratto.md` e' l'handoff per revisori
indipendenti (modelli diversi). Ogni review va in
`.scratch/dashboard-v6/review-contratto/` come `review-<NN>-<modello>-<effort>.md`.
Le review vanno scritte SENZA leggere le precedenti. Una sessione successiva le
analizzera' e portera' i punti strutturali all'utente in forma decisionale.

### Stato del prototipo
Cinque schermate costruite e funzionanti su dati reali in `.scratch/dashboard-v6/proto/`
(server: `python -m http.server 4300` dalla cartella `dashboard-v6`). Trenta immagini
del risultato in `checks/celle-v2/`. Sei scenari coperti: sana, guasto singolo,
multi-valvola, deriva diffusa, macchina ferma, OEE degradato.

### Report di delega
`Claude Manager Vault/Retrospectives/opus-high/2026-08-18-dashboard-tentativo-6.md`;
20 righe appese all'Effort Ledger (6-25).

## 2026-08-18 (4) — Le cinque schermate esistono; scenario diffuso generato; blur test superato

### Changed
- Costruite S2 (`view-valvole.js`) e S3 (`view-valvola.js`); S4/S5
  (`view-oee.js`, `view-oee-dettaglio.js`) delegate e integrate. Tutte registrate
  nel router. CSS dei grafici in `styles-plot.css`, foglio separato per corsia di
  scrittura (due autori sullo stesso file e' un incidente gia' avvenuto in v3).
- **Scenario deriva diffusa generato**: `scenarios/m6_global_diffusa.yaml`
  (instabilita' pressione globale, severita' massima) -> `work/m6_global_diffusa_1d`.
  La run esistente `m3_global` dava 10 valvole su 35, una sotto la soglia di 11:
  si e' cambiato lo SCENARIO, non il criterio. La nuova run da' **18/35** al
  criterio congelato e **35/35 in allarme** dopo la catena prediction->alert.
  I sei scenari formano ora la scala 0 / 1 / 9 / 35 / stale / degradato.
- `fixtures/generate.py` accetta uno o piu' slug per rigenerare solo quelli:
  rigenerare tutto avrebbe sovrascritto gli alert e gli anomaly_score veri.

### Scelte di progetto
- **S2, asse normalizzato**: lo scostamento e' espresso in MULTIPLI DEL LIMITE
  congelato, non in valori assoluti. In valori assoluti servirebbero 35 bande
  diverse. Conseguenza dichiarata: la "banda piu' larga per i segnali rumorosi"
  prevista dal contratto e' gia' dentro l'asse (la tolleranza maggiore sta al
  denominatore); resta il marcatore tratteggiato a portare l'incertezza.
- **S3, il modello parla per ultimo**: l'ipotesi della prediction vive dentro il
  disclosure "Perche'?", sotto le prove osservate. L'ambito suggerito si deriva dai
  fatti (`sample_valid`, `sequence_ok`, natura del segnale), mai dall'etichetta del
  modello, con quattro esiti incluso "evidenza insufficiente".
- **Coerenza dello stato diffuso**: sopra soglia l'anello smette di evidenziare
  individui, sparisce il marcatore di apice, la legenda diventa una voce sola e la
  banda allarme passa a un messaggio collettivo. Indicare una valvola mentre si dice
  "la causa e' comune" manderebbe il tecnico a smontare l'unica cosa che quasi
  certamente non e' il problema.
- **S4**: il primario e' il terzetto A/P/Q, non la cifra OEE. La Performance
  inchiodata a ~1,0 e' dichiarata in una riga dentro il primario, non nascosta.

### Validation
- Blur test (contratto §3.1) eseguito su tutte e cinque le schermate: un solo
  centro di massa per schermata. Su S4 l'occhio cade sul blocco di perdita della
  Qualita', che e' il fattore realmente basso.
- Scenario sano: 0 falsi positivi. Multi-valvola: 9 corrette, separate per natura
  del segnale (6 affidabili, 3 solo rumorosi). Diffuso: nessun individuo indicato.
- S3 ridotta da 6 a 4 regioni in pagina comprimendo le affordance secondarie in
  una riga: con il telaio si sta esattamente al tetto di sei.
- `scrollWidth == clientWidth`, zero elementi oltre il bordo, zero errori console.

### Remaining
Loop di accettazione (5 schermate x 6 scenari, risposte attese sigillate, giudice
fresco, tutti i tentativi registrati); poi il verdetto dell'utente, che e' l'unico
che accetta. Restano non verificati: resa in tema chiaro, larghezze strette.

## 2026-08-18 (3) — Decisioni prese: target OEE provisionato, database di test bonificati

### Changed
- `edge/scripts/provision_speed_target.py` (nuovo): deriva la portata nominale
  dalla geometria dell'impianto (35 valvole x 3600 s / `rotation_ms`) e la
  persiste nel KV `speed_target`. Eseguito: 39.375 cph scritto e riletto.
  Sostituisce il writer che in un impianto reale sarebbe il PLC via ingest.
- `pipeline/api.py`: protetta anche la lettura di `machine_state_history`
  nell'OEE — su un'installazione parziale sollevava un 500 invece di degradare,
  asimmetria rispetto alla lettura dei cicli che era gia' protetta.
- `pipeline/tests/conftest.py` (nuovo): `drop_db_if_ephemeral` con guardia
  stretta; `test_api.py`, `test_oee.py`, `test_baseline.py` lo chiamano a fine
  sessione.
- `edge/scripts/cleanup_test_databases.py` (nuovo): bonifica dell'arretrato,
  dry-run di default. Eseguito con `--apply`: 49 database rimossi, 0 falliti.
- Fixture OEE riallineate al target provisionato (`fixtures/align_oee.py`).

### Validation
- `pytest pipeline/tests/` su PostgreSQL reale: **144 passed**, prima e dopo la
  bonifica.
- Nessun residuo nuovo dopo una suite completa (52 database prima, 52 dopo).
- Avvio del container Postgres: da ~150 s a **3 s**; occupazione da 484 MB a 43 MB.
- Dashboard verificata in browser: OEE 78,7% sano, 76,7% con guasti, 16,6% a
  riposo con stato OMAC letterale e valori dichiarati non in tempo reale.

### Remaining
Schermate S2-S5; scenario deriva diffusa; `speed_by_status` incoerente col tag
`SpeedActual` (minore, tocca il contratto OPC UA); il database operazionale
`plcsim` e' vuoto (nessuna tabella `cycles`) e va popolato prima della demo.

## 2026-08-18 (2) — I tre difetti risolti: OEE onesto, catena ML popolata, route baseline aggiunta

### Changed
- `pipeline/api.py`: `_oee_speed_target` ora ritorna anche la PROVENIENZA del
  target; `/machine/oee` espone `performance_detail.speed_target_source` e
  `ratio_osservato`, e degrada con reason quando un target non verificato
  produce un rapporto oltre `PERFORMANCE_RATIO_IMPLAUSIBILE = 1.25` invece di
  fabbricare un OEE (osservato 194%). Sovravelocita' normale non degrada.
- `pipeline/api.py`: nuova route **`GET /valves/baseline`** — riferimento sano
  per valvola (media, sigma, p50, MRbar, UCL/LCL XmR, tasso qualita', tasso
  SUSPECT), SQL con LAG per valvola, finestra DICHIARATA mai dedotta, degrado
  esplicito invece di 404. Registrata PRIMA di `/valves/{valve_id}`, altrimenti
  "baseline" verrebbe letta come valve_id.
- `pipeline/tests/test_baseline.py`: 6 test nuovi. `pipeline/tests/test_oee.py`:
  2 test nuovi sul degrado del target e sulla sovravelocita' normale.
- `.scratch/dashboard-v6/fixtures/predict.py`: esegue davvero la catena
  prediction -> alert sulle run reali con il modello addestrato; fixture di
  a-sana, b-guasto-singolo e c-multi-valvola ora contengono alert e
  `anomaly_score` VERI. `align_oee.py`: allinea le fixture OEE al contratto nuovo.
- Dashboard: la classificazione ora COMBINA alert engine e scostamento KPI
  invece di lasciar vincere l'alert (produceva 9 cerchi rossi identici, nessun
  punto focale); la valvola con lo scostamento maggiore riceve un marcatore di
  apice; la banda allarme punta al caso peggiore, non al piu' longevo.

### Validation
- `pytest pipeline/tests/` su PostgreSQL reale: **142 passed** prima dei test
  nuovi, **13 passed** su test_oee dopo, **6 passed** su test_baseline.
- Formula della baseline confrontata con un'implementazione indipendente:
  scostamento massimo **0,004%**.
- Rapporto OEE misurato su tutte le fixture: **2,537-2,540**, coerente con la
  cadenza misurata di 3,200 s per valvola.
- Dashboard verificata in browser reale: run sana 0 falsi positivi, run con
  guasti 9/35 corrette e separate per natura del segnale, OEE dichiarato
  degradato invece di 194,9%.

### Remaining
Schermate S2-S5; scenario deriva diffusa; riconciliazione `rotation_ms` vs
`Speed_Target` (decisione umana, vedi OPEN_QUESTIONS); 50 database di test
residui da rimuovere se l'utente lo autorizza.

## 2026-08-18 — Dashboard tentativo 6: piano, review a 5 tier, contratto congelato, panoramica costruita; tre difetti reali trovati nel backend

### Changed
- Nuovo piano dashboard `.scratch/dashboard-v6/plan.md`, sottoposto a un fan-out di
  cinque reviewer con lenti diverse su tier diversi (opus low/medium/high, sonnet
  low/medium). Valutazione delle critiche in `reviews/grading.md`, revisioni in
  `plan-revisions.md`.
- Contratto di design `.scratch/dashboard-v6/contract.md`, congelato PRIMA del build:
  sistema visivo, budget di elementi verificabile col blur test, navigazione completa
  (banda allarme persistente, ritorno, stato nell'hash), stati OMAC letterali,
  regola affidabilita' -> prominenza, protocollo di accettazione.
- Fixture reali `.scratch/dashboard-v6/fixtures/`: 5 scenari su 6 dalla forma esatta
  delle 8 route, generate da parquet reali; 35 serie KPI per scenario. Il generatore si
  e' rifiutato di fabbricare alert, anomaly_score e lo scenario di deriva diffusa.
- Criterio di deviazione congelato in `.scratch/dashboard-v6/checks/criterio.md` dopo una
  calibration run su 8 run sane indipendenti (560 misure): soglia 5.0 sul rapporto
  |media - baseline| / (3*sigma/sqrt(n)), sigma = MRbar/1.128, n = 200.
  0 falsi positivi su 560 misure sane, 0 falsi negativi sui 9 guasti.
- Prototipo `.scratch/dashboard-v6/proto/`: telaio, adapter, derive e schermata
  panoramica (giostra a 35 valvole) verificati in browser reale su scenario sano e
  multi-valvola. Le altre quattro schermate non sono ancora costruite.
- Decisioni utente: forma panoramica = giostra; quinta schermata ridisegnata sui dati
  reali; sottrazioni a PRODUCT.md a discrezione del pianificatore (lenti e badge fuori,
  handoff ridotto a un pulsante, epistemica a una riga).

### Difetti trovati nel progetto (non nella dashboard)
1. **OEE fuori scala.** `plcsim/config.py:48` `rotation_ms = 3200` implica ~39.375
   lattine/ora, ma `pipeline/api.py:106` fissa `DEFAULT_SPEED_TARGET_CPH = 15500`.
   `/machine/oee` restituisce Performance ~254% e OEE ~194% su run sane. Il
   `Speed_Target=15500` del glossario non e' lattine/ora; l'API lo tratta come tale.
2. **Catena prediction -> alert vuota.** Nessun `anomaly_score` reale esiste per le run
   in `work/`, quindi nessuna prediction, quindi `alerts` vuoto in ogni scenario.
3. **Nessuna baseline esposta.** Verificato che lo scostamento NON e' rilevabile dentro
   la singola serie KPI a nessuna ampiezza consentita dall'API (fino a 5000 cicli): le
   popolazioni deviante e sana si sovrappongono interamente, perche' il confronto
   recente-vs-precedente misura la derivata del degrado, non il livello. Con le sole 8
   route la dashboard non puo' dire onestamente quali valvole richiedono attenzione.
   Specifica della route mancante in `fixtures/BASELINE-PROPOSTA.md`.

### Errori del pianificatore corretti dalle review
- "9 valvole in zona morta visivamente morte": falso. `active_valves = 26` e' l'estensione
  angolare della zona utile, non un sottoinsieme di valvole; tutte e 35 riempiono
  (~17.000 cicli ciascuna). Errore gia' trovato e corretto nel ciclo v4 e reintrodotto.
- Regola affidabilita' -> prominenza sull'asse sbagliato (incertezza del segnale invece di
  sospetto della valvola) e con encoding inerte (costante su tutti i punti).
- "1 critica · 7 warning": la tabella `alerts` non ha campo severita'.
- Stati OMAC ridotti a RUNNING/STOPPED.
- Criterio di deviazione basato su `2.66*MRbar`: e' il limite del punto singolo, ~14 volte
  troppo largo per una media di 200 cicli.

### Remaining
Schermate S2-S5 da costruire; scenario `d-deriva-diffusa` richiede una run YAML dedicata;
loop di accettazione (5 schermate x 6 scenari, giudice fresco, tutti i tentativi
registrati); i tre difetti sopra sono da decidere con l'utente.

## 2026-08-18 — Dashboard builds deleted by user decision; clean restart from preserved user data

### Changed
- Deleted permanently (none of it git-tracked): the v4 "Minimale Estremo"
  dashboard (`.scratch/dashboard-v4/` — prototype, checks/gates/shots,
  persona reviews, generator scripts, plan-v1.1→v1.8 + contract-v1.8), the
  older dashboard leftovers (`.impeccable/`, `shots/`, `.scratch/crops/`,
  `.scratch/vision-crops/`, `c1-valve-detail-1920.png`), and three
  old-dashboard handoffs (restart mandate 2026-08-16, parallel-blind
  2026-08-16, p7-luna-verdict). Two stale `python -m http.server` processes
  (ports 4173/4187) still serving the prototype were terminated first.
- Preserved as planning input for the next agent: `Proposte/` (user's raw
  data and own plan ideas), `.scratch/dashboard-v4-research/` (r1–r4),
  `.scratch/tmp_extract/` (raw article extracts), `feedback/`, `risposte/`,
  `.scratch/m10/spec.md`.

### Why
User wants to start the dashboard over; the raw input data and "what we
planned" (in `Proposte/`) must survive so a fresh agent can gather the data
and produce a new plan on its own, without anchoring to the deleted v4 plan.

### Remaining
New dashboard planning pass from the preserved inputs, then build; binding
constraints unchanged (read-only observation API only; never simulator or
ground truth). All previous dashboard gate verdicts are history.

## 2026-08-17 — Dashboard responsive hardening (F1): extreme-narrow proxy overflow fixed, 63-cell matrix clean

### Changed
- Fixed the F1 residual of the final review (page-level horizontal overflow
  at the extreme-narrow layout proxy, measured in a real browser). Root
  cause: intrinsic-min-width elements at extreme layout — nowrap `.chip`
  pills ("tutte le valvole raggiungibili" L1 right 288, "filling_time_ms ·
  affidabile" L2 right 272, "machine/state 200" L3 right 182.6) and the
  native trace select `#trace-trigger` (min-content 196) in the trace
  toolbar; the L1/L3 data tables were already contained in `.table-scroll`
  (`overflow:auto`) regions and did not contribute (measured `unclipped=0`).
- Minimal fix — CSS only + truthful labels, no `overflow-x:hidden` hack, no
  change to the existing 980/620 breakpoints (390px behavior unchanged):
  new explicit `@media (max-width: 340px)` in `styles.css` (threshold
  measured: overflow starts ≤200px viewport; 340 covers 195=390@200% and
  160=320@200% with metric margin for font fallback) —
  `.chip { white-space: normal; overflow-wrap: anywhere; }`;
  `.row-btns select { min-width: 0; max-width: 100%; flex: 1 1 120px; }`
  (native ellipsis, selects stack on own rows, `#trace-trigger` 134×44 at
  195px, keyboard-operable); `overflow-wrap: anywhere` on
  `.invar-text/.scope-reason/.panel-hint/.view-sub/.ledger-layer .layer-body`
  for unbreakable-token prose at ≤180px. `js/views.mjs`: table-scroll
  labels now truthfully say "verticalmente e, se necessario,
  ORIZZONTALMENTE" where tables gain internal horizontal scroll at ≤200px
  (L1 exceptions, L3 score/alert/chiusure) plus a new label on the L3
  provenance table — the only horizontal-scroll regions are labeled data
  tables (contract G4).
- Post-fix real-browser measurements (Chromium, same routes, cache-busting):
  195px proxy (=390px @ 200%) `scrollWidth == clientWidth` on L0/L1/L2/L3
  (was L2 272>180, L1 288>180, L3 182>180); 160px sweep clean over
  scen-a/b/e/f × both lenses (145/145 samples) with one documented
  1px transient (L0-scen-f @160 during the rolling animation, not
  reproduced on 8 consecutive samples) and 0 unclipped elements per sweep;
  63-cell matrix (9 routes × 7 widths 1315/657/390/320/195/180/160) all
  `sw == cw` with `unclipped == 0` except that single transient.
- Real Ctrl++ zoom: attempted — inert in headless (clientWidth and
  devicePixelRatio unchanged after Ctrl+ ×2); `documentElement.style.zoom`
  non-representative and not used as a verdict. The 195px layout proxy is
  the exact mathematical layout equivalent of 390px @ 200% and is now
  clean; the contract cell stays honest `[nr]` for real zoom with the
  instrumented layout equivalent verified.
- Validation after the fix: `node checks/validate.mjs` 253/253 (exit 0,
  build root and repo root), `node --check` clean on all 8 `.mjs`;
  browser console Total 0 (Errors 0, Warnings 0); resources at 195px
  L1 27 / L2 29 / L3 42 — all local fixtures, 0 HTTP ≥400, 0 external
  hosts. Screenshots regenerated for the affected L1/L3 desktop+narrow
  full-page shots incl. the updated scroll labels (G7); non-affected shots
  untouched. Evidence: `.scratch/dashboard-restart-build/checks/browser-evidence.md`
  sez. G (G1–G8) with the M2 200%-zoom row updated.

### Why
The final review required the 200%-zoom extreme-narrow reflow cell to be
resolved or explicitly waived; headless Playwright-MCP cannot drive real
browser zoom, so the bounded pass fixed the actual layout overflow at the
exact layout proxy (195px = 390px @ 200%) and kept the real-zoom cell
honestly unclaimed.

### Remaining
Optional residuals only (all disclosed, non-blocking): real Ctrl++ zoom
instrumentation or an explicit contract waiver (sole `[nr]` cell), the one
documented non-reproducible 1px transient, loading/skeleton capture (`[nc]`),
no pre-fix `styles.css` snapshot retained for detector diffs (keep the
snapshot habit in future passes), and the documented screenshot timestamp
caveat (~8-minute side-tab skew, sub-4px geometry). Then real read-only API
integration and the stakeholder demo dry-run.

## 2026-08-16/17 — Dashboard restart executed: Signal Bench prototype built, SHIP-WITH-FIXES, finish artifacts written

### Changed
- Executed the restart mandate end-to-end after independent plan review: new
  visual world **Signal Bench / Trace & Trigger** (impeccable grounded
  candidate 3, direction seed `cb343391`) — case-led instrument bench, matte
  near-black light-on-dark with no theme toggle, phosphor observed trace,
  amber trigger/uncertainty, etched graticule confined to the trace canvas.
  Plan and contract locked BEFORE build in `.scratch/dashboard-restart/`
  (`plan.md`, `contract.md`, `plan-review.md`, `benchmark.md`); contract
  required a ≤150-word first `<body>` comment carrying seed `cb343391` and
  six labels (validated by the checker).
- Built the static prototype at `.scratch/dashboard-restart-build/`
  (index.html, styles.css, `js/{main,router,state,adapter,trace,views}.mjs`,
  fixture generator + validator under `checks/`): L0 Base / L1 Impianto /
  L2 Caso / L3 Evidenza via hash navigation with alert deep-links and query
  state (lens/window/setup/stato/modalita/tb/trigger/cursore); six
  endpoint-shaped fixture scenarios A–F (healthy, local,
  automation/data-coherence, data-quality, global/ambiguous, recovery);
  read-only adapter mirroring the FastAPI observation API (catalog exactly
  1..35, 21-field KPI series, `null`/`404`/`501`/degraded preserved,
  DEMO·fixture marking, no ground truth or hidden scenario labels in
  payloads); two lenses with invariance verified at URL/DOM/geometry level
  (lens screenshots differ on 206/3626 pixel rows only).
- Gates and evidence: `node checks/validate.mjs` **253/253** (exit 0, repo
  and build root); `node --check` on all 8 `.mjs`; real-browser pass
  (Playwright Chromium, served on 4173): **0 console errors/warnings**, 26
  local fixture requests, zero external hosts, desktop 1315×948 and narrow
  390×844, footer at document end, no horizontal clipping at 390,
  keyboard/focus-visible (amber 2px outline), reduced-motion emulation
  honored, touch targets ≥44px, API-error toggle and setup switching
  verified. Evidence: `.scratch/dashboard-restart-build/checks/browser-evidence.md`
  + shots under `checks/shots/`.
- Impeccable CSS detector run ONCE (2026-08-17): single `side-tab` finding
  fixed in `styles.css` only (`.state-box` + `.layer-* h4` → 1px lateral
  border + 2px top accent; 0 lateral borders >1px after); receipt
  `checks/side-tab-fix-receipt.md`. Copy fixes: `valvolae→valvola(e)` and
  `riga/e→righe` helper; `calcolO` documented as intentional CSS uppercase
  (non-issue).
- Final review (2026-08-17,
  `.scratch/dashboard-restart-reviews/post-fix-final-review.md`):
  **SHIP-WITH-FIXES — no blockers**; all gate-blocking truth, epistemic,
  lens, a11y and evidence-honesty checks pass.
- Finish artifacts (post-review, per contract): root `DESIGN.md` describing
  the shipped world + sidecar `.impeccable/design.json` (schemaVersion 2);
  receipt `.scratch/dashboard-restart-reviews/design-doc-receipt.md`.

### Why
The M10 visual gate restarted from scratch after the user rejected the old
P&ID world as too complex/illegible; the new case-led Signal Bench world is
the fresh, readable design built from the mandate, with the old world as
anti-reference only.

### Remaining
Disclosed residuals (no clean zero-risk claim): (1) real Ctrl++ 200% zoom
stays `[nr]` (headless zoom inert) — the extreme-narrow overflow itself is
RESOLVED by the F1 fix (2026-08-17, next entry): the 195px proxy (=390px @
200%) now measures `sw == cw` on L0–L3 (was L2 `272 > 180`, L1 `288 > 180`,
L3 `182 > 180`); (2) newest screenshots predate the side-tab CSS fix by ~8
min (sub-4px border geometry, caveat documented; the F1 pass re-captured
the affected L1/L3 shots); (3) loading/skeleton state never captured
(`[nc]`, exists in code); (4) no pre-fix `styles.css` snapshot retained for
the detector diff; (5) one documented non-reproducible 1px transient in the
160px sweep (63-cell matrix clean except it). Optional finish lane:
real-zoom instrumentation or explicit waiver, loading capture. Then real
read-only API integration and the stakeholder demo dry-run.

## 2026-08-16 — Dashboard restart: DESIGN.md deleted, mandate handoff written

### Changed
- Deleted `DESIGN.md` (root): the old visual system (P&ID world, v2, v3) is
  explicitly discarded as too complex and illegible; no replacement written
  now — the next session chooses a new design from scratch.
- Verified `.scratch/dashboard/`, `.scratch/dashboard-iteration-v2/`,
  `.scratch/dashboard-iteration-v3/` are not present in the worktree (their
  files were never git-tracked).
- Wrote the restart mandate for the next Luna root manager:
  `.scratch/handoffs/plc-sim-dashboard-restart-2026-08-16.md` — context under
  220k tokens, orchestration-only manager, delegated research/build/review,
  impeccable used read-only as the working skill (routing/new-work), new
  visual world chosen autonomously with contract saved before build,
  independent plan review before implementation, browser/screenshot evidence,
  product truths preserved; no implementation/plan/benchmark analysis now.
- Updated `.project/*` so `DESIGN.md` is no longer registered as the active
  system and the new handoff is the pointer for the dashboard work.

### Why

The user rejected the old design (too complex, illegible); the next session
starts the dashboard visual work from a clean slate, with the old visual
world as anti-reference only.

### Remaining

- Next session executes the mandate: plan after reading skills and material,
  independent plan review, then orchestrated design/build/review.

## 2026-08-16 — Dashboard v3 redesign minimal + gate G6 COMPLETE

### Changed
- Registered the user's minimal feedback ("la voglio minimal": too many lines,
  too much/small text, too few charts) as spec + issues in
  `.scratch/dashboard-iteration-v3/`; deep analysis of the benchmark material
  (7 articles + ~30 real OEE dashboard screenshots), 4 persona reviews
  (C1–C4), converged into contract v3.1 (charter vincolante).
- Delegated the implementation to a flash-coordinator branch (P0 pre-flight
  G5 baseline → P6): anti-frame, typography floor 11px, hero unico (L0
  fact:oee 28–44px), ring v3 (r10/14/18, label 12u, cap 330, fill pieno alert
  + flag solo unique-most-severe), hub senza testo + barra 35 celle, L0
  declutter (riga problema, inbox minimo, trend turno onesto), L2 chart-first
  (testata 1 riga, hero FT, risposta rapida, XmR come strato, default-open
  testata+risposta+IMP+STP), L1 lista dietro disclosure. Redesign =
  PRESENTAZIONE only: fixture/derive/data-layer/validator mai toccati.
- Gate G6 COMPLETE: redesign-v3 34/34 · smoke 8/8 · journeys 57/57 · criteria
  45/45 · invariants 29/32 (I-7-B/C/D preesistenti, firma P0) · antibug
  268/355 (87 ab1-console favicon-only nominali, firma P0) · usability-v2
  14/14 · shots 33/33 (light+dark 1920/390 incl. closed e readout) · derive
  74/74 · node --check 25/25 · 0 errori console reali · fixture immutate
  (sha256 scenario-B/oee-shift.json `8e17104d…dbb04`). Matrice delta M01–M22
  pre-scritta; decisione manager M20: cap parole L2 80→110 (misura onesta 104).
- Incidente recuperato: worker orfano v2 aveva sovrascritto parte
  di `js/views/l0-home.js`; terminato e band-1/band-3 head ricostruiti per
  receipt P2/P4 (`checks/v3-recovery-l0.md`), suite ri-verificate.
- DESIGN.md (root) aggiornato con registro decay/rest in un'unica fase;
  README del prototipo lista `redesign-v3.mjs`. I 4 TODO issues del v3 sono
  risolti (ready-for-agent → resolved).

### Why

User feedback 2026-08-15: the dashboard was "terribile" in presentation — a
redesign (not an improvement) was requested; v3 cuts frames/text and puts
charts first while keeping the clean-line blueprint vibe and all data
contracts intact.

### Remaining

- User acceptance of v3 and manual blind-tester criteria; then real API
  integration. Residual non-blocking notes in `v3-branch-report.md` §5.

## 2026-08-15 — Dashboard v2 iteration + P7 polish (user navigation feedback)

### Changed
- Registered the user's v2 feedback as spec + issues in `.scratch/dashboard-iteration-v2/`.
- Ran a vision inventory of G4 screenshots and 4 persona reviews (novice operator, expert maintainer, dashboard designer, HMI accessibility) in `.scratch/dashboard/iter-v2/review-cards/`; converged into contract-v2.
- Delegated v2 implementation to a flash-coordinator branch (6-7 phases): dark blueprint theme + C/S/N toggle, L0 focal point (problem row, hub, 700px ring, readout, disclosure), hover vocabulary, L2 details/summary disclosure + deep-link + 13-field handoff, separation and a11y fixes; gate G5 green (smoke 8, journeys 57, criteria 45+AC-D6/7, antibug 355 with only the known injected favicon failing, usability-v2 14, shots 29 incl. dark, derive 74).
- Closing visual review (luna): SHIP-CON-RISERVE → polish branch P7 resolved all three reservations (problem row became a CTA, readout/legend upgraded from micro-meta scale, L2 header breath) → final verdict SHIP, DESIGN.md updated, memory files updated by worker.

### Why
User's main pain: "non so a cosa guardare" plus navigation weakness; v2 makes the first glance answer "is there a problem, where, how severe" before anything else.

### Remaining
- Blind-tester manual criteria and user acceptance of v2; then real API integration.

# Recent work

## 2026-08-14 — First M10 dashboard version (P&ID prototype)

### Changed

- Built a navigable prototype in `.scratch/dashboard/prototype/`: L0–L3 with 2
  lenses, scenarios A–F from fixtures faithful to the real API, a 12-field
  handoff sheet, and deep-linking.
- Gate G1 fixture/validator green: 74 unit tests DER-1..8. Gate G2 QA: journeys
  56/56, criteria 43/43, anti-bug checks. Gate G3 review: Luna fallback vision
  for the crash, review team of 3 reviewers: 0 blockers, 27 findings all
  resolved. Gate G4 re-validation post-P7: suites clean.
- Impeccable finish pass: verdict ship-with-fixes, `DESIGN.md` written, surface
  brief created.
- 6 manual criteria PENDING (blind tester); user choice: testing in a future
  session.

### Budget/credits note

- Higgsfield: 5 sketches round 1 + 3 sketches round 2 (z_image) + 3 comp
  flux_2; ~1.25 residual credits, not significant.

## 2026-08-14 — Bootstrap shared project memory

### Changed

- Initialized a local Git repository on `main`; there was no prior Git history or
  remote.
- Added the project-memory workflow to `AGENTS.md`.
- Added `.project/STATE.md`, `.project/DECISIONS.md`,
  `.project/OPEN_QUESTIONS.md`, and `.project/RECENT_WORK.md`.
- Captured the current M10/dashboard state, active architectural decisions,
  publication boundary, known limitations, and next priorities from source,
  specs, handoffs, ADRs, and tests present in the working directory.
- Created the private GitHub repository `RazAndAlex/PLC-Sim-V`, configured
  `origin`, and pushed `main` with upstream tracking.

### Why

Provide a small, durable, Git-backed context surface for a GitHub-connected
ChatGPT reader without implicitly publishing the large untracked project tree,
generated data, screenshots, local database state, or private review evidence.

### Validation

- Confirmed the project root and the absence of previous commits and remotes.
- Cross-checked the memory against `CONTEXT.md`, the repository agent guidance,
  dashboard/M8–M10 specs and handoffs, ADRs 0001–0021, representative source
  entry points, dependency pins, and current file timestamps.
- A targeted OEE/storage test command was attempted but did not collect tests:
  the active Python installation reported `No module named pytest`.
- Latest existing documented evidence, not rerun in this session: 252 simulator
  tests passed; 65 pipeline tests passed with one warning; M9 and M10 acceptance
  checks exited successfully on 2026-08-13. The newer OEE backend remains to be
  rerun in the locked environment.
- The initial memory commit `5ba964c` was pushed successfully to
  `origin/main`; repository visibility was verified as private.

### Remaining

- Validate and publish the small memory follow-up that records the canonical
  repository.
- Keep every unrelated untracked project file local unless the publication policy
  is explicitly broadened in a future request.

## 2026-08-19 — Le tre pagine accettate girano sull'API vera

### Cosa

Le tre pagine v7 (`a/` MACCHINA, `v1/` VALVOLE, `oee/` OEE), finora viste solo
su fixture congelate, sono state aperte in un browser **sui dati veri del
database**, senza modificare una riga della dashboard.

Aggiunto `.scratch/dashboard-v7/server_api.py`: gemello di `server.py`, serve
gli stessi file statici e traduce `/api/<scenario>/<route>` in `/<route>`
sull'API vera (`pipeline/api.py`, porta 8123). I due server sono
intercambiabili — fixture contro reale si confrontano cambiando la porta.
`server.py` non è stato toccato.

- 8077 fixture · 8078 API vera all'istante di fine run · 8079 API vera ad
  «adesso» vero (percorso dato vecchio).
- L'istante di osservazione è letto da `max(event_ts)` e **dichiarato in un log
  all'avvio**, mai cablato. Iniettato solo su `machine/oee` e
  `machine/oee/series`: l'età del dato si propaga da sé al resto della pagina.
- `/scenari` serve **una voce sola** — il database contiene un run solo.
- Il proxy inietta `limit=400` su `/valves/{id}/kpi`: l'API vera ha default 200,
  ma le legende delle pagine dicono 400. Senza, le legende mentirebbero.

### Esito

Tutte e tre le pagine si aprono e reggono. Il confronto campo per campo è in
`.scratch/backend-2026-08-19/DIFFERENZE-REALE-VS-FIXTURE.md`.

**Una sola differenza pesa**: `/valves/baseline` risponde `degraded`, quindi la
giostra delle 35 valvole — l'elemento primario della pagina VALVOLE — non si
disegna. Le pagine dichiarano l'assenza invece di inventare, ma il riferimento
di qualità sparisce. Causa a monte, non risolvibile nel proxy: la baseline
richiede un run sano che il database non può ospitare (`cycles` ha chiave
`(valve_id, cycle_id)` senza discriminante di run). Vedi OPEN_QUESTIONS.

## 2026-08-19 (pomeriggio) — Chiave di run e storico lungo

### Il difetto che teneva tutto invisibile

`pipeline/cycles_backfill.py` prometteva di respingere i doppioni su
`(valve_id, cycle_id)` ma usava `cycles.is_duplicated()` sull'intero frame, che
marca duplicata solo la riga identica su **tutte** le 21 colonne. Due run con la
stessa chiave e misure diverse — il caso reale — passavano il guard e finivano al
DB, dove `ON CONFLICT DO NOTHING` li scartava in silenzio abbassando
`rows_inserted` senza spiegare perché. Verificato in laboratorio, corretto,
coperto da un test nuovo. Una finzione di test che metteva due giorni con gli
stessi numeri di ciclo è stata corretta: era una collisione che la tabella non
può rappresentare.

### La chiave di run

`cycles` ha ora `run_id VARCHAR NOT NULL`, chiave primaria
`(run_id, valve_id, cycle_id)` e due indici — `(run_id, event_ts)` e
`(run_id, valve_id, cycle_id DESC)`, che prima non esistevano affatto: ogni
query faceva seq scan completo. La migrazione è idempotente e **applicata al DB
reale**: 603.664 righe conservate, zero NULL, 51 secondi, backup in
`.scratch/backup-2026-08-19/cycles_pre_runid.dump`.

`machine_state` porta la chiave KV `current_run_id`. `resolve_run_id()` risolve
esplicito → KV → unico run in tabella, e solleva `AmbiguousRunError` se restano
più candidati: senza filtro `DISTINCT ON (valve_id) ORDER BY cycle_id DESC` non
restituisce il ciclo più recente ma quello del run **più lungo**, cioè una
macchina sana mostrata al posto di quella guasta.

`pipeline/api.py` è run-aware su tutti i punti: `_count_cycles`, la query OEE,
`_first_cycle_ts`, `_sigma_media_sql`, `_baseline_sql`, `_baseline_window`
(ora `{run_id, start, end}`, retrocompatibile), `list_valves`, `valve_kpi`.
Ogni risposta dichiara il run che l'ha prodotta. Su run ambiguo: 200 degradato
con `reason`, coerente con il resto delle route, mai un 500.

Prova di non-contaminazione con controllo negativo: disattivando il filtro,
7 test su 10 falliscono. Suite: **203 verdi**.

### Lo storico di 60 giorni

`plcsim/run.py` accetta `--start` e `--end` (`--end now` fa terminare la run
adesso e ricava l'inizio all'indietro da `--days`): l'orologio era cablato al
2026-06-01. Sei test, compreso quello sui due orologi interni che devono
condividere l'ancoraggio.

`Telemetry.write()` era il punto di rottura nascosto: scaricava su disco durante
la corsa ma **rileggeva tutto in memoria** per il file finale. Su 60 giorni
sarebbe fallito dopo ore di generazione. Ora scrive in streaming
(`scan_parquet` → `sink_parquet`), collaudato su una run vera da un giorno.

`scenarios/storico_60d.yaml`: 12 giorni sani (finestra di riferimento, 207.228
cicli/valvola contro gli 86.296 delle fixture), poi degrado lento sulla valvola 8,
rottura improvvisa sulla 21, condizione diffusa sul gruppo 13-18, guasto di
sensore sulla 30 ancora in salita a fine run. **Il motore dei guasti non sa
riparare** (severità monotona, un fault per valvola): gli allarmi chiusi nello
storico saranno quelli spontanei, non riparazioni. Dichiarato nel file.

## 2026-08-21, percorso live Blocco A

Il mapping edge è ora generato dalle costanti OPC UA. Contiene 567 voci, con 7
tag macchina e 16 tag per 35 valvole. Il flow sottoscrive il mapping e usa un
watermark indipendente per ogni `ValveNN.LastCycleId`.

Il builder crea l'envelope dalla valvola che ha generato il trigger. Non usa
`Machine.DataReady` e non usa un fallback `cycleCounter`.

Le verifiche statiche sono verdi. `edge/tests/parity_check.py` ha 17 pass e 1
skip. Il generatore produce lo stesso `tag-mapping.js` presente nel repository.

La suite `pipeline/tests` ha dato **298 passed, 1 warning in 177,52 s**. È
un'evidenza separata e non sostituisce il requisito runtime del Blocco A.

La prova runtime è poi stata eseguita con Docker disponibile e deploy `full`
del flow tramite l'Admin API Node-RED. I log hanno confermato mapping 567,
subscription 567 e 35 trigger. Il tentativo 4 ha ricevuto e scritto 269 eventi,
con zero reject e backlog zero, ma dopo circa 30 secondi il server OPC UA ha
registrato la scadenza della subscription con `publish cycle count(31) >
lifetime count(30)` e `BadNoSubscription`. Per oltre due minuti il contatore è
rimasto fermo. I 269 eventi coprono le 35 valvole, con 7 o 8 eventi per
valvola, ma solo per 25,812 secondi. La prova continua di dieci minuti e la
copertura nella relativa finestra non sono quindi completate. Non sono stati
modificati database, core congelato, Blocco B o Blocco C.

## 2026-08-21, controprova Blocco A dopo restart Node-RED

Il solo container Node-RED è stato riavviato alle 22:47:12+02:00; dopo il
preflight dei log (mapping 567, 35 trigger, subscription 567, MQTT connesso)
non è stato eseguito alcun POST a `/flows`. La finestra raw successiva è durata
18 min 51,598 s, con 11.869 envelope per tutte le 35 valvole (339--340 per
valvola), nessun gap superiore a 10 s e massimo gap di 3,703 s. Il checkpoint
ha registrato 4.812 ricevuti e 4.812 scritti come conteggi cumulativi, zero
reject, duplicati, invalidi e reconnect; ritardo medio circa 1,74 ms.

Il controllo indipendente dei timestamp raw attraversa anche l'interruzione
del gestore: 1.575 record continui, 35 valvole e massimo gap 398 ms. La suite
edge è risultata `19 passed`; `pipeline/tests`, usando un basetemp locale per
evitare un PermissionError della temp utente, è risultata `298 passed`.
Il rapporto con le due uscite integrali di `misura_percorso.py` è
`.scratch/percorso-live/BLOCK-A-SUBSCRIPTION-RESTART-REPORT-20260821.md`.

## 2026-08-22 — Blocco B, checkpoint finestra live

- Riutilizzato il gate `GATE_PASS` prima del live.
- OPC UA PID `42088` e ingest in due sessioni controllate hanno ricevuto
  CmdStart. L'ingest ha registrato `written=1211` entro le 01:06:57 CEST.
- La finestra è stata chiusa per ordine del Terra manager. Nessun supervisor,
  backfill o inference è stato avviato. Dopo cleanup, 4840 e 4841 non avevano
  listener. Il PID ingest non era leggibile per `Accesso negato`, quindi non è
  stato fermato alcun processo ignoto.
- Dopo escalation autorizzata, Node-RED è stato riavviato e il container era
  `running`; `/health` ha risposto 404. La misura v21 post è uscita con codice
  0 e resta `7 su 150`, con le nove valvole allarmate immutate. Report:
  `.scratch/percorso-live/BLOCK-B-BATTITO-REPORT-20260822.md`.

## 2026-08-22, Blocco B, secondo tentativo

- Avviati con PID attribuibili il server OPC UA (42924), l'ingest (14388) e il
  supervisor (27556). CmdStart ha dato `Running=True`; la sola partizione live
  era `date=2026-08-21`.
- Il primo heartbeat ha fermato il supervisor: backfill exit 2 per 6.217
  duplicati su `(valve_id, cycle_id)`. L'inference non è stata eseguita.
- Fermati i soli PID avviati. Porte 4840/4841 e connessioni al broker pulite.
  Dopo restart Node-RED, v21 resta 7 su 150 con le nove valvole allarmate
  invariate. Nessun cambiamento a cycles, predictions o `current_run_id`.
- Esito: `BLOCKED_FOR_REASONING / NEEDS-REVIEW`. Report:
  `.scratch/percorso-live/BLOCK-B-BATTITO-REPORT-20260822.md`.

## 2026-08-22, Blocco B, terzo tentativo isolato

- Creata una root raw nuova e non preesistente:
  `.scratch/percorso-live/attempt3-20260822T013358/raw`.
- Il server OPC UA ha ricevuto CmdStart, ma l'ingest è terminato subito:
  `Start-Process` ha spezzato il percorso con spazio di `--out`.
- Non avviato il supervisor. Fermati solo i PID attribuibili; porte e broker
  puliti. Node-RED post-cleanup `healthy`; v21, nove allarmi e
  `current_run_id` invariati.
- Esito: `BLOCKED_FOR_REASONING / NEEDS-REVIEW`. Nessun quarto giro.

## 2026-08-22, Blocco B, quarta corsa live — autorizzata dall'utente

- Copia di sicurezza `plcsim_pre_run4_20260822.dump` (585.296.036 byte,
  `pg_restore --list` 51 voci) prima di accendere.
- Catena accesa nell'ordine: server OPC UA, ingest su `data/raw`, riavvio di
  Node-RED (567 sottoscrizioni, 35 trigger), CmdStart, supervisore.
- **Il battito funziona da raw a cycles.** `verifica_battito.py 10` ha visto
  +6.653 righe raw e +6.651 cycles; `predict` fermo a 723.110.
- L'incrementale non rilegge il pregresso: 1.432/1.432 · 161/1.593 · 693/2.286
  · 638/2.924 · 692/3.616. Primo battito 3.668 ms.
- Riavvio a metà corsa: supervisore B ha inserito 372 righe sulle 3.988 lette,
  esattamente le mancanti. Totale 8.886 righe su 8.886 chiavi distinte.
- Valvola 21 invariata a 7 su 150, nove allarmi immutati, `current_run_id`
  intatto. Nessuna predizione prodotta, quindi nulla poteva spingerla fuori.
- Unica modifica di prodotto: `pipeline/cycles_backfill.py:753`, argomenti
  `dates` e `db_url` invertiti nel log d'avvio.
- `pipeline/tests` → 307 passed.
- Rapporto: `.scratch/percorso-live/BLOCK-B-RUN4-REPORT-20260822.md`.

## 2026-08-22 — `predictions` riceve il discriminante di run

- Migrazione idempotente `Storage._migrate_predictions_run_id`, ricalcata su
  quella di `cycles` del 2026-08-19. Le 723.110 righe esistenti attribuite a
  `storico_60d`, verificato dal `window_end_cycle_id` massimo (1.036.100,
  coerente con 36.241.832 cicli su 35 valvole).
- `run_id` NON entra nel record wire: `prediction-v1.json` ha
  `additionalProperties: false`. È parametro di `insert_prediction`, come per
  `cycles`.
- Watermark (`existing_window_end_cycle_ids`) e cronologia allarmi
  (`load_score_history`) filtrano per run. Le rotte prediction dell'API
  filtrano sul run risolto e degradano senza filtro se il run è ambiguo.
- **Risultato: l'inference sul run live ha prodotto 140 record.** Prima erano
  zero e sarebbero rimasti zero.
- Storico invariato: 723.110 prediction, valvola 21 a 7 su 150, nove allarmi.
- `pipeline/tests` → 307 passed; `tests` + `edge` → 259 passed.
- Rapporto: `.scratch/percorso-live/RUN-ID-PREDICTIONS-REPORT-20260822.md`.

## 2026-08-22 — `alerts` per run, e due difetti trovati accendendo la catena

- `alerts` e `alert_transitions` hanno `run_id`; chiave unica
  `(run_id, valve_id, fault_type)`; `alert_id_for` include il run. Migrazione
  transazionale: 12 allarmi e 64.180 transizioni riscritte, zero orfane.
  Guardia a runtime rimossa, la separazione è nello schema.

- **DIFETTO 1 — il cursore incrementale perdeva righe in silenzio.** Il
  cursore era un high-water mark per valvola sul `cycle_id`, e dava per
  scontato che i cicli arrivassero in ordine. Non è vero: alla ripartenza di
  Node-RED la prima lettura della subscription consegna il valore CORRENTE di
  `LastCycleId`. Sulla valvola 1 è arrivato un `cycle_id` 274 prima della
  sequenza reale, ripartita da 4; il cursore ha preso 274 come soglia e ha
  scartato ogni ciclo autentico successivo. Il log diceva «backfill ok … 0
  righe inserite», che si legge come "niente di nuovo" e non come "ho buttato
  via 643 righe". Corretto spostando il cursore su `ingest_ts`, che è monotono
  per costruzione; confronto `>=` per non perdere le righe sul bordo del
  flush, con l'ON CONFLICT del writer che assorbe la sovrapposizione. Dopo la
  correzione un backfill ha recuperato **2.554 righe** che erano state perse.
  Test di regressione:
  `test_cursore_non_perde_i_cicli_arrivati_fuori_ordine`.

- **DIFETTO 2 — il supervisore non passava il run all'inference.** Il comando
  aveva `--dates` e `--raw` ma non `--run-id`, quindi l'inference ricadeva sul
  KV `current_run_id` (il run storico): watermark sbagliato, `prediction: 0
  record prodotti` a ogni battito mentre i cicli entravano regolarmente. E se
  avesse prodotto, avrebbe scritto sotto l'identità del run storico. Corretto
  in `SupervisorConfig.inference_command`, con l'asserzione aggiunta al test
  del supervisore.

- Dopo le due correzioni il battito produce da solo: 533 cicli incrementali e
  **105 prediction** in un singolo giro.
- `pipeline/tests` → 308 passed.

## 2026-08-22 — percorso live CHIUSO: i dati arrivano da soli fino alla diagnosi

Catena completa e autonoma:
`macchina → OPC UA → Node-RED → MQTT → raw → cycles → predizioni → allarmi`.

- **Criterio di accettazione superato**: `verifica_battito.py 10` a catena
  accesa, **uscita 0**, tutti e tre gli stadi in movimento (+6.814 raw,
  +6.281 cycles, +140 predizioni in dieci minuti).
- **Prova end-to-end con un guasto vero**: macchina avviata sana
  (`scenarios/m5_healthy.yaml`), guasto `restriction` severità 0.95 iniettato
  dal vivo via OPC UA sulla valvola 5. Visibile nei dati grezzi
  (`delta_pulse` medio 1957 contro ~9 delle altre), isolato dal modello alla
  prima finestra (punteggio 1,000 contro 0,431 della seconda valvola), e
  arrivato fino all'**allarme aperto al ciclo 700**, con una transizione
  registrata. I nove allarmi dello storico sono rimasti invariati e la
  valvola 21 a 7 su 150.
- **Riavvio a metà corsa**: fermato a 11.003 righe, il supervisore nuovo ne ha
  inserite 270 — esattamente le mancanti. Fine corsa 11.913 righe su 11.913
  chiavi distinte, zero duplicati.
- Suite `pipeline/tests` + `tests` + `edge` → **567 passed**.

**Terzo difetto trovato accendendo la catena: la coda del broker.** Dopo un
quarto d'ora il battito si è fermato su 158 duplicati `(valve_id, cycle_id)`
dentro UNA sola sessione del simulatore. Gli `event_ts` lo spiegano: quelle
righe venivano dalla sessione precedente, rimaste nella coda persistente MQTT
(QoS 1, `clean_session=False`, client id fisso `plcsim-ingest-v1`) e consegnate
all'ingest alla riconnessione; poi la macchina nuova raggiunge davvero quel
cycle_id e collide. La coda persistente esiste di proposito e non va tolta.

**Regola operativa che ne consegue**: un run live nuovo parte da una sessione
broker pulita — `--client-id` dedicato al run — come già parte da una
partizione nuova e da un Node-RED riavviato. Il guard sui duplicati ha fatto
il suo mestiere: si è fermato dicendo perché, invece di scegliere in silenzio.

Rapporto: `.scratch/percorso-live/PERCORSO-LIVE-CHIUSURA-20260822.md`.
Resta aperto solo il **Blocco C**, che è una scelta di prodotto.

---

## 2026-08-22, sera — ambiente riacceso e le due decisioni portate all'utente

**Riavvio.** API (8123) e proxy (8078) giravano con codice vecchio: l'API era su
dal 21/08 alle 14:20, il proxy dal 20/08 alle 16:01. Fermati e riaccesi tutti e
due. `GET /health` → `{"status":"ok","db":true}`. Le cinque pagine rispondono 200
e MACCHINA si disegna per intero a 1536x770.

**Difetto trovato e corretto: `/alerts` non accettava il run.** `_alerts_rows`
risolveva il run solo dal KV `current_run_id`, e la route non aveva il parametro
`run_id` che tutte le altre hanno dal 22/08. Puntando la dashboard su una corsa
diversa, ogni grafico seguiva la corsa chiesta e la sola fascia degli allarmi
restava ancorata al KV: al tecnico sarebbe apparsa una macchina diversa da quella
dei grafici accanto. Aggiunto `run_id` a `/alerts` e `/alerts/history`, con
default invariato. Prova: sulla corsa live la fascia passa da 9 allarmi dello
storico a 1, la valvola 5 iniettata dal vivo.

**Aggiunto `--run` al proxy** (`.scratch/dashboard-v7/server_api.py`): inoltra
`run_id` all'API e prende come istante di osservazione la fine di quella corsa.
Serve a mettere due corse a confronto **senza toccare il KV**, che è la decisione
dell'utente. Il KV resta su `storico_60d`.

**Misure del confronto**, dalle route vere:

| | `storico_60d` | `live_20260822_run7` |
|---|---|---|
| cicli | 36.241.832 | 17.978 |
| durata | 21 giu → 19 ago | 28 minuti |
| allarmi attivi | 9 | 1 (valvola 5) |
| punti in `machine/oee/series` | 179 | 1 |
| OEE turno | 73,9 % | non calcolabile |

**Buco di navigazione misurato**, e non risolto d'ufficio: ogni pagina rimanda
solo alle pagine nate prima di lei. MACCHINA → VALVOLE. VALVOLE → MACCHINA.
OEE → MACCHINA, VALVOLE. TEMPO → le tre precedenti. CARTA → le quattro
precedenti. Nessuno rimanda a CARTA.

Suite `pipeline/tests` + `tests` + `edge` dopo la modifica: **567 passed**, la
stessa base di stamattina.

Le due decisioni sono davanti all'utente come pagina con indirizzi cliccabili:
<https://claude.ai/code/artifact/3c94f521-9029-429d-b21f-8e16fa1907cb>

### Menu unico sulle cinque pagine, applicato e verificato a schermo

`collegaNav()` sta ora in `comune/dati.js`: riscrive l'indirizzo di ogni
collegamento dentro `.nav-voci` aggiungendo lo scenario corrente. Prima la stessa
cosa era scritta a mano in tre posti, nominava gli id uno per uno, e `pc/` e `k1/`
non la facevano affatto.

Tutti e cinque gli `index.html` portano lo stesso menu a cinque voci, nell'ordine
MACCHINA, VALVOLE, OEE, TEMPO, CARTA. La pagina corrente resta senza collegamento.
Spariti gli id vecchi `vai-valvole` e `vai-macchina` (restano solo in `v3/`, una
variante morta di VALVOLE, lasciata com'era).

**Un difetto che si è visto solo aprendo le pagine**: `a/stile.css` era l'unica
delle cinque senza la regola `.nav-voci li a{ color:inherit; text-decoration:none }`.
Con un collegamento solo il difetto passava quasi inosservato, con quattro il menu
di MACCHINA è comparso blu e sottolineato, con le voci già visitate in viola.
Regola spostata in `comune/lessico.css`, dove il menu condiviso ha il suo posto.

Verifica fatta a schermo, non solo con `curl`, a 1536x770 sui dati veri: tutte e
cinque le pagine si disegnano per intero e il menu è identico. Il giro da MACCHINA
a CARTA con un clic vero funziona. Il worker aveva dichiarato di non aver aperto
il browser, ed è esattamente lì che stava il difetto.

Nota, non corretta perché precedente a questo lavoro: i collegamenti portano
`?scn=a-sana`, cioè lo scenario predefinito del guscio a fixture, mentre il proxy
serve un solo scenario e lo ignora di proposito. Innocuo, ma adesso si vede
nell'indirizzo a ogni passaggio di pagina.

### La pulizia passa a una sessione nuova

Handoff in `.scratch/HANDOFF-pulizia.md`. Non è stata fatta qui perché
cancellare non si torna indietro e i nomi nel handoff precedente erano sbagliati:
le varianti scartate di TEMPO sono `pa/` e `pb/`, non `ta/` e `tc/`, che non sono
mai esistite. L'utente aveva approvato leggendo i nomi sbagliati.

Inventario misurato, tutto sotto `.scratch/dashboard-v7/`: da togliere `pa/`,
`pb/` (TEMPO), `v2/`, `v3/` (VALVOLE), `b/`, `c/` (MACCHINA), che nessuno nomina.
Restano `tb/`, nominata da `RECENT_WORK.md` e da tre `PACCHETTO-*.md`, e `k1o/`,
`k2/`, `k3/`, che `DECISIONS.md` dichiara di tenere.

Alla radice: `180` e `nul` sono file vuoti da errori di riga di comando, `e.g` è
un pezzo di prompt finito in una redirezione, `tmp_report.txt` è una versione
tronca di `work/m8_acceptance_report.md`, `_tmp_m9_repro/` contiene un solo
parquet. Segnalata anche `.pi-subagents/`, **331 MB**, più di tutto il resto
messo insieme: sono trascrizioni di run, cioè prove, e non si toccano senza
chiedere.

## 2026-08-23 — la pulizia, e la lentezza della serie OEE isolata

### La pulizia

Fatta con i nomi corretti, dopo che l'utente li ha confermati: l'approvazione
precedente era stata data leggendo `ta/` e `tc/`, che non sono mai esistite.
Tolte `pa/`, `pb/`, `v2/`, `v3/`, `b/`, `c/` da `.scratch/dashboard-v7/`, più
`180`, `nul`, `e.g`, `tmp_report.txt`, `_tmp_m9_repro/` alla radice e
`.playwright-mcp/`. `.pi-subagents/` resta per decisione dell'utente.

Prima di cancellare: verificato che **niente fosse tracciato da git** (nessuna
copia da cui tornare indietro), che nessun documento nominasse le sei cartelle,
e che le cinque pagine vive rimandassero solo a `a/`, `v1/`, `oee/`, `pc/`,
`k1/`, `comune/`. Verificato anche che `work/m8_acceptance_report.md` fosse
intatto prima di togliere `tmp_report.txt`, che ne è una copia tronca.

Dopo: cinque pagine aperte a 1536x770 sui dati veri, giro completo del menu con
clic veri, e suite a **567 passed** su due corse. Identica alla base.

### Un difetto misurato che non era dove sembrava

`GET /machine/oee/series` era registrato come «senza `at` costa 3,3 s, causa non
isolata». La causa è isolata e **non è il parametro**: è se l'istante cade su
un'ora esatta. Allineato costa 0,58 s, non allineato da 0,9 a 6,2 s. Senza `at`
la finestra finisce «adesso», che non è quasi mai allineato.

Il tempo è tutto in `_conta_bordi`: con `at` allineato quella fase costa 0,000 s
perché i bordi d'ora sono vuoti e non vengono chiesti; non allineato ne servono
circa 230, cioè ~1,75 milioni di tuple d'indice.

**Due strade percorse e scartate come cause**, perché è utile che non si
ripercorrano: l'indice di copertura esiste già e il piano è `Index Only Scan`
con `Heap Fetches: 0`; e l'indice non è gonfio, `pgstatindex` dà densità 90,05%
e frammentazione 0,08%.

**La lezione sul metodo**: la stessa chiamata ha dato 13,3 s, poi 5,3 s, poi
0,93 s nella stessa giornata, al variare del buffer di Postgres. Avevo riportato
«peggiorato da 3,3 a 5,3 s» prima di accorgermene, ed era falso. Su questa route
un tempo assoluto non vuol dire niente da solo: si confronta allineato contro
non allineato, nella stessa condizione di cache.

### Il residuo `?scn=a-sana`

`collegaNav` in `comune/dati.js` ora aggiunge il parametro solo se lo scenario
non è quello predefinito. Sul proxy dei dati veri gli indirizzi tornano puliti
(`/v1/`, `/oee/`, `/pc/`, `/k1/`); il guscio a fixture continua a portarsi
dietro uno scenario scelto. Verificato a schermo con il giro completo del menu,
non con `curl`.

### M11

L'utente ha accettato K=5/N=150 come definitiva. La domanda sulla latenza è
chiusa in `OPEN_QUESTIONS.md`. Di M11 resta la classificazione del modello sulla
valvola 21, che non è un problema di taratura.

## 2026-08-23, secondo turno — la ripulitura delle domande aperte

L'utente ha chiesto se restassero decisioni da prendere. Controllando una per
una invece di riportare i titoli, **non ne restava nessuna** — ma il file diceva
il contrario, e questo era il difetto vero.

### Il difetto: undici sezioni mentivano nel titolo

`OPEN_QUESTIONS.md` aveva undici sezioni intitolate «APERTA» o «Aperto» che
erano state chiuse nei giorni precedenti senza che nessuno le depennasse. Chi
leggeva il file rifaceva indagini già fatte. Ci sono cascato due volte in questa
sessione prima di accorgermene.

Ognuna è stata verificata **sulla cosa, non sull'etichetta**:

| sezione | come è stata verificata |
|---|---|
| cronologia allarmi senza `run_id` | `\d predictions` sullo schema: la colonna c'è, con due indici per run |
| `alerts` non distingue i run | `alerts` e `alert_transitions` hanno `run_id`, chiave unica `(run_id, valve_id, fault_type)` |
| confronto fra valvole «decisione non presa» | **era stata presa**: è la striscia «LE 35 SULLA STESSA SCALA» in TEMPO, scelta fra tre varianti, e `LESSICO.md` era già stato corretto per ammetterla |
| gate `validate.py` non passa | eseguito: `OK: nessun fallimento` sui sei scenari. Il difetto dei `GT_TOKENS` era già corretto con `GT_DEROGHE` |
| carta di controllo «non ancora costruita» | è la pagina CARTA `/k1/`, accettata il 22 agosto |
| OEE gonfiato a bordo finestra | l'API dichiara la finestra parziale, `pipeline/api.py:915` |
| percorso live mai esercitato | chiuso il 22 agosto con un guasto iniettato dal vivo |
| tre Blocco B e due gemelle | ognuna aveva già la propria «CHIUSA» più in basso: chi leggeva dall'alto incontrava prima quella sbagliata |

Nessun testo è stato cancellato: le sezioni portano ora un riquadro
**«SUPERATA — non rifare questa indagine»** con la prova. In testa al file c'è
la regola che mancava: chi chiude una voce marca la sezione, non scrive solo una
voce nuova più in basso.

### Gli avvisi della suite

Il progetto non aveva **nessun** file di configurazione di pytest, e i
marcatori `slow` e `opcua` erano usati senza essere dichiarati: tre dei quattro
avvisi erano pytest che sospettava un errore di battitura. Aggiunto `pytest.ini`
con le due dichiarazioni. Raccolta verificata prima e dopo: 567 e 567.

Il quarto avviso **resta di proposito**. È uno `StarletteDeprecationWarning` che
nasce in `fastapi/testclient.py`, cioè in una libreria: toglierlo vuol dire
installare `httpx2`, cioè modificare l'ambiente Python, che il 22 agosto è stato
deciso di non toccare. Nasconderlo con un filtro sarebbe peggio che tenerlo — è
l'unico segnale rimasto di una dipendenza che invecchia, ed è scritto dentro
`pytest.ini` perché il prossimo che lo vede non lo silenzi per comodità.

### Cosa non è stato fatto, e perché

I tre difetti dei generatori di fixture (`history_extract_ef.py`,
`predict.py::alert_rows`, `alert-history.json`) vivono solo dentro
`.scratch/dashboard-v6/fixtures/`, superata dal 22 agosto. Correggerli non
avrebbe effetto su niente di osservabile. Restano scritti, non corretti.

La deriva lunga settimane è stata riproposta e l'utente ha risposto di no per
adesso.

## 2026-08-23 — M11: il silenzio della valvola 21 è capito, e la correzione è stata scartata dalla verifica

Il lavoro è partito dall'ultima voce rimasta di M11: il modello dice `healthy` a
0,0029 sulla valvola 21, che lo scenario `storico_60d` guasta con un
`opening_delay` di severità 45 dal ciclo 431.725.

### Prima cosa misurata: il classificatore sbaglia una volta sola

Confronto fra `/valves/{id}/score` sull'API viva e `scenarios/storico_60d.yaml`:
valvola 8 `restriction` giusto, valvole 13-18 `pressure_instability` giuste,
valvola 30 `restriction` giusto. **Otto guasti su nove classificati bene.** La 21
è un caso isolato, non la punta di un problema diffuso.

### La causa vera, che completa la diagnosi del 21 agosto

Non è il normalizzatore. È il **set di addestramento**. Ogni finestra
`opening_delay` in `train.parquet` sta fra z 5,65 e 24,9 sulla famiglia
fillingtime; il guasto della valvola 21 atterra a z 3,81 / 1,95 / 0,00. La
severità 45 è dentro l'intervallo addestrato, ma in spazio **normalizzato** il
guasto è fuori dominio, e il confine del classificatore non ci arriva.

Il segnale nei dati grezzi c'è tutto: il 90,86% delle finestre dopo l'onset
supera il 99,9° percentile pre-onset della valvola stessa su `mean_fillingtime`.
Non manca niente in ingresso.

### Tre ipotesi abbattute con la misura

- **Normalizzazione robusta (mediana/MAD)**: non muove la valvola 21 (0,085 /
  663 / 5 contro 0,089 / 699 / 4) e distrugge il resto — argmax `restriction` da
  100% a **0%**, `flowmeter_dropout` da 29,9% a **0%**.
- **La guardia sigma=0 su `max_fillingtime`**: morta per un motivo fisico, non di
  taratura. Il valore grezzo della valvola 21 è esattamente 2130,00 con sd 0,00
  **sia prima sia dopo l'onset**. La feature non porta informazione su questa
  valvola, e nessun riscalamento la recupera. Sostituire il sigma azzerato con la
  mediana fra valvole ha cambiato i numeri di zero: 0,0892 / 699 / 4, identici.
- **Il taglio del sigma senza riaddestrare**: fa dire `opening_delay` con
  sicurezza, al prezzo del **53,6% di falsi positivi sulla finestra sana della
  valvola stessa** e 4.941 allarmi sani sulle 35. La base larga della 21 è rumore
  vero, non un artefatto di normalizzazione.

Chiarita anche la cancellazione che era rimasta senza spiegazione:
`mean_pulsecount` e `mean_deltapulse` sono anticorrelate a r = -1,0000 con pesi
uguali e opposti, quindi la coppia conta due volte la stessa grandezza. Colonne
duplicate così ce ne sono nove. Ma lo stesso segno compare sulla valvola
portatrice dell'addestramento: è un difetto di igiene, non la causa del silenzio.

### La correzione che funzionava sullo scenario, e che la verifica ha bocciato

L'aumento del set di addestramento con copie attenuate delle finestre guaste
(stesso vettore z moltiplicato per k < 1, stessa etichetta) sposta il confine
verso il basso e **funziona sui 60 giorni**. Due punti di lavoro misurati:

| | ultima finestra v21 | FP pre-onset v21 | finestre sane sulle 35 | argmax giusto post-onset |
|---|---|---|---|---|
| modello spedito | 0,0029 `healthy` | 0,24% | 411 | 4 / 11.962 |
| R12 | 0,857 **`opening_delay`** | 1,79% | 828 | 8.976 |
| R24 | 0,333 `healthy` | **0,24%** | **287** | 5.798 |

Sullo scenario R24 sembrava migliore del modello spedito ovunque. **Sugli split
`val` e `test` del progetto cade.** Macro-F1 su `val`: **0,7704 del modello
spedito contro 0,7174 di R12 e 0,7122 di R24**, cioè 5,8 punti persi. Il costo
sta su 147 finestre `opening_delay` e 59 `flowmeter_dropout` che il modello
spedito indovina e i candidati no.

Il rischio per cui la verifica era stata scritta — le due classi che lo scenario
non esercita mai — **non si è materializzato**: `closing_delay` e
`flowmeter_glitch` restano entro un punto dal riferimento su tutti e due gli
split, perché stanno lontano dall'asse fillingtime che l'aumento allunga. Il
danno è caduto su `flowmeter_dropout`, una classe che **lo scenario contiene** ma
misura solo col tasso di anomalia (100% sopra 0,5 in tutti e tre i modelli), e
che quindi lo nascondeva.

### Perché non si ripara spostando il confine

Nella matrice di confusione la casella fuori diagonale dominante, in tutti e tre
i modelli e su tutti e due gli split, è **`opening_delay` contro `restriction`**.
Le due classi stanno sullo stesso asse, z(`mean_fillingtime`), e differiscono
**solo in ampiezza**: mediane su `val` 21,5 e 9,9, su `test` 18,4 e 11,4. Un
aumento per classe non può separarle, perché non c'è niente da separare in quella
direzione. **Serve una feature che discrimini, non un confine riscalato.**

Due varianti selettive provate e rifiutate: recuperano `opening_delay` su `val` a
25,9 e 27,1 e abbattono i falsi allarmi, ma fanno crollare `restriction` a 31-34
su entrambi gli split.

### Il fatto che ridimensiona tutta la voce

`comune/dati.js:44-56` è l'intera superficie API della dashboard — `stato`,
`oee`, `oeeSerie`, `valvole`, `valvola`, `valvolaKpi`, `allarmi`, `storico`,
`pareto`. **Non c'è `score`.** Nessuna delle cinque pagine legge
`/valves/{id}/score`, quindi `predicted_label` non arriva a schermo da nessuna
parte. Correggere la classificazione della valvola 21, da sola, non cambierebbe
un pixel.

Quello che si vede davvero è un'altra cosa: tutti e nove gli allarmi attivi hanno
`fault_type: "score_aggregation"`, e `a/pagina.js:632` e `v1/pagina.js:829`
stampano quel token tale e quale. A un manutentore la dashboard scrive oggi
«score_aggregation · da 10:02» su ogni valvola in allarme, **senza nessun nome di
guasto**, nemmeno sulle otto che il modello classifica correttamente. La lineage
tecnica nell'engine è una decisione presa il 21 agosto e va lasciata stare: il
difetto è nella resa, non nel motore.

### Provenienza: un rischio trovato che non era registrato

`_resolve_model_version` (`pipeline/inference.py:75-96`) non trova
`model_version` nel sidecar del modello e ripiega su `manifest.yaml:code_version`
= `d-w4-c950bcb3f5d5`. Un modello riaddestrato senza toccare il manifest
scriverebbe predizioni **con la stessa `model_version` delle 723k già
persistite**, e `load_score_history` (`pipeline/alert.py:620`) partiziona solo per
`run_id`. I due modelli si mescolerebbero dentro la stessa corsa senza lasciare
traccia. **Chiunque spedisca un modello nuovo deve bumpare il manifest prima.**

### Cosa è stato toccato

Niente fuori da `.scratch/silenzio-21/`, dove restano `n1.py`..`n13.py`,
`p1.py`..`p3.py`, `p1_results.json` e `fr_all.parquet`. `model.joblib`,
`zstats.json`, gli split, `plcsim/` e `pipeline/` sono intatti. Il banco `n2.py`
riproduce il riferimento registrato alla cifra: 0,0029 / 0,0892 / 699 / 4 / 0,24%.

## 2026-08-23 — il nome del guasto arriva a schermo, e M11 si chiude

Eseguito il piano approvato dall'utente, sette passi.

### Un'assunzione del piano e' caduta subito, in meglio

Il piano prevedeva di aggiungere `score` alla superficie API di `comune/dati.js`
e temeva trentacinque chiamate su MACCHINA. Non serve niente di tutto questo:
**`/valves` porta gia' `last_prediction.predicted_label` per tutte e trentacinque
le valvole**, nella chiamata che le pagine fanno comunque. Nessuna route nuova,
nessuna chiamata in piu', nessun costo di latenza.

### Cosa e' cambiato

- **`comune/dati.js`** — `NOME_GUASTO`, i sette nomi in italiano in un dizionario
  solo, e `nomeGuasto(valvola)`, che li applica. Restituisce «il modello la dice
  sana» quando l'etichetta e' `healthy`, «guasto non diagnosticato» quando la
  predizione manca, e l'etichetta cruda quando e' sconosciuta al dizionario.
- **`a/pagina.js:636`** — la riga dell'allarme nel pannello valvola.
- **`v1/pagina.js`** — la stessa riga (`:839`), l'etichetta di accessibilita'
  della cella (`:401`) e il tooltip della giostra (`:246`). Il nome viaggia sulla
  riga per valvola come `nomeAtt` (`:322`).
- **`giornoOra()`** al posto di `ora()` sugli allarmi, su entrambe le pagine.
  In `v1/` la funzione non esisteva ed e' stata aggiunta.
- **`LESSICO.md`** — sezione 6bis, la grammatica dei nomi di guasto.

### Il difetto che ho introdotto e come e' venuto fuori

Togliendo `tipiAtt` da `v1/pagina.js` ho lasciato un secondo uso vivo in
`tipValvola()`: il tooltip della giostra sollevava `ReferenceError: tipiAtt is
not defined` su ogni passaggio del mouse. **La schermata sembrava a posto** — il
difetto stava su un percorso che uno screenshot non attraversa. L'ha trovato la
console del browser. `tipValvola()` e' stata sganciata dal parametro e legge
`v.nomeAtt`, e il percorso e' stato riesercitato su tutte e 35 le celle, con
`mousemove` e `focus`, a zero errori.

### Verifica

Sui dati veri di `storico_60d`, API 8123, proxy 8078, viewport 1536x770:

| | prima | dopo |
|---|---|---|
| valvola 8 | `score_aggregation · da 07:05` | `restringimento · da 03/07, 07:05` |
| valvole 13-18 | `score_aggregation` | `pressione instabile` |
| valvola 21 | `score_aggregation` | `il modello la dice sana` |
| valvola 30 | `score_aggregation` | `restringimento` |

Le 35 etichette di accessibilita' di VALVOLE non contengono piu' nessun
`score_aggregation`. Console pulita su tutte e cinque le pagine — MACCHINA,
VALVOLE, OEE, TEMPO, CARTA — e `scrollWidth` uguale a `innerWidth`, quindi
nessun trabocco orizzontale.

Suite: **567 su 567**, un solo avviso, nei quattro comandi registrati in
`STATE.md`. Fuori da `.scratch/dashboard-v7/` e `.project/` non e' stato
modificato nessun file: verificato con `find -newermt`.

## 2026-08-23 · Guida tecnica: scelte di forma e prima tappa approvata

Aperto il lavoro di presentazione del progetto: due artefatti didattici, una
guida tecnica in otto tappe e un "viaggio del dato" per chi non ha mai sentito
nominare PLC, OPC UA o broker. Nessun codice del simulatore, della pipeline o
della dashboard e' stato toccato.

### Scelte di forma, prese dall'utente su anteprime funzionanti

Le quattro decisioni strutturali sono state portate all'utente come una pagina di
anteprime vere da cliccare, non come prosa
(<https://claude.ai/code/artifact/d96105f0-db79-4903-8f88-07377c93876c>). Ha
scelto: mappa dell'impianto incollata in cima (A3), codice vero del repository
con le righe che si accendono (B1), scena ferma e testo che scorre per il
viaggio del dato (C1), giostra vista in pianta che ruota (D1).

### La prima tappa, in tre varianti

Tappa **02 OPC UA**, costruita in tre varianti indipendenti con lo stesso
briefing, gli stessi numeri e lo stesso codice: cambiava solo il principio
organizzativo. L'utente ha scelto la variante A, la catena di domande
(<https://claude.ai/code/artifact/0a8839ff-f127-4d09-8741-90a266283779>).
Scartate: B, l'anatomia in tre pezzi
(<https://claude.ai/code/artifact/e4374ffb-3ebc-4d4e-a9de-b2b08f18f691>); C, la
sessione pratica in cinque passi
(<https://claude.ai/code/artifact/878bc1e6-aeb3-460a-b6d5-76f3cd6be566>).

Contenuto verificato sul repository, non riassunto a memoria: righe 302-311 e
344-368 di `plcsim/opcua_server.py`, NodeId presi da `edge/tag-mapping.yaml`,
567 tag contati (7 di macchina piu' 16 per ognuna delle 35 valvole), e il ciclo
417 del 22 agosto letto dal Parquet (valvola 29, 1980 ms, 2505 impulsi contro un
bersaglio di 2500).

### Correzione sulla scrittura

L'utente ha respinto la prima consegna della variante A per gli em dash: ne aveva
visti quattro, ce n'erano dodici nel corpo, due negli `aria-label` generati da
JavaScript e due en dash nei riferimenti di riga. La lezione registrata: la
ripulitura del testo si fa **prima** di pubblicare e su ogni stringa visibile del
file, non solo sulla risposta in chat.

### Grammatica estratta

`.scratch/presentazione/GRAMMATICA.md`, piu' i pezzi comuni in
`comune/{testa,coda}.html` e il montatore `costruisci.py`. La tappa 02 rimontata
dai pezzi comuni e' identica a quella approvata a meno di un commento nel codice,
verificato con `diff`. Dalla tappa 03 in poi la lingua non e' piu' in
discussione.

## 2026-08-23 (sera) · Le altre sette tappe, il viaggio del dato, e la revisione

Completata la presentazione: 24 pagine della guida (otto tappe per tre varianti)
piu' tre varianti del viaggio del dato. Indirizzi in
`.scratch/presentazione/INDIRIZZI.md`. Nessun file del simulatore, della
pipeline o della dashboard e' stato toccato.

### Le tre varianti, e cosa cambia fra loro

Grammatica identica, un solo asse di variazione: **a** la catena di domande
(il principio approvato per la tappa 02), **b** l'anatomia in pezzi con un
"se togli questo pezzo" per ciascuno, **c** la sessione pratica coi comandi
veri in ordine cronologico. Il viaggio del dato usa gli stessi tre principi su
undici tappe, con la scena ferma che si trasforma e la giostra vista in pianta.

### Il controllo automatico

`verifica.py` gira su tutte le pagine montate: trattini lunghi, segnaposto,
`<svg>` senza descrizione, note che puntano a righe di codice inesistenti,
risorse esterne, temi mancanti, tag non chiusi. **25 su 25 pulite.** La sintassi
del JavaScript e' controllata a parte con `node --check`: aveva trovato un
apostrofo dentro una stringa a virgolette singole che rompeva in silenzio le tre
pagine del viaggio.

### La revisione, fatta da tre agenti separati

Nessuno dei tre ha modificato file: hanno prodotto elenchi, applicati da me.

**Fatti** (16 vere, 3 false, 1 non verificabile). Le tre false, corrette:
i nodi OPC UA si chiamano `Valve01…Valve35`, non `Valve00…Valve34`; il record
di ciclo ha **19** colonne (`telemetry.py` `CYCLE_COLUMNS`), non 21; l'esempio di
risposta di `/valves/29` conteneva `cycles_24h` e `filling_ok_rate`, che in
`pipeline/api.py` non esistono, e chiamava `probability` un campo che si chiama
`probabilities` ed e' un oggetto per classe. Corretta anche una contraddizione:
`tag-mapping.yaml` e' generato, non scritto a mano. Il dettaglio piu' esposto
regge: la riga valvola 29 / ciclo 417 corrisponde al Parquet campo per campo, e
18 511 righe e' esatto.

**Scrittura** (36 sostituzioni su 19 file). Il tic per antitesi era ancora vivo
in 17 punti, tre dei quali titoli; la formula "il modo piu' X per" in tre tappe;
il titolo vuoto "Una cosa che va detta" ripetuto in sette file; ventiquattro due
punti usati come connettivo.

**Leggibilita'.** Il difetto peggiore non era nel testo ma nel disegno: nel
viaggio la prosa diceva *rubinetto* e *scatti* mentre la scena accanto diceva
*valvola* e *impulsi*, e mostrava `-5` dove il testo diceva "cinque in piu'".
Allineato il vocabolario della scena alla prosa, tolti `slot` e `OEE` che non
erano mai sciolti, spiegato il segno dello scarto. Nella guida sono state
sciolte alla prima comparsa **PLC**, **KPI**, **tag**, **QoS** e **OEE**, che
non erano definiti in nessuna delle 21 pagine.


## 2026-08-23 · La scelta, e le tre correzioni che ha portato

L'utente ha scelto la **variante a** per tutte e otto le tappe della guida e per
il viaggio del dato. Il principio organizzativo del progetto e' quindi uno solo,
la catena di domande; le varianti b e c restano pubblicate come archivio del
confronto e non vanno piu' aggiornate.

Con la scelta sono arrivati tre difetti visivi, tutti corretti e ripubblicati
agli stessi indirizzi:

1. **I sei strati del simulatore.** Le etichette di destra uscivano dal riquadro.
   Ora sono ancorate al bordo destro invece che posizionate sperando che ci stiano.
2. **Il grafico di Docker.** Il filo del consumatore attraversava il riquadro di
   Node-RED. Ora passa sotto i riquadri, tratteggiato.
3. **Il finale del viaggio.** L'ultima scena adesso rispecchia la dashboard vera,
   con le cinque voci di navigazione, le 35 postazioni e il riquadro di dettaglio
   della 29 che porta il dato arrivato in fondo al percorso.

Nessuna delle tre era visibile al verificatore automatico: erano difetti di
geometria dentro gli SVG.


## 2026-08-24 · L'IIoT chiuso, e le quattro cose che lo tenevano aperto

L'utente ha chiesto di portare a chiusura il progetto IIoT, cioe' la roadmap
`docs/roadmap-iiot.md`. Non era chiudibile per un motivo stupido e uno serio.

### Il motivo stupido: due cose si chiamavano M11

Nella roadmap M11 e' sicurezza e hardening, segnata opzionale dal 2026-08-12 e
mai aperta. In `STATE.md` e in `DECISIONS.md` «M11» indica invece la taratura
dell'allarme, dichiarata chiusa il 2026-08-23. Chi leggeva «M11 e' chiuso»
concludeva che la roadmap fosse finita.

I due nomi sono ora **M11-sicurezza** e **M11-taratura** e non vanno piu' usati
nudi. La prima e' dichiarata **fuori ambito, POC accettato**, per decisione
dell'utente presa davanti al quadro completo dei sei gradini. Non e' un rinvio.
Motivo: la catena gira su una macchina sola, in locale, e non e' mai stata
esposta; i file di configurazione dichiarano gia' da soli di essere un POC.

### Il motivo serio: quattro documenti dicevano cose diverse

I rapporti di collaudo M9 e M10 portavano dal 2026-08-13 un verdetto **ritirato**
mentre la memoria di progetto trattava M10 come accettata. Rifatti entrambi oggi,
con una sezione `## 8` appesa in coda: i verdetti vecchi non sono stati toccati.

**M9**: tutti i criteri PASS su due corse. AC-M9-2, il criterio che aveva causato
la ritrattazione perche' l'harness confrontava con `zstats=None`, oggi passa con
gli zstats reali del modello. E' la prima volta che quel criterio viene
verificato davvero.

**M10**: due FAIL e un `TypeError` alla prima esecuzione, **nessuno dei tre un
difetto del prodotto**. L'harness era rimasto indietro rispetto a due decisioni
prese dopo il freeze:

- non fissava la modalita' dell'alert engine, quindi dal 2026-08-21 ereditava il
  default score-only K=5/N=150 e misurava un motore che il criterio non descrive:
  con sei record in sequenza K non si raggiunge mai, `labels=[]`;
- `alert_id_for`, `upsert_alert` e `insert_transition` avevano preso un `run_id`
  obbligatorio il 2026-08-22, e l'harness non era mai stato aggiornato.

Corretti tutti e due, M10 esce 0 su tutti i criteri. Throughput 666.011 rec/s
contro una soglia informativa di 1.000.

### Una deviazione dichiarata, non aggirata

AC-M10-5 asserisce il vincolo unico `uq_alerts_valve_fault` su
`(valve_id, fault_type)`. La separazione dei run lo ha sostituito con
`uq_alerts_run_valve_fault` a tre colonne. Il criterio congelato non e' stato
ammorbidito: e' stato **superato da una decisione successiva**, e la cosa e'
registrata in `work/review-gate-log.md` come deviazione regolamentata, con il suo
limite scritto. Il limite: con `RUN_ID` fisso nell'harness, nessun assert prova
che due corse diverse producano due righe distinte.

### La verifica indipendente ha trovato sette rilievi, e uno era grosso

Un verificatore che non aveva scritto i report li ha contestati, come chiede il
protocollo. Il rilievo grosso: avevo dato AC-M9-0 per PASS scrivendo «nessun file
core modificato», ed e' falso. `plcsim/run.py` e' del 2026-08-19, quando gli sono
stati aggiunti `--start` e `--end`.

Misurato invece che dedotto: rigenerata la corsa sana di un giorno con seed 42 e
`m4_healthy.yaml`, e confrontata con l'ancora `work/m4_healthy_1d` dell'11 agosto.
**I tre parquet sono identici byte per byte** (SHA-256), 604.398 cicli in
entrambe. La modifica e' retrocompatibile per costruzione: il default di
`--start` e' `2026-06-01T00:00:00Z`, lo stesso ancoraggio che prima era cablato.

**Resta aperto**: l'invariante 1 della roadmap chiede un ADR esplicito per ogni
modifica ai cinque file core, e per quella del 19 agosto l'ADR non esiste. Va
scritto oggi riconoscendo la modifica, oppure va dichiarata una deroga. Non lo
decide un report di accettazione.

### Un secondo rilievo mi ha smentito, e la misura gli ha dato ragione

Avevo scritto che la suite lanciata con il Python di sistema salta 154 test in
silenzio. Il verificatore ha osservato che quella corsa aveva **due** variabili:
l'interprete e Postgres ancora in avvio. Corsa di controllo con il Python di
sistema e Postgres `healthy`: **567 passed, zero skip**. Non era l'interprete.

Il reperto resta e cambia causa: una suite lanciata su un Postgres che non e'
ancora `healthy` salta piu' di un quarto dei test e chiude comunque con «passed».
La regola operativa: si aspetta `docker inspect plcsim-postgres --format
"{{.State.Health.Status}}"` uguale a `healthy`, e il numero da confrontare e'
**567 passed**.

### Il preflight, perche' le regole a memoria si saltano

`edge/scripts/preflight_live_run.py` trasforma in controllo eseguibile le quattro
regole d'avvio di un run live, che vivevano come prosa in due file diversi:
partizione raw nuova, Node-RED riavviato a server gia' in ascolto, sessione
broker pulita con `--client-id` dedicato, `--run-id` esplicito e non gia' usato.
Solo letture, esce 0/1/2. Tre stati e non due: un controllo che non si e' potuto
eseguire non e' un controllo passato.

Ha gia' guadagnato il suo posto due volte nella stessa sessione: ha colto il
server OPC UA spento, e poi ha confermato l'ordine di avvio corretto.

### La prova generale, superata

Catena accesa nell'ordine giusto e `verifica_battito.py 10` con uscita **0**: in
dieci minuti raw +193, cycles +187, predizioni +3, tutti e tre gli stadi vivi.
Evidenza in `work/ricollaudo-20260824/esito_battito-20260824.json`. Catena poi
spenta; a fine sessione non resta acceso nulla che non fosse acceso prima.

### Le due voci minori, rinviate per iscritto

`SpeedActual` dichiara una velocita' 2,5 volte sotto la cadenza reale: rinviata,
perche' nessun consumatore a valle legge quel tag e sistemarlo tocca il contratto
OPC UA. La provenienza del modello: rinviata con condizione di riapertura
esplicita, il giorno in cui si spedisce un modello riaddestrato.

### Le catture

Tutte in `work/ricollaudo-20260824/`, non in una cartella di sessione: le due
corse pulite della suite, la corsa sporca iniziale, la corsa di controllo, i due
collaudi M9, il collaudo M10, il gate del battito e il sommario della corsa di
bit-identita'.
