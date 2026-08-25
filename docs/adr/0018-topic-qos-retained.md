# ADR-0018: Topic MQTT v1 / QoS / retained (contratto macchina-agnostico)

Data: 2026-08-12 · Stato: proposto (bozza per ratifica M8)

## Contesto

M8 è la prima vera pipeline dati del progetto (contesto §77): `Node-RED → MQTT → consumer Python → raw Parquet`. L'envelope JSON v1.0 è già congelato (ADR-0017), ma il **livello di trasporto** tra edge e servizi IT (contesto §19) non ha ancora un contratto: la struttura dei topic è "una possibile struttura, ancora da definire" (contesto §20) e QoS/retained restano da decidere prima che il branch MQTT del flow (issue M8-03), il broker nel compose (issue M8-01) e il consumer (spec §6) vengano implementati. La roadmap §4 elenca ADR 0018 (topic/QoS/retained) e 0019 (layout raw + dedup) come ADR attesi di M8; l'handoff M8 registra tra le decisioni già prese il topic `plant/filler01/telemetry/valve` e QoS 1.

Tre ragioni impongono il congelamento del contratto PRIMA dell'implementazione:

1. **Confine macchina-agnostico** (spec §2.5, contesto §84-§85): dopo OPC UA nulla deve sapere se dietro c'è il simulatore Python, PLCSIM o un PLC reale — i topic non devono contenere dettagli simulatore (`sim`, `plcsim`, `python`); il namespace OPC UA resta l'API della macchina (contesto §86).
2. **QoS 1 ⇒ dedup obbligatoria** (contesto §18): at-least-once può riconsegnare lo stesso messaggio; serve `event_id` come chiave di dedup lato consumer — decisione che condiziona il consumer (spec §6.3) e i criteri di accettazione (AC-M8-1/3).
3. **Retained solo dove ha senso** (contesto §21): un evento di riempimento NON è retained (il replay sarebbe un falso duplicato), uno stato corrente sì (un nuovo subscriber conosce subito lo stato corrente senza aspettare il prossimo cambio).

## Decisione

1. **Topic v1 congelati** (spec §4.1; da confermare in calibration M8, spec §10):

| Topic | Payload (schema) | QoS | Retain | Uso in M8 |
|---|---|---|---|---|
| `plant/filler01/telemetry/valve` | envelope v1.0 wire / v1.1 stored (`event_type=valve_cycle`) | **1** | **false** | OBBLIGATORIO (pubblicato dal flow, consumato e persistito) |
| `plant/filler01/state` | stato macchina (JSON minimo, spec §4.3) | **1** | **true** | Contratto congelato; pubblicazione/salvataggio OPZIONALI in M8 (default: solo contratto) |
| `plant/filler01/alarm` | da definire (M10) | **1** | false (se usato) | Topic RISERVATO: nessun payload pubblicato in M8 |

