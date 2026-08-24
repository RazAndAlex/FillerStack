# Active decisions

Updated: 2026-08-21

This file summarizes active decisions evidenced by `CONTEXT.md`, `docs/adr/`, and
the current implementation. The ADRs remain authoritative for detail.

## Alert operativi score-only K=5/N=150

- L'apertura operativa usa almeno 5 punteggi `anomaly_score >= 0.5` negli ultimi
  150 cicli della stessa valvola. `threshold_open` resta 0,5.
- `predicted_label` non discrimina mai l'apertura. Tutti gli eventi usano la
  lineage tecnica `score_aggregation`, anche se le label predette cambiano.
- K=5/N=150 e' il default scelto sulla simulazione dell'intera storia: zero
  falsi positivi, 9 guasti iniettati su 9 rilevati, valvola 21 attiva per il
  93,0% del run e 8 aperture. K=5/N=100 dava 70,8% e 33 aperture.
- Ground truth e scenario servono solo a valutazione e misura offline. Non
  entrano nell'engine, nel replay, nel database operativo o nell'API.
- Modello, feature extraction, inference e schema ML-F1 restano invariati. La
  classificazione diagnostica e l'apertura operativa sono problemi distinti.
- Il replay dello storico parte esclusivamente dalle predizioni persistite ed e'
  distruttivo solo con opzioni esplicite e backup verificato. Il dump precedente
  al replay resta in `.scratch/taratura-aggregazione/backup-2026-08-21/`.
- Il replay N=150 ha portato il database da 11 a 12 alert e da 64.105 a 64.180
  transizioni. Il backup e' in
  `.scratch/taratura-aggregazione/backup-2026-08-21-n150/alerts-pre-score-replay.dump`
  con SHA-256
  `4443A54C4931227D424B14372D702FB28CB89103E93AD683B19885D02C006045`.
- La misura su nove valvole mostra un peggioramento massimo di 0,4 s e un
  miglioramento di 2.080,69 s (-34 min 40,69 s) sulla 21. N=150 legge 5.250 righe per 35 valvole
  e ha una mediana warm di 9,811 ms, contro 7,625 ms di N=100.
- Al riavvio inference ricostruisce le ultime N decisioni score-only delle 35
  valvole in ordine cronologico. Esclude il lotto già persistito tramite gli
  UUID esatti `prediction_id`. L'ordine totale usa `prediction_ts`,
  `window_end_cycle_id`, `prediction_id`, quindi resta deterministico anche in
  caso di parità e di identificatori di ciclo riutilizzati tra run. Il seed non
  scrive eventi. `load_states` resta compatibile e non rende persistenti
  cooldown o streak legacy.

## Causal simulation boundary

- The simulator is a six-layer causal core: explicit clock, scenario/fault
  engine, physical plant, virtual sensors, virtual PLC, and cycle validation.
- Ground truth is emitted separately and is unavailable to the PLC and ML
  operational paths.
- KPI anomalies must emerge from hidden causes propagating through the simulated
  process and sensors; code must not inject the final anomalous KPI directly.
- The default simulation clock uses a fixed 1 ms step, with PLC scan cadence
  configured separately. Deterministic bulk execution is a same-environment
  regression contract.
- The carousel has 35 valves with 26 useful filling positions. Per-valve behavior
  follows the ratified nine-state PLC sequence; Filling Step Out emerges from
  geometry and timing.

Relevant ADRs: 0001, 0004, 0005, 0007, 0008, 0009, 0010, 0011.

## Telemetry, scenarios, and analytics

- Telemetry exposes three distinct outputs: observable events/cycles, fault
  timeline, and ground truth. The latter two are never operational inputs.
- Scenarios are declarative YAML with deterministic seed streams and explicit
  fault onset, severity, and scope.
- Analytics is a layer above telemetry, not part of the virtual PLC. Operational
  stability (`MachineStable`) and health (`MachineHealthy`) are different concepts.
- The healthy baseline is fixed from verified healthy data and does not update
  during degradation. XmR, top-10 valve stability, and rate detectors are the
  current diagnostic basis.

Relevant ADRs: 0012, 0013, 0014.

## ML dataset and model

- Ground-truth labels are joined only during offline dataset construction and
  evaluation. Feature computation uses observable signals only.
- Train, validation, test, and baseline runs use separate seeds and provenance;
  manifests and hashes protect reproducibility.
- The current classifier is a deterministic multinomial logistic regression.
  Batch and live inference share one feature implementation and normalization;
  anti-skew requires bit-identical 43-feature output.
- Prediction records carry model and feature-schema versions. Envelope v1.0/v1.1
  contracts remain immutable; the extended live contract uses wire v1.2 and
  stored v1.3.

Relevant ADRs: 0015, 0020. The operational SQLite choice in ADR-0020 is superseded
by ADR-0021.

## Real-time and IIoT transport

- OPC UA is served by `asyncua` from the real-time wrapper and remains outside the
  deterministic bulk fingerprint.
- Node-RED performs centralized tag mapping and transport shaping only; it must
  not contain domain, diagnostic, or ML logic.
- MQTT telemetry uses versioned JSON envelopes, QoS 1, non-retained valve-cycle
  messages, retained machine state, ingestion timestamps, validation, and
  deterministic deduplication.
- Raw telemetry is stored as partitioned Parquet with atomic flush behavior.

Relevant ADRs: 0016, 0017, 0018, 0019.

## Operational data, alerting, and UI

- Storage is three-tier: PostgreSQL for hot operational history, Parquet for raw
  long-term telemetry, and SQLite/files for local application configuration.
- `pipeline/storage.py` is the operational persistence boundary and uses
  SQLAlchemy Core with psycopg. PostgreSQL supersedes the M9 SQLite prediction
  store without changing the prediction contract.
- Prediction and alert decision are separate. The pure alert engine owns threshold,
  persistence, hysteresis, cooldown, deduplication, and transition semantics.
- FastAPI is a read-only, machine-agnostic observation plane. The dashboard reads
  the API/database and never reads the simulator or ground truth directly.
- M10 dashboard composition is deliberately iterative with the user. The ratified
  information hierarchy is OEE/machine state first, the 35-valve diagnostic view
  second, and per-valve evidence as drill-down.
