# Roadmap IIoT — da OPC UA alla pipeline ML completa

> **Data:** 2026-08-12 · **Stato:** proposta di piano, da ratificare milestone per milestone
> **Fonte:** documento di contesto IIoT/ML/OPC UA del 2026-08-12 (§74-§79 in particolare), materiale di lavoro locale fuori dal repository
> **Prerequisiti coperti:** simulatore V3 causale (M1-M4), analytics layer (M5), ML baseline/modello (Track D, ADR-0015)

Questa roadmap porta il progetto dallo stato attuale (simulatore bulk → Parquet → analytics → ML offline)
alla pipeline completa `simulatore → OPC UA → Node-RED → MQTT → storage → ML inference → dashboard`,
seguendo l'ordine §74 del documento di contesto. Ogni milestone ha spec propria (documento locale),
ADR per le decisioni architetturali, e accettazione secondo il protocollo di accettazione (vincolante M5+).

## 0. Punto di partenza (verificato 2026-08-12)

| Componente | Stato |
|---|---|
| Simulation core (plant/sensors/PLC/validation) | ✅ fatto, **congelato** (plc.py, validation.py, config.py, plant.py, run.py) |
| Telemetria bulk → 4 parquet (cycles/events/GT/fault_timeline) | ✅ fatto |
| Scenario engine + fault injection da YAML | ✅ fatto (iniezione **solo a avvio run**, non runtime) |
| Analytics (baseline, XmR, health, detect_faulted_valves) | ✅ fatto (M5) |
| ML dataset/model/metrics/pipeline (logistic, seed separati) | ✅ fatto (offline, su parquet bulk) |
| **Modalità real-time del clock** | ❌ manca (clock ha solo tick/jump_to, nessun pacing wall-clock) |
| **OPC UA server** | ❌ da fare |
| **Fault injection a runtime** | ❌ da fare (serve per i comandi OPC UA) |
| Node-RED / MQTT / storage live / inference online / dashboard | ❌ da fare |

Decisioni d'ambiente già prese con il committente (2026-08-12):
- **Node-RED e Mosquitto in Docker locale** (Docker Desktop), stack versionato in `edge/`.
- **Collaudo OPC UA doppio binario**: test automatici pytest con client `asyncua` + checklist manuale UAExpert.
- Deliverable di piano in `docs/` + spec di milestone in locale, ADR in `docs/adr/`.

## 1. Panoramica milestone

```
M6  OPC UA server + real-time adapter          → collaudo UAExpert
M7  Node-RED edge client (Docker)              → JSON envelope validato
M8  MQTT + ingestion + raw storage             → pipeline dati vera
M9  Feature live + ML inference online         → prediction su fault iniettato
M10 Alert + API + dashboard                    → demo end-to-end
M11 (opz.) Sicurezza OPC UA + hardening        → CHIUSA NON SVOLTA (2026-08-24)
```

**Attenzione al nome.** «M11» in `.project/STATE.md` e nelle voci di
`DECISIONS.md` fino al 2026-08-23 indica la **taratura dell'allarme**, non questa
milestone. La milestone della roadmap si chiama **M11-sicurezza** ed è dichiarata
non svolta (§7).

Ogni milestone produce un **commit/artefatto verde verificato** prima di aprire la successiva.
Il ramo bulk (generazione dataset accelerata) resta intatto e convive col ramo real-time (§50-§51 del contesto).

---

## 2. M6 — OPC UA server + real-time adapter

**Spec:** spec M6 (locale) · **ADR:** 0016

**Obiettivo.** Il simulatore V3 espone un namespace OPC UA piccolo ma significativo (~25 tag:
macchina, una valvola completa, controlli simulazione), con clock real-time (o accelerato/stepped),
comandi scrivibili con causa-effetto verificabile, e fault injection a runtime.

**Deliverable.**
- `plcsim/realtime.py` — pacing loop (stessa logica bulk, avanzamento wall-clock/stepped).
- `plcsim/opcua_server.py` — server `asyncua` embedded, bridge thread-safe.
- `plcsim/serve.py` — entry point `python -m plcsim.serve` (run.py resta congelato).
- Estensione fault engine: `inject()` a runtime (scenario.py non è tra i file congelati).
- Test automatici `tests/test_opcua_*.py` (client asyncua, modalità stepped).
- Checklist UAExpert manuale (test matrix §55 del contesto).

**Collaudo.** Test pytest (connect/browse/read/subscription/write/permesso-negato/comando-stop/fault-injection/reconnect/invalid-node)
in modalità stepped riproducibile + checklist UAExpert guidata. Criteri congelati dopo calibration run
(per acceptance-protocol): correttezza e performance (pacing 1× per ≥30 min senza drift) separate.

