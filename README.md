# FillerStack

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/downloads/)
[![tests](https://img.shields.io/badge/tests-567%20passed-brightgreen.svg)](#quickstart)

The whole stack behind a rotary can-filler, from the valve to the screen.

```
process → sensors → virtual PLC → KPIs → telemetry → OPC UA → Node-RED
                                                                  ↓
   dashboard ← API ← alerts ← predictions ← features ← Parquet ← MQTT
```

Each stage knows only its input. The virtual PLC does not know which valve was
given a fault; the model does not know either; the alert engine sees a score,
not a cause. The simulator is the instrument, not the point. It exists so you
can score the diagnosis at the far end: it knows the fault, and nothing
downstream does.

The machine is a 35-valve carousel with 26 active filling positions.

## The numbers

| | |
|---|---|
| Test suite | **567 passed**, zero skipped, against a healthy PostgreSQL |
| Fault detection | **9 of 9** injected faults caught, **zero** false positives |
| Alert rule | 5 windows out of 150 above threshold, per valve |
| Determinism | same seed → **byte-identical** Parquet (SHA-256, two weeks apart) |
| One simulated day | 604,398 cycles in 288 s, about 300× real time |
| Live chain, ten minutes | raw +193, cycles +187, predictions +3, all three stages alive |

## Quickstart

Python 3.14, and Docker only if you want the live chain. Dependencies are
pinned without ranges: the determinism fingerprint is a same-environment
guarantee, and a loose pin breaks it.

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
```

**See the dashboard.** Double-click `dashboard.bat`. Nothing but Python is
needed: with no database running it serves a recorded snapshot of the API's own
responses, and the selector at the top of the page carries the date it was
taken. Real numbers, frozen, and the page says so.

**Generate a day of data.**

```bash
.venv/Scripts/python -m plcsim.run --days 1 --seed 42 --out work/my_run
```

**Run the live chain.** Check the startup order with the preflight rather than
from memory. It exits 0, 1, or 2, because a check that could not run is not a
check that passed.

```bash
.venv/Scripts/python edge/scripts/preflight_live_run.py
```

**Run the tests.** Wait for PostgreSQL to report healthy first. Against a
container that is still starting, 154 of the 567 tests skip silently and pytest
still reports success.

```bash
docker inspect plcsim-postgres --format "{{.State.Health.Status}}"   # healthy
.venv/Scripts/python -m pytest -q                                    # 567 passed
```

## Not production-ready

Said plainly, because the alternative is letting someone find out.

- **There is no prognosis.** The model reads 50 past cycles and labels the last
  one: the present, not the future. It answers "does this look wrong", not "how
  long until it fails". I plan to add remaining useful life in a second version.
- **The 7-class fault classifier is nearly decorative.** What drives the alerts
  is `1 − P(healthy)` from the same model. The class label only puts a name on
  screen, and on one fault mode it disagrees with the alert. The dashboard
  declares the disagreement instead of hiding it.
- **Security is an accepted POC.** Plaintext broker, anonymous access, no OPC UA
  certificates. Written down as out of scope, with a stated condition for
  reopening it: the day the chain leaves a single local machine.
- **One machine, one line, synthetic data.** Nothing here has run against real
  plant hardware.

## Where to start reading

**[Il viaggio del dato](https://razandalex.github.io/FillerStack/)**. Eleven
stops from the can to the screen, following one number: 2505 pulses, born
inside a can at 10:03 and read off a screen eleven handovers later. The page
explains every acronym before it uses one.

It is in Italian, like the dashboard, because the technicians who would use it
are.

## What is in here

| Path | What it holds |
|---|---|
| `plcsim/` | The simulator: plant physics, sensors, virtual PLC, validation, scenarios, OPC UA server, the ML pipeline |
| `pipeline/` | MQTT ingestion, feature store, online inference, alert engine, PostgreSQL storage, the read-only API |
| `dashboard/` | The five supervision pages, the live server and the recorded-data server |
| `edge/` | Node-RED flows, Mosquitto and PostgreSQL compose, the live-run preflight, edge tests |
| `docs/` | Architecture decision records, the IIoT roadmap, an anonymised case study |
| `sito/` | The published site: sources, the three build scripts, the grammar it obeys |
| `scenarios/` | Fault scenario definitions. Each fault has a severity ramp, not a switch |
| `tests/` | The suite |
| `.project/` | Project memory: state, decisions, open questions, recent work |

The Python package is still called `plcsim`, and the containers still carry a
`plcsim-` prefix. That is deliberate. I renamed the project once the old name
started doing the wrong job. It named the simulator, and readers took the whole
thing for one. Internal identifiers do not have that job, and renaming them
would touch two frozen core files and break a persistent MQTT session for
nothing.

## Provenance

This is personal work, written on my own machine. It is not company work and
contains no company material.

It started from two things: datasets generated by an existing PLC simulator for
a machine of this kind, and a written description of how that simulator worked.
I never had its source code. An earlier project of mine tried to reproduce it by
fitting distributions to those datasets, and reached a statistical match without
recovering the underlying waveform.

This one does not work that way. It opens a valve, integrates a flow, and counts
pulses; the filling time and tail time are consequences of that, not values
drawn from a distribution. Nothing of the earlier generator survives here.

What does cross over is a calibration, not an implementation.
`plcsim/valve_params.csv` holds four measured averages per valve, from which the
code derives each valve's physical constants by inversion. It says how fast a
healthy valve fills, not how the machine behaves. A handful of constants come
from the same reading: the 46-cycle driver period, the per-valve phase offset,
and the 250 ml recipe.

The original datasets are not in this repository and are not published with it.

## License

MIT. See [LICENSE](LICENSE).
