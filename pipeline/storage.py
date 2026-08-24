"""Storage operazionale M10 (ADR-0021) — accesso PostgreSQL via SQLAlchemy.

Livello di accesso UNICO allo storico operazionale (KPI/predictions/alert/
stato macchina). Tutto il codice che persiste o interroga il dato operativo
passa da qui: l'esistenza di un backend specifico (PostgreSQL in compose) è
un dettaglio di infra, il resto del codice si accoppia a QUESTA interfaccia.

Schema v1 (spec M10 §2):

- `predictions`         — prediction record v1 (1:1 con prediction-v1.json);
- `alerts`              — stato currente per (valve_id, fault_type), UNA riga
                          per lineage (unique `uq_alerts_run_valve_fault`): il ciclo
                          di vita open→sustained→closed→reopen AGGIORNA la
                          stessa riga, PK `alert_id` stabile e deterministica
                          (`alert_id_for`, uuid5 su ALERT_NS);
- `alert_transitions`   — log append-only delle transizioni (tracciabilità),
                          con FK ad `alerts.alert_id`;
- `machine_state`       — stato OMAC corrente (key/value); ospita anche il
                          KV `bottle_counter` (writer realtime, OEE Home L0);
- `machine_state_history` — history append-only delle transizioni OMAC
                           (OEE Home L0, spec dashboard §7.2 / oee-backend
                           spec B): id SERIAL, state_code/state_label,
                           entered_ts/exited_ts, source, indice su entered_ts.

Convivenza coi layer esistenti (spec M10, architettura a 3 livelli):
- raw ad alta frequenza → Parquet partizionato (M8, ADR-0019), INVARIATO;
- config locale app → SQLite, INVARIATO;
- operazionale caldo → QUESTO layer (PostgreSQL).

Dipendenze nuove (registrate in requirements.txt, spec M10 §8): SQLAlchemy
(layer ORM/core) + psycopg (driver). L'engine usa `psycopg` (v3) se
disponibile, altrimenti `postgresql+psycopg2` — ma il contratto è SQLAlchemy,
quindi il chiamante non si accoppia al driver.

Anti-POC-itis: nessun ORM pesante — si usa SQLAlchemy **Core** (Table +
expressions) per trasparenza e testabilità; le transazioni sono esplicite.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid5

import uuid

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

# ---------------------------------------------------------------------------
# Connessione (default POC + overridabili via env / parametro)
# ---------------------------------------------------------------------------
DEFAULT_DATABASE_URL = "postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim"

# Fallbacks: se psycopg (v3) non è installato, prova psycopg2; l'URL è
# generato dal chiamante `make_engine` con `driver` esplicito.
SETTINGS_TABLE = "machine_state"

# Chiave KV del contatore bottiglie corrente (machine_state, spec oee §B2).
BOTTLE_COUNTER_KEY = "bottle_counter"


def _default_url() -> str:
    import os
    return os.environ.get("PLCSIM_DATABASE_URL", DEFAULT_DATABASE_URL)


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Crea un engine SQLAlchemy per PostgreSQL.

    `url` può essere:
    - `None` → `PLCSIM_DATABASE_URL` o `DEFAULT_DATABASE_URL`;
    - un URL SQLAlchemy completo già postgres (`postgresql+psycopg://…` o
      `postgresql+psycopg2://…`) → usato così com'è;
    - una stringa host NUDO senza scheme (`://` assente, es.
      `localhost:5432/plcsim`) → prefixata con `postgresql+psycopg://`;
    - qualsiasi altro URL che contiene `://` ma non inizia con `postgresql+`
      (es. `sqlite:///x.db`, `postgres://…`) → `ValueError("unsupported DB
      url")`: niente riscritture silenziose di scheme estranei.

    `connect_timeout=3` è applicato sul path `+psycopg` (testabilità, ADR-0021).
    """
    if url is None:
        url = _default_url()
    if "://" not in url:
        # stringa host nuda (host[:port[/dbname]]) → dialetto postgres default
        url = "postgresql+psycopg://" + url
    elif not url.startswith("postgresql+"):
        raise ValueError("unsupported DB url")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        url = url.replace("postgresql+psycopg://", "postgresql+psycopg2://", 1)
    connect_args: dict[str, Any] = {}
    if "+psycopg" in url:
        connect_args["connect_timeout"] = 3
    return create_engine(url, echo=echo, future=True, connect_args=connect_args)


