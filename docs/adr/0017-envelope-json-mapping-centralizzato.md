# ADR-0017: Envelope JSON v1.0 + tag-mapping centralizzato; watermark senza ack (emendamento T6: null per partial)

Data: 2026-08-12 · Stato: ratificato

## Contesto

La pipeline IIoT (roadmap §3) prosegue con l'edge integration layer: Node-RED
(container Docker) sostituisce UAExpert come client operativo del server OPC
UA M6 (ADR-0016), acquisisce i cicli valvola guidata da
`DataReady`/`CycleCounter` e li normalizza in un formato stabile e versionato
per il consumer M8 (MQTT → Parquet). Tre decisioni interdipendenti definiscono
il contratto di confine macchina↔IT:

1. **Formato dei record**: l'envelope JSON v1.0 (spec M7 §4.2, contesto §17)
   — schema comune versionato (`schema_version` in ogni record, §58),
   validabile, con `ingest_ts` riservato (assente in v1.0, obbligatorio da M8).
2. **Fonte del mapping**: i NodeId non devono mai essere hard-coded nei flow
   (roadmap §3 rischio 2, §91-§92) — serve un'unica fonte versionata
   (`edge/tag-mapping.yaml`, formato §57) da cui generare deterministicamente
   l'artefatto caricato dai flow (`tag-mapping.js`).
3. **Modello di acquisizione**: M6 §5 rimandava a M7 la decisione
   sull'ack/gestione del pulse `DataReady` (che dura un solo scan, 10 ms, e
   può essere perso da un client lento, spec M6 §5).

## Decisione

1. **Envelope JSON v1.0 congelato in `edge/schemas/envelope-v1.json`** —
   struttura §4.2: `schema_version`, `event_id` (uuid v4), `event_type`
   (`valve_cycle`), `event_ts` (orologio edge), `source_ts` (ISO8601 UTC;
   in M6 non esiste un tag "tempo simulazione" esposto: il test usa un'ancora
   deterministica, il flow reale userà il ServerTimestamp del blocco con
   fallback `event_ts`), `machine_id`, `cycle_id` (LastCycleId), `valve_id`,
   `data` (12 campi, chiavi = logical_name del mapping), `quality`
   (`valid`/`completeness`). `recipe_id` NON in v1.0 (nessun tag ricetta nel
   namespace M6) — aggiunta possibile in v1.1, documentata nello schema come
   campo futuro. `ingest_ts` riservato e rifiutato dal validatore in v1.0.
   **Emendamento T6 ratificato in calibration**: i 12 campi `data.*`
   ammettono `null` — un tag non leggibile (qualità OPC UA cattiva) produce
   `null` nel campo, `quality.valid=false`, `completeness='partial'`, e il
   record resta VALIDO per lo schema: nessun ciclo sparisce dalla sequenza e
   il consumer può contarli (metriche §89).
2. **Mapping centralizzato: `edge/tag-mapping.yaml` unica fonte** (15 voci:
   2 trigger `machine.*` + 13 `Valve01.*`, NodeId REALI di
   `plcsim/opcua_server.py`, ns=2). `edge/scripts/build_tag_mapping.py` genera
   `edge/tag-mapping.js` con conversione deterministica (stesso yaml ⇒ stesso
   js, byte-identico — T0 lo verifica con sha256). I flow caricano il js
   all'avvio (mapping loader) e NON contengono NodeId hard-coded (AC-M7-4:
   0 occorrenze `ns=2;s=` nei flow). La chiave della voce yaml =
   `logical_name` = chiave di `data.*` dell'envelope; dal prefisso `valveNN`
   si deriva `valve_id`.
