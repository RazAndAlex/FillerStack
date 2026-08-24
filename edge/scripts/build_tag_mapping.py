"""Build and validate the OPC UA tag mapping.

The simulator contract is the source of truth for tag names, data types and
the valve count. The YAML file is the reviewable source artifact and the JS
file is a deterministic JSON array consumed by Node-RED, generated from it.

Examples::

    python edge/scripts/build_tag_mapping.py --generate
    python edge/scripts/build_tag_mapping.py edge/tag-mapping.yaml /tmp/tags.js
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_SOURCE_PATH = REPO_ROOT / "plcsim" / "opcua_server.py"
DEFAULT_YAML_PATH = REPO_ROOT / "edge" / "tag-mapping.yaml"
DEFAULT_JS_PATH = REPO_ROOT / "edge" / "tag-mapping.js"

REQUIRED_FIELDS = ("node_id", "datatype", "unit", "access", "sampling_mode")
ALLOWED_DATATYPES = frozenset({"Boolean", "Int32", "Int64", "String", "Double"})
ALLOWED_ACCESS = frozenset({"read"})
ALLOWED_SAMPLING = frozenset({"event"})
ALLOWED_UNITS = frozenset({"ms", "impulsi", "slot", "-", "count"})
NODE_ID_PREFIX = "ns=2;s=Filler01"


class MappingBuildError(ValueError):
    """A source-contract or mapping validation error."""


@dataclass(frozen=True)
class OpcUaContract:
    """The subset of ``opcua_server.py`` used to build the mapping."""

    machine_tags: tuple[tuple[str, str], ...]
    valve_tags: tuple[tuple[str, str], ...]
    n_valves: int


def _variant_type_name(value: Any) -> str:
    """Return the short asyncua VariantType name from an imported value."""
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    text = str(value)
    return text.rsplit(".", 1)[-1]


def _contract_from_values(machine: Any, valve: Any, n_valves: Any) -> OpcUaContract:
    def normalise(raw: Any, label: str) -> tuple[tuple[str, str], ...]:
        if not isinstance(raw, (tuple, list)) or not raw:
            raise MappingBuildError(f"{label} must be a non-empty tuple/list")
        result: list[tuple[str, str]] = []
        for item in raw:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise MappingBuildError(f"{label} contains a malformed tag entry")
            tag_name, variant_type = item
            if not isinstance(tag_name, str) or not tag_name:
                raise MappingBuildError(f"{label} contains a tag without a name")
            result.append((tag_name, _variant_type_name(variant_type)))
        names = [name for name, _ in result]
        if len(names) != len(set(names)):
            raise MappingBuildError(f"{label} contains duplicate tag names")
        unknown = sorted(set(datatype for _, datatype in result) - ALLOWED_DATATYPES)
        if unknown:
            raise MappingBuildError(
                f"{label} contains unsupported VariantType(s): {', '.join(unknown)}"
            )
        return tuple(result)

    if not isinstance(n_valves, int) or isinstance(n_valves, bool) or n_valves < 1:
        raise MappingBuildError("N_VALVES_CONTRACT must be a positive integer")
    return OpcUaContract(
        machine_tags=normalise(machine, "MACHINE_TAGS"),
        valve_tags=normalise(valve, "VALVE_TAGS"),
        n_valves=n_valves,
    )


def _load_contract_from_import(source_path: Path) -> OpcUaContract:
    """Read the named contract constants from an importable simulator module."""
    spec = importlib.util.spec_from_file_location("_plcsim_opcua_contract", source_path)
    if spec is None or spec.loader is None:
        raise MappingBuildError(f"cannot load simulator module: {source_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return _contract_from_values(
        module.MACHINE_TAGS, module.VALVE_TAGS, module.N_VALVES_CONTRACT
    )


def _ast_assignment(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                return node.value
    raise MappingBuildError(f"{name} not found in simulator source")


def _ast_tag_tuple(value: ast.AST, name: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, (ast.Tuple, ast.List)):
        raise MappingBuildError(f"{name} is not a literal tuple/list")
    entries: list[tuple[str, str]] = []
    for item in value.elts:
        if not isinstance(item, (ast.Tuple, ast.List)) or len(item.elts) != 2:
            raise MappingBuildError(f"{name} contains a malformed literal entry")
        tag_name, variant = item.elts
        if not isinstance(tag_name, ast.Constant) or not isinstance(tag_name.value, str):
            raise MappingBuildError(f"{name} contains a non-string tag name")
        if not isinstance(variant, ast.Attribute):
            raise MappingBuildError(f"{name} contains a non-literal VariantType")
        entries.append((tag_name.value, variant.attr))
    return entries


def _load_contract_from_ast(source_path: Path) -> OpcUaContract:
    """Read literal contract constants without importing optional asyncua deps.

    ``opcua_server.py`` imports asyncua at module load time. The AST fallback
    keeps the mapping builder usable in a static-check environment while still
    reading the actual source constants rather than carrying a second tag list.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    machine = _ast_tag_tuple(_ast_assignment(tree, "MACHINE_TAGS"), "MACHINE_TAGS")
    valve = _ast_tag_tuple(_ast_assignment(tree, "VALVE_TAGS"), "VALVE_TAGS")
    n_valves_node = _ast_assignment(tree, "N_VALVES_CONTRACT")
    try:
        n_valves = ast.literal_eval(n_valves_node)
    except (ValueError, TypeError) as exc:
        raise MappingBuildError("N_VALVES_CONTRACT is not a literal integer") from exc
    return _contract_from_values(machine, valve, n_valves)


