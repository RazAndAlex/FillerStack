"""Registra una fotografia delle risposte dell'API, per la modalita' demo.

La dashboard legge SOLO le route GET di `pipeline/api.py` (CLAUDE.md). Questo
script non aggira quella regola: chiama esattamente quelle route e salva su
disco cio' che rispondono, byte per byte. `server.py` poi le ripropone alla
pagina senza toccarle.

Serve a una cosa sola: far vedere la dashboard a chi scarica il progetto e non
ha ne' Docker ne' PostgreSQL. I numeri sono veri, prodotti dal simulatore e
passati per tutta la catena; sono fermi al momento della registrazione, e la
pagina lo dichiara.

    # con l'API vera in ascolto
    python -m uvicorn pipeline.api:app --port 8123
    python dashboard/registra_demo.py --api http://127.0.0.1:8123

Esce 0 se ha registrato tutto, 1 se una route obbligatoria non ha risposto.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

QUI = Path(__file__).resolve().parent
DESTINAZIONE = QUI / "demo" / "registrato"

# route -> nome del file, nella forma che `server.py` si aspetta.
ROUTE = {
    "health": "health.json",
    "manifest": "manifest.json",
    "machine/state": "machine-state.json",
    "machine/oee?window=shift": "machine-oee-shift.json",
    "machine/oee?window=day": "machine-oee-day.json",
    "machine/oee/series": "machine-oee-series.json",
    "valves": "valves.json",
    "valves/baseline": "baseline.json",
    "alerts": "alerts.json",
    "alerts/history": "alert-history.json",
    "alerts/transitions": "alert-transitions.json",
    "alerts/pareto": "alert-pareto.json",
}

# Route che possono legittimamente mancare: la loro assenza si registra come
# fatto, non come errore. Nessuna di queste ferma la registrazione.
FACOLTATIVE = {"alerts/transitions", "alerts/pareto", "manifest",
               "valves/baseline"}

VALVOLE = range(1, 36)


def chiedi(base: str, route: str, timeout: float) -> bytes | None:
    url = f"{base.rstrip('/')}/{route}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        print(f"  {route}: HTTP {e.code}")
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  {route}: irraggiungibile ({e})")
        return None


def scrivi(nome: str, dati: bytes) -> int:
    p = DESTINAZIONE / nome
    p.write_bytes(dati)
    return len(dati)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default="http://127.0.0.1:8123",
                    help="indirizzo dell'API vera")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)

    DESTINAZIONE.mkdir(parents=True, exist_ok=True)
    print(f"registro da {args.api} in {DESTINAZIONE}")

    totale = 0
    mancanti: list[str] = []

    for route, nome in ROUTE.items():
        dati = chiedi(args.api, route, args.timeout)
        if dati is None:
            mancanti.append(route)
            continue
        totale += scrivi(nome, dati)

    # Dettaglio per valvola: la pagina VALVOLE ne apre una alla volta.
    for vid in VALVOLE:
        for suffisso, nome in (("", f"valve-{vid}.json"),
                               ("/kpi", f"valve-{vid}-kpi.json"),
                               ("/score", f"valve-{vid}-score.json")):
            dati = chiedi(args.api, f"valves/{vid}{suffisso}", args.timeout)
            if dati is not None:
                totale += scrivi(nome, dati)

    # L'istante della fotografia, che la pagina mostra invece di fingere che
    # i dati siano di adesso.
    (DESTINAZIONE / "registrazione.json").write_text(json.dumps({
        "registrato_il": datetime.now(timezone.utc).isoformat(),
        "sorgente": args.api,
        "route": len(ROUTE) + len(VALVOLE) * 3,
        "nota": "Fotografia delle risposte GET di pipeline/api.py. "
                "I dati sono veri e fermi a questo istante.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    bloccanti = [r for r in mancanti if r not in FACOLTATIVE]
    print(f"\nscritti {totale/1024:.0f} KB in {DESTINAZIONE}")
    if mancanti:
        print(f"non risposte: {', '.join(mancanti)}")
    if bloccanti:
        print(f"MANCANO route obbligatorie: {', '.join(bloccanti)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