- The dashboard visual world was ISA P&ID engineering-paper style: paper
  `#faf7f0`, indigo `#1c2b4a`, red `#a83232`, amber `#7a5b12` (all AA). L0 uses
  three bands: cartouche + OEE, carousel ring, striped inbox. The mechanical and
  logical lenses filter only L2/L3. Valve prominence is multi-cue out of the box
  (never color alone), and a reliability-badge taxonomy appears on every screen.
  **RETIRED 2026-08-16**: `DESIGN.md` (root) was deleted and no dashboard
  visual system is registered as active until the restart contract lands (see
  "Dashboard restart" below). Historical references: `.impeccable/surfaces/…`;
  ADR-0021 unchanged.
- Iteration v2 (2026-08-15, user navigation feedback, contract in
  `.scratch/dashboard/iter-v2/review-cards/contract-v2.md`): the ring stays a
  CIRCLE (bigger: 700px, readable labels) with the user's perspective idea
  honored as a decorative elliptical plinth — an ellipse ring was rejected
  because side-node pitch collapses ~44% and would risk alert-localization;
  dark mode is a token-only "blueprint" VIEW (CARTA·SISTEMA·NOTTE toggle,
  OS-aware default), not a dark world; L0 keeps the ratified TIC-01→VIC-35→ALM-01
  band order with a problem row yielding the first-glance answer; L2 uses native
  details/summary progressive disclosure (EVD/TRD closed by default) with `&blk=`
  deep-links; every interactive element has hover/active/focus states; a11y
  fixes include removing `aria-live` from `main`, removing the SVG `role=img`,
  and a 1000px topbar breakpoint (200% reflow).
- OEE uses rolling 8-hour shift and 24-hour day windows. Availability derives from
  OMAC state history, Performance from actual timestamped cycles versus target
  speed during Running time, and Quality from `fill_quality_ok`; insufficient
  history returns a degraded response instead of fabricated values.
- v2 QA contracts (G5, superset of G4): usability-v2 suite (U2-01..14), AC-D6
  disclosure / AC-D7 deep-link, AB-10..12 (dark contrast / reflow / a11y tree);
  collapsed blocks stay in the DOM so all `data-fact-id`/`data-band`/`data-trend`
  contracts hold.

Relevant ADR: 0021; dashboard contracts are in `.scratch/dashboard/` locally.

## Dashboard v3 (redesign minimal) — SUPERSEDED 2026-08-16

- The v3 redesign is PRESENTATION-only (contract v3.1, charter vincolante in
  `.scratch/dashboard-iteration-v3/contract-v3.md`, emended after reviews
  C1–C4): CSS + presentation DOM + label copy; never the fact-id VALUES, and
  never fixture / derive.js / data-layer.js / validator / generate-fixtures
  (data contracts intact).
- The user's minimal feedback drove it: fewer lines/frames, larger text
  (floor 11px operational, ring labels 12u, hero 28–44px), charts over text
  (≤2 chart types per view, ring = `map` exempt).
- Gate G6 COMPLETE (2026-08-16, evidence `v3-branch-report.md` + receipt
  `v3-p0…p6-*.md`): redesign-v3 34/34 · smoke 8/8 · journeys 57/57 · criteria
  45/45 · invariants 29/32 (I-7-B/C/D pre-existing baseline signature) ·
  antibug 268/355 (87 ab1-console favicon-only nominal, P0 signature) ·
  usability-v2 14/14 · shots 33/33 (light+dark 1920/390 incl. closed and
  readout states) · derive 74/74 · node --check 25/25 · 0 real console
  errors · fixtures immutate (sha256 `8e17104d…dbb04`).
- Every surface-check change went through the delta matrix (M01–M22, written
  BEFORE the diff in `checks/v3-branch-report.md`); data contracts never
  touched. M22 (antibug `ab5-d-case18`) aligned the check expectation with the
  P5-approved one-row L2 header (decision V3-4 option (a)): check updated,
  design not reverted.
- Manager decision M20: the L2 word cap was recalibrated 80→110 (honest
  measure 104 after legal trims of the frozen copy; L0 ≤60 and L1 ≤50
  unchanged).
- After an orphaned v2 worker overwrote part of `js/views/l0-home.js`, the
  process was terminated and band-1/band-3 head rebuilt per the
  P2/P4 receipts (`checks/v3-recovery-l0.md`); ring/trend/inbox and all suites
  re-verified.
- DESIGN.md (root) got a decay/rest registry in one phase (no "two truths"
  window), and the prototype README lists `redesign-v3.mjs`.
- Exposed contracts for other branches: hero `[data-testid="hero"]` (L0
  fact:oee; L2 fact:filling-time-valve{N} mech / fact:score-valve{N} log);
  ring v3 (r10/14/18, label 12u, cap 330, alert full fill + stroke 2.8 + flag
  only on unique-most-severe); hub without text + 35-cell bar
  (hub-status/map-legend); default-open L2 = header+quick+IMP+STP (EVD/TRD/MTX
  closed); fact:pacing-bottiglie (new L0 presentation id); state-pill without
  fact-id on L0; L1 list behind disclosure (wait `state:'attached'`).
- The 4 TODO issues of v3 went `ready-for-agent` → resolved with the branch
  closure (tickets in `.scratch/dashboard-iteration-v3/issues/`; 04
  materiale-di-studio was already resolved before the branch).

Relevant sources: `.scratch/dashboard-iteration-v3/contract-v3.md`, `.scratch/
dashboard/prototype/checks/v3-branch-report.md` and `v3-p0…p6-*.md`,
`v3-recovery-l0.md`; DESIGN.md root.

> Historical note (2026-08-16): this whole section is SUPERSEDED — the old
> visual system was discarded, `DESIGN.md` (root) deleted, and
> `.scratch/dashboard/`, `.scratch/dashboard-iteration-v2/`,
> `.scratch/dashboard-iteration-v3/` are not present in the worktree. The
> files cited above no longer exist on disk; do not search for them. None of
> the v3 visual decisions are active.

## Dashboard restart — old visual system discarded (2026-08-16)

- The old dashboard visual system (P&ID world, v1/v2/v3) is explicitly
  DISCARDED as too complex and illegible: `DESIGN.md` (root) deleted;
  `.scratch/dashboard/`, `.scratch/dashboard-iteration-v2/`,
  `.scratch/dashboard-iteration-v3/` are not present in the worktree. No
  dashboard visual system is registered as active.