# ---------------------------------------------------------------------------
# Alert: stato corrente + lineage deterministico
# ---------------------------------------------------------------------------
# Stati possibili della riga alerts (condivisi: importabili da qui per
# API/alert engine/dashboard — niente literal sparsi).
ALERT_STATUSES: tuple[str, ...] = ("open", "sustained", "closed")

# Namespace FISSO per gli alert id deterministici di lineage: la PK della riga
# `alerts` è stabile per (valve_id, fault_type) attraverso l'intero ciclo di
# vita open→sustained→closed→reopen. Il valore è arbitrario ma congelato:
# cambiarlo cambierebbe tutti gli alert_id storici.
ALERT_NS = uuid.UUID("7d2b8c4e-1a5f-4b3d-9c6e-8f0a2b4d6e81")

# Nome del vincolo UNIQUE pre-fix (3 colonne) che init() cerca per la
# migrazione POC (vedi Storage._migrate_stale_alert_schema).
_STALE_ALERT_CONSTRAINT = "uq_alerts_valve_fault_status"

# Run attribuito alle prediction gia' presenti quando `run_id` e' stato
# aggiunto a `predictions` (2026-08-22). Le 723.110 righe di allora erano
# state rigenerate su `storico_60d` dopo la migrazione di `cycles` del
# 2026-08-19: verificato perche' il `window_end_cycle_id` massimo per la
# valvola 1 era 1.036.100, coerente con i 36.241.832 cicli di quel run su 35
# valvole. La migrazione e' un no-op se la colonna esiste gia'.
LEGACY_PREDICTIONS_RUN_ID = "storico_60d"


def alert_id_for(valve_id: int, fault_type: str, run_id: str) -> UUID:
    """Alert id deterministico di lineage per (run_id, valve_id, fault_type).

    ``uuid5(ALERT_NS, f"{run_id}:{valve_id}:{fault_type}")`` — identico a
    ogni upsert, quindi la PK della riga `alerts` non cambia mai per una
    data terna, anche dopo close e reopen.

    Il run è entrato nella derivazione il 2026-08-22. L'invariante che
    `ALERT_NS` protegge è la **stabilità lungo la vita di un allarme** — l'id
    non deve cambiare quando lo stato passa open→sustained→closed→reopen — e
    resta intatta: dentro un run l'id è stabile esattamente come prima.
    Quello che è cambiato, una volta sola, è la base degli id storici, che la
    migrazione ha riscritto insieme alla chiave esterna delle transizioni.
    Era possibile perché nessun codice fuori da `alert.py` e `storage.py`
    usa un `alert_id` come chiave, e nulla lo conserva fuori dal database.
    """
    run = str(run_id).strip()
    if not run:
        raise ValueError(
            "run_id mancante: senza run due macchine diverse condividerebbero "
            "la stessa riga di allarme")
    return uuid5(ALERT_NS, f"{run}:{valve_id}:{fault_type}")


# ---------------------------------------------------------------------------
# Schema v1 (SQLAlchemy Core)
# ---------------------------------------------------------------------------
metadata: Any = None  # popolato in build_metadata()


