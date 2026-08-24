"""Modalita' demo: la dashboard sui dati registrati, senza Docker.

Non e' l'API vera: e' un guscio che ripropone la fotografia salvata da
`registra_demo.py` in `dashboard/demo/registrato/`, con la stessa forma di
risposta delle route di `pipeline/api.py`. Nessun calcolo e nessun dato nuovo:
legge JSON dal disco e lo restituisce tale e quale.

I numeri sono veri — prodotti dal simulatore e passati per tutta la catena — e
fermi all'istante della registrazione. Il selettore in cima alla pagina lo
dichiara, invece di far credere che siano di adesso.

Serve a chi scarica il progetto e vuole vedere la dashboard senza installare
PostgreSQL. Per i dati vivi c'e' `server_api.py`.

    python dashboard/server.py --port 8078
    -> http://127.0.0.1:8078/
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from functools import lru_cache
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

QUI = Path(__file__).resolve().parent
FIXTURES = QUI / "demo"
SCENARIO = "registrato"


def _titolo() -> str:
    """Il selettore porta la data della fotografia, non un'etichetta muta."""
    p = FIXTURES / SCENARIO / "registrazione.json"
    try:
        quando = json.loads(p.read_text(encoding="utf-8"))["registrato_il"]
        g = datetime.fromisoformat(quando).strftime("%d/%m/%Y %H:%M")
        return f"Dati registrati il {g} UTC"
    except (OSError, ValueError, KeyError):
        return "Dati registrati"


SCENARI = [(SCENARIO, _titolo())]
SLUG = {s for s, _ in SCENARI}

# route -> file nella cartella dello scenario
ROUTE_FILE = {
    "health": "health.json",
    "machine/state": "machine-state.json",
    "machine/oee": None,          # ?window=shift|day
    "machine/oee/series": "machine-oee-series.json",
    "valves": "valves.json",
    "alerts": "alerts.json",
    "alerts/history": "alert-history.json",
    "alerts/transitions": "alert-transitions.json",
    "alerts/pareto": "alert-pareto.json",
    "manifest": "manifest.json",
}


@lru_cache(maxsize=4)
def leggi_comune(nome: str) -> bytes:
    """File non legati a una singola route (la baseline sana)."""
    return (FIXTURES / SCENARIO / nome).read_bytes()


@lru_cache(maxsize=256)
def leggi(scenario: str, nome: str) -> bytes:
    p = FIXTURES / scenario / nome
    if not p.exists():
        raise FileNotFoundError(f"{scenario}/{nome}")
    return p.read_bytes()


class Handler(SimpleHTTPRequestHandler):
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
            return self._api(parti[1:], u.query)
        if u.path == "/scenari":
            return self._json(json.dumps(
                [{"slug": s, "titolo": t} for s, t in SCENARI]).encode())
        return super().do_GET()

    def _api(self, parti: list[str], query: str) -> None:
        if not parti or parti[0] not in SLUG:
            return self._errore(
                f"scenario sconosciuto; usa uno di {sorted(SLUG)}")
        scenario, route = parti[0], "/".join(parti[1:])

        # /api/<scn>/valves/<id>  e  /api/<scn>/valves/<id>/score|kpi
        if route.startswith("valves/") and route != "valves/baseline":
            resto = route[len("valves/"):].split("/")
            if resto[0].isdigit():
                vid = resto[0]
                suff = resto[1] if len(resto) > 1 else None
                nome = {None: f"valve-{vid}.json",
                        "score": f"valve-{vid}-score.json",
                        "kpi": f"valve-{vid}-kpi.json"}.get(suff)
                if nome is None:
                    return self._errore(f"sotto-route ignota: {suff}")
                try:
                    return self._json(leggi(scenario, nome))
                except FileNotFoundError:
                    # dettaglio non generato per questa valvola: e' un fatto,
                    # non un errore. Lo dichiara, non lo inventa.
                    return self._json(json.dumps({
                        "valve_id": int(vid),
                        "__status": "dettaglio non disponibile per questa "
                                    "valvola in questo scenario"}).encode())

        if route == "valves/baseline":
            return self._json(leggi_comune("baseline.json"))

        if route == "machine/oee":
            finestra = "shift"
            for kv in query.split("&"):
                if kv.startswith("window="):
                    finestra = kv.split("=", 1)[1]
            if finestra not in ("shift", "day"):
                return self._errore("window deve essere shift o day", 400)
            return self._json(leggi(scenario, f"machine-oee-{finestra}.json"))

        nome = ROUTE_FILE.get(route)
        if nome is None:
            return self._errore(
                f"route ignota: {route}; disponibili {sorted(ROUTE_FILE)}")
        try:
            return self._json(leggi(scenario, nome))
        except FileNotFoundError as e:
            return self._errore(str(e))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="dashboard, modalita' demo")
    ap.add_argument("--port", type=int, default=8078)
    args = ap.parse_args()

    cartella = FIXTURES / SCENARIO
    if not cartella.is_dir():
        raise SystemExit(
            f"manca la registrazione in {cartella}.\n"
            "Rigenerala con l'API viva:  python dashboard/registra_demo.py")

    # A un thread solo il server si impianta: una pagina carica decine di
    # file e il browser tiene aperte piu' connessioni insieme.
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"dashboard (demo) su http://127.0.0.1:{args.port}/   "
          "Ctrl-C per fermare")
    print(f"  {_titolo()} — dati fermi, nessun database richiesto")
    srv.serve_forever()
