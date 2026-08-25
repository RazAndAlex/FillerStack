"""Server OPC UA embedded (M6, ADR-0016) — bridge thread-safe verso RealtimeSim.

Architettura: il server asyncua gira in un THREAD asyncio separato
(`threading.Thread` + `asyncio.new_event_loop()`), mentre la simulazione
(`plcsim/realtime.py`, RealtimeSim) vive nel thread del chiamante
(main/serve oppure il thread del test in modalità stepped). Il bridge è
thread-safe per costruzione:

  - comandi in ingresso (server → sim): la write di un client su un tag
    SimulationControl viene intercettata dall'hook PreWrite del server
    (CallbackType.PreWrite) che valida e accoda. I comandi macchina
    (CmdStart/CmdStop/CmdReset) finiscono nella queue thread-safe di
    RealtimeSim.submit_command(); l'iniezione fault (ForceFault e
    parametri) è validata nel write handler e applicata subito
    all'engine (vedi "Iniezione fault" sotto).
  - tag in uscita (sim → server): il task di pubblicazione (ogni
    publish_ms, default 10 ms) drena `TagSnapshot.drain_changes()` e
    scrive i tag cambiati sui nodi OPC UA (push immediato — il pulse
    DataReady dura un solo scan, 10 ms reali, e un poll più lento lo
    perderebbe, spec §5).

Contratto tag: spec §4, namespace v1, radice `Filler01` (ns = 2: asyncua
riserva ns=1 per sé — l'indice è preso dal ritorno di
register_namespace e i NodeId sono costruiti programmaticamente da lì).
Oggetti: Machine (7 tag READ), ValveNN (16 tag READ, una per valvola
esposta — default Valve01) e SimulationControl (8 tag RW) — 31 tag totali
(M6: 7+13, M9/ADR-0020 issue M9-01: +3 per le feature ML). Tipi OPC UA
dei nodi allineati al contratto (Int64 per CycleCounter/BottleCounter/
LastCycleId, Int32 per State e tag Int32, Double per Speed*, Boolean,
String per DiagnosticStatus): il task di pubblicazione scrive con il
varianttype del nodo (nessuna conversione implicita).

Sicurezza: POC dichiarata (contesto §69, ADR-0016): server anonymous su
localhost, `set_security_policy([NoSecurity])` per silenziare i warning
della policy di default. Security policy/certificati rimandati a M11.

Nota dipendenze: il core congelato (plc.py, validation.py, config.py,
plant.py, run.py) NON importa mai moduli OPC UA — opcua_server.py è
osservatore del simulatore (importa solo .realtime e .scenario); nessun
import circolare (realtime/scenario non importano opcua_server).

Iniezione fault (spec §4.3 + decisione 1 §11 + D5): ForceFault è un
LIVELLO (resta TRUE finché non riscritto FALSE). La write di
ForceFault=TRUE (o di un parametro mentre è forzato) applica
`engine.inject(fault_type, valve_id, severity, duration_cycles)` con i
parametri correnti; ForceFault=FALSE applica `engine.remove(valve_id)`.
RealtimeSim non espone una coda fault (core congelato): l'applicazione è
EAGER nel write handler, non accodata — le singole assegnazioni dei
canali plant sono atomiche sotto GIL e il plant le legge al passo
successivo (race accettato e documentato per il POC, stessa natura del
bridge comandi). Mapping dei parametri (documentato):
  - FaultValve: indici del CONTRATTO 1-35 → valve_id interno 0-based
    (FaultValve - 1, coerente con TagSnapshot.exposed_valves).
  - FaultType: String validata SOLO contro FAULT_TYPES di
    plcsim/scenario.py ("TAIL_INSTABILITY" NON è un tipo engine — è lo
    scenario M9 — write rifiutata).
  - FaultSeverity: Double in (0,1]; per i tipi a delay
    (closing_delay/opening_delay) convertita in ms:
    ms = max(1, int(round(sev * 100.0))) — mapping 0-1 → 1-100 ms — e
    passata come INTERO all'engine (che la valida _is_int); per i tipi a
    ratio (restriction/pressure_instability/flowmeter_*) passata com'è.
  - FaultDurationCycles: Int32 >= 0 (0 = attivo finché ForceFault=FALSE;
    > 0 = countdown gestito da engine.on_cycle).

Codici di errore usati (write rifiutate): ua.StatusCodes.BadInvalidArgument
(argumento non valido: FaultType ignoto, tipo Python non valido),
ua.StatusCodes.BadOutOfRange (valore fuori range: FaultValve,
FaultSeverity, FaultDurationCycles), BadUserAccessDenied (delegato
all'access control per i tag READ — set_writable(False)). Se
engine.inject solleva ValueError (valvola coperta da un fault YAML dello
scenario) la write di ForceFault=TRUE è rifiutata con
BadInvalidArgument e il motivo è loggato.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

from asyncua import Server, ua
from asyncua.common.callback import CallbackType

from .realtime import RealtimeSim
from .scenario import FAULT_TYPES

__all__ = ("OpcuaServer", "NAMESPACE_URI", "MACHINE_TAGS", "VALVE_TAGS",
           "SIM_TAGS", "N_VALVES_CONTRACT")

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema del namespace v1 (spec §4) — (nome tag, varianttype OPC UA)
# ---------------------------------------------------------------------------
NAMESPACE_URI = "urn:plcsim:filler01"
# URI dell'applicazione (identità del server, esposta in Server_NamespaceArray
# alla posizione 1): DISTINTA dall'URI del namespace, così il namespace del
# contratto (spec §4, ns=2 naturale: asyncua riserva ns=1 per sé) resta 2.
APPLICATION_URI = "urn:plcsim:server"

MACHINE_TAGS: tuple = (          # §4.1 — tutti READ
    ("Running", ua.VariantType.Boolean),
    ("State", ua.VariantType.Int32),
    ("SpeedActual", ua.VariantType.Double),
    ("SpeedTarget", ua.VariantType.Double),
    ("BottleCounter", ua.VariantType.Int64),
    ("CycleCounter", ua.VariantType.Int64),
    ("DataReady", ua.VariantType.Boolean),
)

VALVE_TAGS: tuple = (            # §4.2 — tutti READ, schema generico ValveNN
    ("FillingTime_ms", ua.VariantType.Int32),
    ("TailTime_ms", ua.VariantType.Int32),
    ("TailPulse", ua.VariantType.Int32),
    ("PulseCount", ua.VariantType.Int32),
    ("Target", ua.VariantType.Int32),
    ("DeltaPulse", ua.VariantType.Int32),
    ("FillingStepOut", ua.VariantType.Int32),
    ("FillingOK", ua.VariantType.Boolean),
    ("FillQualityOK", ua.VariantType.Boolean),
    ("SequenceOK", ua.VariantType.Boolean),
    ("SampleValid", ua.VariantType.Boolean),
    ("DiagnosticStatus", ua.VariantType.String),
    # M9 (ADR-0020, issue M9-01): 3 tag per le 6 feature ML mancanti nel
    # contratto M6 (close_reason/position_limit/filling_overtime) — output
    # deterministici del PLC (validation.py), NON GT, machine-agnostici.
    ("CloseReason", ua.VariantType.String),
    ("PositionLimit", ua.VariantType.Boolean),
    ("FillingOvertime", ua.VariantType.Boolean),
    ("LastCycleId", ua.VariantType.Int64),
)

SIM_TAGS: tuple = (              # §4.3 — tutti RW
    ("CmdStart", ua.VariantType.Boolean),
    ("CmdStop", ua.VariantType.Boolean),
    ("CmdReset", ua.VariantType.Boolean),
    ("ForceFault", ua.VariantType.Boolean),
    ("FaultValve", ua.VariantType.Int32),
    ("FaultType", ua.VariantType.String),
    ("FaultSeverity", ua.VariantType.Double),
    ("FaultDurationCycles", ua.VariantType.Int32),
)

# path tag contratto → comando RealtimeSim (D2)
_CMD_TO_NAME = {"CmdStart": "start", "CmdStop": "stop", "CmdReset": "reset"}

# tipi fault "a delay": FaultSeverity (0,1] → ms (mapping 0-1 → 1-100 ms)
_DELAY_TYPES = frozenset({"closing_delay", "opening_delay"})

# valvole del CONTRATTO (1-35, spec §4.3); interne 0-based (valve_id = x - 1)
N_VALVES_CONTRACT = 35

# parametri iniziali dei tag SimulationControl (coerenti con _fault_params)
_DEFAULT_FAULT_PARAMS = {
    "valve": 1,                  # FaultValve (indice contratto)
    "fault_type": "restriction",  # FaultType (valido: primo di FAULT_TYPES)
    "severity": 0.5,             # FaultSeverity
    "duration": 0,               # FaultDurationCycles (0 = finché ForceFault=FALSE)
}


class OpcuaServer:
    """Server OPC UA embedded per il simulatore (thread asyncio separato).

    Parametri:
      realtime_sim: RealtimeSim già costruito (plcsim/realtime.py) — il
          server lo osserva (snapshot) e gli invia comandi (queue).
      port: porta TCP (default 4840).
      host: host di bind dell'endpoint (default "localhost"; es.
          "0.0.0.0" per esporre il server alla rete/ai container Docker —
          vedi deviazione registrata nel collaudo M7).
      endpoint: URL endpoint opc.tcp; se contiene ``{port}`` viene
          formattato con la porta (default
          ``opc.tcp://{host}:{port}/filler01/``).
      publish_ms: periodo del task di pubblicazione (default 10 ms).

    Ciclo di vita: ``start()`` avvia il thread asyncio (init server,
    namespace, nodi, task di pubblicazione, server.start()) e attende che
    sia pronto; ``stop()`` ferma task + server + thread (join). Riavviabile:
    ogni start() ricostruisce thread/loop/server/namespace da zero — i tag
    READ ripartono dallo stato CORRENTE dello snapshot (coerenza
    server↔sim), i parametri fault e i livelli ripartono dai default.
    Attenzione: stop() durante start() (prima che il server sia pronto)
    non è garantito (thread join con timeout) — usare start()/stop()
    sequenziali.

    Espone ``TAG_PATHS`` (dict path contratto → Node asyncua, popolato a
    runtime da start()) e ``get_node(path)``.
    """

    def __init__(self, realtime_sim: RealtimeSim, port: int = 4840,
                 endpoint: Optional[str] = None, publish_ms: float = 10.0,
                 host: str = "localhost"):
        if not isinstance(realtime_sim, RealtimeSim):
            raise TypeError(
                f"realtime_sim deve essere una RealtimeSim (ricevuto "
                f"{type(realtime_sim).__name__})")
        if int(port) < 0 or int(port) > 65535:
            raise ValueError(f"porta fuori range: {port!r}")
        if publish_ms <= 0:
            raise ValueError(f"publish_ms deve essere > 0 (ricevuto {publish_ms!r})")
        self._sim = realtime_sim
        self.port = int(port)
        if endpoint is None:
            endpoint = f"opc.tcp://{host}:{self.port}/filler01/"
        self.endpoint = endpoint.format(port=self.port)
        self.publish_ms = float(publish_ms)
        # -- stato runtime (solo thread asyncio; azzerato a ogni start) -----
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._stop_evt: Optional[asyncio.Event] = None
        self._server: Optional[Server] = None
        self._publish_task: Optional[asyncio.Task] = None
        # path contratto → (Node, varianttype) costruiti da start()
        self.TAG_PATHS: dict[str, Any] = {}
        self._vtypes: dict[str, ua.VariantType] = {}
        self._nodeid_to_path: dict[ua.NodeId, str] = {}
        # -- bridge comandi --------------------------------------------------
        # tag Cmd* in attesa di auto-reset (dopo che il sim ha consumato)
        self._pending_cmd_reset: set[str] = set()
        # -- parametri fault (level, spec §4.3) ------------------------------
        self._fault_params: dict = dict(_DEFAULT_FAULT_PARAMS)
        self._force = False
        self.namespace_index: Optional[int] = None

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        """Avvia il thread asyncio e attende che il server sia pronto.

        Dopo il ritorno il server ascolta sull'endpoint e TAG_PATHS è
        popolato. Rilancia l'errore di avvio se il thread fallisce.
        """
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("server OPC UA già avviato")
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._run_thread, name="plcsim-opcua", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=20.0):
            raise RuntimeError("timeout di avvio del server OPC UA "
                               f"({self.endpoint})")
        if self._start_error is not None:
            raise RuntimeError(
                f"avvio del server OPC UA fallito: {self._start_error}") \
                from self._start_error

    def stop(self) -> None:
        """Ferma task di pubblicazione + server + thread (join, riavviabile)."""
        stop_evt = self._stop_evt
        loop = self._loop
        if stop_evt is not None and loop is not None and loop.is_running():
            loop.call_soon_threadsafe(stop_evt.set)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10.0)
            if thread.is_alive():
                _logger.warning("thread server OPC UA non terminato entro 10 s")
        # azzera lo stato runtime (un nuovo start() ricostruisce tutto)
        self._thread = None
        self._loop = None
        self._stop_evt = None
        self._server = None
        self._publish_task = None
        self.TAG_PATHS.clear()
        self._vtypes.clear()
        self._nodeid_to_path.clear()
        self._pending_cmd_reset.clear()
        self._fault_params = dict(_DEFAULT_FAULT_PARAMS)
        self._force = False
        self.namespace_index = None
        self._ready.clear()

    def get_node(self, path: str):
        """Nodo OPC UA del tag (path contratto, es. 'Machine.Running').

        Ritorna None se il path non esiste nel namespace costruito.
        """
        return self.TAG_PATHS.get(path)

    # ---------------------------------------------------------- internals --
    def _run_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:  # avvio fallito: sblocca start() con l'errore
            self._start_error = exc
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                self._loop = None

    async def _serve(self) -> None:
        self._stop_evt = asyncio.Event()
        server = Server()
        server.set_endpoint(self.endpoint)
        server.set_server_name("FillerStack — Filler01 (M6 POC)")
        # POC: anonymous su localhost, security policy a M11 (contesto §69)
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        await server.init()          # PRIMA di register_namespace (probe M6)
        # set_application_uri sostituisce la voce 1 del namespace array:
        # URI dell'applicazione distinto da quello del namespace (ns=2)
        await server.set_application_uri(APPLICATION_URI)
        self.namespace_index = await server.register_namespace(NAMESPACE_URI)
        self._server = server
        await self._build_address_space(server, self.namespace_index)
        # hook write client → bridge comandi (server → sim)
        server.subscribe_server_callback(CallbackType.PreWrite, self._on_pre_write)
        self._publish_task = asyncio.create_task(self._publish_loop())
        try:
            await server.start()
            self._ready.set()
            await self._stop_evt.wait()
        finally:
            if self._publish_task is not None:
                self._publish_task.cancel()
                try:
                    await self._publish_task
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                await server.stop()
            except Exception:
                _logger.exception("errore durante server.stop()")

    # ---------------------------------------------------- address space -----
    async def _build_address_space(self, server: Server, ns: int) -> None:
        """Costruisce gli oggetti e i 31 tag del contratto (spec §4).

        Valori iniziali letti dallo snapshot della simulazione (coerenza
        server ↔ sim); i nodi READ hanno AccessLevel=CurrentRead
        (set_writable(False)), i tag SimulationControl sono RW. NodeId
        stringa leggibili ('Filler01.Machine.Running', ns = indice
        registrato); il BrowseName resta l'ultimo segmento.
        """
        init = self._sim.snapshot.read()   # dict annidato thread-safe
        root = await server.nodes.objects.add_object(
            ua.NodeId("Filler01", ns), "Filler01")
        # -- Machine (READ) --------------------------------------------------
        machine = await root.add_object(
            ua.NodeId("Filler01.Machine", ns), "Machine")
        for tag, vtype in MACHINE_TAGS:
            path = f"Machine.{tag}"
            node = await machine.add_variable(
                ua.NodeId(f"Filler01.Machine.{tag}", ns), tag,
                init["Machine"].get(tag), varianttype=vtype)
            await node.set_writable(False)
            self.TAG_PATHS[path] = node
            self._vtypes[path] = vtype
            self._nodeid_to_path[node.nodeid] = path
        # -- ValveNN (READ, una per valvola esposta) -------------------------
        for v in self._sim.snapshot.exposed_valves:   # indici del contratto
            group = f"Valve{v:02d}"
            obj = await root.add_object(
                ua.NodeId(f"Filler01.{group}", ns), group)
            for tag, vtype in VALVE_TAGS:
                path = f"{group}.{tag}"
                node = await obj.add_variable(
                    ua.NodeId(f"Filler01.{group}.{tag}", ns), tag,
                    init[group].get(tag), varianttype=vtype)
                await node.set_writable(False)
                self.TAG_PATHS[path] = node
                self._vtypes[path] = vtype
                self._nodeid_to_path[node.nodeid] = path
        # -- SimulationControl (RW, bridge comandi) --------------------------
        sim_ctrl = await root.add_object(
            ua.NodeId("Filler01.SimulationControl", ns), "SimulationControl")
        sim_init = {
            "CmdStart": False, "CmdStop": False, "CmdReset": False,
            "ForceFault": self._force,
            "FaultValve": self._fault_params["valve"],
            "FaultType": self._fault_params["fault_type"],
            "FaultSeverity": self._fault_params["severity"],
            "FaultDurationCycles": self._fault_params["duration"],
        }
        for tag, vtype in SIM_TAGS:
            path = f"SimulationControl.{tag}"
            node = await sim_ctrl.add_variable(
                ua.NodeId(f"Filler01.SimulationControl.{tag}", ns), tag,
                sim_init[tag], varianttype=vtype)
            await node.set_writable(True)
            self.TAG_PATHS[path] = node
            self._vtypes[path] = vtype
            self._nodeid_to_path[node.nodeid] = path

    # ------------------------------------------------------- bridge write --
    async def _on_pre_write(self, event, callback_service) -> None:
        """Hook PreWrite: intercetta le write dei client sui tag di controllo.

        Il dispatch PreWrite avviene PRIMA di attribute_service.write: le
        write valide procedono normalmente (il tag resta col valore
        scritto dal client; i pulse Cmd* vengono auto-resettati a FALSE
        dal task di pubblicazione); le write non valide alzano
        ua.UaStatusCodeError che il server converte in ServiceFault per
        il client (write rifiutata con il codice dell'errore).
        """
        params = getattr(event, "request_params", None)
        if not isinstance(params, ua.WriteParameters):
            return
        for wv in params.NodesToWrite:
            if wv.AttributeId != ua.AttributeIds.Value:
                continue
            path = self._nodeid_to_path.get(wv.NodeId)
            if path is None:
                continue  # write su nodi non di controllo: non nostra
            dv = wv.Value
            value = dv.Value.Value if dv.Value is not None else None
            # TAG_PATHS usa path completi ('SimulationControl.CmdStop'):
            # al dispatch interessa il nome del tag (CmdStop, ForceFault, ...)
            tag = path[len("SimulationControl."):] \
                if path.startswith("SimulationControl.") else path
            self._handle_client_write(tag, value)

    def _handle_client_write(self, tag: str, value) -> None:
        """Validazione + dispatch di una write su SimulationControl.

        `tag` è il nome del tag (CmdStop, ForceFault, FaultValve, ...).
        Alza ua.UaStatusCodeError per rifiutare la write. Nota: l'access
        control (set_writable) blocca già le write sui tag READ con
        BadUserAccessDenied — qui arrivano solo i tag SimulationControl RW.
        """
        if tag in _CMD_TO_NAME:
            # (a) comandi macchina: fronte TRUE → coda al sim (pulse).
            # La write è accettata (Good); l'auto-reset a FALSE avviene
            # nel task di pubblicazione dopo che il sim ha consumato
            # (command_pending == False).
            if value is True:
                self._sim.submit_command({"name": _CMD_TO_NAME[tag]})
                self._pending_cmd_reset.add(f"SimulationControl.{tag}")
            return
        if tag == "FaultValve":
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 1 <= value <= N_VALVES_CONTRACT):
                _logger.warning(
                    "write FaultValve rifiutata: %r (attesi interi 1-%d)",
                    value, N_VALVES_CONTRACT)
                raise ua.UaStatusCodeError(ua.StatusCodes.BadOutOfRange)
            self._commit_fault_param("valve", value)
            return
        if tag == "FaultType":
            if not isinstance(value, str) or value not in FAULT_TYPES:
                _logger.warning(
                    "write FaultType rifiutata: %r (attesi: %s)", value,
                    ", ".join(FAULT_TYPES))
                raise ua.UaStatusCodeError(ua.StatusCodes.BadInvalidArgument)
            self._commit_fault_param("fault_type", value)
            return
        if tag == "FaultSeverity":
            if (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0.0 < float(value) <= 1.0):
                _logger.warning(
                    "write FaultSeverity rifiutata: %r (attesa in (0,1])",
                    value)
                raise ua.UaStatusCodeError(ua.StatusCodes.BadOutOfRange)
            self._commit_fault_param("severity", float(value))
            return
        if tag == "FaultDurationCycles":
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < 0):
                _logger.warning(
                    "write FaultDurationCycles rifiutata: %r (atteso intero "
                    ">= 0)", value)
                raise ua.UaStatusCodeError(ua.StatusCodes.BadOutOfRange)
            self._commit_fault_param("duration", value)
            return
        if tag == "ForceFault":
            if not isinstance(value, bool):
                raise ua.UaStatusCodeError(ua.StatusCodes.BadInvalidArgument)
            if value:
                # livello ON: applica subito l'iniezione coi parametri
                # correnti; se l'engine la rifiuta (ValueError: valvola
                # coperta da fault YAML) la write è rifiutata e il livello
                # NON viene attivato.
                self._apply_fault(self._fault_params)
                self._force = True
            else:
                self._force = False
                engine = self._sim.engine
                if engine is not None:
                    engine.remove(self._fault_params["valve"] - 1)
            return

    def _commit_fault_param(self, key: str, value) -> None:
        """Aggiorna un parametro fault; se forzato, riapplica il set.

        La riapplicazione avviene sul set NUOVO (copia): se l'engine la
        rifiuta la write è rifiutata e i parametri restano quelli
        precedenti (nessuno stato incoerente).
        """
        new_params = dict(self._fault_params)
        new_params[key] = value
        if self._force:
            self._apply_fault(new_params)   # può alzare BadInvalidArgument
        self._fault_params = new_params

    def _apply_fault(self, params: dict) -> None:
        """Applica engine.inject con un set di parametri (semantica level).

        Prima rimuove il fault runtime della valvola target (level: un
        solo fault attivo alla volta; remove() è no-op sicuro se la
        valvola non ha un fault runtime) poi inietta il nuovo set.
        engine.inject ha già semantica REPLACE per la stessa valvola.

        Senza engine (serve senza scenario): write accettata ma nessuna
        iniezione — warning loggato (serve.py potrà rifiutare a monte).
        ValueError di inject (valvola coperta da fault YAML) → write
        rifiutata con BadInvalidArgument + messaggio nel log.
        """
        engine = self._sim.engine
        valve_id = params["valve"] - 1        # contratto 1-35 → interno 0-34
        severity: Any = params["severity"]
        if params["fault_type"] in _DELAY_TYPES:
            # spec §11 decisione 1: FaultSeverity (0,1] → ms (1-100 ms)
            severity = max(1, int(round(severity * 100.0)))
        if engine is None:
            _logger.warning(
                "ForceFault=TRUE accettata ma il sim non ha engine (serve "
                "senza scenario): nessuna iniezione applicata")
            return
        try:
            engine.remove(valve_id)
            engine.inject(params["fault_type"], valve_id, severity,
                          params["duration"])
        except ValueError as exc:
            _logger.error("iniezione fault rifiutata: %s", exc)
            raise ua.UaStatusCodeError(
                ua.StatusCodes.BadInvalidArgument) from exc

    # ----------------------------------------------------- pubblicazione ----
    async def _publish_loop(self) -> None:
        """Task periodico (publish_ms): push cambi snapshot + auto-reset."""
        while True:
            await asyncio.sleep(self.publish_ms / 1000.0)
            try:
                await self._publish_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("errore nel tick di pubblicazione")

    async def _publish_tick(self) -> None:
        """Un tick: (1) drain dei cambi snapshot → nodi (push immediato,
        incluso il pulse DataReady); (2) auto-reset dei pulse Cmd* dopo
        che il sim ha consumato il comando (command_pending == False).

        I tipi dei valori scritti combaciano col tipo del nodo
        (varianttype esplicito, spec §4): Int64 per i contatori,
        Int32 per State e tag Int32, Double per Speed*, Boolean, String.
        """
        for path, value in self._sim.snapshot.drain_changes():
            try:
                node = self.TAG_PATHS.get(path)
                if node is None:
                    continue  # tag dello snapshot non esposto nel namespace
                await node.write_value(value, varianttype=self._vtypes[path])
            except Exception:
                _logger.exception("pubblicazione tag %s fallita", path)
        if self._pending_cmd_reset and not self._sim.command_pending:
            for path in tuple(self._pending_cmd_reset):
                try:
                    await self.TAG_PATHS[path].write_value(
                        False, varianttype=ua.VariantType.Boolean)
                except Exception:
                    _logger.exception("auto-reset comando %s fallito", path)
                self._pending_cmd_reset.discard(path)
