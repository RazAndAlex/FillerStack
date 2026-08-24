# ADR-0021: Storage operazionale PostgreSQL + alert engine + API + dashboard (M10)

Data: 2026-08-13 · Stato: proposto (bozza per ratifica M10)

## Contesto

M10 chiude il loop IIoT `processo → dato → storico → KPI/ML → API → visualizzazione`
(roadmap §6, contesto §66). Tre esigenze si combinano e impongono decisioni
architetturali esplicite PRIMA di implementare:

1. **Lo storico ha bisogno di una fonte operativa unica e interrogabile** (contesto §65):
   finora le prediction M9 persistevano su SQLite locale (`data/operational/predictions.db`,
   ADR-0020 §Decisione 7 "SQLite stdlib, POC"); il raw ad alta frequenza su Parquet
   partizionato (M8); la config locale app su file/SQLite. Per una API e una dashboard
   che devono rispondere a query live su KPI/prediction/alert, SQLite mono-file non basta
   (niente concorrenza multi-writer, niente tipi ricchi, niente orchestrazione nel compose).
2. **La prediction deve diventare decisione controllabile** (contesto §67–§68): in M9 lo
   `anomaly_score` è un valore, senza soglie/persistenza/isteresi/cooldown/dedup. M10 deve
   aggiungere l'alert engine come layer separato dal modello, senza toccare il modello.
3. **Serve un observation plane esposto** (contesto §84–86, §93–95): una API read-only che
   la dashboard consuma, machine-agnostic (legge dal DB, mai dal simulatore), separata dal
   control plane (OPC UA) e dall'analytics plane (ML che osserva, non controlla).

Il perimetro è fissato dalla spec M10 (`.scratch/m10/spec.md`) e dagli invarianti
trasversali (core congelato; GT mai nel percorso decisionale; bit-identità bulk). Restano
aperte per questo ADR: *dove* vive lo storico operazionale, *come* si accede (driver),
*come* evolve la persistenza delle prediction M9, e *quali* sono i confini tra prediction,
decisione (alert) e visualizzazione.

## Decisione

1. **Architettura di storage a tre livelli** (ratificata con l'utente 2026-08-13):
   - **Operazionale "caldo"** → **PostgreSQL** (compose): KPI, cicli, eventi, predictions
     ML, alert, stato macchina — fonte unica per le query live;
   - **Raw "freddo"/lungo** → **Parquet** partizionato (`data/raw/...`, M8 ADR-0019):
     telemetria grezza ad alta frequenza, per analisi/query offline — INVARIATO;
   - **Config locale app** → **SQLite**: configurazione dell'applicazione (non dati
     operativi). Il dato operativo non vive più qui.
   ```text
   Postgres  → KPI / cicli / eventi / predictions / alert / stato macchina   (attivo)
   Parquet   → telemetria grezza ad alta frequenza (storico lungo, offline)  (invariato)
   SQLite    → config locale dell'app (non operazionale)                     (invariato)
   ```
2. **PostgreSQL come backend dello storico operazionale** (inversione di ADR-0020 §Decisione 7):
   il servizio `postgres:17` entra in `edge/docker-compose.yml` (pinnato, volume persistente
   `./postgres/data`, credenziali dev `plcsim`/`plcsim` dichiarate POC, healthcheck
   `pg_isready`). ADR-0020 §Decisione 7 ("DB operazionale = SQLite stdlib") è **sostituita**:
   valeva per il POC minimo M9, ma la crescita a M10 (API + dashboard + alert persistenti)
   rende PostgreSQL la scelta corretta (§65: "Parquet + PostgreSQL" era una delle due opzioni
   esplicite del contesto). La decisione M9 di non aggiungere DuckDB/PostgreSQL resta valida
   come *progressione*: M9 si è tenuta minimale, M10 matura allo storage reale.
3. **Accesso via SQLAlchemy (driver psycopg)** (spec §2): `pipeline/storage.py` è l'UNICO
   layer di accesso allo storico operazionale, con SQLAlchemy **Core** (Table + expressions,
   niente ORM pesante). Il resto del codice si accoppia a QUESTA interfaccia, non al driver:
   il backend specifico è un dettaglio di infra. `make_engine` regola `connect_timeout` breve
   (testabilità). Nessun accoppiamento a `psycopg` diretto nel data-plane.
4. **Schema v1 (4 tabelle)** (spec §2): `predictions` (1:1 con `prediction-v1.json`, JSONB
   per `probabilities`, indici su `(valve_id, window_end_cycle_id)` e `(valve_id,
   prediction_ts)`); `alerts` (stato corrente per `(valve_id, fault_type)`, unique constraint
   `uq_alerts_valve_fault_status` per dedup); `alert_transitions` (log append-only delle
   transizioni, tracciabilità §68); `machine_state` (key/value per stato OMAC).
5. **Migrazione predictions M9 → PostgreSQL** (spec §4): `pipeline/inference.py` passa da
   `sqlite3` a `pipeline/storage.py`. Il contratto di prediction **invariato**
   (`prediction-v1.json`, `pipeline/prediction_schema.py`, campi/semantica): cambia solo il
   backend. Il watermark (`window_end_cycle_id`) e il dedup (`prediction_id`) restano identici
   nel comportamento. Anti-skew AC-M9-2 resta bit-identico (le feature non toccano il DB).
