# LESSICO — regole per le pagine della dashboard v7

Quattro pagine sono accettate dall'utente: `a/` MACCHINA, `v1/` VALVOLE, `oee/` OEE
(19 agosto) e `pc/` TEMPO (20 agosto). **Non sono esempi: sono lo standard.** Chi
costruisce la prossima rispetta quanto segue. Dove una regola ti sembra sbagliata,
portala all'utente — non risolverla da solo a favore delle tue prove.

Grammatica implementata: `comune/lessico.css` (caricalo **prima** del tuo `stile.css`).
Nel tuo `stile.css` sta **solo** la disposizione della tua pagina: aree della griglia e
gli elementi che esistono solo lì.

```html
<link rel="stylesheet" href="/comune/lessico.css">
<link rel="stylesheet" href="stile.css">
```

Le regole nate da TEMPO portano il riferimento al codice che le prova, nella forma
`pc/pagina.js:233`. Serve a chi deve rileggere il meccanismo, non a chi deve solo
applicarlo.

---

## 1 · La regola del colore

Implementata in `tinta()` in `a/pagina.js` e `pc/pagina.js:56`. È la regola più
importante del documento: la versione precedente è stata respinta con *«è come essere
colpito, tutti questi colori fanno un effetto»*.

**Il colore lo prende solo ciò che ha una gravità. Tutto il resto è neutro.**

Ogni grandezza ha un riferimento `{v, tol, morta?, alto?}`. Dato un valore `v`:

| condizione | tinta |
|---|---|
| `v` nullo, o riferimento assente | `--dato` (neutro) |
| `v` **sopra** la tacca | `--dato` (neutro) |
| `v` sotto la tacca di meno di `morta` (zona morta) | `--dato` (neutro) |
| sotto di ≤ `tol` | `--sev1` |
| sotto di ≤ 2·`tol` | `--sev2` |
| sotto di ≤ 3·`tol` | `--sev3` |
| sotto di più di 3·`tol` | `--sev4` |
| `v` sopra la tacca di più di `alto`, dove `alto` è definito | `--sev2` |

Conseguenze da non negoziare:

- **Un valore che sta bene non diventa verde.** Non esiste un verde. Stare bene è
  l'assenza di tinta, non una tinta.
- **Sopra il riferimento è neutro.** Superare la base non è un evento da segnalare, salvo
  le grandezze con `alto` (la prestazione, dove sfondare il target è un dato sospetto).
- **La zona morta esiste perché il rumore non è un segnale.** Sulla prestazione lo scarto
  osservato è 0,1–0,4 punti su tutti e sei gli scenari e non vale mai esattamente 1,000:
  senza `morta` sarebbe tinta sempre, e un colore sempre acceso non è un colore. Per una
  singola valvola la zona morta vale **2σ della sua base**: l'oscillazione giornaliera di
  una valvola sana non è un segnale (`pc/pagina.js:688`).
- **Uno stato non è una gravità.** La ripartizione del tempo per stato OMAC è tutta
  neutra, differenziata solo per opacità. `Idle` non è rosso: una macchina ferma per
  scelta non produce rosso.
- **Il numerale segue l'arco solo quando la gravità è reale.** Con tinta `dato` o `sev1`
  il numero resta `--ink`: un colore tenue su un numero grande griderebbe più del dovuto.
- **Il colore sta sul bordo, non addosso a ciò che si legge.** Quando una stessa forma
  deve portare insieme un'identità da leggere e uno stato da vedere, lo stato prende un
  filo sul bordo — un contorno, o un lato solo — e non un riempimento che invade lo
  spazio del testo. La striscia di CARTA lo ha imparato a sue spese: due fasce piene da
  11 px sopra e sotto lasciavano al numero della valvola 12 px dei 34 della cella, ed è
  stata respinta con *«i rettangoli un po' rendono il numero meno chiaro»*. Un filo da
  5 px porta la stessa gravità e non toglie niente alla lettura
  (`k1/pagina.js:458`).

### Dove la tinta non entra

Tre regole di astensione, tutte nate da TEMPO:

- **Quando un riquadro contiene più grafici affiancati, la tinta la porta uno solo.** Le
  tre componenti dell'OEE sono disegnate con `tint: false` (`pc/pagina.js:553`), il
  grafico primario no. Tre traiettorie che si tingono insieme sono una fascia, non tre
  segnali.
- **Nel pannello di dettaglio il grafico primario è neutro** (`pc/pagina.js:1261`). Chi ha
  aperto quel pannello sa già quale valvola sta guardando: ripeterglielo col colore non
  aggiunge niente e consuma l'unica risorsa che si esaurisce.
