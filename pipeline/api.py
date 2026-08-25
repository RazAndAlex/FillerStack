"""API M10 (ADR-0021) — FastAPI read-only sullo storico operazionale.

Espone stato macchina, KPI, prediction e alert in modo machine-agnostic
(invariante §84–85): legge SOLO dal DB operazionale (PostgreSQL via
`pipeline/storage.py`), mai dal simulatore. L'API è il contratto che la
dashboard consuma; nessuna scrittura qui (il data-plane è ingest/inference/
alert engine, il control-plane è OPC UA — questa API è puro observation
plane, contesto §93–95).

Endpoint (spec M10 §5):
- GET /health                   liveness/readiness (DB reachable)
- GET /machine/state            stato OMAC corrente
- GET /machine/oee              OEE live Home L0 (window hour|shift|day, prev
                                window, degraded con reason se dati
                                insufficienti)
- GET /machine/oee/series       serie temporale di OEE: la STESSA risposta di
                                /machine/oee con `at` camminante all'indietro
                                (nessuna semantica nuova, un solo punto di
                                verita')
- GET /valves/quality/series    qualita' (good/total) di OGNI valvola in
                                secchielli CONTIGUI (hour|day|week), letta dal
                                riepilogo `cycle_rollup_hour`
- GET /valves                   catalogo fisso valvole 1..35 + ultima
                                prediction + alert attivi + ultimo KPI
- GET /valves/baseline          riferimento sano per valvola (media, sigma,
                                MRbar, UCL/LCL XmR) sulla finestra DICHIARATA
- GET /valves/profile           lo stesso profilo per TUTTE le valvole del
                                periodo in una chiamata sola (mappa
                                valve_id -> {periodo, base})
- GET /valves/{valve_id}        dettaglio valvola (prediction/alert attivi)
- GET /valves/{valve_id}/score  serie anomaly_score + predicted_label
- GET /valves/{valve_id}/profile
                                profilo del ciclo MEDIO nel periodo (sei medie
                                dal riepilogo `cycle_rollup_hour`) piu' lo
                                stesso profilo sulla finestra sana della STESSA
                                valvola
- GET /valves/{valve_id}/kpi    serie KPI per ciclo (colonne operazionali,
                                18 colonne, ordinate per cycle_id DESC)
- GET /alerts                   stato CURRENTE degli alert (una riga per
                                (valve_id, fault_type)); lo storico delle
                                transizioni (incluse le chiusure) vive in
                                `alert_transitions`
- GET /alerts/history           la tabella `alerts` INTERA, senza filtro di
                                stato (stessa forma di /alerts)

Dipendenze: fastapi, uvicorn (registrate in requirements.txt fin da questa
milestone). Nessuna logica di business qui: solo proiezione del DB.

## Il run (2026-08-19)

La tabella `cycles` contiene piu' run del simulatore. `cycle_id` riparte da 1 a
ogni run e i run si SOVRAPPONGONO nel tempo di parete: una query senza filtro di
run non fallisce, restituisce un numero plausibile e sbagliato. Ogni route che
tocca `cycles` (`/machine/oee`, `/machine/oee/series`, `/valves`,
`/valves/baseline`, `/valves/{id}/kpi`) accetta percio' un parametro opzionale
e uniforme `run_id` e dichiara in risposta quale run ha risposto
(`source.run_id`, `__meta.run_id`, `window.run_id`, `run_id`). Non dichiarato →
KV `current_run_id`, poi l'unico run presente; con piu' run e nessuno indicato
la risposta resta **200 degradata** con il motivo — mai un 500, mai una scelta
silenziosa (`_resolve_run`).
"""
from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from math import gcd
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import DateTime, desc, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from pipeline.cycle_rollup import BUCKET as ROLLUP_BUCKET
from pipeline.cycle_rollup import PROFILE_METRICS, ROLLUP_TABLE
from pipeline.cycle_rollup import n_col as _n_col
from pipeline.cycle_rollup import sum_col as _sum_col
from pipeline.cycle_rollup import ceil_hour as _ceil_ora
from pipeline.cycle_rollup import floor_hour as _floor_ora
from pipeline.storage import ALERT_STATUSES, Storage, make_engine

app = FastAPI(
    title="FillerStack — Operational API",
    description="API read-only sullo storico operazionale (Prediction/Alert/KPI).",
    version="1.0.0",
)

# storage lazy su module import: l'engine si costruisce alla prima richiesta
# per non forzare una connessione DB all'import (testabilità).
_store: Storage | None = None

# Stati "attivi" (una riga correntemente aperta/sostenuta).
_ACTIVE_ALERT_STATUSES = ALERT_STATUSES[:2]  # ("open", "sustained")


def _storage() -> Storage:
    global _store
    if _store is None:
        _store = Storage(make_engine())
    return _store


def _row_to_dict(row) -> dict[str, Any]:
    return dict(row._mapping)


def _iso(v) -> str | None:
    """datetime tz-aware → ISO8601 (per JSON deterministico)."""
    return v.isoformat() if v is not None else None


_KPI_TS_FIELDS = ("event_ts", "source_ts", "ingest_ts")


def _kpi_iso(row: dict[str, Any]) -> dict[str, Any]:
    """Timestamp di una riga `cycles` in ISO8601 con offset `+00:00`.

    Perche' esiste: `CyclesStorage.kpi_series` / `latest_kpi_by_valve`
    ritornano `datetime` grezzi, che FastAPI serializza con il suffisso `Z`,
    mentre ogni altra route passa da `_iso()` e produce `+00:00`. Nella STESSA
    risposta `/valves` convivevano quindi i due formati (`prediction_ts` con
    `+00:00`, `event_ts` con `Z`): stesso istante, due grafie. La conversione
    sta qui, al confine di presentazione, e non nello storage, che continua a
    restituire oggetti `datetime` ai suoi altri consumatori.
    """
    for col in _KPI_TS_FIELDS:
        if isinstance(row.get(col), datetime):
            row[col] = _iso(row[col])
    return row


def _cycles_store(st: Storage) -> Any | None:
    """Helper lazy per la serie KPI per ciclo.

    La tabella `cycles` + `CyclesStorage` vivono in
    `pipeline/cycles_storage.py`, file aggiunto da un altro worker dello
    stesso pool (può non esistere ancora a runtime). Qui si importa DENTRO
    il handler e, se import o tabella non sono disponibili, si degrada:
    - GET /valves/{valve_id}/kpi → 501 con messaggio chiaro;
    - GET /valves → `last_kpi: None` (il catalogo resta servibile).
    Il codice assume che il modulo esista (contratto congelato: 18 colonne
    operazionali, `kpi_series(valve_id, limit)`, `latest_kpi_by_valve()`).
    """
    try:
        from pipeline.cycles_storage import CyclesStorage  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return CyclesStorage(st.engine)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Run corrente (2026-08-19) — `cycles.run_id`
# ---------------------------------------------------------------------------
# `cycle_id` riparte da 1 a ogni run del simulatore e i run si SOVRAPPONGONO
# nel tempo di parete: ne' (valve_id, cycle_id) ne' un filtro su `event_ts`
# distinguono due run. Una query senza filtro di run non da' errore — da' un
# numero plausibile e sbagliato (il run piu' LUNGO al posto del piu' recente,
# o due run mescolati dentro la stessa finestra analitica). Ogni punto
# dell'API che tocca `cycles` passa quindi di qui.
RUN_AMBIGUO_HINT = (
    "passare `run_id` in query oppure persistere la chiave KV "
    "`current_run_id` in machine_state")


def _resolve_run(st: Storage, run_id: str | None = None) -> tuple[str | None, str | None]:
    """Risolve il run da interrogare → `(run, reason)`.

    `reason` non-None significa **ambiguo**: piu' run in tabella e nessuno
    indicato. In quel caso la route NON interroga `cycles` e degrada con
    motivo (200 + `degraded: true`), coerente con `/valves/baseline` senza
    finestra dichiarata e con `/machine/oee` senza cicli: **mai un 500, mai un
    numero scelto in silenzio**.

    `run = None` significa invece "nessun filtro necessario": tabella vuota,
    modulo `cycles_storage` assente, o schema senza la colonna `run_id`
    (installazione parziale / tabella di test). Non e' un degrado: non c'e'
    nulla da distinguere.
    """
    cs = _cycles_store(st)
    if cs is None:
        return run_id, None
    from pipeline.cycles_storage import AmbiguousRunError  # noqa: PLC0415
    try:
        return cs.resolve_run_id(run_id), None
    except AmbiguousRunError as exc:
        return None, f"run non determinato: {exc}"
    except Exception:
        # colonna `run_id` inesistente (schema legacy): nessun run da
        # distinguere, si legge la tabella intera.
        return run_id, None


# ---------------------------------------------------------------------------
# OEE Home L0 (spec dashboard §4bis/§7.2, oee-backend-spec §D) — macchina
# indipendente: legge SOLO dal DB operazionale (history OMAC + cycles).
# ---------------------------------------------------------------------------
# Finestre: hour = 1h rolling, shift = 8h rolling, day = 24h rolling
# (oee-backend-spec §D; `hour` aggiunto il 2026-08-19).
#
# Perche' esiste `hour` — e perche' NON esiste `15min` (misure in
# misure del 2026-08-19 §c.5, su due run reali; il documento resta locale):
# - a 1 h la finestra contiene ~39.374 cicli, quindi il rumore binomiale sulla
#   Quality vale 0,0022 e sta SOTTO l'escursione reale della Q su una giornata
#   (0,0071–0,0090); a 15 min il rumore sale a 0,0042 e a 5 min a 0,0072, cioe'
#   pari o superiore a tutto il segnale disponibile;
# - l'Availability passa da 71 a 11 valori distinti (una fermata si legge
#   nell'ora in cui e' avvenuta, non spalmata su 24 h); a 15 min diventa
#   binaria e smette di essere un indicatore;
# - alla spaziatura di 2 h della serie, `day` condivide il 91,67% dei dati fra
#   punti consecutivi; `hour` lo porta a 0%.
# Cio' che `hour` NON compra: la Quality non "si sblocca" a nessuna ampiezza di
# finestra (§c.3). Una finestra piu' corta aumenta la risoluzione temporale di
# A e dell'OEE, non la sensibilita' della Q.
HOUR_INTERVAL = timedelta(hours=1)
SHIFT_INTERVAL = timedelta(hours=8)
DAY_INTERVAL = timedelta(hours=24)
# Finestre larghe, aggiunte il 2026-08-20 con il riepilogo orario
# (`pipeline/cycle_rollup.py`): prima erano impraticabili — una media su 7 o 30
# giorni voleva dire contare milioni di cicli a ogni punto. Sono le ampiezze
# che rendono leggibile una serie su 60 giorni: `day` su due mesi mostra il
# ritmo giornaliero, `week`/`month` la deriva sotto quel ritmo.
WEEK_INTERVAL = timedelta(days=7)
MONTH_INTERVAL = timedelta(days=30)

OeeWindow = Literal["hour", "shift", "day", "week", "month"]

WINDOW_INTERVALS: dict[str, timedelta] = {
    "hour": HOUR_INTERVAL,
    "shift": SHIFT_INTERVAL,
    "day": DAY_INTERVAL,
    "week": WEEK_INTERVAL,
    "month": MONTH_INTERVAL,
}

# OMAC: 1 = Running (CONTEXT.md / realtime.py OMAC_CODES).
OMAC_RUNNING_CODE = 1

# SpeedTarget del V3 (Recipe, spec §7.2): 15.500 cph. Default se nessun
# valore è mai stato persistito nel KV `machine_state` (`speed_target`).
DEFAULT_SPEED_TARGET_CPH = 15500.0

# Oltre questo rapporto real/theoretical, con un target NON verificato, non si
# sta piu' misurando una macchina veloce: si sta misurando un target incoerente
# con l'impianto. Una macchina reale supera regolarmente il target di qualche
# punto percentuale (sovravelocita' normale), quindi la soglia non puo' essere
# 1.0; il caso osservato che ha motivato il controllo era 2.54 (cadenza cicli
# 3.2 s/valvola contro un target di 15500 cph, docs/V3-DESIGN.md §188 lo
# registra come punto aperto: "da riconciliare con la cadenza osservata").
PERFORMANCE_RATIO_IMPLAUSIBILE = 1.25

# Sotto questa soglia un buco di copertura e' arrotondamento fra i bordi degli
# intervalli OMAC, non una lacuna: non si marchia la finestra come parziale.
COVERAGE_TOLLERANZA_S = 1.0


def _oee_speed_target(st: Storage) -> tuple[float, str]:
    """SpeedTarget per le teoriche, con la sua PROVENIENZA.

    Ritorna `(valore, sorgente)` dove sorgente e' `"kv"` se il valore viene
    dal KV `speed_target` persistito (writer futuri / PLC reale), oppure
    `"default"` se si sta usando la costante `DEFAULT_SPEED_TARGET_CPH`.

    La provenienza NON e' un dettaglio: un target non verificato che produce
    Performance > 1 non e' una macchina veloce, e' un target sbagliato, e
    l'API deve poterlo dire invece di moltiplicarlo dentro l'OEE.
    """
    v = st.get_machine_state("speed_target")
    if isinstance(v, dict):
        v = v.get("speed_target", v.get("value"))
    if v is None:
        return DEFAULT_SPEED_TARGET_CPH, "default"
    try:
        return float(v), "kv"
    except (TypeError, ValueError):
        return DEFAULT_SPEED_TARGET_CPH, "default"


class _CycleCounts:
    """Conteggi di `cycles` per finestra + il discriminante "esistono dati?".

    **Perche' e' una classe e non piu' una sola query.** La versione
    precedente chiedeva le tre misure in un colpo solo, con la finestra
    temporale dentro i `COUNT(*) FILTER (...)` e nel `WHERE` il solo
    `run_id`. Cosi' scritta, contare un giorno costava la lettura INTEGRA
    del run: misurato su `storico_60d` (36.241.832 righe) un `Seq Scan` da
    605.626 buffer (~4,7 GB) e 76 s per finestra — e ogni risposta ne
    calcola due (corrente e precedente), ogni punto di serie altre due.

    Le tre misure sono due domande diverse, e vanno separate:

    - le due di finestra (`total`, `good`) hanno il predicato temporale nel
      `WHERE`, cosi' l'indice `ix_cycles_run_event_ts` puo' lavorare;
    - il discriminante "esistono righe con `event_ts`?" non ha finestra ed
      e' molto piu' economico da chiedere come `MIN(event_ts) IS NOT NULL`:
      l'indice restituisce il primo elemento e si ferma (misurato: 0,2 ms a
      cache calda contro la scansione totale). E' un'esistenza, non un
      conteggio, ed e' sempre stata consumata solo come tale (`== 0` /
      `> 0`), quindi il comportamento osservabile non cambia.

    L'oggetto e' a vita di richiesta e memorizza il discriminante: una sola
    domanda per risposta HTTP invece di una per finestra.
    """

    def __init__(self, st: Storage, run: str | None = None):
        self.st = st
        self.run = run
        self._ha_ts: bool | None = None

    # -- discriminante: esistono righe con event_ts in questo run? ---------
    def ha_event_ts(self) -> bool:
        if self._ha_ts is None:
            sql = "SELECT MIN(event_ts) FROM cycles"
            params: dict[str, Any] = {}
            if self.run is not None:
                sql += " WHERE run_id = :run"
                params["run"] = self.run
            with self.st.engine.connect() as conn:
                self._ha_ts = conn.execute(text(sql), params).scalar() is not None
        return self._ha_ts

    # -- righe (valve_id, total, good) della finestra ----------------------
    def _righe(self, start: datetime, end: datetime) -> list[tuple[int, int, int]]:
        cond = ["event_ts >= :start", "event_ts < :end"]
        params: dict[str, Any] = {"start": start, "end": end}
        if self.run is not None:
            cond.insert(0, "run_id = :run")
            params["run"] = self.run
        with self.st.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT valve_id, COUNT(*) AS total, "
                "  COUNT(*) FILTER (WHERE fill_quality_ok = TRUE) AS good "
                "FROM cycles WHERE " + " AND ".join(cond) +
                " GROUP BY valve_id ORDER BY valve_id"), params).all()
        return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]

    def window(self, start: datetime, end: datetime):
        """La stessa quadrupla di sempre: (total, good, with_ts, per_valve)."""
        try:
            ha_ts = self.ha_event_ts()
            righe = self._righe(start, end)
        except SQLAlchemyError:
            return None
        total = good = 0
        per_valve: list[dict[str, Any]] = []
        for valve_id, v_total, v_good in righe:
            total += v_total
            good += v_good
            per_valve.append({
                "valve_id": valve_id,
                "good": v_good,
                "total": v_total,
                # nessun ciclo nella finestra → nessuna qualita' MISURATA: null,
                # non 0.0 (che leggerebbe come "tutti gli scarti").
                "quality": round(v_good / v_total, 3) if v_total > 0 else None,
            })
        return total, good, ha_ts, per_valve