6. **Alert engine come layer separato dal modello** (spec §3, contesto §67–§68):
   `pipeline/alert.py` è logica pura (nessun DB, nessun import sklearn/polars), con parametri
   congelabili `threshold_open`, `hysteresis`, `persistence`, `cooldown_seconds`, `dedup`.
   Stati `open`/`sustained`/`closed`; isteresi (soglia di chiusura < apertura); dedup (un solo
   alert aperto per valvola+fault); ogni transizione è tracciabile in `alert_transitions`.
   Il `fault_type` deriva da `predicted_label ≠ healthy`; `healthy` non apre alert.
7. **API FastAPI read-only come observation plane** (spec §5): `pipeline/api.py` espone
   stato macchina, KPI, prediction, alert, serie score — machine-agnostic (legge solo dal DB).
   Nessuna logica di business qui: solo proiezione dello storico. `fastapi`+`uvicorn` aggiunti
   a `requirements.txt` (prima estensione del lockfile dalle milestone core, registrata).
8. **Dashboard Vega-Lite iterata con l'utente** (spec §6, contesto §66): la visualizzazione
   è l'oggetto di iterazione interattiva (input umano fondamentale per una UI), non un
   deliverable deciso a monte. Il criterio minimo demo (3 scenari) vincola il perimetro anche
   se l'estetica evolve durante lo sviluppo.

## Alternative scartate

1. **Restare su SQLite per lo storico operazionale di M10** — scartata: non regge query live
   concorrenti su API+dashboard, manca di tipi ricchi (UUID/JSONB/timestamptz), e costringerebbe
   la dashboard a leggere da file Parquet direttamente (violando l'observation plane
   machine-agnostic). PostgreSQL era già l'opzione "reale" del §65.
2. **TimescaleDB / time-series dedicato (InfluxDB/QuestDB) per la telemetria ad alta frequenza**
   — rimandata a M11+: richiede un secondo sistema per un POC; il raw ad alta frequenza resta
   su Parquet (cold), e lo storico operazionale (record/prediction/alert) è ben servito da
   PostgreSQL relazionale. Se in futuro servirà downsampling/retention nativi sulla telemetria
   raw, TimescaleDB (estensione su Postgres) è il candidato naturale.
3. **psycopg diretto senza SQLAlchemy** — scartata: SQLAlchemy Core dà astrazione e testabilità
   (il data-plane non si accoppia al driver), costo di una dipendenza ampiamente giustificato.
4. **Alert dentro il modello o nell'inference** — scartata: violerebbe la separazione
   prediction/decisione (§68); l'alert engine deve essere un layer a sé, con parametri
   congelabili e tracciabilità delle transizioni, non logica annidata nel consumer inference.
5. **Dashboard con dati embedded / lettura diretta dal simulatore** — scartata: l'invariante
   §84–85 impone che API e dashboard leggano dal DB e siano machine-agnostic; nessuna UI deve
   sapere se dietro c'è il simulatore o un PLC reale.

## Conseguenze

- **Una fonte operativa unica**: API, dashboard e alert engine leggono/scrivono SOLO da
  `pipeline/storage.py` (PostgreSQL); il raw resta Parquet, la config resta SQLite. Nessun
  codice nuovo legge prediction da SQLite.
- **Retrocompatibilità delle prediction**: il contratto `prediction-v1.json` è intatto;
  l'unica rottura è il backend (SQLite→PostgreSQL). Un eventuale `pipeline/migrate_predictions.py`
  copia i record esistenti dallo storico SQLite M9 (se presente) al nuovo store.
- **Anti-skew invariato**: AC-M9-2 (bit-identità batch≡live) continua a valere; la migrazione
  del backend non tocca le 43 feature né lo z-score.
- **Separazione netta prediction/decisione**: lo `anomaly_score` resta un valore; l'alert
  engine decide gli stati con parametri congelati e transizioni tracciate — verificabile
  deterministicamente sui tre scenari demo (healthy/fault/rientro).
- **Nuove dipendenze** (registrate in `requirements.txt`): `sqlalchemy`, `psycopg[binary]`,
  `fastapi`, `uvicorn[standard]` (+ `httpx` test-only per il TestClient FastAPI).
- **Test a due livelli**: logica pura (alert engine, schema, prediction record) sempre attiva;
  test che toccano PostgreSQL marcati `postgres` e skippati senza server (avvio: 
  `docker compose up -d postgres`); un DB di test dedicato `plcsim_test` isola i test CRUD.

## Riferimenti

- `.scratch/m10/spec.md` (§1 storage, §2 schema, §3 alert, §4 migrazione, §5 API, §6 dashboard, §7 collaudo, §8 criteri)
- `docs/roadmap-iiot.md` §6 (M10) · `docs/adr/0020-feature-store-unico-model-versioning.md` §Decisione 7 (invertita)
- `pipeline/prediction_schema.py` + `edge/schemas/prediction-v1.json` (contratto prediction invariato)
- `pipeline/inference.py` (backend migrato) · `pipeline/alert.py` · `pipeline/storage.py` · `pipeline/api.py`
- `work/acceptance-protocol.md` (calibration → freeze → acceptance) · `Proposte/contesto_..._2026-08-12.md` §65–§68