2. **Gerarchia e granularità**: `plant/<machine_id>/<categoria>` (contesto §20). Il livello evento-ciclo è `telemetry/valve`: la granularità per-valvola resta NEL PAYLOAD (`valve_id` nell'envelope), non nel topic — non serve decidere ora `plant/filler01/valve/12/cycle`. `state` è lo stato corrente della macchina; `alarm` è riservato agli alert operativi M10. `plant` è il livello di impianto, `filler01` è il `machine_id` dell'envelope: topic **machine-agnostici** (spec §2.5), nessun riferimento a simulatore/PLCSIM.

3. **QoS e retained — riepilogo decisionale** (spec §4.2):
   - Eventi (`telemetry/valve`): **QoS 1, retain false** — at-least-once con dedup su `event_id` (contesto §18), idempotenza totale lato storage (spec §6.3).
   - Stati (`state`): **QoS 1, retain true** — l'ultimo valore resta sul broker (contesto §21).
   - Allarmi (`alarm`): QoS 1, retain false se usato (in M8 nessuna pubblicazione).
   - Broker: **mosquitto:1883 plaintext, nessuna autenticazione in M8** — POC dichiarato (handoff M8; hardening a M11 con auth/ACL, roadmap §7).

4. **Payload e wire format** (spec §4.3): wire (edge → broker) = envelope **v1.0 compatto** (JSON senza indentazione, una riga per record; `schema_version="1.0"`, `ingest_ts` ASSENTE — campo riservato, rifiutato se presente); stored (consumer → Parquet) = envelope **v1.1** con `ingest_ts` iniettato dal consumer e `schema_version="1.1"` (spec §5.3). **Un messaggio = un record valvola**: niente batch/array sul wire (spec M7 §4.2). `machine_id` nel payload resta `filler01` (coerente col topic); `valve_id` nel payload, non nel topic (M8: una sola valvola esposta).

## Alternative scartate

1. **QoS 0** — rifiutato: perdita possibile tra edge e broker; il watermark M7 protegge solo il tratto OPC UA→edge, non il trasporto MQTT; un evento perso è un ciclo perso per lo storico (il server M6 non ha storico, nessun backfill). QoS 1 è il minimo che garantisce consegna almeno-una-volta fino al broker.
2. **QoS 2** — non necessario: handshake a 4 passi, costo e latenza maggiori; il consumer deduplica comunque su `event_id` (contesto §18), quindi QoS 1 + dedup raggiunge exactly-once logico al costo minore (spec §1).
3. **Retain su telemetry** — rifiutato: un retained message verrebbe consegnato a ogni nuovo subscriber come se fosse un evento corrente; il replay di un ciclo di riempimento è un falso duplicato (contesto §21). Retained SOLO per gli stati.
4. **Batch (array di record per messaggio)** — rifiutato: un messaggio = un record (spec M7 §4.2); il batch complicherebbe dedup (`event_id` per elemento), validazione e metriche, e il volume M8 (ordine di MB/giorno, spec §9) non richiede aggregazione sul wire.
5. **Topic per-valvola** (`plant/filler01/valve/12/cycle`) — scartato per v1: la granularità resta nel payload (`valve_id`), il topic è stabile e indipendente dal numero di valvole esposte (espansione a 35 valvole in M9+ senza nuovi topic; contesto §20: "non è necessario decidere subito la granularità"). L'opzione resta documentata come possibile evoluzione futura, non un vincolo.

## Conseguenze

- **Flow Node-RED (issue M8-03)**: nodo `mqtt out` in coda al builder envelope v1.0 di M7 — topic `plant/filler01/telemetry/valve`, QoS 1, retain false, broker `mosquitto:1883` (plaintext, no auth, POC); il branch non introduce alcun `ns=2;s=` (AC-M7-4 resta valido, grep T0); il record MQTT deve essere IDENTICO al record su `out.jsonl` (T1).
- **Consumer (spec §6)**: subscribe QoS 1 con sessione persistente (`clean_session=False`, client id fisso `plcsim-ingest-v1`); **PUBACK dopo il flush** (mai ack prima di scrittura atomica + aggiornamento dedup set) per non perdere messaggi tra ack e flush; dedup su `event_id` obbligatoria (AC-M8-3). Il topic `state` NON è consumato in M8 (default: solo contratto).
- **Dedup come conseguenza diretta del QoS 1**: at-least-once ⇒ redelivery possibile ⇒ `event_id` è la chiave di idempotenza (contesto §18); senza retain su telemetry le redelivery restano limitate alla finestra QoS del broker e non vengono mai "rifiutate a valle" come falsi eventi.
- **Hardening M11**: il POC plaintext (`allow_anonymous`, listener 1883, spec §7.2) è dichiarato e sarà sostituito da autenticazione + ACL sui topic (roadmap §7); il contratto topic v1 non cambia con l'hardening — la gerarchia `plant/<machine_id>/<categoria>` è già pronta per ACL per-macchina.
- **Machine-agnosticismo**: nessun topic contiene dettagli simulatore; il contratto regge la sostituzione del simulatore con un PLC reale (contesto §85) senza cambi di topic né di consumer.
- **Vincolo di formato**: la v1.0 wire resta congelata e immutata (ADR-0017); la v1.1 esiste SOLO come formato stored (iniezione `ingest_ts` lato consumer, spec §5.3) — l'edge non genera mai `ingest_ts` (spec §5.1, §58).
- **Calibration M8**: topic/QoS/retained finali congelati in calibration (spec §10); la domanda aperta spec §12.2 (topic `state`: solo contratto o anche pubblicazione + `machine_state.parquet`) ha proposta default "solo contratto" — se attivata, è issue separata senza cambiare questo contratto.

## Riferimenti

Le fonti citate come `.scratch/...`, `work/...` e `Proposte/...` sono documenti
di lavoro locali: restano fuori dal repository pubblicato (vedi ADR-0023).

- `docs/roadmap-iiot.md` §4 (ADR attesi 0018/0019; proposta topic/QoS/retained) · §7 (hardening M11: auth/ACL)
- `.scratch/m8/spec.md` §4 (contratto MQTT), §5.1-§5.3 (v1.1 delta, validazione wire/stored), §6 (consumer, dedup), §7 (flow MQTT + compose mosquitto), §10 (calibration)
- `Proposte/contesto_progetto_IIoT_ML_OPCUA_pipeline_aggiornato_2026-08-12.md` §18-§21 (event_id/dedup, ruolo MQTT, topic, retained), §58 (schema versionato), §77 (M8), §84-§86 (contratto macchina-agnostico, namespace OPC UA)
- `.scratch/handoffs/plc-sim-v3-handoff-2026-08-12-m8-mqtt.md` (decisioni già prese: topic `plant/filler01/telemetry/valve`, QoS 1; broker plaintext POC)
- `.scratch/m8/issues/01-mqtt-broker-compose.md` (broker mosquitto + build step palette) · `.scratch/m8/issues/03-node-red-mqtt-publish.md` (publish MQTT del flow)
- `docs/adr/0017-envelope-json-mapping-centralizzato.md` (envelope v1.0, base del contratto wire) · `docs/adr/0016-opcua-server-asyncua-realtime-fuori-fingerprint.md` (server M6, confine macchina↔IT)
