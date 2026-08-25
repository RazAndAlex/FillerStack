# Grammatica della guida tecnica

Estratta dalla tappa **02 OPC UA**, approvata dall'utente il 2026-08-23 dopo il
confronto fra tre varianti indipendenti (A scelta, B e C scartate).

**Il 2026-08-23 l'utente ha scelto la variante `a` per tutte e otto le tappe e
per il viaggio del dato.** Il principio organizzativo del progetto e' quindi uno
solo: la catena di domande. Le varianti `b` e `c` restano pubblicate come
archivio del confronto, e non vanno piu' aggiornate.

Dalla tappa 03 in poi questa lingua **non e' piu' in discussione**. Le tappe
nuove la ereditano; se una tappa ha bisogno di qualcosa che qui non c'e', la
cosa si aggiunge a questo documento e a `comune/`, non al singolo file.

## 0. Cosa e' gia' deciso, e da chi

Quattro scelte di forma, prese dall'utente su anteprime funzionanti il
2026-08-23 (artefatto "Bivi del racconto"):

| Sigla | Scelta | Dove vive |
|---|---|---|
| A3 | mappa dell'impianto incollata in cima, che si accende dove sei | `comune/testa.html` + `comune/coda.html` |
| B1 | codice vero del repo, con le righe che si accendono cliccando le note | classi `.cod-blocco` `.nota` |
| C1 | scena ferma e testo che scorre | vale per il **secondo** artefatto, il viaggio del dato |
| D1 | giostra vista in pianta, che ruota | vale per il **secondo** artefatto |

C1 e D1 sono registrate qui perche' i due artefatti condividono le tinte, ma
riguardano l'altra pagina.

## 1. Le tinte vengono dalla dashboard, non da qui

Il blocco `:root` di `comune/testa.html` e' copiato da
`dashboard/comune/lessico.css`. Non si ritoccano i valori: chi passa
dalla guida alla dashboard non deve accorgersi di aver cambiato mondo.

Vale intera anche la **regola del colore** della dashboard: il colore lo prende
solo cio' che ha una gravita' o una scelta. Un valore che sta bene resta grigio.
Nella guida `--attenz` compare in quattro punti soltanto, ed e' il tetto:

1. la scritta `SEI QUI` sotto la mappa;
2. il bordo sinistro della nota aperta;
3. lo sfondo della riga di codice accesa;
4. la cella della scheda-dato che porta il numero fuori bersaglio.

Se una tappa nuova ha bisogno di un quinto punto giallo, quasi certamente il
problema e' che sta segnalando qualcosa che non e' una gravita'.

I tre temi vanno tenuti tutti e tre: `:root` chiaro, `@media
(prefers-color-scheme: dark)` protetto da `:not([data-theme="light"])`, e
`:root[data-theme="dark"]`. Il tasto in alto a destra passa da uno all'altro.

## 2. L'impianto di una tappa

Ordine fisso, dall'alto:

1. **barra di navigazione** identica a quella della dashboard;
2. **mappa dell'impianto**, otto riquadri e la scritta `SEI QUI`, incollata
   sotto la barra e sempre visibile mentre si scorre;
3. **testata**: occhiello `Guida · tappa N di 8`, `<h1>` col nome della
   tecnologia e basta, un paragrafo `.guida` che aggancia la tappa precedente,
   una riga `.min` che dice come e' organizzata la tappa;
4. **una sequenza di riquadri `.box`**, uno per passo del ragionamento;
5. **la riga `.avanti`**, che pone la domanda a cui risponde la tappa dopo.

La testata aggancia sempre la tappa precedente con un fatto, mai con una
formula. Nella 02: *"alla tappa precedente il simulatore ha calcolato un
riempimento: 1980 ms, 2505 impulsi"*.

## 3. Il principio organizzativo scelto: la catena di domande

E' la variante A. Ogni riquadro e' **una domanda che il lettore si fa davvero**,
scritta nella barra-titolo, in minuscolo, dopo il punto medio:

    Domanda 2 · quindi e' un file Python?

Le domande vanno nell'ordine in cui vengono in mente, non nell'ordine in cui la
materia e' organizzata. Nella tappa 02: che cos'e', e' un file Python, cosa
trova chi si collega, come lo provo, e se domani ci fosse una macchina vera.

L'ultima domanda di ogni tappa e' quella che **giustifica l'esistenza della
tappa** nel progetto. Nella 02 e' il confine macchina-agnostico.

Dentro il riquadro: un `<h2>` che e' gia' la risposta in una riga, poi la prosa,
poi il disegno o il codice. **La risposta non si fa aspettare.**

## 4. Il codice

Righe vere del repository, con il file e il numero di riga dichiarati sopra in
`.cod-file`. Si puo' accorciare (si scrive "accorciate") ma non si puo'
riscrivere per farlo sembrare piu' pulito di com'e'.

