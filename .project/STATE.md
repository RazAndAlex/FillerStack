# Project state

Updated: 2026-09-02

> **Cited sources.** Many entries point at working documents under
> `.scratch/`, `work/`, `Proposte/`, `feedback/` and the `HANDOFF-*.md`
> files. Those are local, git-ignored material: they are not part of the
> published repository, and the paths resolve only in a working copy.

> Le sezioni sono in ordine cronologico e **l'ultima vince**. Il paragrafo
> «Current objective and milestone» qui sotto e' del 2026-08-13 e descrive uno
> stato superato: per lo stato di oggi si legge l'ultima sezione, «Stato al
> 2026-08-30».

## Project

FillerStack is the whole chain behind an isobaric rotary can-filler. It models a
35-valve carousel (26 active filling positions), virtual sensors, a virtual PLC,
cycle validation, telemetry, analytics, machine-learning inference, and an IIoT
path, ending in a supervision dashboard.

The name changed on 2026-08-24: «PLC Sim V» named the instrument and readers
consistently took the project for a simulator and nothing else. Internal
identifiers — the `plcsim` package, the `plcsim-*` containers, the MQTT client
id — were deliberately left alone: nobody reads them as the project's name, and
renaming them touches two frozen core files for no gain. Ground truth remains separate from every operational input used
by the virtual PLC or ML.

Canonical GitHub repository: `RazAndAlex/FillerStack`,
<https://github.com/RazAndAlex/FillerStack>. Local `main` tracks `origin/main`.
Publication mode is `full-source` (see `.project/PUBLICATION.yaml`): the
project source, the dashboard and the site are all in the published history.
What stays out is working material and regenerable output: datasets, run
outputs, the local issue tracker, handover notes, and local infrastructure
state. The memory-only policy this file described until 2026-08-24 was
superseded when the code entered the repository.

## Current objective and milestone

The current milestone is the visual completion of M10: a data-backed supervision
and diagnostic dashboard with a machine-level OEE home (L0), a 35-valve machine
view (L1), and valve drill-down (L2/L3). The non-visual M10 scope—PostgreSQL
operational storage, alert engine, read-only FastAPI observation API, and the M9
prediction migration—was documented as accepted on 2026-08-13.

Alert engine status (2026-08-21): the operational default is score-only
K=5/N=150 per valve, with the label-independent `score_aggregation` lineage and
`threshold_open=0.5`. Full-history simulation selected this setting because it
kept zero false positives, detected 9/9 injected faults, and kept valve 21 active
for 93.0% of the run with 8 openings. K=5/N=100 kept valve 21 active for 70.8%
with 33 openings. The measured delay change across nine valves had a maximum
degradation of +0.4 s and improved valve 21 by 2,080.69 s (-34 min 40.69 s).
The N=150 history read 5,250 rows for 35 valves, with a 9.811 ms warm median
versus 7.625 ms for N=100.
The operational alert store was rebuilt transactionally from 723,110 persisted
predictions after a verified `pg_dump`; the API now reports active alerts on
valves 8, 13-18, 21, and 30. Legacy per-label persistence remains available only
with K/N both disabled. With K/N enabled, inference rebuilds the per-valve
boolean history from the latest stored predictions before processing a new
batch. It excludes the current already-persisted batch by the exact
`prediction_id` UUIDs, so cycle identifiers reused by another run cannot remove
valid history. Ties use the total order `prediction_ts`,
`window_end_cycle_id`, `prediction_id`. The seed has no writes or alert events.
Legacy cooldown and streak state remain ephemeral.

**Latest dashboard status (2026-08-19): the dashboard is COMPLETE and ACCEPTED.**
Three pages — `MACCHINA · VALVOLE · OEE` — all accepted by the user, after six
versions rejected. Final verdict, verbatim, after using them linked together:
*"si mi piace e funzionano bene. oee mi va bene cosi come e'"*.

Live at `dashboard/` (`python dashboard/server_api.py`, port 8077): `/a/` how the machine is doing · `/v1/` which valve to open (the carousel,
35 valves in their physical positions) · `/oee/` why the OEE is what it is (a
waterfall of the day's time). Shared grammar in `LESSICO.md` and
`comune/lessico.css`, extracted from the approved page so later pages inherit it.

A fourth page, VALVOLA, was **cut from the plan** on a measured fact: per-valve
detail holds 400 cycles covering **22 minutes** of a 24-hour day, and no other
route exists — it would have been the panel drawn larger. It had been in the plan
for six versions unexamined.

What made the difference from the six failures: every structurally new page was
built as **three independent variants** (identical packet on disk, neutral names,
same tier, organizing principle the only variable), delivered as **clickable URLs
rather than images**, and chosen by the user before any further work.

Open, non-blocking: `d-deriva-diffusa` on VALVOLE carries 4 tinted elements against
a ceiling of 3; the valve panel partly repeats the hover tooltip; the OEE gauge
reference stays `?rif=sano` with `?rif=oggi` still reachable and never chosen.

Four requests for the backend came out of the build — state transitions with their
timestamps (without them no state timeline), the XmR limits from `/valves/baseline`
which are unusable per-cycle (a healthy valve reads 293/400 out of limits), a
shorter OEE window (with shift/day the three components are flat by construction),
and per-valve history beyond 400 cycles. Plus: regenerate the six fixtures on
comparable downtime profiles, which is what makes the OEE read inverted.

Validation note: the user's real viewport is **1536x770 CSS px** (1920x1080 monitor
at 125% Windows scaling). Earlier checks at 1920x1080 were the wrong measure.

**Dashboard visual gate — RESTART FROM ZERO (2026-08-18).** Every dashboard
visual attempt so far — v1 (P&ID world), v2, v3 (redesign minimal), the Signal
Bench / Trace & Trigger restart prototype, and the v4 "Minimale Estremo"
prototype at `.scratch/dashboard-v4/` — was DELETED on 2026-08-18 by user
decision, together with all build evidence, gates, persona reviews, generator
scripts, the v4 plan-v1.1→v1.8 iterations and contract-v1.8, the old design
mocks/sketches (`.impeccable/`), and all screenshot leftovers (`shots/`,
`.scratch/crops/`, `.scratch/vision-crops/`, `c1-valve-detail-1920.png`), plus
the three old-dashboard handoffs (restart mandate 2026-08-16, parallel-blind
2026-08-16, p7-luna-verdict). None of the deleted visual decisions, plans, or
review verdicts (G5/G6, SHIP-WITH-FIXES, F1) are active; earlier memory
paragraphs describing those builds are history only.

Preserved as input for the next dashboard planning agent (the user's raw
material plus the research distilled from it):

- `Proposte/` — user-provided data: `nuova idea piano dashboard.txt` and
  `Nuova revisione piano per dashboard.txt` (the user's own plan ideas — this
  is what the user means by "what we planned"), `dati per fare una dashboard/`
  (Inductive Automation HMI design article), `ispirazione dashboard/`
  (benchmark dashboard material), project context and roadmap/shareholder
  presentations.
