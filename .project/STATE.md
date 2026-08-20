# Project state

Updated: 2026-08-20

## Project

PLC Sim V is a causal, layered simulator of an isobaric rotary can-filler. It
models a 35-valve carousel (26 active filling positions), virtual sensors, a
virtual PLC, cycle validation, telemetry, analytics, machine-learning inference,
and an IIoT path. Ground truth remains separate from every operational input used
by the virtual PLC or ML.

Canonical GitHub repository: `RazAndAlex/PLC-Sim-V`, private,
<https://github.com/RazAndAlex/PLC-Sim-V>. Local `main` tracks `origin/main`.
The repository follows a memory-only publication policy: project source,
datasets, generated outputs, screenshots, issue evidence, and local
infrastructure state are not part of the published history.

## Current objective and milestone

The current milestone is the visual completion of M10: a data-backed supervision
and diagnostic dashboard with a machine-level OEE home (L0), a 35-valve machine
view (L1), and valve drill-down (L2/L3). The non-visual M10 scope—PostgreSQL
operational storage, alert engine, read-only FastAPI observation API, and the M9
prediction migration—was documented as accepted on 2026-08-13.

**Latest dashboard status (2026-08-19): the dashboard is COMPLETE and ACCEPTED.**
Three pages — `MACCHINA · VALVOLE · OEE` — all accepted by the user, after six
versions rejected. Final verdict, verbatim, after using them linked together:
*"si mi piace e funzionano bene. oee mi va bene cosi come e'"*.

Live at `.scratch/dashboard-v7/` (`python .scratch/dashboard-v7/server.py`, port
8077): `/a/` how the machine is doing · `/v1/` which valve to open (the carousel,
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

**Non e' live.** Nessun processo alimenta il database in continuo; il ponte OPC UA
copre una valvola su 35.

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

- Superseded 2026-08-19: `pytest` 9.1.1 is available and the full suite was rerun
  — **433 passed, 4 warnings**, on Python 3.14.6. The four warnings are benign
  (two unregistered pytest markers, a Starlette/httpx deprecation).
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
3. Progettare con l'utente la seconda meta' del percorso live: oggi il ponte OPC
   UA copre una valvola su 35 e nessun processo alimenta il database in continuo.
4. Costruire la carta di controllo sulla media mobile di 46 cicli **come seconda
   variante** accanto a quella attuale, da confrontare sui dati veri.
5. Rigenerare le sei fixture su profili di fermata confrontabili, piu' i due
   difetti trovati al loro interno.
6. Indagare i due silenzi del modello: l'instabilita' di pressione che non muove
   la qualita', e il ritardo di apertura della valvola 21 che l'inferenza non
   riconosce.


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
