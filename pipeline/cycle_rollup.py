"""Riepilogo orario dei cicli — tabella `cycle_rollup_hour`.

Entry point CLI: ``python -m pipeline.cycle_rollup``.

## Perché esiste (misurato il 2026-08-20 sul database `plcsim`)

Il run `storico_60d` contiene **36.241.832 cicli** su 60 giorni. Le due
componenti dell'OEE che si leggono da `cycles` — Performance (`COUNT(*)`) e
Quality (`COUNT(*) FILTER (WHERE fill_quality_ok)`) — costano, aggregate
sull'intero periodo, **53,4 secondi**. È il motivo per cui
`SERIES_SPAN_MAX` in `pipeline/api.py` tagliava la serie OEE a 48 ore:
alzare il tetto senza precalcolo avrebbe prodotto una pagina da un minuto.

L'Availability NON è coinvolta: viene da `machine_state_history`, che ha 300
righe. Il costo è tutto nel conteggio dei cicli, e questo modulo lo
precalcola.

Una riga per **(run_id, bucket_ts, valve_id)**: 60 giorni × ~15,5 ore
lavorate × 35 valvole ≈ 32.000 righe al posto di 36 milioni.

## La regola che rende il riepilogo affidabile: solo ore COMPLETE

`fill()` riassume soltanto le ore **strettamente precedenti** all'ora che
contiene il ciclo più recente del run. Un secchiello nel riepilogo è quindi
sempre un'ora finita: chi legge non deve chiedersi se il numero che ha in
mano sia parziale, e non esiste alcuna colonna "completo sì/no" da tenere
allineata (una verità in più è un secondo posto dove divergere).

Il prezzo dichiarato: l'ora in corso non è mai nel riepilogo, e chi legge la
prende da `cycles` — è una lettura da ~39.000 righe, cioè millisecondi.

## Idempotenza e ripartenza

La PK `(run_id, bucket_ts, valve_id)` è anche la chiave `ON CONFLICT`, con
`DO UPDATE`: rieseguire il riempimento sullo stesso periodo riscrive gli
stessi valori. `DO UPDATE` e non `DO NOTHING` perché un'ora che fosse stata
riassunta in anticipo (o cicli arrivati in ritardo dentro un'ora già chiusa)
deve poter essere **corretta**, non ignorata in silenzio.

`--since-last` riparte dall'ultimo secchiello presente per quel run e
ricalcola quell'ora: non rilegge i 36 milioni di righe a monte.

## Contiguità — precondizione, non dettaglio

Chi legge deduce la copertura del riepilogo da `MIN(bucket_ts)` /
`MAX(bucket_ts)` del run: un'ora senza cicli non produce riga (esattamente
come non la produce il `GROUP BY` diretto su `cycles`), quindi "riga assente"
significa "zero cicli". Ne segue che un riempimento **a buchi** — riempire
giugno e agosto saltando luglio — farebbe leggere luglio come zero invece che
come "non riassunto". `fill()` scrive sempre un intervallo contiguo e
`--since-last` estende dalla coda: rispettata questa precondizione, un buco
non è producibile. Non aggirare la CLI con INSERT manuali.

## Verità nascosta

Si legge solo `cycles`, e di `cycles` solo `run_id`, `event_ts`, `valve_id`,
`fill_quality_ok` e le sei colonne operazionali di `PROFILE_METRICS`. Mai
`label`, mai `ground_truth.parquet`, mai `fault_timeline.parquet`.

CLI::

    python -m pipeline.cycle_rollup --run-id storico_60d --since-last
    python -m pipeline.cycle_rollup --run-id storico_60d --from 2026-06-21 --to 2026-07-01
    python -m pipeline.cycle_rollup --run-id storico_60d --status

Exit codes: 0 = ok; 2 = run inesistente in `cycles` o intervallo vuoto.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    text,
)
from sqlalchemy.engine import Engine

from pipeline.storage import make_engine

logger = logging.getLogger("pipeline.cycle_rollup")

DATABASE_URL_DEFAULT = "postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim"

ROLLUP_TABLE = "cycle_rollup_hour"

# Grana del riepilogo. È una decisione, non una costante di comodo: l'ora è la
# più grossa che lascia esatte anche le finestre `hour` dell'OEE (una finestra
# di un'ora disallineata tocca due secchielli e due bordi parziali, mai di più).
BUCKET = timedelta(hours=1)

# Le grandezze del ciclo che il riepilogo somma, oltre a `total`/`good`.
#
# Perche' proprio queste sei: sono le colonne che disegnano il **profilo del
# ciclo medio** (riempimento + coda) nel pannello della valvola. Misurato sul
# run `storico_60d`, quattro guasti diversi producono quattro firme distinte su
# questa terna (`filling_time_ms`, `tail_time_ms`, `tail_pulse`), e
# `pulse_count`/`delta_pulse`/`filling_step_out` completano la forma. Senza
# riepilogo la media su 60 giorni si paga con una scansione da ~53 secondi.
#
# Ogni grandezza porta con se' il PROPRIO conteggio (`n_...`) e non riusa
# `total`: le colonne KPI di `cycles` sono nullable (cicli parziali, policy T6),
# quindi il numero di valori misurati e' `COUNT(colonna)` — le righe con NULL
# non entrano. Dividere una somma per `total` darebbe una media piu' bassa del
# vero ogni volta che nell'ora esiste anche un solo ciclo parziale.
PROFILE_METRICS: tuple[str, ...] = (
    "filling_time_ms",
    "tail_time_ms",
    "tail_pulse",
    "pulse_count",
    "delta_pulse",
    "filling_step_out",
)


def sum_col(metric: str) -> str:
    """Nome della colonna somma nel riepilogo."""
    return f"sum_{metric}"


def n_col(metric: str) -> str:
    """Nome della colonna conteggio (valori NON nulli) nel riepilogo."""
    return f"n_{metric}"

# `fill()` aggrega un pezzo di periodo per volta: un GROUP BY su 60 giorni
# interi ordina ~36 milioni di righe e sfonda `work_mem`. Un giorno alla volta
# sono ~600.000 righe per statement, che stanno in memoria e rendono il
# progresso ispezionabile (una riga di log per pezzo).
FILL_CHUNK = timedelta(days=1)

# `work_mem` di default sul Postgres di sviluppo è basso e l'aggregazione va
# su disco. Impostato per sessione (SET LOCAL non serve: è una connessione
# usa-e-getta), mai a livello di cluster: nessuna configurazione globale
# cambiata da qui.
WORK_MEM = "256MB"


def build_cycle_rollup_metadata() -> MetaData:
    """MetaData singola-tabella `cycle_rollup_hour` (standalone, come `cycles`).

    Stesso motivo per cui `build_cycles_metadata()` è standalone: il riepilogo
    vive nello stesso database ma con ciclo di vita proprio (init/drop
    separati), così la dashboard non dipende da migrazioni dello schema
    operazionale M10.

    PK composita **(run_id, bucket_ts, valve_id)**:

    - `run_id` in testa perché due run si sovrappongono nel tempo di parete
      (`cycles_storage.build_cycles_metadata` documenta il caso): senza,
      sommare i secchielli di un'ora mescolerebbe due macchine diverse.
    - `bucket_ts` è l'inizio dell'ora **in UTC**, non "l'ora del server":
      `date_trunc('hour', ...)` su un `timestamptz` tronca nel fuso della
      sessione, e su un fuso a mezz'ora (+05:30) produrrebbe secchielli
      sfasati di 30 minuti rispetto a quelli che il lettore ricostruisce in
      Python. La query di riempimento tronca quindi esplicitamente
      `AT TIME ZONE 'UTC'`.
    - `valve_id` perché l'OEE serve la qualità anche disaggregata per valvola
      (`_worst_quality_valve`, `per_valve=true`): un riepilogo di sola
      macchina avrebbe costretto a rileggere `cycles` per quella richiesta.

    La PK è anche la chiave `ON CONFLICT` di `fill()` (`DO UPDATE`): vedi il
    docstring del modulo.

    Colonne di misura: `total` e `good` — esattamente le due che
    `_CycleCounts.window()` produce da `cycles` — più, per ognuna delle
    `PROFILE_METRICS`, la coppia `sum_<m>` / `n_<m>`. Nient'altro: `quality`
    non c'è perché è `good/total`, e nessuna media è memorizzata, perché una
    media non si somma. Sono le somme e i conteggi a essere additivi, e la
    media si ricava a valle come `sum/n`: così un periodo qualsiasi resta
    esatto invece di essere la media di medie di ore con peso diverso.

    `n_<m>` è `COUNT(<m>)`, cioè il numero di righe con valore **non nullo**, e
    non `total`: vedi il commento su `PROFILE_METRICS`.

    Le colonne di profilo sono **nullable** per una ragione di migrazione: su
    una tabella già popolata `ADD COLUMN` non può riempire il passato, quindi
    `NULL` significa "ora riassunta prima della migrazione, da ricalcolare" —
    che è un fatto diverso da "zero valori misurati" (`n = 0`). Chi legge deve
    distinguerli e dichiararsi degradato sul primo, mai servirlo come zero.

    `BigInteger` per i contatori: `total` per (ora, valvola) sta in un `int`,
    ma la stessa colonna viene sommata su 60 giorni dal lettore e il tipo della
    somma deve reggere senza che nessuno debba ricordarsene.

    Indice `ix_rollup_run_bucket`: la lettura tipica è "tutti i secchielli di
    un run in un intervallo", cioè una scansione di range su (run_id,
    bucket_ts). La PK ha le stesse colonne in testa e servirebbe, ma
    l'ordinamento per valvola in coda la rende meno selettiva per questa
    forma; l'indice dedicato costa poco su ~32.000 righe.
    """
    m = MetaData()
    Table(
        ROLLUP_TABLE, m,
        Column("run_id", String, nullable=False),
        Column("bucket_ts", DateTime(timezone=True), nullable=False),
        Column("valve_id", Integer, nullable=False),
        Column("total", BigInteger, nullable=False),
        Column("good", BigInteger, nullable=False),
        # somma e conteggio per ogni grandezza del profilo, nell'ordine di
        # PROFILE_METRICS. Nullable: vedi il docstring (migrazione).
        *(c for m in PROFILE_METRICS for c in (
            Column(sum_col(m), BigInteger, nullable=True),
            Column(n_col(m), BigInteger, nullable=True))),
        PrimaryKeyConstraint("run_id", "bucket_ts", "valve_id",
                             name="pk_cycle_rollup_hour"),
        Index("ix_rollup_run_bucket", "run_id", "bucket_ts"),
    )
    return m


def floor_hour(t: datetime) -> datetime:
    """Inizio dell'ora UTC che contiene `t` (il `bucket_ts` di `t`)."""
    return t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def ceil_hour(t: datetime) -> datetime:
    """Primo bordo d'ora UTC **non precedente** a `t`; `t` allineata → `t`.

    Serve al lettore per separare le ore intere dai due bordi parziali; il caso
    "già allineata" deve restare fermo, altrimenti un'ora intera verrebbe
    contata due volte.
    """
    f = floor_hour(t)
    return f if f == t else f + BUCKET