Il blocco `.cod-blocco` e' due colonne sopra i 900 px: codice a sinistra, note a
destra. Ogni riga del `<pre>` e' uno `<span class="r" data-r="N">`; ogni nota e'
un `<button class="nota" data-r="N">` con un titolo breve e un testo. Il
comportamento e' gia' scritto in `comune/coda.html`: la prima nota parte aperta.

Le note hanno titoli **concreti e un po' irriverenti**: "L'indirizzo di casa",
"Il cognome dei tag", "Sola lettura, e non per gentilezza". Non "Configurazione
dell'endpoint".

Da tre a sei note per blocco. Sotto le tre, il blocco non serve; sopra le sei,
il lettore smette di cliccare.

## 5. I numeri

Ogni cifra viene dal repository, dai Parquet in `data/` o dalle route GET di
`pipeline/api.py`, ed e' congelata nel file. Nessun numero inventato, che e' gia'
un impegno del progetto.

Quando un numero compare, compare **con la sua provenienza**: *"riga vera del 22
agosto: valvola 29, ciclo 417"*. La scheda-dato `.scheda-dato` chiude la tappa
con quattro o cinque numeri che la riassumono, e la cella con `.att` e' quella
che merita attenzione.

## 6. La scrittura

Il testo passa da `its-writing` (italiano) prima di pubblicare, non dopo. Vale su
ogni stringa visibile del file, comprese quelle costruite da JavaScript e gli
`aria-label`.

Regole che l'utente ha verificato contando:

- **niente em dash e niente en dash.** Dove servirebbe uno stacco si chiude la
  frase o si mette una virgola; nei separatori grafici si usa il punto medio;
  negli intervalli numerici il trattino semplice;
- niente due punti a meta' frase come connettivo, solo davanti a un elenco o a
  un esempio;
- una idea per frase, e la frase corta vince;
- si dice cosa fa il meccanismo, non che sensazione da'.

Il gergo del reparto resta col suo nome vero (FT, TT, OMAC, QoS, NodeId), ma la
prima volta che compare viene spiegato in una riga.

## 7. Accessibilita', che qui non e' un extra

Ogni `<svg>` informativo porta `role="img"` e un `aria-label` che **descrive il
fatto**, non la figura. Le note sono `<button>` veri, quindi si raggiungono da
tastiera. Il fuoco e' sempre visibile (`:focus-visible`, mai `outline:none` da
solo). `prefers-reduced-motion` spegne tutto il movimento.

## 8. Come si costruisce una tappa nuova

1. si scrive `tappe/NN-nome.html`: prima riga il `<title>`, poi il solo
   `<div class="foglio">`, senza `<style>` e senza barra;
2. `python sito/costruisci.py NN` monta il file in `build/`;
3. si pubblica `build/NN-nome.html` come artefatto e si manda l'indirizzo.

Nessuna tappa duplica le tinte o la mappa. Se qualcosa va cambiato per tutte, si
cambia in `comune/` e si rimonta.

## 9. Le otto tappe

| N | Tappa | La domanda che chiude |
|---|---|---|
| 01 | Simulatore | come fa un programma a comportarsi come una macchina? |
| 02 | OPC UA | **approvata** il 2026-08-23, variante a |
| 03 | Node-RED | chi legge, e cosa ne fa? |
| 04 | MQTT | perche' non basta leggere direttamente? |
| 05 | Mosquitto | dove finiscono i messaggi, e chi li ascolta? |
| 06 | Docker | perche' tre programmi diversi si accendono con un comando solo? |
| 07 | Machine learning | come si insegna a una macchina cosa e' normale? |
| 08 | API e dashboard | cosa vede alla fine il tecnico in reparto? |

Regola di consegna, ereditata dalla dashboard: **una tappa alla volta, mostrata
funzionante, prima di cominciare la successiva.**


## 10. Il secondo artefatto, il viaggio del dato

