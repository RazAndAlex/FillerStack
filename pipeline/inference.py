"""Consumer inference M9 (ADR-0020) — modello ML -> prediction storicizzate.

Entry point: `python -m pipeline.inference`. Carica il modello (Track D,
`work/ml_dataset/model/model.joblib` + sidecar con model_version/feature_
schema_version), legge il raw già persistito (scan incrementale su watermark
`cycle_id`), calcola le 43 feature con `pipeline/features.py` (riuso di
`plcsim/ml_dataset.py`, zero training-serving skew), applica il modello
(`predict` + `predict_proba`) e produce **prediction record v1** conformi a
`edge/schemas/prediction-v1.json`, persistiti sullo **storico operazionale
PostgreSQL** (M10, ADR-0021 — in M9 era SQLite stdlib; il backend è cambiato,
il contratto di prediction NO).

MQTT: la pubblicazione su `plant/filler01/prediction` (QoS 1, retain false,
spec M9 §6.1) **NON è implementata** in questo modulo — nessun client paho,
nessun publish. Il topic resta riservato per ADR-0018; l'emissione è rinviata
a uno scope dedicato (vedi commento in fondo al file). Niente affermazioni
"predisposto ma disattivato": il percorso di publish non esiste.

Confini (contesto §93-§95): il ML OSSERVA, non controlla. Nessuna write su
OPC UA / broker di controllo; nessuna GT in ingresso (solo i 43 feature da
segnali); l'`anomaly_score` è un valore (la decisione/alert è M10).

Determinismo: lettura ordinata per (machine_code, cycle_id), modello logistico
lbfgs deterministico, nessun RNG. Lo scan incrementale usa `window_end_cycle_id`
come watermark: a ogni run riparte dall'ultima finestra già predetta (idempotente,
nessun duplicato).

Dipendenze: stdlib + ciò che già esiste (polars, sklearn, jsonschema) +
SQLAlchemy/psycopg (storage, M10). Nessun'altra dipendenza nuova.
"""
from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path
from typing import Any, Optional

import polars as pl

from pipeline.features import (
    FEATURE_COLUMNS,
    load_raw_valve_cycles,
    load_zstats,
    live_features,
)
from pipeline.prediction_schema import build_prediction
from pipeline.cycles_storage import CURRENT_RUN_ID_KEY
from pipeline.storage import Storage, make_engine
from plcsim.ml_model import MLModel

# ---------------------------------------------------------------------------
# Costanti / percorsi di default
# ---------------------------------------------------------------------------
MODEL_DIR_DEFAULT = Path("work/ml_dataset/model")
MODEL_PATH_DEFAULT = MODEL_DIR_DEFAULT / "model.joblib"
ZSTATS_PATH_DEFAULT = MODEL_DIR_DEFAULT / "zstats.json"
RAW_DIR_DEFAULT = Path("data/raw")
DATABASE_URL_DEFAULT = "postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim"
MANIFEST_DEFAULT = Path("work/ml_dataset/manifest.yaml")

FEATURE_SCHEMA_VERSION_DEFAULT = "ML-F1"  # work/ml-feature-schema.md, congelato


# ---------------------------------------------------------------------------
# MQTT (spec M9 §6.1 / review F3): la pubblicazione su
# `plant/filler01/prediction` (QoS 1, retain false) NON È IMPLEMENTATA e NON è
# "predisposta ma disattivata" — non esiste alcun client paho in questo
# modulo. Il topic resta riservato (ADR-0018); l'emissione è RINVIATA a uno
# scope dedicato. Questa riga + la docstring del modulo sono l'unico stato
# documentato della decisione.
# ---------------------------------------------------------------------------


def _resolve_model_version(sidecar_meta: dict, manifest: Optional[Path]) -> str:
    """model_version dal sidecar; fallback al code_version del manifest.

    ADR-0020: il model artifact porta `model_version`; il modello Track D
    (pre-M9) ha model_version=None → si deriva dal manifest `code_version`.
    RIGETTA (raise) se non risolvibile: nessuna prediction senza provenienza
    (ADR-0020 §5) — niente placeholder "unknown" (fix F7).
    """
    mv = sidecar_meta.get("model_version")
    if mv:
        return str(mv)
    if manifest is not None and manifest.exists():
        try:
            import yaml
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            mv = data.get("code_version")
            if mv:
                return str(mv)
        except Exception:
            pass
    raise ValueError(
        "model_version non risolvibile (sidecar senza model_version e manifest "
        "senza code_version) — niente prediction senza provenienza (ADR-0020 §5)")