3. **Acquisizione watermark, niente ack server (v1)** — flusso §4.3:
   (1) init: `watermark = CycleCounter` corrente alla prima notifica (nessun
   backfill dei cicli chiusi durante il down — il server M6 non ha storico);
   (2) trigger: subscription su `DataReady` (reattività) E `CycleCounter`
   (fonte di verità — recupero anche col pulse perso); (3) check
   `CycleCounter > watermark` ⇒ nuova chiusura; (4) lettura blocco `Valve01.*`
   dal mapping; (5) build envelope; (6) publish (debug + test-sink); (7)
   `watermark = CycleCounter`. Reconnect/epoch: se `CycleCounter < watermark`
   (server riavviato, contatori a 0) ⇒ nuovo epoch, `watermark =
   CycleCounter` corrente + log "epoch reset" — nessun duplicato tra run.
   L'ack server (handshake §15) resta documentato come opzione futura di
   robustezza, NON implementato in v1. Limite documentato (burst): il server
   espone solo l'ultimo ciclo chiuso — finestra di lettura > cadenza cicli ⇒
   gap loggati (blocchi intermedi non leggibili), mai duplicati; il watermark
   converge sempre al `CycleCounter` corrente (verificato in T5: 85 gap su
   finestra 1000 scan, gap-set identici client/oracle).

## Conseguenze

- Il confine macchina↔IT resta l'OPC UA come API della macchina (§86);
  Node-RED è SOLO edge integration layer (acquisisci-mappa-normalizza-timestampa-
  impacchetta), mai logica di dominio nei flow (roadmap §3 rischio 1).
- M7 è additiva: solo `edge/` + test + docs; il core `plcsim/*` e M6 non
  vengono toccati ⇒ bit-identità bulk preservata per costruzione e verificata
  dallo stesso test della regressione (AC-M7-0, metodo AC-M5-1).
- Il mapping yaml diventa il punto unico di modifica per l'espansione a 35
  valvole (M9+): aggiunta di voci + `--exposed-valves`, nessun cambio di flow.
- Il formato v1.0 è congelato: `ingest_ts` arriva con M8 (consumer), `recipe_id`
  eventualmente in v1.1; ogni evoluzione richiede nuova versione di schema e
  ADR.
- Il watermark in-memory (flow context) implica: un riavvio del container è
  un nuovo epoch (nessun backfill, nessun duplicato) — la persistenza non
  aggiunge garanzie in v1 ed è rimandata (spec §10 Q1).
- L'emendamento T6 (null per partial) estende la semantica `quality` rispetto
  al testo originale spec §4.2: policy congelata in calibration
  (`work/m7_acceptance/calibration.md`) e ratificata con questo ADR.
- I criteri Docker-dipendenti (checklist manuale §7 = AC-M7-3, latenza
  `event_ts − source_ts` = AC-M7-5) restano BLOCCATI DA AMBIENTE finché
  Docker Desktop non è installato: il metodo di collaudo è documentato
  (`work/m7_acceptance/calibration.md` §4, `checklist_manual.md`) e
  l'esecuzione è il primo passo a Docker installato.
- Sicurezza OPC UA (certificati, policy) resta rimandata a M11; il collaudo
  M7 gira anonymous su localhost, dichiarato POC (spec M6 §7).

## Riferimenti

Le fonti citate come `.scratch/...`, `work/...` e `Proposte/...` sono documenti
di lavoro locali: restano fuori dal repository pubblicato (vedi ADR-0023).

- `docs/roadmap-iiot.md` §3-§4 · `Proposte/contesto_progetto_IIoT_ML_OPCUA_pipeline_aggiornato_2026-08-12.md` §14-§21, §55-§58, §84-§87, §89-§92
- `.scratch/m7/spec.md` §2-§6 (contratto M7) · `work/acceptance-protocol.md` (calibration → freeze → acceptance)
- `work/m7_acceptance/calibration.md` (evidenza di calibration; emendamento T6)
- `work/m7_acceptance/criteria_frozen.md|json` (criteri congelati 2026-08-12T13:13:50)
- `docs/adr/0016-opcua-server-asyncua-realtime-fuori-fingerprint.md` (server M6, base del contratto)
