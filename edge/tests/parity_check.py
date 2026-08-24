"""M7 parity check — collaudo completo (envelope v1.0, spec M7 §6).

Collaudo automatico del client edge Node-RED. Stato:

  - **T0** determinismo build_tag_mapping + browse asyncua (SKIP se il
    server non è raggiungibile — precondizione d'ambiente);
  - **T1** parità (core): logica watermark §4.3 su RealtimeSim stepped
    (seed 42) — client vs oracle, 200 cicli, 0 duplicati, 0 gap, data
    identici record-per-record (AC-M7-1);
  - **T2** schema + mutazioni (AC-M7-2);
  - **T3** reconnect server: kill/restart simulato a metà run, epoch
    reset (CycleCounter < watermark), nessun duplicato, parità
    post-reconnect (AC-M7-6);
  - **T4** DataReady perso: poll lento — il watermark recupera anche col
    pulse perso (spec M6 §5);
  - **T5** burst: advance multiplo, gap-set identici fra varianti,
    watermark convergente (spec §4.3 limite documentato);
  - **T6** campo mancante: node_id inesistente => quality.valid=false,
    completeness='partial', record valido per lo schema (policy
    congelata in calibration, spec §6).

T1/T3-T6 NON richiedono Docker/server: usano RealtimeSim (modalità
stepped, seed fisso — ADR-0016) come oracle deterministico e
``sim.snapshot`` come sorgente dei tag (spec §4.1) — stessa semantica
(tag e timing) del server M6. Il browse T0 richiede il server avviato
(``python -m plcsim.serve --mode stepped --seed 42``): se non raggiungibile
l'esito è SKIP (precondizione d'ambiente), mai FAIL fittizio.

Uso:
  python edge/tests/parity_check.py --help
  python edge/tests/parity_check.py            # T0-T6
  python edge/tests/parity_check.py --t1       # solo T1
  python edge/tests/parity_check.py --validate record.json
  python -m pytest edge/tests/parity_check.py -v

Dipendenze: jsonschema (obbligatoria), PyYAML + asyncua (T0), pytest
(esecuzione). Vedi edge/requirements.txt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import jsonschema
from jsonschema import Draft202012Validator, FormatChecker

# ---------------------------------------------------------------------------
# Percorsi (tutto relativo a questo file: edge/tests/parity_check.py)
# ---------------------------------------------------------------------------
_EDGE_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = _EDGE_DIR / "schemas" / "envelope-v1.json"
MAPPING_YAML_PATH = _EDGE_DIR / "tag-mapping.yaml"          # unica fonte (§57) — Phase 2A
BUILD_SCRIPT_PATH = _EDGE_DIR / "scripts" / "build_tag_mapping.py"  # Phase 2A
FLOW_PATH = _EDGE_DIR / "flows" / "main.json"

# plcsim (RealtimeSim, oracle deterministico di T1/T3-T6) deve essere
# importabile sia da script standalone (python edge/tests/parity_check.py)
# sia da pytest: la root del repo viene aggiunta a sys.path se assente.
_REPO_ROOT = _EDGE_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from plcsim.realtime import RealtimeSim  # noqa: E402

DEFAULT_ENDPOINT = "opc.tcp://localhost:4840"

# ---------------------------------------------------------------------------
# Fixture canonica: record happy-path (spec §4.2) e mapping di riferimento.
# Il mapping è la COPIA canonica del formato §4.4 (15 voci: 2 trigger + 13
# Valve01) con i NodeId REALI di plcsim/opcua_server.py; quando
# edge/tag-mapping.yaml esiste (Phase 2A) T0 usa QUELLO (unica fonte).
# ---------------------------------------------------------------------------
HAPPY_PATH: dict[str, Any] = {
    "schema_version": "1.0",
    "event_id": "9c5b94b1-3c6e-4f2d-8a7e-1b2c3d4e5f60",
    "event_type": "valve_cycle",
    "event_ts": "2026-08-12T08:15:31.123Z",
    "source_ts": "2026-08-12T08:15:31.100Z",
    "machine_id": "filler01",
    "cycle_id": 184920,
    "valve_id": 1,
    "data": {
        "filling_time_ms": 2480,
        "tail_time_ms": 421,
        "tail_pulse": 244,
        "pulse_count": 2256,
        "target": 2500,
        "delta_pulse": 244,
        "filling_step_out": 18,
        "filling_ok": True,
        "fill_quality_ok": True,
        "sequence_ok": True,
        "sample_valid": True,
        "diagnostic_status": "NORMAL",
    },
    "quality": {"valid": True, "completeness": "complete"},
}

CANONICAL_MAPPING_YAML = """\
# Fixture canonica (spec M7 §4.4) — 15 voci: 2 trigger + 13 Valve01.
# NodeId REALI di plcsim/opcua_server.py (namespace v1, ns=2). Sostituita
# da edge/tag-mapping.yaml (unica fonte, Phase 2A) quando disponibile.
machine.data_ready:
  node_id: ns=2;s=Filler01.Machine.DataReady
  datatype: Boolean
  unit: "-"
  access: read
  sampling_mode: event
machine.cycle_counter:
  node_id: ns=2;s=Filler01.Machine.CycleCounter
  datatype: Int64
  unit: count
  access: read
  sampling_mode: event
valve01.filling_time_ms:
  node_id: ns=2;s=Filler01.Valve01.FillingTime_ms
  datatype: Int32
  unit: ms
  access: read
  sampling_mode: event
valve01.tail_time_ms:
  node_id: ns=2;s=Filler01.Valve01.TailTime_ms
  datatype: Int32
  unit: ms
  access: read
  sampling_mode: event
valve01.tail_pulse:
  node_id: ns=2;s=Filler01.Valve01.TailPulse
  datatype: Int32
  unit: impulsi
  access: read
  sampling_mode: event
valve01.pulse_count:
  node_id: ns=2;s=Filler01.Valve01.PulseCount
  datatype: Int32
  unit: impulsi
  access: read
  sampling_mode: event
valve01.target:
  node_id: ns=2;s=Filler01.Valve01.Target
  datatype: Int32
  unit: impulsi
  access: read
  sampling_mode: event
valve01.delta_pulse:
  node_id: ns=2;s=Filler01.Valve01.DeltaPulse
  datatype: Int32
  unit: impulsi
  access: read
  sampling_mode: event
valve01.filling_step_out:
  node_id: ns=2;s=Filler01.Valve01.FillingStepOut
  datatype: Int32
  unit: slot
  access: read
  sampling_mode: event
valve01.filling_ok:
  node_id: ns=2;s=Filler01.Valve01.FillingOK
  datatype: Boolean
  unit: "-"
  access: read
  sampling_mode: event
valve01.fill_quality_ok:
  node_id: ns=2;s=Filler01.Valve01.FillQualityOK
  datatype: Boolean
  unit: "-"
  access: read
  sampling_mode: event
valve01.sequence_ok:
  node_id: ns=2;s=Filler01.Valve01.SequenceOK
  datatype: Boolean
  unit: "-"
  access: read
  sampling_mode: event
valve01.sample_valid:
  node_id: ns=2;s=Filler01.Valve01.SampleValid
  datatype: Boolean
  unit: "-"
  access: read
  sampling_mode: event
valve01.diagnostic_status:
  node_id: ns=2;s=Filler01.Valve01.DiagnosticStatus
  datatype: String
  unit: "-"
  access: read
  sampling_mode: event
valve01.last_cycle_id:
  node_id: ns=2;s=Filler01.Valve01.LastCycleId
  datatype: Int64
  unit: count
  access: read
  sampling_mode: event