**Uscita.** Bit-identità bulk preservata (healthy 1gg M6 ≡ M5); test matrix §55 verde su entrambi i binari.

**Rischi.** Accoppiamento asyncio↔loop simulazione (mitigato da bridge a code/snapshot);
tentazione di esporre subito 35 valvole (il contratto tag resta piccolo per scelta, espansione parametrica).

## 3. M7 — Node-RED edge client

**ADR atteso:** 0017 (envelope JSON + mapping tag centralizzato)

**Obiettivo.** Node-RED (container Docker) sostituisce UAExpert come client operativo:
subscription sui NodeId del contratto, acquisizione guidata da `DataReady`, normalizzazione
in envelope JSON comune (§17: `schema_version`, `event_id`, triade timestamp, `machine_id`,
`cycle_id`, `valve_id`, `data`, `quality`).

**Deliverable.**
- `edge/docker-compose.yml` (servizio `nodered`; predisposto per `mosquitto` a M8).
- `edge/flows/` — flow versionati in repo.
- `edge/tag-mapping.yaml` — contratto tag (§57: logical_name, node_id, datatype, unit, access, sampling_mode). **Unica fonte del mapping.**
- JSON Schema dell'envelope v1.0 + validatore Python di test.

**Collaudo.** §56 del contesto: confronto valori Node-RED vs client Python di riferimento a parità di run stepped;
verifica timestamp, DataReady (no duplicati né cicli persi), reconnect automatico, JSON conforme a schema.

**Uscita.** Per ogni ciclo valvola esposto, un JSON valido nel debug node, con `event_id` univoco.

**Rischi.** Logica che migra dentro Node-RED (vietato: è solo edge integration layer, §14);
mapping hard-coded nei flow (vietato: deve derivare dal file di mapping).

## 4. M8 — MQTT + ingestion + raw storage

**ADR atteso:** 0018 (topic/QoS/retained), 0019 (layout raw + dedup)

**Obiettivo.** §77 del contesto: `Node-RED → MQTT → consumer Python → raw Parquet`.
Prima vera pipeline dati con deduplicazione e storico partizionato.

**Deliverable.**
- Servizio `mosquitto` nel compose (config versionata).
- Topic v1 (proposta: `plant/filler01/telemetry/valve`, `.../state`, `.../alarm`; QoS 1 per eventi, retained solo per stati, §20-§21).
- `plcsim/ingest.py` (o pacchetto `pipeline/`) — consumer MQTT: dedup su `event_id` (§18), validazione schema, scrittura Parquet partizionato `data/raw/machine=filler01/date=YYYY-MM-DD/` (§33).
- Misura **budget dati** (§63): bytes/record, record/giorno, GB/giorno proiettati — allegata al report di milestone.

**Collaudo.** Run stepped a seed fisso: **parità record-per-record** tra raw MQTT e telemetria diretta del simulatore
(stessa sorgente, due percorsi). Test dedup su redelivery forzata, test burst, test broker down → buffer/retry Node-RED.

**Uscita.** Pipeline E2E verde; nessun record perso/duplicato sul run di accettazione; report budget dati.

**Rischi.** Volume dati sottostimato (mitigato dalla misura §63 prima di decidere retention);
deriva di schema (mitigata da `schema_version` + validatore condiviso M7/M8).

## 5. M9 — Feature live + ML inference online

**ADR atteso:** 0020 (feature store unico batch/online + model versioning)

**Obiettivo.** Il modello esistente (logistic, Track D) diventa servizio di inference online:
stesse feature del training (§60, niente training-serving skew), prediction storicizzate e tracciabili.

**Deliverable.**
- Feature service Python che **riusa `plcsim/analytics.py`** su finestra scorrevole live (hot path, §61).
- Model artifact con metadata completi (§47: model_version, feature_schema_version, dataset, metriche, soglia, git equivalent).
- Schema prediction (§48) + tabella su DB operazionale (candidato: PostgreSQL in compose; fallback documentato SQLite/DuckDB se si vuole restare minimi, §65).
- Inference come consumer MQTT (il ML non tocca mai il controllo macchina, §93-94).

**Collaudo.** Test E2E §54 (il test cardine del progetto): fault injection da OPC UA → KPI valvola alterati →
feature che cambiano → anomaly/prediction → record salvato. Più: test anti-skew (stesse feature batch e online
a parità di input ⇒ vettori identici); metriche detection delay / FP / FN su scenari noti (§79).

**Uscita.** Scenario `TAIL_INSTABILITY` su valvola esposta rilevato entro un delay misurato e congelato come criterio;
zero falsi positivi sul run healthy di accettazione.

**Rischi.** Leakage (§82-83: mai GT tra le feature); skew feature (mitigato dal riuso dello stesso codice);
prestazioni del modello sintetico scambiate per validazione industriale (§73: resta POC, dichiarato).