- The next session restarts the visual work from scratch: the new design is
  chosen independently (impeccable new-work — skill at
  `C:/Users/Utente/.agents/skills/impeccable/`, used read-only), the contract
  is saved before build, and plan and direction pass independent review
  before implementation. The previous design is not a style source.
- The old BLIND rule ("do not look at the existing dashboard") is obsolete:
  the files are gone; the work must still be a new design, not a repair.
- Mandate: `.scratch/handoffs/plc-sim-dashboard-restart-2026-08-16.md` —
  context budget under 220k tokens, orchestration-only manager, delegated
  research/build/review, one writer lane per overlapping file, no receipts
  without evidence. Product truths (PRODUCT.md, `Proposte/Nuova revisione
  piano per dashboard.txt`, `.scratch/dashboard-research.md`, CONTEXT.md)
  stay authoritative.

### Restart EXECUTED (2026-08-16/17) — the new active visual world

- The new visual world is **Signal Bench / Trace & Trigger** (impeccable
  grounded candidate 3, direction seed `cb343391`), chosen autonomously with
  the contract saved BEFORE build (`.scratch/dashboard-restart/contract.md`,
  incl. the mandatory ≤150-word first `<body>` comment carrying the seed and
  six labels) and plan + direction passed through fresh non-author review
  (`plan-review.md`) before any code. The previous design is not a style
  source.
- Build root `.scratch/dashboard-restart-build/` is a static prototype only:
  L0–L3 hash navigation with alert deep-links, six endpoint-shaped fixture
  scenarios A–F, read-only adapter over fixtures mirroring the read-only
  FastAPI observation API (catalog exactly 1..35, 21-field KPI series,
  `null`/`404`/`501`/degraded OEE preserved), two lenses with verified
  invariance, Italian copy, DEMO·fixture marking. No new endpoint, schema,
  fixture mutation, ground-truth label, or write path was needed (stop
  conditions honored).
- Detector and finish policy held: the Impeccable CSS detector ran exactly
  once (side-tab fix, `styles.css` only; receipt
  `.scratch/dashboard-restart-build/checks/side-tab-fix-receipt.md`); root
  `DESIGN.md` was written only AFTER finish review as the shipped-world
  artifact, with the `.impeccable/design.json` sidecar (receipt
  `.scratch/dashboard-restart-reviews/design-doc-receipt.md`).
- **Verdict (2026-08-17):** the post-fix final review is SHIP-WITH-FIXES —
  no blockers. M10 visual gate is prototype-complete, but no clean zero-risk
  claim is made: the 200%-zoom extreme-narrow cell is RESOLVED at its exact
  instrumented layout equivalent — the F1 responsive-hardening pass
  (minimal `@media (max-width: 340px)` CSS — nowrap `.chip` and the native
  trace select wrap/stack — plus truthful table-scroll labels; root cause:
  nowrap chips + native trace select at extreme layout) makes the 195px
  proxy (=390px @ 200%) measure `sw == cw` on L0–L3 (was L2 `272 > 180`,
  L1 `288 > 180`, L3 `182 > 180`); the 160px sweep is clean except one
  documented non-reproducible 1px transient; the 63-cell matrix is clean
  except that transient — while real Ctrl++ zoom stays honest `[nr]`
  (headless zoom inert) and the remaining residuals stay disclosed
  (screenshot timestamp caveat, missing loading/skeleton capture, no
  retained pre-fix `styles.css` snapshot). Decision: keep the real-zoom
  cell disclosed and unclaimed rather than force a false PASS; follow-up is
  a single bounded finish lane (real-zoom instrumentation or an explicit
  waiver, optional loading capture) per the plan's review budget.

## Dashboard builds deleted — clean restart from user data (2026-08-18)

- By user decision, every dashboard visual build was deleted permanently:
  the v4 "Minimale Estremo" prototype (`.scratch/dashboard-v4/`, including
  prototype, checks/gates, persona reviews, generator scripts, and all
  plan-v1.1→v1.8 iterations + contract-v1.8), the leftovers of the older
  v1–v3 and Signal Bench attempts (`.impeccable/` mocks and sketches,
  `shots/`, `.scratch/crops/`, `.scratch/vision-crops/`, root
  `c1-valve-detail-1920.png`), and the old-dashboard handoffs (restart
  mandate 2026-08-16, parallel-blind 2026-08-16, p7-luna-verdict). The files
  were not git-tracked; deletion is final.
- Preserved as the sole basis for the next dashboard: the user's raw input
  in `Proposte/` (their own plan-idea txt files — "what we planned" per the
  user — the Inductive Automation HMI design article, and the ispirazione
  benchmark material), the research notes `.scratch/dashboard-v4-research/`
  (r1–r4), the raw article extracts `.scratch/tmp_extract/`, `feedback/`,
  `risposte/`, and the M10 spec `.scratch/m10/spec.md`.
- The next dashboard will be planned fresh by an agent from the preserved
  material; the deleted v4 plan ("Minimale Estremo") must not anchor it.
  Binding constraints remain: read-only observation API / operational
  database only, never the simulator or ground truth; machine-agnostic and
  database-backed; L0–L3 decision-hierarchy intent per `.scratch/m10/spec.md`.
- All prior dashboard gate verdicts (G5/G6, SHIP-WITH-FIXES, F1) refer to
  deleted artifacts and are history only.

## 2026-08-18 — Dashboard v6: two root decisions and a four-screen structure

- **Severity has two axes.** Prominence follows deviation from a quantity's own declared
  baseline — for valves, for A/P/Q, for scrap alike. 100% is not a reference for this
  machine: it is a value never observed. The calibrated healthy baseline is ~21.3% scrap.
  Consequence: no colour on a quantity that is not calculable in the current state; a
  machine idle by choice produces no red.
- **The "case" object is a measure, not a screen.** Per-valve scrap excess — current
  `fill_quality_ok` rate minus the valve's own `fill_quality_ok_rate` from
  `/valves/baseline` — is the same quantity that composes OEE Quality, decomposed per
  valve. It appears on both branches and joins them without inventing causality. It says
  *where*, never *why*, and always declares its window.
