"""Proxy: le tre pagine v7 servite sui DATI VERI di `pipeline/api.py`.

Gemello di `server.py` (che legge le fixture congelate). Serve gli STESSI file
statici dalla stessa cartella, ma invece di leggere JSON dal disco inoltra ogni
chiamata all'API vera. Le pagine non cambiano di una riga: la traduzione fra la
forma che il guscio produce (`/api/<scenario>/<route>`) e la forma vera
(`/<route>`) avviene qui.

Due server intercambiabili sulla stessa dashboard, quindi confrontare fixture e
reale costa solo cambiare porta.

    # l'API vera, prima
    python -m uvicorn pipeline.api:app --port 8123

    # istante = fine della run nel database (default)
    python .scratch/dashboard-v7/server_api.py --port 8078

    # istante = adesso vero: pagine degradate, percorso "dato vecchio"
    python .scratch/dashboard-v7/server_api.py --port 8079 --at now

Regole rispettate (CLAUDE.md, HANDOFF-api-vera.md):
- non riempie buchi e non ripiega MAI sulle fixture: se l'API vera risponde
  vuoto o `degraded`, quello arriva alla pagina tale e quale;
- l'istante di osservazione e' DICHIARATO in un log all'avvio, mai cablato;
- nessuna riga di `a/`, `v1/`, `oee/`, `comune/` viene toccata.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

QUI = Path(__file__).resolve().parent
# La radice del progetto: serve a importare `pipeline`. Ricavata dalla
# posizione di questo file, che sta in <radice>/dashboard/.
RADICE = QUI.parent

# Il database contiene UN SOLO run (work/m4_demo_dropout_1d): `cycles` ha
# chiave (valve_id, cycle_id) senza data e ogni run rinumera da 1, quindi un
# secondo run scarterebbe in silenzio i propri cicli. Il menu' mostra una voce
# sola perche' una sola esiste: sei voci prometterebbero cinque scenari
# inesistenti.
# Il titolo nomina il run CORRENTE, letto dal database all'avvio: scriverlo a
# mano significherebbe che il menu' mente il giorno in cui il run cambia.
SCENARI = [
    ("b-guasto-singolo", "Dati reali"),
]

# Le nove route che le tre pagine chiamano davvero
# (.scratch/backend-2026-08-19/DIVARIO-ROUTE.md). `alerts/pareto` e `manifest`
# sono helper morti di versioni precedenti: nessuna pagina li usa.
ROUTE_AMMESSE = {
    "machine/state",
    "machine/oee",
    "machine/oee/series",
    "valves",
    "valves/baseline",
    # Qualita' per valvola in secchielli contigui (2026-08-20): e' l'unica
    # route che porta l'andamento della SINGOLA valvola nel tempo, e nessuna
    # pagina puo' disegnarlo senza.
    "valves/quality/series",
    # Profilo di TUTTE le valvole in una chiamata (2026-08-20). Sta qui e non
    # nella regola per `valves/<id>/...` sotto: non ha un numero in mezzo, e
    # `"profile".isdigit()` e' falso — senza questa riga verrebbe rifiutata.
    "valves/profile",
    "alerts",
    "alerts/history",
    "health",
}

# Le sole due route che accettano e servono `at`. Tutte le altre restituiscono
# «l'ultimo disponibile», che su un database statico e' gia' la fine della run.
# La pagina ricava l'eta' del dato da `oee.at` (comune/dati.js: etaDato), quindi
# iniettare `at` qui si propaga coerentemente a tutto il resto.
ROUTE_CON_AT = {"machine/oee", "machine/oee/series"}

# `/valves/{id}/kpi` ha default `limit=200` nell'API vera, ma il guscio ne
# serviva 400 e le pagine lo DICONO scritto ("gli ultimi 400 riempimenti",
# "tacca = mediana dei 400 cicli"). Senza questo, le legende mentirebbero.
# Non e' un numero inventato: e' il parametro che il disegno accettato
# assume, ripristinato nella traduzione — che e' il mestiere del proxy.
KPI_LIMIT = 400


def chiave_file(route: str, query: str) -> str:
    """route + query -> nome di file corto, stabile, valido su Windows.

    La chiave comprende la query perche' le pagine chiedono intervalli diversi
    dalla stessa route: una serie su due settimane non e' la stessa risposta
    della stessa serie su due mesi. Registrarle sotto la stessa chiave farebbe
    rispondere con un periodo diverso da quello chiesto, e in silenzio.

    La query non finisce pero' nel nome per esteso: le date ISO percent-encoded
    producevano nomi da 90 caratteri, e su Windows un `git clone` in una
    cartella gia' profonda falliva con «Filename too long». Il nome porta la
    route in chiaro, che serve a capire cosa c'e' dentro guardando la cartella,
    e la query come impronta.
    """
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", route).strip("_")[:48]
    if not query:
        return base + ".json"
    impronta = hashlib.sha256(query.encode()).hexdigest()[:10]
    return f"{base}-{impronta}.json"


def istante_fine_run() -> tuple[datetime, str | None]:
    """Ultimo `event_ts` del RUN CORRENTE. Letto dal DB, non cablato.

    Il filtro sul run non e' un dettaglio: da quando `cycles` ospita piu' run,
    un `max(event_ts)` senza filtro risponde con la fine del run piu' recente
    in assoluto, che non e' necessariamente quello che la dashboard sta
    guardando. Il run corrente e' quello dichiarato in `machine_state`.
    """
    sys.path.insert(0, str(RADICE))
    from sqlalchemy import text

    from pipeline.cycles_storage import CyclesStorage
    from pipeline.storage import make_engine

    run = CyclesStorage().resolve_run_id(None)
    eng = make_engine()
    with eng.connect() as c:
        if run is None:
            v = c.execute(text("select max(event_ts) from cycles")).scalar_one()
        else:
            v = c.execute(text("select max(event_ts) from cycles "
                               "where run_id = :r"), {"r": run}).scalar_one()
    if v is None:
        raise SystemExit("cycles e' vuota: nessun istante da cui partire")
    return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)), run


def fine_di_una_run(run: str) -> datetime:
    """Ultimo `event_ts` della run NOMINATA. Stessa regola di
    `istante_fine_run`, ma il run arriva dalla riga di comando invece che dal
    KV: serve a mettere due corse a confronto senza spostare il KV, che e' una
    decisione dell'utente e non un effetto collaterale di un confronto."""
    sys.path.insert(0, str(RADICE))
    from sqlalchemy import text

    from pipeline.storage import make_engine

    with make_engine().connect() as c:
        v = c.execute(text("select max(event_ts) from cycles "
                           "where run_id = :r"), {"r": run}).scalar_one()
    if v is None:
        raise SystemExit(f"la run {run!r} non ha cicli in `cycles`")
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def risolvi_at(spec: str) -> tuple[datetime | None, str | None]:
    if spec == "now":
        return None, None                # nessuna iniezione: «adesso» vero
    if spec == "auto":
        return istante_fine_run()
    return datetime.fromisoformat(spec.replace("Z", "+00:00")), None