def build_metadata():
    from sqlalchemy import MetaData
    m = MetaData()

    predictions = Table(
        "predictions", m,
        Column("prediction_id", PG_UUID(as_uuid=True), primary_key=True),
        Column("model_version", String, nullable=False),
        Column("feature_schema_version", String, nullable=False),
        Column("prediction_ts", DateTime(timezone=True), nullable=False),
        Column("machine_id", String, nullable=False),
        Column("valve_id", Integer, nullable=False),
        Column("window_idx", Integer, nullable=False),
        Column("window_end_cycle_id", Integer, nullable=False),
        Column("predicted_label", String, nullable=False),
        Column("anomaly_score", Float, nullable=False),
        Column("probabilities", JSONB, nullable=False),
        Column("feature_fingerprint", String(64), nullable=False),
        Column("run_id", String, nullable=False),
        Index("ix_predictions_valve_wcid", "valve_id", "window_end_cycle_id"),
        Index("ix_predictions_valve_ts", "valve_id", "prediction_ts"),
        Index("ix_predictions_run_valve_wcid",
              "run_id", "valve_id", "window_end_cycle_id"),
        Index("ix_predictions_run_valve_ts",
              "run_id", "valve_id", "prediction_ts"),
    )

    alerts = Table(
        "alerts", m,
        Column("alert_id", PG_UUID(as_uuid=True), primary_key=True),
        Column("valve_id", Integer, nullable=False),
        Column("fault_type", String, nullable=False),
        Column("status", String, nullable=False),  # open | sustained | closed
        Column("opened_ts", DateTime(timezone=True)),
        Column("last_seen_ts", DateTime(timezone=True)),
        Column("closed_ts", DateTime(timezone=True)),
        Column("max_score_seen", Float, default=0.0),
        Column("n_cycles_above", Integer, default=0),
        Column("opened_at_cycle_id", Integer),
        Column("closed_at_cycle_id", Integer),
        Column("run_id", String, nullable=False),
        # dedup (ADR-0021, spec M10 §2): UNA riga per
        # (run_id, valve_id, fault_type). Il vincolo pre-fix includeva
        # `status` (3 colonne) e accumulava una riga per episodio; ora il
        # ciclo di vita open→sustained→closed→reopen AGGIORNA la stessa riga
        # (PK `alert_id` stabile, mai toccata dall'ON CONFLICT).
        #
        # `run_id` è entrato nella chiave il 2026-08-22. Senza, le righe erano
        # condivise fra run: un run live ereditava gli stati del run corrente
        # con cronologia punteggi vuota e la prima finestra sotto soglia ne
        # chiudeva gli allarmi. Misurato sul database vero: la valvola 21
        # passava `sustained → closed` alla prima prediction live.
        UniqueConstraint("run_id", "valve_id", "fault_type",
                         name="uq_alerts_run_valve_fault"),
    )

    alert_transitions = Table(
        "alert_transitions", m,
        Column("transition_id", PG_UUID(as_uuid=True), primary_key=True),
        Column("alert_id", PG_UUID(as_uuid=True),
               ForeignKey("alerts.alert_id"), nullable=False),
        Column("transition_ts", DateTime(timezone=True), nullable=False),
        Column("from_status", String, nullable=False),
        Column("to_status", String, nullable=False),
        Column("anomaly_score", Float, nullable=False),
        Column("threshold_open", Float, nullable=False),
        Column("threshold_close", Float, nullable=False),
        Column("window_end_cycle_id", Integer, nullable=False),
        Column("valve_id", Integer, nullable=False),
        Column("fault_type", String, nullable=False),
        Column("run_id", String, nullable=False),
        Index("ix_alert_transitions_alert", "alert_id"),
        Index("ix_alert_transitions_valve", "valve_id"),
        Index("ix_alert_transitions_run_valve", "run_id", "valve_id"),
    )

    machine_state = Table(
        "machine_state", m,
        Column("key", String, primary_key=True),
        Column("value", Text, nullable=False),
        Column("updated_ts", DateTime(timezone=True), nullable=False),
    )

    # History append-only delle transizioni OMAC (spec oee-backend §B1):
    # una riga per transizione, entered_ts = quando la macchina È ENTRATA
    # nello stato, exited_ts = quando ne è uscita (NULL = stato corrente).
    # È la sorgente di Availability dell'OEE Home L0 (finestra turno/giorno):
    # l'endpoint aggrega gli intervalli [entered_ts, exited_ts) clippati
    # sulla finestra. Popolata dal writer realtime (plcsim/realtime.py) e
    # dal consumer MQTT (pipeline/ingest.py, topic plant/filler01/state).
    machine_state_history = Table(
        "machine_state_history", m,
        Column("id", Integer, primary_key=True, autoincrement=True),  # SERIAL
        Column("state_code", Integer, nullable=False),   # OMAC: 1 Running …
        Column("state_label", Text, nullable=False),     # "Running" ecc.
        Column("entered_ts", DateTime(timezone=True), nullable=False),
        Column("exited_ts", DateTime(timezone=True)),
        Column("source", Text),   # "realtime" | "mqtt:plant/filler01/state" | …
        Index("ix_machine_state_history_entered", "entered_ts"),
    )

    return m


