# FillerStack

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/downloads/)
[![tests](https://img.shields.io/badge/tests-567%20passed-brightgreen.svg)](#running-it)

The whole stack behind a rotary can-filler, from the valve to the screen: a
causal simulator of the machine, an OPC UA server, a Node-RED edge client, MQTT,
a Python consumer writing partitioned Parquet, feature extraction, an online
classifier, an alert engine, a read-only API, and a supervision dashboard for
maintenance and PLC technicians.

The simulator is the instrument, not the point. It exists so that the diagnosis
at the far end can be *scored*: the machine knows which valve it broke, and
nothing downstream does.

The machine is a 35-valve carousel with 26 active filling positions. Everything
downstream sees only what a real installation would see. That goes for the PLC
logic, the KPIs, the model and the dashboard alike. Ground truth stays inside the
simulator and is never an input to anything that makes a decision.

## Why it is built this way

Most PLC simulators replay recorded data. This one generates it causally, in
layers, so that a fault has a physical origin and the signature it produces
downstream is a consequence rather than an annotation:

```
process → sensors → virtual PLC → KPIs → telemetry → OPC UA
                                                        ↓
   dashboard ← API ← alerts ← predictions ← features ← Parquet ← MQTT ← Node-RED
```

Each stage only knows its input. The virtual PLC does not know which valve was
given a fault; the model does not know either; the alert engine sees a score, not
a cause. That separation is what makes the chain worth measuring. A detection is
a real detection, not a lookup.

## What is in here

| Path | What it holds |
|---|---|
| `plcsim/` | The simulator: plant physics, sensors, virtual PLC, validation, scenarios, OPC UA server, the ML pipeline |
| `pipeline/` | MQTT ingestion, feature store, online inference, alert engine, PostgreSQL storage, the read-only FastAPI |
| `dashboard/` | The five supervision pages, plus the live server and the recorded-data server |
| `edge/` | Node-RED flows, Mosquitto and PostgreSQL compose, the live-run preflight, edge tests |
| `docs/` | Architecture decision records (`adr/`), the IIoT roadmap, an anonymised case study |
| `scenarios/` | Fault scenario definitions. Each fault has a severity ramp, not a switch |
| `tests/` | The suite (567 tests) |
| `.project/` | Project memory: state, decisions, open questions, recent work |

`CONTEXT.md` is the glossary, `PRODUCT.md` describes the dashboard and who it is
for, and `AGENTS.md` holds the working protocol.

The Python package is still called `plcsim`, and the containers still carry a
`plcsim-` prefix. That is deliberate. The project was renamed once the old name
started doing the wrong job — it named the simulator, and readers took the whole
thing for one. Internal identifiers do not have that job, and renaming them would
touch two frozen core files and break a persistent MQTT session for nothing.

## Running it

Python 3.14, plus Docker for the edge services. Dependencies are pinned, without
ranges, in `requirements.txt`. The determinism fingerprint is a same-environment
guarantee, and a loose pin breaks it.

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
```

**Generate a day of data.** Deterministic on seed; the default anchor is
`2026-06-01T00:00:00Z`.

```bash
.venv/Scripts/python -m plcsim.run --days 1 --seed 42 --out work/my_run
.venv/Scripts/python -m plcsim.run --days 1 --scenario scenarios/m4_healthy.yaml
```

**Open the dashboard.** Double-click `dashboard.bat` in the project root. It
works with nothing installed but Python.

If PostgreSQL is running, it starts the API and serves live data. If it is not,
it serves a recorded snapshot of the same API responses instead, and the
selector at the top of the page carries the date the snapshot was taken. The
numbers are real either way, produced by the simulator and carried through the
whole chain; in the second case they are frozen, and the page says so rather
than implying they are current.

Five pages: MACCHINA (machine state and today's alarms), VALVOLE (all 35 valves
as the carousel they physically are), OEE, TEMPO (trends), CARTA (control
charts). They are in Italian, because the technicians who would use them are.

Closing the window shuts everything down. The child processes are bound to a
Windows Job Object with `KILL_ON_JOB_CLOSE`, because sharing a console was not
enough to kill them.

The snapshot lives in `dashboard/demo/` and is regenerated from a live chain;
`dashboard/server_demo.py` documents how. Move a date range in demo mode and you
step outside the recording: the page tells you, instead of showing a period you
did not ask for.

**Run the live chain.** Start it in order, and check the order with the preflight
rather than from memory:

```bash
.venv/Scripts/python edge/scripts/preflight_live_run.py
```

It exits 0, 1, or 2. Three states, not two, because a check that could not run is
not a check that passed.

**Run the tests.** Wait for PostgreSQL to report healthy first:

```bash
docker inspect plcsim-postgres --format "{{.State.Health.Status}}"   # must be: healthy
.venv/Scripts/python -m pytest -q                                     # expect: 567 passed
```

This matters more than it looks. Against a container that is still starting, 154
of the 567 tests skip silently and pytest still reports success. The number to
compare against is 567; any other total is itself a signal.

## What works, and what does not

Working and measured:

- The simulator reproduces the statistical signature of its reference baseline,
  and regenerating a run at the same seed is byte-identical (verified by SHA-256
  against an anchor run from two weeks earlier).
- The chain runs end to end. A ten-minute live check moved 193 raw records, 187
  cycles and 3 predictions through all three stages.
- The alert engine opens on 5 windows out of 150 above threshold, per valve. On a
  full-history replay it caught 9 of 9 injected faults with zero false positives.
- The dashboard is a five-page supervision UI built for technicians, accepted
  after six rejected versions. It reads only the API's GET routes, never the
  database and never the simulator. It opens without any infrastructure, on a
  recorded snapshot of those same routes.

Not here, and worth knowing before you look for it:

- **There is no prognosis.** The model reads 50 past cycles and labels the *last
  cycle of the window*, which is the present and not the future. It answers "does
  this look wrong", not "how long until it fails". Remaining useful life is planned for a
  second version; `.project/DECISIONS.md` records why it was not in the first.
- **The 7-class fault classifier is nearly decorative.** What actually drives the
  alerts is `1 − P(healthy)` from the same model. The class label only puts a
  name on screen, and on one fault mode it disagrees with the alert.
- **Security is an accepted POC.** The broker is plaintext with anonymous access,
  and there are no OPC UA certificates. This is written down as out of scope, with
  a stated condition for reopening it: the day the chain leaves a single local
  machine.

## Provenance

This is personal work, written on my own machine. It is not company work and
contains no company material.

It started from two things: datasets generated by an existing PLC simulator for a
machine of this kind, and a written description of how that simulator worked. I
never had its source code. An earlier project of mine tried to reproduce it by
fitting distributions to those datasets, and reached a statistical match without
recovering the underlying waveform.

This simulator does not work that way. It opens a valve, integrates a flow, and
counts pulses; the filling time and tail time are consequences of that, not
values drawn from a distribution. Nothing of the earlier generator survives here.

What does cross over is a calibration, not an implementation.
`plcsim/valve_params.csv` holds four measured averages per valve, from which the
physical constants of each valve are derived by inversion. It is what tells the
model how fast a healthy valve fills, not how the machine behaves. A handful of
constants come from the same reading: the 46-cycle driver period, the per-valve
phase offset, and the 250 ml recipe.

The original datasets are not in this repository and are not published with it.

## Status

The IIoT roadmap is closed: milestones M6 through M10 are up and accepted, and the
security milestone is closed as deliberately not done. Current state, the decisions
behind it and what is still open live in `.project/`. Read `STATE.md` first, and
believe a section heading only if it carries a date.

## License

MIT. See [LICENSE](LICENSE).
