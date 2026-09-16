"""Verdetto precalcolato ora per ora — tabella `decision_rollup_hour`.

Entry point CLI: ``python -m pipeline.decision_rollup``.

## Perche' esiste (misurato il 2026-09-16 sul database `plcsim`)

`GET /valves/decision` calcolava il verdetto a ogni richiesta, e il costo
cresceva con la posizione nella corsa: 0,05 s al 5% della corsa, 1,14 s a
meta', **4,6 s all'ultima ora** del run `storico_60d`. La lettura da Postgres
e' 0,21 s ed e' costante, quindi il tempo era tutto calcolo: il 95% dentro
`decision.stima_crollo`, con `decision.t_cdf` chiamata 309.356 volte per
richiesta. L'utente ha aperto la pagina e ha detto che e' lenta, ed e' un
difetto di correttezza: un comando che risponde dopo secondi si legge come
rotto.

Il fatto che rende il precalcolo quasi gratis: `decision.linea()`, che
percorre **tutte e 1407 le ore** di tutte e 35 le valvole, costa quanto una
sola richiesta all'ultima ora. Una passata sulla corsa calcola ogni casella al
prezzo di una. Prima di questo modulo quel lavoro veniva buttato via a ogni
richiesta.

Una riga per **(run_id, ora_ts, valve_id)**: 1407 ore x 35 valvole = 49.245
righe per corsa, e le due rotte leggono invece di ricalcolare.

## Che cosa c'e' in una riga, e perche' proprio quello

Una riga deve bastare a ricostruire la risposta del contratto senza rifare un
conto. Da `decision.camminata()` arrivano `azione`, `di_fila`, `chiamata_a`,
`arrivo`, `sostituita`, `D`, `R`, `p1`, `delta_p`, `tasso_scarti`, `q_es`,
`motivo`. Due campi in piu' li costruisce `decision.percorso()` e non la
camminata:

- `eser`, cioe' se l'ora sta fra quelle di esercizio della valvola.
- `mostra`, l'azione da mostrare: fuori esercizio ripete l'ultima azione vista
  **in** esercizio. E' una colonna e non un conto in lettura perche' si
  trascina dall'inizio della corsa, e una finestra di 24 righe non basta a
  ricavarla.

Il blocco `stima` e' un JSONB per intero: ha forma diversa nei sei esiti del
contratto e appiattirlo in colonne vorrebbe dire una colonna nullable per
ogni caso.

`storia_24h` **non** e' una colonna. `percorso()` prende le ultime 24 **righe
della griglia** fino al bersaglio, quindi in lettura si prende
`ORDER BY ora_ts DESC LIMIT 24` sulla stessa valvola. Sono righe, non ore: la
griglia puo' non essere contigua nel tempo, e un `WHERE ora_ts > bersaglio -
24h` darebbe una storia piu' corta ogni volta che la corsa ha un buco.

`parametri()` non e' in tabella: e' una funzione pura di costanti e si calcola
in lettura, dove costa nulla. Una costante messa in tabella e' solo un secondo
posto da cui puo' divergere.

## Idempotenza e ripartenza

La PK `(run_id, ora_ts, valve_id)` e' anche la chiave `ON CONFLICT`, con
`DO UPDATE`: rieseguire il riempimento sullo stesso run riscrive gli stessi
valori invece di duplicarli. `DO UPDATE` e non `DO NOTHING` perche' se il
riepilogo dei cicli si allunga, o se una costante della politica cambia, le
ore gia' scritte devono poter essere **corrette**, non ignorate in silenzio.

## Contiguita' — precondizione, non dettaglio

Non esiste `--from` / `--to`: il riempimento copre sempre la corsa intera di
quel run. La copertura si deduce da `MIN(ora_ts)` / `MAX(ora_ts)`, e un
riempimento a buchi farebbe leggere un buco come «zero intervieni, zero
degradate», cioe' come «tutto calmo». Su questa pagina e' il difetto peggiore
possibile: la pagina mente proprio nel punto in cui non sa. Il riempimento e'
per corsa intera anche perche' non costa nulla farlo cosi': la camminata di
`decision.camminata()` deve comunque partire dall'inizio della corsa, visto
che gli «intervieni» di fila e l'arrivo della squadra dipendono da tutto cio'
che e' successo prima.

## Freschezza

Due condizioni, e servono tutte e due.

**La corsa non si e' allungata.** `MAX(ora_ts)` deve valere l'ultimo
secchiello di `cycle_rollup_hour` per quel run. La griglia di
`decision.griglia()` finisce sempre li', quindi se i due non coincidono il
riepilogo dei cicli si e' allungato dopo il precalcolo.

**La politica non e' cambiata.** La colonna `impronta` porta, riga per riga,
lo sha256 di `decision.impronta_parametri(finestra)`: le costanti della
politica piu' gli estremi della finestra sana. Serve perche' il controllo
sulla copertura, da solo, vedeva allungarsi la corsa e non vedeva
nient'altro — cambiare `PREZZO_VALVOLA_EUR` o la finestra del KV
`baseline_window` lasciava la tabella formalmente fresca e faceva servire i
numeri vecchi in silenzio. `fresco()` pretende che le impronte distinte del
run siano esattamente `{impronta corrente}`: un run con impronte miste, cioe'
un riempimento interrotto a meta', non e' fresco.

Le rotte controllano, e quando non e' fresco ricalcolano come prima: una
risposta lenta e' un difetto, una risposta vecchia e' una bugia.

## Verita' nascosta

Si legge solo cio' che legge la rotta: `cycle_rollup_hour` attraverso
`api._serie_per_decisione`, e il KV `baseline_window`. Mai `label`, mai
`ground_truth.parquet`, mai `fault_timeline.parquet`. La regola della
camminata non e' riscritta qui: questo modulo **consuma**
`decision.camminata()` esattamente come fanno `percorso()` e `linea()`.

CLI::

    python -m pipeline.decision_rollup --run-id storico_60d
    python -m pipeline.decision_rollup --run-id storico_60d --status

Exit codes: 0 = ok; 2 = run inesistente, senza cicli riassunti, senza valvole
attive o senza finestra sana dichiarata.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from pipeline import decision
from pipeline.cycle_rollup import DATABASE_URL_DEFAULT
from pipeline.storage import make_engine

logger = logging.getLogger("pipeline.decision_rollup")

DECISION_TABLE = "decision_rollup_hour"

ORA = timedelta(hours=1)

# Quante righe per statement. 49.245 righe in un solo `executemany` costruisce
# una lista di parametri da decine di MB e non lascia vedere il progresso; un
# pezzo da 2.000 sta in memoria e stampa una riga di log ogni pezzo.
PEZZO = 2000

# L'ordine dei campi del blocco `stima` come lo costruisce
# `decision.stima_piena`. JSONB non conserva l'ordine delle chiavi, quindi in
# lettura va rimesso: senza, la risposta della rotta avrebbe gli stessi dati in
# ordine diverso a seconda che li abbia calcolati o letti, e due risposte che
# differiscono per l'ordine sono due risposte da spiegare.
ORDINE_STIMA = (
    "esito", "segnale", "ore_di_osservazione", "n", "nota",
    "crollo_lo", "crollo_hat", "crollo_hi",
    "ore_a_crollo_lo", "ore_a_crollo_hat", "ore_a_crollo_hi",
)


def build_decision_rollup_metadata() -> MetaData:
    """MetaData singola-tabella `decision_rollup_hour`, come `cycle_rollup_hour`.

    Sta da sola per lo stesso motivo: vive nello stesso database ma con ciclo
    di vita proprio, cosi' la dashboard non dipende da migrazioni dello schema
    operazionale.

    PK composita **(run_id, ora_ts, valve_id)**:

    - `run_id` in testa perche' due corse si sovrappongono nel tempo di parete,
      e una chiamata della squadra di una corsa non deve comparire nell'altra.
    - `ora_ts` e' l'ora della griglia **in UTC**, cioe' l'istante che
      `decision.griglia()` chiama `adesso`.
    - `valve_id` perche' la risposta e' per valvola: il conteggio di macchina
      e' la somma delle 35 righe di quell'ora, non un numero a parte.

    `d_eur` e `r_eur` si chiamano cosi' e non `D`/`R` perche' Postgres
    ripiega gli identificatori non quotati in minuscolo, e due colonne che
    differiscono solo per il caso sarebbero la stessa colonna.

    I numeri sono `Float`, cioe' `double precision`, e si arrotondano in
    lettura come fa `decision.decisione()`: arrotondare in scrittura
    congelerebbe nella tabella una scelta di presentazione, e cambiarla in
    futuro vorrebbe dire ricalcolare 49.245 righe invece di cambiare una riga
    di codice.

    Indice `ix_decisione_run_ora`: le due letture tipiche sono «tutte le
    valvole a un'ora» e «tutte le ore di una valvola fino a un'ora». La PK
    serve la prima; per la seconda serve la testa `(run_id, valve_id)`, e
    l'indice dedicato la copre. Costa poco su ~50.000 righe.
    """
    m = MetaData()
    Table(
        DECISION_TABLE, m,
        Column("run_id", String, nullable=False),
        Column("ora_ts", DateTime(timezone=True), nullable=False),
        Column("valve_id", Integer, nullable=False),
        Column("azione", String, nullable=False),
        Column("mostra", String, nullable=False),
        Column("di_fila", Integer, nullable=False),
        Column("chiamata_a", DateTime(timezone=True), nullable=True),
        Column("arrivo", DateTime(timezone=True), nullable=True),
        Column("sostituita", Boolean, nullable=False),
        Column("eser", Boolean, nullable=False),
        Column("d_eur", Float, nullable=False),
        Column("r_eur", Float, nullable=False),
        Column("p1", Float, nullable=False),
        Column("delta_p", Float, nullable=False),
        Column("tasso_scarti", Float, nullable=False),
        Column("q_es", Float, nullable=False),
        Column("motivo", Text, nullable=False),
        Column("stima", JSONB, nullable=False),
        Column("impronta", String, nullable=False),
        PrimaryKeyConstraint("run_id", "ora_ts", "valve_id",
                             name="pk_decision_rollup_hour"),
        Index("ix_decisione_run_valvola", "run_id", "valve_id", "ora_ts"),
    )
    return m


class DecisionRollupError(Exception):
    """Errore di dominio (messaggio gia' operativo)."""


def _utc(v: datetime) -> datetime:
    return (v.replace(tzinfo=timezone.utc) if v.tzinfo is None
            else v.astimezone(timezone.utc))


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


def _z(v: datetime | None) -> str | None:
    """L'istante in forma `2026-07-04T07:00:00Z`, come dice il contratto."""
    return None if v is None else _utc(v).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stima_ordinata(d: dict[str, Any]) -> dict[str, Any]:
    """Il blocco `stima` riletto da JSONB, con l'ordine di `stima_piena`."""
    fuori = {k: d[k] for k in ORDINE_STIMA if k in d}
    # una chiave non prevista non si perde: meglio in coda che sparita.
    fuori.update({k: v for k, v in d.items() if k not in fuori})
    return fuori


# --------------------------------------------------------------------------
#  gli ingredienti del calcolo, gli stessi che usa la rotta
# --------------------------------------------------------------------------

def ingredienti(engine: Engine, run_id: str) -> tuple[dict, dict, datetime]:
    """`(serie, finestra, ultima_ora)` per il run, come li prende la rotta.

    L'import di `pipeline.api` sta **dentro** la funzione e non in testa al
    modulo: le rotte importano questo modulo, quindi un import in testa
    chiuderebbe un cerchio. Passa da qui e non da una query propria perche' la
    serie che il precalcolo macina deve essere la stessa, secchiello per
    secchiello, che la rotta macinerebbe: due prelievi diversi darebbero due
    verdetti diversi sulla stessa ora.
    """
    from pipeline import api  # noqa: PLC0415  (vedi docstring)
    from pipeline.storage import Storage  # noqa: PLC0415

    st = Storage(engine)
    cov_lo, cov_hi, _prima = api._copertura_riepilogo(st, run_id)
    if cov_lo is None or cov_hi is None:
        raise DecisionRollupError(
            f"run {run_id!r}: nessuna ora riassunta in cycle_rollup_hour. "
            f"Riempire prima il riepilogo dei cicli con "
            f"`python -m pipeline.cycle_rollup --run-id {run_id} --since-last`")
    serie = api._serie_per_decisione(st, run_id, cov_lo, cov_hi + ORA)
    if not serie:
        raise DecisionRollupError(
            f"run {run_id!r}: nessuna valvola attiva, nessun secchiello con "
            f"almeno {decision.MIN_TOT} cicli")
    fin_start, fin_end, _src, _kv = api._baseline_window(st, None, None)
    if fin_start is None or fin_end is None:
        raise DecisionRollupError(
            "nessuna finestra sana dichiarata: senza riferimento sano il "
            "segnale non e' definibile. Persistere il KV `baseline_window` "
            "{run_id, start, end}")
    finestra = {"start": api._iso(fin_start), "end": api._iso(fin_end)}
    return serie, finestra, cov_hi


def righe_della_corsa(serie: dict[int, list[dict]], finestra: dict,
                      run_id: str, fine: str):
    """Le righe da scrivere, una per (ora, valvola), in una passata sola.

    E' la stessa camminata di `decision.percorso()` e `decision.linea()`, letta
    fino in fondo invece che fino a un bersaglio: `mostra` si trascina di ora
    in ora con la stessa regola di `percorso()`, e il blocco `stima` esce da
    `decision.stima_piena()`, che dopo il riuso della scansione non ricalcola
    piu' niente.
    """
    impronta = decision.impronta_parametri(finestra)
    for v in sorted(serie):
        f = decision.fatti_valvola(serie[v], finestra, fine)
        ultima_esercizio = None
        for passo in decision.camminata(f):
            ve = passo["v"]
            eser = passo["t"] in f["esercizio"]
            if eser:
                ultima_esercizio = ve["azione"]
            yield {
                "run": run_id,
                "ora": passo["t"],
                "valvola": v,
                "azione": ve["azione"],
                "mostra": (ve["azione"] if eser
                           else (ultima_esercizio or ve["azione"])),
                "di_fila": passo["di_fila"],
                "chiamata_a": (decision.ts(passo["chiamata_a"])
                               if passo["chiamata_a"] else None),
                "arrivo": (decision.ts(passo["arrivo"])
                           if passo["arrivo"] else None),
                "sostituita": bool(passo["sostituita"]),
                "eser": bool(eser),
                "d_eur": float(ve["D"]),
                "r_eur": float(ve["R"]),
                "p1": float(ve["p1"]),
                "delta_p": float(ve["delta_p"]),
                "tasso_scarti": float(ve["tasso_scarti"]),
                "q_es": float(ve["q_es"]),
                "motivo": ve["motivo"],
                "stima": json.dumps(
                    decision.stima_piena(f, ve["adesso"], passo["sostituita"]),
                    ensure_ascii=False),
                "impronta": impronta,
            }


# --------------------------------------------------------------------------
#  la tabella
# --------------------------------------------------------------------------

_COLONNE = ("run_id", "ora_ts", "valve_id", "azione", "mostra", "di_fila",
            "chiamata_a", "arrivo", "sostituita", "eser", "d_eur", "r_eur",
            "p1", "delta_p", "tasso_scarti", "q_es", "motivo", "stima",
            "impronta")

_PARAM = {"run_id": ":run", "ora_ts": ":ora", "valve_id": ":valvola",
          "stima": "CAST(:stima AS JSONB)"}


def _sql_insert() -> str:
    valori = ", ".join(_PARAM.get(c, f":{c}") for c in _COLONNE)
    agg = ", ".join(f"{c} = EXCLUDED.{c}" for c in _COLONNE[3:])
    return (f"INSERT INTO {DECISION_TABLE} ({', '.join(_COLONNE)}) "
            f"VALUES ({valori}) "
            f"ON CONFLICT (run_id, ora_ts, valve_id) DO UPDATE SET {agg}")


class DecisionRollup:
    """Accesso a `decision_rollup_hour`: riempimento e lettura del verdetto."""

    def __init__(self, engine: Engine | None = None, url: str | None = None):
        self.engine = engine or make_engine(url)
        self.metadata = build_decision_rollup_metadata()
        self.table = self.metadata.tables[DECISION_TABLE]

    def init(self) -> None:
        """Crea la tabella if-not-exists. Idempotente. Non tocca altre tabelle."""
        self.metadata.create_all(self.engine, checkfirst=True)

    def drop_all(self) -> None:
        """Rimuove solo `decision_rollup_hour` (test/reset)."""
        self.metadata.drop_all(self.engine, checkfirst=True)

    # -- stato ------------------------------------------------------------
    def coverage(self, run_id: str) -> tuple[datetime | None, datetime | None]:
        """`(prima_ora, ultima_ora)` precalcolate per il run, o `(None, None)`.

        E' la copertura dichiarata al lettore, e per la precondizione di
        contiguita' l'intervallo e' pieno: ogni ora fra i due estremi ha le sue
        righe.
        """
        with self.engine.connect() as conn:
            row = conn.execute(text(
                f"SELECT MIN(ora_ts), MAX(ora_ts) FROM {DECISION_TABLE} "
                "WHERE run_id = :run"), {"run": run_id}).first()
        if row is None or row[0] is None:
            return None, None
        return _utc(row[0]), _utc(row[1])

    def rows_for(self, run_id: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(
                f"SELECT COUNT(*) FROM {DECISION_TABLE} WHERE run_id = :run"),
                {"run": run_id}).scalar_one())

    def impronte(self, run_id: str) -> set[str]:
        """Le impronte distinte scritte per il run. Vuoto = run non precalcolato."""
        with self.engine.connect() as conn:
            return {str(r[0]) for r in conn.execute(text(
                f"SELECT DISTINCT impronta FROM {DECISION_TABLE} "
                "WHERE run_id = :run"), {"run": run_id}).all()}

    def fresco(self, run_id: str, cov_hi: datetime, finestra: dict) -> bool:
        """Il precalcolo e' ancora buono? Due domande, non una.

        **Copertura.** `decision.griglia()` finisce sempre all'ultimo
        secchiello disponibile, quindi `MAX(ora_ts)` diverso da `cov_hi`
        significa che il riepilogo dei cicli si e' allungato dopo il
        precalcolo.

        **Impronta.** La copertura da sola vedeva allungarsi la corsa e non
        vedeva nient'altro: una costante della politica o la finestra sana
        potevano cambiare e la tabella restava formalmente fresca, servendo i
        numeri vecchi in silenzio. Qui si pretende che l'insieme delle impronte
        distinte del run sia **esattamente** `{impronta corrente}`. Un run con
        impronte miste — un riempimento interrotto a meta' — non e' fresco:
        sarebbe una risposta cucita con due politiche diverse.

        Chi legge e non e' fresco ricalcola: una risposta lenta e' un difetto,
        una vecchia e' una bugia.
        """
        _lo, hi = self.coverage(run_id)
        if hi is None or hi != _utc(cov_hi):
            return False
        return self.impronte(run_id) == {decision.impronta_parametri(finestra)}

    def migrate(self) -> list[str]:
        """Aggiunge la colonna `impronta` a una tabella gia' esistente.

        Serve perche' `create_all(checkfirst=True)` vede la tabella e non fa
        nulla: dove `decision_rollup_hour` esiste gia' — quella vera ha 49.245
        righe per corsa — la colonna nuova non comparirebbe mai.

        Le righe preesistenti non hanno un'impronta sensata da ricevere: sono
        state scritte quando la politica non veniva registrata, e inventare un
        valore vorrebbe dire dichiarare fresco cio' che non si sa. Si
        **buttano** e si ricalcolano: la corsa intera costa ~5 s. Solo dopo la
        colonna diventa `NOT NULL`, cosi' la garanzia vale anche nello schema e
        non solo nel codice.

        `ADD COLUMN IF NOT EXISTS` e' un no-op sulla colonna gia' presente:
        rieseguire la migrazione non e' distinguibile dall'eseguirla una volta.

        **Come si torna indietro**: `ALTER TABLE decision_rollup_hour DROP
        COLUMN IF EXISTS impronta`. Nessuna lettura preesistente la nomina.
        """
        aggiunte: list[str] = []
        with self.engine.begin() as conn:
            presenti = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"), {"t": DECISION_TABLE}).all()}
            if not presenti:
                return aggiunte  # tabella assente: ci pensa init()
            if "impronta" not in presenti:
                conn.execute(text(
                    f"ALTER TABLE {DECISION_TABLE} "
                    "ADD COLUMN IF NOT EXISTS impronta TEXT"))
                aggiunte.append("impronta")
            svuotate = conn.execute(text(
                f"DELETE FROM {DECISION_TABLE} WHERE impronta IS NULL")).rowcount
            conn.execute(text(
                f"ALTER TABLE {DECISION_TABLE} "
                "ALTER COLUMN impronta SET NOT NULL"))
        if svuotate:
            logger.info("scartate %d righe senza impronta: vanno ricalcolate "
                        "con `python -m pipeline.decision_rollup --run-id ...`",
                        svuotate)
        return aggiunte

    # -- riempimento -------------------------------------------------------
    def fill(self, run_id: str) -> dict[str, Any]:
        """Precalcola la corsa **intera** del run e la scrive.

        Non prende estremi: vedi la precondizione di contiguita' nel docstring
        del modulo. Ritorna un riassunto ispezionabile.
        """
        t0 = time.perf_counter()
        serie, finestra, cov_hi = ingredienti(self.engine, run_id)
        t_prelievo = time.perf_counter() - t0

        t1 = time.perf_counter()
        righe = list(righe_della_corsa(serie, finestra, run_id, _iso(cov_hi)))
        t_calcolo = time.perf_counter() - t1
        if not righe:
            raise DecisionRollupError(
                f"run {run_id!r}: la griglia oraria e' vuota, nessuna valvola "
                "ha 24 h di secchielli")

        sql = text(_sql_insert())
        t2 = time.perf_counter()
        with self.engine.begin() as conn:
            for k in range(0, len(righe), PEZZO):
                pezzo = righe[k:k + PEZZO]
                conn.execute(sql, pezzo)
                logger.info("scritte %d/%d righe", k + len(pezzo), len(righe))
        t_scrittura = time.perf_counter() - t2

        ore = {r["ora"] for r in righe}
        return {
            "run_id": run_id,
            "from": _iso(min(ore)), "to": _iso(max(ore)),
            "ore": len(ore), "valvole": len({r["valvola"] for r in righe}),
            "rows": len(righe),
            "s_prelievo": round(t_prelievo, 2),
            "s_calcolo": round(t_calcolo, 2),
            "s_scrittura": round(t_scrittura, 2),
        }

    # -- lettura -----------------------------------------------------------
    def valvole_del_run(self, run_id: str) -> list[int]:
        with self.engine.connect() as conn:
            return [int(r[0]) for r in conn.execute(text(
                f"SELECT DISTINCT valve_id FROM {DECISION_TABLE} "
                "WHERE run_id = :run ORDER BY valve_id"),
                {"run": run_id}).all()]

    def decisione(self, run_id: str, ora: datetime) -> dict[str, Any]:
        """Il corpo di `GET /valves/decision` a `ora`, letto dalla tabella.

        Deve uscirne lo stesso oggetto che produrrebbe `decision.decisione()`,
        campo per campo: e' lo sha256 del corpo il criterio di accettazione di
        questo modulo, non un «equivalente».

        Due letture: la riga dell'ora per ogni valvola, e le ultime 24 righe
        per valvola fino a quell'ora (`storia_24h`). La seconda e' una
        `row_number()` perche' sono le ultime 24 **righe**, non le ultime 24
        ore: vedi il docstring del modulo.
        """
        ora = _utc(ora)
        with self.engine.connect() as conn:
            correnti = conn.execute(text(
                "SELECT valve_id, azione, di_fila, chiamata_a, arrivo, "
                "       sostituita, d_eur, r_eur, p1, delta_p, tasso_scarti, "
                "       q_es, motivo, stima "
                f"FROM {DECISION_TABLE} "
                "WHERE run_id = :run AND ora_ts = :ora ORDER BY valve_id"),
                {"run": run_id, "ora": ora}).all()
            storie = conn.execute(text(
                "SELECT valve_id, ora_ts, mostra, d_eur, r_eur, eser FROM ("
                "  SELECT valve_id, ora_ts, mostra, d_eur, r_eur, eser, "
                "         row_number() OVER (PARTITION BY valve_id "
                "                            ORDER BY ora_ts DESC) AS rn "
                f"  FROM {DECISION_TABLE} "
                "  WHERE run_id = :run AND ora_ts <= :ora) q "
                "WHERE rn <= 24 ORDER BY valve_id, ora_ts"),
                {"run": run_id, "ora": ora}).all()

        per_valvola: dict[int, list[dict[str, Any]]] = {}
        for valve_id, ora_ts, mostra, d_eur, r_eur, eser in storie:
            voce: dict[str, Any] = {"at": _z(ora_ts), "azione": mostra,
                                    "D": round(d_eur, 2), "R": round(r_eur, 2)}
            if not eser:
                voce["esercizio"] = False
            per_valvola.setdefault(int(valve_id), []).append(voce)

        conteggi = {"intervieni": 0, "continua_degradata": 0,
                    "continua": 0, "senza_dati": 0}
        viste = {int(r[0]) for r in correnti}
        valvole: list[dict[str, Any]] = []
        for v in self.valvole_del_run(run_id):
            if v not in viste:
                conteggi["senza_dati"] += 1
                valvole.append(decision._valvola_senza_dati(v))
        for (valve_id, azione, di_fila, chiamata_a, arrivo, sostituita,
             d_eur, r_eur, p1, delta_p, tasso_scarti, q_es, motivo,
             stima) in correnti:
            chiamata = chiamata_a is not None and not sostituita
            conteggi["continua_degradata" if azione == "continua degradata"
                     else azione] += 1
            riga: dict[str, Any] = {
                "valve_id": int(valve_id),
                "azione": azione,
                "conferma": {"di_fila": int(di_fila),
                             "K": decision.K_CONFERMA,
                             "chiamata": bool(chiamata)},
                "D": round(d_eur, 2), "R": round(r_eur, 2),
                "p1": round(p1, 6), "delta_p": round(delta_p, 6),
                "tasso_scarti_eur_h": round(tasso_scarti, 2),
                "q_es": round(q_es, 3),
                "motivo": motivo,
                "canale": decision.CANALE, "classe": decision.CLASSE,
                "stima": _stima_ordinata(stima),
                "storia_24h": per_valvola.get(int(valve_id), []),
            }
            if chiamata:
                riga["squadra"] = {"arrivo": _z(arrivo), "ore": int(decision.L_H)}
            valvole.append(riga)
        valvole.sort(key=lambda r: r["valve_id"])
        return {"run_id": run_id, "adesso": _z(ora),
                "parametri": decision.parametri(), "conteggi": conteggi,
                "valvole": valvole}

    def linea(self, run_id: str) -> dict[str, Any]:
        """Il corpo di `GET /valves/decision/timeline`, letto dalla tabella.

        Le chiamate si riconoscono da `chiamata_a = ora_ts`: `camminata()`
        scrive `chiamata_a` all'ora in cui la chiamata parte e poi se la
        trascina, quindi l'unica riga in cui i due coincidono e' proprio quella
        del `nuova_chiamata` della camminata. Non serve una colonna in piu'
        per dire una cosa che i dati gia' dicono.
        """
        with self.engine.connect() as conn:
            conti = conn.execute(text(
                "SELECT ora_ts, "
                "  COUNT(*) FILTER (WHERE azione = 'intervieni'), "
                "  COUNT(*) FILTER (WHERE azione = 'continua degradata') "
                f"FROM {DECISION_TABLE} WHERE run_id = :run "
                "GROUP BY ora_ts ORDER BY ora_ts"), {"run": run_id}).all()
            chiamate_righe = conn.execute(text(
                "SELECT valve_id, chiamata_a, arrivo "
                f"FROM {DECISION_TABLE} "
                "WHERE run_id = :run AND chiamata_a = ora_ts"),
                {"run": run_id}).all()

        if not conti:
            return {"run_id": run_id, "prima_ora": None, "ultima_ora": None,
                    "ore": 0, "i": [], "d": [], "chiamate": []}

        per_ora = {_utc(t): (int(i), int(d)) for t, i, d in conti}
        prima, ultima = min(per_ora), max(per_ora)
        n = int((ultima - prima).total_seconds() // 3600) + 1
        ore_tutte = [prima + timedelta(hours=k) for k in range(n)]
        chiamate = [{"valvola": int(v), "chiamata": _z(c), "arrivo": _z(a)}
                    for v, c, a in chiamate_righe]
        chiamate.sort(key=lambda c: (c["chiamata"], c["valvola"]))
        return {
            "run_id": run_id,
            "prima_ora": _z(prima), "ultima_ora": _z(ultima), "ore": n,
            "i": [per_ora.get(t, (0, 0))[0] for t in ore_tutte],
            "d": [per_ora.get(t, (0, 0))[1] for t in ore_tutte],
            "chiamate": chiamate,
        }


# --------------------------------------------------------------------------
#  CLI
# --------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.decision_rollup",
        description="Precalcola il verdetto ora per ora (decision_rollup_hour).")
    ap.add_argument("--run-id", required=True, help="corsa da precalcolare")
    ap.add_argument("--db-url", default=DATABASE_URL_DEFAULT)
    ap.add_argument("--status", action="store_true",
                    help="stampa copertura e righe senza scrivere nulla")
    return ap


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_arg_parser().parse_args(argv)
    r = DecisionRollup(url=args.db_url)
    r.init()
    r.migrate()
    if args.status:
        prima, ultima = r.coverage(args.run_id)
        print(f"run              {args.run_id}")
        print(f"verdetto         {_iso(prima)} -> {_iso(ultima)}")
        print(f"righe            {r.rows_for(args.run_id)}")
        print(f"valvole          {len(r.valvole_del_run(args.run_id))}")
        in_tabella = sorted(r.impronte(args.run_id))
        try:
            _s, finestra, _c = ingredienti(r.engine, args.run_id)
        except DecisionRollupError as exc:
            print(f"impronta tabella {', '.join(in_tabella) or '(nessuna riga)'}")
            print(f"impronta adesso  non calcolabile: {exc}")
            return 0
        corrente = decision.impronta_parametri(finestra)
        print(f"impronta tabella {', '.join(in_tabella) or '(nessuna riga)'}")
        print(f"impronta adesso  {corrente}")
        if in_tabella == [corrente]:
            print("                 coincidono: le rotte leggono la tabella")
        elif not in_tabella:
            print("                 la tabella e' vuota per questo run: "
                  "le rotte ricalcolano a ogni richiesta")
        elif len(in_tabella) > 1:
            print("                 impronte MISTE in tabella: il riempimento "
                  "si e' fermato a meta', le rotte ricalcolano")
        else:
            print("                 NON coincidono: i parametri o la finestra "
                  "sana sono cambiati dopo il precalcolo, le rotte "
                  "ricalcolano. Rilanciare il riempimento.")
        return 0
    try:
        s = r.fill(args.run_id)
    except DecisionRollupError as exc:
        logger.error("%s", exc)
        return 2
    print(f"precalcolate le ore {s['from']} -> {s['to']} "
          f"({s['ore']} ore x {s['valvole']} valvole, {s['rows']} righe)")
    print(f"prelievo {s['s_prelievo']} s, calcolo {s['s_calcolo']} s, "
          f"scrittura {s['s_scrittura']} s")
    return 0


__all__ = ["DecisionRollup", "DecisionRollupError",
           "build_decision_rollup_metadata", "DECISION_TABLE", "ORA",
           "ingredienti", "righe_della_corsa"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
