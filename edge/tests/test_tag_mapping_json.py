"""Regression checks for the Node-RED tag-mapping JSON boundary."""
from __future__ import annotations

import json
import re
from pathlib import Path


EDGE_DIR = Path(__file__).resolve().parents[1]
MAPPING_PATH = EDGE_DIR / "tag-mapping.js"
EXPECTED_ENTRIES = 567
VALVE_TAGS = (
    "filling_time_ms",
    "tail_time_ms",
    "tail_pulse",
    "pulse_count",
    "target",
    "delta_pulse",
    "filling_step_out",
    "filling_ok",
    "fill_quality_ok",
    "sequence_ok",
    "sample_valid",
    "diagnostic_status",
    "close_reason",
    "position_limit",
    "filling_overtime",
    "last_cycle_id",
)


def _normalise_valve_entry(entry: dict[str, str]) -> dict[str, str]:
    normalised = dict(entry)
    normalised["logical_name"] = re.sub(r"valve\d+", "valveNN", entry["logical_name"])
    normalised["node_id"] = re.sub(r"Valve\d+", "ValveNN", entry["node_id"])
    return normalised


def test_mapping_file_is_json_with_expected_count_and_valve_parity() -> None:
    source = MAPPING_PATH.read_text(encoding="utf-8")
    mapping = json.loads(source)

    assert len(mapping) == EXPECTED_ENTRIES
    by_name = {entry["logical_name"]: entry for entry in mapping}
    assert len(by_name) == EXPECTED_ENTRIES

    for suffix in VALVE_TAGS:
        valve01 = by_name[f"valve01.{suffix}"]
        valve35 = by_name[f"valve35.{suffix}"]
        assert _normalise_valve_entry(valve01) == _normalise_valve_entry(valve35)
