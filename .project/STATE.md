# Project state

Updated: 2026-08-14

## Project

PLC Sim V is a causal, layered simulator of an isobaric rotary can-filler. It
models a 35-valve carousel (26 active filling positions), virtual sensors, a
virtual PLC, cycle validation, telemetry, analytics, machine-learning inference,
and an IIoT path. Ground truth remains separate from every operational input used
by the virtual PLC or ML.

Canonical GitHub repository: not configured yet. The local Git repository was
initialized on `main` on 2026-08-14 and currently follows a memory-only
publication policy: project source, datasets, generated outputs, screenshots,
issue evidence, and local infrastructure state are not part of the intended
memory commit.

## Current objective and milestone

The current milestone is the visual completion of M10: a data-backed supervision
and diagnostic dashboard with a machine-level OEE home (L0), a 35-valve machine
view (L1), and valve drill-down (L2/L3). The non-visual M10 scope—PostgreSQL
operational storage, alert engine, read-only FastAPI observation API, and the M9
prediction migration—was documented as accepted on 2026-08-13.

The newest local implementation work adds the OEE data wire needed before the
dashboard mock: timestamped cycles, machine-state history, state/counter writers,
cycle backfill, and `GET /machine/oee`. This work is visible in the source and
tests dated 2026-08-14, but it has not been revalidated in this memory-publication
session because the active Python installation does not provide `pytest`.

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

## Active work

- Validate the 2026-08-14 OEE backend changes and any PostgreSQL migrations in an
  environment with the locked Python dependencies and the isolated test database.
- Iterate the dashboard with the user from the ratified information hierarchy,
  using reliable volume/filling signals first and contextualizing noisier tail
  signals.
- Connect the dashboard only to the observation API/operational database; it must
  not read directly from the simulator or ground truth.

## Known issues and limitations

- The active `python` executable during memory publication lacks `pytest`; no test
  suite was rerun on 2026-08-14. The latest documented pre-OEE results are 252
  core tests passed, 65 pipeline tests passed with one warning, and successful M9
  and M10 acceptance checks on 2026-08-13.
- PostgreSQL integration tests require the dedicated test database and skip when
  the server is unavailable. They must never write to the operational `plcsim`
  database.
- The dashboard has a detailed specification but no accepted final visual design.
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

1. Restore the locked test environment and run the new cycle-storage, backfill,
   storage, OEE, API, and regression tests; record the exact result.
2. Exercise OEE against timestamped cycles and OMAC transitions, including empty
   and partially backfilled windows.
3. Produce and review the first dashboard mock with the user, then implement the
   accepted L0/L1/L2 flow against FastAPI.
4. Run the M10 end-to-end demo for healthy, fault, and recovery scenarios.
5. Revisit tail calibration, alert calibration, and group-level diagnostics only
   after the M10 visual gate is closed.

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
