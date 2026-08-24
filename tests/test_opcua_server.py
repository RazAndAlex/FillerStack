"""Test automatici M6 — server OPC UA (spec §7, test 1-8).

Modalità stepped, seed fisso 42, giorno template reale (lo skip delle fasi
vuote porta la macchina a Running in 2 iterazioni), scenario VUOTO
(engine attivo per il test 6 di fault injection, nessun fault YAML —
scenario_id 99, seed 42). Client asyncua in un thread asyncio separato
(helper submit = run_coroutine_threadsafe). Il server OPC UA gira nel suo
thread asyncio; i tag pubblicati ogni 10 ms: dopo advance() si attende
prima di leggere (spec §5, push ~10 ms).

Marker `opcua` NON registrato (nessun pytest.ini/pyproject in repo — fuori
scope M6): attesa PytestUnknownMarkWarning, i test restano verdi.

Copertura (spec §7):
  1. test_connect_browse        — sessione attiva, namespace Filler01 con i
     3 oggetti e i 31 tag del contratto coi tipi OPC UA (ns=2).
  2. test_read_static           — SpeedTarget=15500.0, State=4 all'avvio,
     Valve01.Target=2500 dopo il primo ciclo chiuso della valvola esposta.
  3. test_subscription          — CycleCounter avanza dopo N scan comandati
     (DataReady best-effort, spec §5: il pulse può coalescere).
  4. test_cmd_stop_cause_effect — CmdStop=TRUE → State 1→2→3, Running=FALSE,
     auto-reset a FALSE del pulse.
  5. test_permesso_negato       — write su tag READ → BadUserAccessDenied,
     valore invariato.
  6. test_fault_injection       — FaultValve=1, FaultType='closing_delay',
     FaultSeverity=0.7 (→70 ms), FaultDurationCycles=20, ForceFault=TRUE:
     distribuzione Valve01.TailTime_ms spostata (criterio provvisorio:
     shift medio ≥ 30 ms, criterio definitivo dalla calibration W7);
     countdown auto-rimozione; rientro con ForceFault=FALSE;
     FaultType='TAIL_INSTABILITY' → BadInvalidArgument.
  7. test_invalid_nodeid        — read NodeId inesistente → BadNodeIdUnknown,
     server stabile.
  8. test_reconnect             — disconnect + reconnect (nuovo client):
     valori coerenti, subscription riattivabile.

Il test 9 (regressione bulk 1-day) è in tests/test_opcua_regression.py.
"""
from __future__ import annotations

import asyncio
import socket
import statistics
import sys
import threading
import time
from pathlib import Path

import pytest
from asyncua import Client, ua

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plcsim.opcua_server import (  # noqa: E402
    MACHINE_TAGS, OpcuaServer, SIM_TAGS, VALVE_TAGS,
)
from plcsim.realtime import RealtimeSim  # noqa: E402
from plcsim.scenario import Scenario  # noqa: E402

pytestmark = pytest.mark.opcua

SEED = 42
ENDPOINT_TPL = "opc.tcp://localhost:{port}/filler01/"
# codici attesi (verificati in esecuzione, work/m6_w3_server/REPORT.md)
BAD_USER_ACCESS_DENIED = int(ua.StatusCodes.BadUserAccessDenied)   # 2149515264
BAD_NODE_ID_UNKNOWN = int(ua.StatusCodes.BadNodeIdUnknown)         # 2150891520
BAD_INVALID_ARGUMENT = int(ua.StatusCodes.BadInvalidArgument)      # 2158690304

# datatype NodeId (ns=0) → nome OPC UA (per il browse dei tipi)
_DT = {1: "Boolean", 6: "Int32", 8: "Int64", 11: "Double", 12: "String"}


def _dt_name(nid) -> str:
    """Nome del tipo OPC UA dal NodeId del datatype."""
    if nid.NamespaceIndex == 0 and isinstance(nid.Identifier, int):
        return _DT.get(nid.Identifier, f"ns=0;id={nid.Identifier}")
    return str(nid)