- **Una navigazione non si tinge, finché la scelta non è essa stessa la domanda.** La
  striscia del tempo è `--dato` fisso (`pc/pagina.js:328`): è il posto dove si sceglie
  cosa guardare, non un posto dove si guarda. La striscia delle 35 valvole di CARTA è
  l'eccezione, ed è dichiarata: lì la popolazione è chiusa e la domanda è *quale*
  valvola, non *quando*, quindi una fila di numeri tutti uguali costringerebbe a
  provarli a uno a uno. L'utente l'ha chiesto con queste parole — *«è importante che ti
  dica quali valvole vuoi guardare»* — e la striscia intera conta comunque come **una**
  regione tinta. Il confine fra i due casi: si tinge un selettore solo quando ciò che
  sceglie è un insieme finito di oggetti confrontabili fra loro.

### La rampa di popolazione

Una lettura di popolazione — tutte le valvole su un metro solo — **non usa la rampa
storica**. Ha una propria rampa ancorata alla mediana del gruppo corrente (`popTinta()`,
`pc/pagina.js:893`): dentro la fascia neutro, entro due volte la fascia `--sev2`, entro
cinque `--sev3`, oltre `--sev4`. Dalla parte buona resta neutro, come sempre.

### La metrica: conta gli elementi che portano tinta

Prima di consegnare, conta le **regioni** della pagina che portano una tinta (`sev1`–
`sev4`, `attenz`, `grave`). Una striscia di 35 valvole è **una** regione, non 35.

- Scenario sano: **al massimo 1**. MACCHINA ne ha **0** (solo le chiavi della legenda
  sono colorate, e la legenda non è un dato).
- Scenario di deriva diffusa: **al massimo 3**. MACCHINA ne ha **3** su
  `d-deriva-diffusa`: l'arco del gauge OEE, il suo numerale, la striscia valvole.
- TEMPO ne ha **3** come massimo strutturale: il grafico primario dell'OEE, la fascia
  delle 35 traiettorie, il riquadro di popolazione. Le tre componenti, la striscia e il
  pannello non entrano nel conto, per le tre regole di astensione qui sopra.
- CARTA ne ha **3** come massimo strutturale: le due carte e la striscia delle 35. Sulla
  valvola 21, dove il disaccordo fra le due carte è al suo estremo, ne porta **2**: la
  carta del ciclo singolo resta neutra perché è dentro banda.

Se la tua pagina ne ha di più, non stai segnalando: stai colpendo.

---

## 2 · Come si disegna un riferimento

### I due meccanismi, e quando si usa quale

Fino a TEMPO questo documento diceva che il meccanismo era uno solo. **Adesso sono due**,
e la differenza fra loro è la ragione per cui TEMPO esiste. Dichiarare quale stai usando
è obbligatorio, a schermo e non solo nel codice.

**Primo: ogni grandezza contro la propria base storica.** È il meccanismo predefinito.
Ogni grafico disegna il proprio intervallo normale; un punto oltre la linea *è* la
spiegazione.

**Secondo: ogni valvola contro la mediana delle altre trentaquattro, nello stesso
istante** (`pc/pagina.js:848`, `875`). Esiste perché il primo meccanismo è **cieco su ciò
che è sempre stato storto**: le valvole 9 e 21 sono anomale dal primo giorno della run,
quindi quell'anomalia è dentro la loro stessa base e il confronto con sé stesse le
dichiara a posto. Serve anche per i guasti piccoli e comuni a più valvole, dove la
differenza si vede solo fra popolazioni.

Il secondo meccanismo **non sostituisce il primo: gli sta accanto**, in un riquadro
proprio, e i due si leggono uno sopra l'altro. Non si fondono mai in un punteggio unico.

Tre marchi, sempre insieme:

1. **La tacca** — la linea del riferimento. Tratteggiata `5 4`, `--rif`, spessore 1,5 su
   fondo bianco; sul gauge è un segmento radiale pieno che attraversa l'arco.
2. **La banda** — l'intervallo normale attorno alla tacca, `--banda` sui gauge e
   `--traccia` sui grafici a fondo pieno. Larghezza `±tol`, oppure `±3σ` sulle carte di
   controllo della singola valvola.
3. **L'etichetta con la provenienza** — `rif. 64% · run sano`, `riferimento 50,4%`,
   `marcia di riferimento 64%`, `base della valvola 13 su 86.296 cicli sani · banda ±3σ`.