- **Four screens, not five**: TRIAGE, VALVOLE, VALVOLA, OEE. The carousel is no longer the
  overview primary — the blur test rejected it as such twice, independently — but moves
  into VALVOLE as the physical localiser answering "which valve do I open". User decision.
- The data contract is **9 routes**, not 8: `/valves/baseline` is a structural dependency.
- Acceptance keeps the v1 criterion (judged honest by all five reviewers: it failed twice
  and stopped the third patch cycle) with five corrections, the first being that
  expectations are sealed **before** the build, with a hash.
- Governing document: `.scratch/dashboard-v6/contract-v2.md`. v1 (`contract.md`) is
  history.

## 2026-08-19 — Dashboard v7: la pagina MACCHINA, accettata

- **Una pagina sola, costruita tre volte, consegnata come URL.** E' la parte del
  piano che in sei tentativi non era mai stata eseguita, ed e' quella che ha
  cambiato l'esito. Le alternative strutturali non si giudicano su prosa o
  schizzi: consenso dato in quel modo e' gia' stato ribaltato due volte al primo
  contatto col prodotto vero.
- **Il briefing delle varianti sta su disco, non nei prompt.** `PACCHETTO-comune.md`
  e' letto dai tre costruttori: duplicarlo avrebbe introdotto differenze
  involontarie e reso il confronto privo di valore. L'unica variabile fra A, B e C
  e' il principio organizzativo — non il modello, non l'effort.
- **Il colore si giudica sullo schermo intero.** Nessun elemento colorato era
  sbagliato da solo; era la somma a essere respinta (*"e' come essere colpito"*).
  Regola adottata: il colore lo prende solo cio' che ha una gravita', e cresce
  gradualmente sotto il riferimento. Cio' che sta bene resta neutro — non diventa
  verde. Metrica di controllo: contare gli elementi che portano tinta.
- **Un grafico deve mostrare l'andamento, non il superamento.** La traiettoria non
  va mai coperta dai propri marcatori di allarme ne' schiacciata contro un bordo,
  nemmeno quando esce interamente dalla banda. Traiettoria, riferimento atteso e
  ampiezza dell'intervallo devono leggersi insieme.
- **La dashboard e' interattiva.** La deroga alla staticita' presa verso la skill
  `dataviz` e' caduta su richiesta esplicita dell'utente, due volte: hover e
  affordance su ogni valvola, hover informativo su ogni grafico, accesso da
  tastiera.
- **I difetti dei dati restano visibili.** L'OEE ribaltato non e' stato aggirato
  ne' cambiando la gerarchia della pagina ne' truccando il riferimento: e' un
  artefatto di fixture costruite da run con profili di fermata non confrontabili,
  e va risolto rigenerando gli scenari.
- La route `GET /valves/baseline` e' confermata **dipendenza strutturale**: senza
  di essa il confronto fra valvole non e' costruibile, ed e' ora esposta dal
  server delle fixture.

## 2026-08-19 (2) — Dashboard v7 completa: le decisioni che reggono le tre pagine

- **Tre pagine, non quattro.** `VALVOLA` e' stata cancellata su un fatto misurato:
  400 cicli per valvola coprono 22 minuti su 24 ore. Una pagina si giustifica con i
  dati che ha, non con la posizione nel piano — ed era nel piano da sei versioni
  senza che nessuno la verificasse. **Prima di costruire una pagina, misurare se i
  dati la sostengono.**
- **Il metodo delle tre varianti e' la pratica adottata** per ogni pagina
  strutturalmente nuova: pacchetto identico su disco (non duplicato nei prompt, che
  introdurrebbe differenze involontarie), nomi neutri, stesso tier, unica variabile
  il principio organizzativo; consegna come URL cliccabili, mai come immagini;
  scelta dell'utente prima di qualunque altro lavoro.
- **La grammatica si estrae appena una pagina e' approvata.** `LESSICO.md` +
  `comune/lessico.css`: dalla seconda pagina in poi la lingua non e' piu' in
  discussione e le varianti ereditano invece di reinventare. E' cio' che impedisce
  a una dashboard di diventare tre dashboard.
- **Una macchina non in marcia non produce rosso.** Con le route attuali un fermo
  voluto e un guasto non sono distinguibili (su `e-macchina-ferma` il turno ha
  6.120 s di marcia su 28.800 *pianificati*): il calo e' reale ma nulla dice se sia
  voluto. Il numero resta, la tinta no — colorare sarebbe un verdetto non
  ricavabile. Lo stato OMAC e l'eta' del dato dicono cio' che si sa con certezza.
- **Una discrepanza fra due indicatori onesti si indirizza, non si elimina.**
  L'utente ha scelto la candela proprio perche' puo' contraddire l'allarme: il
  disaccordo dice che il problema non e' nella grandezza mostrata, quindi
  restringe il campo. La risposta va resa raggiungibile (il suggerimento mostra
  tutte le grandezze), non rimossa insieme alla domanda.
- **Nessun indice sintetico per valvola.** Nessuna grandezza singola copre i sei
  scenari, ed e' stato misurato da tre costruttori indipendenti; fondere le misure
  in un punteggio unico ricrea il punteggio di anomalia che l'utente ha respinto
  (*"non capisco cosa significa"*). Si mostrano grandezze nominate, ciascuna
  confrontata con la base della **propria** valvola.
- **Si valida a 1536x770 px CSS**, il viewport reale dell'utente (monitor 1920x1080
  con scalatura Windows al 125%). Le verifiche a 1920x1080 misuravano altro.

## 2026-08-20 — Lo storico, il riepilogo orario, e la pagina TEMPO

- **`cycles` ha un discriminante di run.** Chiave primaria a tre colonne
  `(run_id, valve_id, cycle_id)`, migrazione idempotente applicata al database
  vero. Chiude la domanda aperta «un database, un run»: era stata scelta la terza
  via delle tre elencate, cioe' la cura vera invece dell'aggiramento. Senza di
  essa non poteva esistere una baseline sana accanto a un run guasto.
- **Le tabelle di intelligenza si rigenerano, non si migrano.** Davanti alla
  scelta fra svuotare previsioni e allarmi e ricalcolarli sul run nuovo, oppure
  portare `run_id` anche li' (mezza giornata), l'utente ha scelto la prima.
  Conseguenza accettata: il vecchio run da un giorno conserva i cicli ma non ha
  piu' diagnosi associata. Portare la chiave di run anche alle tabelle derivate
  resta lavoro da fare quando il percorso live si accendera' davvero.