- `.scratch/dashboard-v4-research/` — r1–r4 research notes distilled from the
  user data (HMI design principles, OEE dashboard texts, 17 benchmark images,
  domain + 5-screen proposal).
- `.scratch/tmp_extract/` — raw text extracts of the benchmark/inspiration
  articles.
- `feedback/feedback m2-m3-m4.txt`, `risposte/risposte domanda 1 e 2.txt` —
  earlier user feedback/answers.
- `.scratch/m10/spec.md` — the M10 milestone spec (unchanged).

That fresh planning pass produced dashboard v6 and is now closed by a failed
two-cycle gate. No further code iteration is authorized by its contract until the
five-screen structure, the missing case-diagnostic spine, and the quality-to-valve
attribution boundary have been decided with the user. Binding constraints remain:
read-only observation API / operational database only — never the simulator or
ground truth; machine-agnostic and database-backed.


## Stato al 2026-08-20 — lo storico, il riepilogo, e la pagina TEMPO

Le tre pagine accettate girano sui **dati veri** attraverso il proxy
`.scratch/dashboard-v7/server_api.py` (porta 8078; 8079 e' lo stesso con
l'«adesso» vero, 8077 resta il guscio a fixture come termine di paragone).

Il database contiene ora due run, distinti da `run_id`: `storico_60d`
(36,2 milioni di cicli, dal 2026-06-21 al 2026-08-19 19:29:35 UTC, quattro guasti
scritti) e il vecchio `m4_demo_dropout_1d`, che conserva i cicli ma non ha piu'
diagnosi associata.

Il tetto di 48 ore sulla serie e' caduto: `cycle_rollup_hour` riassume i cicli per
`(run, ora, valvola)` in 34.090 righe, e la serie sui 60 giorni costa **0,6 s**
contro i 147 s del meccanismo precedente. Esiste `GET /valves/quality/series` per
la qualita' per valvola nel tempo. Suite a **513 test verdi**.

**Una quarta pagina, TEMPO, e' in costruzione.** Tre varianti della navigazione
nel tempo sono state costruite col metodo adottato e messe davanti all'utente come
indirizzi cliccabili; ha scelto `tb/`, la striscia dei due mesi sempre in vista
con la finestra trascinabile, motivando: *«hai tutto controllo di dove guardare e
la lunghezza del periodo»*. Le varianti scartate (`ta/`, `tc/`) restano su disco
in attesa di conferma prima di essere rimosse. Il primo giro di correzioni su
`tb/` e' in corso: la striscia che mostra troppo poco, il dettaglio per valvola
mancante, il «dati fermi» scritto invece che disegnato, la coda tagliata di un
giorno e mezzo.

**Il ponte OPC UA copre tutte e 35 le valvole dal 2026-08-21.** Il mapping edge
ha 567 tag generati dalle costanti di `plcsim/opcua_server.py`, l'innesco e' per
valvola (`ValveNN.LastCycleId`, non piu' `Machine.DataReady`), e la catena
simulatore -> OPC UA -> Node-RED -> MQTT -> ingest ha retto **20 min 44 s
continui** a 10,49 cicli/s, 35 valvole su 35, gap massimo per valvola 3,703 s,
zero scarti di validazione e zero `ingest_ts` nulli. Verificato sul parquet raw,
non sul rapporto.

**Non e' ancora live fino in fondo.** Nessun processo porta i dati dal raw al
database in continuo: `pipeline/cycles_backfill.py` resta una botta sola a mano.
E' il Blocco B di `.scratch/HANDOFF-percorso-live.md`.

Nota di metodo, costata tre tentativi: **ogni corsa di misura parte da un
container Node-RED riavviato.** Il fallimento a 26 secondi dei primi tentativi
non era un difetto del prodotto — era stato di sessione rotto accumulato in un
container su da due giorni, dentro cui i flow venivano ridistribuiti a caldo.

## Implemented capabilities

- Deterministic fixed-step causal simulation with explicit clock, carousel,
  physical process, sensors, per-valve PLC state machines, cycle closure, and
  reproducible YAML fault scenarios.
- Cycle telemetry with Filling Time, Tail Time, Tail Pulse, Pulse Count,
  Delta Pulse, Filling Step Out, quality/sequence/validity flags, events, and a
  physically separate ground-truth stream.
- Analytics and healthy-baseline workflow with rolling statistics, XmR limits,
  valve comparison, machine stability/health separation, and diagnostic status.
- Deterministic ML dataset, feature, normalization, training, evaluation, model
  sidecar, and online inference pipeline. The live/batch contract contains 43
  features and versioned prediction records.
- Real-time execution and OPC UA exposure through `asyncua`, with Node-RED edge
  mapping, versioned JSON envelopes, MQTT transport, validation, deduplication,
  and partitioned Parquet raw storage.
- PostgreSQL operational store accessed through SQLAlchemy Core, persistent
  predictions and alert transitions, a pure alert state machine with persistence,
  hysteresis, cooldown and deduplication, and a read-only FastAPI observation API.
- OEE backend implementation for rolling shift/day windows using cycle timestamps
  and OMAC machine-state history, including degraded responses when evidence is
  insufficient.

## Backend operational chain — COMPLETE and VERIFIED (2026-08-19)

The operational database is no longer empty. Root cause of its emptiness was
found and closed: `data/` did not exist at all, so the ingest path had never run
and nothing downstream could. Two new modules close the gap —
`pipeline/raw_replay.py` (bulk run to canonical raw) and
`pipeline/state_history_backfill.py` (OMAC transitions from `events.parquet`).
With `data/raw` populated, `cycles_backfill`, `features` and `inference` work
unmodified.

`plcsim` now holds `work/m4_demo_dropout_1d`: cycles 603,664 · predictions 12,060
· alerts 6 · alert_transitions 334 · machine_state_history 5. **The ML reaches
the API in full**: 35 of 35 valves carry `last_prediction` from the real model
`d-w4-c950bcb3f5d5` (schema `ML-F1`), valve 13 at `flowmeter_dropout` score 1.0
with two sustained alerts, 33 healthy.

Three backend defects were found by comparing the real API against the frozen
fixtures, and fixed: `prediction_ts` on the wall clock (which made the accepted
dashboard's data-age indicator vanish, not read zero), `n_cycles_above`
incremented on alert close, and an ISO format inconsistent inside one response.
Two routes the accepted dashboard calls were missing and were added
(`/machine/oee/series`, `/alerts/history`), plus `window=hour` and per-valve
quality on the OEE window.

Final independent verification: **zero remaining divergences of category
"backend problem"**; 835 of 835 prediction timestamps identical to the fixture.
Suite: **433 tests, all green** (396 at session start). No test writes to
`plcsim`.

**The dashboard and the backend have never talked to each other.** The three
accepted pages still run against the frozen-fixture shell. Closing that is
`HANDOFF-api-vera.md` and is the next priority.

## Active work

- Dashboard v6: gate failed twice; no third correction cycle. Two independent
  signed contract reviews are complete. Active work is now the user's structural
  decision, not implementation.
- Validate the 2026-08-14 OEE backend changes and any PostgreSQL migrations in an
  environment with the locked Python dependencies and the isolated test database.
- Connect the (future) dashboard only to the observation API/operational
  database; it must not read directly from the simulator or ground truth.

## Known issues and limitations

- Current check on 2026-08-21: `python -m pytest pipeline/tests -q` reported
  **298 passed, 1 warning in 177.52s (0:02:57)** with Python 3.14. The run used
  the existing user site-packages and the uv archive through `PYTHONPATH`; no
  package was installed. The default runtime is still not self-contained because
  its system and bundled environments do not provide `pytest` or `polars`.
  The warning is a `StarletteDeprecationWarning` in `fastapi/testclient.py` for
  using httpx with `starlette.testclient`; the suggested follow-up is httpx2.
- Historical evidence from 2026-08-19: `pytest` 9.1.1 was available and the
  full suite reported **433 passed, 4 warnings** on Python 3.14.6.
- PostgreSQL integration tests require the dedicated test database and skip when
  the server is unavailable. They must never write to the operational `plcsim`
  database.
- All previous dashboard builds (v1/v2/v3, Signal Bench restart, v4) and their
  residual-issue lists, gate verdicts, and integration-contract gaps were
  deleted on 2026-08-18; they are history only, recorded in DECISIONS.md.
- Tail Time and Tail Pulse remain calibration-sensitive and should be presented as
  relative/contextual signals rather than absolute truth.
- Raw event-derived ML features intentionally whitelist only supported events, so
  event views are incomplete by design.
- `ValveGroupMap` exists as configuration but group/controller membership is not
  yet exposed as an operational query surface.
- The optional MQTT prediction topic is prepared but not required for the current
  database-backed dashboard path.
- Alert thresholds are frozen for the M10 POC; broader multi-fault and severity
  calibration is deferred to M11.

## Next priorities

1. **Chiudere il primo giro di correzioni su `tb/`** e farlo guardare all'utente.
   Nessuna pagina nuova prima che quella sia approvata.
2. Estrarre la grammatica della pagina TEMPO in `LESSICO.md` e
   `comune/lessico.css` appena e' accettata, com'e' stato fatto per le prime tre.
3. **Blocco B del percorso live:** rendere incrementale il backfill dei cicli e
   dare alla catena un battito, cosi' che il raw arrivi al database senza un
   comando a mano. La larghezza a 35 valvole e' fatta; manca il movimento.
   Subito dopo, la scelta che spetta all'utente (Blocco C): la dashboard mostra
   il run live o continua a mostrare `storico_60d`?
4. Costruire la carta di controllo sulla media mobile di 46 cicli **come seconda
   variante** accanto a quella attuale, da confrontare sui dati veri.
5. Rigenerare le sei fixture su profili di fermata confrontabili, piu' i due
   difetti trovati al loro interno.
6. Valutare se la copertura di K=5/N=150 giustifica i ritardi osservati, da
   1 h 15 min a 15 h 12 min, oppure se M11 deve introdurre un aggregatore con
   una latenza piu' controllabile. La classificazione ML resta un tema separato.


## Architecture

```text
Scenario + hidden ground truth
        -> physical plant -> virtual sensors -> virtual PLC -> cycle validation
        -> telemetry / OPC UA -> Node-RED -> MQTT -> ingest
        -> Parquet raw history + PostgreSQL operational history
        -> live features -> versioned ML inference -> alert engine
        -> read-only FastAPI observation API -> dashboard
```

Primary modules:

- `plcsim/`: causal simulator, telemetry, analytics, ML dataset/model, real-time
  execution, and OPC UA server.
- `pipeline/`: validation, ingest, feature extraction, inference, alerting,
  PostgreSQL storage, cycle backfill, and FastAPI.
- `edge/`: Docker Compose, Node-RED flows, tag mapping, transport schemas, and
  parity checks.
- `scenarios/`: reproducible YAML healthy/fault scenarios.
- `tests/` and `pipeline/tests/`: simulator, contract, integration, storage, API,
  inference, and OEE tests.
- `.scratch/`: local issue/spec/handoff evidence; not intended for memory-only
  publication.
- `docs/adr/`: active architectural decisions.

## Constraints

- Ground truth never enters the operational PLC, analytics, or ML decision path.
- The virtual PLC derives KPIs from observable sensor signals; faults introduce
  hidden causes rather than directly writing anomalous KPI values.
- Preserve deterministic bulk behavior and frozen core contracts unless an
  explicit decision reopens them.
- Keep analytics/ML out of the PLC control layer; prediction, alert decision, API,
  and visualization remain separate layers.
- Storage is three-tier: PostgreSQL for hot operational data, partitioned Parquet
  for raw long-lived telemetry, and SQLite/files only for local application config.
- API and dashboard are machine-agnostic and database-backed.
- Dependency versions in `requirements.txt` are normative for the verified
  environment.
- Use the repository-local Markdown issue tracker conventions in
  `docs/agents/issue-tracker.md`.

## Blocco A edge, 2026-08-21

Il mapping edge ora contiene 567 tag. Include 7 tag macchina e 16 tag per
ognuna delle 35 valvole. Il flow usa `ValveNN.LastCycleId` per ogni trigger.

La verifica statica è completa. Il flow attivo è stato ridistribuito con
l'Admin API Node-RED e i log hanno confermato mapping 567, 35 trigger,
subscription 567 e MQTT connesso.

La controprova runtime, senza deploy dopo il riavvio del solo container
Node-RED alle 22:47:12+02:00, ha prodotto una finestra raw continua di
18 min 51,598 s: 11.869 envelope da tutte le 35 valvole, 339--340 per
valvola, nessun gap superiore a 10 s e massimo gap per valvola 3,703 s.
Il checkpoint ha riportato 4.812 envelope ricevuti e 4.812 scritti (conteggi,
non rate), zero reject, duplicati, invalidi e reconnect; il ritardo medio era
circa 1,74 ms. L'analisi dei timestamp raw conferma inoltre 1.575 record
senza interruzioni attraverso il cambio di agente. Non è stato usato il
database e i Blocchi B e C non sono iniziati. Il dettaglio è in
`.scratch/percorso-live/BLOCK-A-SUBSCRIPTION-RESTART-REPORT-20260821.md`.

## 2026-08-22 — Blocco B, finestra live interrotta

Il gate `GATE_PASS` è stato riusato. Il server OPC UA e l'ingest hanno
prodotto battito live nella sola partizione `date=2026-08-21`; l'ingest ha
registrato 1.211 record scritti e CmdStart ha dato `Running=True`. Il Terra
manager ha fermato la finestra al checkpoint. Non sono stati avviati
supervisor, backfill o inference e non sono cambiati database, schema o
`current_run_id`. Dopo cleanup non risultavano listener sulle porte 4840 e
4841. Il restart post autorizzato ha trovato `plcsim-nodered` in esecuzione,
ma `/health` ha risposto 404. La misura v21 post è terminata con exit 0 e resta
7 su 150, con le stesse nove valvole allarmate del gate. Dettaglio:
`.scratch/percorso-live/BLOCK-B-BATTITO-REPORT-20260822.md`.

## 2026-08-22 — Blocco B, secondo e ultimo tentativo

Il supervisor `live_20260822_battito_attempt2` si è fermato al primo
heartbeat. Il backfill sulla sola data live `2026-08-21` ha trovato 6.217
duplicati su `(valve_id, cycle_id)` e ha restituito exit 2; l'inference non è
partita. I PID attribuibili sono stati fermati e le porte 4840/4841 e le
connessioni al broker risultano pulite. Il restart post di Node-RED è riuscito;
la misura v21 resta 7 su 150 con le nove valvole allarmate invariate. I conteggi
cycles e predictions non sono cambiati e `current_run_id` resta `storico_60d`.
Lo stato è `BLOCKED_FOR_REASONING / NEEDS-REVIEW`. Dettaglio:
`.scratch/percorso-live/BLOCK-B-BATTITO-REPORT-20260822.md`.

## 2026-08-22 — Blocco B, terzo tentativo isolato

La root raw nuova `attempt3-20260822T013358/raw` era assente prima della
creazione. L'ingest è terminato subito perché `Start-Process` ha spezzato il
percorso con spazio passato a `--out`. Il supervisor non è stato avviato. I
soli PID attribuibili sono stati fermati; porte 4840/4841 e broker host sono
puliti. Node-RED è `healthy`; v21, nove allarmi e `current_run_id` restano
invariati. Blocco B resta `BLOCKED_FOR_REASONING / NEEDS-REVIEW`.

## Stato al 2026-08-22, sera — supera tutte le sezioni precedenti

Questa sezione è la più recente. Dove contraddice una sezione più in alto, vale
questa. In particolare supera i tre rapporti del Blocco B qui sopra, che
lasciavano il percorso live `BLOCKED_FOR_REASONING`, e la sezione del 20 agosto,
che parla di tre pagine.

**La dashboard ha cinque pagine, tutte accettate dall'utente.**

| pagina | indirizzo | accettata |
|---|---|---|
| MACCHINA | `/a/` | 19 agosto |
| VALVOLE | `/v1/` | 19 agosto |
| OEE | `/oee/` | 19 agosto |
| TEMPO | `/pc/` | 20 agosto |
| CARTA | `/k1/` | 22 agosto |

**Il percorso live è chiuso.** La catena
`macchina → OPC UA → Node-RED → MQTT → raw → cycles → predizioni → allarmi`
gira da sola, provata con un guasto vero iniettato dal vivo sulla valvola 5:
punteggio del modello 1,000 alla prima finestra, allarme aperto da solo al ciclo
700. Criterio di accettazione `verifica_battito.py 10` con uscita 0. Prove in
`.scratch/percorso-live/PERCORSO-LIVE-CHIUSURA-20260822.md`.

Tre regole valgono per ogni corsa live nuova, e servono tutte e tre:
riavviare il container Node-RED prima della misura, usare un `--client-id`
dedicato alla corsa, partire da una partizione raw nuova con `--run-id`
esplicito passato anche all'inference.

**Il Blocco C è deciso.** La dashboard mostra `storico_60d`. Il KV
`current_run_id` resta lì. Vedi `DECISIONS.md`, 2026-08-22.

**Numeri di oggi**, misurati e non riferiti:

| | |
|---|---|
| suite `pipeline/tests` + `tests` + `edge` | 567 passed, due corse |
| cicli nello storico | 36.241.832 |
| predizioni persistite | 723.110 |
| allarmi attivi | 9 — valvole 8, 13-18, 21, 30 |
| corse live nel database | 4, dal 22 agosto, nessuna mostrata |
| viewport vero dell'utente | 1536x770 px CSS |
| copia di sicurezza | `plcsim_pre_run4_20260822.dump` |

**Come si accende tutto**: `docker start plcsim-postgres`, poi
`python -m uvicorn pipeline.api:app --port 8123`, poi
`python dashboard/server_api.py --port 8078`. Riavvia sempre tutti e
due: l'elenco delle route ammesse vive dentro il processo del proxy, e un proxy
vecchio contro un'API nuova dà 404 che sembrano route mancanti.

**Difetti noti ancora aperti**

- Le sei fixture congelate hanno profili di fermata non confrontabili, e l'OEE
  che ne esce si legge al rovescio. È l'ultimo difetto dei dati conosciuto.
- L'ambiente Python non è bloccato. La suite passa appoggiandosi ai
  `site-packages` dell'utente e all'archivio uv via `PYTHONPATH`.
- Il menu è cresciuto per accumulo: ogni pagina rimanda solo alle pagine nate
  prima di lei, e nessuna rimanda a CARTA.
- Nessuna pagina legge `alert-history.json`.
- Il silenzio del modello sulla valvola 21 è spiegato ma non corretto, materia
  di M11.
- `GET /machine/oee/series` senza `at` costa 3,3 s, causa non isolata.

## Stato al 2026-08-23 — supera tutte le sezioni precedenti

Questa sezione è la più recente. Dove contraddice una sezione più in alto, vale
questa. In particolare **corregge tre voci dell'elenco «difetti noti ancora
aperti»** della sezione del 22 agosto sera: due erano già chiuse e nessuno le
aveva depennate, la terza aveva un numero che non è una costante.

**La pulizia del disco è fatta.** Vedi `DECISIONS.md`, 2026-08-23. Tolte le sei
varianti scartate (`pa/`, `pb/`, `v2/`, `v3/`, `b/`, `c/`), i cinque file sparsi
alla radice e `.playwright-mcp/`. `.pi-subagents/` resta, 331 MB, per decisione
dell'utente. Suite dopo la pulizia: **567 passed**, due corse, identica alla base.

**M11 non è più una questione di taratura.** K=5/N=150 è accettata come
definitiva. Resta solo la classificazione del modello sulla valvola 21.

### Le tre voci corrette

- ~~«Il menu è cresciuto per accumulo, nessuna pagina rimanda a CARTA»~~ —
  **già corretto il 22 agosto**. Tutte e quattro le altre pagine rimandano a
  `/k1/`. Verificato nel codice e cliccando il menu a schermo.
- ~~«Nessuna pagina legge `alert-history.json`»~~ — **voce senza oggetto**. Quel
  file esiste solo nelle fixture congelate della v6, superate per decisione del
  22 agosto. La route viva `/alerts/history` è invece usata da
  `comune/dati.js`.
- ~~«`GET /machine/oee/series` senza `at` costa 3,3 s, causa non isolata»~~ —
  **causa isolata, ma il numero non è una costante**. Vedi qui sotto.

### `/machine/oee/series`: la causa è l'allineamento all'ora, non il parametro

Misurato il 2026-08-23, profilando le fasi interne invece di dedurle.

Il discriminante **non è la presenza di `at`**: è se quell'istante cade su
un'ora esatta.

| `at` | tempo |
|---|---|
| allineato all'ora | 0,58 s |
| non allineato | da 0,9 s a 6,2 s |

Senza `at` la finestra finisce «adesso», che non è quasi mai allineato: per
questo il difetto sembrava riguardare il parametro.

**Dove va il tempo.** Tutto in `_conta_bordi`, l'unico statement che legge da
`cycles` i bordi d'ora parziali. Con `at` allineato quel tempo è **0,000 s**:
i bordi sono vuoti e non vengono nemmeno chiesti. Con `at` non allineato la
serie ne chiede circa 230, che valgono ~1,75 milioni di tuple d'indice.

**Non è un difetto di indice né di manutenzione.** Il piano dice
`Index Only Scan` con `Heap Fetches: 0` su `ix_cycles_run_event_ts_cover`, e
`pgstatindex` dà densità delle foglie 90,05% e frammentazione 0,08%. L'indice
di copertura c'è già e funziona: il lavoro è reale, non sprecato.

**Perché il numero scritto oscillava.** La stessa identica chiamata, nella
stessa giornata, ha dato **13,3 s** alla prima esecuzione, poi 5,5 / 5,1 / 5,3 s,
e infine **0,93 s**. È lo stato del buffer di Postgres, non una regressione del
codice. Il «3,3 s» registrato prima era un punto di quell'intervallo. Chi
riprende il lavoro non deve inseguire una differenza fra 3,3 e 5,3: **deve
confrontare allineato contro non allineato, a cache pari.**

**La strada per una correzione**, se e quando si vorrà: eliminare i bordi. O si
allineano all'ora gli istanti della serie — costa che il punto più recente sia
vecchio fino a un'ora, e rompe l'identità dichiarata «ogni punto è la risposta
esatta di `/machine/oee` con quell'`at`» — oppure si aggiunge un riepilogo a
grana più fine dell'ora. Nessuna delle due è una rifinitura: **è una decisione
di prodotto e va portata all'utente.**

### Difetti noti ancora aperti, elenco aggiornato

- Le sei fixture congelate hanno profili di fermata non confrontabili, e l'OEE
  che ne esce si legge al rovescio. Deciso: non si rigenerano.
- L'ambiente Python non è bloccato. Deciso: il progetto resta su questa macchina.
- Il silenzio del modello sulla valvola 21, materia di M11. Non è taratura.
- ~~`/machine/oee/series` con `at` non allineato all'ora~~ — **non è più un
  difetto aperto**. Causa isolata qui sopra, e il 2026-08-23 l'utente ha deciso
  di lasciarla così: il costo accettato è circa un secondo, solo sul grafico
  dell'andamento, solo al caricamento. Vedi `DECISIONS.md`. La strada giusta se
  un giorno servirà è il riepilogo a grana di minuto, **non** l'arrotondamento
  all'ora, che è scartato.
- `StarletteDeprecationWarning` in `fastapi/testclient.py` per l'uso di httpx
  con `starlette.testclient`. **È l'unico avviso rimasto della suite** e resta
  di proposito: nasce in una libreria e toglierlo vuol dire installare `httpx2`,
  cioè modificare l'ambiente. Il motivo è scritto dentro `pytest.ini`, perché
  nessuno lo silenzi con un filtro.

### Aggiunte del secondo turno del 2026-08-23

- **`OPEN_QUESTIONS.md` è stato ripulito.** Undici sezioni erano intitolate
  «APERTA» pur essendo chiuse da giorni, e facevano rifare indagini già fatte.
  Ognuna è stata verificata sulla cosa e non sull'etichetta, e porta ora un
  riquadro «SUPERATA» con la prova. **Al 2026-08-23 non c'è nessuna domanda
  aperta che richieda una decisione dell'utente.**
- **Esiste `pytest.ini`**, che prima non c'era. Dichiara i marcatori `slow` e
  `opcua`, e toglie tre dei quattro avvisi. Non cambia cosa viene raccolto:
  567 prima, 567 dopo.
- **La deriva lunga settimane è stata riproposta e rifiutata per adesso.**
- **I tre difetti dei generatori di fixture non si correggono**, perché vivono
  solo in codice superato. Vedi `DECISIONS.md`.

## Stato al 2026-08-23, secondo aggiornamento — M11 indagato fino in fondo

- **I quattro documenti `.project/` sono in git.** Commit
  `docs: record the 2026-08-21/23 work and make the open questions trustworthy`.
  Prima di questo restavano solo su disco.
- **Il silenzio del modello sulla valvola 21 è capito e la correzione è stata
  scartata dalla verifica.** La causa è il dominio del set di addestramento in
  spazio normalizzato, non il normalizzatore. L'aumento del set funziona sullo
  scenario a 60 giorni e perde 5,8 punti di macro-F1 su `val`: non si spedisce.
  `opening_delay` e `restriction` differiscono solo in ampiezza sullo stesso
  asse, quindi serve una feature nuova, non un confine spostato. Numeri completi
  in `RECENT_WORK.md`.
- **Il classificatore sbaglia una volta sola su nove.** Otto guasti su nove sono
  etichettati correttamente sull'API viva.
- **La dashboard non legge mai `predicted_label`.** `comune/dati.js:44-56` non ha
  `score`. La classificazione, giusta o sbagliata, non arriva a schermo. Tutti e
  nove gli allarmi si leggono «score_aggregation».
- **Due domande nuove sono aperte** in `OPEN_QUESTIONS.md`, entrambe del
  2026-08-23: quale strada prendere su M11, e la provenienza non tracciabile del
  modello (`inference.py:75-96` ripiega su `manifest.yaml:code_version`).
- Niente è stato toccato fuori da `.scratch/silenzio-21/`. Modello, normalizzatore,
  split, `plcsim/` e `pipeline/` sono intatti. La suite resta a 567.

### La suite verificata il 2026-08-23, e come va eseguita

567 su 567, un solo avviso — quello noto di `fastapi/testclient.py`. Il conto si
compone così: `pipeline/tests` 308, `tests` 258 (di cui 9 marcati `opcua`),
`edge/tests` 1.

**In una corsa sola non arriva in fondo**: impiega circa un quarto d'ora, e i
nove test `opcua` da soli ne prendono cinque, quindi qualunque limite di tempo la
tronca all'80% circa senza dire perché. Va spezzata:

    python -m pytest -q pipeline/tests        # 308, ~3 min
    python -m pytest -q tests -m "not opcua"  # 249, ~6,5 min
    python -m pytest -q -m opcua              #   9, ~5 min
    python -m pytest -q edge/tests            #   1, immediato

Due trappole costate tempo: `pytest-timeout` non e' installato, quindi
`--timeout` fa fallire la riga di comando con codice 4; e incanalare l'uscita in
`tail` la bufferizza fino alla fine, per cui un processo morto lascia un file
vuoto e sembra ancora in corso. Si scrive su file e si guarda quello.

## Stato al 2026-08-23, terzo aggiornamento — M11 chiuso, i guasti hanno un nome

- **M11-taratura e' chiuso.** Non sulla classificazione del modello, che resta
  come e' e come e' spiegata, ma sulla constatazione che l'allarme funziona. Vedi
  `DECISIONS.md`. Da non confondere con **M11-sicurezza**, la milestone della
  roadmap IIoT, che e' un'altra cosa: vedi l'aggiornamento del 2026-08-24.
- **Le pagine MACCHINA e VALVOLE scrivono il nome del guasto**, preso da
  `last_prediction.predicted_label` che `/valves` gia' porta. Nessuna route
  nuova, nessuna chiamata in piu'. Otto valvole su nove prendono il nome giusto;
  la 21 dichiara che il modello non concorda.
- **Il dizionario dei nomi vive in un posto solo**, `comune/dati.js`. La regola e
  il perche' stanno in `LESSICO.md`, sezione 6bis.
- **Sugli allarmi la data porta il giorno.** Era una data sbagliata, non una
  rifinitura.
- Restano fuori dalla modifica le altre tre pagine, la lineage nel motore, il
  modello, il normalizzatore e l'API.
- **Resta aperta una sola voce**: la provenienza del modello
  (`inference.py:75-96` ripiega su `manifest.yaml:code_version`). Conta il giorno
  in cui si spedisse un modello nuovo.
- Suite 567 su 567.

### Come si apre la dashboard, dal 2026-08-23

Alla radice ci sono `dashboard.bat` e `dashboard.ps1`. Doppio clic sul `.bat` e
basta: accende il container Postgres se è fermo, l'API sulla 8123, il server
delle pagine sulla 8078, aspetta che rispondano davvero e apre il browser su
`http://127.0.0.1:8078/a/`. Chiudendo la finestra si spegne tutto.

Due cose non ovvie, tutte e due trovate provando e non ragionando:

- **I processi figli non muoiono da soli.** Condividere la finestra non basta:
  uccidendo il padre di colpo, i due Python restavano vivi con le porte
  occupate. Lo script li lega a un **Job Object** di Windows con
  `KILL_ON_JOB_CLOSE`, e allora muoiono comunque la finestra se ne vada.
  Verificato con `Stop-Process -Force` sul processo che tiene il job: nessuna
  porta rimasta in ascolto.
- **Le copie vecchie sulle porte sono la trappola nota** di questo progetto: la
  pagina prende un 404 e sembra che la route non esista, mentre gira codice di
  ieri. Lo script ferma quello che trova sulle due porte, ma **solo se la riga
  di comando è la nostra**; altrimenti si ferma e lo dice.

Il container Postgres si spegne solo se è stato acceso dallo script, e solo
uscendo con Ctrl+C. Chiudendo con la croce il blocco finale non gira.

Nota: sulla porta 8077 può esserci ancora `server.py`, il guscio a fixture
avviato il 21 agosto. Non c'entra col lanciatore e non viene toccato.

## Stato al 2026-08-24 — la roadmap IIoT e' chiusa

- **`docs/roadmap-iiot.md` e' chiusa.** M6, M7, M8, M9 e M10 sono saliti;
  **M11-sicurezza e' dichiarata fuori ambito, POC accettato**, per decisione
  dell'utente. Non e' un rinvio: chi riapre il progetto non deve rimetterla in
  discussione. Le quattro voci restano scritte come la lista da riaprire **se**
  un giorno la catena esce da questa macchina.
- **Attenzione al nome.** «M11» senza qualificatore e' ambiguo e non va usato.
  **M11-sicurezza** e' la milestone della roadmap, mai aperta e ora chiusa come
  non svolta. **M11-taratura** e' K=5/N=150 e la classificazione del modello
  sulla valvola 21, chiusa il 2026-08-23.
- **I collaudi M9 e M10 non portano piu' un verdetto ritirato.** Rifatti il
  2026-08-24, sezioni `## 8` in coda ai due report. Entrambi **ESITO
  COMPLESSIVO: OK**. I verdetti vecchi restano scritti sopra come storia.
- **Il percorso live e' stato riprovato oggi**, dopo le migrazioni e la pulizia:
  `verifica_battito.py 10` con uscita 0, tutti e tre gli stadi vivi in dieci
  minuti. La catena e' poi stata spenta.
- **Esiste un preflight**: `edge/scripts/preflight_live_run.py`. Va lanciato
  prima di ogni run live. Le quattro regole d'avvio non vivono piu' solo come
  prosa.
- **La suite si misura a Postgres `healthy`.** Su un container che sta ancora
  salendo, 154 test dei 567 saltano in silenzio e pytest chiude comunque con
  «passed». Il numero da confrontare e' **567 passed**; un totale diverso e' di
  per se' un segnale. Non e' l'interprete: verificato con una corsa di controllo.
- **Restano aperte due voci, entrambe rinviate per iscritto** in
  `OPEN_QUESTIONS.md`: `SpeedActual` fuori scala e la provenienza del modello.
  Nessuna delle due morde oggi, ognuna ha la sua condizione di riapertura.
- **Resta aperto un reperto nuovo**: `plcsim/run.py` e' stato modificato il
  2026-08-19 e l'invariante 1 della roadmap chiede un ADR esplicito per i cinque
  file core. Quell'ADR non esiste. La bit-identita' e' stata **misurata** e
  regge (tre parquet identici per SHA-256 contro `work/m4_healthy_1d`), quindi
  non c'e' un danno: c'e' un documento mancante. Va scritto, o va dichiarata una
  deroga.
- Evidenza della giornata in `work/ricollaudo-20260824/`.

## Stato al 2026-08-30 — supera tutte le sezioni precedenti

- **I passi 2 e 3 del piano v2 (analisi predittiva vera) sono eseguiti e
  verificati.** Passo 2: run della deriva lenta su scenario nuovo
  `scenarios/deriva_lenta_60d.yaml` (rampe di 14-21 giorni, seed 43) —
  36.224.625 cicli in 60 giorni, ricaricati per intero sotto
  `run_id='deriva_lenta_60d'`: 722.768 predizioni e 14 allarmi (9 sustained,
  esattamente le 9 valvole guaste). Passo 3: anticipi misurati — restriction
  v8 **75,5 h** e flowmeter_dropout v30 **47,0 h** (contro 5,3 h e 7,5 h dello
  storico); vuoti dichiarati dove la qualità non degrada mai (`closing_delay`
  v21, `pressure_instability` 13-18).
- **Le evidenze vivono sotto `.scratch/`** (git-ignorato):
  `.scratch/v2-predittiva/passo-2/PASSO2-EVIDENZA.md` e
  `.scratch/v2-predittiva/passo-3/PASSO3-MISURA-ANTICIPO.md` con
  `risultati_anticipo.json` nella stessa cartella. Il dettaglio è in
  `RECENT_WORK.md`, 2026-08-30.
- **Cosa resta del piano**: passo 4 in corso (trend sulle grandezze nominali,
  la strada per i guasti che la qualità non vede), poi i passi 5 e 6 davanti.
- **Il passo 4 del piano v2 è eseguito e verificato**: tutti e quattro i tipi
  di guasto hanno una progressione leggibile della severità su canali fisici
  esistenti, senza toccare il modello — restriction v8 via media
  `filling_time_ms` (esce di banda 10,5 h dopo l'onset, 78,0 h prima del
  crollo della qualità), closing_delay v21 via media `tail_time_ms` (pendenza
  +45,2 ms/rampa contro 45 ms di severità dichiarata), pressure_instability
  13-18 via σ oraria di `filling_time_ms` (in progressione su tutte e sei le
  valvole in entrambe le run, Spearman 0,995-1,000), flowmeter_dropout v30 via
  media `filling_time_ms` (0,86 h dopo l'onset, 48,0 h prima della qualità).
  Vuoto dichiarato: le pressioni di gruppo non sono persistite su DB
  (`TankPressure` è ground truth interna), dei driver resta solo l'effetto
  sulla portata.
- **Verdetto di gate sul passo 5**: per rendere leggibile la progressione il
  lavoro sul modello NON è imposto dai dati; il passo 5 resta una decisione
  utente (i contratti congelati ML-F1 + provenienza del modello restano tali,
  nessun lavoro sul modello avviato). Cinque dei sei canali hanno già baseline
  API e carta CARTA; solo σ(filling_time) richiede un'aggregazione non
  precalcolata.
- Evidenza del passo 4: `.scratch/v2-predittiva/passo-4/PASSO4-PROGRESSIONE.md`
  con `risultati_progressione.json` (verifica: secondo run indipendente,
  JSON identico a parte il timestamp). Dettaglio in `RECENT_WORK.md`,
  2026-08-30. **Cosa resta del piano**: passo 5 (decisione utente) e passo 6
  davanti.
- **Il passo 6 del piano v2 ha la sua scelta: la superficie predittiva v2 e'
  la forma «strisce»** (wb del giro 2,
  `.scratch/v2-predittiva/passo-6/varianti-r2/wb/`), scelta dall'utente fra
  le tre forme del design-round (wa «mosaico», wb «strisce», wc «banda»)
  giudicate contro la domanda dei cinque secondi ratificata. Il giro 1
  (va/vb/vc) era stato respinto: *«fatti male design wise, troppo testo»*.
  Entrambe le tornate restano archiviate su disco sotto
  `.scratch/v2-predittiva/passo-6/`. Decisione in `DECISIONS.md`,
  2026-08-30; esecuzione del giro in `RECENT_WORK.md`, 2026-08-30.
- **Cosa restava**: il graft — fatto. **Il graft su wb «strisce» e' eseguito
  e la forma innestata e' APPROVATA dall'utente**: *«okay, la approvo»*.
  Innestati il margine come numero grande dominante (da wa «mosaico») e il
  territorio delle zone di deriva con la traiettoria ricolorata per zona nel
  pannello di dettaglio (da wc «banda»); il glifo dashed-void di wc non e'
  stato innestato perche' wb ne aveva gia' un equivalente. Verifica a
  1536×770 nei due temi: 70 parole di landing invariate, zero errori
  console, 12/12 righe intere nel viewport. Evidenza:
  `.scratch/v2-predittiva/passo-6/verifica-r2/REPORT.md` (sezione graft) e
  `.scratch/handoffs/graft-wb-strisce-r2.md`.
- **La decisione sulla forma e' FINALE** (DECISIONS.md, 2026-08-30).
  **Cosa resta**: il ramo pagina vera e' avviato — la superficie diventa una
  pagina reale della dashboard costruita sulla forma «strisce» innestata;
  l'accettazione dell'utente a schermo resta pendente per la pagina stessa.
- **La pagina PREDITTIVA esiste nella dashboard ed e' ACCETTATA dall'utente
  a schermo** (2026-09-02, verdetto testuale *«si va bene»*): vive in
  `dashboard/predittiva/` (index.html, pagina.js, stile.css), la voce di nav
  e' presente su tutte le pagine esistenti (a, v1, oee, pc, k1) e la route
  `valves/progression/series` e' nella whitelist di `dashboard/server_api.py`.
  Verifica indipendente superata: margini ricalcolati dall'API identici a
  quelli mostrati, suite da 578 test verde, entrambi i temi a schermo.
- **Il milestone v2 e' CHIUSO**: il piano v2 e' completo — passi 1-4
  eseguiti e verificati, passo 5 rinviato per decisione utente (i dati non
  lo impongono), passo 6 chiuso (forma scelta, innestata, pagina accettata).
- **Cosa resta aperto**: niente di pianificato. SpeedActual e la provenienza
  del modello restano differiti con le loro condizioni di riapertura,
  invariati.


## Stato al 2026-09-14 — supera tutte le sezioni precedenti

- **Suite verde per intero, verificata il 14 settembre**: 578 passati con il
  container Postgres sano (11 min 34 s); 390 passati e 188 saltati senza
  database (27 min). Il container impiega ~5 minuti a diventare `healthy` al
  riavvio: aspettarlo prima di lanciare pytest.
- **Audit completo consegnato come artefatto** (v2 chiusa, catena in piedi,
  nove incoerenze docs/codice non bloccanti, elencate in `RECENT_WORK.md`).
- **Direzione dopo la v2 decisa: Policy Lab** (DECISIONS.md, 2026-09-14). La
  pagina di piano e' l'artefatto «Policy Lab FillerStack». I costi vengono da
  una ricerca su fonti reali; l'unico parametro mancante e' il prezzo di una
  valvola isobarica e del kit guarnizioni, in attesa di quotazione da Eckenroth.
- **Policy Lab 6a costruito e misurato il 15 settembre** (`work/policy-lab/`): stime entro ±12 h da +24 h, 6/6, fascia coperta 6/6. I margini fisici della pagina PREDITTIVA sono 78 h (v8) e 48 h (v30); 75,5 e 47,0 sono l'anticipo del modello (vedi RECENT_WORK 2026-09-15).
- **Banco di prova del Policy Lab fatto, verificato e corretto il 15 settembre sera** (`work/policy-lab/banco.py`, `RISULTATI-BANCO.md`, `VERIFICA-BANCO.md`): su deriva_lenta_60d a P = 1.500 EUR la stima 6a costa 9.312 EUR contro 14.094 (a guasto), 15.763 (soglia di oggi), 120.874 (calendario), 4.452 (oracolo). Pareggio con la soglia di oggi a 587 EUR (1.345 con fermo a 9.200 EUR/h). Su storico_60d (margini 6-8 h) la stima non fa in tempo. Il 15 sera l'utente ha scelto di scrivere la politica 6c.
- **Politica 6c «costo atteso» scritta, misurata, verificata e corretta il 15 settembre notte** (`work/policy-lab/politica.py`, `RISULTATI-6C.md`, `VERIFICA-6C.md`): 9.333 EUR al riferimento contro 9.312 della 6a, zero uscite su sane da 500 EUR in su, pareggio con la soglia di oggi a 589 EUR. Policy Lab v7 pubblicato con la decisione aperta: A pezzo 7 (schermata della decisione in dashboard, tre varianti, raccomandata), B altre corse, C fermata notturna (RECENT_WORK 2026-09-15 notte).
- **Il verdetto 6c esce dall'API dal 15 settembre** (`pipeline/decision.py`, rotta `GET /valves/decision`, `"valves/decision"` in `ROUTE_AMMESSE` a `dashboard/server_api.py:76`, 19 test nuovi): il calcolo e' riscritto dentro `pipeline/` senza un solo import da `work/`, perche' il memo a chiave `id()` di `work/policy-lab/banco.py:67-78` non e' sicuro dentro un processo che serve piu' richieste. Verificata da me contro tutti e sei i file registrati campo per campo, 0 differenze, risposta da 0,32 s a 2,17 s sui sei istanti e 4,81 s sull'ultima ora, suite intera 338 passed. La pagina non legge piu' i file registrati.
- **La schermata della decisione e' montata dal 15 settembre** (`dashboard/decisione/`, voce DECISIONE nella nav delle altre sei pagine): la giostra approvata delle 35 valvole, e in fondo a tutta larghezza una striscia lunga quanto la corsa con cui si sceglie l'ora. La striscia vive su `GET /valves/decision/timeline`, una richiesta sola all'apertura che porta i conteggi di tutte le 1.407 ore in 5,1 s e 8 kB. La camminata sulla griglia sta in un posto solo, il generatore `camminata(f)` che sia `percorso` sia `linea` consumano, altrimenti striscia e verdetto divergerebbero in silenzio sulla stessa ora. Suite da 338 a 350 test. Verificata da me: 32 controlli su 32 sulla rotta (quattordici ore confrontate una per una contro `GET /valves/decision`) e 12 su 12 sulla pagina a 1536x770. Due difetti corretti da me dopo aver guardato le immagini: la scritta «crollo: nessuna stima» che usciva dalla scheda, e il trascinamento che selezionava il nome del mese.
- **Il verdetto e' precalcolato dal 16 settembre, e il 16 l'utente ha confermato che basta** («okay, adesso la velocita' va bene», dopo aver aperto la pagina) (`pipeline/decision_rollup.py`, tabella `decision_rollup_hour`): l'utente ha aperto la pagina e l'ha trovata lenta. Misurato su tre livelli, il database non c'entra: la lettura da Postgres costa 0,21 s costanti, il calcolo 6c costa 4,6 s all'ultima ora e cresce piu' che linearmente, con il 95% del tempo dentro `stima_crollo` e 309.356 chiamate a `t_cdf` per richiesta. `decision.linea()` calcola tutte le 1.407 ore per lo stesso prezzo di una sola, quindi si precalcola una volta per corsa (49.245 righe, ~5,5 s) e le due rotte leggono, ripiegando sul calcolo quando la tabella non e' fresca. Ultima ora da ~6 s a 0,05 s, striscia da 10,3 s a 0,07 s, prima schermata in un browser vero da ~17 s a 1,35 s a freddo e 0,17 s a caldo. Il metro preso prima del lavoro, lo sha256 del corpo di `decision.decisione()` per ciascuna corsa, e' rimasto identico (`c63b36f0...` e `a91dfc13...`), e il confronto campo per campo fra corpo calcolato e corpo letto dalla tabella da' zero differenze su tutte e 35 le valvole.
- **La freschezza della tabella guarda anche i parametri** (`decision.impronta_parametri`, colonna `impronta`): confrontare solo `MAX(ora_ts)` lasciava la tabella formalmente fresca dopo un cambio di costante o di finestra sana, e servivano numeri vecchi in silenzio. `PREZZO_VALVOLA_EUR` aspetta un preventivo vero, quindi non era teorico. L'impronta e' lo sha256 delle diciassette costanti della politica piu' gli estremi della finestra, e un test confronta l'elenco con le maiuscole del modulo cosi' dimenticarne una nuova diventa un errore. Nello stesso giro e' stato chiuso un secondo difetto: `verdetto()` prendeva prezzo, `F_U` e `L` come valori predefiniti di firma, che Python calcola una volta sola al caricamento, quindi la risposta poteva dichiarare un prezzo e usarne un altro.
- **Due difetti della schermata trovati dall'utente il 16 settembre, guardando una schermata sola** (`dashboard/decisione/stile.css`, `pagina.js`): la tessera portava due assi di significato sulla stessa proprieta' grafica, il colore del contorno per il verdetto e il tratteggio dello stesso contorno per la stima del crollo. Sulla valvola 8, che era `intervieni` e insieme senza stima, il grigio del tratteggio sovrascriveva il rosso, quindi il centro contava «1 intervieni» e sull'anello non c'era niente di rosso. Corretto lo stesso giorno, e poi sostituito dalla forma B scelta dall'utente: il tratteggio sparisce, il contorno resta pieno e porta solo il verdetto, e «senza stima» diventa un quadratino vuoto di 7 unita' all'angolo della tessera che compare solo dove il verdetto non e' `continua`. Secondo difetto trovato da me sulla stessa scheda: il conto alla rovescia della squadra scriveva sempre «24,0 h» perche' `squadra.ore` vale `int(L_H)`, che e' il ritardo nominale. Corretto nella pagina e non nella rotta, apposta, per non spostare i due sha256 del metro. La suite passa prima e dopo, perche' i due difetti stanno tutti e due nella pagina: 365 test in `pipeline/tests`, 624 raccogliendo dalla radice, cioe' anche `edge/tests`. Il numero da citare e' 624.
- **Cosa resta aperto**: la quotazione; la pulizia delle incoerenze docs/codice
  (README «cinque pagine», titolo «strisce» di PREDITTIVA, conteggi test),
  tutte visibili e da mostrare all'utente prima di toccarle; provenienza del
  modello e SpeedActual invariati con le loro condizioni; quale delle due
  corse debba essere quella di casa della schermata della decisione. Chiusa il 16 settembre: dove sta il segno «senza stima» sulla giostra, con l'utente che ha scelto la forma B fra le quattro disegnate in `work/pezzo7/stima/` («okay, vai con la tua raccomandazione») e che il 16, sulla pagina vera, ha confermato il segno («si va bene il quadrato, lo avevo gia approvato questo»). Chiusa il 16 settembre anche la parte sui dati, il «segnale dal futuro»: `fatti_valvola` cercava il primo segno di degrado sulla corsa intera, quindi la risposta a un'ora poteva portare una data successiva a quell'ora. Colpiva 1.224 ore su 1.407 in `storico_60d` e arrivava sullo schermo in 96 ore, contro le zero che avevo previsto. L'utente ha scelto di cercare il segno solo fino all'ora guardata («va bene vai con la A»), e la correzione porta il segnale ora per ora anche dentro il precalcolo, perche' quello chiama `fatti_valvola` una volta sola a fine corsa. Il taglio vale per il solo segnale: la baseline resta sulla serie intera, perche' la finestra sana finisce dentro la corsa. Costo misurato: il segno e' lo stesso e compare 24 ore dopo, quante ne pretende `REGIME_MIN_H`. I due sha256 del metro e le cifre di `linea()` non si muovono.