**La provenienza di ogni riferimento deve essere leggibile a schermo senza aprire il
codice.** È una regola, non un vezzo: un riferimento senza provenienza è un numero che il
tecnico non può contestare. Le provenienze in uso sono `run sano`, `target`, `baseline`,
`mediana del gruppo`.

**Una banda misurata ha sempre un minimo, e il minimo si dichiara.** `Math.max(sd,
minimo)` (`pc/pagina.js:136`), e a schermo «banda ±1σ, minimo ±2 punti»
(`pc/pagina.js:145`). Senza il minimo, 1σ su una base stabile userebbe tutta la rampa di
gravità dentro il rumore.

**Una soglia dichiarata si scrive col numero che la giustifica, e si ripete a schermo in
unità reali.** `POP_FASCIA = 0,05` esiste perché sulla base sana la valvola più bassa
dista 3,3 punti dalla mediana e le due anomale venti; `TEMPO_FASCIA = 80 ms` perché la
più lenta dista 55 ms e le anomale 108 (`pc/pagina.js:785`, `791`). Non è derivata da una
sigma: è scritta nel codice e scritta a schermo (`pc/pagina.js:1042`).

**Quando molti riquadri gemelli si guardano insieme, l'altezza di ciascuno si misura in
unità della propria banda, non nei propri dati** — quattro bande per riquadro, dichiarato
a schermo (`pc/pagina.js:752`, `782`). Altrimenti il riquadro più tranquillo sembra il più
mosso, perché ha lo zoom più forte.

**100% non è il riferimento di questa macchina**: non è mai stato osservato. La linea base
sana è ~21,3% di scarto. La prominenza visiva segue lo scostamento dalla base dichiarata
di ciascuna grandezza, non la distanza da 100. Il fondoscala dei gauge è **1,10**, perché
`performance` vale 1,001–1,002 in cinque punti della serie e non deve sfondare.

**La distanza fra due valvole si dice in punti di qualità, non in percento di percento**
(`pc/pagina.js:798`): «20 punti sotto», mai «20% sotto l'80%».

**In una popolazione l'ordine sull'asse è l'identità della valvola, non il suo giudizio**
(`pc/pagina.js:875`). Le valvole stanno nell'ordine di macchina. Il posto in classifica
esiste, ma si chiede passandoci sopra: non ordina la vista e non è mai il marchio
principale.

---

## 3 · Le regole dei grafici

Nate da un difetto reale: 400 marcatori sovrapposti avevano coperto la linea, e l'utente
ha detto *«non mi spiega come sta andando la valvola»*.

- **La traiettoria è il marchio più in alto e non viene mai coperta.** Si disegna dopo la
  banda, dopo la tacca, dopo tutto. Nessun marcatore le passa sopra.
- **Marcatori solo sugli attraversamenti della banda** (entra/esce). Sono pochi per
  costruzione. Un pallino per ogni punto fuori banda produce una fascia piena che
  seppellisce la linea, esattamente il difetto respinto.
- **Il «quanto è fuori» lo dicono altri marchi**: l'asse σ a destra, un contatore
  (`400 cicli su 400 fuori banda`), il suggerimento al passaggio del mouse. Mai altri
  pallini.
- **La traiettoria non si schiaccia mai contro un bordo, nemmeno quando esce interamente
  dalla banda.** Il dominio contiene sempre banda più dati, ma garantisce ai dati una
  quota minima di altezza: 16% dell'intervallo più un 8% di margine sui grafici della
  singola valvola, e `max((hi−lo)·0,14, 0,004)` sulla striscia dei due mesi
  (`pc/pagina.js:313`), dove l'OEE si muove di tre punti in sessanta giorni e un dominio
  generoso lo appiattirebbe in una riga. Nella popolazione il dominio contiene tutti i
  dati **e** tutta la fascia, e chi vale zero sta a zero, non schiacciato contro un bordo
  scelto per comodità (`pc/pagina.js:964`).
- **Il divario fra due traiettorie misurate si riempie.** Fra la traiettoria e il suo
  riferimento (`pc/pagina.js:318`), e fra la silhouette del ciclo e quella della base
  (`pc/pagina.js:1233`): le grandezze grezze di due valvole guaste distano l'8%, i loro
  scarti dalla base il 28%. La differenza sta nel divario, non nell'altezza. L'area va
  chiusa da dati veri, mai da un bordo inventato.