class InferenceConsumer:
    """Inference read-only sul raw: feature -> modello -> prediction -> DB."""

    def __init__(
        self,
        model_path: str | Path = MODEL_PATH_DEFAULT,
        zstats_path: str | Path = ZSTATS_PATH_DEFAULT,
        raw_dir: str | Path = RAW_DIR_DEFAULT,
        db_url: str = DATABASE_URL_DEFAULT,
        manifest: str | Path | None = MANIFEST_DEFAULT,
        model_version: str | None = None,
        feature_schema_version: str = FEATURE_SCHEMA_VERSION_DEFAULT,
        healthy_label: str = "healthy",
        run_id: str | None = None,
    ) -> None:
        # run_id: discriminante delle prediction prodotte da questo consumer.
        # None significa "risolvilo dal KV `current_run_id` alla prima
        # connessione" — non "nessun run": una prediction senza run non e'
        # scrivibile (colonna NOT NULL) e il watermark non saprebbe da quale
        # storia leggere.
        self._run_id = str(run_id).strip() if run_id is not None else None
        if run_id is not None and not self._run_id:
            raise ValueError("run_id vuoto: passare un run esplicito o None")
        self.model_path = Path(model_path)
        self.zstats_path = Path(zstats_path)
        self.raw_dir = Path(raw_dir)
        self.db_url = db_url
        self.manifest = Path(manifest) if manifest else None
        self.healthy_label = healthy_label
        self.feature_schema_version = feature_schema_version

        # carica modello + z-score (nessun refit). Guardia anti-skew
        # (AC-M9-2 / review F8): senza zstats il modello riceverebbe feature
        # GREZZE (addestrato su z-score per-valvola) — rifiuto di costruire
        # il consumer in questo stato. live_features(zstats=None) resta
        # disponibile per harness/test: il raise è SOLO qui, alla costruzione.
        self.model: MLModel = MLModel.load(self.model_path)
        if not self.zstats_path.exists():
            raise FileNotFoundError(
                f"zstats.json mancante: {self.zstats_path} — rifiuto di "
                f"eseguire inferenza NON normalizzata (anti-skew AC-M9-2, "
                f"ADR-0020 §1). Ripristina il normalizer del modello.")
        self.zstats = load_zstats(str(self.zstats_path.parent))
        # risolvi model_version (sidecar -> manifest -> explicit).
        # _resolve_model_version RIGETTA (raise) se non risolvibile:
        # nessuna prediction senza provenienza (ADR-0020 §5, fix F7).
        sidecar = json.loads(
            Path(str(self.model_path) + ".json").read_text(encoding="utf-8"))
        self.model_version = model_version or \
            _resolve_model_version(sidecar, self.manifest)

        self.classes_ = [str(c) for c in self.model.classes_]
        self._db: Storage | None = None  # connessione lazy

    # -- storage operazionale (PostgreSQL, ADR-0021) ------------------------
    def _storage(self) -> Storage:
        if self._db is None:
            self._db = Storage(make_engine(self.db_url))
            self._db.init()
        return self._db

    @property
    def run_id(self) -> str:
        """Run di questo consumer, risolto una volta sola.

        Se non e' stato passato esplicitamente si legge il KV
        `current_run_id`, che e' la stessa fonte usata dalle rotte di lettura
        dell'API. Se manca anche quello il consumer si rifiuta di produrre:
        una prediction senza run tornerebbe a mescolare le storie.
        """
        # getattr con default: gli harness di test costruiscono il consumer
        # con `object.__new__` e non passano da `__init__`; l'assenza
        # dell'attributo vale "non ancora risolto", non un errore.
        if getattr(self, "_run_id", None) is None:
            resolved = self._storage().get_machine_state(CURRENT_RUN_ID_KEY)
            if not isinstance(resolved, str) or not resolved.strip():
                raise RuntimeError(
                    f"run_id non risolvibile: passare --run-id oppure "
                    f"valorizzare il KV `{CURRENT_RUN_ID_KEY}`")
            self._run_id = resolved.strip()
        return self._run_id

    def _existing_window_end_cycle_ids(self, valve_id: int) -> set[int]:
        """Watermark: window_end_cycle_id già predetti per (valvola, run)."""
        return self._storage().existing_window_end_cycle_ids(
            valve_id, self.run_id)

    def persist(self, record: dict[str, Any]) -> bool:
        """Inserisce un prediction record (dedup su prediction_id, UPSERT-safe).

        Ritorna True se la riga è stata effettivamente inserita — contratto
        Storage.insert_prediction (affidabile via RETURNING, no TOCTOU).

        Il run è quello del consumer, passato a parte: non entra nel record,
        che deve restare conforme a `prediction-v1.json`.
        """
        return self._storage().insert_prediction(record, self.run_id)

    # -- inferenza ----------------------------------------------------------
    @staticmethod
    def _event_ts_map(cycles: pl.DataFrame) -> dict[tuple[str, int], str | None]:
        """Mappa (machine_code, cycle_id) -> `event_ts` del ciclo.

        `cycles` è il frame di `load_raw_valve_cycles`, che porta già
        `machine_code`, `cycle_id` ed `event_ts` (stringa ISO8601 UTC, es.
        `2026-06-01T08:21:00.800000+00:00`). Nessuna conversione: l'`event_ts`
        raw è già `format: date-time` per `edge/schemas/prediction-v1.json`.
        """
        missing = [c for c in ("machine_code", "cycle_id", "event_ts")
                   if c not in cycles.columns]
        if missing:
            raise ValueError(
                f"cycles privo delle colonne {missing} — impossibile datare le "
                f"prediction sull'asse-dato")
        return {
            (str(mc), int(cid)): (str(ts) if ts is not None else None)
            for mc, cid, ts in cycles.select(
                ["machine_code", "cycle_id", "event_ts"]).iter_rows()
        }

    def predict_frame(
        self,
        features: pl.DataFrame,
        cycles: pl.DataFrame | None = None,
    ) -> list[dict[str, Any]]:
        """43 feature -> prediction record per finestra (deterministico).

        `features`: frame da `live_features` (con FEATURE_COLUMNS + chiavi
        machine_code/window_idx/last_cycle_id). Ritorna una lista di record
        prediction v1 (uno per riga), ordinati per (machine_code, window_idx).

        `cycles`: il frame raw da `load_raw_valve_cycles` da cui `features` è
        stato derivato. Serve UNICAMENTE a datare le prediction: il
        `prediction_ts` di ogni record è l'`event_ts` del ciclo che chiude la
        finestra (`window_end_cycle_id`), cioè il **tempo del dato**, non
        `now()`. Sul percorso live i due tempi coincidono a meno della latenza
        di inferenza; su un replay offline la differenza è l'intero
        scostamento fra data del dato e data di esecuzione (misurato: 78
        giorni su `work/m4_demo_dropout_1d`).

        Politica sui dati mancanti (regola «nessun numero inventato»):

        - se l'`event_ts` del ciclo di fine finestra è **assente o null**, il
          record viene **saltato** (con warning esplicito), non emesso con un
          timestamp di ripiego. Lasciare il campo assente non è un'opzione:
          `prediction_ts` è in `required` di `edge/schemas/prediction-v1.json`
          e `validate_prediction` rigetterebbe il record; ripiegare su `now()`
          fabbricherebbe un tempo che il dato non ha. Una finestra non datata
          è una finestra che non si può storicizzare, quindi non si emette.
        - se `cycles` non è passato affatto (`None`), non esiste alcuna
          sorgente di tempo-dato: si conserva il comportamento storico
          (`build_prediction` ripiega su `now_utc_iso()`). Questo ramo è
          riservato agli harness in-memory che NON persistono; `run()` passa
          sempre `cycles`.
        """
        if features.height == 0:
            return []
        ts_map = self._event_ts_map(cycles) if cycles is not None else None
        feats = features.sort(["machine_code", "window_idx"])
        X = feats.select(FEATURE_COLUMNS).to_numpy()
        y_pred = [str(c) for c in self.model.predict(X)]
        proba = self.model.predict_proba(X)
        keys = feats.select(["machine_code", "window_idx", "last_cycle_id"]).iter_rows()
        records: list[dict[str, Any]] = []
        for i, ((mc, wid, last_cycle), label, prob_row) in enumerate(
                zip(keys, y_pred, proba)):
            probs = {c: float(p) for c, p in zip(self.classes_, prob_row)}
            feat_vec = [float(v) for v in X[i]]  # vettore 43 feature della riga
            prediction_ts: str | None = None
            if ts_map is not None:
                prediction_ts = ts_map.get((str(mc), int(last_cycle)))
                if prediction_ts is None:
                    warnings.warn(
                        f"event_ts assente per il ciclo di fine finestra "
                        f"({mc}, cycle_id={last_cycle}): prediction saltata "
                        f"(nessun timestamp inventato)",
                        RuntimeWarning, stacklevel=2)
                    continue
            records.append(build_prediction(
                machine_code=str(mc),
                window_idx=int(wid),
                window_end_cycle_id=int(last_cycle),
                predicted_label=str(label),
                probabilities=probs,
                feature_vector=feat_vec,
                model_version=self.model_version,
                feature_schema_version=self.feature_schema_version,
                healthy_label=self.healthy_label,
                prediction_ts=prediction_ts,
            ))
        return records

    def run(self, dates: Optional[list[str]] = None) -> int:
        """Run completo: raw -> features -> prediction -> persist (+ alert M10).

        Watermark: salta le finestre con (valve_id, window_end_cycle_id) già
        predetti (idempotente sul restart: nessun duplicato logico). Gli
        existing window_end_cycle_id sono caricati UNA volta per valvola (fix
        F8: prima una query per record — N+1 round-trip). Il conteggio dei
        record NUOVI deriva dal valore di ritorno di insert_prediction
        (True = inserito). Ritorna il numero di prediction record NUOVI.
        """
        cycles = load_raw_valve_cycles(self.raw_dir, dates=dates)
        if cycles.height == 0:
            return 0
        feats = live_features(cycles, None, zstats=self.zstats)
        records = self.predict_frame(feats, cycles=cycles)
        # watermark per valvola: fetch una sola volta per valvola (non per
        # record); la cache copre anche i record successivi della stessa valvola
        existing_by_valve: dict[int, set[int]] = {}
        new_records = 0
        new_records_list: list[dict[str, Any]] = []
        for rec in records:
            valve_id = rec["valve_id"]
            existing = existing_by_valve.get(valve_id)
            if existing is None:
                existing = self._existing_window_end_cycle_ids(valve_id)
                existing_by_valve[valve_id] = existing
            if rec["window_end_cycle_id"] in existing:
                continue  # watermark: già predetta questa finestra
            if self.persist(rec):
                new_records += 1
                new_records_list.append(rec)
        self._process_alert_transitions(new_records_list)
        return new_records

    # -- alert M10 (wiring difensivo, ADR-0021) ----------------------------
    def _process_alert_transitions(
            self, records: list[dict[str, Any]]) -> None:
        """Wire M10 prediction→alert con degrado controllato.

        Solo i record NUOVI di questo run (le finestre già predette sono
        state processate nel run che le ha persistite — riprocessarle
        duplicherebbe sustain/transizioni). Se gli helper alert sono assenti
        o il wiring fallisce, le prediction sono comunque persistite; le
        transizioni sono saltate con un warning esplicito (loud), mai
        con un crash del run.
        """
        if not records:
            return
        # Nessuna guardia di run qui. Ce n'era una fra il 2026-08-22 mattina
        # e il pomeriggio, quando `predictions` aveva gia' il discriminante di
        # run e `alerts` no: allora un run non corrente ereditava gli stati
        # altrui con cronologia vuota e li chiudeva alla prima finestra sotto
        # soglia. Ora `alerts` e `alert_transitions` hanno anch'esse il run e
        # la separazione e' nello schema, dove doveva stare: ogni run vede
        # SOLO i propri allarmi, quindi non c'e' piu' niente da proteggere a
        # runtime.
        helpers = _alert_helpers()
        if helpers is None:
            warnings.warn(
                "alert wiring M10 assente: pipeline.alert non espone ancora "
                "AlertEngine/persist_events/load_states — transizioni alert "
                "SALTATE; le prediction restano persistite (degrado controllato)",
                RuntimeWarning, stacklevel=2)
            return
        engine_cls, config_cls, persist_events, load_states = helpers
        try:
            config = (config_cls(healthy_label=self.healthy_label)
                      if config_cls is not None else None)
            storage = self._storage()
            engine = engine_cls(config)
            states = load_states(storage, config, self.run_id)
            if isinstance(states, dict):
                engine.states = states
            if getattr(engine.config, "score_aggregation_enabled", False):
                load_score_history = _alert_score_history_loader()
                if load_score_history is not None:
                    engine._score_history = load_score_history(
                        storage,
                        engine.config,
                        excluded_prediction_ids=_score_history_exclusions(records),
                        run_id=self.run_id,
                    )
            events = engine.process(records)
            if events:
                persist_events(events, storage, self.run_id)
        except Exception as exc:  # noqa: BLE001 — degrado, mai perdere prediction
            warnings.warn(
                f"alert wiring M10 fallito ({type(exc).__name__}: {exc}) — "
                f"transizioni alert SALTATE; le prediction restano persistite",
                RuntimeWarning, stacklevel=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.inference",
        description="Consumer inference M9: raw -> 43 feature -> modello -> "
                    "prediction record v1 (PostgreSQL operazionale, ADR-0021).")
    ap.add_argument("--model", default=str(MODEL_PATH_DEFAULT))
    ap.add_argument("--zstats", default=str(ZSTATS_PATH_DEFAULT))
    ap.add_argument("--raw", default=str(RAW_DIR_DEFAULT))
    ap.add_argument("--db-url", default=DATABASE_URL_DEFAULT,
                    help="URL SQLAlchemy del DB operazionale (default: Postgres "
                         "locale; override con PLCSIM_DATABASE_URL)")
    ap.add_argument("--manifest", default=str(MANIFEST_DEFAULT))
    ap.add_argument("--model-version", default=None,
                    help="override model_version (default: sidecar o manifest)")
    ap.add_argument("--feature-schema-version",
                    default=FEATURE_SCHEMA_VERSION_DEFAULT)
    ap.add_argument("--dates", nargs="*", default=None,
                    help="date (YYYY-MM-DD) da processare (default: tutte)")
    ap.add_argument("--run-id", default=None,
                    help="run cui attribuire le prediction (default: KV "
                         "`current_run_id`)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    consumer = InferenceConsumer(
        model_path=args.model, zstats_path=args.zstats, raw_dir=args.raw,
        db_url=args.db_url, manifest=args.manifest,
        model_version=args.model_version,
        feature_schema_version=args.feature_schema_version,
        run_id=args.run_id)
    n = consumer.run(dates=args.dates)
    print(f"prediction: {n} record prodotti")
    return 0