class _CycleCountsBucketed(_CycleCounts):
    """Lo stesso contratto, ma per una SERIE: una lettura sola per tutti i punti.

    `/machine/oee/series` chiede 25 punti per finestra e ogni punto due
    finestre: 50 conteggi che si sovrappongono quasi del tutto (passo 2 h,
    finestra 24 h → due punti vicini condividono 22 h di cicli). Rileggerli
    uno per uno moltiplica per cinquanta lo stesso lavoro.

    Qui si legge UNA volta l'intervallo che copre tutti i punti, aggregato in
    secchielli di `grain` secondi ancorati a `anchor` (il bordo destro della
    serie), e ogni finestra e' la SOMMA dei suoi secchielli. E' esatto, non
    approssimato: passo e ampiezza di ogni finestra sono multipli interi di
    `grain` (gcd di passo e ampiezza), quindi ogni bordo di finestra cade su
    un bordo di secchiello e i secchielli partizionano la finestra senza
    residui. Il predicato per riga e' identico a quello della query diretta
    (`event_ts >= start AND event_ts < end`, stesso `run_id`), percio' i
    numeri serviti sono gli stessi — il test
    `test_bucket_identico_alla_query_diretta` lo verifica riga per riga.
    """

    def __init__(self, st: Storage, run: str | None,
                 anchor: datetime, lo: datetime, grain: timedelta):
        super().__init__(st, run)
        self.anchor = anchor
        self.lo = lo
        self.grain_s = int(grain.total_seconds())
        self._secchielli: dict[int, dict[int, tuple[int, int]]] | None = None

    def _carica(self) -> dict[int, dict[int, tuple[int, int]]]:
        if self._secchielli is None:
            cond = ["event_ts >= :lo", "event_ts < :anchor"]
            params: dict[str, Any] = {"lo": self.lo, "anchor": self.anchor,
                                      "grain": self.grain_s}
            if self.run is not None:
                cond.insert(0, "run_id = :run")
                params["run"] = self.run
            with self.st.engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT valve_id, "
                    # ceil(...)-1, non floor(...): il secchiello b deve
                    # contenere [anchor-(b+1)g, anchor-b*g), chiuso a
                    # SINISTRA come le finestre ([start, end)). Con floor
                    # sarebbe chiuso a destra e un ciclo caduto esattamente
                    # su un bordo finirebbe nella finestra sbagliata.
                    "  (ceil(EXTRACT(EPOCH FROM (:anchor - event_ts)) "
                    "        / :grain) - 1)::bigint AS b, "
                    "  COUNT(*) AS total, "
                    "  COUNT(*) FILTER (WHERE fill_quality_ok = TRUE) AS good "
                    "FROM cycles WHERE " + " AND ".join(cond) +
                    " GROUP BY valve_id, b"), params).all()
            acc: dict[int, dict[int, tuple[int, int]]] = {}
            for valve_id, b, tot, gd in rows:
                acc.setdefault(int(valve_id), {})[int(b)] = (int(tot), int(gd))
            self._secchielli = acc
        return self._secchielli

    def _righe(self, start: datetime, end: datetime) -> list[tuple[int, int, int]]:
        # Finestra fuori dall'intervallo precaricato → si torna alla query
        # diretta: meglio una lettura in piu' che un numero incompleto.
        if start < self.lo or end > self.anchor:
            return super()._righe(start, end)
        # ...e lo stesso se un bordo NON cade su un bordo di secchiello.
        # L'esattezza di questa classe poggia sull'ipotesi che passo e
        # ampiezza siano multipli di `grain`: e' vera per come la costruisce
        # `_contatore_cicli`, ma non e' imposta dal codice, e se cade i due
        # `//` qui sotto troncano la finestra ai secchielli interni — cioe'
        # restituiscono un numero piu' piccolo, senza dirlo. Verificarla costa
        # due resti; scoperta il 2026-08-20 su una finestra costruita a mano.
        if (int((self.anchor - start).total_seconds()) % self.grain_s
                or int((self.anchor - end).total_seconds()) % self.grain_s):
            return super()._righe(start, end)
        b_lo = int((self.anchor - end).total_seconds()) // self.grain_s
        b_hi = int((self.anchor - start).total_seconds()) // self.grain_s
        out: list[tuple[int, int, int]] = []
        for valve_id in sorted(self._carica()):
            tot = gd = 0
            secchi = self._secchielli[valve_id]
            for b in range(b_lo, b_hi):
                v = secchi.get(b)
                if v is not None:
                    tot += v[0]
                    gd += v[1]
            # valvola senza cicli NELLA FINESTRA: non e' una riga, esattamente
            # come nel GROUP BY diretto (che non la restituirebbe).
            if tot:
                out.append((valve_id, tot, gd))
        return out


class _CycleCountsRollup(_CycleCountsBucketed):
    """Lo stesso contratto, servito dal riepilogo orario `cycle_rollup_hour`.

    **La forma, e perche' e' esatta.** Le finestre dell'OEE non sono allineate
    all'ora: l'`at` corrente del run storico e' `19:29:35`, quindi una finestra
    `day` va dalle 19:29:35 alle 19:29:35. Sommare secchielli orari darebbe un
    numero sbagliato ai due bordi. La decomposizione usata qui e' invece
    un'identita', non un'approssimazione:

        [start, end) = [start, ceil_ora(start))          <- bordo sinistro
                     + [ceil_ora(start), floor_ora(end)) <- ore INTERE
                     + [floor_ora(end), end)             <- bordo destro

    Le ore intere vengono dal riepilogo; i due bordi si leggono direttamente da
    `cycles` e valgono al massimo un'ora ciascuno (~39.000 cicli, una discesa
    di indice). Un bordo su un istante gia' allineato all'ora e' vuoto e non
    viene nemmeno chiesto. **Nessun arrotondamento in nessun punto**: i
    conteggi sono interi e devono coincidere con la query diretta, cifra per
    cifra (lo verifica `pipeline/tests/test_cycle_rollup.py`).

    **Perche' i bordi si preparano tutti insieme.** Una serie di 200 punti ha
    ~230 istanti di bordo distinti: chiederli uno per uno sono 460 andate e
    ritorni. `prepara()` li raccoglie e li chiede in UN solo statement, con la
    lista degli intervalli come `VALUES` e un `JOIN` su `cycles`: il piano
    diventa un ciclo di discese di indice, tante quante i bordi.

    **Quando NON si usa il riepilogo.** Se la tabella manca, se il run non e'
    coperto, o se la finestra chiesta esce dalla copertura, si ricade sulla
    classe base (`_CycleCountsBucketed`, cioe' la lettura diretta di `cycles`
    in secchielli). Meglio una lettura piu' lenta che un numero incompleto:
    le ore non riassunte non valgono zero.

    La copertura si deduce da `MIN/MAX(bucket_ts)` del run. Un'ora senza cicli
    non ha righe nel riepilogo, esattamente come non le ha nel `GROUP BY`
    diretto: "riga assente" significa "zero cicli", e vale solo dentro
    l'intervallo coperto. La contiguita' del riempimento e' la precondizione
    che rende vera questa deduzione — vedi il docstring di
    `pipeline/cycle_rollup.py`.
    """

    def __init__(self, st: Storage, run: str | None,
                 anchor: datetime, lo: datetime, grain: timedelta):
        super().__init__(st, run, anchor, lo, grain)
        self._cov: tuple[datetime, datetime] | None = None   # (primo, ultimo) bucket
        self._sx_illimitato = False
        self._attivo = False
        self._buckets: dict[int, tuple[list[datetime], list[int], list[int]]] = {}
        self._bordi: dict[tuple[datetime, datetime], dict[int, tuple[int, int]]] = {}

    # -- preparazione ------------------------------------------------------
    def prepara(self, finestre: list[tuple[datetime, datetime]]) -> bool:
        """Carica secchielli e bordi per tutte le finestre della richiesta.

        Ritorna False se il riepilogo non e' utilizzabile (tabella assente,
        run non coperto): in quel caso ogni `_righe` ricade sulla base.
        """
        if not self._carica_copertura():
            return False
        lo = min(s for s, _ in finestre)
        hi = max(e for _, e in finestre)
        try:
            self._buckets = self._leggi_secchielli(_ceil_ora(lo), _floor_ora(hi))
            self._bordi = self._leggi_bordi(finestre)
        except SQLAlchemyError:
            self._cov = None
            return False
        self._attivo = True
        return True

    def _carica_copertura(self) -> bool:
        """Copertura del riepilogo, piu' il permesso di estenderla a sinistra.

        Una finestra che comincia PRIMA del primo secchiello non e' per forza
        fuori copertura: se prima di quell'istante il run non ha alcun ciclo,
        quel vuoto e' un fatto, non un'ignoranza. Verificarlo costa una
        discesa di indice (`LIMIT 1`) e vale molto: senza, una serie su 60
        giorni ricade tutta sulla lettura diretta proprio nel caso per cui
        esiste il riepilogo — misurato il 2026-08-20, 147 secondi contro 0,3.
        """
        if self._cov is not None:
            return True
        cond: list[str] = []
        params: dict[str, Any] = {}
        if self.run is not None:
            cond.append("run_id = :run")
            params["run"] = self.run
        dove = (" WHERE " + " AND ".join(cond)) if cond else ""
        try:
            with self.st.engine.connect() as conn:
                row = conn.execute(text(
                    f"SELECT MIN(bucket_ts), MAX(bucket_ts) "
                    f"FROM {ROLLUP_TABLE}{dove}"), params).first()
                if row is None or row[0] is None:
                    return False
                cov_lo = _as_utc(row[0])
                prima = conn.execute(text(
                    "SELECT 1 FROM cycles" +
                    (dove + " AND " if cond else " WHERE ") +
                    "event_ts < :cov_lo LIMIT 1"),
                    {**params, "cov_lo": cov_lo}).first()
        except SQLAlchemyError:
            return False
        self._cov = (cov_lo, _as_utc(row[1]))
        self._sx_illimitato = prima is None
        return True

    def _leggi_secchielli(self, lo: datetime, hi: datetime):
        """Secchielli `[lo, hi)` come SOMME PROGRESSIVE, una per valvola.

        Non come dizionario ora→conteggio: sommare a mano le ore di una
        finestra costerebbe 1.440 passi per valvola per punto, e una serie di
        200 punti su due mesi ne farebbe decine di milioni in Python — il
        precalcolo in SQL sarebbe stato vanificato dal ciclo che lo consuma.
        Con le somme progressive ogni finestra e' una sottrazione fra due
        indici trovati per bisezione, cioe' un tempo che non dipende
        dall'ampiezza.
        """
        cond = ["bucket_ts >= :lo", "bucket_ts < :hi"]
        params: dict[str, Any] = {"lo": lo, "hi": hi}
        if self.run is not None:
            cond.insert(0, "run_id = :run")
            params["run"] = self.run
        with self.st.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT valve_id, bucket_ts, total, good "
                f"FROM {ROLLUP_TABLE} WHERE " + " AND ".join(cond) +
                " ORDER BY valve_id, bucket_ts"), params).all()
        acc: dict[int, tuple[list[datetime], list[int], list[int]]] = {}
        for valve_id, bucket_ts, total, good in rows:
            v = acc.setdefault(int(valve_id), ([], [0], [0]))
            v[0].append(_as_utc(bucket_ts))
            v[1].append(v[1][-1] + int(total))
            v[2].append(v[2][-1] + int(good))
        return acc

    def _leggi_bordi(self, finestre: list[tuple[datetime, datetime]]):
        """I due bordi parziali di ogni finestra, in una sola interrogazione.

        Ogni bordo e' un intervallo di meno di un'ora; quelli vuoti (istante
        gia' allineato all'ora) non vengono chiesti. Gli intervalli si
        ripetono molto fra i punti di una serie: si deduplicano prima.

        Una finestra piu' corta di un'ora, o a cavallo di due sole ore senza
        alcuna ora intera in mezzo, e' **tutta bordo**: entra qui per intero e
        viaggia nello stesso statement. Costa quanto la lettura diretta che
        avrebbe richiesto, ma senza un'andata e ritorno per punto.
        """
        voluti: set[tuple[datetime, datetime]] = set()
        for start, end in finestre:
            cs, fe = _ceil_ora(start), _floor_ora(end)
            if cs >= fe:
                voluti.add((start, end))      # finestra tutta dentro i bordi
                continue
            if start < cs:
                voluti.add((start, cs))
            if fe < end:
                voluti.add((fe, end))
        if not voluti:
            return {}
        # I bordi che si possono NON leggere: la coda `[t, ceil(t))` di un'ora
        # di cui si legge gia' la testa `[floor(t), t)` e' la differenza fra
        # l'ora intera (che il riepilogo conosce) e la testa. Vedi
        # `_complementari`.
        complementari, ore_intere = self._complementari(voluti)
        acc = self._conta_bordi(sorted(voluti - set(complementari)))
        if complementari:
            ore = self._ore_intere(ore_intere)
            rileggere: list[tuple[datetime, datetime]] = []
            for coda, (h, taglio) in complementari.items():
                testa = acc.get((h, taglio), {})
                resto: dict[int, tuple[int, int]] = {}
                negativo = False
                for valve_id, (tot, gd) in ore.get(h, {}).items():
                    t0, g0 = testa.get(valve_id, (0, 0))
                    dt, dg = tot - t0, gd - g0
                    if dt < 0 or dg < 0:
                        negativo = True
                        break
                    if dt:
                        resto[valve_id] = (dt, dg)
                if negativo or any(v not in ore.get(h, {}) for v in testa):
                    # Il riepilogo e la testa non tornano: non si inventa un
                    # numero, si legge la coda da `cycles` come prima.
                    rileggere.append(coda)
                else:
                    acc[coda] = resto
            if rileggere:
                acc.update(self._conta_bordi(sorted(rileggere)))
        return acc

    def _complementari(self, voluti: set[tuple[datetime, datetime]]):
        """Le code d'ora che si ricavano per DIFFERENZA invece di leggerle.

        In una serie di OEE ogni istante di bordo `t` compare due volte: come
        inizio di una finestra, che chiede la coda `[t, ceil(t))`, e come fine
        di un'altra, che chiede la testa `[floor(t), t)`. Le due meta' fanno
        l'ora intera `[floor(t), ceil(t))`, e l'ora intera il riepilogo la sa
        gia'. Leggere solo la testa dimezza le righe di `cycles` percorse —
        misurato il 2026-08-21: 3,5 milioni di righe di indice, 5,8 s.

        Non e' un'approssimazione: sono conteggi interi e la sottrazione e'
        esatta valvola per valvola, esattamente come la somma dei secchielli.
        Si applica solo se l'ora e' dentro la copertura del riepilogo
        (`_autorevole`); fuori, "riga assente" non vuol dire zero.

        Ritorna `({coda: (ora, taglio)}, {ore da chiedere al riepilogo})`.
        """
        teste: dict[datetime, set[datetime]] = {}
        for a, b in voluti:
            h = _floor_ora(a)
            if a == h and b < h + ROLLUP_BUCKET:
                teste.setdefault(h, set()).add(b)
        fuori: dict[tuple[datetime, datetime], tuple[datetime, datetime]] = {}
        ore: set[datetime] = set()
        for a, b in voluti:
            h = _floor_ora(a)
            if (a > h and b == h + ROLLUP_BUCKET
                    and a in teste.get(h, ())
                    and self._autorevole(h, h + ROLLUP_BUCKET)):
                fuori[(a, b)] = (h, a)
                ore.add(h)
        return fuori, ore

    def _ore_intere(self, ore: set[datetime]):
        """`{ora: {valve_id: (total, good)}}` per le ore chieste, dal riepilogo."""
        if not ore:
            return {}
        elenco = sorted(ore)
        params: dict[str, Any] = {f"h{i}": h for i, h in enumerate(elenco)}
        segnaposto = ", ".join(f"CAST(:h{i} AS timestamptz)"
                               for i in range(len(elenco)))
        cond = [f"bucket_ts IN ({segnaposto})"]
        if self.run is not None:
            cond.insert(0, "run_id = :run")
            params["run"] = self.run
        with self.st.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT bucket_ts, valve_id, total, good "
                f"FROM {ROLLUP_TABLE} WHERE " + " AND ".join(cond)),
                params).all()
        acc: dict[datetime, dict[int, tuple[int, int]]] = {h: {} for h in elenco}
        for bucket_ts, valve_id, total, good in rows:
            acc[_as_utc(bucket_ts)][int(valve_id)] = (int(total), int(good))
        return acc

    def _conta_bordi(self, ordinati: list[tuple[datetime, datetime]]):
        """I conteggi per valvola degli intervalli dati, in un solo statement."""
        if not ordinati:
            return {}
        valori = ", ".join(
            f"({i}, CAST(:a{i} AS timestamptz), CAST(:b{i} AS timestamptz))"
            for i in range(len(ordinati)))
        params: dict[str, Any] = {}
        for i, (a, b) in enumerate(ordinati):
            params[f"a{i}"] = a
            params[f"b{i}"] = b
        filtro_run = ""
        if self.run is not None:
            filtro_run = "c.run_id = :run AND "
            params["run"] = self.run
        with self.st.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT b.i, c.valve_id, COUNT(*) AS total, "
                "  COUNT(*) FILTER (WHERE c.fill_quality_ok = TRUE) AS good "
                f"FROM (VALUES {valori}) AS b(i, lo, hi) "
                "JOIN cycles c ON " + filtro_run +
                "  c.event_ts >= b.lo AND c.event_ts < b.hi "
                "GROUP BY b.i, c.valve_id"), params).all()
        acc: dict[tuple[datetime, datetime], dict[int, tuple[int, int]]] = {
            k: {} for k in ordinati}
        for i, valve_id, total, good in rows:
            acc[ordinati[int(i)]][int(valve_id)] = (int(total), int(good))
        return acc

    # -- lettura -----------------------------------------------------------
    def _autorevole(self, cs: datetime, fe: datetime) -> bool:
        """Il riepilogo puo' rispondere per TUTTE le ore intere `[cs, fe)`?

        Fuori copertura non vale zero: vale "non lo so", e si torna a `cycles`.
        """
        if self._cov is None:
            return False
        primo, ultimo = self._cov
        return ((cs >= primo or self._sx_illimitato)
                and fe <= ultimo + ROLLUP_BUCKET)

    def _righe(self, start: datetime, end: datetime) -> list[tuple[int, int, int]]:
        if not self._attivo:
            # riepilogo non disponibile: comportamento precedente a questo
            # lavoro (lettura di `cycles` in secchielli).
            return super()._righe(start, end)
        cs, fe = _ceil_ora(start), _floor_ora(end)
        if cs >= fe or not self._autorevole(cs, fe):
            # Nessuna ora intera riusabile, o ore fuori copertura. Si legge
            # `cycles` DIRETTAMENTE, non in secchielli: i secchielli di
            # `_CycleCountsBucketed` sono ancorati all'`at` della richiesta, e
            # una finestra che non e' un multiplo del loro passo verrebbe
            # troncata ai bordi — un numero sbagliato in silenzio.
            pronto = self._bordi.get((start, end))
            if pronto is not None:
                return [(v, t, g) for v, (t, g) in sorted(pronto.items()) if t]
            return _CycleCounts._righe(self, start, end)
        acc: dict[int, list[int]] = {}

        def somma(valve_id: int, tot: int, gd: int) -> None:
            v = acc.setdefault(valve_id, [0, 0])
            v[0] += tot
            v[1] += gd

        for bordo in ((start, cs), (fe, end)):
            for valve_id, (tot, gd) in self._bordi.get(bordo, {}).items():
                somma(valve_id, tot, gd)
        for valve_id, (ore, ptot, pgood) in self._buckets.items():
            i = bisect_left(ore, cs)
            j = bisect_left(ore, fe)
            if j > i:
                somma(valve_id, ptot[j] - ptot[i], pgood[j] - pgood[i])
        # valvola senza cicli nella finestra: nessuna riga, come nel GROUP BY
        # diretto (che non la restituirebbe).
        return [(vid, v[0], v[1]) for vid, v in sorted(acc.items()) if v[0]]