- **Traiettoria, riferimento atteso e ampiezza dell'intervallo si leggono insieme**, in un
  colpo d'occhio, senza legenda da decodificare. Se la banda è troppo stretta per tre
  etichette (`+3σ`, media, `−3σ`), se ne scrive **una sola** invece di tre sovrapposte.
- **Due etichette non si sovrappongono mai: quella che perde arretra, sale, o non si
  scrive** (`pc/pagina.js:1249`, `1277`, `1281`). Non è una rifinitura: due numeri
  accavallati sono due numeri illeggibili.
- **Nessuna serie multi-variabile da decodificare.** Niente bolle che comprimono quattro
  dimensioni in colore, dimensione e posizione.
- **Un grafico ricostruito da pochi punti misurati li tiene visibili e li unisce con
  segmenti retti.** Nessuna interpolazione morbida senza una misura che la giustifichi: il
  profilo del ciclo ha **tre** punti veri — apertura, fine riempimento, fine coda — e
  disegnarlo come un'onda continua inventerebbe tutto ciò che sta in mezzo
  (`pc/pagina.js:1131`, `1266`).
- **Quando la sorgente non serve dispersione non si stima una banda: se ne dichiara
  l'assenza** a schermo (`pc/pagina.js:1230`, testo al `1305`: «medie senza dispersione:
  nessuna banda»). Una banda inventata è peggio di nessuna banda.
- **Il riquadro decide le dimensioni.** Il `viewBox` si riscrive in pixel reali a ogni
  disegno (`pc/pagina.js:45`), così il testo non si deforma mai quando la finestra cambia.
  Ridisegno su `resize` con 120 ms di attesa, e su cambio tema.
- **Una convenzione grafica per riquadro**, intitolata con un sostantivo. Noioso di
  proposito. Più copie della **stessa** convenzione impilate contano come una sola — le
  tre componenti dell'OEE sono tre grafici identici, con lo stesso asse e lo stesso verso
  — ma solo se restano neutre (§1). Due convenzioni diverse nello stesso riquadro restano
  vietate.
- **Due valori da confrontare fra loro stanno accostati, non ai due estremi della
  forma.** La distanza fra due segni dice quanto sono legati: due misure che vanno lette
  insieme devono cadere dentro la stessa occhiata. Nella cella di CARTA le due carte
  stanno su una riga sola divisa a metà — sinistra il ciclo singolo, destra la media di
  46 — dopo che la variante con un filo in alto e uno in basso è stata scartata con
  *«guardare uno in alto e un altro in basso li separa un po' troppo»*. Vale insieme al
  divieto di comprimere più dimensioni in un segno solo: lì il difetto è la fusione, qui
  la dispersione, e la forma giusta sta in mezzo — due segni distinti ma adiacenti.

---

## 4 · Le regole dell'interazione

- **Tutto ciò che è cliccabile lo dichiara**: `cursor:pointer`, contorno `--ink` a
  spessore 2 su hover e su `:focus-visible`, etichetta in grassetto. Se non ha hover, non
  è cliccabile. **Ciò che si trascina lo dichiara allo stesso modo**, con `cursor:grab` e
  `cursor:grabbing` mentre si trascina (classe `.trascino`).
- **Deroga per le celle contigue**: quando le superfici afferrabili sono colonne adiacenti
  senza fessure, si **illuminano** invece di incorniciarsi (`.cella-pop:hover`). Un
  contorno spesso 2 fra colonne contigue disegna una griglia che nessuno ha chiesto. Il
  contorno resta su `:focus-visible`, dove serve a dire dove sta il fuoco.
- **Il segno della scelta e il segno del verdetto non usano lo stesso canale.** In una
  cella che porta tutti e due, la scelta resta il contorno `--ink` a spessore 2 con
  l'etichetta in grassetto — il segno che l'utente riconosce già — e il verdetto scende
  sul bordo basso. Se il verdetto prendesse il contorno, i due significati si
  sovrapporrebbero e uno dei due andrebbe spostato altrove
  (`k1/pagina.js:448`, `458`).
- **Il bersaglio del clic è la cella intera e si disegna per primo, sotto tutti gli altri
  marchi** (`pc/pagina.js:725`, `998`). Fra due celle contigue non resta un pixel
  scoperto: un pixel scoperto è un clic perso. C'è un difetto reale dietro questa regola —
  `mousedown` sul testo e `mouseup` sull'svg mandavano il `click` all'antenato comune.
- **Ogni grafico ha un hover informativo**, con **bersaglio sulla X più vicina**: non si
  deve centrare il punto. Si calcola l'indice dalla coordinata X e si aggancia una mira
  verticale più un segnalino sul punto.
