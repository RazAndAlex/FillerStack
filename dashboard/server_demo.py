"""Modalita' demo: la dashboard sui dati registrati, senza database.

Non e' l'API vera. E' un guscio che ripropone la fotografia salvata in
`dashboard/demo/registrato/` con la stessa forma di risposta delle route di
`pipeline/api.py`. Nessun calcolo e nessun dato nuovo: legge un file e lo
restituisce tale e quale.

I numeri sono veri — prodotti dal simulatore e passati per tutta la catena — e
fermi all'istante della registrazione. Il selettore in cima alla pagina porta
quella data, invece di lasciar credere che siano di adesso.

Serve a chi scarica il progetto e vuole vedere la dashboard senza installare
Docker e PostgreSQL. Usa solo la libreria standard. Per i dati vivi c'e'
`server_api.py`.

    python dashboard/server_demo.py --port 8078
    -> http://127.0.0.1:8078/

## Cosa succede quando un dato non e' stato registrato

Risponde 404, e le pagine lo dichiarano a schermo. E' voluto. La registrazione
e' indicizzata su route **e query**, perche' una serie chiesta su due settimane
non e' la stessa risposta della stessa serie chiesta su due mesi. Servire
l'una al posto dell'altra mostrerebbe un periodo diverso da quello chiesto,
in silenzio: un buco dichiarato e' meglio di un numero sbagliato.

Le viste predefinite delle cinque pagine sono registrate. Spostando un
intervallo di date si esce dalla fotografia, e la pagina lo dice.

## Come si rigenera

Con la catena viva, e il proxy in registrazione:

    python -m uvicorn pipeline.api:app --port 8123
    python dashboard/server_api.py --port 8079 --registra dashboard/demo/registrato
    python dashboard/registra_demo.py --da http://127.0.0.1:8079

poi si aprono le cinque pagine su `http://127.0.0.1:8079/` una per una: il
proxy salva esattamente cio' che ognuna ha chiesto.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from functools import lru_cache
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

QUI = Path(__file__).resolve().parent
REGISTRAZIONE = QUI / "demo" / "registrato"
SCENARIO = "registrato"


def chiave_file(route: str, query: str) -> str:
    """route + query -> nome di file. Identica a quella di `server_api.py`.

    Le due copie devono restare uguali: e' il contratto fra chi registra e chi
    ripropone. Se una cambia, la demo smette di trovare i suoi file.
    """
    grezzo = route + (("?" + query) if query else "")
    ripulito = re.sub(r"[^A-Za-z0-9._-]+", "_", grezzo).strip("_")
    if len(ripulito) > 120:
        impronta = hashlib.sha256(grezzo.encode()).hexdigest()[:12]
        ripulito = ripulito[:100] + "-" + impronta
    return ripulito + ".json"


def titolo() -> str:
    """Il selettore porta la data della fotografia, non un'etichetta muta."""
    try:
        quando = json.loads(
            (REGISTRAZIONE / "registrazione.json").read_text(encoding="utf-8")
        )["registrato_il"]
        g = datetime.fromisoformat(quando).strftime("%d/%m/%Y %H:%M")
        return f"Dati registrati il {g} UTC"
    except (OSError, ValueError, KeyError):
        return "Dati registrati"


@lru_cache(maxsize=512)
def leggi(nome: str) -> tuple[bytes, bool]:
    """Restituisce (corpo, e_compresso).

    Sul disco le risposte stanno compresse: sono serie numeriche, scendono al
    3%, e la sola pagina CARTA ne chiede 35 da due megabyte l'una. Non si
    decomprimono qui: si passano al browser con l'intestazione giusta, che e'
    quello che farebbe qualunque server vero.
    """
    gz = REGISTRAZIONE / (nome + ".gz")
    if gz.exists():
        return gz.read_bytes(), True
    p = REGISTRAZIONE / nome
    if not p.exists():
        raise FileNotFoundError(nome)
    return p.read_bytes(), False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(QUI), **kw)

    def log_message(self, *a):  # silenzio
        pass

    def _json(self, payload: bytes, code: int = 200,
              compresso: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if compresso:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _errore(self, msg: str, code: int = 404) -> None:
        self._json(json.dumps({"errore": msg}, ensure_ascii=False).encode(),
                   code)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        parti = [p for p in u.path.split("/") if p]

        if u.path == "/scenari":
            return self._json(json.dumps(
                [{"slug": SCENARIO, "titolo": titolo()}]).encode())

        if parti[:1] == ["api"]:
            if len(parti) < 2:
                return self._errore("manca la route")
            # Lo scenario nel percorso e' ignorato: la registrazione e' una
            # sola, e un vecchio link con `?scn=` addosso deve funzionare.
            route = "/".join(parti[2:])
            if not route:
                return self._errore("manca la route")
            try:
                corpo, compresso = leggi(chiave_file(route, u.query))
                return self._json(corpo, compresso=compresso)
            except FileNotFoundError:
                return self._errore(
                    f"non registrato in questa demo: {route}"
                    + (f"?{u.query}" if u.query else "")
                    + " — i dati sono una fotografia, non un database")

        return super().do_GET()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="dashboard, modalita' demo")
    ap.add_argument("--port", type=int, default=8078)
    args = ap.parse_args()

    if not REGISTRAZIONE.is_dir():
        raise SystemExit(
            f"manca la registrazione in {REGISTRAZIONE}.\n"
            "Come si rigenera: vedi il docstring di questo file.")

    quanti = len(list(REGISTRAZIONE.glob("*.json*")))
    # A un thread solo il server si impianta: una pagina carica decine di file
    # e il browser tiene aperte piu' connessioni insieme.
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"dashboard (demo) su http://127.0.0.1:{args.port}/   "
          "Ctrl-C per fermare")
    print(f"  {titolo()} — {quanti} risposte registrate, nessun database")
    srv.serve_forever()