class CycleRollupError(Exception):
    """Errore di dominio (messaggio già operativo)."""


class CycleRollup:
    """Accesso a `cycle_rollup_hour`: riempimento e lettura dei secchielli."""

    def __init__(self, engine: Engine | None = None, url: str | None = None):
        self.engine = engine or make_engine(url)
        self.metadata = build_cycle_rollup_metadata()
        self.table = self.metadata.tables[ROLLUP_TABLE]

    def init(self) -> None:
        """Crea la tabella if-not-exists e migra. Idempotente. Non tocca `cycles`."""
        self.metadata.create_all(self.engine, checkfirst=True)
        self.migrate()

    def migrate(self) -> list[str]:
        """Aggiunge le colonne di profilo mancanti. Ritorna quelle aggiunte.

        Serve perche' `create_all(checkfirst=True)` vede la tabella e non fa
        nulla: su un'installazione dove `cycle_rollup_hour` esiste gia' — quella
        vera ha 34.090 righe — le colonne nuove non comparirebbero mai.

        `ADD COLUMN IF NOT EXISTS` e' un no-op sulla colonna gia' presente,
        quindi rieseguire la migrazione non e' distinguibile dall'eseguirla una
        volta sola. Su Postgres l'aggiunta di una colonna nullable senza default
        e' solo un cambio di catalogo: non riscrive le 34.090 righe e non prende
        un lock lungo.

        Le righe preesistenti restano a `NULL`: la migrazione dichiara la forma,
        e' `fill()` che calcola i valori. Il riempimento e' `ON CONFLICT DO
        UPDATE`, quindi ripassare sulle stesse ore le completa senza duplicare.

        **Come si torna indietro**: le colonne sono additive e nessuna
        lettura preesistente le nomina, quindi il rollback e' scartarle —

            ALTER TABLE cycle_rollup_hour
              DROP COLUMN IF EXISTS sum_filling_time_ms,
              DROP COLUMN IF EXISTS n_filling_time_ms, ...  (le 12 colonne)

        oppure semplicemente lasciarle: `total` e `good` non cambiano di un
        valore, e il codice vecchio non le legge. Nessun dato preesistente viene
        riscritto in nessuno dei due casi.
        """
        aggiunte: list[str] = []
        with self.engine.begin() as conn:
            presenti = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"), {"t": ROLLUP_TABLE}).all()}
            for m in PROFILE_METRICS:
                for nome in (sum_col(m), n_col(m)):
                    if nome in presenti:
                        continue
                    # nome generato da PROFILE_METRICS, mai input esterno.
                    conn.execute(text(
                        f"ALTER TABLE {ROLLUP_TABLE} "
                        f"ADD COLUMN IF NOT EXISTS {nome} BIGINT"))
                    aggiunte.append(nome)
        if aggiunte:
            logger.info("migrazione %s: aggiunte %s", ROLLUP_TABLE,
                        ", ".join(aggiunte))
        return aggiunte

    def drop_all(self) -> None:
        """Rimuove solo `cycle_rollup_hour` (test/reset)."""
        self.metadata.drop_all(self.engine, checkfirst=True)

    # -- stato ------------------------------------------------------------
    def coverage(self, run_id: str) -> tuple[datetime | None, datetime | None]:
        """`(primo_bucket, ultimo_bucket)` presenti per il run, o `(None, None)`.

        È la copertura **dichiarata al lettore**: le ore riassunte sono
        `[primo, ultimo + 1h)`. Un'ora vuota dentro l'intervallo non ha righe e
        vale zero — vedi la precondizione di contiguità nel docstring del
        modulo.
        """
        with self.engine.connect() as conn:
            row = conn.execute(text(
                f"SELECT MIN(bucket_ts), MAX(bucket_ts) FROM {ROLLUP_TABLE} "
                "WHERE run_id = :run"), {"run": run_id}).first()
        if row is None or row[0] is None:
            return None, None
        return _utc(row[0]), _utc(row[1])

    def cycles_extent(self, run_id: str) -> tuple[datetime | None, datetime | None]:
        """`(primo_ciclo, ultimo_ciclo)` del run in `cycles` (due letture di indice)."""
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT MIN(event_ts), MAX(event_ts) FROM cycles "
                "WHERE run_id = :run"), {"run": run_id}).first()
        if row is None or row[0] is None:
            return None, None
        return _utc(row[0]), _utc(row[1])

    def rows_for(self, run_id: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(
                f"SELECT COUNT(*) FROM {ROLLUP_TABLE} WHERE run_id = :run"),
                {"run": run_id}).scalar_one())

    # -- riempimento -------------------------------------------------------
    def fill(self, run_id: str, start: datetime | None = None,
             end: datetime | None = None,
             since_last: bool = False) -> dict[str, Any]:
        """Riassume le ore complete del run in `[start, end)`.

        `start`/`end` vengono allineati verso l'esterno all'ora (`floor` a
        sinistra, `ceil` a destra): un secchiello o si riempie tutto o non si
        riempie, mai a metà.

        `end` viene poi **tagliato** a `floor_hour(ultimo_ciclo)`, cioè
        all'inizio dell'ora in corso: solo ore complete entrano (vedi il
        docstring del modulo). `start` di default è `floor_hour(primo_ciclo)`.

        `since_last=True` fa ripartire `start` dall'ultimo secchiello presente
        — quell'ora viene **ricalcolata** perché è l'unica che poteva essere
        stata scritta quando era ancora incompleta, e `DO UPDATE` la corregge.

        Ritorna un riassunto ispezionabile: intervallo effettivo, pezzi,
        righe scritte, ore saltate.
        """
        primo, ultimo = self.cycles_extent(run_id)
        if primo is None:
            raise CycleRollupError(
                f"run {run_id!r}: nessun ciclo con event_ts in `cycles` "
                "(niente da riassumere)")
        limite = floor_hour(ultimo)

        if since_last:
            _, ultimo_bucket = self.coverage(run_id)
            if ultimo_bucket is not None:
                start = ultimo_bucket
        lo = floor_hour(start) if start is not None else floor_hour(primo)
        hi = ceil_hour(end) if end is not None else limite
        hi = min(hi, limite)
        if hi <= lo:
            return {"run_id": run_id, "from": _iso(lo), "to": _iso(lo),
                    "chunks": 0, "rows": 0,
                    "nota": f"nulla da riassumere: l'ora in corso "
                            f"({_iso(limite)}) è il limite delle ore complete"}

        scritte = 0
        pezzi = 0
        with self.engine.begin() as conn:
            conn.execute(text(f"SET work_mem = '{WORK_MEM}'"))
            t = lo
            while t < hi:
                t2 = min(t + FILL_CHUNK, hi)
                n = conn.execute(text(
                    f"INSERT INTO {ROLLUP_TABLE} "
                    "  (run_id, bucket_ts, valve_id, total, good"
                    + "".join(f", {sum_col(m)}, {n_col(m)}"
                              for m in PROFILE_METRICS) + ") "
                    "SELECT run_id, "
                    # troncamento esplicito in UTC: vedi
                    # build_cycle_rollup_metadata (fusi a mezz'ora)
                    "  (date_trunc('hour', event_ts AT TIME ZONE 'UTC') "
                    "     AT TIME ZONE 'UTC') AS bucket_ts, "
                    "  valve_id, COUNT(*), "
                    "  COUNT(*) FILTER (WHERE fill_quality_ok = TRUE)"
                    # SUM(col) ignora i NULL e COUNT(col) li esclude: le due
                    # cose combaciano per costruzione, e la media sum/n e'
                    # quindi la media dei soli cicli che hanno misurato quella
                    # grandezza. SUM su zero righe non nulle da' NULL — e li'
                    # n vale 0, quindi il lettore non ci divide mai.
                    + "".join(f", SUM({m}), COUNT({m})" for m in PROFILE_METRICS)
                    + " FROM cycles "
                    "WHERE run_id = :run AND event_ts >= :lo AND event_ts < :hi "
                    "GROUP BY run_id, bucket_ts, valve_id "
                    "ON CONFLICT (run_id, bucket_ts, valve_id) DO UPDATE SET "
                    "  total = EXCLUDED.total, good = EXCLUDED.good"
                    + "".join(f", {sum_col(m)} = EXCLUDED.{sum_col(m)}"
                              f", {n_col(m)} = EXCLUDED.{n_col(m)}"
                              for m in PROFILE_METRICS)),
                    {"run": run_id, "lo": t, "hi": t2}).rowcount
                scritte += max(n, 0)
                pezzi += 1
                logger.info("%s -> %s: %d righe", _iso(t), _iso(t2), max(n, 0))
                t = t2
        return {"run_id": run_id, "from": _iso(lo), "to": _iso(hi),
                "chunks": pezzi, "rows": scritte, "nota": None}

    # -- lettura -----------------------------------------------------------
    def buckets(self, run_id: str, lo: datetime,
                hi: datetime) -> dict[int, dict[datetime, tuple[int, int]]]:
        """Secchielli `[lo, hi)` come `{valve_id: {bucket_ts: (total, good)}}`.

        Una sola lettura per tutta l'ampiezza richiesta: è il pezzo che
        sostituisce la scansione di `cycles`.
        """
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT valve_id, bucket_ts, total, good FROM {ROLLUP_TABLE} "
                "WHERE run_id = :run AND bucket_ts >= :lo AND bucket_ts < :hi"),
                {"run": run_id, "lo": lo, "hi": hi}).all()
        acc: dict[int, dict[datetime, tuple[int, int]]] = {}
        for valve_id, bucket_ts, total, good in rows:
            acc.setdefault(int(valve_id), {})[_utc(bucket_ts)] = (int(total), int(good))
        return acc