- **Un riepilogo precalcolato, non un tetto piu' alto.** Alzare `SERIES_SPAN_MAX`
  e basta avrebbe prodotto una pagina da 53 secondi: misurato prima di scegliere.
  La grana e' l'ora, per `(run, ora, valvola)`.
- **I bordi di finestra si leggono, non si arrotondano.** Le finestre dell'OEE non
  sono allineate all'ora, quindi la somma di secchielli e' esatta solo
  all'interno: i due bordi parziali si leggono da `cycles`, al massimo un'ora
  ciascuno. L'arrotondamento all'ora piu' vicina e' stato escluso per contratto —
  avrebbe prodotto numeri plausibili e falsi, che e' la categoria di errore contro
  cui il progetto ha gia' una regola.
- **Il riepilogo contiene solo ore complete.** Un secchiello presente e' sempre
  un'ora finita, cosi' non serve una colonna «completo si'/no» da tenere
  allineata. L'ora in corso la legge il lettore da `cycles`.
- **Nessun troncamento silenzioso.** Quando una serie non entra nel tetto dei
  punti, la grana si dirada e la risposta **dichiara** quale grana ha risposto. Il
  comportamento precedente — tenere gli ultimi N punti e perdere il resto senza un
  segno — e' stato trattato come difetto, non come limite.
- **La qualita' per valvola nel tempo e' una route, non un campo in piu'.**
  `GET /valves/quality/series` risponde alla domanda senza modificare il contratto
  di `/machine/oee/series` ne' `quality_detail.per_valve`.
- **`quality: null` e `quality: 0.0` sono fatti diversi.** Zero cicli significa
  «non misurata»; cicli presenti tutti scartati significa «tutti scarti». La
  valvola 8 dopo inizio luglio produce il secondo, non il primo.
- **La navigazione nel tempo scelta dall'utente e' la striscia trascinabile.**
  Fra tre varianti costruite col metodo gia' adottato — pacchetto identico su
  disco, stesso modello e stesso effort, unica variabile il principio
  organizzativo, consegna come indirizzi cliccabili — ha scelto quella con i due
  mesi sempre in vista e la finestra che si trascina e si allarga. La motivazione
  e' registrata come vincolante: *«hai tutto controllo di dove guardare e la
  lunghezza del periodo»*, contro la variante col cursore unico che fondeva le due
  scelte in un comando solo. **Non fondere comandi che governano grandezze
  distinte.**
- **Il contenuto delle varianti e' rimasto identico apposta.** Se fosse cambiato
  anche quello, il giudizio non sarebbe stato attribuibile alla navigazione e la
  lezione non sarebbe esistita.
- **Prima di allargare l'orizzonte dei dati si verifica che l'interfaccia sappia
  attraversarlo.** Sessanta giorni sono stati generati e caricati mentre l'API ne
  serviva due: tre ore di simulazione e una migrazione di schema per dati non
  visibili. E' la regola simmetrica a quella gia' in vigore — *prima di costruire
  una pagina, misurare se i dati la sostengono* — e vale nella direzione opposta.
- **Il verdetto in una cella numerata sta sul bordo, non sul riempimento.** Le
  due fasce piene che portavano il verdetto delle due carte nella striscia di
  CARTA sono state respinte perche' comprimevano il numero della valvola:
  *«i rettangoli un po' rendono il numero meno chiaro»*. Sostituite da una riga
  di 5 px sotto il numero.
- **Due valori da confrontare stanno accostati, non ai due estremi.** Fra le
  cinque forme messe a confronto, quella con un filo in alto e uno in basso e'
  stata scartata — *«guardare uno in alto e un altro in basso li separa un po'
  troppo»* — a favore di una riga sola divisa a meta'. La distanza fra due segni
  codifica quanto sono legati.
- **Le varianti di un singolo elemento si guardano in un artefatto di
  anteprime.** Per un elemento dentro una schermata gia' accettata non si rifa'
  l'innesto nella pagina a ogni ipotesi: si pubblica un artefatto con tutte le
  opzioni una sotto l'altra, a grandezza reale e con la palette vera, stessi
  stati incolonnati. E' l'utente che ha proposto il metodo. Resta invariato il
  processo delle tre varianti vere per una schermata strutturalmente nuova.
- **Una finestra di OEE che comincia prima dei dati si dichiara parziale.** Fra
  quattro opzioni — dichiarare, contare il mancante come fermo, rifiutare la
  risposta, lasciare com'e' — l'utente ha scelto di dichiarare. L'API continua a
  rispondere e a dare i numeri che sa dare, ma dice quanta finestra non ha storia
  e quanta. Contare il tempo mancante come fermo e' stato scartato perche'
  sarebbe un numero inventato: in questo progetto i dati assenti si dichiarano.
- **`degraded` non allarga il proprio significato.** Resta «A, P o Q sono
  nulli». Una finestra parziale produce tutti e tre i numeri, quindi non e'
  degradata: porta un marchio proprio. Allargare `degraded` avrebbe fatto
  sparire l'OEE dalle pagine all'inizio dello storico, che nessuno ha chiesto.
- **Un difetto trovato dentro un artefatto secondario si segue fino alla sua
  causa, anche se la causa sta altrove.** La contraddizione dell'OEE sembrava un
  difetto delle sei fixture congelate; il worker ha dimostrato che le fixture
  replicano fedelmente l'API e si e' fermato invece di correggerle. La
  correzione e' andata dove stava la causa, e li' vale anche per le pagine vive.

## 2026-08-21, identità del ciclo edge

Il flow edge identifica una chiusura solo da `ValveNN.LastCycleId`.
`Machine.DataReady` non identifica una valvola.

Il builder rifiuta un envelope senza `LastCycleId`. Non usa `cycleCounter` come
fallback.

## 2026-08-21, stop della misura live del Blocco A

Il deploy del flow corrente usa il contenuto letto dall'Admin API Node-RED e un
deploy `full`, con backup della copia precedente del container. Non richiede il
riavvio del container e non tocca il database.