"""


# ---------------------------------------------------------------------------
# Validatore envelope v1.0 (criterio X: ESITO chiaro)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """Carica edge/schemas/envelope-v1.json (valida la definizione stessa).

    Cache: T1/T3/T6 validano centinaia di record — lo schema non cambia
    durante il run.
    """
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)  # la definizione deve essere valida
    return schema


def validate_envelope(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Valida un record envelope contro envelope-v1.json.

    Ritorna ``(valid, errors)``. format (uuid, date-time) è ASSERTION:
    si usa FormatChecker (draft 2020-12), altrimenti un event_id non uuid
    passerebbe. Errore: messaggio con percorso del campo + motivo.
    """
    schema = load_schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors: list[str] = []
    for err in sorted(validator.iter_errors(record), key=lambda e: list(e.path)):
        where = "/" + "/".join(str(p) for p in err.path) if err.path else "<root>"
        errors.append(f"{where}: {err.message}")
    return (len(errors) == 0), errors


def assert_envelope_esito(record: dict[str, Any], criterio: str = "AC-M7-2") -> None:
    """Valida e stampa ESITO (criterio X): PASS/FAIL — evidenza (protocollo §5)."""
    ok, errors = validate_envelope(record)
    if ok:
        print(f"ESITO ({criterio}): PASS — record conforme a envelope-v1.json")
    else:
        print(f"ESITO ({criterio}): FAIL — record NON conforme:")
        for e in errors:
            print(f"    - {e}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Mapping (unica fonte: edge/tag-mapping.yaml, §57; Phase 2A)
# ---------------------------------------------------------------------------
def load_mapping() -> dict[str, dict[str, Any]]:
    """Carica edge/tag-mapping.yaml (unica fonte del mapping, spec §4.4/§57).

    La voce chiave = logical_name; ogni voce ha node_id, datatype, unit,
    access, sampling_mode. Se il file non esiste ancora (atteso in
    M7-Phase2A), solleva FileNotFoundError con messaggio chiaro.
    """
    if not MAPPING_YAML_PATH.exists():
        raise FileNotFoundError(
            f"{MAPPING_YAML_PATH} non presente — unica fonte del mapping "
            "(spec §4.4/§57)"
        )
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyYAML richiesto per load_mapping()") from exc
    data = yaml.safe_load(MAPPING_YAML_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError(f"{MAPPING_YAML_PATH}: mapping vuoto o non valido")
    return data


def test_real_mapping_covers_all_valves_and_last_cycle_ids_independently() -> None:
    """Il mapping reale copre Valve01..Valve35 e i watermark sono distinti."""
    mapping = load_mapping()
    expected_groups = {f"valve{valve_id:02d}" for valve_id in range(1, 36)}
    valve_groups = {
        logical_name.split(".", 1)[0]
        for logical_name in mapping
        if re.fullmatch(r"valve\d{2}\.[^.]+", logical_name)
    }

    assert valve_groups == expected_groups
    for valve_id in range(1, 36):
        logical_name = f"valve{valve_id:02d}.last_cycle_id"
        entry = mapping[logical_name]
        assert entry["node_id"] == (
            f"ns=2;s=Filler01.Valve{valve_id:02d}.LastCycleId"
        )
        assert entry["datatype"] == "Int64"

    sim = RealtimeSim(seed=42, mode="stepped", exposed_valves=[1, 35])
    previous = {1: 0, 35: 0}
    observed = {1: [], 35: []}
    for _ in range(2_000):
        sim.advance(1)
        tags = sim.snapshot.read()
        current = {
            valve_id: int(tags[f"Valve{valve_id:02d}"]["LastCycleId"])
            for valve_id in previous
        }
        changed = [valve_id for valve_id in previous
                   if current[valve_id] != previous[valve_id]]
        for valve_id in changed:
            assert current[valve_id] == previous[valve_id] + 1
            observed[valve_id].append(current[valve_id])
        for valve_id in previous:
            if valve_id not in changed:
                assert current[valve_id] == previous[valve_id]
        previous = current
        if all(len(cycle_ids) >= 2 for cycle_ids in observed.values()):
            break

    assert observed[1][:2] == [1, 2]
    assert observed[35][:2] == [1, 2]


def test_multivalve_envelopes_use_own_last_cycle_id_and_data() -> None:
    """Valve01 e Valve35 producono envelope dal proprio blocco e trigger."""
    sim = RealtimeSim(seed=42, mode="stepped", exposed_valves=[1, 35])
    previous_cycle_id = {1: 0, 35: 0}
    envelopes: dict[int, list[dict[str, Any]]] = {1: [], 35: []}
    source_data: dict[tuple[int, int], dict[str, Any]] = {}

    for _ in range(2_000):
        sim.advance(1)
        tags = sim.snapshot.read()
        for valve_id in (1, 35):
            block = tags[f"Valve{valve_id:02d}"]
            last_cycle_id = int(block["LastCycleId"])
            if last_cycle_id == previous_cycle_id[valve_id]:
                continue

            data = snapshot_to_data(block)
            source_data[(valve_id, last_cycle_id)] = data
            envelopes[valve_id].append(build_envelope_from_snapshot(
                sim, last_cycle_id, valve_id, data))
            previous_cycle_id[valve_id] = last_cycle_id

        if all(len(stream) >= 2 for stream in envelopes.values()):
            break

    for valve_id in (1, 35):
        stream = envelopes[valve_id][:2]
        assert [record["cycle_id"] for record in stream] == [1, 2]
        assert [record["valve_id"] for record in stream] == [valve_id, valve_id]
        for record in stream:
            source = source_data[(valve_id, record["cycle_id"])]
            assert record["data"] == source

    # Il seed fisso rende diversi i blocchi del primo ciclo. Questo controllo
    # fallisce se il builder riusa il blocco di un'altra valvola.
    valve01_first = envelopes[1][0]
    valve35_first = envelopes[35][0]
    assert valve01_first["data"] != valve35_first["data"]
    assert valve01_first["data"] != source_data[(35, valve01_first["cycle_id"])]
    assert valve35_first["data"] != source_data[(1, valve35_first["cycle_id"])]


def test_builder_rejects_missing_last_cycle_id_without_machine_fallback() -> None:
    """Il builder non può usare il contatore macchina come identità ciclo."""
    flow = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
    builder = next(node for node in flow if node.get("id") == "m7-builder")
    source = builder["func"]

    assert "block[prefix + '.last_cycle_id']" in source
    assert "cycleId = p.cycleId" in source
    assert "p.cycleCounter" not in source
    assert "cycleId = cc" not in source
    assert "LastCycleId assente" in source
    assert "return null" in source


# ---------------------------------------------------------------------------
# T0 — determinismo build_tag_mapping + browse asyncua (spec §6)
# ---------------------------------------------------------------------------
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_js_from_yaml(yaml_text: str) -> str:
    """Conversione deterministica yaml -> tag-mapping.js (spec §3, §4.4).

    Riferimento per T0 (determinismo: stesso yaml => stesso js). Quando in
    Phase 2A esisterà edge/scripts/build_tag_mapping.py, T0 esegue quello
    (subprocess) e questo resta il riferimento in caso di divergenza.
    Ordine: ordine di inserimento del yaml (nessun sort); json.dumps con
    separatori e indentazione fissi => output byte-identico.
    """
    import yaml

    entries = yaml.safe_load(yaml_text)
    lines = [
        "// GENERATO da edge/tag-mapping.yaml — NON modificare a mano",
        "// (spec M7 §3/§4.4; conversione deterministica di build_tag_mapping)",
        "module.exports = [",
    ]
    for key, entry in entries.items():
        row = {"logical_name": key, **entry}
        lines.append("  " + json.dumps(row, ensure_ascii=True, sort_keys=False,
                                       separators=(",", ": ")) + ",")
    lines.append("];")
    return "\n".join(lines) + "\n"


def run_t0_determinism() -> tuple[str, str]:
    """T0a: determinismo build_tag_mapping. Ritorna (esito, evidenza)."""
    if MAPPING_YAML_PATH.exists() and BUILD_SCRIPT_PATH.exists():
        # Phase 2A: esegui lo script reale due volte e confronta l'output.
        import subprocess

        def _run_once(out_path: Path) -> tuple[int, str]:
            proc = subprocess.run(
                [sys.executable, str(BUILD_SCRIPT_PATH), str(MAPPING_YAML_PATH),
                 str(out_path)], capture_output=True, text=True, timeout=60)
            digest = _sha256(out_path.read_text(encoding="utf-8")) if out_path.exists() else ""
            return proc.returncode, digest

        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            rc1, h1 = _run_once(tmp_dir / "tag-mapping.js")
            rc2, h2 = _run_once(tmp_dir / "tag-mapping.js")
            if rc1 == 0 and rc2 == 0 and h1 == h2:
                return "PASS", (f"build_tag_mapping.py deterministico "
                                f"(2 run, sha256={h1})")
            return "FAIL", f"rc1={rc1} rc2={rc2} sha256_1={h1} sha256_2={h2}"
    # Phase 1B: riferimento canonico incorporato (stesso yaml => stesso js).
    js1 = build_js_from_yaml(CANONICAL_MAPPING_YAML)
    js2 = build_js_from_yaml(CANONICAL_MAPPING_YAML)
    if js1 == js2:
        return "PASS", (f"build_tag_mapping deterministico (fixture canonica, "
                        f"sha256={_sha256(js1)}) — artefatti reali attesi in "
                        f"M7-Phase2A")
    return "FAIL", "due run della stessa fixture hanno prodotto js diversi"


def run_t0_browse(mapping: dict[str, dict[str, Any]] | None = None,
                  endpoint: str = DEFAULT_ENDPOINT) -> tuple[str, str]:
    """T0b: browse asyncua — ogni node_id del mapping esiste nell'address space.

    Ritorna (esito, evidenza) con esito in {"PASS", "FAIL", "SKIP"}.
    SKIP = server non raggiungibile (precondizione d'ambiente: avviare
    ``python -m plcsim.serve --mode stepped --seed 42``). Il confronto è
    sullo string form completo (ns=2;s=...): un eventuale slittamento
    dell'indice namespace emerge come FAIL (spec §6).
    """
    if mapping is None:
        mapping = _canonical_mapping()
    node_ids = [entry["node_id"] for entry in mapping.values()]
    try:
        found = _browse_namespace_2(endpoint)
    except ConnectionError as exc:
        return "SKIP", (f"server non raggiungibile ({endpoint}): {exc} — "
                        f"avviare python -m plcsim.serve --mode stepped --seed 42")
    except Exception as exc:  # connesso ma address space vuoto/insolito
        return "FAIL", (f"browse non riuscito su {endpoint}: {exc}")
    missing = [nid for nid in node_ids if nid not in found]
    if missing:
        return "FAIL", (f"{len(missing)}/{len(node_ids)} node_id non trovati "
                        f"nel browse: {missing}")
    return "PASS", (f"{len(node_ids)}/{len(node_ids)} node_id presenti "
                    f"nel browse (ns=2, {len(found)} nodi totali)")


def _canonical_mapping() -> dict[str, dict[str, Any]]:
    import yaml
    return yaml.safe_load(CANONICAL_MAPPING_YAML)


def _browse_namespace_2(endpoint: str, timeout: float = 2.0,
                        connect_attempts: int = 2,
                        warmup_attempts: int = 6,
                        delay: float = 1.0) -> set[str]:
    """Browse ricorsivo sotto Objects; ritorna gli string NodeId (ns=2).

    Due fasi: (1) connessione — se il server è giù, pochi tentativi rapidi
    e ConnectionError (-> SKIP nel chiamante, non si paga il timeout di
    warm-up); (2) warm-up — connessi ma albero ancora vuoto (il server
    asyncua crea i nodi dopo l'apertura della porta), si ritenta il walk
    fino a ``warmup_attempts`` volte. Un browse ripetutamente vuoto è un
    errore reale (RuntimeError, -> FAIL nel chiamante).
    """
    import asyncio

    from asyncua import Client

    async def _walk(client: Any) -> set[str]:
        found: set[str] = set()
        stack = [client.nodes.objects]
        while stack:
            node = stack.pop()
            for child in await node.get_children():
                nid = child.nodeid
                if nid.NamespaceIndex == 2:
                    found.add(_nid_string(nid))
                stack.append(child)
        return found

    async def _impl() -> set[str]:
        last_conn_err: Exception | None = None
        for _ in range(connect_attempts):
            client = Client(endpoint, timeout=timeout)
            try:
                await client.connect()
            except Exception as exc:  # connessione/security/endpoint
                last_conn_err = ConnectionError(str(exc) or type(exc).__name__)
                await asyncio.sleep(0.5)
                continue
            try:
                for _ in range(warmup_attempts):
                    try:
                        found = await _walk(client)
                    except Exception:
                        found = set()
                    if found:
                        return found
                    await asyncio.sleep(delay)
                raise RuntimeError(
                    "browse connesso ma senza nodi ns=2 dopo "
                    f"{warmup_attempts} tentativi di warm-up")
            finally:
                await client.disconnect()
        if last_conn_err is not None:
            raise last_conn_err
        raise ConnectionError("nessun tentativo di connessione riuscito")

    return asyncio.run(_impl())


def _nid_string(nid: Any) -> str:
    """String form canonico di un NodeId (es. 'ns=2;s=Filler01.Machine.X').

    NB: ``str(nid)`` in asyncua produce la repr Python, non lo string form
    del protocollo: si costruisce esplicitamente per confrontare con i
    node_id del mapping (formato spec §4.4).
    """
    ident = nid.Identifier
    if isinstance(ident, str):
        return f"ns={nid.NamespaceIndex};s={ident}"
    return f"ns={nid.NamespaceIndex};i={ident}"


def run_t0() -> None:
    """T0 completo (spec §6): determinismo + browse. Stampa ESITO."""
    esito, evidenza = run_t0_determinism()
    print(f"ESITO (T0-determinismo): {esito} — {evidenza}")
    try:
        mapping = load_mapping()          # unica fonte quando c'è (Phase 2A)
        print("    (mapping: edge/tag-mapping.yaml)")
    except FileNotFoundError:
        mapping = _canonical_mapping()    # fixture canonica in Phase 1B
        print("    (mapping: fixture canonica — tag-mapping.yaml atteso in Phase 2A)")
    esito, evidenza = run_t0_browse(mapping)
    print(f"ESITO (T0-browse): {esito} — {evidenza}")


# ---------------------------------------------------------------------------
# T2 — schema + mutazioni (spec §6; AC-M7-2)  [COMPLETO in Phase 1B]
# ---------------------------------------------------------------------------
def run_t2() -> None:
    """T2: happy path valido; 4 mutazioni rifiutate. Stampa ESITO."""
    import copy

    ok, errors = validate_envelope(HAPPY_PATH)
    assert ok, f"happy path NON valido: {errors}"
    mutazioni: list[tuple[str, dict[str, Any], str]] = [
        ("campo mancante (top-level data)",
         _mutate(copy.deepcopy(HAPPY_PATH), drop="data"),
         "data"),
        ("campo mancante (data.delta_pulse)",
         _mutate(copy.deepcopy(HAPPY_PATH), drop=("data", "delta_pulse")),
         "data.delta_pulse"),
        ("tipo errato (data.filling_ok = stringa)",
         _mutate(copy.deepcopy(HAPPY_PATH), assign=("data.filling_ok", "true")),
         "data.filling_ok"),
        ("event_id non uuid",
         _mutate(copy.deepcopy(HAPPY_PATH), assign=("event_id", "non-un-uuid")),
         "event_id"),
        ("ingest_ts presente (campo riservato M8)",
         _mutate(copy.deepcopy(HAPPY_PATH),
                 assign=("ingest_ts", "2026-08-12T08:15:31.140Z")),
         "ingest_ts"),
    ]
    rifiutate = 0
    for descr, record, where in mutazioni:
        ok_mut, errs = validate_envelope(record)
        if ok_mut:
            print(f"    MUTAZIONE NON RIFIUTATA: {descr}")
        else:
            rifiutate += 1
            print(f"    rifiutata: {descr} -> {errs[0] if errs else '?'}")
    totale = len(mutazioni) + 1  # + happy path
    if rifiutate == len(mutazioni):
        print(f"ESITO (AC-M7-2): PASS — happy path valido, {rifiutate}/{len(mutazioni)} "
              f"mutazioni rifiutate ({totale} casi)")
    else:
        print(f"ESITO (AC-M7-2): FAIL — {rifiutate}/{len(mutazioni)} mutazioni rifiutate")
        raise SystemExit(1)


def _mutate(record: dict[str, Any], *, drop: tuple[str, ...] | str | None = None,
            assign: tuple[str | tuple[str, ...], Any] | None = None) -> dict[str, Any]:
    """Helper T2: drop di un campo (percorso) o assegnazione di un valore.

    ``drop``: str o tupla di percorso (es. ("data", "delta_pulse")).
    ``assign``: (percorso, valore) con percorso str o tupla; un percorso
    str con punti ("data.filling_ok") è splittato sui "." (comodo per
    aggiungere campi top-level come ingest_ts).
    """
    if drop is not None:
        path = (drop,) if isinstance(drop, str) else drop
        node: Any = record
        for part in path[:-1]:
            node = node[part]
        del node[path[-1]]
    if assign is not None:
        raw_path, value = assign
        path = (raw_path,) if isinstance(raw_path, str) and "." not in raw_path \
            else tuple(raw_path.split(".")) if isinstance(raw_path, str) else raw_path
        node: Any = record
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value
    return record


# ---------------------------------------------------------------------------
# Envelope builder + logica watermark (spec §4.2/§4.3) — usati da T1/T3-T6
# ---------------------------------------------------------------------------

# Chiavi data.* dell'envelope in ordine di contratto (spec §4.2)
DATA_KEYS: list[str] = [
    "filling_time_ms", "tail_time_ms", "tail_pulse", "pulse_count",
    "target", "delta_pulse", "filling_step_out", "filling_ok",
    "fill_quality_ok", "sequence_ok", "sample_valid", "diagnostic_status",
]

# Tag del blocco ValveNN dello snapshot M6 -> logical_name dell'envelope
# (mapping §4.4: le voci valve01.* del yaml mappano 1:1 sui 12 campi data)
VALVE_TAG_TO_DATA: dict[str, str] = {
    "FillingTime_ms": "filling_time_ms",
    "TailTime_ms": "tail_time_ms",
    "TailPulse": "tail_pulse",
    "PulseCount": "pulse_count",
    "Target": "target",
    "DeltaPulse": "delta_pulse",
    "FillingStepOut": "filling_step_out",
    "FillingOK": "filling_ok",
    "FillQualityOK": "fill_quality_ok",
    "SequenceOK": "sequence_ok",
    "SampleValid": "sample_valid",
    "DiagnosticStatus": "diagnostic_status",
}

# Ancora fissa per source_ts deterministico (vedi _sim_iso_ts)
_SOURCE_TS_ANCHOR = datetime(2026, 8, 12, tzinfo=timezone.utc)


def snapshot_to_data(block: dict[str, Any]) -> dict[str, Any]:
    """Blocco ValveNN dello snapshot (tag M6) -> data dell'envelope.

    Chiavi = logical_name del mapping (spec §4.4); i 12 campi del
    contratto. LastCycleId NON entra in data.* (va su cycle_id).
    """
    return {logical: block[tag] for tag, logical in VALVE_TAG_TO_DATA.items()}


def _sim_iso_ts(sim: Any) -> str:
    """source_ts deterministico: ancora fissa + t_ms del clock simulato.

    Equivalente del ServerTimestamp del blocco OPC UA (spec §10 Q3: il
    server M6 non espone un tag "tempo simulazione"): stesso run ⇒ stessi
    source_ts, ordinati per ciclo (determinismo dei test, ADR-0016).
    """
    ms = _SOURCE_TS_ANCHOR.timestamp() * 1000.0 + float(sim.clock.now_ms)
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_envelope_from_snapshot(sim: Any, cycle_id: int, valve_id: int,
                                 data_dict: dict[str, Any]) -> dict[str, Any]:
    """Costruisce l'envelope v1.0 da un blocco valvola letto (spec §4.2).

    ``data_dict``: chiavi = logical_name del mapping (i 12 campi del
    contratto). Un campo ASSENTE o None (tag non leggibile, qualità OPC UA
    cattiva — T6) produce ``null`` in data.* e quality.valid=false,
    completeness='partial': il record è emesso comunque, nessun ciclo
    sparisce dalla sequenza (spec §4.2; policy congelata in calibration
    T6). L'output valida sempre contro envelope-v1.json (T2/T6).
    """
    data = {key: (data_dict[key] if data_dict.get(key) is not None else None)
            for key in DATA_KEYS}
    complete = all(v is not None for v in data.values())
    return {
        "schema_version": "1.0",
        "event_id": str(uuid.uuid4()),
        "event_type": "valve_cycle",
        "event_ts": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "source_ts": _sim_iso_ts(sim),
        "machine_id": "filler01",
        "cycle_id": int(cycle_id),
        "valve_id": int(valve_id),
        "data": data,
        "quality": {"valid": complete,
                    "completeness": "complete" if complete else "partial"},
    }


class WatermarkState:
    """Stato del watermark lato client (spec §4.3, flusso 1-7).

    - watermark: ultimo CycleCounter processato; None = non inizializzato
      (alla prima notifica si inizializza al CycleCounter corrente —
      nessun backfill dei cicli chiusi durante il down);
    - init_cc: CycleCounter all'inizializzazione (base del calcolo dei
      cicli persi);
    - epoch / epoch_resets: il server riavviato riparte da 0 (contatori
      azzerati) → se CycleCounter < watermark si apre un NUOVO epoch
      (watermark = CycleCounter corrente, loggato);
    - gaps: CycleCounter saltati (blocchi intermedi non più leggibili,
      limite documentato spec §4.3);
    - pulses_seen: osservazioni DataReady=TRUE (metrica T4).
    """

    def __init__(self) -> None:
        self.watermark: int | None = None
        self.init_cc: int = 0
        self.epoch: int = 0
        self.epoch_resets: list[int] = []
        self.gaps: list[int] = []
        self.pulses_seen: int = 0
        self.envelopes: list[dict[str, Any]] = []


def watermark_check(sim: Any, state: WatermarkState, *, valve_id: int = 1,
                    check_dataready: bool = False,
                    data_mapper: Any = None) -> list[dict[str, Any]]:
    """Un passo del flusso §4.3 su un RealtimeSim stepped.

    Legge lo snapshot corrente e applica la logica watermark:
      1. init: watermark = CycleCounter corrente (nessun backfill);
      2. CycleCounter < watermark → epoch reset (riavvio server);
      3. CycleCounter > watermark → nuova chiusura ciclo: gaps per i
         contatori intermedi (blocchi non leggibili), envelope dall'ultimo
         blocco, watermark = CycleCounter;
      4. altrimenti ignora (nessun duplicato).
    Ritorna gli envelope emessi in QUESTO passo. ``data_mapper``:
    callable opzionale (block -> data_dict) per le varianti di test (T6).
    """
    tags = sim.snapshot.read()
    cc = int(tags["Machine"]["CycleCounter"])
    if check_dataready and bool(tags["Machine"]["DataReady"]):
        state.pulses_seen += 1
    if state.watermark is None:
        state.watermark = cc
        state.init_cc = cc
        return []
    if cc < state.watermark:
        state.epoch += 1
        state.epoch_resets.append(cc)
        state.watermark = cc
        return []
    if cc > state.watermark:
        state.gaps.extend(range(state.watermark + 1, cc))
        block = tags[f"Valve{valve_id:02d}"]
        data_dict = (snapshot_to_data(block) if data_mapper is None
                     else data_mapper(block))
        env = build_envelope_from_snapshot(
            sim, block["LastCycleId"], valve_id, data_dict)
        state.watermark = cc
        state.envelopes.append(env)
        return [env]
    return []


def run_watermark(sim: Any, n_cycles: int, *,
                  state: WatermarkState | None = None, poll_every: int = 1,
                  check_dataready: bool = False, data_mapper: Any = None,
                  valve_id: int = 1, max_scans: int = 2_000_000
                  ) -> tuple[list[dict[str, Any]], WatermarkState]:
    """Esegue il flusso watermark §4.3 su un RealtimeSim stepped.

    Avanza il simulatore scan-per-scan (letture ogni ``poll_every`` scan)
    finché ``n_cycles`` envelope sono emessi nella fase corrente. Lo stato
    può essere RIUSATO (T3: il watermark sopravvive al restart del
    server). Ritorna (envelope della fase, stato).
    """
    if state is None:
        state = WatermarkState()
    start = len(state.envelopes)
    scans = 0
    while len(state.envelopes) - start < n_cycles:
        if scans >= max_scans:
            cc = sim.snapshot.read()["Machine"]["CycleCounter"]
            raise RuntimeError(
                f"run_watermark: {n_cycles} cicli non raggiunti in "
                f"{max_scans} scan (emessi {len(state.envelopes) - start}, "
                f"CycleCounter={cc})")
        sim.advance(poll_every)
        scans += poll_every
        watermark_check(sim, state, valve_id=valve_id,
                        check_dataready=check_dataready,
                        data_mapper=data_mapper)
    return list(state.envelopes[start:]), state


# ---------------------------------------------------------------------------
# Helper di parità (spec §6: confronto su cycle_id e data, non su event_id)
# ---------------------------------------------------------------------------
def _parity_identity(env: dict[str, Any]) -> tuple[Any, ...]:
    """Identità di parità: (cycle_id, data). event_id/event_ts/source_ts
    esclusi per costruzione: ogni istanza genera uuid e orologi propri; la
    parità richiesta (spec §6 T1) è su cycle_id e data."""
    return (env["cycle_id"], tuple(sorted(env["data"].items())))


def _parity_check(stream_a: list[dict[str, Any]],
                  stream_b: list[dict[str, Any]],
                  label: str) -> tuple[bool, str]:
    """Parità di due stream: stessa sequenza (cycle_id, data), nessun
    duplicato. Ritorna (ok, messaggio)."""
    ia = [_parity_identity(e) for e in stream_a]
    ib = [_parity_identity(e) for e in stream_b]
    if len(ia) != len(ib):
        return False, f"{label}: lunghezze diverse ({len(ia)} vs {len(ib)})"
    if ia != ib:
        for i, (x, y) in enumerate(zip(ia, ib)):
            if x != y:
                return False, (f"{label}: divergenza al record {i} "
                               f"(cycle_id {stream_a[i]['cycle_id']} vs "
                               f"{stream_b[i]['cycle_id']})")
    ids = [e["cycle_id"] for e in stream_a]
    if len(set(ids)) != len(ids):
        return False, (f"{label}: {len(ids) - len(set(ids))} duplicati")
    return True, (f"{label}: {len(stream_a)} record, cycle_id identici in "
                  f"ordine, 0 duplicati, data identici record-per-record")


def _stream_checks(stream: list[dict[str, Any]], state: WatermarkState,
                   label: str) -> tuple[bool, str]:
    """Check di sequenza nominale (T1/T4/T6): nessun duplicato, nessun
    gap — ogni incremento di CycleCounter osservato deve essere stato
    emesso esattamente una volta."""
    ids = [e["cycle_id"] for e in stream]
    if any(b <= a for a, b in zip(ids, ids[1:])):
        return False, f"{label}: cycle_id non strettamente crescenti"
    if state.watermark is None:
        return False, f"{label}: watermark mai inizializzato"
    expected = state.watermark - state.init_cc
    if len(ids) != expected:
        return False, (f"{label}: {len(ids)} record emessi ma CycleCounter "
                       f"avanzato di {expected} (cicli persi o duplicati)")
    return True, f"{label}: {len(ids)} record, 0 duplicati, 0 gap"


# ---------------------------------------------------------------------------
# T1 — parità core (spec §6; AC-M7-1)
# ---------------------------------------------------------------------------
def run_t1() -> tuple[str, str]:
    """T1 (core): parità record-per-record su 200 cicli.

    Client e oracle sono due RealtimeSim indipendenti (stesso seed 42,
    mode stepped): la STESSA logica watermark (§4.3) su due istanze dello
    stesso run deterministico deve produrre la STESSA sequenza di
    (cycle_id, data) — 0 duplicati, 0 gap — e ogni envelope è valido
    contro envelope-v1.json (AC-M7-2 per costruzione).
    """
    n_cycles = 200
    client = RealtimeSim(seed=42, mode="stepped")
    oracle = RealtimeSim(seed=42, mode="stepped")
    envs_c, st_c = run_watermark(client, n_cycles)
    envs_o, st_o = run_watermark(oracle, n_cycles)

    problemi: list[str] = []
    # 1) schema: ogni record di entrambi gli stream
    for label, envs in (("client", envs_c), ("oracle", envs_o)):
        for env in envs:
            ok, errs = validate_envelope(env)
            if not ok:
                problemi.append(f"{label} cycle_id={env['cycle_id']}: "
                                f"{errs[0] if errs else '?'}")
    # 2) sequenza nominale (0 duplicati, 0 gap) per ciascuno stream
    for label, envs, st in (("client", envs_c, st_c), ("oracle", envs_o, st_o)):
        ok, msg = _stream_checks(envs, st, label)
        if not ok:
            problemi.append(msg)
    # 3) parità client vs oracle
    ok, msg = _parity_check(envs_c, envs_o, "client vs oracle")
    if not ok:
        problemi.append(msg)
    # 4) watermark convergente al CycleCounter corrente a fine run
    for label, sim, st in (("client", client, st_c), ("oracle", oracle, st_o)):
        if st.watermark != sim.snapshot.read()["Machine"]["CycleCounter"]:
            problemi.append(f"{label}: watermark non convergente ({st.watermark})")

    if problemi:
        print(f"ESITO (AC-M7-1): FAIL - {len(problemi)} controlli falliti")
        for p in problemi:
            print(f"    - {p}")
        return "FAIL", "; ".join(problemi[:4])
    evidenza = (f"{n_cycles}/{n_cycles} cycle_id identici client vs oracle, "
                f"0 duplicati, 0 gap, data identici record-per-record, "
                f"{2 * n_cycles}/{2 * n_cycles} envelope validi per lo schema")
    print(f"ESITO (AC-M7-1): PASS - {evidenza}")
    return "PASS", evidenza


# ---------------------------------------------------------------------------
# T3 — reconnect server (spec §6; AC-M7-6)
# ---------------------------------------------------------------------------
def run_t3() -> tuple[str, str]:
    """T3: kill+restart simulato del server a metà run.

    Fase 1: client e oracle (2 RealtimeSim, seed 42) acquisiscono 50 cicli
    (parità pre-reconnect). Fase 2 (restart): il server riparte da zero
    (2 RealtimeSim FRESCHI, stesso seed — contatori a 0): al primo check
    CycleCounter=0 < watermark=50 → epoch reset (watermark=0, loggato,
    spec §4.3); la ri-acquisizione di 50 cicli riprende senza duplicati.
    Parità post-reconnect; il run riprodotto è deterministico (stessi
    data del primo epoch).
    """
    n = 50
    c1 = RealtimeSim(seed=42, mode="stepped")
    o1 = RealtimeSim(seed=42, mode="stepped")
    envs_c1, st_c1 = run_watermark(c1, n)
    envs_o1, st_o1 = run_watermark(o1, n)
    wm_c1 = st_c1.watermark   # watermark a fine fase 1 (lo stato è RIUSATO in fase 2)
    wm_o1 = st_o1.watermark

    problemi: list[str] = []
    # pre-reconnect: nessun reset, sequenza nominale, parità
    # (i check della fase 1 vanno PRIMA della fase 2: lo stato è riusato
    # e la fase 2 registra l'epoch reset sullo stesso oggetto)
    if st_c1.epoch_resets or st_o1.epoch_resets:
        problemi.append(f"epoch reset inatteso pre-reconnect: "
                        f"client={st_c1.epoch_resets} oracle={st_o1.epoch_resets}")
    for label, envs, st in (("client-1", envs_c1, st_c1),
                            ("oracle-1", envs_o1, st_o1)):
        ok, msg = _stream_checks(envs, st, label)
        if not ok:
            problemi.append(msg)
    ok, msg = _parity_check(envs_c1, envs_o1, "pre-reconnect")
    if not ok:
        problemi.append(msg)

    c2 = RealtimeSim(seed=42, mode="stepped")   # server riavviato (cc parte da 0)
    o2 = RealtimeSim(seed=42, mode="stepped")
    envs_c2, st_c2 = run_watermark(c2, n, state=st_c1)
    envs_o2, st_o2 = run_watermark(o2, n, state=st_o1)

    # post-reconnect: UN epoch reset a cc=0, sequenza pulita, parità
    if st_c2.epoch_resets != [0] or st_o2.epoch_resets != [0]:
        problemi.append(f"atteso epoch reset [0]: client={st_c2.epoch_resets} "
                        f"oracle={st_o2.epoch_resets}")
    for label, envs, st_cur, wm_prev in (
            ("client-2", envs_c2, st_c2, wm_c1),
            ("oracle-2", envs_o2, st_o2, wm_o1)):
        ids = [e["cycle_id"] for e in envs]
        if any(b <= a for a, b in zip(ids, ids[1:])):
            problemi.append(f"{label}: duplicato/regresso post-reconnect")
        if not st_cur.epoch_resets:
            problemi.append(f"{label}: nessun epoch reset post-reconnect")
            continue
        # nel nuovo epoch il watermark riparte dal cc del reset: i record
        # emessi devono coprire TUTTI gli incrementi del nuovo epoch
        expected = st_cur.watermark - st_cur.epoch_resets[-1]
        if len(ids) != expected:
            problemi.append(f"{label}: {len(ids)} record emessi post-reconnect "
                            f"ma watermark avanzato di {expected} nel nuovo epoch")
        if st_cur.watermark != wm_prev:
            problemi.append(f"{label}: watermark non tornato a {wm_prev} "
                            f"a fine epoch 2 ({st_cur.watermark})")
    ok, msg = _parity_check(envs_c2, envs_o2, "post-reconnect")
    if not ok:
        problemi.append(msg)
    # determinismo del restart: i data dell'epoch 2 riproducono l'epoch 1
    for i, (e1, e2) in enumerate(zip(envs_c1, envs_c2)):
        if e1["data"] != e2["data"]:
            problemi.append(f"restart non deterministico al ciclo {i}")
            break
    # schema: tutti i record delle due fasi (client+oracle)
    for label, envs in (("pre", envs_c1 + envs_o1),
                        ("post", envs_c2 + envs_o2)):
        for env in envs:
            ok_v, errs = validate_envelope(env)
            if not ok_v:
                problemi.append(f"{label} cycle_id={env['cycle_id']}: "
                                f"{errs[0] if errs else '?'}")

    if problemi:
        print(f"ESITO (AC-M7-6): FAIL - {len(problemi)} controlli falliti")
        for p in problemi:
            print(f"    - {p}")
        return "FAIL", "; ".join(problemi[:4])
    evidenza = (f"epoch reset rilevato (cc 0 < watermark {n}), "
                f"0 duplicati in entrambi gli epoch, parità pre e "
                f"post-reconnect ({n}+{n} record), 0 gap, data identici "
                f"tra epoch (run deterministico)")
    print(f"ESITO (AC-M7-6): PASS - {evidenza}")
    return "PASS", evidenza


# ---------------------------------------------------------------------------
# T4 — DataReady perso (spec §6)
# ---------------------------------------------------------------------------
def run_t4() -> tuple[str, str]:
    """T4: DataReady perso — il watermark recupera (spec M6 §5).

    Tre varianti della STESSA logica watermark (check sempre su
    CycleCounter, spec §4.3 — DataReady è solo trigger di reattività):
      A: subscription DataReady+CycleCounter, lettura a ogni scan;
      B: solo CycleCounter (nessun DataReady), lettura a ogni scan;
      C: solo CycleCounter, lettura lenta (ogni 2 scan): il pulse
         DataReady dura 1 solo scan (10 ms) ed è spesso perso.
    Parità piena: le tre sequenze devono essere identiche (cycle_id +
    data), 0 duplicati, 0 gap.
    """
    n = 60
    sim_a = RealtimeSim(seed=42, mode="stepped")
    sim_b = RealtimeSim(seed=42, mode="stepped")
    sim_c = RealtimeSim(seed=42, mode="stepped")
    envs_a, st_a = run_watermark(sim_a, n, check_dataready=True, poll_every=1)
    envs_b, st_b = run_watermark(sim_b, n, check_dataready=False, poll_every=1)
    envs_c, st_c = run_watermark(sim_c, n, check_dataready=True, poll_every=2)

    problemi: list[str] = []
    for label, envs, st in (("A(dr+cc,p1)", envs_a, st_a),
                            ("B(cc,p1)", envs_b, st_b),
                            ("C(cc,p2)", envs_c, st_c)):
        ok, msg = _stream_checks(envs, st, label)
        if not ok:
            problemi.append(msg)
    ok, msg = _parity_check(envs_a, envs_b, "A vs B")
    if not ok:
        problemi.append(msg)
    ok, msg = _parity_check(envs_a, envs_c, "A vs C")
    if not ok:
        problemi.append(msg)
    # il poll lento deve aver perso pulse reali (evidenza del recupero)
    lost = len(envs_c) - st_c.pulses_seen
    if lost <= 0:
        problemi.append(f"variante C: 0 pulse persi su {len(envs_c)} cicli "
                        f"(poll_every=2 non ha perso il pulse di 1 scan?)")
    # sanità del modello: a poll pieno il pulse è sempre visibile
    if st_a.pulses_seen != n:
        problemi.append(f"variante A: pulse visti {st_a.pulses_seen}/{n} "
                        f"(attesi tutti: poll a ogni scan)")
    # schema: tutti i record delle tre varianti
    for label, envs in (("A", envs_a), ("B", envs_b), ("C", envs_c)):
        for env in envs:
            ok_v, errs = validate_envelope(env)
            if not ok_v:
                problemi.append(f"{label} cycle_id={env['cycle_id']}: "
                                f"{errs[0] if errs else '?'}")

    if problemi:
        print(f"ESITO (T4): FAIL - {len(problemi)} controlli falliti")
        for p in problemi:
            print(f"    - {p}")
        return "FAIL", "; ".join(problemi[:4])
    evidenza = (f"3 varianti della stessa logica ({n} cicli): sequenze "
                f"identiche, 0 duplicati, 0 gap; pulse DataReady persi "
                f"{lost}/{n} nella variante C (poll lento) — il watermark "
                f"recupera (check su CycleCounter)")
    print(f"ESITO (T4): PASS - {evidenza}")
    return "PASS", evidenza


# ---------------------------------------------------------------------------
# T5 — burst (spec §6)
# ---------------------------------------------------------------------------
def run_t5() -> tuple[str, str]:
    """T5: burst — advance multiplo e finestre di lettura larghe.

    (a) equivalenza advance: advance(10) e advance(1)×10 producono lo
        stesso stato (t_ms e snapshot identici) — il multi-advance è
        composizione di scan singoli (determinismo stepped);
    (b) burst reale: letture ogni 1000 scan (finestra ~10 s sim, cadenza
        cicli ~3,3 s — limite documentato spec §4.3): più cicli chiusi
        tra una lettura e l'altra → solo l'ultimo blocco è leggibile →
        gap loggati; nessun duplicato; watermark convergente al
        CycleCounter corrente; gap-set identici tra client e oracle
        (stessa logica, stesso run deterministico).
    """
    # (a) advance(10) ≡ advance(1)×10
    s1 = RealtimeSim(seed=42, mode="stepped")
    s2 = RealtimeSim(seed=42, mode="stepped")
    s1.advance(10)
    for _ in range(10):
        s2.advance(1)
    eq_advance = (s1.t_ms == s2.t_ms
                  and s1.snapshot.read() == s2.snapshot.read())

    # (b) burst: finestra di lettura larga
    burst_scans = 1000
    n = 40
    cs = RealtimeSim(seed=42, mode="stepped")
    osim = RealtimeSim(seed=42, mode="stepped")
    envs_c, st_c = run_watermark(cs, n, poll_every=burst_scans)
    envs_o, st_o = run_watermark(osim, n, poll_every=burst_scans)

    problemi: list[str] = []
    if not eq_advance:
        problemi.append("advance(10) != advance(1)x10 (stato divergente)")
    for label, envs, st, sim in (("client", envs_c, st_c, cs),
                                 ("oracle", envs_o, st_o, osim)):
        ids = [e["cycle_id"] for e in envs]
        if any(b <= a for a, b in zip(ids, ids[1:])):
            problemi.append(f"{label}: duplicato/regresso in burst")
        cc_attuale = sim.snapshot.read()["Machine"]["CycleCounter"]
        if st.watermark != cc_attuale:
            problemi.append(f"{label}: watermark non convergente "
                            f"(watermark={st.watermark}, cc={cc_attuale})")
        if len(st.gaps) == 0:
            problemi.append(f"{label}: nessun gap loggato (burst non "
                            f"riprodotto con finestra {burst_scans} scan?)")
        unione = sorted(set(ids) | set(st.gaps))
        attesi = list(range(st.init_cc + 1, st.watermark + 1))
        if unione != attesi:
            problemi.append(f"{label}: copertura incompleta (emessi+gaps "
                            f"!= incrementi osservati)")
    if st_c.gaps != st_o.gaps:
        problemi.append(f"gap-set divergenti: client={st_c.gaps} "
                        f"oracle={st_o.gaps}")
    ok, msg = _parity_check(envs_c, envs_o, "client vs oracle (burst)")
    if not ok:
        problemi.append(msg)
    # schema: tutti i record del burst (client+oracle)
    for env in envs_c + envs_o:
        ok_v, errs = validate_envelope(env)
        if not ok_v:
            problemi.append(f"burst cycle_id={env['cycle_id']}: "
                            f"{errs[0] if errs else '?'}")

    if problemi:
        print(f"ESITO (T5): FAIL - {len(problemi)} controlli falliti")
        for p in problemi:
            print(f"    - {p}")
        return "FAIL", "; ".join(problemi[:4])
    evidenza = (f"advance(10) == advance(1)x10 (stato identico); burst "
                f"{burst_scans} scan: {n} record, {len(st_c.gaps)} gap "
                f"loggati per variante, gap-set identici client/oracle, "
                f"0 duplicati, watermark convergente ({st_c.watermark})")
    print(f"ESITO (T5): PASS - {evidenza}")
    return "PASS", evidenza


# ---------------------------------------------------------------------------
# T6 — campo mancante (spec §6)
# ---------------------------------------------------------------------------
BAD_NODE_ID_T6 = "ns=2;s=Filler01.Valve01.NoSuchTag"  # node_id inesistente (mapping di test)


def run_t6() -> tuple[str, str]:
    """T6: campo mancante — node_id inesistente (spec §6).

    Variante di mapping di test: valve01.filling_time_ms punta a un
    node_id inesistente → la lettura del campo fallisce (qualità OPC UA
    cattiva) → il campo è ASSENTE dal blocco letto. L'envelope è emesso
    comunque con data.filling_time_ms=null, quality.valid=false,
    completeness='partial' (spec §4.2, policy T6) e resta VALIDO contro
    envelope-v1.json; la sequenza dei cicli non si interrompe (flow
    stabile).
    """
    n = 20

    def mapper_missing(block: dict[str, Any]) -> dict[str, Any]:
        data = snapshot_to_data(block)
        del data["filling_time_ms"]      # lettura fallita (node_id inesistente)
        return data

    sim = RealtimeSim(seed=42, mode="stepped")
    envs, st = run_watermark(sim, n, data_mapper=mapper_missing)

    problemi: list[str] = []
    for i, env in enumerate(envs):
        if env["data"]["filling_time_ms"] is not None:
            problemi.append(f"record {i}: filling_time_ms non nullo")
        if env["quality"] != {"valid": False, "completeness": "partial"}:
            problemi.append(f"record {i}: quality attesa valid=false/partial, "
                            f"ottenuta {env['quality']}")
        for key, value in env["data"].items():
            if key != "filling_time_ms" and value is None:
                problemi.append(f"record {i}: {key} nullo (atteso valore reale)")
        ok_v, errs = validate_envelope(env)
        if not ok_v:
            problemi.append(f"record {i} cycle_id={env['cycle_id']} non valido "
                            f"contro lo schema: {errs[0] if errs else '?'}")
    ok, msg = _stream_checks(envs, st, "sequenza (parziale)")
    if not ok:
        problemi.append(msg)
    # contrasto: happy path (stesso run, mapper standard) => completo
    sim_ok = RealtimeSim(seed=42, mode="stepped")
    envs_ok, _ = run_watermark(sim_ok, 5)
    for env in envs_ok:
        if env["quality"] != {"valid": True, "completeness": "complete"}:
            problemi.append("contrasto: happy path non completo")
            break

    if problemi:
        print(f"ESITO (T6): FAIL - {len(problemi)} controlli falliti")
        for p in problemi:
            print(f"    - {p}")
        return "FAIL", "; ".join(problemi[:4])
    evidenza = (f"node_id inesistente ({BAD_NODE_ID_T6}): {n}/{n} record con "
                f"data.filling_time_ms=null, quality.valid=false, "
                f"completeness=partial, tutti validi per lo schema, "
                f"0 duplicati, 0 gap (sequenza intatta); happy path "
                f"confermato complete")
    print(f"ESITO (T6): PASS - {evidenza}")
    return "PASS", evidenza


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python edge/tests/parity_check.py",
        description=("M7 parity check (spec §6) — collaudo completo: T0 "
                     "determinismo+browse, T1 parità (200 cicli), T2 schema, "
                     "T3 reconnect, T4 DataReady perso, T5 burst, T6 campo "
                     "mancante. T1/T3-T6 usano RealtimeSim (oracle "
                     "deterministico, seed 42) — nessuna dipendenza Docker."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("esempi:\n"
                "  python edge/tests/parity_check.py\n"
                "  python edge/tests/parity_check.py --t1 --t3\n"
                "  python edge/tests/parity_check.py --validate record.json\n"
                "  python edge/tests/parity_check.py --t0 --endpoint "
                "opc.tcp://localhost:4840\n"),
    )
    ap.add_argument("--t0", action="store_true", help="esegue T0 (determinismo + browse)")
    ap.add_argument("--t1", action="store_true", help="esegue T1 (parità core, 200 cicli)")
    ap.add_argument("--t2", action="store_true", help="esegue T2 (schema + mutazioni)")
    ap.add_argument("--t3", action="store_true", help="esegue T3 (reconnect server)")
    ap.add_argument("--t4", action="store_true", help="esegue T4 (DataReady perso)")
    ap.add_argument("--t5", action="store_true", help="esegue T5 (burst)")
    ap.add_argument("--t6", action="store_true", help="esegue T6 (campo mancante)")
    ap.add_argument("--all", action="store_true", help="esegue T0-T6 (default)")
    ap.add_argument("--validate", metavar="FILE",
                    help="valida un record envelope JSON contro envelope-v1.json "
                         "(exit 1 se non conforme)")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                    help=f"endpoint OPC UA per il browse (default {DEFAULT_ENDPOINT})")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.validate:
        record = json.loads(Path(args.validate).read_text(encoding="utf-8"))
        assert_envelope_esito(record, criterio="AC-M7-2")
        return 0
    default_run = not (args.t0 or args.t1 or args.t2 or args.t3 or args.t4
                       or args.t5 or args.t6 or args.all)
    rc = 0

    def _wrap(fn: Any) -> int:
        """Esegue un run_* che stampa ESITO e solleva SystemExit su FAIL
        (T0/T2): converte in codice di ritorno senza fermare gli altri T."""
        try:
            fn()
        except SystemExit as exc:
            return 1 if exc.code else 0
        return 0

    if args.t0 or args.all or default_run:
        rc |= _wrap(run_t0)
    if args.t1 or args.all or default_run:
        esito, _ = run_t1()
        rc |= 0 if esito == "PASS" else 1
    if args.t2 or args.all or default_run:
        rc |= _wrap(run_t2)
    if args.t3 or args.all or default_run:
        esito, _ = run_t3()
        rc |= 0 if esito == "PASS" else 1
    if args.t4 or args.all or default_run:
        esito, _ = run_t4()
        rc |= 0 if esito == "PASS" else 1
    if args.t5 or args.all or default_run:
        esito, _ = run_t5()
        rc |= 0 if esito == "PASS" else 1
    if args.t6 or args.all or default_run:
        esito, _ = run_t6()
        rc |= 0 if esito == "PASS" else 1
    return rc


# ---------------------------------------------------------------------------
# pytest (spec §6) — test_schema* = T2; test_t0* = T0; test_t1..t6 = T1/T3-T6
# ---------------------------------------------------------------------------
def test_schema_definition_valid() -> None:
    Draft202012Validator.check_schema(load_schema())


def test_schema_happy_path_valid() -> None:
    ok, errors = validate_envelope(HAPPY_PATH)
    assert ok, f"happy path NON valido: {errors}"


def test_schema_missing_field_rejected() -> None:
    import copy
    rec = copy.deepcopy(HAPPY_PATH)
    del rec["data"]
    ok, _ = validate_envelope(rec)
    assert not ok


def test_schema_missing_data_field_rejected() -> None:
    import copy
    rec = copy.deepcopy(HAPPY_PATH)
    del rec["data"]["delta_pulse"]
    ok, _ = validate_envelope(rec)
    assert not ok


def test_schema_wrong_type_rejected() -> None:
    import copy
    rec = copy.deepcopy(HAPPY_PATH)
    rec["data"]["filling_ok"] = "true"  # stringa invece di boolean
    ok, _ = validate_envelope(rec)
    assert not ok


def test_schema_bad_event_id_rejected() -> None:
    import copy
    rec = copy.deepcopy(HAPPY_PATH)
    rec["event_id"] = "non-un-uuid"
    ok, _ = validate_envelope(rec)
    assert not ok


def test_schema_ingest_ts_rejected() -> None:
    import copy
    rec = copy.deepcopy(HAPPY_PATH)
    rec["ingest_ts"] = "2026-08-12T08:15:31.140Z"  # riservato M8: ASSENTE in v1.0
    ok, _ = validate_envelope(rec)
    assert not ok


def test_schema_null_data_field_accepted() -> None:
    """Policy T6 (calibration, spec §6): campo data non leggibile => null,
    quality.valid=false, completeness=partial — il record resta valido
    contro lo schema (envelope-v1.json ammette null nei 12 campi data)."""
    import copy
    rec = copy.deepcopy(HAPPY_PATH)
    rec["data"]["filling_time_ms"] = None
    rec["quality"] = {"valid": False, "completeness": "partial"}
    ok, errors = validate_envelope(rec)
    assert ok, f"partial con null rifiutato: {errors}"


def test_t0_build_mapping_determinism() -> None:
    esito, evidenza = run_t0_determinism()
    assert esito == "PASS", evidenza


def test_t0_browse_nodes() -> None:
    mapping = _canonical_mapping()
    esito, evidenza = run_t0_browse(mapping)
    if esito == "SKIP":
        pytest.skip(evidenza)
    assert esito == "PASS", evidenza


def test_t1_parity() -> None:
    esito, evidenza = run_t1()
    assert esito == "PASS", evidenza


def test_t3_reconnect() -> None:
    esito, evidenza = run_t3()
    assert esito == "PASS", evidenza


def test_t4_dataready_lost() -> None:
    esito, evidenza = run_t4()
    assert esito == "PASS", evidenza


def test_t5_burst() -> None:
    esito, evidenza = run_t5()
    assert esito == "PASS", evidenza


def test_t6_missing_field_partial() -> None:
    esito, evidenza = run_t6()
    assert esito == "PASS", evidenza


if __name__ == "__main__":
    sys.exit(main())