# ---------------------------------------------------------------------------
# Alert wiring M10 (ADR-0021) — connessione prediction → transizioni alert.
#
# Gli helper `AlertEngine` / `persist_events` / `load_states` di
# `pipeline/alert.py` sono aggiunti da un worker parallelo nella stessa wave:
# l'import è LAZY e DIFENSIVO. Se il modulo non li espone ancora, il run
# persiste comunque le prediction e SALTA le transizioni alert con un warning
# esplicito — nessun crash, nessun silenzio. Contratto pinned:
# AlertEngine, persist_events(events, storage), load_states(storage, config).
# ---------------------------------------------------------------------------


def _alert_helpers() -> tuple | None:
    """Helper alert (M10) se disponibili, altrimenti None.

    Ritorna (AlertEngine, AlertConfig, persist_events, load_states) — o None
    se pipeline/alert non espone ancora il wiring (worker parallelo in corso).
    """
    try:
        from pipeline import alert as alert_module
    except Exception:
        return None
    engine_cls = getattr(alert_module, "AlertEngine", None)
    config_cls = getattr(alert_module, "AlertConfig", None)
    persist_events = getattr(alert_module, "persist_events", None)
    load_states = getattr(alert_module, "load_states", None)
    if engine_cls is None or persist_events is None or load_states is None:
        return None
    return engine_cls, config_cls, persist_events, load_states


def _alert_score_history_loader() -> Any | None:
    """Ritorna il loader K/N opzionale senza cambiare `_alert_helpers`."""
    try:
        from pipeline import alert as alert_module
    except Exception:
        return None
    loader = getattr(alert_module, "load_score_history", None)
    return loader if callable(loader) else None


def _score_history_exclusions(records: list[dict[str, Any]]) -> list[str]:
    """Identità del lotto già persistito da escludere dal seed K/N."""
    return [str(record["prediction_id"]) for record in records]


if __name__ == "__main__":
    raise SystemExit(main())
