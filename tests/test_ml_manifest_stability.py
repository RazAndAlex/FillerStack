"""T1 — determinismo BYTE del manifest (build-features idempotente).

Problema (handoff §6.2): due build-features consecutivi riscrivono
work/ml_dataset/manifest.yaml con byte DIVERSI — l'ordine delle chiavi
di normalization.per_valve segue l'ordine di iterazione di polars
group_by (fit_normalizer.iter_rows), non deterministico.

Fix: scrittore canonico `dump_manifest` in plcsim.ml_dataset.py —
chiavi delle mappe ordinate ricorsivamente (deterministico),
ordine delle LISTE preservato (`runs` è semantico), safe_dump in
blocco con allow_unicode=True → i byte emessi dipendono SOLO dal
contenuto. Difesa in profondità: normalizer_to_manifest emette le
chiavi machine_code ordinate (normalizer_from_manifest è insensibile
all'ordine → backward compatible).

Unit (veloci, nessun run): dump_manifest su dict con ordini di
inserimento DIVERSI ma stesso contenuto → file byte-identici;
ordine liste preservato; round-trip YAML; normalizer_to_manifest
ordinato + round-trip.

Integration (marcato slow, eseguito comunque): due build-features
consecutivi via subprocess (riusa i parquet esistenti sotto
work/ml_dataset/runs/, sola lettura) → manifest byte-identici
(sha256). build-features riscrive features/*.parquet e manifest.yaml
— comportamento NORMALE del comando, consentito.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plcsim.ml_dataset import (          # noqa: E402
    dump_manifest,
    normalizer_from_manifest,
    normalizer_to_manifest,
)

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "work" / "ml_dataset" / "manifest.yaml"
_RUN_PQ = (REPO / "work" / "ml_dataset" / "runs" / "healthy_train"
           / "valve_cycles.parquet")


def _manifest_like(valve_order: list[str]) -> dict:
    """Manifest v0 realistico con per_valve nell'ordine di inserimento dato."""
    per_valve = {}
    for mc in valve_order:
        per_valve[mc] = {
            f"mean_{f}": [float(i + 0.5), float(i + 1)]
            for i, f in enumerate(("fillingtime", "tailtime"))
        }
    return {
        "version": 0,
        "N": 50,
        "tail_policy": "drop",
        "feature_schema": "work/ml-feature-schema.md",
        "generated_at": "2026-08-11T10:32:50Z",
        "runs": [{"name": "healthy_train", "scenario_file":
                  "scenarios/m4_healthy.yaml", "seed": 101, "split": "train",
                  "hashes": {"valve_cycles": "abc123"}, "n_cycles": 100}],
        "normalization": {"source_run": "healthy_train",
                          "per_valve": per_valve},
    }


# --------------------------------------------------------------------------
# Unit: dump_manifest — byte-stabile indipendente dall'ordine di inserimento
# --------------------------------------------------------------------------

def test_dump_manifest_byte_stable_across_insertion_orders(tmp_path):
    valves = [f"valve{i}" for i in (34, 8, 17, 2, 20, 13, 5)]
    d1 = _manifest_like(valves)                      # ordine di inserimento A
    d2 = _manifest_like(list(reversed(valves)))      # ordine B, stesso contenuto
    assert d1 == d2                                  # contenuto identico
    p1, p2 = tmp_path / "m1.yaml", tmp_path / "m2.yaml"
    dump_manifest(d1, p1)
    dump_manifest(d2, p2)
    b1, b2 = p1.read_bytes(), p2.read_bytes()
    assert b1 == b2, "dump_manifest NON byte-stabile al variare "
    "dell'ordine di inserimento"
    # il file resta YAML valido e ri-legge lo stesso contenuto
    assert yaml.safe_load(b1) == d1
    # e l'ordine delle chiavi emesse è deterministico (ordinato)
    loaded = yaml.safe_load(b1)
    assert list(loaded["normalization"]["per_valve"]) == sorted(valves)


def test_dump_manifest_preserves_runs_list_order(tmp_path):
    """L'ordine delle LISTE è semantico (`runs`) e va preservato."""
    data = _manifest_like(["valve1"])
    data["runs"] = [{"name": "b"}, {"name": "a"}, {"name": "c"}]
    p = tmp_path / "m.yaml"
    dump_manifest(data, p)
    loaded = yaml.safe_load(p.read_bytes())
    assert [r["name"] for r in loaded["runs"]] == ["b", "a", "c"]


def test_dump_manifest_accepts_str_path(tmp_path):
    p = tmp_path / "m.yaml"
    dump_manifest(_manifest_like(["valve1"]), str(p))
    assert p.exists()
    assert yaml.safe_load(p.read_bytes()) == _manifest_like(["valve1"])


# --------------------------------------------------------------------------
# Unit: normalizer_to_manifest — chiavi machine_code ORDINATE + round-trip
# --------------------------------------------------------------------------

def test_normalizer_to_manifest_sorted_keys_and_roundtrip():
    stats = {
        "valve34": {"mean_fillingtime": (1.5, 0.25),
                    "std_fillingtime": (2.0, None)},
        "valve8": {"mean_fillingtime": (3.5, 0.5)},
        "valve17": {"mean_fillingtime": (4.5, 0.75)},
    }
    out = normalizer_to_manifest(stats)
    assert list(out) == ["valve17", "valve34", "valve8"]   # sorted
    assert out["valve34"]["mean_fillingtime"] == [1.5, 0.25]
    assert out["valve8"]["mean_fillingtime"] == [3.5, 0.5]
    # round-trip: normalizer_from_manifest è insensibile all'ordine
    assert normalizer_from_manifest(out) == stats


def test_normalizer_to_manifest_empty():
    assert normalizer_to_manifest({}) == {}
    assert normalizer_from_manifest({}) == {}


# --------------------------------------------------------------------------
# Integration: due build-features consecutivi → manifest byte-identici
# --------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.skipif(not _RUN_PQ.exists(),
                    reason="run parquet assenti (pre-marker): build-features "
                           "non eseguibile")
def test_build_features_manifest_byte_stable():
    """Due build-features consecutivi devono produrre manifest IDENTICI
    byte-per-byte (sha256). Riusa i run parquet esistenti (sola lettura)."""
    def build() -> float:
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, "-m", "plcsim.ml_pipeline", "build-features"],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            timeout=300)
        assert proc.returncode == 0, \
            f"build-features exit {proc.returncode}: {proc.stderr[-3000:]}"
        return time.time() - t0

    t1 = build()
    h1 = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    t2 = build()
    h2 = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    assert h1 == h2, (
        f"manifest NON byte-stabile tra due build-features consecutivi: "
        f"run1 sha256={h1} run2 sha256={h2}")
    print(f"[slow] build-features wall: run1={t1:.1f}s run2={t2:.1f}s "
          f"manifest sha256={h1}")
