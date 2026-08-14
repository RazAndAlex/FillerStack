# Active decisions

Updated: 2026-08-14

This file summarizes active decisions evidenced by `CONTEXT.md`, `docs/adr/`, and
the current implementation. The ADRs remain authoritative for detail.

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
- OEE uses rolling 8-hour shift and 24-hour day windows. Availability derives from
  OMAC state history, Performance from actual timestamped cycles versus target
  speed during Running time, and Quality from `fill_quality_ok`; insufficient
  history returns a degraded response instead of fabricated values.

Relevant ADR: 0021; dashboard contracts are in `.scratch/dashboard/` locally.