def _count_cycles(st: Storage, start: datetime, end: datetime,
                  run: str | None = None, counter: "_CycleCounts | None" = None):
    """Conteggi cycles su [start, end) NEL RUN: (total, good, with_ts, per_valve).

    - total: righe con event_ts nella finestra (→ Performance real);
    - good : di queste, con fill_quality_ok = TRUE (→ Quality);
    - with_ts: **discriminante** "esistono dati?" — True se nel run esiste
      almeno una riga con `event_ts` valorizzato, False se `event_ts` e' NULL
      ovunque o la tabella e' vuota. Era un conteggio, ed e' sempre stato letto
      solo come `== 0` / `> 0`: ora e' l'esistenza che gia' era, e costa una
      lettura di indice invece della scansione del run (vedi `_CycleCounts`).
    - per_valve: la stessa misura DISAGGREGATA per valvola, come lista di
      `{valve_id, good, total, quality}` ordinata per valve_id. Nasce dalla
      STESSA query e quindi dalla STESSA finestra: e' l'unico modo di renderla
      confrontabile con la Q di macchina (`/valves/{id}/kpi` copre 400 cicli =
      ~22 minuti, `/valves/baseline` copre la run sana di riferimento).

    Ritorna None se la tabella `cycles` o la colonna `event_ts` non
    esistono: la tabella vive in pipeline/cycles_storage.py (modulo di un
    altro worker; `event_ts` può ancora non esistere a runtime) — l'API
    DEGRADA con reason chiaro, mai 404. Query raw text() per non
    accoppiarsi allo schema di cycles_storage (solo valve_id + event_ts +
    fill_quality_ok, contrato oee-backend §D).

    I totali di macchina sono la SOMMA delle righe per valvola, non una
    seconda aggregazione: un unico GROUP BY (35 gruppi) sostituisce la vecchia
    query scalare, quindi la disaggregazione non costa una scansione in piu'.
    Una finestra SENZA cicli non da' 35 righe a zero: da' ZERO righe, quindi
    `per_valve` vuoto e `total = 0` — e' la condizione che a valle distingue
    "nessun ciclo prodotto in questa finestra" da "qualita' pari a zero".

    `run` non-None → `WHERE run_id = :run`: due run nel database si
    sovrappongono nel tempo di parete, quindi senza il filtro i conteggi
    sommerebbero cicli di run diversi caduti nella stessa finestra.
    `run = None` → nessun filtro (tabella senza nozione di run).

    `counter` (opzionale) e' il fornitore dei conteggi da riusare fra piu'
    finestre della stessa richiesta: assente, se ne costruisce uno usa-e-getta.
    """
    c = counter if counter is not None else _CycleCounts(st, run)
    return c.window(start, end)


