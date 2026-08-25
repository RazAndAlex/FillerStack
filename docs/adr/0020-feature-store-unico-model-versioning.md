# ADR-0020: Feature store unico batch/online + model versioning (inference M9)

Data: 2026-08-13 · Stato: proposto (bozza per ratifica M9)

## Contesto

M9 porta il modello ML (Track D / ADR-0015, offline su Parquet bulk) a **inference online** (roadmap §5, contesto §77-§80). Due esigenze indipendenti impongono una decisione architetturale esplicita PRIMA di implementare:

1. **Serve una sola definizione delle feature** (contesto §60): idealmente il codice feature è unico e alimenta sia il batch training sia l'inference online; reimplementare le feature nel feature service (o peggio in Node-RED) produrrebbe **training-serving skew** (§60) — il modello vedrebbe in produzione vettori calcolati con codice/logica diversi da quelli del training. Lo schema feature è già congelato (ML-F1, `work/ml-feature-schema.md`, **43 feature esatte**), e l'estrattore è `plcsim/ml_dataset.py` (`window_cycles`, `compute_window_features`, z-score `normalizer_from_manifest`). Il feature service live deve **riusare** quel codice, non duplicarlo.
2. **Serve tracciabilità del modello in ogni prediction** (contesto §47-§48/§80): una prediction storicizzata deve poter essere ricondotta al model artifact che l'ha prodotta (versione modello, versione schema feature, dataset, soglia) — proprio perché il modello è uno "scenario POC su dati sintetici" (§73): senza `model_version`/`feature_schema_version` la prediction è inaffidabile a posteriori.

Il perimetro è fissato dalla spec M9 §4-§6 (feature service live con riuso, prediction schema, inference consumer read-only) e dagli invarianti (core congelato; GT mai nel percorso decisionale; analytics plane ≠ control plane). Restano aperte per questo ADR: *come* garantire il riuso (import vs copia), *dove* vivono feature service/inference, *come* codificare il versioning del modello, e *quale* DB operazionale.

## Decisione

1. **Feature store unico via riuso (import, non copia)** (spec §4.1, contesto §60): `pipeline/features.py` importa direttamente `plcsim/ml_dataset.py` (`window_cycles`, `compute_window_features`, `transform_zscore`, `normalizer_from_manifest`) e `plcsim/ml_model.py`. **Nessuna reimplementazione** delle 43 feature; il mapping `data.*` dell'envelope v1.1 (`pipeline/ingest.py`) → colonne `valve_cycles` attese da `compute_window_features` è un adattatore esplicito e testato (1:1, anti-skew), non un secondo estrattore. La *single source of truth* dello schema feature resta `work/ml-feature-schema.md` (ML-F1); l'unico codice che lo materializza resta `ml_dataset.py`.
2. **Anti-skew come invariante verificabile** (AC-M9-2): a parità di sequenza di 50 cicli, il vettore 43 feature prodotto dal percorso **batch** (`build_dataset`/`compute_window_features`) e dal percorso **live** (`pipeline/features.py`) è **bit-identico** (Float64, stessa guardia σ=0 ⇒ z=0, stesso z-score da `model/zstats.json`). Una differenza è un FAIL di correttezza da investigare prima di ogni altro giudizio (stessa gerarchia della bit-identità bulk, ADR-0016).
3. **Finestra di feature: emissione a unità finestra (N=50)** (spec §4.1, §11 Q2): il feature service emette un vettore feature alla chiusura di ogni finestra piena di 50 cicli consecutivi per valvola (`window_idx = (cycle_id−1)//50`, drop della coda parziale), coerente col training a unità finestra. Prima di 50 cicli nessuna feature (il training DROP analogamente). Deterministica, nessun RNG.
4. **Model versioning nel sidecar del model artifact** (spec §6.2, contesto §47/§80): il sidecar JSON di `MLModel.save` (`model.joblib.json`) — già con `schema_version`, `kind`, `random_state`, iperparametri, `classes` — è esteso (additivo, non rompe `MLModel.load`) con `model_version` (derivato dal `code_version` del manifest o da un tag esplicito) e `feature_schema_version` (riferito a ML-F1). Ogni record di prediction riporta **obbligatoriamente** `model_version` + `feature_schema_version` (spec §5): niente prediction senza provenienza.
5. **Prediction schema v1** (spec §5, contesto §48): record `{schema_version, prediction_id (uuid), model_version, feature_schema_version, prediction_ts, machine_id, valve_id, window_idx, window_end_cycle_id, predicted_label (7 classi), anomaly_score (1−P(healthy)), probabilities (per classe), feature_fingerprint (sha256 dei 43)}` — `additionalProperties:false`, formati `uuid`/`date-time` validati col `FormatChecker` esplicito (pattern `pipeline/validator.py`). Lo `anomaly_score` separa la **predizione** (probabilità) dalla **decisione** (soglia/isteresi a M10, contesto §67-§68).
6. **Inference come consumer read-only sul raw** (spec §6, contesto §93-§95): `pipeline/inference.py` carica `MLModel.load(model.joblib)` + `model/zstats.json` (nessun refit), legge il raw già persistito via **scan incrementale su watermark `cycle_id`** (§11 Q4), calcola le feature via `features.py`, predice, persiste su **SQLite locale** (`data/operational/predictions.db`, contesto §65) e pubblica su `plant/filler01/prediction` (QoS 1, retain false — topic già riservato in M8, attivato in sola emissione). Nessuna write su OPC UA / broker di controllo: il ML osserva, non controlla.
7. **DB operazionale: SQLite locale (stdlib)** (contesto §65): niente PostgreSQL né DuckDB in M9 — `sqlite3` (stdlib, zero dipendenze esterne) + Parquet è sufficiente per il POC locale; il DB file unico `data/operational/predictions.db` con dedup su `prediction_id` e watermark su `window_end_cycle_id`. PostgreSQL/DuckDB restano candidati per un eventuale M9bis.