La prova realtime si ferma se la subscription OPC UA perde continuità. Nel
tentativo 4 la subscription è scaduta dopo circa 30 secondi e il server ha
emesso `BadNoSubscription`. I 269 eventi già scritti restano evidenza di
preflight, non una misura di dieci minuti. La correzione della subscription è
una nuova attività e non è stata tentata nel Blocco A.

## 2026-08-22, la corsa che la dashboard mostra (Blocco C)

La dashboard mostra **`storico_60d`**, i sessanta giorni di storico. Decisione
dell'utente, presa dopo aver aperto le due pagine vere a confronto.

Il KV `current_run_id` resta su `storico_60d` e non va spostato senza una nuova
decisione. Le corse live restano nel database come evidenza del percorso live
chiuso il 22 agosto, ma nessuna pagina le mostra.

La ragione misurata: sulla corsa live più lunga di quel giorno
(`live_20260822_run7`, 28 minuti, 17.978 cicli) `machine/oee/series` risponde con
**1 punto** contro i 179 dello storico, e l'OEE del turno non è calcolabile. Le
quattro pagine accettate sono state disegnate e giudicate su sessanta giorni.
Puntate su una corsa appena nata si vedono mezze vuote e sembrano rotte.

Il percorso funziona in tutti e due i casi. Passare al live, quando una corsa
avrà qualche ora di dati, costa una riga di configurazione.

## 2026-08-22, la pagina CARTA è chiusa

L'utente ha accettato **CARTA (`/k1/`)** dopo averla usata sui dati veri:
*«la pagina carta per me va bene cosi»*. La dashboard ha **cinque pagine
accettate**: MACCHINA, VALVOLE, OEE, TEMPO, CARTA.

`k1o/`, `k2/`, `k3/` restano su disco come varianti e origine dell'innesto.

Resta aperto un solo punto, che è di navigazione e non di contenuto: nessuna
delle altre quattro pagine rimanda a `/k1/`. Il menu è cresciuto per accumulo e
ogni pagina rimanda solo alle pagine nate prima di lei.

## 2026-08-22, `run_id` sulle route degli allarmi

`/alerts` e `/alerts/history` accettano `run_id`, come tutte le altre route di
lettura dal 22 agosto. Il default non cambia: senza il parametro, la risoluzione
resta il KV `current_run_id`.

Il motivo non è la comodità. Senza il parametro, una dashboard puntata su una
corsa diversa mostrava i grafici di quella corsa e la fascia degli allarmi del
KV, cioè due macchine diverse nella stessa schermata.

## 2026-08-22, il guscio a fixture è superato

Le sei fixture congelate in `.scratch/dashboard-v6/fixtures/` **non si
rigenerano**. Decisione dell'utente, presa dopo la misura di chi le legge.

Chi le legge oggi: solo il guscio congelato sulla porta 8077
(`.scratch/dashboard-v7/server.py`) e alcuni script di analisi in `.scratch/`.
Nessuna delle cinque pagine accettate le tocca, e nessun test della suite fa
asserzioni sui loro valori. `pipeline/` le nomina solo dentro commenti.

Il difetto che contava era un altro, ed è già chiuso: l'OEE gonfiato quando la
finestra comincia prima dei dati toccava le pagine vive, e l'API ora dichiara la
finestra parziale (`availability_detail.uncovered_s`, `source.window_partial`,
`pipeline/api.py:915`). Le fixture ereditavano quel difetto, non lo causavano.

Rigenerarle avrebbe richiesto di risimulare corse di guasto con profili di
fermata confrontabili, cioè molte ore, per un termine di paragone che nessuna
pagina viva usa più.

## 2026-08-22, l'ambiente Python resta su questa macchina

Il progetto non deve girare fuori da questa macchina. Nessun lavoro di blocco
delle versioni.

Il limite resta noto e dichiarato: la suite passa appoggiandosi ai
`site-packages` dell'utente e all'archivio uv via `PYTHONPATH`. Chi volesse
portare il progetto altrove deve prima bloccare l'ambiente.

## 2026-08-22, un menu unico sulle cinque pagine

Ogni pagina rimanda alle altre quattro, sempre nello stesso ordine: MACCHINA,
VALVOLE, OEE, TEMPO, CARTA. Decisione dell'utente.

Il menu era cresciuto per accumulo e ogni pagina rimandava solo alle pagine nate
prima di lei. CARTA, accettata oggi, non era raggiungibile da nessuna parte.

## 2026-08-23, la sensibilità di rilevamento va bene così

L'utente ha accettato la taratura **K=5/N=150** come definitiva: *«quella
sensibilità basta»*. La domanda era aperta dal 21 agosto e chiedeva se servisse
un aggregatore con un vincolo di latenza dichiarato.

Non serve. La latenza di prima apertura resta quella misurata e dichiarata: circa
1 h 15 min sulla valvola 30, 5 h 53 min sulla 21, 9 h 36 min sulla 8, e fra
12 h 43 min e 15 h 12 min sulle valvole 13-18. Il criterio che conta è la
copertura, non la prontezza: K=5/N=150 trova tutti e nove i guasti iniettati
senza falsi allarmi.

Conseguenza su M11: resta la classificazione del modello, cioè il silenzio sulla
valvola 21. **Non è più un problema di taratura.** Chi riapre M11 non deve
rimettere in discussione K e N.

## 2026-08-23, la pulizia del disco è fatta

L'utente ha confermato i nomi corretti delle varianti scartate, dopo che
l'approvazione precedente era stata data su nomi inesistenti (`ta/`, `tc/`).

Tolte da `.scratch/dashboard-v7/`: `pa/` e `pb/` (TEMPO, la viva è `pc/`), `v2/`
e `v3/` (VALVOLE, la viva è `v1/`), `b/` e `c/` (MACCHINA, la viva è `a/`).
Tolti dalla radice: `180`, `nul`, `e.g`, `tmp_report.txt`, `_tmp_m9_repro/`.
Tolta `.playwright-mcp/`.

**`.pi-subagents/` resta**, 331 MB. Decisione dell'utente: sono le trascrizioni
dei run degli altri agenti, cioè l'unica traccia di come sono stati ottenuti
certi risultati, e non si rigenerano. Chi cerca spazio su disco guardi altrove.

## 2026-08-23, il parametro `scn` sparisce dagli indirizzi quando non serve

`collegaNav` in `comune/dati.js` aggiunge `?scn=` ai collegamenti del menu solo
se lo scenario **non** è quello predefinito.

