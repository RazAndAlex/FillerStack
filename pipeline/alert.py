"""Alert engine M10 (ADR-0021) — separazione prediction/decisione (§67–§68).

Il modello ML produce `anomaly_score` e `predicted_label`; l'alert engine
decide gli stati alert *senza toccare* il modello. Input: una sequenza di
prediction record v1 (già persistiti in Postgres da `pipeline/inference.py`).
Output: transizioni di stato alert, tracciabili e deterministiche.

Regole (spec M10 §3, contesto §67–§68):

- **open**   — default operativo: almeno 5 score `>= threshold_open` nelle
               ultime 150 predizioni della stessa valvola. Il criterio è
               score-only: `predicted_label` non lo filtra; una sola lineage
               tecnica ``score_aggregation`` evita aperture duplicate.
- **sustain** — una finestra ancora qualificata con score corrente sopra
               soglia aggiorna last_seen/max/n;
- **close**  — una lineage aggregata si chiude quando la sua finestra K/N
               non è più qualificata, rispettando il cooldown.

Per compatibilità, ``score_aggregation_window=0`` e
``score_aggregation_required=0`` ripristinano la regola legacy: persistenza
consecutiva per `(valve_id, predicted_label)`, con isteresi in chiusura.

Il **core dell'engine è logica pura** (nessuna connessione, nessun DB): la
persistenza avviene SOLO tramite i due helper di wiring in fondo al modulo,
`persist_events` (AlertEngine → storage, chiamato da `inference.run()`, spec
M10 §3 "Output: transizioni alert persistite") e `load_states` (ricostruzione
dello stato al restart dalla tabella `alerts`). Il vocabolario stati è
condiviso con `pipeline.storage` (`ALERT_STATUSES`) — niente literal sparsi.

Dipendenze: stdlib + `pipeline.storage` (solo vocabolario/costanti) +
`sqlalchemy` (solo `select`/`tuple_` dentro i due helper di wiring).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Mapping
from typing import Any, Sequence
from uuid import UUID, uuid4

from sqlalchemy import select, text, tuple_

from pipeline.storage import ALERT_STATUSES, alert_id_for

# Stati del ciclo di vita — vocabolario condiviso (storage.ALERT_STATUSES),
# alias locali per leggibilità: niente stringhe literal sparse nel codice.
_OPEN, _SUSTAINED, _CLOSED = ALERT_STATUSES
_SCORE_AGGREGATION_FAULT_TYPE = "score_aggregation"


@dataclass(frozen=True)
class AlertConfig:
    """Parametri congelabili dell'alert engine (calibration → freeze CP-3b).

    Il default operativo e score-only: K=5 score sopra soglia nelle ultime
    N=150 predizioni per valvola, indipendentemente da `predicted_label`.
    Le soglie operative si congelano dopo la calibration su scenari noti
    (§79). `persistence` resta il parametro della compatibilità legacy,
    selezionata impostando entrambi i parametri K/N a zero (o `None`).
    """
    threshold_open: float = 0.5
    hysteresis: float = 0.1
    persistence: int = 2
    cooldown_seconds: float = 60.0
    healthy_label: str = "healthy"
    score_aggregation_window: int | None = 150
    score_aggregation_required: int | None = 5

    @property
    def score_aggregation_enabled(self) -> bool:
        """True quando K-of-N score-only sostituisce la persistenza legacy."""
        return self.score_aggregation_window not in (None, 0)

    @property
    def threshold_close(self) -> float:
        return self.threshold_open - self.hysteresis

    def __post_init__(self) -> None:
        if not 0.0 < self.threshold_open <= 1.0:
            raise ValueError("threshold_open deve essere in (0, 1]")
        if not 0.0 <= self.hysteresis < self.threshold_open:
            raise ValueError("hysteresis deve essere in [0, threshold_open)")
        if self.persistence < 1:
            raise ValueError("persistence deve essere >= 1")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds deve essere >= 0")
        window = self.score_aggregation_window
        required = self.score_aggregation_required
        if window in (None, 0) and required in (None, 0):
            return
        if (window is None or required is None or window == 0 or required == 0):
            raise ValueError(
                "score_aggregation_window e score_aggregation_required "
                "devono essere entrambi disabilitati o entrambi positivi")
        if (not isinstance(window, int) or not isinstance(required, int)
                or isinstance(window, bool) or isinstance(required, bool)):
            raise ValueError("i parametri score_aggregation devono essere interi")
        if window < 1 or required < 1 or required > window:
            raise ValueError(
                "score_aggregation_required deve essere in [1, window]")


@dataclass
class AlertState:
    """Stato currente di un alert per (valve_id, fault_type)."""
    valve_id: int
    fault_type: str
    status: str = _CLOSED  # closed | open | sustained
    opened_ts: str | None = None
    last_seen_ts: str | None = None
    closed_ts: str | None = None
    max_score_seen: float = 0.0
    n_cycles_above: int = 0
    streak: int = 0  # finestre consecutive sopra threshold_open (pending)
    cooldown_until: datetime | None = None
    opened_at_cycle_id: int | None = None
    closed_at_cycle_id: int | None = None


@dataclass
class AlertEvent:
    """Una transizione tracciabile (open/sustain/close).

    NB: nessun campo `gt_status` — "GT" è riservato al glossario Ground Truth
    (CONTEXT.md, ADR-0012) e il campo era morto (sempre == to_status, mai
    letto/persistito). Lo stato di arrivo è `to_status`.
    """
    valve_id: int
    fault_type: str
    from_status: str
    to_status: str
    anomaly_score: float
    threshold_open: float
    threshold_close: float
    window_end_cycle_id: int
    prediction_ts: str


def _fault_type_of(predicted_label: str, healthy_label: str) -> str | None:
    return None if predicted_label == healthy_label else predicted_label


def _parse_ts(ts: str | datetime) -> datetime:
    """ISO8601 → datetime tz-aware (supporta suffisso Z).

    Pass-through se `ts` è già un `datetime` — stesso comportamento di
    `storage._to_dt` (matching 2026-08-13: prima accettava solo str e un
    datetime lo faceva crashare).
    """
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts.replace("Z", "+00:00").replace("z", "+00:00"))


class AlertEngine:
    """Decisione alert su sequenza di prediction record v1.

    Uso:
        engine = AlertEngine(AlertConfig(...))
        events = engine.process(records)   # lista di AlertEvent, in ordine
        persist_events(events, storage)    # wiring (in questo modulo)

    `records` è una sequenza di dict prediction-v1, OPPURE una sequenza di
    tuple/oggetti con i soli campi necessari (valve_id, predicted_label,
    anomaly_score, window_end_cycle_id, prediction_ts). Lo stato interno
    `states` persiste tra chiamate `process`; al restart si ricostruisce con
    `load_states(storage, config)` (dalla tabella `alerts`, non
    dall'alert_transitions — lo stato currente è lì). Quando K/N è attivo,
    `load_score_history(storage, config)` ricostruisce separatamente le
    ultime boolean per valvola.
    """

    def __init__(self, config: AlertConfig | None = None) -> None:
        self.config = config or AlertConfig()
        self.states: dict[tuple[int, str], AlertState] = {}
        # Le entry sono ripristinate da `load_score_history` nel wiring di
        # inference. Streak legacy e cooldown restano volutamente effimeri.
        self._score_history: dict[int, deque[bool]] = {}

    # -- accesso/ricostruzione stato ---------------------------------------
    def _state(self, valve_id: int, fault_type: str) -> AlertState:
        key = (valve_id, fault_type)
        st = self.states.get(key)
        if st is None:
            st = self.states[key] = AlertState(valve_id=valve_id,
                                               fault_type=fault_type)
        return st

    # -- core ---------------------------------------------------------------
    def _handle(self, valve_id: int, fault_type: str, score: float,
                window_end_cycle_id: int, prediction_ts: str) -> list[AlertEvent]:
        cfg = self.config
        st = self._state(valve_id, fault_type)
        events: list[AlertEvent] = []
        ts_dt = _parse_ts(prediction_ts)

        # cooldown scaduto?
        if st.cooldown_until is not None and ts_dt < st.cooldown_until:
            # dentro il cooldown: nessuna riapertura; se era chiuso, resta chiuso
            return events

        if st.status == _CLOSED:
            if score >= cfg.threshold_open:
                st.streak += 1
                if st.streak >= cfg.persistence:
                    st.status = _OPEN
                    st.opened_ts = prediction_ts
                    st.last_seen_ts = prediction_ts
                    st.max_score_seen = score
                    st.n_cycles_above = 1
                    st.opened_at_cycle_id = window_end_cycle_id
                    events.append(self._event(st, _CLOSED, _OPEN, score,
                                              window_end_cycle_id, prediction_ts))
                # else: pending, nessun evento
            else:
                st.streak = 0
            return events

        # status open/sustained
        if score >= cfg.threshold_open:
            # sustain
            prev = st.status
            st.status = _SUSTAINED
            st.last_seen_ts = prediction_ts
            st.max_score_seen = max(st.max_score_seen, score)
            st.n_cycles_above += 1
            events.append(self._event(st, prev, _SUSTAINED, score,
                                      window_end_cycle_id, prediction_ts))
        elif score <= cfg.threshold_close:
            # close (isteresi)
            prev = st.status
            st.status = _CLOSED
            st.closed_ts = prediction_ts
            st.closed_at_cycle_id = window_end_cycle_id
            st.streak = 0
            st.cooldown_until = ts_dt + timedelta(seconds=cfg.cooldown_seconds)
            events.append(self._event(st, prev, _CLOSED, score,
                                      window_end_cycle_id, prediction_ts))
        # else: score in (threshold_close, threshold_open) → isteresi, nessuna transizione
        return events

    def _event(self, st: AlertState, from_status: str, to_status: str,
               score: float, window_end_cycle_id: int,
               prediction_ts: str) -> AlertEvent:
        return AlertEvent(
            valve_id=st.valve_id,
            fault_type=st.fault_type,
            from_status=from_status,
            to_status=to_status,
            anomaly_score=score,
            threshold_open=self.config.threshold_open,
            threshold_close=self.config.threshold_close,
            window_end_cycle_id=window_end_cycle_id,
            prediction_ts=prediction_ts,
        )

    def _handle_score_aggregation(self, valve_id: int, score: float,
                                  window_end_cycle_id: int,
                                  prediction_ts: str) -> list[AlertEvent]:
        """Applica K-of-N per valvola, ignorando deliberatamente la label ML.

        La lineage ha un `fault_type` tecnico stabile: la classe prevista è
        un'ipotesi diagnostica e non può creare aperture parallele quando
        cambia fra finestre che soddisfano lo stesso criterio score-only.
        """
        cfg = self.config
        assert cfg.score_aggregation_enabled
        window = cfg.score_aggregation_window
        required = cfg.score_aggregation_required
        assert window is not None and required is not None
        history = self._score_history.get(valve_id)
        if history is None:
            history = self._score_history[valve_id] = deque(maxlen=window)
        is_above = score >= cfg.threshold_open
        history.append(is_above)
        qualified = sum(history) >= required
        st = self._state(valve_id, _SCORE_AGGREGATION_FAULT_TYPE)
        ts_dt = _parse_ts(prediction_ts)

        if st.cooldown_until is not None and ts_dt < st.cooldown_until:
            return []

        if st.status == _CLOSED:
            if not qualified:
                return []
            st.status = _OPEN
            st.opened_ts = prediction_ts
            st.last_seen_ts = prediction_ts
            st.max_score_seen = score
            st.n_cycles_above = 1
            st.opened_at_cycle_id = window_end_cycle_id
            return [self._event(st, _CLOSED, _OPEN, score,
                                window_end_cycle_id, prediction_ts)]

        if qualified:
            # Persistiamo un sustain solo su una finestra realmente sopra
            # soglia: n_cycles_above continua a significare score raw sopra
            # threshold_open, non il numero di finestre aggregate qualificate.
            if not is_above:
                return []
            prev = st.status
            st.status = _SUSTAINED
            st.last_seen_ts = prediction_ts
            st.max_score_seen = max(st.max_score_seen, score)
            st.n_cycles_above += 1
            return [self._event(st, prev, _SUSTAINED, score,
                                window_end_cycle_id, prediction_ts)]

        prev = st.status
        st.status = _CLOSED
        st.closed_ts = prediction_ts
        st.closed_at_cycle_id = window_end_cycle_id
        st.streak = 0
        st.cooldown_until = ts_dt + timedelta(seconds=cfg.cooldown_seconds)
        return [self._event(st, prev, _CLOSED, score,
                            window_end_cycle_id, prediction_ts)]

    # -- API pubblica -------------------------------------------------------
    def process(self, records: Sequence[Any]) -> list[AlertEvent]:
        """Processa una sequenza di prediction record e ritorna le transizioni.

        Ogni record è un dict prediction-v1 (chiavi valve_id,
        predicted_label, anomaly_score, window_end_cycle_id, prediction_ts)
        oppure una tupla/lista di 5 campi nello stesso ordine. Record
        malformati → ValueError esplicito (difensivo: niente unpack opaco).
        """
        events: list[AlertEvent] = []
        for rec in records:
            # accetta sia dict (prediction-v1) sia tuple
            if isinstance(rec, dict):
                try:
                    valve_id = rec["valve_id"]
                    label = rec["predicted_label"]
                    score = rec["anomaly_score"]
                    wcid = rec["window_end_cycle_id"]
                    ts = rec["prediction_ts"]
                except KeyError as exc:
                    raise ValueError(
                        "malformed prediction record: missing key "
                        f"{exc.args[0]!r} in {rec!r}") from exc
            else:
                if not isinstance(rec, (tuple, list)) or len(rec) != 5:
                    raise ValueError(
                        "malformed prediction record: expected a tuple/list "
                        "of 5 fields (valve_id, predicted_label, "
                        "anomaly_score, window_end_cycle_id, prediction_ts), "
                        f"got {rec!r}")
                valve_id, label, score, wcid, ts = rec
            try:
                score_f = float(score)
                wcid_i = int(wcid)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "malformed prediction record: anomaly_score and "
                    f"window_end_cycle_id must be numeric, got {rec!r}") from exc
            if self.config.score_aggregation_enabled:
                events.extend(self._handle_score_aggregation(valve_id, score_f,
                                                             wcid_i, ts))
                continue
            fault_type = _fault_type_of(label, self.config.healthy_label)
            if fault_type is None:
                # healthy: chiude eventuale alert aperto (se sotto soglia close)
                # NB: gestito chiudendo ogni alert aperto di quella valvola con score
                # basso. Qui health non apre nulla.
                events.extend(self._close_all_for_valve(valve_id, score_f,
                                                        wcid_i, ts))
                continue
            events.extend(self._handle(valve_id, fault_type, score_f,
                                       wcid_i, ts))
        return events

    def _close_all_for_valve(self, valve_id: int, score: float,
                             wcid: int, ts: str) -> list[AlertEvent]:
        """Quando il modello torna `healthy`, chiude gli alert aperti della valvola
        (se score <= threshold_close).

        DECISIONE INTENZIONALE (documentata, spec M10 §3): un singolo record
        `healthy` chiude TUTTI gli alert aperti della valvola — chiusura
        per-valvola, non per (valve_id, fault_type). `healthy` è lo stato
        nominale dell'intera valvola, quindi riporta a regime tutti i fault
        aperti di quella valvola. Il dedup in APERTURA resta per
        (valve_id, fault_type) (§3); solo la chiusura via healthy è
        per-valvola.
        """
        events: list[AlertEvent] = []
        cfg = self.config
        for (vid, ftype), st in list(self.states.items()):
            if vid != valve_id or st.status == _CLOSED:
                continue
            if score <= cfg.threshold_close:
                # from_status = stato REALE pre-chiusura (open o sustained):
                # niente "sustained" fabbricato — la transizione deve essere
                # tracciabile così com'è (ADR-0021 §Decisione 6).
                prev_status = st.status
                st.status = _CLOSED
                st.closed_ts = ts
                st.closed_at_cycle_id = wcid
                st.streak = 0
                st.cooldown_until = _parse_ts(ts) + timedelta(
                    seconds=cfg.cooldown_seconds)
                events.append(AlertEvent(
                    valve_id=valve_id, fault_type=ftype,
                    from_status=prev_status, to_status=_CLOSED,
                    anomaly_score=score, threshold_open=cfg.threshold_open,
                    threshold_close=cfg.threshold_close,
                    window_end_cycle_id=wcid, prediction_ts=ts))
        return events


# ---------------------------------------------------------------------------
# Wiring AlertEngine → storage (spec M10 §3: "Output: transizioni alert
# persistite"). Unico punto di persistenza dell'engine: chiamato da
# inference.run() — nessun altro modulo scrive alerts/alert_transitions.
# ---------------------------------------------------------------------------

def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def _current_alert_rows(storage: Any, keys: set[tuple[int, str]],
                        run_id: str) -> dict[tuple[int, str], dict]:
    """Riga `alerts` corrente per le chiavi richieste (SELECT read-only).

    È la baseline di accumulo di `persist_events`: lo stato persistito prima
    della batch. Tabella piccola (una riga per lineage e per run), filtro IN
    per non leggere tutto, e filtro di run perché la baseline di un run non
    può essere lo stato accumulato da un altro.
    """
    if not keys:
        return {}
    cols = storage.alerts
    stmt = select(cols).where(
        tuple_(cols.c.valve_id, cols.c.fault_type).in_(list(keys)),
        cols.c.run_id == run_id)
    with storage.engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    return {(d["valve_id"], d["fault_type"]): d for d in map(_row_dict, rows)}


def _full_state_payload(ev: AlertEvent, prev: dict | None) -> dict[str, Any]:
    """Stato completo della riga `alerts` DOPO la transizione `ev`.

    Ricostruito dal tracciato degli eventi (seed `prev` = riga persistita
    pre-batch): a ogni evento corrisponde esattamente lo stato che l'engine
    ha in memoria a quell'istante (open rinfresca opened/last_seen e azzera
    closed; sustained aggiorna last_seen/max/n; closed imposta closed_ts e
    conserva opened_ts/last_seen/n_cycles_above — coerente con
    `AlertEngine._handle`, che sul ramo di chiusura NON tocca il contatore
    perché la chiusura avviene sotto soglia).
    Contratto FULL-STATE di `storage.upsert_alert`: tutte le colonne
    mutabili, nessuna deduzione nel chiamante.
    """
    ts, wcid, score = ev.prediction_ts, ev.window_end_cycle_id, ev.anomaly_score
    prev = prev or {}
    if ev.to_status == _OPEN:
        return {
            "status": _OPEN,
            "opened_ts": ts, "opened_at_cycle_id": wcid,
            "last_seen_ts": ts,
            "closed_ts": None, "closed_at_cycle_id": None,
            "max_score_seen": score, "n_cycles_above": 1,
        }
    if ev.to_status == _SUSTAINED:
        return {
            "status": _SUSTAINED,
            "opened_ts": prev.get("opened_ts"),
            "opened_at_cycle_id": prev.get("opened_at_cycle_id"),
            "last_seen_ts": ts,
            "closed_ts": prev.get("closed_ts"),
            "closed_at_cycle_id": prev.get("closed_at_cycle_id"),
            "max_score_seen": max(prev.get("max_score_seen") or 0.0, score),
            "n_cycles_above": (prev.get("n_cycles_above") or 0) + 1,
        }
    if ev.to_status == _CLOSED:
        return {
            "status": _CLOSED,
            "opened_ts": prev.get("opened_ts"),
            "opened_at_cycle_id": prev.get("opened_at_cycle_id"),
            "last_seen_ts": prev.get("last_seen_ts"),
            "closed_ts": ts, "closed_at_cycle_id": wcid,
            "max_score_seen": max(prev.get("max_score_seen") or 0.0, score),
            # CONSERVATO, non incrementato: una chiusura avviene per
            # definizione SOTTO soglia (`score <= threshold_close`), quindi il
            # ciclo che chiude non è un ciclo "above". Il motore in memoria non
            # tocca il contatore sul ramo di chiusura; qui incrementarlo
            # produceva un +1 sistematico su ogni alert chiuso.
            "n_cycles_above": (prev.get("n_cycles_above") or 0),
        }
    raise ValueError(
        f"invalid to_status {ev.to_status!r}: must be one of {ALERT_STATUSES}")


def persist_events(events: Sequence[AlertEvent], storage: Any,
                   run_id: str | None = None) -> int:
    """Persiste una batch di transizioni alert (AlertEngine → storage).

    Per OGNI `AlertEvent`:
    - `storage.upsert_alert` con lo stato COMPLETO della riga (alert_id di
      lineage da `storage.alert_id_for(valve_id, fault_type)` — mai
      inventato nel chiamante);
    - `storage.insert_transition` (log append-only, transition_id uuid4).

    Lo stato completo per evento è ricostruito dal tracciato ordinato degli
    eventi con seed dalla riga corrente in DB (read-only): è ESATTAMENTE lo
    stato interno dell'engine a quell'istante, quindi una batch dopo un
    restart (engine ricostruito da `load_states`) NON azzera l'accumulo
    (max_score_seen / n_cycles_above) e non duplica l'apertura di alert già
    aperti. Ritorna il numero di eventi persistiti.

    Contratto: `events` è la sequenza completa delle transizioni dalla
    baseline persistita (in ordine di emissione di `AlertEngine.process`).
    """
    run = _resolve_history_run_id(storage, run_id)
    keys = {(ev.valve_id, ev.fault_type) for ev in events}
    baseline = _current_alert_rows(storage, keys, run)
    n = 0
    for ev in events:
        payload = _full_state_payload(ev, baseline.get((ev.valve_id,
                                                        ev.fault_type)))
        baseline[(ev.valve_id, ev.fault_type)] = payload
        alert_id = alert_id_for(ev.valve_id, ev.fault_type, run)
        storage.upsert_alert(
            alert_id=str(alert_id), valve_id=ev.valve_id,
            fault_type=ev.fault_type, run_id=run, **payload)
        storage.insert_transition(
            transition_id=str(uuid4()), alert_id=str(alert_id),
            transition_ts=_parse_ts(ev.prediction_ts),
            from_status=ev.from_status, to_status=ev.to_status,
            anomaly_score=ev.anomaly_score,
            threshold_open=ev.threshold_open,
            threshold_close=ev.threshold_close,
            window_end_cycle_id=ev.window_end_cycle_id,
            valve_id=ev.valve_id, fault_type=ev.fault_type,
            run_id=run)
        n += 1
    return n


def load_states(storage: Any, config: AlertConfig | None = None,
                run_id: str | None = None) -> dict:
    """Ricostruisce `AlertEngine.states` dalla tabella `alerts` (restart-safe).

    Legge le righe CURRENTI (read-only, nessuna scrittura) e produce il dict
    `{(valve_id, fault_type): AlertState}` da assegnare a `engine.states`
    (o fare `.update(...)`) prima di riprocessare: un restart non azzera
    l'accumulo (max_score_seen / n_cycles_above / opened_ts / last_seen_ts)
    e non duplica le transizioni (un alert già aperto resta aperto: la
    stessa finestra riprocessata emette "sustained", non una seconda
    "open").

    LIMITI ACCETTATI (non persistiti nella tabella `alerts`):
    - `cooldown_until` — dopo un restart un alert chiuso da meno del
      cooldown può riaprirsi subito (perdita documentata, POC);
    - `streak` pending pre-open (finestre sopra soglia prima di `open`) — non
      esiste una riga finché l'alert non è aperto, quindi al restart riparte
      da zero.
    La cronologia K/N score-only è ricostruita separatamente da
    `load_score_history`. `config` è riservato per compatibilità e oggi serve
    solo a validare l'argomento: non altera il caricamento.
    """
    if config is not None and not isinstance(config, AlertConfig):
        raise TypeError(f"config must be an AlertConfig, got {type(config).__name__}")
    # Solo gli allarmi del run richiesto: caricare quelli di un altro run
    # significherebbe ereditarne gli stati con una cronologia punteggi che
    # non li giustifica, e chiuderli alla prima finestra sotto soglia.
    run = _resolve_history_run_id(storage, run_id)
    states: dict[tuple[int, str], AlertState] = {}
    with storage.engine.connect() as conn:
        rows = conn.execute(
            select(storage.alerts)
            .where(storage.alerts.c.run_id == run)).fetchall()
    for d in map(_row_dict, rows):
        status = d["status"]
        if status not in ALERT_STATUSES:
            raise ValueError(
                f"alerts table has invalid status {status!r} for "
                f"(valve_id={d['valve_id']}, fault_type={d['fault_type']!r}); "
                f"must be one of {ALERT_STATUSES}")
        states[(d["valve_id"], d["fault_type"])] = AlertState(
            valve_id=d["valve_id"], fault_type=d["fault_type"], status=status,
            opened_ts=d["opened_ts"].isoformat() if d["opened_ts"] else None,
            last_seen_ts=d["last_seen_ts"].isoformat() if d["last_seen_ts"] else None,
            closed_ts=d["closed_ts"].isoformat() if d["closed_ts"] else None,
            max_score_seen=d["max_score_seen"] or 0.0,
            n_cycles_above=d["n_cycles_above"] or 0,
            opened_at_cycle_id=d["opened_at_cycle_id"],
            closed_at_cycle_id=d["closed_at_cycle_id"],
            # streak / cooldown_until: non persistiti → ripartono da zero
            # (perdita accettata e documentata qui sopra).
        )
    return states


def _score_history_sql() -> str:
    """Query delle ultime N prediction per ciascuna delle 35 valvole.

    Il valore bound `before_<valve>` è un limite esclusivo per la valvola. Il
    binding esplicito evita di interpolare valori runtime nella query.

    La cronologia è **per run** (`:run_id`, 2026-08-22). Prima non lo era, e
    poiché l'ordinamento è per `prediction_ts DESC`, le prediction di un run
    live entravano in testa alla storia di un run storico spingendone fuori
    le più vecchie, una alla volta. Misurato allora: otto delle nove valvole
    in allarme stavano a 150 su 150 e reggevano, ma la valvola 21 stava a 7
    su 150 — margine 2 sul K=5 — e si sarebbe spenta dopo 70 finestre live
    senza che nessun test fallisse.
    """
    valves = ", ".join(
        f"({valve_id}, CAST(:before_{valve_id} AS INTEGER))"
        for valve_id in range(1, 36))
    return f"""
        SELECT recent.valve_id, recent.anomaly_score
        FROM (VALUES {valves}) AS requested(valve_id, before_window_end_cycle_id)
        CROSS JOIN LATERAL (
            SELECT p.valve_id, p.anomaly_score, p.prediction_ts,
                   p.window_end_cycle_id, p.prediction_id
            FROM predictions AS p
            WHERE p.valve_id = requested.valve_id
              AND p.run_id = :run_id
              AND (requested.before_window_end_cycle_id IS NULL
                   OR p.window_end_cycle_id < requested.before_window_end_cycle_id)
              AND p.prediction_id <> ALL(CAST(:excluded_prediction_ids AS UUID[]))
            ORDER BY p.prediction_ts DESC, p.window_end_cycle_id DESC,
                     p.prediction_id DESC
            LIMIT :window
        ) AS recent
        ORDER BY recent.valve_id, recent.prediction_ts,
                 recent.window_end_cycle_id, recent.prediction_id
    """


def _resolve_history_run_id(storage: Any, run_id: str | None) -> str:
    """Run della cronologia allarmi: esplicito, altrimenti dal KV.

    Il fallback è `current_run_id`, cioè lo stesso run che le rotte di
    lettura mostrano: la cronologia degli allarmi e la pagina che la espone
    non possono guardare run diversi.
    """
    if run_id is not None:
        resolved = str(run_id).strip()
        if not resolved:
            raise ValueError("run_id vuoto: passare un run esplicito o None")
        return resolved
    from pipeline.cycles_storage import CURRENT_RUN_ID_KEY
    getter = getattr(storage, "get_machine_state", None)
    resolved = getter(CURRENT_RUN_ID_KEY) if callable(getter) else None
    if not isinstance(resolved, str) or not resolved.strip():
        raise RuntimeError(
            f"run della cronologia allarmi non risolvibile: passare run_id "
            f"oppure valorizzare il KV `{CURRENT_RUN_ID_KEY}`")
    return resolved.strip()


def load_score_history(
        storage: Any,
        config: AlertConfig,
        *,
        before_window_end_cycle_ids: Mapping[int, int] | None = None,
        excluded_prediction_ids: Sequence[str] | None = None,
        run_id: str | None = None,
) -> dict[int, deque[bool]]:
    """Ricostruisce le ultime N decisioni score-only senza effetti collaterali.

    Ogni deque contiene solo i boolean `anomaly_score >= threshold_open`, in
    ordine cronologico. Non crea entry senza prediction e non aggiunge padding.
    `excluded_prediction_ids` esclude per identità le prediction del lotto che
    `InferenceConsumer` processerà subito dopo il seed. Questo impedisce il
    doppio conteggio anche quando due run riusano lo stesso cycle id. Il limite
    per cycle id resta disponibile per i chiamanti precedenti.

    `run_id` delimita la storia a un solo run; con `None` si legge il KV
    `current_run_id`, la stessa fonte delle rotte di lettura. Un allarme
    appartiene al run che lo ha generato: mescolare le storie faceva sì che
    un run nuovo, entrando in testa per `prediction_ts`, chiudesse in
    silenzio gli allarmi di quello vecchio.

    Con K/N disabilitato ritorna subito un dict vuoto senza aprire una
    connessione al database.
    """
    if not isinstance(config, AlertConfig):
        raise TypeError(f"config must be an AlertConfig, got {type(config).__name__}")
    if not config.score_aggregation_enabled:
        return {}

    window = config.score_aggregation_window
    assert window is not None
    resolved_run_id = _resolve_history_run_id(storage, run_id)
    cutoffs = before_window_end_cycle_ids or {}
    params: dict[str, Any] = {
        "window": window,
        "run_id": resolved_run_id,
        "excluded_prediction_ids": [
            UUID(value) for value in (excluded_prediction_ids or ())
        ],
    }
    for valve_id in range(1, 36):
        cutoff = cutoffs.get(valve_id)
        params[f"before_{valve_id}"] = int(cutoff) if cutoff is not None else None

    history: dict[int, deque[bool]] = {}
    with storage.engine.connect() as conn:
        rows = conn.execute(text(_score_history_sql()), params).fetchall()
    for d in map(_row_dict, rows):
        valve_id = int(d["valve_id"])
        values = history.get(valve_id)
        if values is None:
            values = history[valve_id] = deque(maxlen=window)
        values.append(float(d["anomaly_score"]) >= config.threshold_open)
    return history


__all__ = ["AlertConfig", "AlertState", "AlertEvent", "AlertEngine",
           "persist_events", "load_states", "load_score_history"]