- **Il suggerimento non sposta mai il contenuto**: `position:fixed`, `pointer-events:none`,
  si ribalta ai bordi della finestra. Contiene valore, momento e **scostamento dal
  riferimento** — mai una spiegazione.
- **Accesso da tastiera su ogni grafico interattivo**: `tabindex="0"`, `aria-label` che
  dice quanti punti e quale riferimento; frecce ← → per scorrere, `Home` e `Fine` agli
  estremi, `Esc` per spegnere. Il ruolo è `role="img"` per un grafico e **`role="slider"`
  per un elemento trascinabile** — e uno slider porta `aria-valuenow`, `aria-valuemin`,
  `aria-valuemax`, altrimenti è dichiarato e non descritto.
- **Il fuoco entra sul punto che porta il senso, non per forza sull'ultimo.** Su una serie
  temporale è l'ultimo punto; su un grafico di pochi punti è quello che spiega la forma —
  nel profilo del ciclo è la fine riempimento, non la fine coda (`pc/pagina.js:1315`).
- **Gli ascoltatori si rilegano a ogni ridisegno**, con un `AbortController` per riquadro
  (`pc/pagina.js:561`, `1189`, `1312`). Senza, si accatastano puntando a marchi che non
  sono più nel documento.
- **Un interruttore fra due letture sta nell'intestazione del riquadro**, non è un bottone
  di sistema, e **la voce spenta porta un fatto misurato** — quante valvole stanno fuori
  con quel metro — non solo il proprio nome (`pc/pagina.js:908`).
- **Il periodo in vista è scritto nella barra-titolo di ogni riquadro che lo rispetta**, e
  cambia mentre si trascina (`pc/pagina.js:219`, classe `.per`).
- **Il dettaglio si apre sopra la pagina, non la sostituisce**: pannello a destra, velo,
  `role="dialog"`, `aria-modal`, chiusura con la ✕, con il velo o con `Esc`, e **il focus
  torna all'elemento da cui si è partiti** (`pc/pagina.js:1052`, `1075`).

---

## 5 · Il tempo, e i confini del dato

La sezione è nata con TEMPO. Vale per qualunque pagina che dia all'utente il governo di
una finestra temporale.

### La navigazione

- **La navigazione temporale non può entrare nel vuoto.** La scala si allunga oltre
  l'ultimo dato — `SBORDO = 0,055`, il 5,5% dell'estensione (`pc/pagina.js:237`) — ma il
  trascinamento resta chiuso fra il primo e l'ultimo ciclo esistente (`pc/pagina.js:233`,
  `166`). Il margine serve a far vedere che il tracciato finisce, non a permettere di
  andarci.
- **Ai bordi la finestra trasla, non si accorcia** (`pc/pagina.js:166`). Se sfonda a
  sinistra si sposta a destra della stessa quantità. La durata scelta è una decisione
  dell'utente, e il bordo non gliela cambia sotto le mani.
- **La finestra ha una durata minima dichiarata** — sei ore su TEMPO (`pc/pagina.js:163`)
  — e non ci scende mai.
- **La finestra selezionata scrive la propria durata in fondo a sé stessa, non in cima**
  (`pc/pagina.js:296`), dove passa la tacca del riferimento.
- **Un clic fuori dalla finestra la sposta centrata sul clic e prosegue in trascinamento**
  (`pc/pagina.js:393`): non è un errore da correggere, è il modo più veloce di arrivare
  lontano.
- **Le maniglie hanno una zona di presa più larga del proprio disegno** — 7 px
  (`pc/pagina.js:395`).
- **La tastiera governa tutte le dimensioni separatamente**: ← → scorrono, `Shift` scorre
  di una finestra intera, ↑ stringe, ↓ allarga, `Home` e `Fine` agli estremi
  (`pc/pagina.js:419`). Chi non ha il mouse deve poter fare le stesse due cose — spostare
  e ridimensionare — non una sola che le fonde.
- **La granularità della richiesta segue la larghezza della finestra**, non una scelta
  dell'utente: ore sotto i tre giorni, giorni sotto i trentadue, settimane oltre
  (`pc/pagina.js:633`).

### Dove finisce il dato

- **Dove finisce il dato lo dice un segno, non una frase.** Il tratto oltre l'ultimo ciclo
  è un rettangolo con pattern diagonale, chiuso da una sbarra `--ink` spessa 2
  (`pc/pagina.js:347`, `369`). La domanda «è il PLC live?» si chiude così, non con una
  riga di prosa.