Contro il proxy sui dati veri lo scenario è ignorato di proposito e resta sempre
il predefinito: il parametro compariva nella barra dell'indirizzo a ogni cambio
pagina senza significare niente. Contro il guscio a fixture, dove gli scenari
esistono davvero, uno scenario scelto viene ancora portato di pagina in pagina.

## 2026-08-23, la serie OEE resta com'è

L'utente ha deciso di **non correggere** il secondo scarso che
`GET /machine/oee/series` costa quando l'istante chiesto non cade su un'ora
esatta: *«lascia così»*. La decisione è presa dopo che le tre strade gli sono
state spiegate con i costi di ciascuna.

Le due correzioni possibili, e perché sono state lasciate:

- **Arrotondare all'ora gli istanti della serie** azzera il costo, ma il punto
  più recente diventa vecchio fino a un'ora e soprattutto rompe la garanzia che
  l'ultimo punto del grafico e il valore corrente in cima alla pagina siano lo
  stesso numero, calcolato dalla stessa funzione. Quella garanzia è deliberata.
  **Questa strada è scartata**, non solo rinviata: scambia una proprietà vera
  con una velocità che nessuno ha chiesto.
- **Un riepilogo a grana di minuto** non toglie niente e non cambia nessun
  numero, ma è una tabella nuova, una migrazione su 36 milioni di righe e una
  cosa in più da tenere aggiornata. **Resta la strada giusta** se un giorno quel
  secondo darà fastidio davvero.

Il costo accettato: circa un secondo, solo sul grafico dell'andamento, solo al
caricamento della pagina.

Quello che questa sessione lascia in eredità non è una correzione ma una
diagnosi: la causa è l'allineamento all'ora, non il parametro `at`, e non è né
un indice mancante né un indice gonfio. Vedi `STATE.md`, 2026-08-23. Chi
riapre il tema non deve rimisurare da capo, e soprattutto non deve inseguire la
differenza fra 3,3 e 5,3 secondi: è la stessa misura con la cache in due stati.

## 2026-08-23, la deriva lunga settimane resta fuori

Riproposta all'utente come l'unica voce in elenco che direbbe qualcosa di nuovo
sul prodotto, e **rifiutata per adesso**: *«per adesso quello della deriva no»*.

La domanda a cui risponderebbe resta senza risposta e va detta com'è: oggi il
modello rileva ogni guasto in circa due ore — sulla valvola 8 passa da rumore a
saturazione fra le 04:00 e le 06:00 del 3 luglio — quindi **non c'è spazio per
misurare quanto anticipa il degrado**, e non si può affermare che serva a
prevenire invece che solo ad accorgersi. Chi la riaprirà non deve rifare
l'analisi: è scritta in `OPEN_QUESTIONS.md`, e manca solo la run.

## 2026-08-23, i tre difetti dei generatori di fixture non si correggono

`history_extract_ef.py` che scrive storici vuoti, la classe di difetto latente
in `predict.py::alert_rows` e la voce su `alert-history.json` vivono **solo**
dentro `.scratch/dashboard-v6/fixtures/`, cioè i generatori delle fixture
congelate.

Quelle fixture sono superate dal 22 agosto e non si rigenerano: nessuna delle
cinque pagine accettate le legge e nessun test fa asserzioni sui loro valori.
Correggere quei tre difetti non avrebbe effetto su niente di osservabile.
**Restano scritti, non corretti.** Chi un giorno dovesse rimettere in vita quel
guscio li trova documentati.

Il quarto difetto della stessa lista, il gate `validate.py`, **era già stato
corretto** e non se n'era accorto nessuno: eseguito il 2026-08-23 dà
`OK: nessun fallimento` sui sei scenari.

## 2026-08-23, M11 — la correzione misurata non si spedisce

Il riaddestramento con set aumentato (copie attenuate delle finestre guaste)
recupera la valvola 21 sullo scenario a 60 giorni e **perde su `val`**: macro-F1
da 0,7704 a 0,7122, cioè 5,8 punti, su 147 finestre `opening_delay` e 59
`flowmeter_dropout` che il modello spedito indovina. **Scartato, non rinviato.**

Il motivo per cui non si ripara con un'altra taratura: `opening_delay` e
`restriction` stanno sullo stesso asse z(`mean_fillingtime`) e differiscono solo
in ampiezza. Nessun riscalamento del confine le separa. Chi riapre M11 non deve
riprovare l'aumento del set né la normalizzazione robusta — sono misurati
entrambi, i numeri stanno in `RECENT_WORK.md` del 2026-08-23.

Vincolo scoperto e da rispettare: prima di spedire un modello qualsiasi va
bumpato `manifest.yaml:code_version`, o aggiunto `model_version` al sidecar.
Altrimenti le predizioni nuove sono indistinguibili dalle 723k vecchie e la
cronologia K/N le mescola.

Resta da decidere se aprire lavoro sulle feature o chiudere M11 sull'allarme che
già funziona. Vedi `OPEN_QUESTIONS.md`.

## 2026-08-23, M11 si chiude sull'allarme e il lavoro va sulla resa

L'utente ha scelto la strada raccomandata: non si apre lavoro nuovo sulle feature
per far parlare il modello sulla valvola 21. **M11 e' chiuso.** L'allarme la
segnala per il 93,0% della corsa, quindi il manutentore e' avvisato; quello che
mancava era il nome del guasto, e mancava su tutte e nove le valvole in allarme.

**Le pagine non stampano piu' `alert.fault_type`.** Quel campo vale sempre
`score_aggregation` — la lineage tecnica decisa il 21 agosto — e a schermo si
leggeva tale e quale. Il nome viene ora da `last_prediction.predicted_label`, che
`/valves` porta gia' per tutte e trentacinque: **nessuna route nuova, nessuna
chiamata in piu', nessun tocco al motore o all'API**.

