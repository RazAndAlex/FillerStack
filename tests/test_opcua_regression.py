"""Test automatici M6 — regressione bulk (spec §7, test 9).

Run healthy 1 giorno con `plcsim.run.run_days` bit-identico al riferimento
M5 (work/m5_healthy_1d, scenario_id 61, seed 42): valve_cycles/events/
ground_truth confrontate con lo stesso metodo AC-M5-1
(work/m5_acceptance_check.py check_ac1): drop 'scenario_id' + csv bytes
identici + stessa altezza/colonne. Verifica anche run_summary.json
(n_cycles == 604398, scenario_id 61).

Il run 1-day è il run VERO (~3.5-5 min su questa macchina: 208 s al
reference run M5): NON ridotto (la bit-identità richiede il run intero).

Marker `opcua` NON registrato (nessun pytest.ini/pyproject in repo — fuori
scope M6): attesa PytestUnknownMarkWarning, il test resta verde.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plcsim.config import SimConfig  # noqa: E402
from plcsim.run import run_days  # noqa: E402
from plcsim.scenario import load_scenario  # noqa: E402

pytestmark = pytest.mark.opcua

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "work" / "m5_healthy_1d"        # riferimento PINNATO M5 (seed 42)
EXPECTED_CYCLES = 604398
EXPECTED_SCENARIO_ID = 61


def _csv_bytes(df: pl.DataFrame) -> bytes:
    """CSV del DataFrame (metodo AC-M5-1: confronto su bytes, non hash)."""
    return df.write_csv().encode()


@pytest.mark.skipif(
    not (REF / "valve_cycles.parquet").exists(),
    reason=(
        "manca il riferimento pinnato M5 in work/m5_healthy_1d. È materiale di "
        "lavoro, escluso dal repository di proposito (.gitignore, "
        "PUBLICATION.yaml): senza, la bit-identità non ha un termine di "
        "paragone. In locale il test gira normalmente."
    ),
)
def test_bulk_regression(tmp_path):
    """Test 9 spec: run healthy 1gg bit-identico al riferimento M5.

    Bit-identità (vincolo invariante §2.2 della spec M6): unica differenza
    ammessa scenario_id — qui entrambi i run hanno scenario_id 61 (stesso
    scenario), quindi drop('scenario_id') + csv bytes identici.
    """
    scenario = load_scenario(str(ROOT / "scenarios" / "m5_healthy.yaml"))
    cfg = SimConfig.build(seed=42)   # seed 42 (lo scenario ha seed: null)
    out = Path(tmp_path)
    run_days(cfg, 1, out=out, progress=False, scenario=scenario)

    # AC-M5-1 (pattern work/m5_acceptance_check.py check_ac1)
    for name in ("valve_cycles", "events", "ground_truth"):
        new = pl.read_parquet(out / f"{name}.parquet")
        old = pl.read_parquet(REF / f"{name}.parquet")
        assert new.height == old.height, \
            f"{name}: righe {new.height} vs riferimento {old.height}"
        assert new.columns == old.columns, \
            f"{name}: colonne {new.columns} vs riferimento {old.columns}"
        assert _csv_bytes(new.drop("scenario_id")) \
            == _csv_bytes(old.drop("scenario_id")), \
            f"{name}: csv NON bit-identico dopo drop scenario_id"
    vc = pl.read_parquet(out / "valve_cycles.parquet")
    assert vc["scenario_id"].unique().to_list() == [EXPECTED_SCENARIO_ID], \
        vc["scenario_id"].unique().to_list()

    # sidecar run_summary.json: n_cycles == 604398, scenario_id 61
    summary = json.loads((out / "run_summary.json").read_text("utf-8"))
    assert summary["n_cycles"] == EXPECTED_CYCLES, summary["n_cycles"]
    assert summary["scenario_id"] == EXPECTED_SCENARIO_ID, \
        summary.get("scenario_id")
    assert summary["seed"] == 42