# ---------------------------------------------------------------------------
# Storage client
# ---------------------------------------------------------------------------
class Storage:
    """Client di accesso allo storico operazionale (PostgreSQL, SQLAlchemy Core)."""

    def __init__(self, engine: Engine | None = None, url: str | None = None):
        self.engine = engine or make_engine(url)
        self.metadata = build_metadata()
        self.predictions = self.metadata.tables["predictions"]
        self.alerts = self.metadata.tables["alerts"]
        self.alert_transitions = self.metadata.tables["alert_transitions"]
        self.machine_state = self.metadata.tables["machine_state"]
        self.machine_state_history = \
            self.metadata.tables["machine_state_history"]

    # -- lifecycle ----------------------------------------------------------
    def init(self) -> None:
        """Crea tabelle if-not-exists (idempotente, seed) + migrazioni."""
        self._migrate_stale_alert_schema()
        self.metadata.create_all(self.engine, checkfirst=True)
        self._migrate_predictions_run_id()
        self._migrate_alerts_run_id()

    def _migrate_alerts_run_id(self) -> None:
        """MIGRAZIONE `run_id` su `alerts` e `alert_transitions` (2026-08-22).

        Idempotente: se `alerts.run_id` esiste già è un no-op immediato.

        Riscrive anche l'IDENTITÀ delle righe esistenti, perché `alert_id` è
        derivato da `alert_id_for`, che ora include il run. Tutto avviene in
        UNA transazione, nell'ordine imposto dalla chiave esterna:

        1. colonne `run_id` su entrambe le tabelle, valorizzate con
           `LEGACY_PREDICTIONS_RUN_ID` (le righe presenti appartengono allo
           stesso run delle prediction che le hanno generate), poi NOT NULL;
        2. la FK `alert_transitions.alert_id → alerts.alert_id` viene
           rimossa, perché il passo 3 cambia entrambe le estremità;
        3. i nuovi `alert_id` sono calcolati in Python con `alert_id_for` —
           non riprodotti in SQL — così esiste UNA sola definizione di quella
           derivazione e non due che possono divergere;
        4. la FK viene ricreata, e il vincolo unico passa da
           (valve_id, fault_type) a (run_id, valve_id, fault_type).

        Se qualcosa fallisce, la transazione annulla tutto: non esiste uno
        stato intermedio con transizioni orfane.
        """
        insp = inspect(self.engine)
        names = set(insp.get_table_names())
        if "alerts" not in names:
            return
        existing = {c["name"] for c in insp.get_columns("alerts")}
        if "run_id" in existing:
            return
        legacy = LEGACY_PREDICTIONS_RUN_ID
        with self.engine.begin() as conn:
            for table in ("alerts", "alert_transitions"):
                if table not in names:
                    continue
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS run_id VARCHAR"))
                conn.execute(
                    text(f"UPDATE {table} SET run_id = :rid WHERE run_id IS NULL"),
                    {"rid": legacy})
                conn.execute(text(
                    f"ALTER TABLE {table} ALTER COLUMN run_id SET NOT NULL"))

            has_transitions = "alert_transitions" in names
            if has_transitions:
                conn.execute(text(
                    "ALTER TABLE alert_transitions DROP CONSTRAINT IF EXISTS "
                    "alert_transitions_alert_id_fkey"))

            rows = conn.execute(text(
                "SELECT alert_id, valve_id, fault_type FROM alerts")).fetchall()
            for old_id, valve_id, fault_type in rows:
                new_id = alert_id_for(int(valve_id), str(fault_type), legacy)
                if str(new_id) == str(old_id):
                    continue
                conn.execute(
                    text("UPDATE alerts SET alert_id = :new WHERE alert_id = :old"),
                    {"new": new_id, "old": old_id})
                if has_transitions:
                    conn.execute(
                        text("UPDATE alert_transitions SET alert_id = :new "
                             "WHERE alert_id = :old"),
                        {"new": new_id, "old": old_id})

            if has_transitions:
                conn.execute(text(
                    "ALTER TABLE alert_transitions "
                    "ADD CONSTRAINT alert_transitions_alert_id_fkey "
                    "FOREIGN KEY (alert_id) REFERENCES alerts (alert_id)"))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_alert_transitions_run_valve "
                    "ON alert_transitions (run_id, valve_id)"))

            conn.execute(text(
                "ALTER TABLE alerts DROP CONSTRAINT IF EXISTS "
                "uq_alerts_valve_fault"))
            conn.execute(text(
                "ALTER TABLE alerts ADD CONSTRAINT uq_alerts_run_valve_fault "
                "UNIQUE (run_id, valve_id, fault_type)"))

    def _migrate_predictions_run_id(self) -> None:
        """MIGRAZIONE `run_id` su `predictions` (2026-08-22), idempotente.

        Ricalca la migrazione di `cycles` del 2026-08-19
        (`CyclesStorage.init`): se la colonna manca su una tabella gia'
        popolata viene aggiunta nullable, valorizzata con
        `LEGACY_PREDICTIONS_RUN_ID`, poi resa NOT NULL. Se la colonna c'e'
        gia' e' un no-op: nessuna riga toccata, nessun conteggio alterato.

        Serve perche' il watermark dell'inference e la cronologia degli
        allarmi leggevano `predictions` senza discriminante di run. Con lo
        storico che occupava per ogni valvola tutti i `window_end_cycle_id`
        multipli di 50 fino a 1.036.100, un run live che riparte da
        `cycle_id` 1 trovava ogni propria finestra gia' predetta e non
        produceva nulla.
        """
        insp = inspect(self.engine)
        if "predictions" not in insp.get_table_names():
            return
        existing = {c["name"] for c in insp.get_columns("predictions")}
        if "run_id" in existing:
            return
        with self.engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS run_id VARCHAR"))
            conn.execute(
                text("UPDATE predictions SET run_id = :rid WHERE run_id IS NULL"),
                {"rid": LEGACY_PREDICTIONS_RUN_ID})
            conn.execute(text(
                "ALTER TABLE predictions ALTER COLUMN run_id SET NOT NULL"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_predictions_run_valve_wcid "
                "ON predictions (run_id, valve_id, window_end_cycle_id)"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_predictions_run_valve_ts "
                "ON predictions (run_id, valve_id, prediction_ts)"))

    def _migrate_stale_alert_schema(self) -> None:
        """MIGRAZIONE POC del vincolo alert pre-fix (bug B1).

        I DB `plcsim`/`plcsim_test` creati prima del fix portano ancora il
        vincolo UNIQUE a 3 colonne `uq_alerts_valve_fault_status` su
        (valve_id, fault_type, status), che permetteva più righe correnti per
        (valve_id, fault_type). `create_all(checkfirst=True)` NON altera le
        tabelle esistenti, quindi se il vincolo vecchio è presente si
        RICOSTRUISCONO alerts + alert_transitions con lo schema nuovo
        (DROP + CREATE). I dati demo in quei DB sono residui POC e possono
        essere persi (documentato); il log transizioni viene ricreato vuoto.
        """
        insp = inspect(self.engine)
        if "alerts" not in insp.get_table_names():
            return
        uniques = {c["name"] for c in insp.get_unique_constraints("alerts")}
        if _STALE_ALERT_CONSTRAINT not in uniques:
            return  # schema già nuovo (o ignoto): nulla da fare
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alert_transitions CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS alerts CASCADE"))

    def drop_all(self) -> None:
        """Rimozione tabelle (test)."""
        self.metadata.drop_all(self.engine)

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    # -- predictions --------------------------------------------------------
    def insert_prediction(self, record: dict[str, Any],
                          run_id: str) -> bool:
        """Inserisce un prediction record v1 (dict pred-fingerprint). Idempotente
        su prediction_id (INSERT ... ON CONFLICT DO NOTHING). Ritorna True solo
        se la riga è stata effettivamente inserita (False se già presente).

        Senza TOCTOU: nessun check-then-insert — l'ON CONFLICT è l'unico
        meccanismo di dedup. Il valore di ritorno si decide con `.returning()`:
        verificato su psycopg3 (3.3.4) via SQLAlchemy 2.0.52 che
        `CursorResult.rowcount` è -1 in ENTRAMBI i casi per questo statement
        (mentre il rowcount del cursore psycopg3 nudo dà 1/0); `RETURNING` è
        il segnale affidabile: riga restituita = inserita, None = conflitto
        saltato.

        `run_id` è un parametro e NON un campo del record: il contratto wire
        `edge/schemas/prediction-v1.json` dichiara
        `additionalProperties: false`, quindi un record che lo portasse non
        sarebbe più validabile. È la stessa scelta fatta per `cycles`, dove
        il run si attribuisce al momento della scrittura e non viaggia
        nell'envelope.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        run = str(run_id).strip()
        if not run:
            raise ValueError(
                "run_id mancante: una prediction senza run rimescolerebbe "
                "watermark e cronologia allarmi fra run diversi")
        pid = UUID(record["prediction_id"])
        stmt = pg_insert(self.predictions).values({
            "prediction_id": pid,
            "model_version": record["model_version"],
            "feature_schema_version": record["feature_schema_version"],
            "prediction_ts": _to_dt(record["prediction_ts"]),
            "machine_id": record["machine_id"],
            "valve_id": record["valve_id"],
            "window_idx": record["window_idx"],
            "window_end_cycle_id": record["window_end_cycle_id"],
            "predicted_label": record["predicted_label"],
            "anomaly_score": record["anomaly_score"],
            "probabilities": record["probabilities"],
            "feature_fingerprint": record["feature_fingerprint"],
            "run_id": run,
        })
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["prediction_id"]
        ).returning(self.predictions.c.prediction_id)
        with self.engine.begin() as conn:
            result = conn.execute(stmt)
        return result.first() is not None

    def existing_window_end_cycle_ids(self, valve_id: int,
                                      run_id: str) -> set[int]:
        """Watermark: window_end_cycle_id già predetti per (valvola, run).

        Il filtro di run è obbligatorio: senza, le finestre di un run
        storico fanno da watermark a un run nuovo che rinumera i cycle_id
        da 1, e il run nuovo non produce nulla (vedi
        `_migrate_predictions_run_id`).
        """
        if not str(run_id).strip():
            raise ValueError("run_id mancante: il watermark richiede un run")
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self.predictions.c.window_end_cycle_id)
                .where(self.predictions.c.valve_id == valve_id,
                       self.predictions.c.run_id == run_id)
            ).fetchall()
        return {r[0] for r in rows}

    # -- alerts -------------------------------------------------------------
    def upsert_alert(self, *, alert_id: str, valve_id: int, fault_type: str,
                     status: str, run_id: str, opened_ts=None,
                     last_seen_ts=None, closed_ts=None, max_score_seen=0.0,
                     n_cycles_above=0, opened_at_cycle_id=None,
                     closed_at_cycle_id=None) -> None:
        """Upsert della riga di STATO CORRENTE per
        (run_id, valve_id, fault_type).

        CONTRATTO FULL-STATE: il chiamante passa lo stato COMPLETO
        dell'alert (l'alert engine tiene lo stato in memoria e lo persiste
        integralmente). L'ON CONFLICT su `uq_alerts_valve_fault` sovrascrive
        TUTTE le colonne mutabili con i valori passati: una chiamata parziale
        azzererebbe l'accumulo (max_score_seen / n_cycles_above) — non si fa
        alcuna deduzione qui, il chiamante passa sempre lo stato intero.

        `alert_id` NON è mai aggiornato in conflitto: è l'identità di lineage
        deterministica (``alert_id_for``), stabile su open→sustained→closed→
        reopen, quindi la PK della riga non cambia mai. Su riapertura
        (status "open" dopo "closed"), `opened_ts`/`opened_at_cycle_id`
        vengono rinfrescati con i valori del nuovo episodio.
        """
        if status not in ALERT_STATUSES:
            raise ValueError(
                f"invalid alert status {status!r}; must be one of {ALERT_STATUSES}")
        run = str(run_id).strip()
        if not run:
            raise ValueError(
                "run_id mancante: un allarme senza run verrebbe condiviso fra "
                "run diversi, e un run nuovo chiuderebbe gli allarmi del vecchio")
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(self.alerts).values(
            alert_id=UUID(alert_id), valve_id=valve_id, fault_type=fault_type,
            status=status, run_id=run,
            opened_ts=_to_dt(opened_ts) if opened_ts is not None else None,
            last_seen_ts=_to_dt(last_seen_ts) if last_seen_ts is not None else None,
            closed_ts=_to_dt(closed_ts) if closed_ts is not None else None,
            max_score_seen=max_score_seen,
            n_cycles_above=n_cycles_above,
            opened_at_cycle_id=opened_at_cycle_id,
            closed_at_cycle_id=closed_at_cycle_id,
        )
        # Conflitto su (run_id, valve_id, fault_type): aggiorna la riga
        # corrente di QUEL run. NB: alert_id e run_id fuori dal set_ — la PK
        # di lineage e il run di appartenenza non si toccano mai.
        stmt = stmt.on_conflict_do_update(
            constraint="uq_alerts_run_valve_fault",
            set_=dict(
                status=stmt.excluded.status,
                opened_ts=stmt.excluded.opened_ts,
                last_seen_ts=stmt.excluded.last_seen_ts,
                closed_ts=stmt.excluded.closed_ts,
                max_score_seen=stmt.excluded.max_score_seen,
                n_cycles_above=stmt.excluded.n_cycles_above,
                opened_at_cycle_id=stmt.excluded.opened_at_cycle_id,
                closed_at_cycle_id=stmt.excluded.closed_at_cycle_id,
            ),
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def insert_transition(self, *, transition_id: str, alert_id: str,
                          transition_ts, from_status: str, to_status: str,
                          anomaly_score: float, threshold_open: float,
                          threshold_close: float, window_end_cycle_id: int,
                          valve_id: int, fault_type: str,
                          run_id: str) -> None:
        """Append-only su `alert_transitions` (tracciabilità §68).

        `alert_id` è FK verso `alerts.alert_id`: il record esiste solo se la
        riga di lineage corrente esiste (IntegrityError altrimenti).

        `run_id` è ridondante rispetto alla riga `alerts` puntata, ed è qui
        di proposito: il log delle transizioni si filtra per run senza dover
        passare dalla join, che su 64.180 righe è la lettura più frequente.
        """
        with self.engine.begin() as conn:
            conn.execute(self.alert_transitions.insert().values(
                transition_id=UUID(transition_id),
                alert_id=UUID(alert_id),
                transition_ts=transition_ts,
                from_status=from_status, to_status=to_status,
                anomaly_score=anomaly_score, threshold_open=threshold_open,
                threshold_close=threshold_close,
                window_end_cycle_id=window_end_cycle_id,
                valve_id=valve_id, fault_type=fault_type,
                run_id=str(run_id).strip(),
            ))

    # -- machine_state ------------------------------------------------------
    def set_machine_state(self, key: str, value: Any) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(self.machine_state).values(
            key=key, value=json.dumps(value),
            updated_ts=datetime.now(timezone.utc),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_=dict(value=stmt.excluded.value,
                      updated_ts=stmt.excluded.updated_ts),
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def get_machine_state(self, key: str) -> Any | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self.machine_state.c.value)
                .where(self.machine_state.c.key == key)
            ).first()
        return json.loads(row[0]) if row else None

    # -- machine_state_history (OEE Home L0, spec oee-backend §B) -----------
    def log_machine_state_history(self, state_code: int, state_label: str,
                                  entered_ts=None, source: str | None = None) -> None:
        """Append-only: registra l'INGRESSO nello stato OMAC `state_code`.

        `entered_ts` default = now UTC. `exited_ts` resta NULL (stato
        corrente) finché un writer non chiama `close_machine_state_history`
        (o finché la query window non lo clipa con la transizione successiva
        / la fine finestra).
        """
        if entered_ts is None:
            entered_ts = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            conn.execute(self.machine_state_history.insert().values(
                state_code=int(state_code),
                state_label=str(state_label),
                entered_ts=entered_ts,
                exited_ts=None,
                source=source,
            ))

    def close_machine_state_history(self, exited_ts=None) -> None:
        """Chiude la transizione APERTA più recente (exited_ts IS NULL).

        Usata dal writer realtime alla transizione di stato successiva
        (exited_ts = entered_ts della nuova transizione). Nessuna riga
        aperta → no-op.
        """
        if exited_ts is None:
            exited_ts = datetime.now(timezone.utc)
        last_open = (
            select(self.machine_state_history.c.id)
            .where(self.machine_state_history.c.exited_ts.is_(None))
            .order_by(self.machine_state_history.c.entered_ts.desc(),
                      self.machine_state_history.c.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        with self.engine.begin() as conn:
            conn.execute(
                self.machine_state_history.update()
                .where(self.machine_state_history.c.id == last_open)
                .values(exited_ts=exited_ts))

    def current_machine_state_history(self) -> dict[str, Any] | None:
        """Ultima transizione registrata (stato corrente) o None."""
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self.machine_state_history)
                .order_by(self.machine_state_history.c.entered_ts.desc(),
                          self.machine_state_history.c.id.desc())
                .limit(1)
            ).first()
        return dict(row._mapping) if row else None

    def get_machine_state_history(self, start, end) -> list[dict[str, Any]]:
        """Transizioni con intervallo che si SOVRAPPONE a [start, end).

        Filtro: `entered_ts < end` (l'indice su entered_ts serve la query)
        AND (`exited_ts` NULL — stato ancora aperto — OR `exited_ts > start`).
        Ritorna dict completi ordinati per entered_ts (poi id): il chiamante
        (API OEE) costruisce gli intervalli clippati sulla finestra.
        """
        if not isinstance(start, datetime):
            start = _to_dt(start)
        if not isinstance(end, datetime):
            end = _to_dt(end)
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self.machine_state_history)
                .where(
                    self.machine_state_history.c.entered_ts < end,
                    or_(self.machine_state_history.c.exited_ts.is_(None),
                        self.machine_state_history.c.exited_ts > start),
                )
                .order_by(self.machine_state_history.c.entered_ts,
                          self.machine_state_history.c.id)
            ).fetchall()
        return [dict(r._mapping) for r in rows]

    # -- bottle_counter (KV su machine_state, spec oee-backend §B2) ---------
    def set_bottle_counter(self, value: int) -> None:
        """Persiste il contatore bottiglie corrente (KV `bottle_counter`).

        Sorgente: BottleCounter di TagSnapshot (writer realtime, a ogni
        ciclo chiuso) o payload stato su MQTT. È un valore CORRENTE (non
        windowato): l'OEE usa COUNT(cycles) come real; il KV resta come
        cross-check per la Home ("bottiglie oggi").
        """
        self.set_machine_state(BOTTLE_COUNTER_KEY, int(value))

    def get_bottle_counter(self) -> int | None:
        """Contatore bottiglie corrente, o None se mai scritto.

        Tollerante al formato: numero puro o dict {"value": n} (scritture
        future non rompono il lettore).
        """
        v = self.get_machine_state(BOTTLE_COUNTER_KEY)
        if isinstance(v, dict):
            v = v.get("value")
        return int(v) if v is not None else None


def _to_dt(iso: str) -> datetime:
    """ISO8601 → datetime tz-aware."""
    if isinstance(iso, datetime):
        return iso
    return datetime.fromisoformat(iso.replace("Z", "+00:00").replace("z", "+00:00"))


__all__ = [
    "Storage", "make_engine", "build_metadata", "DEFAULT_DATABASE_URL",
    "ALERT_STATUSES", "ALERT_NS", "alert_id_for", "BOTTLE_COUNTER_KEY",
]