- **Lo stesso segno vale in testa.** Un tratto scartato dai calcoli — le prime 24 ore
  della run, che la finestra mobile non copre (`pc/pagina.js:124`) — **occupa il posto che
  gli spetta sull'asse**, tratteggiato, invece di sparire. Sparire falsa la scala.
- **Il tratto morto non si attenua mai**: si disegna **dopo** il velo della finestra
  (`pc/pagina.js:272`). La fine del dato non diventa meno vera perché stai guardando
  altrove.

### L'attesa

- **La schermata segue subito, con i dati già in memoria; la rete affina dopo**
  (`pc/pagina.js:172`, `181`). Un trascinamento che aspetta la risposta prima di muoversi
  si legge come rotto.
- **Vince l'ultima finestra chiesta.** Un contatore `gen` butta le risposte in ritardo
  (`pc/pagina.js:195`): disegnerebbero i punti di un periodo dentro l'asse di un altro.
- **Richieste indipendenti partono insieme**, non in coda, perché costano molto diverso —
  ~460 ms contro ~30 e ~110 (`pc/pagina.js:202`). Ciascuna disegna quando torna.
- **Un disegno che appartiene a un'altra finestra si attenua** — `opacity .45`
  (`pc/pagina.js:956`) — invece di spacciarsi per questa.

---

## 6 · I valori assenti

**`null` non è zero e non è rosso.** In `f-oee-degradato` l'OEE corrente è `null` (zero
cicli nel turno) mentre il turno prima valeva 0,758.

- Si dichiara **«non calcolabile»** / **«non calc.»**, in `--muto`, a corpo ridotto.
- L'arco del gauge diventa **tratteggiato** su tutto il percorso: si vede che il quadrante
  esiste e che il valore manca.
- **Si mostra il confronto precedente se esiste**, con il motivo dell'assenza in unità
  reali (`0 cicli`), e il segnaposto è un rettangolo tratteggiato a terra — non una barra
  a zero.
- **La linea si spezza invece di scendere a zero**: nella serie, un punto `null` chiude il
  tratto e il successivo ne apre uno nuovo (`M` invece di `L`). Mai interpolare sopra un
  buco.
- In una popolazione, `null` è un **cerchio vuoto tratteggiato sulla mediana**, mai zero
  (`pc/pagina.js:1002`). `media: null` significa «non misurata», e si scrive così.
- Un dettaglio assente per una valvola (`__status`) è **«non disponibile»**, mai zero.
- Se una serie intera è vuota, si scrive «serie non disponibile» al centro del riquadro:
  un riquadro vuoto non dichiarato è un difetto.

### Il degrado è graduale, e ha quattro gradini

Nati da TEMPO, in ordine di gravità crescente:

1. **Se la grana fine non risponde si resta sul dato più grosso**, che è un dato vero
   (`pc/pagina.js:700`). «Assente» si scrive solo quando manca anche quello.
2. **Un dato parziale dice quali grandezze mancano, per nome**, non «dati incompleti»
   (`pc/pagina.js:1209`).
3. **Il motivo di un dato ridotto si scrive anche quando non c'è niente da disegnare**
   (`pc/pagina.js:1202`). È il caso in cui serve di più: un riquadro vuoto senza motivo è
   indistinguibile da un difetto.
4. **Quando una serie non c'è si nomina la route che non ha risposto**
   (`pc/pagina.js:713`, `1196`). Chi legge la pagina è un tecnico: il nome della route è
   un'informazione, non un dettaglio interno.

**Un riquadro senza contenuto non si prende lo spazio: si dichiara e si ritira**
(`body.valv-assenti`, `pc/stile.css:48`). La griglia si riorganizza e il riquadro resta
visibile a corpo ridotto, con il proprio motivo.

---

## 6bis · I nomi dei guasti, e cosa si fa quando gli strumenti non concordano

Nato il 2026-08-23, su MACCHINA e VALVOLE. Implementato in `NOME_GUASTO` e
`nomeGuasto()` in `comune/dati.js`, usato in `a/pagina.js:636`,
`v1/pagina.js:246`, `:401` e `:839`.

**Nessuna pagina stampa `alert.fault_type`.** Quel campo vale sempre
`score_aggregation` — è la lineage tecnica dell'apertura, decisa il 2026-08-21,
perché l'allarme si apre sul punteggio e non guarda l'etichetta predetta. Per
mesi le pagine lo hanno stampato tale e quale, e un manutentore leggeva
«score_aggregation · da 10:02» su tutte e nove le valvole in allarme: una parola
interna alla macchina, e **nessun nome di guasto**.

