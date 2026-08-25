
# Product

> **Cos'e' questo documento, e cosa non e'.** Questo e' il **briefing di
> prodotto ratificato il 2026-08-14**, prima che la dashboard venisse
> costruita. Descrive un prodotto imperniato su un «caso diagnostico», con due
> lenti professionali e una gerarchia L0-L3.
>
> **Non e' la dashboard spedita.** Sei versioni costruite su questo briefing
> sono state respinte, e la settima, quella accettata il 2026-08-19, ha una
> forma diversa: cinque pagine affiancate, MACCHINA, VALVOLE, OEE, TEMPO e
> CARTA, senza caso diagnostico, senza lenti e senza scheda di handoff.
> Le cinque pagine sono in `dashboard/`, catturate in `dashboard/shots/`.
>
> Il documento resta qui perche' e' il termine di paragone: il racconto di cosa
> era stato pianificato e cosa e' invece sopravvissuto al contatto con chi la
> dashboard doveva usarla. Va letto come un verbale datato, non come una
> specifica da cui partire. La cronaca delle sei bocciature sta in
> `.project/RECENT_WORK.md`.

## Platform

web

## Stack

Prototipo statico HTML/CSS/JS senza framework né build (scelto per la prima versione: navigabile in locale, dati da fixture JSON fedeli al contratto API reale). Stack definitivo della dashboard di produzione deciso dopo il gate visivo M10 (delegato).

## Users

- **Manutentore meccanico senior**: opera a bordo macchina in reparto, spesso in piedi, luce variabile, urgenza produttiva. Fa leva sulla lente "Manutenzione meccanica". Domande in ordine: la produzione è compromessa? quale valvola/zona? è persistente? è anomalo vs baseline sana della stessa ricetta? quali segnali fisici indiretti? il comando PLC è regolare?
- **Programmatore PLC / automazione**: opera da postazione o in cabina elettrica. Lente "Automazione / PLC". Domande in ordine: dati validi/aggiornati/coerenti? stato macchina e ricetta coerenti? anomalia locale, multipla o globale? sequenza di controllo corretta? comando e risposta di processo coerenti?
- Non sono mai destinatari: la ground truth, i dati raw a un tecnico in urgenza, una "diagnosi automatica" imposta a un utente che non l'ha chiesta.

## Product Purpose

Dashboard di supervisione e diagnostica della riempitrice rotativa isobarica (35 valvole, 26 attive). Aiuta il tecnico a rispondere, in ordine, a due domande: **dove sembra essere il problema** (restringere a valvola / zona / asse causale in pochi secondi) e **che natura ha** (materiale per approfondire e decidere, giudizio finale alla persona competente). Non è un cruscotto KPI e non emette verdetti: contesta visivamente la decisione dell'alert engine e supporta la diagnosi umana.

## Positioning

Il meccanismo differenziante rispetto a uno SCADA o a un cruscotto OEE: un **caso diagnostico** come oggetto centrale — cosa è stato osservato, dove, da quando, quanto è persistente, quale impatto, quali evidenze, quale ambito è più pertinente, quanto è affidabile l'indicazione, quali verifiche mancano, cosa trasferire all'altro ruolo. Due lenti professionali proiettano lo stesso fatto senza mai duplicarlo (una sola sorgente dati, due proiezioni); il cambio di lente non modifica mai caso, finestra temporale, severità, allarme o fatti osservati. L'incertezza è un elemento di prima classe: la UI distingue fatti osservati / interpretazione / ipotesi / azione consigliata, e classifica ogni segnale per affidabilità (affidabile / rumoroso / incompleto).

## Operating Context

- Gerarchia visiva ratificata: L0 home macchina e OEE → L1 vista 35 valvole → L2 caso diagnostico → L3 evidenza tecnica (solo su richiesta).
- Quattro esiti possibili per l'ambito suggerito: meccanico/processo, automazione/PLC, dati/IIoT, evidenza insufficiente o problema condiviso. Mai una classificazione binaria forzata.
- Vincolo architetturale: la dashboard legge solo dal database operazionale / API read-only di osservazione (FastAPI). Non sa se dietro c'è il simulatore o un PLC reale; non legge mai il simulatore né la ground truth.
- In M10 la scheda di handoff è generata, copiabile ed esportabile; niente write-side (assegnazione, commenti, chiusura persistente).
- Scenario operativo a bordo macchina: consultazione rapida al volo E analisi lunga da postazione. Stato corrente e urgenza devono cedere il passo a una decisione in pochi secondi quando serve.

## Capabilities and Constraints