class Handler(SimpleHTTPRequestHandler):
    api_base = "http://127.0.0.1:8123"
    at: datetime | None = None
    registra: Path | None = None
    run: str | None = None
    # Quando e' valorizzato, ogni chiamata inoltrata porta `run_id`: senza,
    # l'API ricadrebbe sul KV `current_run_id` e la pagina mostrerebbe la run
    # storica credendo di mostrare quella live.
    run_forzato: str | None = None

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(QUI), **kw)

    def log_message(self, *a):  # silenzio
        pass

    def _json(self, payload: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _errore(self, msg: str, code: int = 404) -> None:
        self._json(json.dumps({"errore": msg}).encode(), code)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        parti = [p for p in u.path.split("/") if p]
        if parti[:1] == ["api"]:
            return self._inoltra(parti[1:], u.query)
        if u.path == "/scenari":
            suff = f" — run {self.run}" if self.run else ""
            return self._json(json.dumps(
                [{"slug": s, "titolo": t + suff} for s, t in SCENARI]).encode())
        return super().do_GET()

    def _ammessa(self, route: str) -> bool:
        if route in ROUTE_AMMESSE:
            return True
        # /valves/<id> e /valves/<id>/kpi|score|profile
        p = route.split("/")
        return (len(p) in (2, 3) and p[0] == "valves" and p[1].isdigit()
                and (len(p) == 2 or p[2] in ("kpi", "score", "profile")))

    def _inoltra(self, parti: list[str], query: str) -> None:
        if not parti:
            return self._errore("manca lo scenario nel percorso")
        # Lo scenario e' ignorato di proposito: il database ne contiene uno.
        route = "/".join(parti[1:])
        if not self._ammessa(route):
            return self._errore(
                f"route non prevista da questa dashboard: {route}")

        q = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
             if k != "at"]
        # L'iniezione di `at` sposta l'istante di osservazione, cioe' il bordo
        # DESTRO di una finestra che cammina all'indietro. Da quando
        # `machine/oee` e `machine/oee/series` accettano anche un intervallo
        # esplicito (`from`/`to`), le due cose competono: iniettare `at` sopra
        # un periodo scelto da un calendario lo scavalcherebbe in silenzio, e
        # la pagina mostrerebbe un periodo diverso da quello chiesto senza
        # dirlo. Chi porta `from` o `to` ha gia' dichiarato il suo istante.
        esplicito = any(k in ("from", "to") for k, _ in q)
        if self.at is not None and route in ROUTE_CON_AT and not esplicito:
            q.append(("at", self.at.isoformat()))
        if (self.run_forzato
                and not any(k == "run_id" for k, _ in q)):
            q.append(("run_id", self.run_forzato))
        if route.endswith("/kpi") and not any(k == "limit" for k, _ in q):
            q.append(("limit", str(KPI_LIMIT)))
        url = f"{self.api_base}/{route}" + (f"?{urlencode(q)}" if q else "")

        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                corpo = r.read()
                # In registrazione si salva CIO' CHE LA PAGINA HA CHIESTO,
                # non un elenco di route deciso a tavolino: cosi' la demo
                # copre esattamente le viste che qualcuno ha davvero aperto.
                if self.registra is not None and r.status == 200:
                    self.registra.mkdir(parents=True, exist_ok=True)
                    (self.registra / chiave_file(route, query)).write_bytes(corpo)
                return self._json(corpo, r.status)
        except urllib.error.HTTPError as e:
            # L'errore dell'API vera arriva alla pagina tale e quale.
            return self._json(e.read(), e.code)
        except OSError as e:
            return self._json(json.dumps({
                "errore": "API vera non raggiungibile",
                "url": url, "dettaglio": str(e)}).encode(), 502)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8078)
    ap.add_argument("--api", default="http://127.0.0.1:8123")
    ap.add_argument("--at", default="auto",
                    help="auto = fine della run letta dal DB (default) | "
                         "now = adesso vero, pagine degradate | ISO8601")
    ap.add_argument("--registra", default=None,
                    help="cartella in cui salvare le risposte servite, per "
                         "costruire la modalita' demo (dashboard/demo/registrato)")
    ap.add_argument("--run", default=None,
                    help="run_id da inoltrare all'API vera, invece del KV "
                         "`current_run_id`. Con --at auto l'istante di "
                         "osservazione diventa la fine di QUESTA run.")
    a = ap.parse_args()

    if a.run and a.at == "auto":
        at, run = fine_di_una_run(a.run), a.run
    else:
        at, run = risolvi_at(a.at)
        if a.run:
            run = a.run
    Handler.run_forzato = a.run
    Handler.api_base = a.api.rstrip("/")
    Handler.at = at
    Handler.run = run
    Handler.registra = Path(a.registra) if a.registra else None

    print(f"proxy v7 -> API vera {Handler.api_base}")
    print("istante di osservazione: "
          + (f"{at.isoformat()}  (--at {a.at})" if at
             else f"«adesso» vero {datetime.now(timezone.utc).isoformat()} "
                  "— nessun `at` iniettato, pagine degradate attese"))
    print(f"run corrente: {run or '(nessun filtro: un run solo o schema legacy)'}")
    print(f"scenari serviti: {[s for s, _ in SCENARI]}")
    if Handler.registra:
        print(f"REGISTRAZIONE attiva -> {Handler.registra}")
    print(f"http://127.0.0.1:{a.port}/   (Ctrl-C per fermare)", flush=True)
    # Concorrente, non seriale: la pagina apre otto chiamate insieme e una
    # sola di esse (`machine/oee/series`) costa ~8 s. Su un server a una
    # richiesta per volta le otto si mettono in fila e la pagina resta bianca
    # per una dozzina di secondi. Non e' un dettaglio di comodita': con lo
    # storico a 60 giorni la coda diventa inaccettabile.
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