**Il nome sta nella predizione, non nell'allarme.** `/valves` porta già
`last_prediction.predicted_label` per tutte e trentacinque, nella chiamata che le
pagine fanno comunque: nessuna route nuova, nessuna chiamata in più. I sette nomi
vivono in **un dizionario solo**, in `comune/dati.js`. Chi ne aggiunge uno lo
aggiunge lì, non nella propria pagina.

**Quando i due strumenti non concordano, lo si scrive.** Una valvola in allarme
che il modello dice sana non si maschera dietro «non classificato»: la riga
diventa **«il modello la dice sana»**. Succede oggi sulla valvola 21, ed è
informativo — avverte che lì la diagnosi automatica non regge e che l'allarme si
tiene sul solo punteggio. Vale la regola generale: **una discrepanza che insegna
qualcosa non si toglie.** Vedi `.project/OPEN_QUESTIONS.md`, M11.

**Un'etichetta che il dizionario non conosce si stampa com'è**, e una valvola
senza predizione dice «guasto non diagnosticato». Meglio una parola tecnica in
chiaro che un nome inventato.

**Sugli allarmi la data porta il giorno.** `ora()` dà il solo orario, e un
allarme aperto il 3 luglio si leggeva «da 07:05», cioè come stamattina. Sugli
allarmi si usa `giornoOra()`. `ora()` resta buono per l'età dell'ultimo dato, che
è di poche ore.

## 7 · Cosa è vietato

| vietato | perché |
|---|---|
| Prosa sullo schermo | se scrivi una frase, devi una visualizzazione al posto suo |
| Pannelli «perché», verdetti | è un display di stato, non una diagnosi |
| Classifiche di sospetti **come vista** | le valvole 9 e 21 sono anomale per costruzione: risulterebbero rotte per sempre. Il posto in classifica può stare nel suggerimento e nell'`aria-label`, mai nell'ordine dell'asse né come marchio principale |
| Punteggio di anomalia per valvola | respinto testualmente: *«non capisco cosa significa»* |
| Fondere due metri in un indice | qualità e tempo si mostrano uno per volta, ciascuno nella sua unità vera (`pc/pagina.js:804`) |
| Immagini o render della macchina | su una macchina sola è decorazione |
| Guide alla lettura, script di navigazione | se va spiegata, ha già fallito |
| Serie OEE su finestra **turno** | dente di sega da 0,79 a 0,000 ogni notte **anche su macchina sana** |
| Hero, gradienti, emoji, animazioni d'ingresso, palette sgargianti | registro respinto due volte |
| Nascondere le voci di navigazione inerti, o annotarle come tali | la barra dice dove si è, non promette pagine |
| Numeri che non vengono dall'API | nessun riempimento, nessun segnaposto, nessuna serie inventata |
| Interpolazione morbida su punti che non la misurano | il profilo del ciclo ha tre punti veri: il resto sarebbe inventato |

---

## 8 · I fatti sui dati

**I dieci fatti accertati sono in `PACCHETTO-comune.md`, sezione «Fatti sui dati».
Leggili: progettare contro di essi è obbligatorio.** Sei altri sono emersi costruendo
MACCHINA e TEMPO, e valgono allo stesso modo:

11. **I limiti XmR di `/valves/baseline` sono inutilizzabili sul singolo ciclo.**
    `ucl/lcl = media ± 2,66·MRbar` segnala 165–316 cicli su 400 fuori limite su valvole
    **sane** (293 su 400 nel caso misurato): `MRbar` misura lo scarto fra cicli
    consecutivi (σ 8,9–9,1 ms) mentre la dispersione vera è sette volte più larga (std
    70–72 ms), perché il processo deriva lentamente. Usa **media ± 3σ della baseline**. La
    forma decisa e non ancora costruita è una carta sulla **media mobile di 46 cicli**, il
    periodo dell'oscillazione deterministica della macchina.
12. **L'OEE è ribaltato rispetto all'intuizione.** Gli scenari di guasto leggono **più
    alto** del sano (sana 0,503 · guasto singolo 0,756 · deriva diffusa 0,701) perché
    quelle run non si fermano mai. Non usare l'OEE come indicatore di salute meccanica, e
    non stupirti se il guasto ha il gauge più pieno.
