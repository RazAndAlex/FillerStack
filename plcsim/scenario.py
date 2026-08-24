"""Layer 6 (M2/M3) — fault engine + loader YAML scenario (ADR-0012/0013, piano M2 §1-§3).

M3 (pressure_instability): iniezione di ampiezza del driver lento per-valvola
(ADR-0010) sui fault con scope group/global; canale plant._amp_mult.

M4 (flowmeter): iniezione sul canale osservabile plant.last_pulses tramite la
Flowmeter (sensors.py) — dropout (frazione di scan persi) e glitch (tasso di
scan spuri); le maschere si cambiano SOLO via setter e il wrapper per-scan è
installato SOLO quando lo scenario dichiara fault flowmeter. La Flowmeter è
l'unico detentore dello stream sensori (ADR-0013); il fault engine resta
puro e deterministico.

M6 (runtime fault injection, ADR-0016): FaultEngine.inject() inserisce a run
in corso fault locali per-valvola con la stessa semantica YAML dei tipi,
applicazione immediata sui canali plant (riuso di _apply/_apply_amp) e
rimozione esplicita (remove) o countdown in on_cycle. Il registro _runtime è
vuoto nel percorso YAML (no-op a costo minimo) e non viene aggiunto alcun
draw RNG — bit-identità bulk M6 ≡ M5 preservata. Policy thread dell'engine:
inject/remove/countdown serializzati da un lock interno (_rt_lock);
l'applicazione dei canali plant a grana di scan (element-wise GIL) resta
best-effort documentato.

Determinismo puro: nessun generatore casuale, nessuna dipendenza dal PLC.
Le iniezioni sono fattori moltiplicativi di portata (restriction), offset
di tempo meccanici (closing_delay / opening_delay), moltiplicatori di
ampiezza del driver (pressure_instability) e maschere del canale impulsi
(flowmeter), applicati al plant al confine di ciclo; la ground truth usa
rec.cycle_id del CycleRecord (nessun contatore proprio — disposizione PLC-F1).

Nota (ADR-0013): il fallback loader minimale è documentato ma NON
implementato in M2 — pyyaml è l'unico parser (errore chiaro su ImportError).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np

from .sensors import Flowmeter

FAULT_TYPES = ("restriction", "closing_delay", "opening_delay",
               "pressure_instability", "flowmeter_dropout",
               "flowmeter_glitch")
ONSET_MODES = ("abrupt", "gradual")
N_VALVES = 35          # indice valvola target: 0..34
N_GROUPS = 6           # gruppi di valvole: rispecchia config.default_groups(35, 6): G0..G5


# --------------------------------------------------------------------------
# Modello scenario
# --------------------------------------------------------------------------
@dataclass
class FaultOnset:
    mode: str                    # "abrupt" | "gradual"
    start_cycle: int             # 1-based, ciclo della valvola target
    ramp_cycles: int | None = None   # solo gradual, > 0


@dataclass
class FaultSpec:
    fault_type: str              # restriction | closing_delay | opening_delay
                                 # | pressure_instability
    scope: str                   # local | group | global (group/global:
                                 # SOLO pressure_instability)
    valve_id: int | None         # indice valvola target 0..34; obbligatorio
                                 # SOLO con scope local
    severity: float              # restriction/pressure_instability: (0,1];
                                 # delays: ms >= 1
    onset: FaultOnset
    group_id: int | None = None  # obbligatorio SOLO con scope group (0..5)


@dataclass
class Scenario:
    scenario_id: int
    name: str
    seed: int | None
    faults: list[FaultSpec] = field(default_factory=list)

    def faults_active_at(self, valve_id: int, cycle_id: int) -> list[FaultSpec]:
        """Fault della valvola attivi al ciclo (severità > 0)."""
        return [f for f in self.faults
                if f.valve_id == valve_id and severity_at(f, cycle_id) > 0.0]


def severity_at(fault: FaultSpec, cycle_id: int) -> float:
    """Severità applicata al ciclo `cycle_id` della valvola target.

    0 prima di start_cycle; abrupt → severità piena; gradual → rampa lineare
    severity·min(1, (cycle_id − start_cycle + 1)/ramp_cycles), completa al
    ciclo start_cycle + ramp_cycles − 1 (piano §2).
    """
    o = fault.onset
    if cycle_id < o.start_cycle:
        return 0.0
    if o.mode == "abrupt":
        return float(fault.severity)
    k = (cycle_id - o.start_cycle + 1) / o.ramp_cycles
    return float(fault.severity) * min(1.0, k)


def affected_valves(fault: FaultSpec, cfg) -> list[int]:
    """Valvole colpite dal fault (local -> [valve_id]; group -> cfg.groups[group_id];
    global -> tutte le cfg.valves)."""
    if fault.scope == "local":
        return [fault.valve_id]
    if fault.scope == "group":
        if not (0 <= fault.group_id < len(cfg.groups)):
            raise ValueError(f"group_id {fault.group_id} fuori range per "
                             f"cfg.groups (len {len(cfg.groups)})")
        return list(cfg.groups[fault.group_id])
    return list(range(len(cfg.valves)))


# --------------------------------------------------------------------------
# Loader YAML (schema canonico, piano §3)
# --------------------------------------------------------------------------
def _is_int(value) -> bool:
    """Intero vero (i booleani YAML non sono interi validi qui)."""
    return isinstance(value, int) and not isinstance(value, bool)


def load_scenario(path) -> Scenario:
    """Carica e valida uno scenario YAML.

    Errori di validazione → ValueError con messaggio in italiano. Se pyyaml
    non è installato: ValueError esplicito con l'istruzione di installazione.
    """
    try:
        import yaml
    except ImportError:
        raise ValueError("pyyaml non installato: pip install pyyaml") from None
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return _parse(data)


def _parse(data) -> Scenario:
    if not isinstance(data, dict) or "scenario_id" not in data:
        raise ValueError("scenario_id mancante")
    if not _is_int(data["scenario_id"]):
        raise ValueError("scenario_id deve essere un intero")
    if not isinstance(data.get("name"), str) or not data["name"]:
        raise ValueError("name mancante")
    seed = data.get("seed")
    if seed is not None and not _is_int(seed):
        raise ValueError("seed deve essere un intero o null")
    faults_raw = data.get("faults")
    if not isinstance(faults_raw, list):
        raise ValueError("faults deve essere una lista")
    faults = [_parse_fault(f) for f in faults_raw]
    # i fault group/global hanno valve_id None: il check duplicati copre i
    # local (gli overlap via gruppo si risolvono nel costruttore engine)
    valvole = [f.valve_id for f in faults if f.valve_id is not None]
    if len(valvole) != len(set(valvole)):
        raise ValueError("due fault sulla stessa valvola")
    return Scenario(scenario_id=data["scenario_id"], name=data["name"],
                    seed=seed, faults=faults)


def _parse_fault(raw) -> FaultSpec:
    if not isinstance(raw, dict):
        raise ValueError("fault non valido: deve essere una mappa")
    ftype = raw.get("fault_type")
    if ftype not in FAULT_TYPES:
        raise ValueError(f"fault_type sconosciuto: {ftype!r} (attesi: "
                         f"{', '.join(FAULT_TYPES)})")
    scope = raw.get("scope")
    valve_id = raw.get("valve_id")
    group_id = raw.get("group_id")
    if ftype == "pressure_instability":
        if valve_id is not None:
            raise ValueError("valve_id non ammesso con scope group/global")
        if group_id is not None and scope != "group":
            raise ValueError("group_id non ammesso con scope local/global")
        if scope not in ("group", "global"):
            raise ValueError("scope deve essere 'group' o 'global' "
                             "per pressure_instability")
        if scope == "group":
            if group_id is None:
                raise ValueError("group_id obbligatorio con scope=group")
            if not _is_int(group_id) or not 0 <= group_id <= N_GROUPS - 1:
                raise ValueError(f"group_id fuori range [0,{N_GROUPS - 1}]")
    else:
        if scope != "local":
            raise ValueError("scope deve essere 'local' (group/global solo "
                             "per pressure_instability)")
        if not _is_int(valve_id) or not 0 <= valve_id <= N_VALVES - 1:
            raise ValueError(f"valve_id fuori range [0,{N_VALVES - 1}]: "
                             f"{valve_id!r}")
        if group_id is not None:
            raise ValueError("group_id non ammesso con scope local/global")
    severity = raw.get("severity")
    if ftype in ("restriction", "pressure_instability",
                 "flowmeter_dropout", "flowmeter_glitch"):
        if (isinstance(severity, bool) or not isinstance(severity, (int, float))
                or not 0.0 < severity <= 1.0):
            raise ValueError(f"severity {ftype} deve essere in (0,1]: "
                             f"{severity!r}")
        sev = float(severity)
    else:
        if not _is_int(severity) or severity < 1:
            raise ValueError(f"severity {ftype} deve essere un intero >= 1 ms: "
                             f"{severity!r}")
        sev = float(severity)
    onset_raw = raw.get("onset")
    if not isinstance(onset_raw, dict):
        raise ValueError("onset mancante")
    mode = onset_raw.get("mode")
    if mode not in ONSET_MODES:
        raise ValueError(f"onset.mode sconosciuto: {mode!r} (attesi: "
                         f"{', '.join(ONSET_MODES)})")
    ramp = onset_raw.get("ramp_cycles")
    if mode == "gradual":
        if ramp is None:
            raise ValueError("onset.ramp_cycles obbligatorio con mode=gradual")
        if not _is_int(ramp) or ramp <= 0:
            raise ValueError("onset.ramp_cycles deve essere un intero > 0")
    elif ramp is not None:
        raise ValueError("onset.ramp_cycles non ammesso con mode=abrupt")
    start_cycle = onset_raw.get("start_cycle")
    if not _is_int(start_cycle) or start_cycle < 1:
        raise ValueError("onset.start_cycle deve essere un intero >= 1")
    return FaultSpec(fault_type=ftype, scope=scope, valve_id=valve_id,
                     severity=sev,
                     onset=FaultOnset(mode=mode, start_cycle=start_cycle,
                                      ramp_cycles=ramp),
                     group_id=group_id)


# --------------------------------------------------------------------------
# Fault engine (nessun generatore casuale: determinismo puro, ADR-0013)
# --------------------------------------------------------------------------
@dataclass
class RuntimeFault:
    """Fault runtime iniettato via FaultEngine.inject() (M6, ADR-0016).

    Dataclass privata del registro _runtime: fault_type + severità applicata
    + remaining_cycles (None = attivo finché remove() esplicito; int > 0 =
    countdown decrementato a ogni on_cycle della valvola target).
    """
    fault_type: str
    severity: float
    remaining_cycles: int | None = None


class FaultEngine:
    """Applica i fault al plant e produce ground truth + eventi (M2).

    Nessun contatore proprio di cicli: severità GT e iniezione usano
    rec.cycle_id (disposizione PLC-F1). L'iniezione per il ciclo k+1 è
    applicata quando on_cycle processa il record del ciclo k (la valvola è
    chiusa tra i cicli: nessun effetto fisico intermedio); per start_cycle=1
    l'iniezione è pre-applicata alla costruzione (caso limite QA-F7).

    Restriction: plant._restriction[valve] = 1 − s (fattore di portata,
    multiplo per-valvola nel loop del plant). Closing/opening delay:
    plant.mech[valve].{close,open}_delay_ms = int(s) — severità in ms,
    troncata all'intero (documentato). Pressure_instability (M3):
    plant._amp_mult[valve] = 1 + s (moltiplicatore dell'ampiezza del
    driver lento, per-valvola; i fault group/global coprono le valvole di
    cfg.groups[group_id] / tutte). Flowmeter (M4): maschere per-valvola
    sulla Flowmeter (sensors.py) via set_dropout/set_glitch (s ∈ (0,1]),
    monotone come gli altri tipi; il wrapper per-scan (installato SOLO
    con fault flowmeter nello scenario) chiama apply() dopo plant.step.
    Eventi: tuple (ts_ms int, machine_code, evento, nota, cycle_id).

    M6 (runtime fault injection, ADR-0016): FaultEngine.inject() applica a
    run in corso un fault locale per-valvola con la stessa semantica YAML
    (riuso di _apply/_apply_amp; pressure_instability locale = estensione
    documentata), installa on-demand il wrapper per-scan della Flowmeter se
    serve, e mantiene il registro _runtime (valve_id -> RuntimeFault) con
    countdown in on_cycle e rimozione esplicita via remove(). Engine puro:
    nessun draw RNG aggiunto.
    """

    def __init__(self, plant, scenario: Scenario, cfg):
        self.plant = plant
        self.scenario = scenario
        self.cfg = cfg
        n = len(cfg.valves)
        # stato applicato per-valvola: fattori 1−s (ones = sano) e delay int
        self._applied_restriction = np.ones(n, dtype=np.float64)
        self._applied_open_delay = np.zeros(n, dtype=np.int64)
        self._applied_close_delay = np.zeros(n, dtype=np.int64)
        # stato applicato M4: maschere flowmeter per-valvola (0 = sana)
        self._applied_dropout = np.zeros(n, dtype=np.float64)
        self._applied_glitch = np.zeros(n, dtype=np.float64)
        self._events: list[tuple] = []
        # M6 (runtime fault injection, ADR-0016): registro fault runtime
        # per-valvola (valve_id -> RuntimeFault); vuoto nel percorso YAML ->
        # on_cycle resta no-op (bit-identità bulk M6 ≡ M5 preservata)
        self._runtime: dict[int, RuntimeFault] = {}
        # M6 (review M6-A1/D2): lock che serializza inject/remove/countdown
        # (thread server OPC UA vs thread loop). Nessun lock annidato:
        # inject/on_cycle usano _remove_locked (lock già acquisito).
        self._rt_lock = threading.Lock()
        # fault locali per-valvola (valve_id non None; gli overlap locali
        # sono già esclusi dal parser: comportamento M2 identico)
        self._fault_by_valve = {f.valve_id: (i, f)
                                for i, f in enumerate(scenario.faults)
                                if f.valve_id is not None}
        # M3 (pressure_instability): fault group/global per-valvola; overlap
        # gruppo-gruppo o locale-gruppo sulla stessa valvola -> errore
        self._group_by_valve: dict[int, FaultSpec] = {}
        for i, f in enumerate(scenario.faults):
            if f.scope == "local":
                continue
            for v in affected_valves(f, cfg):
                if v in self._group_by_valve or v in self._fault_by_valve:
                    raise ValueError("due fault sulla stessa valvola")
                self._group_by_valve[v] = f
        # stato applicato dell'ampiezza del driver (ones = sano)
        self._applied_amp_mult = np.ones(n, dtype=np.float64)
        # timeline: primo ciclo affetto per (indice fault, valvola) — per i
        # local la chiave è (i, valve_id): compatibile col comportamento M2
        self._first_cycle_ts: dict[tuple[int, int], int | None] = {
            (i, v): None
            for i, f in enumerate(scenario.faults)
            for v in affected_valves(f, cfg)}
        # FAULT_START una volta per fault (i local: invariato vs M2)
        self._started_faults: set[int] = set()
        # M4 (flowmeter): hook per-scan. La Flowmeter è l'unico detentore
        # dello stream sensori (ADR-0013) e la guardia _active tiene il
        # percorso sano a costo zero. Il wrapper è installato SOLO quando lo
        # scenario dichiara almeno un fault flowmeter: negli scenari
        # M2/M3/healthy plant.step resta l'originale (bit-identità M4-sano ≡ M3).
        self._flowmeter = Flowmeter(cfg, plant)
        if any(f.fault_type in ("flowmeter_dropout", "flowmeter_glitch")
               for f in scenario.faults):
            self._orig_step = plant.step
            plant.step = self._step_flowmeter
        # QA-F7: start_cycle <= 1 → iniezione pre-applicata alla costruzione
        # (local: canale M2; group/global: canale ampiezza M3)
        for f in scenario.faults:
            if f.onset.start_cycle <= 1:
                if f.scope == "local":
                    self._apply(f, severity_at(f, 1))
                else:
                    for v in affected_valves(f, cfg):
                        self._apply_amp(f, v, severity_at(f, 1))

    # -- iniezione ---------------------------------------------------------
    def _apply(self, fault: FaultSpec, s_next: float) -> None:
        """Applica (se più severa dell'attuale) l'iniezione per-valvola."""
        v = fault.valve_id
        if fault.fault_type == "restriction":
            factor = 1.0 - s_next
            if factor < self._applied_restriction[v]:
                self._applied_restriction[v] = factor
                self.plant._restriction[v] = factor
        elif fault.fault_type == "closing_delay":
            delay = int(s_next)
            if delay > self._applied_close_delay[v]:
                self._applied_close_delay[v] = delay
                self.plant.mech[v].close_delay_ms = delay
        elif fault.fault_type == "flowmeter_dropout":
            if s_next > self._applied_dropout[v]:
                self._applied_dropout[v] = s_next
                self._flowmeter.set_dropout(v, s_next)
        elif fault.fault_type == "flowmeter_glitch":
            if s_next > self._applied_glitch[v]:
                self._applied_glitch[v] = s_next
                self._flowmeter.set_glitch(v, s_next)
        else:  # opening_delay
            delay = int(s_next)
            if delay > self._applied_open_delay[v]:
                self._applied_open_delay[v] = delay
                self.plant.mech[v].open_delay_ms = delay

    def _apply_amp(self, fault: FaultSpec, valve: int, s_next: float) -> None:
        """Iniezione M3: amp_v = 1 + s_next (incremento RELATIVO dell'ampiezza
        del driver); applicata se più severa dell'attuale (rampa monotona)."""
        m = 1.0 + s_next
        if m > self._applied_amp_mult[valve]:
            self._applied_amp_mult[valve] = m
            self.plant._amp_mult[valve] = m

    # -- M6: fault injection runtime (ADR-0016) ------------------------------
    def inject(self, fault_type: str, valve_id: int, severity,
               duration_cycles: int = 0) -> None:
        """Inietta a run in corso un fault runtime per-valvola (M6, ADR-0016).

        Applicazione IMMEDIATA sui canali plant, stessa semantica YAML dei
        tipi (riuso di _apply/_apply_amp):
          - restriction            -> plant._restriction[v] = 1 − severity
          - closing/opening_delay  -> plant.mech[v].{close,open}_delay_ms =
            int ms >= 1
          - pressure_instability   -> plant._amp_mult[v] = 1 + severity
            (ESTENSIONE documentata: lo YAML la vuole solo scope group/global;
            via runtime il target è una singola valvola — scope locale)
          - flowmeter_dropout/glitch -> setter Flowmeter set_dropout/set_glitch;
            il wrapper per-scan è installato ON-DEMAND (se lo scenario non
            dichiara fault flowmeter) PRIMA del primo plant.step successivo.

        duration_cycles=0 -> il fault resta attivo finché remove(valve_id)
        esplicito (fault a livello, non schedulati); >0 -> countdown: il
        contatore decrementa a ogni on_cycle della valvola target e a 0 i
        canali vengono azzerati da remove().

        REPLACE: se la valvola ha già un fault runtime attivo, il nuovo
        inject rimuove il vecchio (canali azzerati) e applica il nuovo — una
        severità più blanda prende davvero effetto (_apply è monotona solo
        verso il più severo).

        ValueError: fault_type/valve_id/severity/duration_cycles non validi,
        o valvola già coperta da un fault YAML dello scenario (stessa regola
        duplicati del parser: _fault_by_valve / _group_by_valve).
        """
        if fault_type not in FAULT_TYPES:
            raise ValueError(f"fault_type sconosciuto: {fault_type!r} "
                             f"(attesi: {', '.join(FAULT_TYPES)})")
        n = len(self.cfg.valves)
        if not _is_int(valve_id) or not 0 <= valve_id < n:
            raise ValueError(f"valve_id fuori range [0,{n - 1}]: {valve_id!r}")
        sev = self._validate_runtime_severity(fault_type, severity)
        if not _is_int(duration_cycles) or duration_cycles < 0:
            raise ValueError("duration_cycles deve essere un intero >= 0: "
                             f"{duration_cycles!r}")
        if valve_id in self._fault_by_valve or valve_id in self._group_by_valve:
            raise ValueError(
                f"valvola {valve_id} già coperta da un fault YAML dello "
                f"scenario: inject runtime non ammesso (stessa regola "
                f"duplicati del parser)")
        # Sezione critica serializzata dal lock _rt_lock (policy thread
        # dell'engine): REPLACE + applicazione canali + registrazione nel
        # registro _runtime. Nessun lock annidato: si usa _remove_locked
        # (che assume il lock già acquisito), NON remove().
        with self._rt_lock:
            # REPLACE: rimozione del fault runtime precedente (canali azzerati)
            if valve_id in self._runtime:
                self._remove_locked(valve_id)
            # FaultSpec temporaneo (scope locale, onset irrilevante: applicazione
            # immediata) — riusa l'iniezione YAML per la stessa semantica per tipo
            fault = FaultSpec(fault_type=fault_type, scope="local",
                              valve_id=valve_id, severity=sev,
                              onset=FaultOnset(mode="abrupt", start_cycle=1))
            if fault_type == "pressure_instability":
                self._apply_amp(fault, valve_id, sev)
            else:
                if fault_type in ("flowmeter_dropout", "flowmeter_glitch"):
                    self._ensure_flowmeter_wrapper()
                self._apply(fault, sev)
            self._runtime[valve_id] = RuntimeFault(
                fault_type=fault_type, severity=sev,
                remaining_cycles=None if duration_cycles == 0
                else int(duration_cycles))

    def remove(self, valve_id: int) -> None:
        """Rimuove il fault runtime della valvola e azzera i canali plant.

        Rimozione esplicita: i fault runtime sono a livello, non schedulati
        (duration_cycles=0 resta attivo finché remove() non viene chiamato).
        Azzera TUTTI i canali della valvola (restriction 1.0, delays 0,
        amp_mult 1.0, maschere flowmeter 0 via setter) e lo stato applicato
        _applied_* (un inject successivo meno severo prende davvero effetto).
        No-op se la valvola non ha un fault runtime: i fault YAML non vengono
        MAI toccati.

        Thread-safety (policy thread dell'engine): serializzato con inject()
        e il countdown di on_cycle dal lock _rt_lock; il pop è idempotente
        (pop(valve_id, None)) — nessun check-then-pop non atomico.
        """
        with self._rt_lock:
            self._remove_locked(valve_id)

    def _remove_locked(self, valve_id: int) -> None:
        """Rimozione del fault runtime: assume _rt_lock già acquisito.

        Pop IDEMPOTENTE (pop(valve_id, None)): sotto il lock non ci sono
        check-then-pop non atomici; la guardia su _runtime evita il lavoro
        di azzeramento dei canali quando la valvola non ha un fault runtime.
        Chiamata solo da remove() (con il lock) e dall'interno della sezione
        critica di inject()/on_cycle (lock già acquisito: nessun lock
        annidato).
        """
        if valve_id not in self._runtime:
            return
        self._runtime.pop(valve_id, None)
        p = self.plant
        p._restriction[valve_id] = 1.0
        p.mech[valve_id].open_delay_ms = 0.0
        p.mech[valve_id].close_delay_ms = 0.0
        p._amp_mult[valve_id] = 1.0
        self._flowmeter.set_dropout(valve_id, 0.0)
        self._flowmeter.set_glitch(valve_id, 0.0)
        self._applied_restriction[valve_id] = 1.0
        self._applied_open_delay[valve_id] = 0
        self._applied_close_delay[valve_id] = 0
        self._applied_amp_mult[valve_id] = 1.0
        self._applied_dropout[valve_id] = 0.0
        self._applied_glitch[valve_id] = 0.0

    def _validate_runtime_severity(self, fault_type: str, severity) -> float:
        """Severità validata con la stessa semantica YAML per tipo."""
        if fault_type in ("restriction", "pressure_instability",
                          "flowmeter_dropout", "flowmeter_glitch"):
            if (isinstance(severity, bool)
                    or not isinstance(severity, (int, float))
                    or not 0.0 < severity <= 1.0):
                raise ValueError(f"severity {fault_type} deve essere in "
                                 f"(0,1]: {severity!r}")
            return float(severity)
        if not _is_int(severity) or severity < 1:
            raise ValueError(f"severity {fault_type} deve essere un intero "
                             f">= 1 ms: {severity!r}")
        return float(severity)

    def _ensure_flowmeter_wrapper(self) -> None:
        """Installa on-demand il wrapper per-scan della Flowmeter (M6).

        In __init__ il wrapper è installato SOLO quando lo scenario dichiara
        fault flowmeter; inject() con un fault flowmeter runtime deve
        garantirlo PRIMA del primo plant.step successivo. Se già installato
        (scenario con fault flowmeter), no-op. La rimozione di un fault
        flowmeter non disinstalla il wrapper: con maschere residue a 0 è una
        no-op a costo zero (guardia _active della Flowmeter).
        """
        if not hasattr(self, "_orig_step"):
            self._orig_step = self.plant.step
            self.plant.step = self._step_flowmeter

    def runtime_faults(self) -> dict[int, RuntimeFault]:
        """Copia del registro fault runtime (valve_id -> RuntimeFault).

        Per debug/tracciabilità; la ground truth resta quella dei fault YAML.
        """
        return dict(self._runtime)

    # -- hook per-scan M4 ----------------------------------------------------
    def _step_flowmeter(self, t_ms: int) -> None:
        """Wrapper per-scan: step fisico originale + iniezione Flowmeter.

        Installato dal FaultEngine SOLO quando lo scenario dichiara fault
        flowmeter: nel percorso sano (maschere a 0 / nessun fault) è una
        no-op con zero draw — bit-identità M4-sano ≡ M3 preservata.
        """
        self._orig_step(t_ms)
        late = self._flowmeter.apply(t_ms)
        if late:
            self._events.extend(late)

    # -- confine di ciclo --------------------------------------------------
    def on_cycle(self, rec) -> dict:
        """Hook al confine di ciclo: riga GT + iniezione k+1 + eventi.

        Riga GT: {"cycle_id", "machine_code", "ts_beg", "fault_type",
        "severity", "valve_id", "scenario_id"} — fault_type None e
        severity 0.0 per i cicli sani (schema ADR-0012).
        """
        valve_id = int(rec.machine_code.removeprefix("valve"))
        entry = self._fault_by_valve.get(valve_id)
        fault = entry[1] if entry else self._group_by_valve.get(valve_id)
        affetto = fault is not None and rec.cycle_id >= fault.onset.start_cycle
        severita = severity_at(fault, rec.cycle_id) if affetto else 0.0
        gt = {
            "cycle_id": rec.cycle_id,
            "machine_code": rec.machine_code,
            "ts_beg": rec.ts_beg,
            "fault_type": fault.fault_type if affetto else None,
            "severity": float(severita),
            "valve_id": valve_id,
            "scenario_id": self.scenario.scenario_id,
        }
        # (a) iniezione per il ciclo k+1 (se più severa di quella applicata)
        if fault is not None:
            if entry is not None:
                self._apply(fault, severity_at(fault, rec.cycle_id + 1))
            else:
                self._apply_amp(fault, valve_id,
                                severity_at(fault, rec.cycle_id + 1))
        # (b) eventi engine
        if affetto:
            if entry is not None:
                fi = entry[0]
            else:
                fi = next(i for i, f in enumerate(self.scenario.faults)
                          if f is fault)
            key = (fi, valve_id)
            if fi not in self._started_faults:
                self._started_faults.add(fi)
                note = (f"{fault.fault_type} severity={fault.severity} "
                        f"start_cycle={fault.onset.start_cycle}")
                if fault.scope == "group":
                    note += f" group_id={fault.group_id}"
                elif fault.scope == "global":
                    note += " scope=global"
                self._events.append((rec.ts_beg, rec.machine_code,
                                     "FAULT_START", note, rec.cycle_id))
            if self._first_cycle_ts[key] is None:
                self._first_cycle_ts[key] = rec.ts_beg
            if (fault.onset.mode == "gradual"
                    and rec.cycle_id < fault.onset.start_cycle
                    + fault.onset.ramp_cycles):
                self._events.append((rec.ts_beg, rec.machine_code,
                                     "FAULT_RAMP",
                                     f"ramp severity={severita}",
                                     rec.cycle_id))
        # comandi per OGNI ciclo completato (ADR-0012; i cicli abortiti non
        # producono CycleRecord né CMD)
        open_cmd = rec.ts_beg + self.cfg.flush_ms + self.cfg.pressurize_ms
        self._events.append((open_cmd, rec.machine_code, "CMD:OPEN", "",
                             rec.cycle_id))
        self._events.append((open_cmd + rec.fillingtime, rec.machine_code,
                             "CMD:CLOSE", rec.close_reason, rec.cycle_id))
        # (c) countdown fault runtime (M6, ADR-0016): decrementa a ogni ciclo
        # completato della valvola target; a 0 -> rimozione che azzera i
        # canali. Serializzato con inject/remove dal lock _rt_lock (policy
        # thread dell'engine): nessun TOCTOU tra il countdown e un inject/
        # remove concorrente del thread server. Registro vuoto (percorso
        # YAML) -> no-op: bit-identità M6 ≡ M5.
        with self._rt_lock:
            rt = self._runtime.get(valve_id)
            if rt is not None and rt.remaining_cycles is not None:
                rt.remaining_cycles -= 1
                if rt.remaining_cycles <= 0:
                    self._remove_locked(valve_id)
        return gt

    # -- coda eventi -------------------------------------------------------
    @property
    def has_events(self) -> bool:
        return bool(self._events)

    def take_events(self) -> list:
        """Drena la coda eventi interna."""
        out, self._events = self._events, []
        return out

    # -- timeline ----------------------------------------------------------
    def timeline(self) -> list[dict]:
        """Una riga per (fault, valvola affetta) (permanenti: end sempre null).

        local → una riga per fault (comportamento M2); group/global → una
        riga per valvola affetta. start_ts = ts_beg (ms) del primo ciclo
        affetto completato di QUELLA valvola, None se nessun ciclo
        processato finora; fault_id = indice 0-based nello YAML.
        """
        rows = []
        for i, f in enumerate(self.scenario.faults):
            for v in affected_valves(f, self.cfg):
                rows.append({
                    "scenario_id": self.scenario.scenario_id,
                    "fault_id": i,
                    "fault_type": f.fault_type,
                    "valve_id": v,
                    "severity": f.severity,
                    "onset_mode": f.onset.mode,
                    "start_cycle": f.onset.start_cycle,
                    "end_cycle": None,
                    "ramp_cycles": f.onset.ramp_cycles,
                    "start_ts": self._first_cycle_ts.get((i, v)),
                    "end_ts": None,
                })
        return rows