Vive in `viaggio/`, con i propri pezzi comuni e il proprio montatore. Eredita le
tinte e la regola del colore, non l'impianto: al posto della mappa in cima ha un
filo di avanzamento, e al posto dei riquadri ha due colonne, il testo che scorre
a sinistra e una scena ferma a destra che si trasforma (scelta C1 dell'utente).

La scena ha undici stati e li disegna un solo pezzo di codice condiviso, quindi
le tre varianti mostrano esattamente le stesse figure e differiscono soltanto
per il testo. La prima e la seconda scena sono la giostra vista in pianta che
ruota (scelta D1).

Il pubblico non ha mai sentito nominare PLC, OPC UA, MQTT o broker, quindi qui
la soglia sulla lingua è più severa che nella guida: **nessuna sigla prima
della sua spiegazione**, e le parole tecniche entrano solo dopo che il concetto
è stato detto in italiano.

## 11. Il controllo prima di pubblicare

`python sito/verifica.py` gira su tutte le pagine montate,
quelle della guida e quelle del viaggio. Blocca:

1. em dash e en dash nel testo;
2. segnaposto non sostituiti dal montatore;
3. la testa incompleta: servono `<!doctype html>`, `<html lang="it">`,
   `<meta charset>` e un `<title>` solo, in quest'ordine, più il `viewport`;
4. un `<svg>` informativo senza descrizione;
5. una nota che punta a una riga di codice che non esiste;
6. la mappa che punta fuori dalla catena, o una pagina senza mappa e senza filo;
7. risorse esterne diverse dal carattere. Un `<a>` non conta: non viene
   caricato, e la pagina regge anche se l'indirizzo è morto;
8. uno dei tre blocchi di tema mancante;
9. tag aperti e mai chiusi fra `section`, `div`, `pre` e `button`.

Il `viewport` e il `doctype` sono entrati il 2026-08-25, insieme al piede con i
collegamenti: prima la pagina non ne aveva nemmeno uno, e chi ci arrivava da un
link non aveva modo di raggiungere il codice.

La sintassi del JavaScript va controllata a parte, con `node --check` sul
contenuto degli `<script>`: il verificatore non la vede, e un apostrofo dentro
una stringa a virgolette singole ha già rotto una pagina in silenzio.


## 12. Le regole nate dalla revisione del 2026-08-23

Tre revisori con mandato ostile hanno letto tutte le pagine. Quello che hanno
trovato diventa regola, perche' altrimenti torna.

**Niente costruzione per antitesi.** «X, non Y», «non solo X ma Y», «non e' X:
e' Y». Suona spiritosa e non dice niente in piu' della meta' affermativa. Si
tiene solo la meta' che porta il fatto. E' il tic su cui l'utente si e' fermato
due volte, e ne erano rimasti diciassette dopo la prima passata.

**Niente «il modo piu' X per».** Ne' «il piu' importante», «la chiave di tutto»,
«merita attenzione», «vale la pena notare». Un titolo che annuncia invece di dire
va riscritto col fatto dentro: non «Una cosa che va detta» ma «Password in chiaro
dentro il file».

**Ogni pagina si legge da sola.** Le sigle portanti si sciolgono alla prima
comparsa **in quella pagina**, perche' il lettore puo' non aver letto le altre:
PLC, KPI, tag, QoS, OEE. Un occhiello che dice «tappa 3 di 8» non autorizza ad
agganciarsi alla tappa 2.

**Il disegno parla la lingua della prosa.** Se il testo dice *rubinetto* e
*scatti*, la figura accanto non puo' dire *valvola* e *impulsi*: il lettore crede
che siano cose diverse. Vale anche per i segni: un `-5` accanto a «cinque in
piu'» va spiegato o cambiato.

**Chi scrive non verifica.** La ripulitura si fa prima di pubblicare, e poi la
controlla qualcun altro con l'incarico di trovare, non di approvare.

**`node --check` non e' opzionale.** `verifica.py` non vede la sintassi del
JavaScript, e un apostrofo dentro una stringa a virgolette singole ha gia' rotto
tre pagine in silenzio con tutti i controlli verdi.


## 13. Le tre correzioni chieste alla scelta

**I sei strati.** Le etichette di destra ("pressione, portata, volume") partivano
da x=200 con allineamento a sinistra e finivano oltre il bordo del riquadro, che
chiude a x=310. Ora sono allineate a destra a x=298 con `text-anchor="end"`:
qualunque testo resta dentro. **Regola:** in un riquadro SVG, un testo che non
parte dal bordo sinistro si ancora al bordo destro, mai al centro sperando che ci
stia.

**Il grafico di Docker.** Il filo che collegava il consumatore Python al broker
partiva da x=130 e arrivava a x=320 in orizzontale, attraversando il riquadro di
Node-RED (x da 166 a 306). Ora scende sotto i riquadri, corre a y=128 e risale
nel bordo inferiore di mosquitto, tratteggiato per distinguerlo dal collegamento
diretto. **Regola:** un filo non passa mai sopra un riquadro che non collega.

**Il finale del viaggio.** L'ultima scena era un riassunto generico. Adesso
rispecchia la dashboard vera: la barra scura con le cinque voci (MACCHINA,
VALVOLE, OEE, TEMPO, CARTA), le 35 postazioni disposte come nella pagina VALVOLE
con la 29 in evidenza, e sotto il riquadro di dettaglio che si apre cliccandola,
con dentro il dato che ha attraversato tutte le undici tappe: 1980 ms, 2505
scatti, cinque in piu' del bersaglio, giudizio sana all'87 per cento. OEE e'
sciolto in didascalia, perche' compare nella barra e senza spiegazione sarebbe
l'unica sigla non sciolta della pagina.