**Dove i due strumenti non concordano, la riga lo dichiara.** Una valvola in
allarme che il modello dice sana si legge «il modello la dice sana». La forma e'
stata scelta fra quattro varianti costruite sui dati veri e sul foglio di stile
vero (<https://claude.ai/code/artifact/6137ca77-0262-497e-b133-85001c7fea43>).
Le due scartate: «non classificato», che nasconde il fatto che una risposta c'e'
ed e' sbagliata; e «in allarme · <nome>», che duplica il titolo della sezione.

**Sugli allarmi la data porta il giorno.** `ora()` dava il solo orario, e un
allarme aperto il 3 luglio si leggeva «da 07:05». Corretto in `giornoOra()` su
entrambe le pagine. Non e' una rifinitura: era una data sbagliata.

Restano fuori: le altre tre pagine, la lineage dentro il motore, il modello, il
normalizzatore e l'API. La voce aperta sulla provenienza del modello resta tale —
riguarda il giorno in cui si spedisse un modello nuovo, che ora non succede.

## 2026-08-23 · La guida tecnica eredita il lessico della dashboard

I due artefatti di presentazione non hanno un linguaggio visivo proprio. Le
tinte, la barra, i riquadri con barra-titolo e la regola del colore sono copiati
da `.scratch/dashboard-v7/comune/lessico.css`, cioe' dalla dashboard che
l'utente ha accettato il 2026-08-19. Motivo: chi passa dalla guida alla
dashboard non deve accorgersi di aver cambiato prodotto.

La regola del colore vale intera. Nella guida `--attenz` compare in quattro punti
soltanto (`SEI QUI`, bordo della nota aperta, riga di codice accesa, cella fuori
bersaglio) ed e' un tetto, non una media.

**Il metodo e' quello che ha fatto accettare la dashboard.** Le scelte di
struttura sono state messe davanti all'utente come anteprime funzionanti da
cliccare, mai come schizzi o prosa; la prima schermata e' stata costruita in tre
varianti indipendenti con nomi neutri e nessuna raccomandazione; si consegna una
schermata alla volta e ci si ferma finche' non e' approvata. Appena approvata, la
grammatica si estrae in un documento e in pezzi comuni condivisi, cosi' le tappe
successive la ereditano invece di reinventarla.

**Niente em dash e niente en dash nel testo dei due artefatti.** L'utente li
conta. Dove servirebbe uno stacco si chiude la frase o si mette una virgola; nei
separatori grafici si usa il punto medio, che e' gia' l'idioma della dashboard;
negli intervalli numerici il trattino semplice. La ripulitura si fa prima di
pubblicare e copre anche le stringhe costruite da JavaScript e gli `aria-label`.


## 2026-08-23 · Chi scrive non e' chi verifica, nemmeno sulla lingua

L'utente ha bocciato due volte il testo della presentazione: la prima per gli em
dash, la seconda per un titolo costruito per antitesi. Ha poi chiesto
esplicitamente che la ripulitura venisse **verificata da un revisore separato**,
perche' fatta da chi ha scritto non funziona.

Da qui in avanti, per i due artefatti di presentazione: la ripulitura si fa prima
di pubblicare, e il risultato passa da un revisore con mandato ostile che produce
un elenco di citazioni esatte, non un giudizio. Vale la stessa regola gia' scritta
per il codice: **chi realizza non verifica se stesso**.

La revisione del 2026-08-23 ha prodotto 36 correzioni di scrittura, tre errori di
fatto e un disallineamento fra testo e disegno che nessun controllo automatico
avrebbe visto. Il costo e' stato tre agenti in parallelo; il difetto piu' grave
che hanno trovato (la scena che diceva "valvola" mentre la prosa diceva
"rubinetto", con un numero di segno opposto) era invisibile sia al verificatore
sia a me.

**Il verificatore automatico non basta e non deve illudere.** `verifica.py` copre
solo le cose gia' costate una bocciatura, e la sintassi del JavaScript la vede
solo `node --check`, che va lanciato a parte: un apostrofo dentro una stringa
aveva rotto tre pagine in silenzio, con il verificatore tutto verde.


## 2026-08-24 · Le due M11, e la sorte di quella di sicurezza

Il progetto aveva due cose diverse che si chiamavano M11, e una delle due era
dichiarata chiusa. Chi leggeva `STATE.md` trovava scritto «M11 e' chiuso» e ne
concludeva che la roadmap IIoT fosse finita. Non lo era.

**Da qui in avanti i due nomi sono distinti e non vanno piu' usati nudi:**

- **M11-sicurezza** e' la milestone della roadmap (`docs/roadmap-iiot.md`, §7):
  security policy OPC UA con certificati e utenti, autenticazione e ACL sul
  broker, iniezione di guasti di comunicazione come scenari YAML, metriche di
  pipeline. Era segnata opzionale fin dal 2026-08-12 e non e' mai stata aperta.
- **M11-taratura** e' l'aggregazione score-only K=5/N=150 e la classificazione
  del modello sulla valvola 21, chiusa il 2026-08-23. E' quella a cui si
  riferiscono `STATE.md` e le voci precedenti di questo file.

### M11-sicurezza non si fa: POC accettato

Decisione dell'utente il 2026-08-24, davanti al quadro completo dei sei gradini.
**La milestone e' dichiarata fuori ambito e il POC e' accettato come tale.** Non
e' un rinvio: e' una chiusura. Chi riapre il progetto non deve rimetterla in
discussione, e chi cerca la sicurezza in questo repository deve trovare questa
riga prima di cercarla nel codice.

Il motivo non e' che la sicurezza non conti. E' che questo progetto gira su una
sola macchina, in locale, e non e' mai stato esposto: `mosquitto.conf` dichiara
da solo «plaintext su 1883, nessuna autenticazione», e il compose di Postgres
porta `POSTGRES_PASSWORD: plcsim` con il commento `POC`. Erano dichiarazioni
oneste di un prototipo, non debiti nascosti. Formalizzarle costa una riga;
onorarle costerebbe giorni e comprerebbe una proprieta' che nessuno esercita.

Le quattro voci restano scritte nella roadmap come ambito **non svolto e non
previsto**. Se un giorno la catena dovesse uscire da questa macchina, quelle
quattro voci sono la lista da riaprire, in quell'ordine.

### Cosa resta vero anche senza M11-sicurezza

La riconnessione OPC UA nativa non funziona sulla palette `node-red-contrib-opcua`
0.2.354 e la versione che la corregge non e' pubblicata su npm. E' un limite di
ambiente, non un difetto di questo codice, ed e' aggirato riavviando il
container: verificato a zero duplicati su 2683 eventi. Si chiude come
**documentato e aggirato**, non come lavoro aperto.
