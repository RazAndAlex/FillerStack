# Overview

Stable purpose, boundaries, and vocabulary. For current status read `STATE.md`.

## What this project is

FillerStack is the **whole chain behind an isobaric rotary can filler** with 35
valves (26 active filling positions): a causal simulator of the machine, the IIoT
path that carries its signals, the model that reads them, and the **diagnostic
dashboard** at the far end. The simulator is what makes the diagnosis
measurable — it knows the fault, and nothing downstream does.

The simulator is layered and deterministic: explicit clock, scenario and fault
engine, physical plant, virtual sensors, virtual PLC state machines, and cycle
validation. On top of that sit cycle telemetry, analytics, a machine-learning
inference path, an operational PostgreSQL store, an alert engine, a read-only
observation API, and an IIoT edge path.

The dashboard is a **supervision surface for mechanical and PLC technicians**.
It is not a KPI cockpit and not a management report. Its recurring failure mode,
across six rejected versions, was a screen where the user could not tell what to
look at.

## Who reads it

Maintenance and PLC technicians at a filling line, reading a screen at roughly
0.5 to 2 metres in variable light. Everything else follows from that.

## Boundaries that hold across versions

- **Ground truth is separate.** The fault timeline, the labels, and the
  ground-truth stream are never available to the virtual PLC, to the ML path, or
  to anything on the operational path.
- **The dashboard's only data source is the read-only observation API**
  (`pipeline/api.py`). It never reads the simulator or the database directly.
- **The simulator geometry is fixed.** `rotation_ms` is load-bearing: zone
  timing, the encoder limit that closes a valve, the useful-window phenomenon on
  the last slots, and the driver oscillation frequency all descend from it.
  Changing it means re-deriving the physical model and retraining the ML model.
- **No invented numbers.** Where a measurement does not exist the response is
  `null`, an empty list, or a declared degraded state with a reason. A missing
  value is never rendered as zero.
- **Only the user accepts a screen.** An internal gate, a blind judge panel, or
  a green test suite is evidence, never acceptance.

## Vocabulary

- **Valve** — one of 35 filling positions on the carousel. Valves differ from
  each other by construction: filling time, tail time, and quality all vary by
  valve even with no fault present.
- **Cycle** — one fill by one valve. The unit of telemetry. Identified by
  `(run_id, valve_id, cycle_id)`; `cycle_id` restarts at 1 for every run, which
  is why `run_id` exists.
- **Run** — one execution of the simulator, loaded into the operational store as
  a coherent stream of cycles. Several runs can coexist in one database.
- **Filling time / tail time / tail pulse / pulse count** — the per-cycle KPIs
  that carry the physical signature of a fault.
- **Encoder limit** — the carousel window running out before the target is
  reached. A cycle that closes for this reason has not failed on quality alone.
- **Baseline** — a declared healthy window, frozen as a key-value entry, against
  which each valve is compared **to itself**.
- **OEE** — availability times performance times quality, over a rolling window.
  In this simulator performance carries no information, because loss of speed is
  not modelled.
- **OMAC state** — the machine state history that gives availability its
  denominator.
- **Scenario** — a YAML description of a run, including the faults injected into
  it, their onset cycle, and their ramp.

## Authoritative sources

Memory is a navigational summary. The implementation, the Git history, the
architecture decision records under `docs/adr/`, and the scenario files are the
primary evidence.

`PRODUCT.md`, `CONTEXT.md`, `CLAUDE.md`, the handoff documents, and all source,
datasets, generated outputs, and local infrastructure state stay **local by
policy** and are not part of the published history. See `PUBLICATION.yaml`.

## A note on language

`OVERVIEW.md` and `STATE.md` are in English. `DECISIONS.md`, `OPEN_QUESTIONS.md`
and the newer entries of `RECENT_WORK.md` are largely in Italian, which is the
working language of the project.