def _utc(v: datetime) -> datetime:
    return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.cycle_rollup",
        description="Riempie il riepilogo orario dei cicli (cycle_rollup_hour).")
    ap.add_argument("--run-id", required=True, help="run di `cycles` da riassumere")
    ap.add_argument("--db-url", default=DATABASE_URL_DEFAULT)
    ap.add_argument("--from", dest="da", default=None,
                    help="inizio ISO8601 (default: primo ciclo del run)")
    ap.add_argument("--to", dest="a", default=None,
                    help="fine ISO8601 esclusa (default: inizio dell'ora in corso)")
    ap.add_argument("--since-last", action="store_true",
                    help="riparte dall'ultimo secchiello presente e lo ricalcola")
    ap.add_argument("--status", action="store_true",
                    help="stampa copertura e righe senza scrivere nulla")
    return ap


def _parse_ts(s: str | None) -> datetime | None:
    if s is None:
        return None
    v = datetime.fromisoformat(s)
    return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_arg_parser().parse_args(argv)
    r = CycleRollup(url=args.db_url)
    r.init()
    if args.status:
        primo, ultimo = r.coverage(args.run_id)
        c0, c1 = r.cycles_extent(args.run_id)
        print(f"run              {args.run_id}")
        print(f"cicli            {_iso(c0)} -> {_iso(c1)}")
        print(f"riepilogo        {_iso(primo)} -> {_iso(ultimo)}")
        print(f"righe            {r.rows_for(args.run_id)}")
        return 0
    try:
        s = r.fill(args.run_id, _parse_ts(args.da), _parse_ts(args.a),
                   since_last=args.since_last)
    except CycleRollupError as exc:
        logger.error("%s", exc)
        return 2
    if s["nota"]:
        print(s["nota"])
    print(f"riassunte le ore {s['from']} -> {s['to']} "
          f"({s['chunks']} pezzi, {s['rows']} righe scritte)")
    return 0


__all__ = ["CycleRollup", "CycleRollupError", "build_cycle_rollup_metadata",
           "ROLLUP_TABLE", "BUCKET", "floor_hour", "ceil_hour",
           "PROFILE_METRICS", "sum_col", "n_col"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