- Dominio: riempitrice rotativa isobarica con giostra a 35 valvole e 26 posizioni utili; valvole `valve0…valve34`. KPI: Filling Time (FT), Tail Time (TT), Tail Pulse (TP), Pulse Count (PC), Target (impulsi ≈ 0,1 ml), Delta Pulse, Filling Step Out (≈ FT/77), flag di ciclo (filling_ok, fill_quality_ok, sequence_ok, sample_valid, position_limit, filling_overtime), diagnostic_status, close_reason. Stati macchina OMAC: 1 Running, 2 Stopping, 3 Stopped, 4 Idle, 5 Resetting, 8 Suspended, 9 Suspending, 11 Starting. OEE = Availability × Performance × Quality su finestre shift (8h) / day (24h) rolling; response degradato con reason se dati insufficienti (mai 404).
- Tassonomia di affidabilità dei segnali: AFFIDABILE (pulse_count/delta_pulse, filling_time_ms, flag PLC, close_reason) in primo piano; RUMOROSO (tail_time_ms, tail_pulse, filling_step_out negli slot 25–26, diagnostic_status=SUSPECT) in secondo piano, sempre con banda/trend, mai valore assoluto presentato come certo; INCOMPLETO (eventi raw con whitelist, gruppi ValveGroupMap non interrogabili) solo in drill-down. Il badge di affidabilità marca la confidenza nel segnale, non la salute della valvola. SUSPECT + QualityOK è condizione di condition monitoring, mai allarme rosso (AC-D5).
- Prediction: predicted_label e anomaly_score esposti come IPOTESI (classe soggetta a rumore, con versioni modello e feature schema e probabilità), mai come verdetto. L'alert resta una decisione separata che la dashboard contesta.
- I gruppi ValveGroupMap non sono un dato operativo interrogabile: non promettere diagnosi "per gruppo PLC" finché non esiste.
- Pubblico italiano; UI in italiano con i nomi dei segnali (FT, TT, TP, Step Out, OMAC) mantenuti nel gergo tecnico reale.
- Contratti dati reali per il prototipo (verificati sul codice il 2026-08-13; il documento di lavoro resta fuori dal repository): 8 route GET; tabelle `machine_state` (KV), `machine_state_history` (OMAC append-only), `cycles` (21 colonne inclusi event_ts nullable), `predictions` (12 colonne), `alerts` (11 colonne, senza severità esplicita), `alert_transitions` (persistita ma non esposta da API); BottleCounter è il KV `bottle_counter`. Gap noti W1 (writer KV `omac_state` assente in produzione → `/machine/state` 404), W2 (probabilities solo in last_prediction), W3 (alert_transitions senza endpoint). Un problema di data quality non deve apparire come guasto meccanico (DoD).
- Vincolo di dominio: tail time e tail pulse restano segnali contestuali sensibili alla calibrazione; non devono da soli determinare l'ambito suggerito; non devono mai essere mostrati come numero assoluto per la macchina.
- Fatto/ipotesi sempre separati: "Ambito suggerito: X · Confidenza: media" con perché e non-ancora-verificato; mai "CAUSA: X".

## Brand Commitments

- Fedeltà al dominio industriale: il linguaggio (unità, nomi segnali, OMAC) viene dal dominio, non dall'inventiva del designer.
- Registro visivo scelto dall'utente (2026-08-14): **tecnico per l'operatore** — sobrio, gerarchico, tollerante all'uso a bordo macchina; l'espressione visiva non può mai oscurare compito, stato o affordance familiare (modo Operate).
- Nessun numero inventato: tutto ciò che la UI mostra deve provenire dal contratto dati o da fixture dichiarate fedeli al contratto.

## Evidence on Hand

I documenti di lavoro elencati qui sotto restano fuori dal repository: sono materiale locale, non pubblicato.

- Piano ratificato (survey agent 2026-08-14) — definisce caso diagnostico, lenti, journey, gerarchia L0–L3, scheda handoff, scenari e Definition of Done.
- Spec visiva locale (tassonomia di affidabilità, AC-D1…D5, perimetro dati).
- Wire OEE backend: specifica locale.
- Contratto dati verificato sul codice reale (route, tabelle, colonne, shape JSON, fixture fedeli).
- Glossario: `CONTEXT.md`.
- Fonti visive industriali esterne (SCADA/condition monitoring/OEE): il piano non le nomina come riferimenti estetici; da raccogliere solo come benchmark anti-pattern, in una fase successiva del piano (§9 fase 3), non vincolanti per la prima versione.

## Product Principles

1. **Il caso diagnostico è il paziente, non la pagina**: ogni livello gerarchico risponde a una decisione (devo occuparmene? dov'è? che natura? confermo o smentisco?), non a uno zoom grafico.
2. **Una sola verità, due lenti**: due proiezioni della stessa sorgente; il cambio di ruolo non cambia mai i fatti osservati né il contesto aperto.
3. **L'incertezza è informazione di primo livello**: confidenza, dati mancanti e qualità delle evidenze sono mostrati esplicitamente e mai levigati.
4. **Affidabilità del segnale ≠ salute della valvola**: la UI onora la gerarchia affidabile/rumoroso/incompleto in ogni schermata.
5. **La dashboard suggerisce, il tecnico decide**: nessun verdetto automatico; ogni suggerimento ha il suo perché e il suo non-ancora-verificato.

## Accessibility & Inclusion

- Uso in ambienti industriali con luce variabile: contrasto AA minimo, livelli WCAG rispettati per il testo operativo.
- Navigazione completa da tastiera, focus visibile, riduzione del movimento rispettata (prefers-reduced-motion), target touch adeguati per l'uso anche su tablet a bordo macchina.
- Il colore non è mai l'unico canale dell'informazione di stato (bordo/icona/testo insieme).