13. **La timeline degli stati non è ricostruibile dalle route attuali.**
    `availability_detail.by_state` dà i **totali** per stato e `source.state_transitions`
    il **numero** di transizioni — mai i loro istanti. Un asse dei tempi sotto quei
    segmenti inventerebbe l'ordine cronologico. Disegna una **ripartizione**, ordinata per
    stato OMAC, senza asse dei tempi.
14. **L'OEE di macchina sui sessanta giorni è quasi piatto**: da 0,504 a 0,473, con la
    disponibilità costante a 0,64 e la prestazione a 0,997–1,000 sempre, perché il
    simulatore non modella la perdita di velocità. Il movimento vero sta **sotto**, nella
    media delle 35 valvole. Un dominio generoso sull'OEE di macchina disegna una riga
    dritta.
15. **Le valvole 9 e 21 sono anomale dal primo giorno della run.** Sulla finestra sana:
    qualità 0,601 e filling time 2.023 ms, contro 0,769–0,826 e 1.835–1.970 ms delle
    altre. Quell'anomalia è dentro la loro stessa base, quindi il confronto «ogni valvola
    contro sé stessa» le dichiara a posto. È la ragione per cui esiste il secondo
    meccanismo di riferimento (§2).
17. **La media mobile di 46 cicli vede un gradino che la banda larga inghiotte.**
    Misurato il 2026-08-21 su 5.000 cicli per valvola (4,4 ore), `filling_time_ms`,
    confrontando la carta in uso — ciclo singolo contro media ±3σ della base — con una
    carta sulla media mobile di 46 cicli contro media ±3σ<sub>46</sub>:

    | valvola | carta in uso | media mobile 46 |
    |---|---|---|
    | 5, sana | 0% fuori | 0% fuori |
    | 9, anomala dal primo giorno | 0% | 0,1% |
    | 8, rampa di restrizione | 100% | 100% |
    | **21, ritardo di apertura** | **0%** | **100%** |
    | 13–18, instabilità di pressione | 0% | 0% |
    | 30, perdita di scansioni | 100% | 100% |

    La banda passa da ±212 ms a ±5,2 ms. Il guadagno non è meno falsi allarmi — a ±3σ la
    carta in uso non ne ha — ma **un guasto in più su quattro**, e per giunta quello su cui
    l'inferenza è muta. Le due carte restano tutte e due cieche sull'instabilità di
    pressione: va detto a schermo, non aggirato.

16. **Un guasto può essere piccolo e comune, non nascosto altrove.** L'instabilità di
    pressione sulle valvole 13–18 vale **+1,7 ms di filling time, lo 0,09%**, e non tocca
    tail time, tail pulse né conteggio impulsi. Non è invisibile perché guardiamo la
    grandezza sbagliata: è invisibile perché è piccolo e condiviso da sei valvole. Solo un
    confronto fra popolazioni lo mostra.

---

## 9 · Accesso ai dati e struttura dei file

- **Unica sorgente: le route servite dal proxy** (specchio di `pipeline/api.py`). Mai la
  fixture, mai il database, mai il simulatore. **L'elenco delle route ammesse vive dentro
  il processo del proxy**: se aggiungi una route all'API e non riavvii `server_api.py`, la
  pagina riceve un 404 e sembra che la route non esista.
- Importa `comune/dati.js`: `api`, `pct`, `num`, `etaDato`, `scenarioCorrente`. Lo
  scenario sta nel parametro `?scn=`.
- Includi `<script type="module" src="/comune/scenario.js"></script>` e un
  `id="switch-scenario"` nella barra: lo switch si monta da solo, deve restare
  raggiungibile e non deve dominare.
- Italiano, separatore decimale la **virgola** (`pct` e `num` lo fanno già).
- Il tema si applica **prima del primo disegno** con lo script inline in `<head>`, per non
  far lampeggiare la pagina. Usa una chiave `localStorage` propria della pagina
  (`tema-v7pc` su TEMPO).

## 10 · Validazione minima

Browser vero **a 1536×770 px CSS** — il viewport reale dell'utente, un monitor 1920×1080
al 125% di scala Windows. I controlli fatti a 1920×1080 sono la misura sbagliata.

Sui dati veri: la finestra iniziale, una finestra stretta, una finestra sui sessanta
giorni, e i due bordi. Sulle fixture: i sei scenari, compresi `e-macchina-ferma` e
`f-oee-degradato`. Zero errori in console. Nessun `NaN`, `undefined` o `null` a schermo.
Conta le regioni tinte (§1).

**Un gate verde non è accettazione.** Quattro versioni respinte hanno passato i propri
controlli. Solo l'utente accetta, e la prima cosa che deve vedere è la pagina che gira.