## Alternative scartate

1. **Feature reimplementate in Node-RED o in un secondo modulo** — scartata: violerebbe il §60 (training-serving skew), duplicherebbe la logica dei 43 feature e romperebbe il vincolo di singola definizione; il riuso via import garantisce bit-identità (AC-M9-2) e manutenibilità.
2. **Feature calcolate via SQL/duckdb sul raw** — scartata per lo stesso motivo: riaggregazioni SQL temporali (§59) reimposterebbero la semantica (slope ai minimi quadrati, z-score per-valvola, whitelist eventi) in un dialetto diverso; le feature restano in Polars/`ml_dataset.py`, DuckDB serve solo lo storage delle prediction.
3. **DB PostgreSQL per le prediction in M9** — scartata per il POC: il §65 ammette esplicitamente Parquet + DB locale (DuckDB/SQLite) senza "cloud artificiale"; PostgreSQL aggiunge orchestrazione nel compose e non serve al test cardine §54.
4. **Model version assente / derivata a runtime dal path** — scartata: senza `model_version` + `feature_schema_version` in ogni prediction la tracciabilità (§80) si rompe; il sidecar è la fonte, la prediction la riporta immutata.
5. **Inference che ri-sottoscrive direttamente MQTT** — scartata per M9: il raw già persistito da `ingest.py` è la singola fonte di verità storica; lo scan incrementale su watermark evita una seconda sottoscrizione e un secondo percorso di dedup; l'eventuale hot-path in-push resta evoluzione futura.

## Conseguenze

- **Riuso vincolante**: ogni modifica futura allo schema feature è SOLO in `work/ml-feature-schema.md` (ML-F1) + `ml_dataset.py`, e ricade automaticamente su batch e online (single source). Il feature service M9 non introduce nuove feature né nuove soglie di feature.
- **Anti-skew come AC permanente**: AC-M9-2 (bit-identità batch≡live) diventa un invariante di regressione per ogni milestone successiva che tocca feature/model — come la bit-identità bulk (ADR-0016) per il core.
- **Tracciabilità**: `model_version`/`feature_schema_version` obbligatori nel sidecar e in ogni prediction ⇒ ogni record è riconducibile al training (dataset version, soglia, seed, §80); il `feature_fingerprint` chiude il loop input→output per il debug.
- **Separazione predizione/decisione**: lo `anomaly_score` in M9 è un valore, non un alert; isteresi/cooldown/dedup/persistenza della decisione restano M10 (contesto §67-§68) — M9 non introduce logica di alert.
- **Scope M9**: solo `pipeline/features.py`, `pipeline/inference.py`, `pipeline/prediction_schema.py`, `edge/schemas/prediction-v1.json`, test, ADR; `data/operational/` è output runtime (non versionato). Core congelato, M6/M7/M8 non toccati. `TAIL_INSTABILITY` del §54 NON è un nuovo FaultType engine: è uno scenario M9 mappato su `closing_delay` esistente (nessuna modifica a `scenario.py`/`FAULT_TYPES`).
- **Verificabilità**: AC-M9-2 (anti-skew), AC-M9-3 (prediction record 100% validi + mutazioni rifiutate), AC-M9-4a/b/c (detection su fault iniettato / delay / FAR healthy), AC-M9-5 (versioni tracciabili) sono gli effetti verificabili; T0 (schema+riuso), T1 (anti-skew), T2 (schema), T3 (E2E §54) della spec §7.

## Riferimenti

Le fonti citate come `.scratch/...`, `work/...` e `Proposte/...` sono documenti
di lavoro locali: restano fuori dal repository pubblicato (vedi ADR-0023).

- `.scratch/m9/spec.md` §4 (feature service), §5 (prediction schema), §6 (inference), §2 (invarianti), §8 (accettazione), §11 (domande aperte)
- `work/ml-feature-schema.md` (ML-F1, 43 feature congelate) · `work/ml_dataset/manifest.yaml` (manifest v0)
- `plcsim/ml_dataset.py` (window_cycles/compute_window_features/transform_zscore) · `plcsim/ml_model.py` (MLModel.save/load, sidecar)
- `pipeline/ingest.py` + `pipeline/validator.py` (raw v1.1, pattern FormatChecker)
- `docs/adr/0015-layer-ml-*.md` (Track D) · `docs/adr/0016-opcua-server-asyncua-realtime-fuori-fingerprint.md` (stepped per test) · `docs/adr/0019-layout-raw-dedup.md` (layout raw M8)
- `Proposte/contesto_progetto_IIoT_ML_OPCUA_pipeline_aggiornato_2026-08-12.md` §47-§48 (model metadata, prediction schema), §59-§61 (dove calcolare feature, una sola definizione, hot/cold path), §65 (DB locale), §77-§80 (milestone, test E2E, riproducibilità), §93-§95 (analytics plane)
- `docs/roadmap-iiot.md` §5 (ADR atteso 0020) · `work/acceptance-protocol.md` (calibration → freeze → acceptance)