class _StoriaOmac:
    """Le transizioni OMAC dell'INTERO arco di una richiesta, lette una volta.

    Perche' esiste (misurato il 2026-08-21): ogni punto di
    `/machine/oee/series` calcola due finestre e ognuna chiedeva la sua fetta
    di `machine_state_history` al database. Con 179 punti sono 358 andate e
    ritorni per finestra — 1,1 s — su una tabella che contiene **300 righe in
    tutto**. Il costo non era la query (0,06 ms), era il viaggio.

    `finestra()` applica in memoria lo STESSO predicato di
    `Storage.get_machine_state_history` (`entered_ts < end` AND `exited_ts`
    NULL o `> start`) e conserva l'ordine: essendo l'arco letto un
    sovrainsieme di ogni sotto-finestra, la lista restituita e' identica riga
    per riga a quella che avrebbe dato il database.

    Se la lettura fallisce, l'eccezione viene RILANCIATA a ogni chiamata:
    `_compute_oee_window` la intercetta e degrada con motivo, esattamente come
    quando interrogava lui il database.
    """

    def __init__(self, st: Storage, lo: datetime, hi: datetime):
        self._errore: SQLAlchemyError | None = None
        try:
            self._righe = st.get_machine_state_history(lo, hi)
        except SQLAlchemyError as exc:
            self._righe = []
            self._errore = exc

    def finestra(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        if self._errore is not None:
            raise self._errore
        return [r for r in self._righe
                if r["entered_ts"] < end
                and (r["exited_ts"] is None or r["exited_ts"] > start)]


def _storia_omac(st: Storage, lo: datetime, hi: datetime) -> _StoriaOmac:
    return _StoriaOmac(st, lo, hi)


def _worst_quality_valve(per_valve: list[dict[str, Any]]) -> dict[str, Any]:
    """La valvola con la qualita' MISURATA piu' bassa nella finestra.

    E' una **misura**, non un'attribuzione: dice quale valvola ha il tasso di
    `fill_quality_ok` piu' basso su questa finestra, e nient'altro. Non dice, e
    non va letto come, "la perdita di qualita' di macchina e' causata da questa
    valvola": una valvola su 35 pesa al massimo il 2,9% del totale prodotto, e
    una valvola a qualita' 0,000 per 15 ore muove la Q di macchina di 0,7
    punti. Chi consuma la route ha quindi bisogno di entrambi i numeri, non di
    un colpevole.

    Valvole senza cicli nella finestra sono escluse (qualita' non misurata, non
    "peggiore"). A parita' di valore vince il valve_id piu' basso (ordine
    deterministico). Nessuna valvola misurata → tutti i campi null.
    """
    misurate = [v for v in per_valve if v["quality"] is not None]
    if not misurate:
        return {"worst_valve": None, "worst_valve_quality": None,
                "worst_valve_total": None}
    w = min(misurate, key=lambda v: (v["quality"], v["valve_id"]))
    return {"worst_valve": w["valve_id"], "worst_valve_quality": w["quality"],
            "worst_valve_total": w["total"]}


def _round1(v):
    return round(v, 1) if v is not None else None


def _round3(v):
    return round(float(v), 3) if v is not None else None


def _compute_oee_window(st: Storage, start: datetime, end: datetime,
                        speed_target: float,
                        speed_target_source: str = "default",
                        per_valve: bool = False,
                        run: str | None = None,
                        run_reason: str | None = None,
                        counter: "_CycleCounts | None" = None,
                        storia: "_StoriaOmac | None" = None) -> dict[str, Any]:
    """OEE su [start, end) — pura lettura DB, riusata per window e prev.

    Regole (oee-backend-spec §D, ratificate):
    - Availability = running_s / planned_s; planned_s = somma degli
      intervalli [entered_ts, exited_ts) clippati su [start, end) di TUTTI
      gli stati OMAC della history (semplificazione POC documentata:
      Idle/Stopping/Stopped lunghi inclusi nel denominatore — un fermo
      lungo nella finestra fa scendere A, segnale corretto). History vuota
      → availability None (degraded).
    - Performance = real / theoretical; real = COUNT(cycles.event_ts in
      finestra), theoretical = speed_target × running_h. Se cycles.event_ts
      non disponibile → real None (degraded).
    - Quality = good / total su cycles (fill_quality_ok TRUE), con la stessa
      misura disaggregata per valvola in `quality_detail` (vedi
      `_worst_quality_valve`: misura, mai attribuzione). `per_valve=True`
      aggiunge la lista completa delle 35 valvole; di default resta `null`
      perche' la serie temporale la moltiplicherebbe per ogni punto.
    - OEE = A × P × Q solo se tutti e tre non-null.
    - Copertura: `planned_s` somma solo la finestra che ha storia dietro. La
      parte scoperta (bordo sinistro dello storico o lacuna in mezzo: lo
      stesso fatto) si DICHIARA — `availability_detail.window_s/uncovered_s/
      coverage` e `source.window_partial` — e non si conta come fermo. Una
      finestra parziale NON e' degradata: i tre numeri escono lo stesso.
    """
    reasons: list[str] = []
    # La lettura della history e' protetta come quella dei cicli: su
    # un'installazione parziale la tabella puo' non esistere, e il contratto
    # di questa route e' degradare con reason — mai un 500, mai un 404.
    history_ko = False
    try:
        transitions = (storia.finestra(start, end) if storia is not None
                       else st.get_machine_state_history(start, end))
    except SQLAlchemyError as exc:
        transitions = []
        history_ko = True
        reasons.append(f"availability: machine_state_history non disponibile ({exc.__class__.__name__})")

    # -- Availability (history OMAC) ---------------------------------------
    running_s: float | None = None
    planned_s = 0.0
    by_state: dict[str, float] = {}
    for i, t in enumerate(transitions):
        seg_start = max(t["entered_ts"], start)
        next_entered = (transitions[i + 1]["entered_ts"]
                        if i + 1 < len(transitions) else None)
        exit_ts = t["exited_ts"]
        seg_end = min(end, exit_ts or end, next_entered or end)
        if seg_end <= seg_start:
            continue
        secs = (seg_end - seg_start).total_seconds()
        planned_s += secs
        by_state[t["state_label"]] = by_state.get(t["state_label"], 0.0) + secs
        if t["state_code"] == OMAC_RUNNING_CODE:
            running_s = (running_s or 0.0) + secs
    # -- Quanta finestra ha davvero una storia dietro ------------------------
    # Il ciclo qui sopra somma in `planned_s` SOLO gli intervalli coperti da
    # righe di history. Un tratto senza righe — all'inizio dello storico o per
    # una lacuna in mezzo, che sono lo stesso fatto — non entra da nessuna
    # parte: il denominatore si accorcia e la disponibilita' sale in silenzio.
    # Non si conta il mancante come fermo (sarebbe un numero inventato): si
    # dichiara quanto manca, e chi legge decide.
    window_s = (end - start).total_seconds()
    uncovered_s = window_s - planned_s
    if uncovered_s <= COVERAGE_TOLLERANZA_S:
        uncovered_s = 0.0
    coverage = (round(planned_s / window_s, 3) if window_s > 0 else None)
    # `history_ko` e' un guasto di lettura, non una lacuna misurata: li' la
    # copertura non e' nota e non si marchia.
    window_partial = (not history_ko) and uncovered_s > 0

    availability: float | None = None
    if history_ko:
        pass                                   # motivo gia' registrato sopra
    elif not transitions:
        reasons.append("nessun cambio di stato macchina registrato in questa finestra")
    elif planned_s <= 0:
        reasons.append("availability: intervalli OMAC vuoti nella finestra")
    else:
        availability = round(running_s / planned_s, 3) if running_s else 0.0
    if window_partial:
        reasons.append(
            f"finestra parziale: {uncovered_s / 3600.0:.1f} h su "
            f"{window_s / 3600.0:.1f} h senza storia di stato macchina "
            "(i valori misurano solo la parte coperta)")

    # -- Performance / Quality (tabella cycles, event_ts) -------------------
    # Run ambiguo: NON si interroga `cycles` (una query senza filtro darebbe
    # un numero plausibile e sbagliato). A e' indipendente dal run e resta
    # calcolata; P e Q degradano con motivo.
    cycles = (None if run_reason
              else _count_cycles(st, start, end, run, counter=counter))
    real: int | None = None
    good = total = 0
    with_ts = False
    valvole: list[dict[str, Any]] = []
    if cycles is not None:
        total, good, with_ts, valvole = cycles
        if not with_ts:
            reasons.append("i cicli registrati non hanno un istante associato: "
                           "prestazione e qualita' non calcolabili")
        else:
            real = total
    elif run_reason:
        reasons.append(f"{run_reason} ({RUN_AMBIGUO_HINT}): prestazione e "
                       "qualita' non calcolabili")
    else:
        reasons.append("storico dei cicli non disponibile: prestazione e qualita' "
                       "non calcolabili")

    running_h = (running_s / 3600.0 if running_s is not None else None)
    theoretical = (round(speed_target * running_h, 1)
                   if running_h is not None else None)
    performance: float | None = None
    ratio_osservato: float | None = None
    if real is not None:
        if theoretical is not None and theoretical > 0:
            ratio_osservato = round(real / theoretical, 3)
            # Un target NON verificato (costante di default, nessun KV
            # `speed_target` persistito) che produce un rapporto > 1 non
            # misura una macchina veloce: misura un target incoerente con
            # l'impianto. Moltiplicarlo dentro l'OEE fabbricherebbe un
            # numero senza significato (osservato: OEE 194% su run sane).
            # Si degrada con motivo, e il rapporto grezzo resta esposto in
            # `performance_detail.ratio_osservato`: niente viene nascosto.
            if (speed_target_source == "default"
                    and ratio_osservato > PERFORMANCE_RATIO_IMPLAUSIBILE):
                reasons.append(
                    "performance: rapporto osservato "
                    f"{ratio_osservato} > {PERFORMANCE_RATIO_IMPLAUSIBILE} "
                    "con speed_target non verificato "
                    f"({speed_target} cph, costante di default: nessun KV "
                    "`speed_target` persistito). Il target non e' coerente "
                    "con la cadenza dei cicli: persistere il KV "
                    "`speed_target` reale dell'impianto")
            else:
                performance = ratio_osservato
        else:
            reasons.append("la macchina non e' mai stata in marcia in questa finestra")
    quality: float | None = None
    if total > 0:
        quality = round(good / total, 3)
    elif cycles is not None and with_ts:
        reasons.append("nessun ciclo prodotto in questa finestra")

    oee = None
    if (availability is not None and performance is not None
            and quality is not None):
        oee = round(availability * performance * quality, 3)
    degraded = (availability is None or performance is None
                or quality is None)
    if degraded and not reasons:
        reasons.append("dati insufficienti")
    return {
        "availability": availability,
        "availability_detail": {
            "running_s": _round1(running_s),
            "planned_s": _round1(planned_s),
            "window_s": _round1(window_s),
            "uncovered_s": _round1(uncovered_s),
            "coverage": coverage,
            "by_state": {k: _round1(v) for k, v in by_state.items()},
        },
        "performance": performance,
        "performance_detail": {
            "real": real, "theoretical": theoretical,
            "speed_target": speed_target,
            "speed_target_source": speed_target_source,
            "ratio_osservato": ratio_osservato,
            "running_h": round(running_h, 3) if running_h is not None else None,
        },
        "quality": quality,
        "quality_detail": {
            "good": good, "total": total,
            **_worst_quality_valve(valvole),
            "per_valve": valvole if per_valve else None,
        },
        "oee": oee,
        "source": {
            "cycles_rows": total if cycles is not None else 0,
            # QUALE run ha risposto: senza questo campo due risposte
            # numericamente diverse sono indistinguibili.
            "run_id": run,
            "state_transitions": len(transitions),
            # Parziale NON e' degradato: A, P e Q escono tutti e tre, sulla
            # parte coperta. Se `degraded` diventasse vero qui, ogni pagina
            # che nasconde i valori degradati smetterebbe di mostrare l'OEE
            # all'inizio dello storico.
            "window_partial": window_partial,
            "degraded": degraded,
            "reason": "; ".join(reasons) if reasons else None,
        },
    }


def _as_utc(v: datetime) -> datetime:
    """ISO naive → interpretato come UTC (mai come ora locale del server)."""
    return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v


def _contatore_cicli(st: Storage, run: str | None, window: str,
                     ats: list[datetime], passo: timedelta) -> _CycleCounts:
    """Il fornitore di conteggi per UNA finestra (`/machine/oee`).

    Caso particolare di `_contatore_richiesta` con una richiesta sola; resta
    come nome perche' e' la porta d'ingresso di `/machine/oee`.
    """
    return _contatore_richiesta(st, run, [(window, ats, passo)])


def _contatore_richiesta(
        st: Storage, run: str | None,
        richieste: list[tuple[str, list[datetime], timedelta]]) -> _CycleCounts:
    """Il fornitore di conteggi per una richiesta OEE, riepilogo se disponibile.

    Un solo posto costruisce il contatore, così `/machine/oee` e ogni punto di
    `/machine/oee/series` leggono i cicli nello stesso modo: due strade
    diverse sarebbero due numeri che possono divergere.

    Ogni `at` porta due finestre (corrente e precedente, vedi `_oee_payload`):
    si dichiarano tutte in anticipo perché i bordi parziali si leggono in
    un'interrogazione sola (`_CycleCountsRollup.prepara`).

    Ricaduta: se il riepilogo non copre la richiesta si resta su
    `_CycleCountsBucketed`, cioè il comportamento precedente a questo lavoro —
    più lento, mai diverso.

    **Un contatore per RICHIESTA, non per finestra (2026-08-21).**
    `/machine/oee/series` serve per default `shift` e `day`, e i loro `at`
    stanno sulla stessa griglia (il passo diradato e' 8 h per entrambe): i
    bordi parziali da leggere in `cycles` sono quindi gli STESSI istanti. Con
    un contatore per finestra venivano letti due volte — misurato il
    2026-08-21: 3,5 milioni di righe di indice per finestra, 6,8 s ciascuna.
    Dichiarare qui tutte le finestre di tutte le `windows` chieste le fa
    entrare in una preparazione sola. Il `grain` e' il gcd di tutti i passi e
    di tutte le ampiezze, quindi ogni bordo di finestra cade ancora su un
    bordo di secchiello: la ricaduta bucketed resta esatta come prima.
    """
    finestre: list[tuple[datetime, datetime]] = []
    secondi: list[int] = []
    for window, ats, passo in richieste:
        interval = WINDOW_INTERVALS[window]
        secondi.append(int(passo.total_seconds()))
        secondi.append(int(interval.total_seconds()))
        for at in ats:
            finestre.append((at - interval, at))
            finestre.append((at - 2 * interval, at - interval))
    lo = min(s for s, _ in finestre)
    hi = max(e for _, e in finestre)
    grain = timedelta(seconds=gcd(*secondi))
    c = _CycleCountsRollup(st, run, anchor=hi, lo=lo, grain=grain)
    c.prepara(finestre)
    return c


def _oee_payload(st: Storage, window: str, end: datetime,
                 speed_target: float, speed_target_source: str,
                 per_valve: bool = False,
                 run: str | None = None,
                 run_reason: str | None = None,
                 counter: "_CycleCounts | None" = None,
                 storia: "_StoriaOmac | None" = None) -> dict[str, Any]:
    """Le 13 chiavi della risposta di `/machine/oee` per un dato `at`.

    UNICO punto di verita' dell'OEE servito: `/machine/oee` e ogni punto di
    `/machine/oee/series` passano di qui, quindi non esistono due strade che
    possano divergere numericamente. Il test
    `test_serie_punto_identico_a_machine_oee` lo verifica campo per campo.

    `counter` e' solo il fornitore dei conteggi (una lettura condivisa fra la
    finestra corrente e la precedente, e fra i punti di una serie): non entra
    nel calcolo e non puo' cambiare un numero — vedi `_CycleCounts`.
    """
    interval = WINDOW_INTERVALS[window]
    start = end - interval
    if counter is None:
        counter = _CycleCounts(st, run)
    cur = _compute_oee_window(st, start, end, speed_target,
                              speed_target_source, per_valve=per_valve,
                              run=run, run_reason=run_reason, counter=counter,
                              storia=storia)
    prev = _compute_oee_window(st, start - interval, start, speed_target,
                               speed_target_source, per_valve=per_valve,
                               run=run, run_reason=run_reason, counter=counter,
                               storia=storia)
    oee, prev_oee = cur["oee"], prev["oee"]
    delta_pp = (round(oee - prev_oee, 3)
                if oee is not None and prev_oee is not None else None)
    return {
        "window": window,
        "at": _iso(end),
        "start": _iso(start),
        "end": _iso(end),
        "availability": cur["availability"],
        "availability_detail": cur["availability_detail"],
        "performance": cur["performance"],
        "performance_detail": cur["performance_detail"],
        "quality": cur["quality"],
        "quality_detail": cur["quality_detail"],
        "oee": oee,
        "prev": {"oee": prev_oee, "delta_pp": delta_pp},
        "source": cur["source"],
    }


@app.get("/machine/oee")
def machine_oee(
    window: OeeWindow = "shift",
    at: datetime | None = Query(
        None, description="fine finestra ISO8601 (default now UTC); "
        "start = at - intervallo"),
    per_valve: bool = Query(
        False, description="aggiunge in quality_detail.per_valve la qualita' "
        "misurata valvola per valvola sulla STESSA finestra"),
    run_id: str | None = Query(
        None, description="run da interrogare in `cycles`; default: KV "
        "`current_run_id`, oppure l'unico run presente"),
) -> dict[str, Any]:
    """OEE live per la Home L0 (spec dashboard §4bis/§7.2).

    Machine-agnostic: legge SOLO dal DB operazionale (machine_state_history
    per Availability, cycles per Performance/Quality), mai dal simulatore.
    Se i dati sono insufficienti risponde 200 con `oee: null` e
    `source.degraded: true` + reason chiaro — MAI 404.

    Risposta: window, at/start/end, availability (+detail), performance
    (+detail), quality (+detail), oee, prev {oee, delta_pp} sulla finestra
    precedente della stessa durata, source {cycles_rows,
    state_transitions, degraded, reason}.

    `window`: `hour` (1 h) · `shift` (8 h) · `day` (24 h) — vedi il commento su
    `HOUR_INTERVAL` per i numeri che giustificano `hour` e per cio' che NON
    compra.

    `quality_detail` porta sempre la qualita' della valvola con il tasso
    misurato piu' basso nella finestra (`worst_valve*`), e con
    `per_valve=true` la lista completa. E' una misura, non un'attribuzione:
    vedi `_worst_quality_valve`.

    `run_id` (opzionale): il run di `cycles` da misurare. Non dichiarato →
    KV `current_run_id`, o l'unico run presente. Con piu' run e nessuno
    indicato la risposta resta 200 con `source.degraded: true` e il motivo:
    P e Q non vengono calcolate su due run mescolati. `source.run_id`
    dichiara sempre quale run ha risposto.
    """
    end = _as_utc(at) if at is not None else datetime.now(timezone.utc)
    st = _storage()
    speed_target, speed_target_source = _oee_speed_target(st)
    run, run_reason = _resolve_run(st, run_id)
    counter = _contatore_cicli(st, run, window, [end],
                               WINDOW_INTERVALS[window])
    return _oee_payload(st, window, end, speed_target, speed_target_source,
                        per_valve=per_valve, run=run, run_reason=run_reason,
                        counter=counter)


# --- serie temporale di OEE --------------------------------------------------
# Passo e ampiezza massima per finestra. Il passo di `shift` e `day` e' quello
# della serie gia' consumata dalla dashboard accettata
# (generatore di fixture locale, `oee_series.py`); per `hour` il passo e' pari
# all'ampiezza, cioe' sovrapposizione 0% fra punti consecutivi (MISURE §c.5).
SERIES_STEP: dict[str, timedelta] = {
    "hour": timedelta(hours=1),
    "shift": timedelta(hours=1),
    "day": timedelta(hours=2),
    # Finestre larghe (2026-08-20): il passo e' il piu' fitto che ha senso
    # leggere sotto quell'ampiezza. Su `week` due punti a 12 h di distanza
    # condividono il 99,7% dei cicli, quindi infittire oltre disegnerebbe la
    # stessa curva con piu' inchiostro.
    "week": timedelta(hours=12),
    "month": timedelta(days=1),
}
# Ampiezza massima della serie. Era 24/48 ore per una sola ragione: senza
# riepilogo ogni punto contava cicli dentro `cycles`, e 60 giorni costavano
# 53,4 secondi (misura del 2026-08-20). Con `cycle_rollup_hour` il costo di un
# punto non dipende piu' dall'ampiezza della finestra, quindi il tetto puo'
# tornare a essere quello che serve al prodotto: i 60 giorni di storico.
SERIES_SPAN_MAX: dict[str, timedelta] = {
    "hour": timedelta(days=7),
    "shift": timedelta(days=60),
    "day": timedelta(days=60),
    "week": timedelta(days=60),
    "month": timedelta(days=60),
}
# Tetto per finestra: ogni punto costa 4 query (finestra + prev, history +
# cycles). Con i passi di default nessuna serie ci arriva; il tetto esiste per
# impedire che una `at` lontanissima trasformi una GET in una scansione lunga.
SERIES_MAX_POINTS = 200

REGOLA_OMISSIONE = (
    "Non si omette nulla: ogni punto e' la risposta esatta di /machine/oee "
    "per quell'`at`, incluso oee=null + source.degraded=true quando la "
    "finestra non ha cicli o transizioni OMAC utili. Omettere il punto "
    "nasconderebbe l'informazione 'qui non c'e' dato', che e' un fatto reale. "
    "La camminata all'indietro si ferma pero' al primo ciclo realmente "
    "presente, cosi' i punti degradati sono quelli genuinamente vuoti "
    "(macchina ferma) e non buchi artificiali."
)


def _first_cycle_ts(st: Storage, run: str | None = None) -> datetime | None:
    """Istante del primo ciclo del RUN in `cycles` (None se non c'e' nulla).

    Serve a limitare la camminata all'indietro della serie: prima di questo
    istante non esiste dato, quindi i punti sarebbero buchi fabbricati.
    Il filtro di run e' essenziale qui: e' l'ANCORA della camminata, e con due
    run sovrapposti il MIN globale allungherebbe la serie fino all'inizio
    dell'altro run, fabbricando punti degradati che non sono buchi reali.
    """
    sql = "SELECT MIN(event_ts) FROM cycles"
    params: dict[str, Any] = {}
    if run is not None:
        sql += " WHERE run_id = :run"
        params["run"] = run
    try:
        with st.engine.connect() as conn:
            v = conn.execute(text(sql), params).scalar()
    except SQLAlchemyError:
        return None
    return _as_utc(v) if isinstance(v, datetime) else None


def _last_cycle_ts(st: Storage, run: str | None = None) -> datetime | None:
    """Istante dell'ULTIMO ciclo del RUN in `cycles` (None se non c'e' nulla).

    Il gemello di `_first_cycle_ts`, e costa quanto lui: `MAX(event_ts)` scende
    l'indice `ix_cycles_run_event_ts` dalla coda e si ferma al primo elemento.
    Serve a distinguere due cose che il riepilogo orario confonde: le ore che
    mancano perche' non sono ancora state riassunte (ignoranza) e le ore che
    mancano perche' il run e' finito (fatto).
    """
    sql = "SELECT MAX(event_ts) FROM cycles"
    params: dict[str, Any] = {}
    if run is not None:
        sql += " WHERE run_id = :run"
        params["run"] = run
    try:
        with st.engine.connect() as conn:
            v = conn.execute(text(sql), params).scalar()
    except SQLAlchemyError:
        return None
    return _as_utc(v) if isinstance(v, datetime) else None


def _passo_serie(window: str, ampiezza: timedelta) -> timedelta:
    """Il passo effettivo: quello di `SERIES_STEP`, diradato se serve.

    `SERIES_MAX_POINTS` e' un tetto duro, e prima di questo lavoro veniva
    raggiunto **troncando la serie**: chiedere 60 giorni a passo 2 h dava 200
    punti, cioe' gli ultimi 16,6 giorni, e i restanti 43 sparivano senza che
    la risposta lo dicesse. Un grafico mutilato in silenzio e' peggio di uno
    piu' rado: qui il passo viene invece MOLTIPLICATO per il piu' piccolo
    intero che fa stare l'intera ampiezza chiesta dentro il tetto, e
    `__meta.<window>.passo` dichiara sempre quale passo ha risposto.

    Il passo resta un multiplo intero di quello di base, quindi anche il
    `grain` dei conteggi resta un multiplo: nessun bordo di finestra si sposta.
    """
    base = SERIES_STEP[window]
    if ampiezza <= timedelta(0):
        return base
    servono = int(ampiezza // base) + 1
    k = -(-servono // SERIES_MAX_POINTS)          # ceil, mai 0
    return base * max(k, 1)


def _serie_ridotta(p: dict[str, Any]) -> dict[str, Any]:
    """Le sole 5 chiavi che un grafico consuma: at + le tre componenti + oee."""
    return {"at": p["at"], "availability": p["availability"],
            "performance": p["performance"], "quality": p["quality"],
            "oee": p["oee"]}


@app.get("/machine/oee/series")
def machine_oee_series(
    at: datetime | None = Query(
        None, description="`at` del punto piu' recente (default now UTC)"),
    windows: str = Query(
        "shift,day",
        description="finestre da servire, separate da virgola "
                    "(hour|shift|day|week|month)"),
    da: datetime | None = Query(
        None, alias="from",
        description="inizio dell'intervallo esplicito (ISO8601). Con `to` "
                    "sostituisce la camminata all'indietro da `at`"),
    a: datetime | None = Query(
        None, alias="to",
        description="fine dell'intervallo esplicito (ISO8601); default `at`"),
    run_id: str | None = Query(
        None, description="run da interrogare in `cycles`; default: KV "
        "`current_run_id`, oppure l'unico run presente"),
) -> dict[str, Any]:
    """Serie temporale di OEE — la stessa route, con `at` all'indietro.

    **Intervallo esplicito (`from`/`to`, 2026-08-20).** Senza, la serie sa
    dire solo "quanto indietro rispetto ad adesso", e un selettore di periodo
    nella dashboard non e' costruibile: non c'e' modo di chiedere "la settimana
    del 12 luglio". Con `from`/`to` i punti coprono l'intervallo dichiarato;
    `to` da solo equivale ad `at`. Il passo resta quello di `SERIES_STEP`,
    diradato se l'intervallo chiesto non ci sta in `SERIES_MAX_POINTS` punti
    (vedi `_passo_serie`: si dirada, non si tronca).

    Zero semantica nuova: ogni punto e' **la risposta esatta** di
    `/machine/oee` con quell'`at` e quella `window` (stessa funzione,
    `_oee_payload`), quindi non esiste alcuna possibilita' che la serie e il
    valore corrente divergano.

    Risposta: per ogni finestra richiesta due liste — `<window>` con i punti
    interi (le 13 chiavi di `/machine/oee`) e `<window>_ridotto` con
    `at + availability + performance + quality + oee`. Default
    `windows=shift,day`, cioe' esattamente `{shift, shift_ridotto, day,
    day_ridotto}`. `__meta` documenta passo, copertura e punti degradati.

    Regola di prodotto (dichiarata in `__meta.regola_omissione`): non si omette
    nulla. Un punto senza cicli o senza transizioni OMAC viene emesso comunque,
    con `oee: null` e `source.degraded: true`.

    `run_id` (opzionale, stessa semantica di `/machine/oee`): il run di
    `cycles` da misurare. `__meta.run_id` dichiara quale run ha risposto e
    `__meta.run_reason` il motivo se e' rimasto ambiguo.
    """
    end = _as_utc(at) if at is not None else datetime.now(timezone.utc)
    if a is not None:
        end = _as_utc(a)
    inizio = _as_utc(da) if da is not None else None
    if inizio is not None and inizio >= end:
        raise HTTPException(
            status_code=422,
            detail=f"intervallo vuoto: from={_iso(inizio)} non e' precedente "
                   f"a to={_iso(end)}")
    chieste = [w.strip() for w in windows.split(",") if w.strip()]
    ignote = [w for w in chieste if w not in WINDOW_INTERVALS]
    if ignote or not chieste:
        raise HTTPException(
            status_code=422,
            detail=f"windows non valido: {windows!r}; valori ammessi "
                   f"{sorted(WINDOW_INTERVALS)}")
    st = _storage()
    speed_target, speed_target_source = _oee_speed_target(st)
    run, run_reason = _resolve_run(st, run_id)
    primo = _first_cycle_ts(st, run) if not run_reason else None
    out: dict[str, Any] = {"__meta": {
        "at_corrente": _iso(end),
        "primo_ciclo_reale": _iso(primo),
        "run_id": run,
        "run_reason": run_reason,
        "speed_target": speed_target,
        "speed_target_source": speed_target_source,
        "origine": "GET /machine/oee richiamata con `at` camminante "
                   "all'indietro (stessa funzione, un solo punto di verita')",
        "regola_omissione": REGOLA_OMISSIONE,
    }}
    out["__meta"]["intervallo_esplicito"] = (
        None if inizio is None else {"from": _iso(inizio), "to": _iso(end)})
    # Passo e istanti di OGNI finestra chiesta, prima di leggere qualunque
    # cosa: i conteggi e la history si preparano una volta sola per l'intera
    # richiesta, non una volta per finestra.
    piano: list[tuple[str, list[datetime], timedelta]] = []
    for window in chieste:
        # copertura: mai prima del primo ciclo reale. Senza cicli si emette
        # comunque UN punto (che sara' degradato): e' il fatto "non c'e' dato".
        # Con `from` esplicito il bordo sinistro e' quello chiesto, sempre
        # limitato al primo ciclo reale: chiedere un periodo antecedente al run
        # non deve fabbricare punti.
        copertura = (end - primo) if primo is not None else timedelta(0)
        if inizio is not None:
            copertura = min(copertura, end - inizio) if primo is not None \
                else timedelta(0)
        ampiezza = min(SERIES_SPAN_MAX[window], max(copertura, timedelta(0)))
        passo = _passo_serie(window, ampiezza)
        n = min(int(ampiezza // passo) + 1, SERIES_MAX_POINTS)
        piano.append((window, [end - k * passo for k in range(n)], passo))
    # I conteggi di TUTTI i punti di TUTTE le finestre in una preparazione
    # sola: ore intere dal riepilogo orario, bordi parziali letti da `cycles`
    # in un solo statement (vedi `_CycleCountsRollup`). Senza riepilogo
    # utilizzabile si ricade sulla lettura in secchielli di prima.
    counter = _contatore_richiesta(st, run, piano)
    # La history OMAC dell'INTERO arco coperto, letta una volta. Ogni punto ne
    # prende la sua fetta in memoria: la tabella ha 300 righe in tutto, mentre
    # i punti sono fino a 200 per finestra — 400 andate e ritorni per leggere
    # sempre lo stesso pugno di righe (misurato: 1,1 s per finestra).
    storia = _storia_omac(st, min(min(a) for _, a, _ in piano)
                          - 2 * max(WINDOW_INTERVALS[w] for w, _, _ in piano),
                          end)
    for window, ats, passo in piano:
        n = len(ats)
        punti = [_oee_payload(st, window, t, speed_target,
                              speed_target_source, run=run,
                              run_reason=run_reason, counter=counter,
                              storia=storia)
                 for t in ats]
        punti.reverse()                      # ordine cronologico crescente
        deg = [p for p in punti if p["source"]["degraded"]]
        out["__meta"][window] = {
            "passo": f"{int(passo.total_seconds() // 60)}min",
            "passo_base": f"{int(SERIES_STEP[window].total_seconds() // 60)}min",
            "conteggi_da": ("cycle_rollup_hour + bordi da cycles"
                            if isinstance(counter, _CycleCountsRollup)
                            and counter._attivo else "cycles"),
            "ampiezza_finestra": f"{int(WINDOW_INTERVALS[window].total_seconds() // 60)}min",
            "punti": n,
            "primo_at": punti[0]["at"],
            "ultimo_at": punti[-1]["at"],
            "punti_degradati": len(deg),
            "motivi_degrado": sorted({p["source"]["reason"] for p in deg
                                      if p["source"]["reason"]}),
        }
        out[window] = punti
        out[f"{window}_ridotto"] = [_serie_ridotta(p) for p in punti]
    return out


# --- qualita' per valvola nel tempo ------------------------------------------
# Perche' esiste una route a parte invece di un flag su `/machine/oee/series`:
# quella serie e' ancorata a un `at` che cammina all'indietro e ogni suo punto
# e' una finestra MOBILE larga quanto la `window` (24 h per `day`), quindi due
# punti consecutivi condividono quasi tutti i cicli. Va bene per leggere "come
# sta andando adesso", non per disegnare 60 giorni: una caduta di qualita' vi
# appare spalmata sull'ampiezza della finestra invece che nel giorno in cui e'
# avvenuta. Qui i secchielli sono invece CONTIGUI e disgiunti — una partizione
# del periodo — ed e' la forma che un grafico per valvola richiede.
GRAIN_TRUNC = {"hour": "hour", "day": "day", "week": "week"}
GRAIN_DURATA = {"hour": timedelta(hours=1), "day": timedelta(days=1),
                "week": timedelta(days=7)}
# Ordine di promozione quando il periodo chiesto non ci sta in
# `SERIES_MAX_POINTS`: si dirada la grana, non si tronca il periodo (stessa
# regola di `_passo_serie` — un grafico mutilato in silenzio e' peggio di uno
# piu' rado).
GRAIN_ORDINE = ("hour", "day", "week")

# Quanta coda non riassunta si accetta di leggere direttamente da `cycles`.
# Il riepilogo si ferma all'ultima ora COMPLETA, quindi in esercizio normale la
# coda vale meno di un'ora: una discesa di indice. Il tetto serve solo al caso
# patologico del riepilogo rimasto indietro di giorni, dove la lettura diretta
# tornerebbe a costare una scansione — li' si preferisce il taglio dichiarato.
CODA_DIRETTA_MAX = timedelta(hours=26)


def _floor_grana(t: datetime, grain: str) -> datetime:
    """Inizio del secchiello di grana `grain` che contiene `t`, in UTC.

    Gli stessi bordi che produce `date_trunc(..., ... AT TIME ZONE 'UTC')` in
    SQL: il giorno comincia a mezzanotte UTC e la settimana il lunedi', come
    `date_trunc('week', ...)`. Devono coincidere cifra per cifra, altrimenti la
    lista di istanti costruita qui in Python e i secchielli aggregati dal
    database si sfaserebbero e un grafico allineerebbe valori sbagliati.
    """
    t = _as_utc(t)
    if grain == "hour":
        return t.replace(minute=0, second=0, microsecond=0)
    g = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return g if grain == "day" else g - timedelta(days=g.weekday())


def _ceil_grana(t: datetime, grain: str) -> datetime:
    f = _floor_grana(t, grain)
    return f if f == _as_utc(t) else f + GRAIN_DURATA[grain]


@app.get("/valves/quality/series")
def valves_quality_series(
    da: datetime = Query(
        ..., alias="from", description="inizio del periodo (ISO8601)"),
    a: datetime = Query(
        ..., alias="to", description="fine del periodo, esclusa (ISO8601)"),
    grain: Literal["hour", "day", "week"] = Query(
        "day", description="ampiezza dei secchielli, contigui e disgiunti"),
    run_id: str | None = Query(
        None, description="run da interrogare; default: KV `current_run_id`, "
        "oppure l'unico run presente"),
) -> dict[str, Any]:
    """Qualita' (`good/total`) di OGNI valvola, secchiello per secchiello.

    Serve la domanda che nessuna route sapeva rispondere: **come si e'
    comportata la singola valvola nel tempo**. `/machine/oee/series` porta i
    totali di macchina e `quality_detail.per_valve` resta `null` nella serie;
    senza questi numeri l'andamento delle 35 valvole sui 60 giorni non e'
    disegnabile.

    Si legge SOLO `cycle_rollup_hour` (piu' `cycles` per una verifica di
    copertura da una riga): il riepilogo e' ~34.000 righe contro 36 milioni,
    quindi qualunque periodo costa millisecondi. Con `grain=hour` i secchielli
    sono le righe del riepilogo; con `day` e `week` sono somme di ore, fatte in
    SQL con `date_trunc(... AT TIME ZONE 'UTC')` — il troncamento esplicito in
    UTC non e' pignoleria: su un fuso a mezz'ora produrrebbe secchielli sfasati
    rispetto a quelli che questa funzione costruisce in Python.

    **Il periodo viene allineato ai bordi dei secchielli**, verso l'esterno: un
    secchiello o c'e' tutto o non c'e'. `from`/`to` in risposta sono quelli
    EFFETTIVI, non quelli chiesti, cosi' chi disegna sa dove comincia il primo
    istante senza ricalcolarlo.

    **Un secchiello senza cicli c'e' lo stesso**, con `total: 0` e `quality:
    null` (`REGOLA_OMISSIONE`: il buco e' un fatto, toglierlo lo nasconde). E
    `quality` e' `null` — mai `0.0` — quando `total` e' zero: zero cicli
    significa "non misurata", non "tutti scarti"; e' la stessa regola di
    `_compute_oee_window`. Tutte le valvole che compaiono nel periodo hanno la
    STESSA lista di istanti, cosi' un grafico le allinea senza indovinare.

    **Copertura.** Il riepilogo contiene solo ore complete, quindi l'ora in
    corso non c'e'. Quella coda non viene piu' tagliata via: si legge
    direttamente da `cycles` e si somma alle ore intere, esattamente come
    `/machine/oee` legge i bordi parziali delle sue finestre
    (`_CycleCountsRollup`). Il periodo arriva percio' fino all'ultimo ciclo che
    esiste davvero, e resta esatto — nessun arrotondamento, stesso predicato per
    riga. Oltre l'ultimo ciclo il periodo si accorcia senza dichiararsi
    degradato: `MAX(event_ts)` dice con certezza che li' non c'e' nulla, e la
    fine di un run non e' un'ignoranza. Restano `degraded` i due casi in cui
    manca davvero un'informazione: il riepilogo indietro di piu' di
    `CODA_DIRETTA_MAX` (leggerne la coda costerebbe una scansione) e `cycles`
    non interrogabile.
    A sinistra il taglio scatta solo se prima del riepilogo esistono davvero
    cicli non riassunti: se il run comincia dopo, quel vuoto e' un fatto.

    **Tetto ai punti.** Oltre `SERIES_MAX_POINTS` la grana viene PROMOSSA
    (hour → day → week) e il campo `grain` in risposta dichiara quale ha
    risposto. Non si tronca il periodo: 60 giorni a grana oraria darebbero 1.440
    punti, e servirne 200 sarebbe restituire gli ultimi otto giorni senza dirlo.
    """
    if _as_utc(a) <= _as_utc(da):
        raise HTTPException(
            status_code=422,
            detail=f"intervallo vuoto: from={_iso(_as_utc(da))} non e' "
                   f"precedente a to={_iso(_as_utc(a))}")
    st = _storage()
    run, run_reason = _resolve_run(st, run_id)
    base: dict[str, Any] = {
        "grain": grain, "from": None, "to": None, "run_id": run,
        "valves": {}, "degraded": True, "reason": None,
    }
    if run_reason:
        base["reason"] = f"{run_reason} ({RUN_AMBIGUO_HINT})"
        return base

    motivi: list[str] = []
    # Promozione della grana PRIMA di leggere: il periodo allineato dipende
    # dalla grana, quindi l'ordine conta.
    for g in GRAIN_ORDINE[GRAIN_ORDINE.index(grain):]:
        lo = _floor_grana(da, g)
        hi = _ceil_grana(a, g)
        if (hi - lo) // GRAIN_DURATA[g] <= SERIES_MAX_POINTS:
            break
    else:
        g = GRAIN_ORDINE[-1]
        lo, hi = _floor_grana(da, g), _ceil_grana(a, g)
    if g != grain:
        motivi.append(
            f"grana promossa da {grain} a {g}: il periodo chiesto vale "
            f"{int((_ceil_grana(a, grain) - _floor_grana(da, grain)) // GRAIN_DURATA[grain])} "
            f"secchielli, oltre il tetto di {SERIES_MAX_POINTS}")

    cov_lo, cov_hi, cicli_prima = _copertura_riepilogo(st, run)
    if cov_lo is None:
        base["grain"] = g
        base["reason"] = (
            f"nessuna ora riassunta in {ROLLUP_TABLE} per il run {run!r}: "
            "riempire il riepilogo con `python -m pipeline.cycle_rollup "
            f"--run-id {run} --since-last`")
        return base
    # `cov_hi` e' l'INIZIO dell'ultima ora riassunta: la copertura arriva a
    # un'ora dopo.
    fine_coperta = cov_hi + ROLLUP_BUCKET
    # La coda: le ore non ancora riassunte si leggono DIRETTAMENTE da `cycles`,
    # come fa `/machine/oee` con i bordi parziali delle sue finestre
    # (`_CycleCountsRollup._leggi_bordi`). Prima di questo lavoro il periodo
    # veniva invece tagliato all'ultima ora intera del riepilogo, e la serie per
    # valvola finiva un giorno e mezzo prima della serie di macchina.
    coda: tuple[datetime, datetime] | None = None
    if hi > _floor_grana(fine_coperta, g):
        ultimo = _last_cycle_ts(st, run)
        # `event_ts` e' l'istante di un ciclo e i secchielli sono chiusi a
        # sinistra: il dato arriva fino a `ultimo` INCLUSO.
        fine_dati = None if ultimo is None else max(
            fine_coperta, ultimo + timedelta(seconds=1))
        if fine_dati is None:
            # `cycles` non risponde: non si sa dove finisca il dato, quindi si
            # torna al taglio conservativo di prima.
            nuovo = _floor_grana(fine_coperta, g)
            motivi.append(
                f"periodo tagliato a {_iso(nuovo)}: oltre quell'istante il "
                "riepilogo non ha ore complete, e `cycles` non e' "
                "interrogabile per sapere dove finisce il dato")
            hi = nuovo
        elif fine_dati - fine_coperta > CODA_DIRETTA_MAX:
            nuovo = _floor_grana(fine_coperta, g)
            motivi.append(
                f"periodo tagliato a {_iso(nuovo)}: oltre quell'istante il "
                f"riepilogo e' indietro di piu' di {CODA_DIRETTA_MAX}, e "
                "leggerne la coda da `cycles` costerebbe una scansione; "
                "riempire il riepilogo con `python -m pipeline.cycle_rollup "
                f"--run-id {run} --since-last`")
            hi = nuovo
        else:
            if fine_dati > fine_coperta:
                coda = (fine_coperta, min(hi, fine_dati))
            # Oltre l'ultimo ciclo non c'e' nulla da servire, e non e' un
            # taglio dichiarabile: e' la fine del run. `MAX(event_ts)` lo sa con
            # certezza, quindi la risposta NON si degrada per questo.
            limite = _ceil_grana(fine_dati, g)
            if hi > limite:
                hi = limite
    if cicli_prima and lo < cov_lo:
        nuovo = _ceil_grana(cov_lo, g)
        motivi.append(
            f"periodo tagliato a sinistra a {_iso(nuovo)}: prima di "
            f"{_iso(cov_lo)} esistono cicli non ancora riassunti")
        lo = max(lo, nuovo)
    if hi <= lo:
        base["grain"] = g
        base["from"] = _iso(lo)
        base["to"] = _iso(lo)
        base["reason"] = "; ".join(motivi) or (
            "il periodo chiesto non interseca le ore riassunte")
        return base

    istanti = []
    t = lo
    while t < hi:
        istanti.append(t)
        t += GRAIN_DURATA[g]

    letti = _leggi_qualita_secchielli(st, run, lo, hi, g)
    if coda is not None and coda[1] > coda[0]:
        for valve_id, per_ts in _leggi_qualita_coda(
                st, run, coda[0], coda[1], g).items():
            dove = letti.setdefault(valve_id, {})
            for t, (tot, gd) in per_ts.items():
                if t < lo or t >= hi:
                    continue
                vecchio = dove.get(t, (0, 0))
                dove[t] = (vecchio[0] + tot, vecchio[1] + gd)
    valvole: dict[str, list[dict[str, Any]]] = {}
    for valve_id in sorted(letti):
        per_ts = letti[valve_id]
        serie = []
        for t in istanti:
            tot, gd = per_ts.get(t, (0, 0))
            serie.append({
                "at": _iso(t), "total": tot, "good": gd,
                # total == 0 → qualita' NON misurata: null, mai 0.0.
                "quality": round(gd / tot, 3) if tot > 0 else None,
            })
        valvole[str(valve_id)] = serie

    return {
        "grain": g, "from": _iso(lo), "to": _iso(hi), "run_id": run,
        "valves": valvole,
        "degraded": bool(motivi),
        "reason": "; ".join(motivi) or None,
    }


def _copertura_riepilogo(
        st: Storage, run: str | None
) -> tuple[datetime | None, datetime | None, bool]:
    """`(primo_bucket, ultimo_bucket, esistono_cicli_prima)` del run.

    Il terzo valore distingue i due modi in cui un periodo puo' cominciare
    prima del riepilogo: se prima del primo secchiello il run non ha alcun
    ciclo, quel vuoto e' un fatto e si serve come zero; se invece i cicli ci
    sono e non sono riassunti, servirli come zero sarebbe un numero inventato.
    Costa una discesa di indice (`LIMIT 1`) — stesso accorgimento di
    `_CycleCountsRollup._carica_copertura`.
    """
    cond, params = [], {}
    if run is not None:
        cond.append("run_id = :run")
        params["run"] = run
    dove = (" WHERE " + " AND ".join(cond)) if cond else ""
    try:
        with st.engine.connect() as conn:
            row = conn.execute(text(
                f"SELECT MIN(bucket_ts), MAX(bucket_ts) "
                f"FROM {ROLLUP_TABLE}{dove}"), params).first()
            if row is None or row[0] is None:
                return None, None, False
            cov_lo = _as_utc(row[0])
            prima = conn.execute(text(
                "SELECT 1 FROM cycles" +
                (dove + " AND " if cond else " WHERE ") +
                "event_ts < :cov_lo LIMIT 1"),
                {**params, "cov_lo": cov_lo}).first()
    except SQLAlchemyError:
        return None, None, False
    return cov_lo, _as_utc(row[1]), prima is not None


def _leggi_qualita_secchielli(
        st: Storage, run: str | None, lo: datetime, hi: datetime, grain: str
) -> dict[int, dict[datetime, tuple[int, int]]]:
    """`{valve_id: {inizio_secchiello: (total, good)}}` dal riepilogo orario.

    L'aggregazione sta in SQL e non in Python perche' il numero di righe da
    sommare cresce col periodo (60 giorni = 34.000 righe) mentre il risultato
    non supera mai `SERIES_MAX_POINTS × valvole`: e' il database a dover
    ridurre. Solo `cycle_rollup_hour` viene letta — nessuna scansione di
    `cycles`.
    """
    cond = ["bucket_ts >= :lo", "bucket_ts < :hi"]
    params: dict[str, Any] = {"lo": lo, "hi": hi}
    if run is not None:
        cond.insert(0, "run_id = :run")
        params["run"] = run
    # `GRAIN_TRUNC` e' un dizionario chiuso e `grain` e' gia' validato dal tipo
    # `Literal` della route: l'interpolazione non porta input dell'utente in
    # SQL. Un bind param qui costringerebbe a un cast esplicito senza aggiungere
    # sicurezza.
    trunc = GRAIN_TRUNC[grain]
    with st.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT valve_id, "
            f"  (date_trunc('{trunc}', bucket_ts AT TIME ZONE 'UTC') "
            "     AT TIME ZONE 'UTC') AS b, "
            "  SUM(total) AS total, SUM(good) AS good "
            f"FROM {ROLLUP_TABLE} WHERE " + " AND ".join(cond) +
            " GROUP BY valve_id, b"), params).all()
    acc: dict[int, dict[datetime, tuple[int, int]]] = {}
    for valve_id, b, total, good in rows:
        acc.setdefault(int(valve_id), {})[_as_utc(b)] = (int(total), int(good))
    return acc


def _leggi_qualita_coda(
        st: Storage, run: str | None, lo: datetime, hi: datetime, grain: str
) -> dict[int, dict[datetime, tuple[int, int]]]:
    """La stessa forma di `_leggi_qualita_secchielli`, ma letta da `cycles`.

    Copre l'intervallo `[lo, hi)` che il riepilogo orario non ha ancora
    riassunto: per costruzione meno di un'ora (il riepilogo si ferma all'ultima
    ora completa), e comunque mai piu' di `CODA_DIRETTA_MAX`, che e' il
    chiamante a garantire. Il predicato per riga e la troncatura sono identici a
    quelli del riepilogo (`event_ts >= lo AND event_ts < hi`, stesso `run_id`,
    stesso `date_trunc(... AT TIME ZONE 'UTC')`), percio' i conteggi si sommano
    a quelli delle ore intere senza doppioni e senza sfasamenti: la coda e' un
    pezzo dell'identita', non una stima.
    """
    cond = ["event_ts >= :lo", "event_ts < :hi"]
    params: dict[str, Any] = {"lo": lo, "hi": hi}
    if run is not None:
        cond.insert(0, "run_id = :run")
        params["run"] = run
    # `grain` e' gia' validato dal `Literal` della route e `GRAIN_TRUNC` e' un
    # dizionario chiuso: nessun input dell'utente finisce in SQL.
    trunc = GRAIN_TRUNC[grain]
    try:
        with st.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT valve_id, "
                f"  (date_trunc('{trunc}', event_ts AT TIME ZONE 'UTC') "
                "     AT TIME ZONE 'UTC') AS b, "
                "  COUNT(*) AS total, "
                "  COUNT(*) FILTER (WHERE fill_quality_ok = TRUE) AS good "
                "FROM cycles WHERE " + " AND ".join(cond) +
                " GROUP BY valve_id, b"), params).all()
    except SQLAlchemyError:
        return {}
    acc: dict[int, dict[datetime, tuple[int, int]]] = {}
    for valve_id, b, total, good in rows:
        acc.setdefault(int(valve_id), {})[_as_utc(b)] = (int(total), int(good))
    return acc


def _sql_ultima_prediction(preds) -> str:
    """L'SQL che serve `last_prediction` a `GET /valves`, parametri `:n` e
    `:run_id`.

    Sta in una funzione a se' perche' e' la query che il test del piano
    interroga con `EXPLAIN`: e' il numero di righe percorse, non il tempo di
    parete, a distinguere questa forma da quella che l'ha preceduta.

    Il filtro di run (2026-08-22) accompagna quello gia' presente sulle rotte
    dei cicli: senza, la scheda di una valvola avrebbe mostrato l'ultima
    prediction di un run qualsiasi accanto ai cicli del run richiesto.
    L'indice che serve questa forma e' `ix_predictions_run_valve_wcid`.
    """
    colonne = ", ".join(f"p.{c.name}" for c in preds.c)
    return (f"SELECT {colonne} "
            "FROM generate_series(1, :n) AS v(valve_id) "
            "CROSS JOIN LATERAL ("
            "  SELECT * FROM predictions p2 "
            "  WHERE p2.valve_id = v.valve_id "
            "    AND (CAST(:run_id AS VARCHAR) IS NULL "
            "         OR p2.run_id = CAST(:run_id AS VARCHAR)) "
            "  ORDER BY p2.window_end_cycle_id DESC LIMIT 1"
            ") p")


@app.get("/health")
def health() -> dict[str, Any]:
    ok = _storage().ping()
    return {"status": "ok" if ok else "degraded", "db": ok}


@app.get("/machine/state")
def machine_state() -> dict[str, Any]:
    st = _storage()
    state = st.get_machine_state("omac_state")
    if state is None:
        raise HTTPException(status_code=404, detail="stato macchina non disponibile")
    return state


@app.get("/valves")
def list_valves(
    run_id: str | None = Query(
        None, description="run da interrogare in `cycles` per `last_kpi`; "
        "default: KV `current_run_id`, oppure l'unico run presente"),
) -> dict[str, Any]:
    """Catalogo FISSO delle 35 valvole (range statico 1..35, nessun metadata
    catalogo valvole nel DB) + per ciascuna: ultima prediction (se presente),
    alert attivi (status open/sustained) e ultimo KPI (via CyclesStorage).

    Niente LIMIT: l'ultima prediction per valvola è calcolata con
    DISTINCT ON (valve_id) ordinato per window_end_cycle_id DESC — nessuna
    valvola può sparire dalla vista macchina (bug C1 fix).

    `run_id` (opzionale): il run di `cycles` da cui prendere `last_kpi`. Senza
    filtro, `DISTINCT ON (valve_id) ORDER BY cycle_id DESC` restituisce il
    ciclo del run **piu' lungo**, non del piu' recente: una macchina sana
    mostrata al posto di quella guasta. Con piu' run e nessuno indicato la
    risposta resta 200: `last_kpi: null` per tutte le valvole,
    `kpi_degraded: true` + `kpi_reason`. Dal 2026-08-22 anche le prediction
    hanno un run: `last_prediction` è filtrata sullo stesso run di
    `last_kpi`, e se il run resta ambiguo il filtro non viene applicato —
    la risposta è quella di prima della colonna, mai un errore. Gli alert
    non hanno nozione di run e restano sempre serviti.
    """
    st = _storage()
    catalog: dict[int, dict[str, Any]] = {
        valve_id: {"valve_id": valve_id, "last_prediction": None,
                   "active_alerts": [], "last_kpi": None}
        for valve_id in range(1, 36)
    }
    # Il run si risolve PRIMA della query sulle prediction: da quando
    # `predictions` ha un discriminante di run (2026-08-22) le due meta' di
    # questa risposta — `last_prediction` e `last_kpi` — devono guardare lo
    # stesso run, altrimenti la scheda accosta la diagnosi di un run ai
    # cicli di un altro. Con run ambiguo il filtro resta None e si legge la
    # tabella intera, come faceva prima della colonna.
    run, run_reason = _resolve_run(st, run_id)
    pred_run = run if run_reason is None else None
    with st.engine.connect() as conn:
        preds = st.predictions
        alerts_t = st.alerts
        # L'ultima prediction di OGNI valvola, con un LATERAL sull'insieme
        # completo delle valvole. Zero LIMIT sul risultato: la lista di
        # partenza e' `generate_series(1, 35)`, cioe' il catalogo, non le
        # righe di `predictions` — una valvola senza alcuna prediction non
        # produce riga qui ed esce dal catalogo con `last_prediction: null`,
        # esattamente come prima (bug C1 fix, invariato).
        #
        # PERCHE' UN LATERAL E NON `DISTINCT ON` (misurato il 2026-08-21 su
        # 723.110 righe): il piano di `DISTINCT ON (valve_id) ORDER BY
        # valve_id, window_end_cycle_id DESC` non sa saltare — il nodo
        # `Unique` percorre TUTTE le righe dell'indice per tenerne 35
        # (44.410 buffer letti, 12,9 s). Non e' un indice mancante:
        # `ix_predictions_valve_wcid` esiste ed e' quello che il piano usa.
        # Il LATERAL chiede l'ultima prediction di UNA valvola per volta,
        # cioe' 35 discese di indice. E' la stessa patologia gia'
        # diagnosticata e risolta su `cycles`
        # (`CyclesStorage.latest_kpi_by_valve`, 84,7 s -> 0,7 ms).
        rows = conn.execute(
            text(_sql_ultima_prediction(preds)),
            {"n": 35, "run_id": pred_run}).fetchall()
        active_filters = [alerts_t.c.status.in_(_ACTIVE_ALERT_STATUSES)]
        if pred_run is not None:
            active_filters.append(alerts_t.c.run_id == pred_run)
        active = conn.execute(select(alerts_t).where(*active_filters)).fetchall()
    for r in rows:
        d = _row_to_dict(r)
        d["prediction_ts"] = _iso(d["prediction_ts"])
        catalog[d["valve_id"]]["last_prediction"] = d
    for a in active:
        d = _row_to_dict(a)
        for col in ("opened_ts", "last_seen_ts", "closed_ts"):
            d[col] = _iso(d[col])
        catalog[d["valve_id"]]["active_alerts"].append(d)
    # ultimo KPI per valvola via CyclesStorage (W2, stesso pool): se il
    # modulo/tabella non è disponibile, si degrada a last_kpi: None.
    cs = _cycles_store(st)
    kpi_reason = run_reason
    if cs is not None and run_reason is None:
        try:
            latest_kpi = cs.latest_kpi_by_valve(run)
        except Exception:
            latest_kpi = {}
        for valve_id, kpi in latest_kpi.items():
            if valve_id in catalog:
                catalog[valve_id]["last_kpi"] = _kpi_iso(kpi)
    return {"valves": catalog, "run_id": run,
            "kpi_degraded": kpi_reason is not None,
            "kpi_reason": (f"{kpi_reason} ({RUN_AMBIGUO_HINT})"
                           if kpi_reason else None)}


# --- KPI della baseline sana ------------------------------------------------
# Solo grandezze osservabili: nessuna ground truth, nessuna etichetta di
# scenario. Sono gli stessi nomi operazionali della tabella `cycles`.
NEWLINE = chr(10)

_BASELINE_KPI = ("filling_time_ms", "tail_time_ms", "tail_pulse",
                 "pulse_count", "delta_pulse", "filling_step_out")

# Numero di cicli su cui i limiti XmR sono leggibili: 46 = periodo
# dell'oscillazione del driver (`driver_period_rot`, plcsim/config.py, valore
# ricavato per FFT). Costante RIPETUTA qui e non importata: l'API non importa
# il simulatore (invariante §84-85, legge solo dal DB).
N_CICLI_DI_RIFERIMENTO = 46

XMR_NOTE = (
    "ucl/lcl sono limiti su una MEDIA di ~46 cicli (n_cicli_di_riferimento), "
    "non sul singolo ciclo: sui dati sani il 73-78% dei cicli cade fuori da "
    "essi, quindi una vista che segnalasse 'ciclo fuori limite' mostrerebbe in "
    "allarme tre quarti dei cicli buoni. La dispersione del singolo ciclo e' "
    "`sigma_full`; quella della media di 46 cicli e' `sigma_media_46`, "
    "MISURATA sui dati (sd delle medie a blocchi pieni di 46 cicli "
    "consecutivi, ddof=1) e non derivata con la regola 1/sqrt(n): quella "
    "regola sbaglia di 23x a n=10 perche' l'oscillazione del driver e' "
    "coerente su cicli vicini."
)


def _run_filter_sql(run: str | None) -> str:
    """Predicato di run da mettere nel WHERE della CTE, prima delle finestre.

    **Dove sta il filtro conta.** In PostgreSQL il `WHERE` di un livello di
    query e' valutato PRIMA delle window function dello stesso livello:
    mettendo `run_id = :run` nel `WHERE` della CTE, `LAG`/`ROW_NUMBER` vedono
    solo le righe del run. Se il filtro stesse fuori (nella SELECT esterna),
    la finestra scorrerebbe attraverso la giunzione fra due run e nascerebbe
    un moving range |x[i]-x[i-1]| che non corrisponde ad alcuna transizione
    fisica: MRbar gonfiato, UCL/LCL (= media +- 2.66*MRbar) piu' larghi,
    baseline piu' PERMISSIVA, cioe' guasti veri che smettono di essere
    segnalati. Le `PARTITION BY` includono comunque `run_id` come seconda
    difesa: cosi' la finestra resta corretta anche se un giorno il filtro
    venisse spostato o dimenticato.
    """
    return "      AND run_id = :run" + NEWLINE if run is not None else ""


def _partition_sql(run: str | None) -> str:
    """`PARTITION BY` delle finestre analitiche — con `run_id` in testa."""
    return "run_id, valve_id" if run is not None else "valve_id"


def _sigma_media_sql(run: str | None = None) -> str:
    """SQL della sd empirica delle medie a blocchi di N cicli consecutivi.

    Perche' misurata e non derivata: la serie non e' iid — c'e' un'oscillazione
    del driver con periodo 46 cicli, quindi la media di n cicli non ha
    dispersione sigma/sqrt(n). Misurato su una run sana (35 valvole,
    `MISURE-b-c.md` §b.6): a n=46 la sd empirica delle medie e' 1,63-5,76 ms
    (mediana 1,79) mentre sigma_full/sqrt(46) darebbe mediana 10,41 ms
    (sovrastima 5,8x) e a n=10 la regola sbaglia di 23x in senso
    anticonservativo. L'unico numero onesto e' quello letto dai dati.

    Solo blocchi PIENI (n_blocco = :n): un blocco parziale in coda avrebbe una
    media piu' rumorosa e gonfierebbe la sd. STDDEV_SAMP (ddof=1) come nelle
    misure di riferimento; con meno di 2 blocchi pieni ritorna NULL, che la
    route pubblica come `null` + motivo — mai un numero plausibile.
    """
    sep = ',' + NEWLINE + ' ' * 11
    medie = sep.join(f"AVG({k}::double precision) AS {k}_bm"
                     for k in _BASELINE_KPI)
    sd = sep.join(f"STDDEV_SAMP({k}_bm) AS {k}_sd" for k in _BASELINE_KPI)
    colonne = ', '.join(_BASELINE_KPI)
    righe = [
        'WITH numerati AS (',
        '    SELECT valve_id,',
        '           (ROW_NUMBER() OVER (PARTITION BY '
        f'{_partition_sql(run)} ORDER BY cycle_id) - 1)',
        '               / CAST(:n AS integer) AS blocco,',
        f'           {colonne}',
        '    FROM cycles',
        '    WHERE event_ts >= :start AND event_ts < :end'
        + NEWLINE + _run_filter_sql(run).rstrip(NEWLINE),
        '), medie AS (',
        '    SELECT valve_id, blocco, COUNT(*) AS n_blocco,',
        f'           {medie}',
        '    FROM numerati GROUP BY valve_id, blocco',
        ')',
        'SELECT valve_id, COUNT(*) AS n_blocchi,',
        f'       {sd}',
        'FROM medie WHERE n_blocco = CAST(:n AS integer)',
        'GROUP BY valve_id ORDER BY valve_id',
    ]
    return NEWLINE.join(righe)


def _baseline_sql(run: str | None = None) -> str:
    """SQL della baseline per valvola: media, sigma, mediana e MRbar.

    MRbar = media del moving range |x[i] - x[i-1]| in ordine di ciclo. E' la
    base dei limiti XmR del progetto (CONTEXT.md): UCL/LCL = media +- 2.66*MRbar.
    Si calcola con LAG in finestra per valvola, non lato applicazione, cosi'
    la serie non deve mai transitare per la rete.
    """
    sep = ',' + NEWLINE + ' ' * 11
    lag = sep.join(
        f"LAG({k}) OVER (PARTITION BY {_partition_sql(run)} "
        f"ORDER BY cycle_id) AS prev_{k}"
        for k in _BASELINE_KPI)
    agg = sep.join(
        f"AVG({k}::double precision) AS {k}_mean, "
        f"STDDEV_POP({k}::double precision) AS {k}_std, "
        f"PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {k}) AS {k}_p50, "
        f"AVG(ABS({k}::double precision - prev_{k})) AS {k}_mrbar"
        for k in _BASELINE_KPI)
    colonne = ', '.join(_BASELINE_KPI)
    righe = [
        'WITH ordinati AS (', 
        '    SELECT valve_id, cycle_id, fill_quality_ok, diagnostic_status,', 
        f'           {colonne},', 
        f'           {lag}', 
        '    FROM cycles',
        '    WHERE event_ts >= :start AND event_ts < :end'
        + NEWLINE + _run_filter_sql(run).rstrip(NEWLINE),
        ')',
        'SELECT valve_id, COUNT(*) AS n,', 
        '       AVG(CASE WHEN fill_quality_ok THEN 1.0 ELSE 0.0 END)'
        '           AS quality_rate,', 
        "       AVG(CASE WHEN diagnostic_status = 'SUSPECT' THEN 1.0"
        '            ELSE 0.0 END) AS suspect_rate,', 
        f'       {agg}', 
        'FROM ordinati GROUP BY valve_id ORDER BY valve_id', 
    ]
    return NEWLINE.join(righe)


def _baseline_window(
        st: Storage,
        start: datetime | None,
        end: datetime | None,
) -> tuple[datetime | None, datetime | None, str, str | None]:
    """Finestra sana: parametri espliciti, altrimenti il KV `baseline_window`.

    Ritorna `(start, end, source, run_id_kv)`.

    NON esiste un default temporale ("ultimi N giorni"): quale finestra sia
    sana e' una decisione umana, non una deduzione dell'API. Se nessuno l'ha
    dichiarata, la baseline non si inventa — si degrada con reason.

    **Il riferimento sano e' un RUN, non solo un intervallo di date.** I run
    del progetto partono lo stesso minuto dello stesso giorno e si
    sovrappongono nel tempo di parete: `{start, end}` da soli non
    identificano nulla. Il KV e' quindi `{run_id, start, end}`.
    Retrocompatibile: un KV vecchio in forma `{start, end}` continua a
    funzionare e il run viene risolto da `_resolve_run` (KV `current_run_id`,
    o unico run presente) — `run_id_kv` resta None.
    """
    if start is not None and end is not None:
        return start, end, "query", None
    try:
        kv = st.get_machine_state("baseline_window")
    except SQLAlchemyError:
        # KV non disponibile (tabella assente su un'installazione parziale):
        # e' un motivo di degrado, non un 500. La route non deve MAI rompersi
        # per una dipendenza opzionale.
        return None, None, "assente", None
    if isinstance(kv, dict):
        try:
            ks, ke = kv.get("start"), kv.get("end")
            krun = kv.get("run_id")
            if ks and ke:
                return (datetime.fromisoformat(str(ks).replace("Z", "+00:00")),
                        datetime.fromisoformat(str(ke).replace("Z", "+00:00")),
                        "kv", str(krun) if krun else None)
        except (TypeError, ValueError):
            pass
    return None, None, "assente", None


# --- baseline memorizzata -------------------------------------------------
# La finestra sana e' DICHIARATA e CONGELATA (KV `baseline_window`): la
# baseline e' percio' un fatto che si calcola UNA volta, non a ogni richiesta.
# Calcolarla costa ~136 s su 6,6 M cicli (misurato: due query, entrambe
# dominate dalla scansione di `cycles` e dallo spill su disco delle finestre
# analitiche). Il risultato viene quindi memorizzato nello STESSO meccanismo
# gia' usato per la finestra (KV in `machine_state`), portandosi dietro la
# CHIAVE DI VALIDITA' `{run_id, start, end}`.
#
# Regola dura: il valore memorizzato si serve SOLO se la chiave combacia
# ESATTAMENTE con la finestra richiesta. Una finestra diversa non riusa mai
# nulla — ricalcola. Mai servire i numeri di un'altra finestra.
#
# Sulla nota "nessuna scrittura qui" del docstring di modulo: questa e' l'unica
# scrittura dell'API, ed e' la memoizzazione della propria lettura, non un
# fatto nuovo — non entra nel data-plane (cycles/predictions/alerts) e il suo
# contenuto e' interamente ricalcolabile dalla chiave che porta con se'.
_BASELINE_CACHE_KEY = "baseline_cache"


def _baseline_cache_id(run: str | None, start: datetime | None,
                       end: datetime | None) -> dict[str, Any]:
    """Chiave di validita': quale run e quale finestra hanno prodotto i numeri."""
    return {"run_id": run, "start": _iso(start), "end": _iso(end)}


def _baseline_cache_read(
        st: Storage, chiave: dict[str, Any],
) -> tuple[dict[str, Any], str | None] | None:
    """Payload memorizzato se e solo se la chiave combacia esattamente."""
    try:
        kv = st.get_machine_state(_BASELINE_CACHE_KEY)
    except SQLAlchemyError:
        return None
    if not isinstance(kv, dict) or kv.get("key") != chiave:
        return None
    payload = kv.get("payload")
    if not isinstance(payload, dict):
        return None
    # JSON non ha chiavi intere: `valves` torna con chiavi stringa. Si
    # ripristinano int, cosi' il payload memorizzato e quello appena calcolato
    # sono lo STESSO oggetto Python, non due forme che si somigliano.
    valves = payload.get("valves")
    if isinstance(valves, dict):
        try:
            payload["valves"] = {int(k): v for k, v in valves.items()}
        except (TypeError, ValueError):
            return None
    ca = kv.get("computed_at")
    return payload, (str(ca) if ca else None)


def _baseline_cache_write(st: Storage, chiave: dict[str, Any],
                          payload: dict[str, Any]) -> str | None:
    """Memorizza payload+chiave. Un KV non scrivibile non e' un errore: la
    route ha gia' i numeri giusti, resta solo lenta la prossima volta."""
    computed_at = datetime.now(timezone.utc).isoformat()
    try:
        st.set_machine_state(_BASELINE_CACHE_KEY, {
            "key": chiave, "computed_at": computed_at, "payload": payload})
    except SQLAlchemyError:
        return None
    return computed_at


def _tune_baseline_conn(conn: Any) -> None:
    """`work_mem` alto per la sola transazione della baseline.

    Misurato con EXPLAIN (ANALYZE, BUFFERS) sui 6,6 M cicli della finestra
    dichiarata, con il default `work_mem = 4MB`: il sort su (valve_id,
    cycle_id) va in `external merge` (~412 MB su disco), l'HashAggregate dei
    blocchi da 46 cicli spilla 405 MB in 25 batch e il GroupAggregate finale
    (PERCENTILE_CONT su 6 KPI) scrive 1,26 GB di temp. Alzando `work_mem` a
    256 MB ogni stadio resta in memoria: 63,3 s -> 17,8 s e 73,0 s -> 13,1 s,
    cioe' 136 s -> 31 s. Il resto e' scansione pura di `cycles` e non si
    comprime (un indice peggiora: il bitmap heap scan misura 20,8 s contro i
    10,4 s del parallel seq scan).

    `SET LOCAL`: vale per la sola transazione corrente e non sporca la
    connessione quando torna nel pool. Solo PostgreSQL.
    """
    if conn.dialect.name != "postgresql":
        return
    conn.execute(text("SET LOCAL work_mem = '256MB'"))
    conn.execute(text("SET LOCAL jit = off"))


@app.get("/valves/baseline")
def valves_baseline(
    start: datetime | None = Query(None, description="inizio finestra sana ISO8601"),
    end: datetime | None = Query(None, description="fine finestra sana ISO8601"),
    run_id: str | None = Query(
        None, description="run sano di riferimento; default: `run_id` del KV "
        "`baseline_window`, poi KV `current_run_id`, poi l'unico run presente"),
    refresh: bool = Query(
        False, description="ricalcola e riscrive la baseline memorizzata "
        "anche se ne esiste una valida per questa stessa finestra"),
) -> dict[str, Any]:
    """Riferimento sano per valvola — media, sigma, mediana, MRbar, UCL/LCL.

    Perche' esiste: lo scostamento di una valvola non e' calcolabile in modo
    onesto senza un riferimento. Il confronto fra valvole nello stesso istante
    non lo sostituisce, perche' alcune valvole sono strutturalmente diverse
    dalle altre e risulterebbero permanentemente anomale; e il confronto
    interno alla serie recente misura la DERIVATA del degrado, non il livello,
    quindi non separa una valvola gia' degradata da una sana (verificato: le
    due popolazioni si sovrappongono interamente a ogni ampiezza di finestra).

    Contratto operativo: la finestra sana e' DICHIARATA, mai dedotta, e la
    baseline non si auto-aggiorna durante un degrado (CONTEXT.md, Healthy
    baseline). Se nessuna finestra e' dichiarata la risposta e' 200 con
    `baseline: null` e `degraded: true` — mai 404, mai valori inventati.

    COME VANNO LETTI ucl/lcl (campo `xmr_note`, e per KPI i tre numeri che li
    rendono leggibili): sono limiti su una **media di ~46 cicli**
    (`n_cicli_di_riferimento` = periodo dell'oscillazione del driver), non sul
    singolo ciclo — il 73-78% dei cicli sani cade fuori da essi. Ogni KPI porta
    quindi `sigma_full` (dispersione del singolo ciclo) e `sigma_media_46`
    (dispersione della media di 46 cicli, MISURATA sui dati come sd delle medie
    a blocchi pieni, mai derivata con 1/sqrt(n): quella regola sbaglia di 23x a
    n=10). Se i blocchi pieni sono meno di due, `sigma_media_46` e' `null` con
    `sigma_media_46_reason` — mai un numero plausibile al suo posto.

    IL RIFERIMENTO SANO E' UN RUN, non solo un intervallo di date: i run del
    progetto si sovrappongono nel tempo di parete, quindi `start`/`end` da
    soli non identificano nulla. Il KV `baseline_window` e' percio'
    `{run_id, start, end}` (forma vecchia `{start, end}` ancora accettata: il
    run si risolve dal KV `current_run_id`). `window.run_id` dichiara sempre
    quale run ha prodotto la baseline. Con piu' run e nessuno indicato la
    risposta e' 200 con `baseline: null` + `degraded: true` e il motivo, come
    per una finestra non dichiarata: mai una baseline su due run mescolati.

    MEMORIZZATA, perche' la finestra e' congelata: calcolarla costa ~136 s su
    6,6 M cicli e il risultato non cambia finche' non cambia la finestra. Il
    valore vive nel KV `baseline_cache` con la chiave `{run_id, start, end}`
    che lo ha prodotto, e si serve SOLO a chiave identica — una finestra
    diversa ricalcola, non riusa. `cached` dichiara in risposta se i numeri
    sono memorizzati o appena calcolati, `computed_at` quando lo sono stati.
    `?refresh=1` forza il ricalcolo e riscrive. Si memorizza solo la finestra
    dichiarata nel KV: una `start`/`end` passata a mano viene sempre calcolata
    e non sfratta il riferimento congelato.
    """
    st = _storage()
    w_start, w_end, w_source, kv_run = _baseline_window(st, start, end)
    run, run_reason = _resolve_run(st, run_id or kv_run)
    base: dict[str, Any] = {
        "window": {"start": _iso(w_start), "end": _iso(w_end),
                   "source": w_source, "run_id": run},
        "kpi": list(_BASELINE_KPI),
        "xmr_k": 2.66,
        "n_cicli_di_riferimento": N_CICLI_DI_RIFERIMENTO,
        "xmr_note": XMR_NOTE,
        "valves": None,
        "n_cicli_per_valvola": None,
        "n_cicli_per_valvola_min": None,
        "n_cicli_per_valvola_max": None,
        "degraded": True,
        "reason": None,
        # Da dove vengono i numeri: calcolati adesso o riletti dalla baseline
        # memorizzata per QUESTA stessa finestra (mai per un'altra).
        "cached": False,
        "computed_at": None,
    }
    if w_start is None or w_end is None:
        base["reason"] = (
            "nessuna finestra sana dichiarata: passare start/end oppure "
            "persistere il KV `baseline_window` {run_id, start, end}. Quale "
            "finestra sia sana e' una decisione umana e l'API non la deduce")
        return base
    if run_reason:
        base["reason"] = (
            f"{run_reason} ({RUN_AMBIGUO_HINT}, o `run_id` dentro il KV "
            "`baseline_window`). Il riferimento sano e' un RUN: i run si "
            "sovrappongono nel tempo di parete e start/end da soli non lo "
            "identificano")
        return base
    # Baseline gia' calcolata per ESATTAMENTE questa finestra e questo run:
    # si serve com'e'. `window` viene riscritto con quello appena risolto,
    # perche' `source` dipende dalla richiesta (query/kv) e non dai numeri.
    chiave = _baseline_cache_id(run, w_start, w_end)
    if not refresh:
        hit = _baseline_cache_read(st, chiave)
        if hit is not None:
            payload, computed_at = hit
            out = dict(payload)
            out["window"] = base["window"]
            out["cached"] = True
            out["computed_at"] = computed_at
            return out

    params: dict[str, Any] = {"start": w_start, "end": w_end}
    if run is not None:
        params["run"] = run
    try:
        with st.engine.connect() as conn:
            _tune_baseline_conn(conn)
            rows = conn.execute(text(_baseline_sql(run)),
                                params).mappings().all()
    except SQLAlchemyError as exc:
        base["reason"] = f"tabella cycles non disponibile: {exc}"
        return base
    if not rows:
        base["reason"] = "nessun ciclo nella finestra dichiarata"
        return base

    # sd empirica delle medie a blocchi di 46 cicli — query separata perche'
    # richiede una seconda aggregazione (per blocco, poi per valvola). Se
    # fallisce, i campi restano null con motivo: mai un valore derivato.
    sigma_media: dict[int, Any] = {}
    sigma_media_ko: str | None = None
    try:
        with st.engine.connect() as conn:
            _tune_baseline_conn(conn)
            sm_rows = conn.execute(
                text(_sigma_media_sql(run)),
                {**params, "n": N_CICLI_DI_RIFERIMENTO}).mappings().all()
        sigma_media = {int(r["valve_id"]): r for r in sm_rows}
    except SQLAlchemyError as exc:
        sigma_media_ko = f"query non eseguibile: {exc.__class__.__name__}"

    valves: dict[int, dict[str, Any]] = {}
    for r in rows:
        v: dict[str, Any] = {
            "valve_id": int(r["valve_id"]),
            "n": int(r["n"]),
            "fill_quality_ok_rate": _round3(r["quality_rate"]),
            "diagnostic_suspect_rate": _round3(r["suspect_rate"]),
        }
        sm = sigma_media.get(int(r["valve_id"]))
        n_blocchi = int(sm["n_blocchi"]) if sm is not None else 0
        for k in _BASELINE_KPI:
            mean, mrbar = r[f"{k}_mean"], r[f"{k}_mrbar"]
            half = 2.66 * float(mrbar) if mrbar is not None else None
            sd_media = sm[f"{k}_sd"] if sm is not None else None
            if sigma_media_ko is not None:
                motivo = sigma_media_ko
            elif n_blocchi < 2:
                motivo = (f"servono almeno 2 blocchi pieni di "
                          f"{N_CICLI_DI_RIFERIMENTO} cicli per misurare la "
                          f"dispersione della media; ce ne sono {n_blocchi}")
            elif sd_media is None:
                motivo = "KPI sempre NULL nella finestra: media non calcolabile"
            else:
                motivo = None
            v[k] = {
                "mean": _round3(mean),
                "std": _round3(r[f"{k}_std"]),
                "p50": _round3(r[f"{k}_p50"]),
                "mrbar": _round3(mrbar),
                # sigma stimato dal moving range: MRbar / d2, d2 = 1.128 per n=2
                "sigma": _round3(float(mrbar) / 1.128) if mrbar is not None else None,
                "ucl": _round3(float(mean) + half) if (mean is not None and half is not None) else None,
                "lcl": _round3(float(mean) - half) if (mean is not None and half is not None) else None,
                # --- come leggere ucl/lcl (vedi xmr_note) -------------------
                # sigma_full: dispersione del SINGOLO ciclo (= `std`, ripetuta
                # sotto il nome che ne dichiara il significato).
                "sigma_full": _round3(r[f"{k}_std"]),
                "n_cicli_di_riferimento": N_CICLI_DI_RIFERIMENTO,
                # sigma_media_46: MISURATA (sd delle medie a blocchi pieni),
                # mai derivata con 1/sqrt(n).
                "sigma_media_46": (None if motivo is not None
                                   else _round3(sd_media)),
                "sigma_media_46_n_blocchi": n_blocchi,
                "sigma_media_46_reason": motivo,
            }
        valves[int(r["valve_id"])] = v

    base["valves"] = valves
    # Numero di cicli su cui poggia la base, in UN numero: la dashboard lo cita
    # in chiaro ("base della valvola N su X cicli sani"). Il conteggio vero e'
    # per valvola (`valves[id].n`) e le valvole non hanno per forza lo stesso
    # numero di cicli nella finestra; qui si espone la MEDIANA, e accanto il
    # minimo e il massimo perche' la dispersione non resti nascosta dietro un
    # solo numero. Nessuna finestra -> il campo resta None (mai un default).
    conteggi = sorted(v["n"] for v in valves.values())
    if conteggi:
        meta = len(conteggi) // 2
        mediana = (conteggi[meta] if len(conteggi) % 2
                   else (conteggi[meta - 1] + conteggi[meta]) // 2)
        base["n_cicli_per_valvola"] = int(mediana)
        base["n_cicli_per_valvola_min"] = int(conteggi[0])
        base["n_cicli_per_valvola_max"] = int(conteggi[-1])
    base["degraded"] = False
    mancanti = [i for i in range(1, 36) if i not in valves]
    if mancanti:
        base["degraded"] = True
        base["reason"] = ("nessun ciclo nella finestra per le valvole "
                          + ", ".join(map(str, mancanti)))
    # Memorizzata solo se i numeri sono completi: se `sigma_media_46` e' saltata
    # per un errore SQL, quel guasto e' dell'istante, non della finestra, e non
    # va congelato in una baseline che verrebbe riservita per sempre.
    #
    # Solo per la finestra DICHIARATA (`w_source == "kv"`): lo slot memorizzato
    # e' uno, e appartiene al riferimento congelato che la dashboard chiede a
    # ogni pagina. Una `start`/`end` ad-hoc e' un'esplorazione una tantum e non
    # deve poter sfrattare quel riferimento, lasciando la dashboard di nuovo a
    # 136 s per il resto della giornata.
    if sigma_media_ko is None and w_source == "kv":
        base["computed_at"] = _baseline_cache_write(st, chiave, base)
    return base


@app.get("/valves/profile")
def valves_profile(
    da: datetime = Query(
        ..., alias="from", description="inizio del periodo (ISO8601)"),
    a: datetime = Query(
        ..., alias="to", description="fine del periodo, esclusa (ISO8601)"),
    run_id: str | None = Query(
        None, description="run da interrogare; default: KV `current_run_id`, "
        "oppure l'unico run presente"),
) -> dict[str, Any]:
    """Il profilo del ciclo medio di TUTTE le valvole nel periodo, in una volta.

    E' `/valves/{id}/profile` senza il filtro per valvola: stessa lettura,
    stesse sei grandezze, stesse regole (`media: null` a `n` zero, bordi
    allineati e restituiti effettivi, `base` dalla finestra sana dichiarata).
    Cambia solo il formato: `valves` e' una mappa `valve_id -> {periodo, base}`,
    con la chiave in stringa perche' JSON non ha chiavi numeriche.

    **A cosa serve.** Mettere le valvole in classifica su una grandezza che non
    sia la qualita' — il tempo di riempimento, per esempio. Senza questa route
    servivano 35 chiamate, e chi disegna rinunciava: un guasto costante che
    allunga il riempimento senza produrre scarti restava invisibile proprio
    nella lettura che esiste per rendere visibili i guasti costanti.

    Compaiono tutte e sole le valvole che hanno cicli nel periodo, ciascuna con
    la forma piena: chi disegna le allinea senza indovinare quali chiavi
    troverà. Una valvola presente nel periodo ma assente dalla finestra sana ha
    `base` con le sei grandezze a `null` — mai un numero inventato.

    Costa quanto la route per una valvola sola: legge le stesse righe di
    riepilogo, raggruppate per valvola invece che filtrate.
    """
    if _as_utc(a) <= _as_utc(da):
        raise HTTPException(
            status_code=422,
            detail=f"intervallo vuoto: from={_iso(_as_utc(da))} non e' "
                   f"precedente a to={_iso(_as_utc(a))}")
    core = _profilo_core(_storage(), None, da, a, run_id)
    per = core.pop("valvole")
    base = core.pop("base_valvole")
    valves = {
        str(v): {
            "periodo": prof,
            "base": None if base is None else base.get(v, _profilo_vuoto()),
        }
        for v, prof in sorted((per or {}).items())
    }
    return {"valves": valves, **core}


@app.get("/valves/{valve_id}")
def valve_detail(valve_id: int) -> dict[str, Any]:
    if not 1 <= valve_id <= 35:
        raise HTTPException(status_code=404, detail="valve_id fuori range 1-35")
    st = _storage()
    preds = st.predictions
    alerts_t = st.alerts
    # Stesso criterio di `GET /valves`: si filtra sul run risolto, e con run
    # ambiguo non si filtra affatto invece di rispondere con un errore.
    run, run_reason = _resolve_run(st, None)
    pred_filters = [preds.c.valve_id == valve_id]
    if run_reason is None and run is not None:
        pred_filters.append(preds.c.run_id == run)
    with st.engine.connect() as conn:
        last_pred = conn.execute(
            select(preds).where(*pred_filters)
            .order_by(desc(preds.c.window_end_cycle_id)).limit(1)
        ).first()
        alert_filters = [alerts_t.c.valve_id == valve_id,
                         alerts_t.c.status.in_(_ACTIVE_ALERT_STATUSES)]
        if run_reason is None and run is not None:
            alert_filters.append(alerts_t.c.run_id == run)
        active_alerts = conn.execute(
            select(alerts_t).where(*alert_filters)).fetchall()
    out: dict[str, Any] = {"valve_id": valve_id}
    if last_pred is not None:
        d = _row_to_dict(last_pred)
        d["prediction_ts"] = _iso(d["prediction_ts"])
        out["last_prediction"] = d
    out["active_alerts"] = []
    for a in active_alerts:
        d = _row_to_dict(a)
        for col in ("opened_ts", "last_seen_ts", "closed_ts"):
            d[col] = _iso(d[col])
        out["active_alerts"].append(d)
    return out


@app.get("/valves/{valve_id}/score")
def valve_score(valve_id: int,
                limit: int = Query(200, ge=1, le=5000)) -> dict[str, Any]:
    if not 1 <= valve_id <= 35:
        raise HTTPException(status_code=404, detail="valve_id fuori range 1-35")
    st = _storage()
    preds = st.predictions
    # Stesso criterio di `GET /valves`: la serie dei punteggi appartiene a un
    # run, e mescolarne due produrrebbe una curva che salta fra due macchine.
    run, run_reason = _resolve_run(st, None)
    score_filters = [preds.c.valve_id == valve_id]
    if run_reason is None and run is not None:
        score_filters.append(preds.c.run_id == run)
    with st.engine.connect() as conn:
        rows = conn.execute(
            select(preds.c.window_end_cycle_id, preds.c.anomaly_score,
                   preds.c.predicted_label, preds.c.prediction_ts)
            .where(*score_filters)
            .order_by(desc(preds.c.window_end_cycle_id)).limit(limit)
        ).fetchall()
    series = [
        {
            "window_end_cycle_id": r[0],
            "anomaly_score": r[1],
            "predicted_label": r[2],
            "prediction_ts": _iso(r[3]),
        }
        for r in rows
    ]
    return {"valve_id": valve_id, "series": series}


@app.get("/valves/{valve_id}/kpi")
def valve_kpi(valve_id: int,
              limit: int = Query(200, ge=1, le=5000),
              run_id: str | None = Query(
                  None, description="run da interrogare in `cycles`; default: "
                  "KV `current_run_id`, oppure l'unico run presente"),
              ) -> dict[str, Any]:
    """Serie KPI per ciclo della valvola (vista valvola, spec M10 §5).

    Ritorna `{"valve_id": int, "series": [<dict delle 18 colonne
    operazionali>]}` ordinato per cycle_id DESC (default 200). Le chiavi
    JSON sono i nomi operazionali di ingest (`machine_id, cycle_id,
    valve_id, filling_time_ms, tail_time_ms, tail_pulse, pulse_count,
    target, delta_pulse, filling_step_out, filling_ok, fill_quality_ok,
    sequence_ok, sample_valid, diagnostic_status, close_reason,
    position_limit, filling_overtime`) — niente alias FT/TT/TP/PC inventati.

    I dati vivono nella tabella `cycles` (pipeline/cycles_storage.py, modulo
    dello stesso pool): import lazy qui — se import o tabella non sono
    disponibili → 501 con messaggio chiaro.

    `run_id` (opzionale): il run della serie. `cycle_id` riparte da 1 a ogni
    run, quindi senza filtro `ORDER BY cycle_id DESC` mescolerebbe due serie
    diverse sotto gli stessi numeri di ciclo. Con piu' run e nessuno indicato
    la risposta e' 200 con `series: []`, `degraded: true` e il motivo — mai
    una serie che mescola due run, mai un 500. `run_id` in risposta dichiara
    quale run ha risposto.
    """
    if not 1 <= valve_id <= 35:
        raise HTTPException(status_code=404, detail="valve_id fuori range 1-35")
    st = _storage()
    run, run_reason = _resolve_run(st, run_id)
    if run_reason:
        return {"valve_id": valve_id, "series": [], "run_id": None,
                "degraded": True,
                "reason": f"{run_reason} ({RUN_AMBIGUO_HINT})"}
    cs = _cycles_store(st)
    if cs is None:
        raise HTTPException(
            status_code=501,
            detail="serie KPI non disponibile: pipeline.cycles_storage "
                   "(tabella cycles) non presente — modulo non ancora "
                   "installato o tabella non inizializzata",
        )
    try:
        series = cs.kpi_series(valve_id, limit=limit, run_id=run)
    except Exception as exc:  # noqa: BLE001 — tabella assente / non inizializzata
        raise HTTPException(
            status_code=501,
            detail=f"serie KPI non disponibile (tabella cycles): {exc}",
        ) from exc
    return {"valve_id": valve_id, "series": [_kpi_iso(r) for r in series],
            "run_id": run, "degraded": False, "reason": None}


# --- profilo del ciclo medio ----------------------------------------------
# Quanto tempo scoperto dal riepilogo questa route accetta di leggere
# direttamente da `cycles`. Non e' `CODA_DIRETTA_MAX` (26 ore) perche' i due
# lettori pagano cose diverse: la serie di qualita' legge `valve_id` e
# `fill_quality_ok`, che stanno nell'indice coprente
# `ix_cycles_run_event_ts_cover` (Index Only Scan); il profilo legge sei
# colonne KPI che nell'indice non ci sono, quindi ogni riga costa una visita
# all'heap. A ~39.000 cicli l'ora, tre ore sono ~117.000 righe e restano nei
# millisecondi; ventisei sarebbero un milione e passa. Oltre il tetto la
# risposta si accorcia e lo dichiara, invece di rallentare in silenzio.
PROFILO_SCOPERTO_MAX = timedelta(hours=3)

# Le medie si servono a un decimale: sono millisecondi e impulsi, e la terza
# cifra decimale di una media su decine di migliaia di cicli non e'
# un'informazione, e' rumore che invita a leggere differenze inesistenti.
PROFILO_DECIMALI = 1


def _profilo_vuoto() -> dict[str, Any]:
    """Forma piena con `n: 0` e `media: null` — mai un dizionario mancante.

    Chi disegna deve trovare sempre le stesse sei chiavi: un profilo che a
    volte porta quattro grandezze e a volte sei costringe la pagina a
    difendersi, e la difesa piu' comoda e' mettere zero.
    """
    return {m: {"media": None, "n": 0} for m in PROFILE_METRICS}


def _profilo_somma(pezzi: list[dict[str, tuple[int | None, int]]]) -> dict[str, Any]:
    """Da somme/conteggi parziali alle medie esatte.

    Le medie NON si mediano: si sommano le somme e i conteggi, e si divide una
    volta sola. Sommare le medie orarie darebbe a un'ora da dieci cicli lo
    stesso peso di un'ora da quattromila.
    """
    fuori: dict[str, Any] = {}
    for m in PROFILE_METRICS:
        s_tot = 0
        n_tot = 0
        for p in pezzi:
            s, n = p.get(m, (None, 0))
            if n:
                s_tot += int(s or 0)
                n_tot += int(n)
        # n == 0 → grandezza NON misurata: `null`, mai 0.0. Zero cicli non e'
        # un riempimento istantaneo. Stessa regola di `quality` in
        # `_compute_oee_window`.
        fuori[m] = {
            "media": round(s_tot / n_tot, PROFILO_DECIMALI) if n_tot else None,
            "n": n_tot,
        }
    return fuori


def _profilo_da_riepilogo(
        st: Storage, run: str | None, valve_id: int | None, lo: datetime,
        hi: datetime
) -> tuple[dict[int, dict[str, tuple[int | None, int]]], int]:
    """Somme e conteggi delle ore intere `[lo, hi)` dal riepilogo, per valvola.

    `valve_id=None` chiede TUTTE le valvole presenti nel periodo: e' la stessa
    interrogazione senza il filtro, raggruppata per valvola. Non e' una seconda
    lettura del riepilogo — e' la stessa, e la route per una valvola sola passa
    di qui con il filtro. Un punto di verita' solo.

    Secondo valore: quante righe del riepilogo hanno le colonne di profilo a
    `NULL`, cioe' sono state riassunte prima della migrazione. Non e' lo stesso
    di `n = 0` (nessun valore misurato): li' il dato manca perche' il
    riepilogo non l'ha ancora calcolato, e servirlo come zero sarebbe un numero
    inventato. Basta una colonna sentinella perche' le dodici sono scritte
    dallo stesso `INSERT`: o ci sono tutte o non c'e' nessuna.
    """
    cond = ["bucket_ts >= :lo", "bucket_ts < :hi"]
    params: dict[str, Any] = {"lo": lo, "hi": hi}
    if valve_id is not None:
        cond.insert(0, "valve_id = :v")
        params["v"] = valve_id
    if run is not None:
        cond.insert(0, "run_id = :run")
        params["run"] = run
    # I nomi di colonna vengono da PROFILE_METRICS, non dalla richiesta.
    sel = ", ".join(f"SUM({_sum_col(m)}), SUM({_n_col(m)})"
                    for m in PROFILE_METRICS)
    sentinella = _n_col(PROFILE_METRICS[0])
    with st.engine.connect() as conn:
        righe = conn.execute(text(
            f"SELECT valve_id, {sel}, "
            f"COUNT(*) FILTER (WHERE {sentinella} IS NULL) "
            f"FROM {ROLLUP_TABLE} WHERE " + " AND ".join(cond)
            + " GROUP BY valve_id"), params).fetchall()
    fuori: dict[int, dict[str, tuple[int | None, int]]] = {}
    incomplete = 0
    for row in righe:
        fuori[int(row[0])] = {m: (row[1 + 2 * i], int(row[2 + 2 * i] or 0))
                              for i, m in enumerate(PROFILE_METRICS)}
        incomplete += int(row[-1] or 0)
    return fuori, incomplete


def _profilo_da_cycles(st: Storage, run: str | None, valve_id: int | None,
                       lo: datetime, hi: datetime
                       ) -> dict[int, dict[str, tuple[int | None, int]]]:
    """La stessa forma, letta da `cycles` per un pezzo non ancora riassunto.

    Predicato per riga identico a quello del riempimento (`event_ts >= lo AND
    event_ts < hi`, stesso `run_id`) e stesse funzioni di aggregazione
    (`SUM`/`COUNT` sulla colonna, che escludono i NULL): il pezzo si somma alle
    ore intere senza doppioni e senza sfasamenti. E' un pezzo dell'identita',
    non una stima.
    """
    cond = ["event_ts >= :lo", "event_ts < :hi"]
    params: dict[str, Any] = {"lo": lo, "hi": hi}
    if valve_id is not None:
        cond.insert(0, "valve_id = :v")
        params["v"] = valve_id
    if run is not None:
        cond.insert(0, "run_id = :run")
        params["run"] = run
    sel = ", ".join(f"SUM({m}), COUNT({m})" for m in PROFILE_METRICS)
    try:
        with st.engine.connect() as conn:
            righe = conn.execute(text(
                f"SELECT valve_id, {sel} FROM cycles WHERE "
                + " AND ".join(cond) + " GROUP BY valve_id"),
                params).fetchall()
    except SQLAlchemyError:
        return {}
    return {int(row[0]): {m: (row[1 + 2 * i], int(row[2 + 2 * i] or 0))
                          for i, m in enumerate(PROFILE_METRICS)}
            for row in righe}


def _profilo_finestra(
        st: Storage, run: str | None, valve_id: int | None, lo: datetime,
        hi: datetime, cov_lo: datetime | None, cov_hi: datetime | None,
        cicli_prima: bool = True,
) -> tuple[dict[int, dict[str, Any]] | None, str | None]:
    """Profilo su `[lo, hi)` per valvola: ore intere dal riepilogo, resto da
    `cycles`. `valve_id=None` = tutte le valvole presenti nel periodo.

    `(profilo, motivo)`. `profilo` e' `None` solo quando la finestra non e'
    servibile per intero — e allora `motivo` dice perche'. Non si serve mai un
    pezzo di finestra facendola passare per intera: una media su meta' periodo
    e' un numero giusto per una domanda che nessuno ha fatto.

    La parte coperta dal riepilogo e' `[cov_lo, cov_hi)`; quello che sporge ai
    lati si legge da `cycles`, entro `PROFILO_SCOPERTO_MAX` in totale.
    """
    if cov_lo is None or cov_hi is None:
        r_lo = r_hi = None
        scoperti = [(lo, hi)]
    else:
        # Il riepilogo puo' servire solo ORE INTERE contenute nella finestra:
        # `ceil` a sinistra e `floor` a destra. Prendere il secchiello che
        # contiene il bordo lo conterebbe per intero — e' il caso della finestra
        # sana, che comincia alle 04:08:38: il secchiello delle 04:00 porta
        # anche i cicli precedenti al bordo. Il contrario (scartarlo) perderebbe
        # 51 minuti in silenzio. I due spezzoni di bordo vanno percio' a
        # `cycles`, che ha il predicato al secondo.
        r_lo = _ceil_ora(max(lo, cov_lo))
        r_hi = _floor_ora(min(hi, cov_hi))
        if r_hi <= r_lo:
            r_lo = r_hi = None
            scoperti = [(lo, hi)]
        else:
            scoperti = [p for p in ((lo, r_lo), (r_hi, hi)) if p[1] > p[0]]
    # Il tetto pesa le RIGHE da leggere, non il tempo di parete. Il tratto che
    # sta prima dell'inizio della copertura e' vuoto quando il run non ha cicli
    # li' (`cicli_prima=False`): chiedere sessanta giorni a un run che ne dura
    # sessanta partendo da mezzanotte invece che dalle 04:08 non deve costare
    # nulla, e prima di questa distinzione costava un rifiuto.
    fuori = timedelta(0)
    for a, b in scoperti:
        if cov_lo is not None and b <= cov_lo and not cicli_prima:
            continue
        inizio = a if (cov_lo is None or cicli_prima) else max(a, cov_lo)
        fuori += max(b - inizio, timedelta(0))
    if fuori > PROFILO_SCOPERTO_MAX:
        return None, (
            f"finestra non servibile: {fuori} non sono riassunti in "
            f"{ROLLUP_TABLE} e leggerli da `cycles` supera il tetto di "
            f"{PROFILO_SCOPERTO_MAX}; riempire il riepilogo con "
            f"`python -m pipeline.cycle_rollup --run-id {run} --since-last`")
    pezzi: list[dict[int, dict[str, tuple[int | None, int]]]] = []
    if r_lo is not None and r_hi is not None:
        dati, incomplete = _profilo_da_riepilogo(st, run, valve_id, r_lo, r_hi)
        if incomplete:
            return None, (
                f"{incomplete} ore riassunte prima che il riepilogo avesse le "
                "colonne di profilo: le medie sarebbero calcolate su una parte "
                "del periodo; ricalcolare con `python -m pipeline.cycle_rollup "
                f"--run-id {run} --from {_iso(r_lo)} --to {_iso(r_hi)}`")
        pezzi.append(dati)
    for a, b in scoperti:
        pezzi.append(_profilo_da_cycles(st, run, valve_id, a, b))
    valvole = sorted({v for p in pezzi for v in p})
    return ({v: _profilo_somma([p[v] for p in pezzi if v in p])
             for v in valvole}, None)


@app.get("/valves/{valve_id}/profile")
def valve_profile(
    valve_id: int,
    da: datetime = Query(
        ..., alias="from", description="inizio del periodo (ISO8601)"),
    a: datetime = Query(
        ..., alias="to", description="fine del periodo, esclusa (ISO8601)"),
    run_id: str | None = Query(
        None, description="run da interrogare; default: KV `current_run_id`, "
        "oppure l'unico run presente"),
) -> dict[str, Any]:
    """Profilo del ciclo MEDIO della valvola nel periodo, e sulla finestra sana.

    Sei medie — `filling_time_ms`, `tail_time_ms`, `tail_pulse`, `pulse_count`,
    `delta_pulse`, `filling_step_out` — piu' il numero di cicli su cui ognuna e'
    calcolata. Servono a disegnare il riempimento e la coda come **una forma
    sola**: sul run `storico_60d` quattro guasti diversi danno quattro firme
    distinte, e la forma li separa senza che nessuno debba leggere una tabella.

    **`base` e' la stessa valvola, non le altre.** E' il profilo sulla finestra
    sana dichiarata (KV `baseline_window`, risolta come in `/valves/baseline`).
    Il confronto giusto e' una valvola contro se stessa da sana: le 35 valvole
    hanno posizioni e storie diverse, e misurare uno scostamento contro la media
    delle altre mescolerebbe il guasto con la dispersione normale della giostra.
    Se la finestra sana non e' dichiarata o non e' servibile, `base` e' `null` e
    `reason` dice perche' — `periodo` risponde comunque.

    **`media: null` quando `n` e' zero.** Zero cicli misurati significa "non
    misurata", mai "zero millisecondi". Le colonne KPI sono nullable (cicli
    parziali, policy T6), percio' ogni grandezza porta il PROPRIO `n`: due
    grandezze dello stesso periodo possono avere denominatori diversi, ed e'
    corretto che si vedano.

    **`from`/`to` in risposta sono quelli EFFETTIVI**, allineati all'ora verso
    l'esterno e tagliati all'ultimo ciclo esistente: chi disegna non deve
    ricalcolarli.

    Si legge il riepilogo `cycle_rollup_hour` (~34.000 righe) piu' i pezzi che
    il riepilogo non copre ancora, entro `PROFILO_SCOPERTO_MAX`. Il periodo
    intero costa millisecondi; leggere le stesse medie da `cycles` sui 60
    giorni costa ~53 secondi.

    Per mettere le 35 valvole a confronto su una di queste grandezze c'e'
    `/valves/profile`, che serve la stessa lettura per tutte in una chiamata
    sola: la logica e' condivisa (`_profilo_core`), quindi i numeri delle due
    route non possono divergere.
    """
    if not 1 <= valve_id <= 35:
        raise HTTPException(status_code=404, detail="valve_id fuori range 1-35")
    if _as_utc(a) <= _as_utc(da):
        raise HTTPException(
            status_code=422,
            detail=f"intervallo vuoto: from={_iso(_as_utc(da))} non e' "
                   f"precedente a to={_iso(_as_utc(a))}")
    st = _storage()
    core = _profilo_core(st, valve_id, da, a, run_id)
    per = core.pop("valvole")
    base = core.pop("base_valvole")
    return {
        "valve_id": valve_id,
        "periodo": _profilo_vuoto() if per is None
                   else per.get(valve_id, _profilo_vuoto()),
        "base": None if base is None
                else base.get(valve_id, _profilo_vuoto()),
        **core,
    }


def _profilo_core(st: Storage, valve_id: int | None, da: datetime,
                  a: datetime, run_id: str | None) -> dict[str, Any]:
    """Il profilo, una volta sola, per una valvola o per tutte.

    `valve_id=None` = tutte le valvole presenti nel periodo. Le due route —
    `/valves/{id}/profile` e `/valves/profile` — sono due formati della STESSA
    lettura: risoluzione del run, allineamento dei bordi, copertura del
    riepilogo, accorciamento dichiarato e finestra sana stanno qui e nient'altro
    li ripete. `valvole` e `base_valvole` sono mappe `valve_id -> sei
    grandezze`; `valvole` e' `None` quando la finestra non e' servibile.
    """
    run, run_reason = _resolve_run(st, run_id)
    base_out: dict[str, Any] = {
        "run_id": run, "from": None, "to": None,
        "valvole": None, "base_valvole": None,
        "base_from": None, "base_to": None,
        "degraded": True, "reason": None,
    }
    if run_reason:
        base_out["reason"] = f"{run_reason} ({RUN_AMBIGUO_HINT})"
        return base_out

    lo, hi = _floor_ora(_as_utc(da)), _ceil_ora(_as_utc(a))
    cov_lo, cov_hi, cicli_prima = _copertura_riepilogo(st, run)
    # `cov_hi` e' l'INIZIO dell'ultima ora riassunta: la copertura arriva a
    # un'ora dopo.
    fine_coperta = None if cov_hi is None else cov_hi + ROLLUP_BUCKET
    motivi: list[str] = []

    # Oltre l'ultimo ciclo non c'e' nulla da servire: accorciare li' non e' un
    # degrado, e' la fine del run, e `MAX(event_ts)` lo sa con certezza.
    ultimo = _last_cycle_ts(st, run)
    if ultimo is not None:
        limite = _ceil_ora(ultimo + timedelta(seconds=1))
        if hi > limite:
            hi = limite
    if hi <= lo:
        base_out["from"] = base_out["to"] = _iso(lo)
        base_out["reason"] = "il periodo chiesto e' oltre l'ultimo ciclo del run"
        return base_out

    periodo, motivo = _profilo_finestra(
        st, run, valve_id, lo, hi, cov_lo, fine_coperta, cicli_prima)
    if periodo is None and fine_coperta is not None and hi > fine_coperta:
        # Secondo tentativo: il periodo si ferma dove finisce il riepilogo.
        # Accorciare e dirlo e' meglio che rispondere vuoto, ma DEVE restare
        # scritto nella risposta, altrimenti la pagina disegna un periodo
        # diverso da quello che ha chiesto senza saperlo.
        nuovo = max(lo, fine_coperta)
        if nuovo > lo:
            motivi.append(f"periodo accorciato a {_iso(nuovo)}: {motivo}")
            hi = nuovo
            periodo, motivo = _profilo_finestra(
                st, run, valve_id, lo, hi, cov_lo, fine_coperta, cicli_prima)
    if periodo is None:
        base_out["from"] = _iso(lo)
        base_out["to"] = _iso(hi)
        base_out["reason"] = "; ".join(motivi + [motivo or "periodo non servibile"])
        return base_out

    # -- la base: stessa valvola, finestra sana dichiarata -------------------
    b_start, b_end, b_source, b_run = _baseline_window(st, None, None)
    base_prof: dict[str, Any] | None = None
    if b_start is None or b_end is None:
        motivi.append(
            "base non disponibile: la finestra sana non e' dichiarata "
            "(KV `baseline_window` assente); persistere {run_id, start, end}")
    elif b_run is not None and b_run != run:
        # Il riferimento sano e' un RUN, non solo un intervallo: confrontare il
        # periodo di un run con la base di un altro sarebbe uno scostamento fra
        # due macchine diverse.
        motivi.append(
            f"base non disponibile: la finestra sana appartiene al run "
            f"{b_run!r}, il periodo al run {run!r}")
    else:
        b_start, b_end = _as_utc(b_start), _as_utc(b_end)
        base_prof, b_motivo = _profilo_finestra(
            st, run, valve_id, b_start, b_end, cov_lo, fine_coperta,
            cicli_prima)
        if base_prof is None:
            motivi.append(f"base non disponibile ({b_source}): {b_motivo}")

    return {
        "run_id": run,
        "from": _iso(lo), "to": _iso(hi),
        "valvole": periodo, "base_valvole": base_prof,
        # La finestra sana esce a parte e NON dentro `base`: `base` ha
        # esattamente le stesse sei chiavi di `periodo`, cosi' chi disegna le
        # due forme le scorre con lo stesso codice.
        "base_from": _iso(b_start) if base_prof is not None else None,
        "base_to": _iso(b_end) if base_prof is not None else None,
        "degraded": bool(motivi), "reason": "; ".join(motivi) or None,
    }


def _alerts_rows(st: Storage, status: str,
                 valve_id: int | None, fault_type: str | None,
                 run_id: str | None = None) -> list[dict[str, Any]]:
    """Righe della tabella `alerts` filtrate per stato — forma unica.

    `status`: `active` (open/sustained) · `closed` · `all` (nessun filtro).
    Ordine: `opened_ts` DESC con NULL in coda (coalesce verso 'epoch'), così le
    righe senza opened_ts non fluttuano in testa (bug C3 fix).

    Dal 2026-08-22 le righe sono filtrate sul run risolto: un allarme
    appartiene al run che l'ha generato, e una lista che li mescolasse
    mostrerebbe al tecnico gli allarmi di una macchina diversa da quella che
    sta guardando. Con run ambiguo il filtro non si applica.

    `run_id` esplicito interroga un run diverso da quello corrente senza
    spostare il KV. Serve a guardare due corse a confronto: senza, questa lista
    resterebbe l'unica parte della pagina ancorata al KV mentre tutto il resto
    segue il run chiesto, e la fascia degli allarmi mostrerebbe una macchina
    diversa dai grafici che le stanno accanto.
    """
    alerts_t = st.alerts
    q = select(alerts_t)
    run, run_reason = _resolve_run(st, run_id)
    if run_reason is None and run is not None:
        q = q.where(alerts_t.c.run_id == run)
    if status == "closed":
        q = q.where(alerts_t.c.status == "closed")
    elif status == "active":
        q = q.where(alerts_t.c.status.in_(_ACTIVE_ALERT_STATUSES))
    if valve_id is not None:
        q = q.where(alerts_t.c.valve_id == valve_id)
    if fault_type is not None:
        q = q.where(alerts_t.c.fault_type == fault_type)
    # cast esplicito del letterale 'epoch' a timestamptz — un literal legato
    # come VARCHAR rompe il coalesce (DatatypeMismatch su psycopg3)
    q = q.order_by(desc(func.coalesce(
        alerts_t.c.opened_ts, func.cast("epoch", DateTime(timezone=True)))))
    with st.engine.connect() as conn:
        rows = conn.execute(q).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        for col in ("opened_ts", "last_seen_ts", "closed_ts"):
            d[col] = _iso(d[col])
        out.append(d)
    return out


@app.get("/alerts")
def alerts(closed: bool = Query(False),
           status: Literal["active", "closed", "all"] | None = Query(
               None, description="filtro di stato; se presente prevale su "
                                 "`closed`. `all` = tutta la tabella"),
           valve_id: int | None = Query(None, ge=1, le=35),
           fault_type: str | None = None,
           run_id: str | None = Query(
               None, description="run da interrogare; default: KV "
               "`current_run_id`, oppure l'unico run presente")) -> dict[str, Any]:
    """Stato CURRENTE degli alert: una riga per (valve_id, fault_type).

    Lo storico delle transizioni — incluse le chiusure e i cicli di vita
    precedenti — vive in `alert_transitions` (log append-only); qui tornano
    solo le righe di stato corrente della tabella `alerts`
    (open/sustained/closed). `closed=1` filtra sulle righe correnti chiuse.

    `closed` è binario e copre due casi su tre (attivi · solo chiusi): il terzo,
    «tutti», si chiede con `status=all` — ed è quello che serve alla vista
    storico. `status` prevale su `closed` quando entrambi sono presenti;
    `closed` resta per compatibilità con i chiamanti esistenti.
    """
    scelto = status or ("closed" if closed else "active")
    return {"alerts": _alerts_rows(_storage(), scelto, valve_id, fault_type,
                                   run_id)}


@app.get("/alerts/history")
def alerts_history(valve_id: int | None = Query(None, ge=1, le=35),
                   fault_type: str | None = None,
                   run_id: str | None = Query(
                       None, description="run da interrogare; default: KV "
                       "`current_run_id`, oppure l'unico run presente"),
                   ) -> dict[str, Any]:
    """La tabella `alerts` INTERA, senza filtro di stato — stessa forma di
    `/alerts` (equivale a `/alerts?status=all`).

    Serve alla vista storico: `/alerts` mostra ciò che è aperto adesso, questa
    mostra anche i cicli di vita già chiusi.

    Cosa NON è: la lista degli allarmi che il motore ha *valutato*. Tornano
    solo le righe **persistite** in `alerts`, cioè quelle che il motore ha
    davvero emesso. Una fixture di v6 conteneva 27 righe contro le 6 reali,
    perché era stata generata iterando lo stato interno del motore invece di
    ciò che aveva emesso: 21 di quelle righe erano voci vuote
    (`n_cycles_above=0`, `max_score_seen=0.0`, `opened_ts=null`). Questa route
    non serve fantasmi.
    """
    return {"alerts": _alerts_rows(_storage(), "all", valve_id, fault_type, run_id)}


# uvicorn: python -m uvicorn pipeline.api:app --reload  (o test client)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