## 6. M10 — Alert + API + dashboard

**ADR atteso:** 0021 (logica alert + stack dashboard)

**Obiettivo.** §66-§68: le prediction diventano alert con logica controllabile (persistenza, isteresi,
cooldown, dedup) e una dashboard Vega-Lite coerente col baseline della tesi.

**Deliverable.**
- Alert engine (prediction → warning/alert con soglie e persistenza congelate; decisione separata dal modello, §68).
- API leggera (FastAPI) su DB operazionale: stato macchina, KPI, prediction, alert.
- Dashboard Vega-Lite (vista macchina: 35 valvole, stato, alert attivi; vista valvola: KPI + score nel tempo).

**Collaudo.** Demo E2E completa: scenario healthy (nessun alert), scenario fault (alert sulla valvola giusta,
nessun alert sulle altre), rientro del fault (alert si chiude con isteresi). Screenshot/shot in `shots/` come da prassi.

**Uscita.** Demo riproducibile da script; documentazione aggiornata (CONTEXT.md + V3-DESIGN.md).

## 7. M11-sicurezza (opzionale) — Sicurezza e robustezza

> **CHIUSA COME NON SVOLTA il 2026-08-24, per decisione dell'utente.** Il POC è
> accettato come tale: la catena gira su una sola macchina, in locale, e non è
> mai stata esposta. Non è un rinvio. Le quattro voci qui sotto restano scritte
> come la lista da riaprire **se** un giorno la catena esce da questa macchina,
> in quest'ordine. Motivazione completa in `.project/DECISIONS.md`, 2026-08-24.
>
> Da non confondere con **M11-taratura** (aggregazione K=5/N=150 e
> classificazione del modello sulla valvola 21), chiusa il 2026-08-23, che è la
> M11 a cui si riferisce `STATE.md`. Sono due cose diverse: non usare il nome
> nudo.

- OPC UA: security policy, certificati, user/password, trust store (§69) — M6-M10 girano anonymous su localhost, **dichiarato POC**.
- Broker: autenticazione, ACL sui topic.
- Fault injection di comunicazione (§72: dato congelato, duplicato, ritardo, disconnect) come scenari YAML dedicati.
- Metriche pipeline (§89: events/sec, latency, duplicate count, reconnect count).

---

## 8. Invarianti trasversali (tutte le milestone)

1. **Core congelato**: nessuna modifica a plc.py / validation.py / config.py / plant.py / run.py senza ADR esplicito che la giustifichi.
2. **Bit-identità bulk**: il fingerprint di determinismo resta definito sulla modalità bulk; la modalità real-time è
   fuori fingerprint per definizione (timing wall-clock) — i test di pipeline usano la **modalità stepped** per restare riproducibili.
3. **Ground truth separata**: la GT non è mai esposta come telemetria OPC UA né usata come feature; `SimulationControl`
   espone solo *comandi* di iniezione, non *stato* del fault engine (§31-32, §82).
4. **Acceptance protocol**: calibration → criteri congelati → acceptance, correttezza/performance separate, report da script.
5. **Contratto machine-agnostico**: dopo OPC UA, nulla nella pipeline deve sapere se dietro c'è il simulatore Python,
   PLCSIM o un PLC reale (§84-85). Il namespace OPC UA è l'API della macchina (§86).
6. **Analytics plane ≠ control plane**: il ML osserva, non controlla (§93-95).
7. **Nomi con unità** (§90) e **mapping centralizzato** (§91-92) ovunque il dato cambia formato.

## 9. Dipendenze esterne e setup

| Dipendenza | Quando | Note |
|---|---|---|
| `asyncua` (pin in requirements.txt) | M6 | server + client di test |
| Docker Desktop | M7 | compose `edge/` con nodered, poi mosquitto (M8), poi postgres (M9) |
| UAExpert installato | M6 | collaudo manuale guidato da checklist |
| `paho-mqtt` (o equivalente) | M8 | consumer Python |
| `fastapi` + `uvicorn` | M10 | API dashboard |

## 10. Metodo di lavoro per ogni milestone

1. Spec di milestone, tenuta in locale (questa roadmap è la fonte; la spec la dettaglia e la può correggere).
2. Issue atomiche, una per file, nel tracker locale.
3. Implementazione delegata a worker con packet bounded (core congelato ⇒ write scope dichiarato in spec).
4. Review indipendente + test.
5. Acceptance run secondo protocollo; report in `work/m<N>_*`.
6. Chiusura: ADR ratificati, CONTEXT.md/V3-DESIGN.md aggiornati, retrospettiva breve.

**Prossimo passo concreto:** ratifica di questa roadmap e della spec M6 → apertura issue M6-01.
