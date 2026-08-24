"""Storage cicli — tabella `cycles` (read-model KPI per ciclo, spec dashboard §7).

Proiezione read-model 1:1 con le colonne del raw M8 (`_DATA_INT_FIELDS` /
`_DATA_BOOL_FIELDS` di `pipeline/ingest.py` — *nessun nome inventato*,
spec dashboard §7):
`run_id, machine_id, cycle_id, valve_id, filling_time_ms, tail_time_ms,
tail_pulse, pulse_count, target, delta_pulse, filling_step_out, filling_ok,
fill_quality_ok, sequence_ok, sample_valid, diagnostic_status, close_reason,
position_limit, filling_overtime, event_ts, source_ts, ingest_ts` (22 colonne
operative: `run_id` discriminante di run + 18 KPI/flag + 3 timestamp
windowabili per OEE, spec §4bis/§7.2 e oee-backend-spec §A).

`run_id` (2026-08-19): `cycle_id` riparte da 1 a ogni run del simulatore e i
run si sovrappongono nel tempo di parete, quindi né la coppia
(valve_id, cycle_id) né un filtro su `event_ts` distinguono due run nello
stesso database. Senza una colonna esplicita il secondo run veniva scartato
in silenzio da ON CONFLICT DO NOTHING.

Modulo STANDALONE: MetaData propria (`build_cycles_metadata`, NON
`storage.build_metadata()`), così la tabella `cycles` vive nello stesso
database ma con un ciclo di vita indipendente (init/drop separati) — la
dashboard non deve attendere migrazioni dello schema operazionale M10
(spec dashboard §7: "aggiungere la serie KPI ora … tabella `cycles`";
spec M10 §2 la marca "estensione futura"). Unica dipendenza da
`pipeline.storage`: `make_engine` (connessione) — niente `Storage`,
niente `build_metadata`. È PostgreSQL-specific (UUID/JSONB non usati qui,
ma ON CONFLICT sì): stessa piattaforma del resto del layer operazionale.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    inspect,
    select,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from pipeline.storage import make_engine

# 22 colonne operative — ordine stabile e congelato (spec dashboard §7,
# mirror di ingest.py + 3 timestamp OEE). Le 3 timestamp sono nullable
# (backfill onesto: sorgente senza campo → NULL, mai fabbricato) e in
# coda dopo filling_overtime per stabilità d'ordine.
CYCLES_COLUMNS: tuple[str, ...] = (
    "run_id",
    "machine_id", "cycle_id", "valve_id",
    "filling_time_ms", "tail_time_ms", "tail_pulse", "pulse_count", "target",
    "delta_pulse", "filling_step_out",
    "filling_ok", "fill_quality_ok", "sequence_ok", "sample_valid",
    "diagnostic_status", "close_reason", "position_limit", "filling_overtime",
    "event_ts", "source_ts", "ingest_ts",
)

# Limite protocollo PostgreSQL: 65.535 bind params/statement; 22 colonne ⇒
# max ~2.978 righe; 1.000 righe/statement = 22.000 params (conservativo).
# `bulk_insert` spezza internamente il batch in chunk di questa dimensione
# (stesso valore usato dal chiamante di backfill, cycles_backfill.py).
BULK_INSERT_CHUNK_ROWS = 1000

# Chiave KV del run corrente in `machine_state` (stesso meccanismo di
# `speed_target` / `baseline_window` — nessun meccanismo nuovo).
CURRENT_RUN_ID_KEY = "current_run_id"

# run_id assegnato alle righe pre-esistenti dalla migrazione: è il run che si
# trovava nel DB `plcsim` al momento della modifica (603.664 righe,
# work/m4_demo_dropout_1d). Costante storica, non un default per nuovi carichi.
# Contratto operativo del progetto: le valvole sono 1..35 (imposto anche in
# pipeline/api.py, "valve_id fuori range 1-35").
N_VALVOLE = 35

LEGACY_RUN_ID = "m4_demo_dropout_1d"


class AmbiguousRunError(RuntimeError):
    """Più run in `cycles` e nessuno selezionato — mai scelta silenziosa."""


_INT_FIELDS = ("filling_time_ms", "tail_time_ms", "tail_pulse", "pulse_count",
               "target", "delta_pulse", "filling_step_out")
_BOOL_FIELDS = ("filling_ok", "fill_quality_ok", "sequence_ok", "sample_valid",
                "position_limit", "filling_overtime")


def build_cycles_metadata() -> MetaData:
    """MetaData singola-tabella `cycles` (standalone, non storage.build_metadata).

    PK composita **(run_id, valve_id, cycle_id)**: `cycle_id` non è globale
    (è il numero ciclo della valvola) e riparte da 1 a ogni run, quindi la
    coppia valvola+ciclo NON è univoca fra run: serve `run_id` in testa.
    La PK è anche la **chiave ON CONFLICT** di `bulk_insert`
    (index_elements=["run_id", "valve_id", "cycle_id"]): un replay dello stesso batch
    (rilettura del Parquet raw, esattamente-once logico) è idempotente — le
    righe già presenti vengono saltate (DO NOTHING), nessun duplicato.

    Tipi coerenti con il raw M8 (spec §6.2): Int64 per cycle_id/valori,
    Boolean per i flag, String per machine_id/diagnostic_status/close_reason;
    DateTime(timezone=True) per event_ts/source_ts/ingest_ts (timestamptz,
    nullable per onestà: righe vecchie o bulk senza timestamp → NULL);
    colonne dati NULL-able (cicli parziali, policy T6), chiave NOT NULL.
    """
    m = MetaData()
    Table(
        "cycles", m,
        Column("run_id", String, nullable=False),
        Column("machine_id", String, nullable=False),
        Column("cycle_id", Integer, nullable=False),
        Column("valve_id", Integer, nullable=False),
        # KPI numerici (raw Int64; null ammessi per cicli parziali T6)
        *(Column(name, Integer, nullable=True) for name in _INT_FIELDS),
        # flag PLC (raw Boolean; null ammessi per cicli parziali T6)
        *(Column(name, Boolean, nullable=True) for name in _BOOL_FIELDS),
        Column("diagnostic_status", String, nullable=True),
        Column("close_reason", String, nullable=True),
        Column("event_ts", DateTime(timezone=True), nullable=True),
        Column("source_ts", DateTime(timezone=True), nullable=True),
        Column("ingest_ts", DateTime(timezone=True), nullable=True),
        PrimaryKeyConstraint("run_id", "valve_id", "cycle_id",
                             name="pk_cycles_run_valve_cycle"),
        # Indici obbligatori (non ottimizzazione): a 60 giorni la tabella pesa
        # ~4,6 GB e senza questi ogni GET fa seq scan completo.
        Index("ix_cycles_run_event_ts", "run_id", "event_ts"),
        Index("ix_cycles_run_valve_cycle_desc", "run_id", "valve_id",
              text("cycle_id DESC")),
        # Coprente per l'OEE: `valve_id` e `fill_quality_ok` in INCLUDE fanno
        # dell'aggregazione per finestra un Index Only Scan (Heap Fetches: 0
        # dopo VACUUM). Misurato il 2026-08-20 su 36,2 milioni di righe:
        # /machine/oee?window=day 1,37 s senza -> 0,85 s con; la serie OEE
        # 9,9 s -> 2,1 s. Pesa ~1,75 GB: e' un costo dichiarato, non un
        # effetto collaterale.
        Index("ix_cycles_run_event_ts_cover", "run_id", "event_ts",
              postgresql_include=["valve_id", "fill_quality_ok"]),
    )
    return m


class CyclesStorage:
    """Accesso alla tabella `cycles` (KPI per ciclo, read-model dashboard).

    Ciclo di vita indipendente dal layer operazionale M10: `init()` crea solo
    `cycles`; `metadata.drop_all(engine)` rimuove solo `cycles`.
    """

    def __init__(self, engine: Engine | None = None, url: str | None = None):
        self.engine = engine or make_engine(url)
        self.metadata = build_cycles_metadata()
        self.cycles = self.metadata.tables["cycles"]

    def init(self) -> None:
        """Crea la tabella `cycles` if-not-exists (idempotente) + migrazioni.

        Due migrazioni idempotenti per DB pre-esistenti:

        1. colonne timestamp (spec wire §A) — ADD COLUMN IF NOT EXISTS;
        2. **`run_id`** (2026-08-19): se la colonna manca, viene aggiunta
           nullable, valorizzata sulle righe esistenti con
           `LEGACY_RUN_ID` (il run che si trovava nel DB al momento della
           migrazione, `m4_demo_dropout_1d`), poi resa NOT NULL, poi la PK
           `(valve_id, cycle_id)` viene sostituita da
           `(run_id, valve_id, cycle_id)`. Se la colonna c'è già, no-op —
           nessuna riga toccata, nessun conteggio alterato.

        Infine crea (IF NOT EXISTS) i due indici di lettura: `create_all`
        non li aggiunge a una tabella già esistente.
        """
        self.metadata.create_all(self.engine, checkfirst=True)
        try:
            insp = inspect(self.engine)
            if "cycles" not in insp.get_table_names():
                return
            existing = {c["name"] for c in insp.get_columns("cycles")}
        except Exception:
            existing = set()

        with self.engine.begin() as conn:
            for col in ("event_ts", "source_ts", "ingest_ts"):
                if col not in existing:
                    conn.execute(text(
                        f"ALTER TABLE cycles ADD COLUMN IF NOT EXISTS {col} TIMESTAMPTZ"))

            if "run_id" not in existing and existing:
                # migrazione run_id su tabella già popolata
                conn.execute(text(
                    "ALTER TABLE cycles ADD COLUMN IF NOT EXISTS run_id VARCHAR"))
                conn.execute(text("UPDATE cycles SET run_id = :rid "
                                  "WHERE run_id IS NULL"),
                             {"rid": LEGACY_RUN_ID})
                conn.execute(text(
                    "ALTER TABLE cycles ALTER COLUMN run_id SET NOT NULL"))
                conn.execute(text(
                    "ALTER TABLE cycles DROP CONSTRAINT IF EXISTS "
                    "pk_cycles_valve_cycle"))
                conn.execute(text(
                    "ALTER TABLE cycles ADD CONSTRAINT pk_cycles_run_valve_cycle "
                    "PRIMARY KEY (run_id, valve_id, cycle_id)"))

            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_cycles_run_event_ts "
                "ON cycles (run_id, event_ts)"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_cycles_run_valve_cycle_desc "
                "ON cycles (run_id, valve_id, cycle_id DESC)"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_cycles_run_event_ts_cover "
                "ON cycles (run_id, event_ts) "
                "INCLUDE (valve_id, fill_quality_ok)"))

    # -- risoluzione del run ------------------------------------------------
    def known_run_ids(self) -> list[str]:
        """I `run_id` distinti presenti in `cycles` (ordinati)."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self.cycles.c.run_id).distinct()
                .order_by(self.cycles.c.run_id)).fetchall()
        return [r[0] for r in rows]

    def current_run_id(self) -> str | None:
        """Il run corrente dalla chiave KV `current_run_id` di `machine_state`.

        Stesso meccanismo di `speed_target`/`baseline_window` (JSON in
        `machine_state.value`). La tabella appartiene al layer M10 e può non
        esistere in un DB di soli `cycles`: assente → None, mai errore.
        """
        try:
            with self.engine.connect() as conn:
                row = conn.execute(
                    text("SELECT value FROM machine_state WHERE key = :k"),
                    {"k": CURRENT_RUN_ID_KEY}).first()
        except Exception:
            return None
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            value = row[0]
        return str(value) if value is not None else None

    def resolve_run_id(self, run_id: str | None) -> str | None:
        """Risolve il run da interrogare, **mai in silenzio**.

        Ordine: argomento esplicito → KV `current_run_id` → unico run
        presente in tabella. Se restano più run candidati e nessuno è stato
        indicato → `AmbiguousRunError`: senza filtro
        `DISTINCT ON (valve_id) ORDER BY cycle_id DESC` non restituisce il
        ciclo più recente ma quello del run **più lungo**, cioè una macchina
        sana mostrata al posto di quella guasta. Meglio un errore che un
        numero sbagliato.
        """
        if run_id is not None:
            return run_id
        kv = self.current_run_id()
        if kv is not None:
            return kv
        runs = self.known_run_ids()
        if len(runs) == 1:
            return runs[0]
        if not runs:
            # tabella vuota: nessuna ambiguità possibile (qualunque filtro
            # darebbe 0 righe) → None = nessun filtro, risultato vuoto.
            return None
        raise AmbiguousRunError(
            f"{len(runs)} run presenti in `cycles` ({runs}) e nessuno "
            f"selezionato: passare run_id esplicito o impostare la chiave KV "
            f"'{CURRENT_RUN_ID_KEY}' in machine_state. Senza filtro il "
            "risultato sarebbe il run più LUNGO, non il più recente")

    def drop_all(self) -> None:
        """Rimozione della tabella `cycles` (test/reset, mirror di
        `Storage.drop_all`). NON tocca le tabelle del layer M10."""
        self.metadata.drop_all(self.engine)

    def bulk_insert(self, records: list[dict[str, Any]]) -> int:
        """Inserimento bulk idempotente: ON CONFLICT (run_id, valve_id, cycle_id)
        DO NOTHING. Ritorna il numero di righe EFFETTIVAMENTE inserite
        (conteggio via RETURNING — su psycopg3/SQLAlchemy il `rowcount` di
        un INSERT ... ON CONFLICT non è un segnale affidabile; stesso
        approccio di `storage.insert_prediction`).

        Il batch viene spezzato internamente in chunk da
        `BULK_INSERT_CHUNK_ROWS` (1.000) righe, uno statement INSERT per
        chunk, così da restare sotto il limite di 65.535 bind params per
        statement del protocollo wire PostgreSQL: INSERT multi-VALUES con
        ON CONFLICT + RETURNING bypassa insertmanyvalues di SQLAlchemy, quindi
        ogni riga costa 22 params (22 colonne) ⇒ 1.000 righe = 22.000 params
        (margine sicuro, max ~2.978 righe per statement). Difesa in profondità:
        il metodo è sicuro anche per batch grandi (il chiamante di backfill
        usa lo stesso chunking).
        """
        if not records:
            return 0
        inserted = 0
        with self.engine.begin() as conn:
            for start in range(0, len(records), BULK_INSERT_CHUNK_ROWS):
                chunk = records[start:start + BULK_INSERT_CHUNK_ROWS]
                stmt = pg_insert(self.cycles).values(list(chunk))
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["run_id", "valve_id", "cycle_id"]
                ).returning(self.cycles.c.cycle_id)
                inserted += len(conn.execute(stmt).fetchall())
        return inserted

    def kpi_series(self, valve_id: int, limit: int = 200,
                   run_id: str | None = None) -> list[dict[str, Any]]:
        """Serie KPI della valvola nel run: ultimi `limit` cicli, cycle_id DESC.

        `run_id=None` → `resolve_run_id` (KV `current_run_id`, o unico run
        presente); più run e nessuno scelto → `AmbiguousRunError`, mai una
        serie che mescola due run.

        Ogni dict ha i 22 campi di `CYCLES_COLUMNS`. Valvola senza cicli →
        lista vuota.
        """
        if limit < 1:
            raise ValueError(f"limit deve essere >= 1, got {limit}")
        run = self.resolve_run_id(run_id)
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self.cycles)
                .where(self.cycles.c.valve_id == valve_id)
                .where(true() if run is None else self.cycles.c.run_id == run)
                .order_by(self.cycles.c.cycle_id.desc())
                .limit(limit)
            ).fetchall()
        return [dict(r._mapping) for r in rows]

    def latest_kpi_by_valve(self,
                            run_id: str | None = None) -> dict[int, dict[str, Any]]:
        """Ultimo KPI per valvola NEL RUN: `{valve_id: dict_22_campi}`.

        Il filtro di run è obbligatorio nella sostanza (risolto, mai
        dimenticabile): senza, `DISTINCT ON (valve_id) ORDER BY cycle_id DESC`
        restituisce il ciclo del run **più lungo**, non del più recente — con
        due run caricati la dashboard mostrerebbe una macchina sana al posto
        di quella guasta, senza alcun errore. `run_id=None` →
        `resolve_run_id`; ambiguità → `AmbiguousRunError`.

        Tabella vuota → {} (nessuna ambiguità possibile).

        PERCHE' UN LATERAL E NON `DISTINCT ON` (misurato il 2026-08-20 su
        36.241.832 righe): `DISTINCT ON (valve_id) ORDER BY valve_id,
        cycle_id DESC` produce un piano che NON sa saltare — il nodo `Unique`
        percorre tutte le righe dell'indice per tenerne 35. Misura:
        915.952 buffer letti, ~7,2 GB, **84,7 secondi**. Non e' un indice
        mancante: l'indice giusto esiste ed e' quello che il piano usa.

        Il `LATERAL` chiede invece l'ultimo ciclo di UNA valvola per volta,
        cioe' 35 discese di indice: 175 buffer, **0,7 ms**. Su un giorno solo
        di dati la differenza non si vedeva; su due mesi mandava in timeout
        `GET /valves`, che e' la route da cui dipende l'intera pagina VALVOLE.

        L'insieme delle valvole e' `1..N_VALVOLE`, il contratto operativo gia'
        imposto altrove (`valve_id fuori range 1-35`). L'alternativa
        `SELECT DISTINCT valve_id FROM cycles` sarebbe essa stessa una
        scansione totale (misurata: 25,3 s) e vanificherebbe il guadagno.
        Una valvola senza cicli non produce riga, esattamente come prima.
        """
        run = self.resolve_run_id(run_id)
        colonne = ", ".join(f"c.{c}" for c in CYCLES_COLUMNS)
        filtro_run = "" if run is None else "AND c2.run_id = :run "
        params: dict[str, Any] = {"n": N_VALVOLE}
        if run is not None:
            params["run"] = run
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT {colonne} "
                "FROM generate_series(1, :n) AS v(valve_id) "
                "CROSS JOIN LATERAL ("
                "  SELECT * FROM cycles c2 "
                f"  WHERE c2.valve_id = v.valve_id {filtro_run}"
                "  ORDER BY c2.cycle_id DESC LIMIT 1"
                ") c"), params).fetchall()
        return {dict(r._mapping)["valve_id"]: dict(r._mapping) for r in rows}


__all__ = ["CyclesStorage", "build_cycles_metadata", "CYCLES_COLUMNS",
           "AmbiguousRunError", "CURRENT_RUN_ID_KEY", "LEGACY_RUN_ID"]