def _free_port() -> int:
    """Porta libera: bind su 0 → porta effettiva → close.

    Race documentata: tra il close e il bind del server un altro processo
    potrebbe prendere la porta (avvio fallirebbe con errore chiaro).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_sim() -> RealtimeSim:
    """RealtimeSim stepped, seed 42, scenario vuoto (engine per il test 6)."""
    return RealtimeSim(
        seed=SEED, mode="stepped",
        scenario=Scenario(scenario_id=99, name="m6-tests", seed=SEED,
                          faults=[]))


class _ClientLoop:
    """Thread con event loop asyncio dedicato per il client asyncua.

    Pattern del task W6: `submit(coro)` = run_coroutine_threadsafe con
    timeout; il thread gira con run_forever() finché close().
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="test-opcua-client")
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro, timeout: float = 30.0):
        """Esegue una coroutine nel loop del client (bloccante sul chiamante)."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop) \
            .result(timeout=timeout)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10.0)
        self._loop.close()


class _SubHandler:
    """Handler subscription: registra le datachange (thread-safe).

    Identifica il nodo da node.nodeid.Identifier (NodeId stringa
    'Filler01.Gruppo.Tag'); fallback a str(node).
    """

    def __init__(self):
        self.events = []
        self._lock = threading.Lock()

    def datachange_notification(self, node, val, data) -> None:
        try:
            ident = node.nodeid.Identifier
        except Exception:
            ident = str(node)
        with self._lock:
            self.events.append((str(ident), val))

    def values_for(self, suffix: str) -> list:
        """Valori delle notifiche dei nodi il cui path finisce con `suffix`."""
        with self._lock:
            return [v for k, v in self.events if k.endswith(suffix)]


class _Env:
    """Sim + server + client collegati (fixture con teardown pulito).

    L'ordine di costruzione è fisso: sim a riposo (Idle) → server (i tag
    READ partono dallo stato CORRENTE dello snapshot) → client connesso.
    """

    def __init__(self):
        self.sim = _make_sim()
        self.port = _free_port()
        self.server = OpcuaServer(self.sim, port=self.port)
        self.server.start()
        self.ns = self.server.namespace_index
        self.client_loop = _ClientLoop()
        self.client = Client(ENDPOINT_TPL.format(port=self.port))
        self.client_loop.submit(self.client.connect())

    def node(self, path: str):
        return self.client.get_node(ua.NodeId(f"Filler01.{path}", self.ns))

    def read_tag(self, path: str, timeout: float = 30.0):
        return self.client_loop.submit(self.node(path).read_value(),
                                       timeout=timeout)

    def write_tag(self, path: str, value, varianttype) -> None:
        self.client_loop.submit(
            self.node(path).write_value(value, varianttype=varianttype))

    def close(self) -> None:
        try:
            self.client_loop.submit(self.client.disconnect(), timeout=10.0)
        except Exception:
            pass  # client già disconnesso (test 8)
        self.server.stop()
        self.client_loop.close()


def _browse_names(env: _Env, node) -> dict:
    """Dizionario {browse_name: figlio} dei figli di un nodo (via client)."""
    out = {}
    for c in env.client_loop.submit(node.get_children()):
        name = env.client_loop.submit(c.read_browse_name()).Name
        out[name] = c
    return out


def _wait_value(env: _Env, path: str, expected, timeout: float = 3.0):
    """Poll del tag via client fino a `expected` (push sim→server ~10 ms)."""
    t0 = time.monotonic()
    last = None
    while time.monotonic() - t0 < timeout:
        last = env.read_tag(path)
        if last == expected:
            return last
        time.sleep(0.02)
    return last


def _ensure_running(sim: RealtimeSim, max_scans: int = 2000) -> None:
    """Porta la macchina a Running (comando start se ferma), qualunque sia
    lo stato corrente (i test condividono il sim del modulo)."""
    if sim.machine.status == "Running":
        return
    if sim.machine.status in ("Idle", "Stopped"):
        sim.submit_command({"name": "start"})
    n = 0
    while sim.machine.status != "Running" and n < max_scans:
        sim.advance(10)
        n += 10
    assert sim.machine.status == "Running", \
        f"macchina non in Running dopo {n} scan (status={sim.machine.status})"


def _collect_tt(sim: RealtimeSim, n_cycles: int,
                timeout_scans: int = 300_000) -> tuple:
    """Campiona Valve01.TailTime_ms per n cicli chiusi della valvola esposta.

    Rileva i confini di ciclo dal CycleCounter dello snapshot (stesso
    thread del test: nessuna race col thread del server); il valore
    campionato è il TT dell'ULTIMO ciclo chiuso (lo snapshot aggiorna
    Valve01.* al confine di ciclo). Ritorna (campioni, scan usati).
    """
    samples = []
    last = sim.snapshot.read()["Machine"]["CycleCounter"]
    scans = 0
    while len(samples) < n_cycles and scans < timeout_scans:
        sim.advance(100)
        scans += 100
        snap = sim.snapshot.read()
        cc = snap["Machine"]["CycleCounter"]
        if cc > last:
            samples.append(snap["Valve01"]["TailTime_ms"])
            last = cc
    return samples, scans


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def env():
    """Ambiente condiviso dai test 1, 3-8 (stato macchina gestito da
    _ensure_running). Teardown: disconnect + server.stop() + close loop."""
    e = _Env()
    yield e
    e.close()


@pytest.fixture(scope="function")
def fresh_env():
    """Ambiente FRESCO per il test 2 (read statici all'avvio: State=4)."""
    e = _Env()
    yield e
    e.close()


# ---------------------------------------------------------------------------
# Test 1 — connect+browse
# ---------------------------------------------------------------------------
def test_connect_browse(env):
    """Test 1 spec: sessione attiva, namespace Filler01 con i 3 oggetti e
    i 31 tag del contratto coi tipi OPC UA (Machine 7 + Valve01 16 +
    SimulationControl 8), ns=2.

    M9 (issue M9-01): Valve01 passa da 13 a 16 tag (aggiunti CloseReason,
    PositionLimit, FillingOvertime — feature ML mancanti nel contratto M6)."""
    # namespace array: asyncua riserva ns=1 per sé; il contratto è ns=2
    assert env.server.namespace_index == 2, env.server.namespace_index
    ns_array = env.client_loop.submit(env.client.get_namespace_array())
    assert len(ns_array) >= 3, ns_array
    assert ns_array[2] == "urn:plcsim:filler01", ns_array

    filler = env.client.get_node(ua.NodeId("Filler01", env.ns))
    children = _browse_names(env, filler)
    assert {"Machine", "Valve01", "SimulationControl"} <= set(children), \
        sorted(children)

    schema = {"Machine": MACHINE_TAGS, "Valve01": VALVE_TAGS,
              "SimulationControl": SIM_TAGS}
    total = 0
    for obj, tags in schema.items():
        sub = _browse_names(env, children[obj])
        for tag, vtype in tags:
            total += 1
            node = sub.get(tag)
            assert node is not None, f"{obj}.{tag} mancante dal browse"
            dt = env.client_loop.submit(node.read_data_type())
            assert _dt_name(dt) == vtype.name, \
                f"{obj}.{tag}: tipo {_dt_name(dt)} atteso {vtype.name}"
    assert total == 31, total


# ---------------------------------------------------------------------------
# Test 2 — read statico (ambiente fresco: State=4 all'avvio)
# ---------------------------------------------------------------------------
def test_read_static(fresh_env):
    """Test 2 spec: read statici all'avvio (SpeedTarget=15500.0, State=4
    Idle, contatori 0) + Valve01.Target=2500 dopo il primo ciclo chiuso."""
    assert fresh_env.read_tag("Machine.SpeedTarget") == 15500.0
    assert fresh_env.read_tag("Machine.State") == 4       # Idle all'avvio
    assert fresh_env.read_tag("Machine.Running") is False
    assert fresh_env.read_tag("Machine.BottleCounter") == 0
    assert fresh_env.read_tag("Machine.CycleCounter") == 0
    assert fresh_env.read_tag("Machine.DataReady") is False
    # Target è del CycleRecord: 0 finché nessun ciclo chiuso per Valve01
    assert fresh_env.read_tag("Valve01.Target") == 0
    # avanza abbastanza scan perché si chiuda almeno un ciclo della valvola
    # esposta (ciclo ~3.2 s = 320 scan; 2 iterazioni → Running, poi ~4 cicli)
    fresh_env.sim.advance(2)
    fresh_env.sim.advance(1400)
    assert _wait_value(fresh_env, "Valve01.Target", 2500, timeout=3.0) == 2500
    assert fresh_env.read_tag("Machine.CycleCounter") >= 1
    assert _wait_value(fresh_env, "Machine.State", 1, timeout=3.0) == 1


# ---------------------------------------------------------------------------
# Test 3 — subscription
# ---------------------------------------------------------------------------
def test_subscription(env):
    """Test 3 spec: subscription su CycleCounter/DataReady; dopo N scan
    comandati CycleCounter è avanzato (assert robusto). DataReady è
    registrata come osservazione (spec §5: il pulse dura 1 scan e con un
    client che campiona più lentamente può coalescere — niente assert
    rigido). Avanzamento a blocchi lenti per rendere osservabile il pulse."""
    _ensure_running(env.sim)
    handler = _SubHandler()
    sub = env.client_loop.submit(env.client.create_subscription(50, handler))
    h_cc = env.client_loop.submit(
        sub.subscribe_data_change(env.node("Machine.CycleCounter")))
    h_dr = env.client_loop.submit(
        sub.subscribe_data_change(env.node("Machine.DataReady")))
    cc0 = env.read_tag("Machine.CycleCounter")
    # 3000 scan in blocchi da 50 con pausa: ~9-10 cicli della valvola esposta
    for _ in range(60):
        env.sim.advance(50)
        time.sleep(0.01)
    time.sleep(0.8)   # lascia pubblicare e consegnare le notifiche
    cc1 = env.read_tag("Machine.CycleCounter")
    assert cc1 > cc0, f"CycleCounter non avanzato: {cc0} -> {cc1}"
    cc_ev = handler.values_for("CycleCounter")
    assert any(isinstance(v, int) and v > cc0 for v in cc_ev), \
        f"nessuna notifica CycleCounter > {cc0}: {cc_ev}"
    # osservazione DataReady (best-effort, spec §5)
    dr_true = sum(1 for v in handler.values_for("DataReady") if v is True)
    dr_false = sum(1 for v in handler.values_for("DataReady") if v is False)
    # registrata per il report: con avanzamento lento il pulse è osservabile
    print(f"[test_subscription] DataReady osservato: TRUE x{dr_true}, "
          f"FALSE x{dr_false} (pulse 1 scan; spec §5 accetta la coalescenza)")
    for h in (h_cc, h_dr):
        env.client_loop.submit(sub.unsubscribe(h))
    env.client_loop.submit(sub.delete())


# ---------------------------------------------------------------------------
# Test 4 — CmdStop causa-effetto
# ---------------------------------------------------------------------------
def test_cmd_stop_cause_effect(env):
    """Test 4 spec: CmdStop=TRUE → advance → State=Stopping (2) → advance
    (50) → State=Stopped (3), Running=FALSE; CmdStop auto-reset a FALSE."""
    _ensure_running(env.sim)
    assert _wait_value(env, "Machine.State", 1, timeout=3.0) == 1
    env.write_tag("SimulationControl.CmdStop", True, ua.VariantType.Boolean)
    assert env.read_tag("SimulationControl.CmdStop") is True  # fronte visibile
    env.sim.advance(1)
    assert env.sim.machine.status == "Stopping"
    assert _wait_value(env, "Machine.State", 2, timeout=3.0) == 2
    env.sim.advance(50)
    assert env.sim.machine.status == "Stopped"
    assert _wait_value(env, "Machine.State", 3, timeout=3.0) == 3
    assert env.read_tag("Machine.Running") is False
    # auto-reset: publish task riporta CmdStop a FALSE dopo il consumo
    assert _wait_value(env, "SimulationControl.CmdStop", False,
                       timeout=3.0) is False


# ---------------------------------------------------------------------------
# Test 5 — permesso negato
# ---------------------------------------------------------------------------
def test_permesso_negato(env):
    """Test 5 spec: write su tag READ (Valve01.FillingTime_ms) →
    BadUserAccessDenied, valore invariato."""
    before = env.read_tag("Valve01.FillingTime_ms")
    try:
        env.write_tag("Valve01.FillingTime_ms", 123, ua.VariantType.Int32)
        pytest.fail("write su tag READ accettata (atteso BadUserAccessDenied)")
    except ua.UaStatusCodeError as exc:
        assert exc.code == BAD_USER_ACCESS_DENIED, \
            f"codice {exc.code} atteso BadUserAccessDenied"
    assert env.read_tag("Valve01.FillingTime_ms") == before


# ---------------------------------------------------------------------------
# Test 6 — fault injection runtime (criterio statistico provvisorio)
# ---------------------------------------------------------------------------
def test_fault_injection(env):
    """Test 6 spec: FaultValve=1, FaultType='closing_delay',
    FaultSeverity=0.7 (→70 ms), FaultDurationCycles=20, ForceFault=TRUE →
    la distribuzione di Valve01.TailTime_ms cambia in modo statisticamente
    rilevabile; ForceFault=FALSE → rientro; FaultType non engine
    ('TAIL_INSTABILITY') → write rifiutata (BadInvalidArgument).

    CRITERIO PROVVISORIO (da congelare in calibration W7): shift medio
    TailTime_ms su finestra post-injection vs 40 cicli pre-injection ≥ 30
    ms; rientro entro ±25 ms dalla baseline pre.
    """
    _ensure_running(env.sim)
    # --- fase A: baseline (40 cicli pre-injection) ---
    pre, _ = _collect_tt(env.sim, 40)
    mean_pre = statistics.mean(pre)

    # --- iniezione via OPC UA (parametri del contratto §4.3) ---
    env.write_tag("SimulationControl.FaultValve", 1, ua.VariantType.Int32)
    env.write_tag("SimulationControl.FaultType", "closing_delay",
                  ua.VariantType.String)
    env.write_tag("SimulationControl.FaultSeverity", 0.7, ua.VariantType.Double)
    env.write_tag("SimulationControl.FaultDurationCycles", 20,
                  ua.VariantType.Int32)
    env.write_tag("SimulationControl.ForceFault", True, ua.VariantType.Boolean)
    rt = env.sim.engine.runtime_faults()
    assert 0 in rt and rt[0].fault_type == "closing_delay" \
        and rt[0].severity == 70.0 and rt[0].remaining_cycles == 20, rt
    assert env.sim.plant.mech[0].close_delay_ms == 70.0

    # --- fase B: finestra fault-attiva (20 cicli, countdown duration) ---
    post, _ = _collect_tt(env.sim, 20)
    mean_post = statistics.mean(post)
    shift = mean_post - mean_pre
    assert shift >= 30.0, \
        f"shift medio {shift:.1f} ms < 30 ms (criterio provvisorio; " \
        f"pre {mean_pre:.1f} -> post {mean_post:.1f})"

    # --- countdown: auto-rimozione dopo 20 cicli della valvola target ---
    assert not env.sim.engine.runtime_faults(), env.sim.engine.runtime_faults()
    assert env.sim.plant.mech[0].close_delay_ms == 0.0

    # --- fase C: re-inject a livello (duration=0) + 40 cicli post ---
    # ForceFault è ancora TRUE (level): la write del parametro riapplica il
    # set (rimozione del vecchio + inject del nuovo).
    env.write_tag("SimulationControl.FaultDurationCycles", 0,
                  ua.VariantType.Int32)
    post40, _ = _collect_tt(env.sim, 40)
    mean_post40 = statistics.mean(post40)
    shift40 = mean_post40 - mean_pre
    assert shift40 >= 30.0, \
        f"shift medio 40 cicli {shift40:.1f} ms < 30 ms (criterio provvisorio)"
    # cross-check E2E: il tag pubblicato dal server riflette il TT degradato
    tt_snap = env.sim.snapshot.read()["Valve01"]["TailTime_ms"]
    assert _wait_value(env, "Valve01.TailTime_ms", tt_snap,
                       timeout=3.0) == tt_snap

    # --- fase D: rientro (ForceFault=FALSE → remove) ---
    env.write_tag("SimulationControl.ForceFault", False,
                  ua.VariantType.Boolean)
    assert not env.sim.engine.runtime_faults()
    assert env.sim.plant.mech[0].close_delay_ms == 0.0
    rec, _ = _collect_tt(env.sim, 40)
    mean_rec = statistics.mean(rec)
    assert abs(mean_rec - mean_pre) <= 25.0, \
        f"rientro {mean_rec:.1f} ms vs baseline {mean_pre:.1f} ms fuori " \
        f"banda ±25 ms (criterio provvisorio)"
    assert mean_post40 - mean_rec >= 30.0
    # cross-check E2E: il tag pubblicato è di nuovo in banda sana
    tt_snap = env.sim.snapshot.read()["Valve01"]["TailTime_ms"]
    assert _wait_value(env, "Valve01.TailTime_ms", tt_snap,
                       timeout=3.0) == tt_snap

    # --- FaultType non engine → write rifiutata (BadInvalidArgument) ---
    try:
        env.write_tag("SimulationControl.FaultType", "TAIL_INSTABILITY",
                      ua.VariantType.String)
        pytest.fail("write FaultType non valido accettata (atteso "
                    "BadInvalidArgument)")
    except ua.UaStatusCodeError as exc:
        assert exc.code == BAD_INVALID_ARGUMENT, \
            f"codice {exc.code} atteso BadInvalidArgument"
    assert env.read_tag("SimulationControl.FaultType") == "closing_delay"


# ---------------------------------------------------------------------------
# Test 7 — invalid NodeId
# ---------------------------------------------------------------------------
def test_invalid_nodeid(env):
    """Test 7 spec: read su NodeId inesistente → BadNodeIdUnknown, server
    stabile (le read successive funzionano)."""
    bad = env.client.get_node(ua.NodeId("Filler01.NonEsiste", env.ns))
    try:
        env.client_loop.submit(bad.read_value())
        pytest.fail("read su NodeId inesistente senza errore")
    except ua.UaStatusCodeError as exc:
        assert exc.code == BAD_NODE_ID_UNKNOWN, \
            f"codice {exc.code} atteso BadNodeIdUnknown"
    assert env.read_tag("Machine.Running") is not None
    assert env.read_tag("Machine.SpeedTarget") == 15500.0


# ---------------------------------------------------------------------------
# Test 8 — reconnect
# ---------------------------------------------------------------------------
def test_reconnect(env):
    """Test 8 spec: client disconnette e riconnette (nuovo Client) →
    valori coerenti (SpeedTarget 15500, contatori monotoni) e subscription
    riattivabile con un nuovo handler."""
    cc_before = env.read_tag("Machine.CycleCounter")
    env.client_loop.submit(env.client.disconnect())
    c2 = Client(ENDPOINT_TPL.format(port=env.port))
    env.client_loop.submit(c2.connect())
    node2 = lambda p: c2.get_node(ua.NodeId(f"Filler01.{p}", env.ns))  # noqa: E731
    try:
        st = env.client_loop.submit(node2("Machine.SpeedTarget").read_value())
        assert st == 15500.0, st
        _ensure_running(env.sim)
        handler = _SubHandler()
        sub = env.client_loop.submit(c2.create_subscription(50, handler))
        h_cc = env.client_loop.submit(
            sub.subscribe_data_change(node2("Machine.CycleCounter")))
        cc0 = env.client_loop.submit(node2("Machine.CycleCounter").read_value())
        env.sim.advance(2000)
        time.sleep(0.8)
        cc1 = env.client_loop.submit(node2("Machine.CycleCounter").read_value())
        assert cc1 >= cc0, f"contatore non monotono: {cc0} -> {cc1}"
        cc_ev = handler.values_for("CycleCounter")
        assert any(isinstance(v, int) and v > cc0 for v in cc_ev), cc_ev
        assert cc1 >= cc_before, f"coerenza col valore pre-reconnect: " \
            f"{cc_before} -> {cc1}"
        env.client_loop.submit(sub.unsubscribe(h_cc))
        env.client_loop.submit(sub.delete())
    finally:
        env.client_loop.submit(c2.disconnect())