def load_contract(source_path: Path = DEFAULT_SOURCE_PATH) -> OpcUaContract:
    """Load simulator constants, falling back to a source-only AST reader."""
    source_path = source_path.resolve()
    import_error: Exception | None = None
    try:
        return _load_contract_from_import(source_path)
    except Exception as exc:  # optional runtime dependencies may be absent
        import_error = exc
    try:
        return _load_contract_from_ast(source_path)
    except Exception as ast_error:
        raise MappingBuildError(
            f"cannot read simulator contract from {source_path}; "
            f"import={import_error}; ast={ast_error}"
        ) from ast_error


def _to_snake_case(tag_name: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", tag_name)
    return re.sub(r"_+", "_", value).lower()


def _unit_for_tag(tag_name: str) -> str:
    """Map stable engineering units not represented by OPC UA type constants."""
    if tag_name.endswith("_ms"):
        return "ms"
    if tag_name in {"TailPulse", "PulseCount", "Target", "DeltaPulse"}:
        return "impulsi"
    if tag_name == "FillingStepOut":
        return "slot"
    if tag_name in {"BottleCounter", "CycleCounter", "LastCycleId"}:
        return "count"
    return "-"


def expected_entries(contract: OpcUaContract) -> dict[str, dict[str, str]]:
    """Build the complete logical mapping from the simulator contract."""
    entries: dict[str, dict[str, str]] = {}

    for tag_name, datatype in contract.machine_tags:
        logical_name = f"machine.{_to_snake_case(tag_name)}"
        entries[logical_name] = {
            "node_id": f"{NODE_ID_PREFIX}.Machine.{tag_name}",
            "datatype": datatype,
            "unit": _unit_for_tag(tag_name),
            "access": "read",
            "sampling_mode": "event",
        }

    for valve_number in range(1, contract.n_valves + 1):
        group = f"Valve{valve_number:02d}"
        logical_group = group.lower()
        for tag_name, datatype in contract.valve_tags:
            logical_name = f"{logical_group}.{_to_snake_case(tag_name)}"
            entries[logical_name] = {
                "node_id": f"{NODE_ID_PREFIX}.{group}.{tag_name}",
                "datatype": datatype,
                "unit": _unit_for_tag(tag_name),
                "access": "read",
                "sampling_mode": "event",
            }
    return entries


def _load_yaml(path: Path) -> dict[str, dict[str, Any]]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise MappingBuildError("PyYAML is required to read tag-mapping.yaml") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MappingBuildError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise MappingBuildError(f"{path}: mapping must be a non-empty object")
    return data


def validate_mapping(
    mapping: dict[str, dict[str, Any]], contract: OpcUaContract
) -> None:
    """Validate YAML against the source-derived catalog with clear errors."""
    expected = expected_entries(contract)
    actual_keys = set(mapping)
    expected_keys = set(expected)
    errors: list[str] = []

    missing = sorted(expected_keys - actual_keys)
    unexpected = sorted(actual_keys - expected_keys)
    if missing:
        errors.append(f"missing logical tags ({len(missing)}): {', '.join(missing[:5])}")
    if unexpected:
        errors.append(
            f"unexpected logical tags ({len(unexpected)}): {', '.join(unexpected[:5])}"
        )

    seen_node_ids: dict[str, str] = {}
    for logical_name, entry in mapping.items():
        if not isinstance(logical_name, str) or not isinstance(entry, dict):
            errors.append(f"{logical_name!r}: entry must be an object")
            continue
        missing_fields = [field for field in REQUIRED_FIELDS if field not in entry]
        if missing_fields:
            errors.append(f"{logical_name}: missing fields {', '.join(missing_fields)}")
            continue
        node_id = entry["node_id"]
        if not isinstance(node_id, str) or not node_id.startswith(f"{NODE_ID_PREFIX}."):
            errors.append(f"{logical_name}: invalid node_id {node_id!r}")
        elif node_id in seen_node_ids:
            errors.append(
                f"duplicate node_id {node_id!r} for {seen_node_ids[node_id]} and {logical_name}"
            )
        else:
            seen_node_ids[node_id] = logical_name
        datatype = entry["datatype"]
        if datatype not in ALLOWED_DATATYPES:
            errors.append(f"{logical_name}: unsupported datatype {datatype!r}")
        if entry["unit"] not in ALLOWED_UNITS:
            errors.append(f"{logical_name}: unsupported unit {entry['unit']!r}")
        if entry["access"] not in ALLOWED_ACCESS:
            errors.append(f"{logical_name}: access must be read")
        if entry["sampling_mode"] not in ALLOWED_SAMPLING:
            errors.append(f"{logical_name}: sampling_mode must be event")

    if not errors:
        for logical_name, source_entry in expected.items():
            actual_entry = mapping[logical_name]
            for field in REQUIRED_FIELDS:
                if actual_entry.get(field) != source_entry[field]:
                    errors.append(
                        f"{logical_name}.{field}: expected {source_entry[field]!r}, "
                        f"got {actual_entry.get(field)!r}"
                    )

    expected_count = len(expected)
    if len(mapping) != expected_count:
        errors.append(
            f"entry count mismatch: expected {expected_count} "
            f"({len(contract.machine_tags)} machine + "
            f"{contract.n_valves}*{len(contract.valve_tags)} valve), got {len(mapping)}"
        )
    if errors:
        raise MappingBuildError("invalid tag mapping:\n- " + "\n- ".join(errors))


def render_js(mapping: dict[str, dict[str, Any]]) -> str:
    """Render the deterministic JSON array parsed by the mapping-loader."""
    rows = [{"logical_name": logical_name, **entry} for logical_name, entry in mapping.items()]
    return json.dumps(rows, ensure_ascii=True, sort_keys=False, indent=2) + "\n"


def render_yaml(mapping: dict[str, dict[str, Any]], contract: OpcUaContract) -> str:
    """Render reviewable YAML while preserving deterministic insertion order."""
    lines = [
        "# edge/tag-mapping.yaml — generated source artifact",
        "# Source: plcsim/opcua_server.py (MACHINE_TAGS + VALVE_TAGS + N_VALVES_CONTRACT)",
        f"# Entries: {len(mapping)} ({len(contract.machine_tags)} machine + "
        f"{contract.n_valves}*{len(contract.valve_tags)} valve)",
        "# Regenerate: python edge/scripts/build_tag_mapping.py --generate",
    ]
    for logical_name, entry in mapping.items():
        lines.append(f"{logical_name}:")
        for field in REQUIRED_FIELDS:
            value = entry[field]
            rendered = '"-"' if value == "-" else str(value)
            lines.append(f"  {field}: {rendered}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "yaml_path", nargs="?", type=Path, default=DEFAULT_YAML_PATH,
        help="mapping YAML to validate (default: edge/tag-mapping.yaml)",
    )
    parser.add_argument(
        "js_path", nargs="?", type=Path, default=DEFAULT_JS_PATH,
        help="generated JSON output (default: edge/tag-mapping.js)",
    )
    parser.add_argument(
        "--source", type=Path, default=DEFAULT_SOURCE_PATH,
        help="simulator contract source (default: plcsim/opcua_server.py)",
    )
    parser.add_argument(
        "--generate", action="store_true",
        help="regenerate YAML from simulator constants before rendering JS",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract = load_contract(args.source)
        if args.generate:
            mapping = expected_entries(contract)
            args.yaml_path.write_text(
                render_yaml(mapping, contract), encoding="utf-8", newline="\n"
            )
        else:
            mapping = _load_yaml(args.yaml_path)
        validate_mapping(mapping, contract)
        args.js_path.write_text(render_js(mapping), encoding="utf-8", newline="\n")
    except (OSError, MappingBuildError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(
        f"OK: {len(mapping)} entries; {len(contract.machine_tags)} machine + "
        f"{contract.n_valves}*{len(contract.valve_tags)} valve; "
        f"wrote {args.js_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
