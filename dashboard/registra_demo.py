"""Registra una fotografia delle risposte dell'API, per la modalita' demo.

La dashboard legge SOLO le route GET di `pipeline/api.py`: e' un vincolo di
progetto. Questo script non lo aggira: registra cio' che quelle route
rispondono e `server_demo.py` lo ripropone alla pagina senza toccarlo.

Registra **attraverso `server_api.py`**, non dall'API cruda, e il motivo non e'
una comodita'. Il proxy fa due traduzioni che la pagina da' per scontate:
sposta l'istante di osservazione alla fine della run nel database (senza,
le finestre OEE cadono fuori dai dati e la pagina esce vuota e degradata) e
chiede 400 cicli per valvola invece dei 200 di default (le legende dicono
«gli ultimi 400 riempimenti»: con 200 mentirebbero). Registrare dall'API
cruda produce una fotografia diversa da cio' che si vede dal vivo.

I numeri sono veri, prodotti dal simulatore e passati per tutta la catena.
Sono fermi al momento della registrazione, e il selettore in cima alla pagina
lo dichiara.

    # la catena viva, nell'ordine
    python -m uvicorn pipeline.api:app --port 8123
    python dashboard/server_api.py --port 8079
    python dashboard/registra_demo.py --da http://127.0.0.1:8079

Esce 0 se ha registrato tutto, 1 se una route obbligatoria non ha risposto.
"""
from __future__ import annotations

import argparse
import gzip
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


SCENARIO = "registrato"


def chiedi(base: str, route: str, timeout: float) -> bytes | None:
    # Il proxy espone le route sotto /api/<scenario>/, come le chiede la pagina.
    url = f"{base.rstrip('/')}/api/{SCENARIO}/{route}"
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
    """Non scrive piu' nulla: salva il proxy, con `--registra`.

    Restituisce solo la dimensione, per il conteggio finale. Il nome del file
    lo decide `server_api.chiave_file`, cosi' che chi registra e chi ripropone
    usino la stessa regola invece di due elenchi da tenere allineati a mano.
    """
    return len(dati)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--da", default="http://127.0.0.1:8079",
                    help="indirizzo di server_api.py (NON dell'API cruda)")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)

    DESTINAZIONE.mkdir(parents=True, exist_ok=True)
    print(f"registro da {args.da} in {DESTINAZIONE}")

    totale = 0
    mancanti: list[str] = []

    for route, nome in ROUTE.items():
        dati = chiedi(args.da, route, args.timeout)
        if dati is None:
            mancanti.append(route)
            continue
        totale += scrivi(nome, dati)

    # Dettaglio per valvola: la pagina VALVOLE ne apre una alla volta.
    for vid in VALVOLE:
        for suffisso, nome in (("", f"valve-{vid}.json"),
                               ("/kpi", f"valve-{vid}-kpi.json"),
                               ("/score", f"valve-{vid}-score.json")):
            dati = chiedi(args.da, f"valves/{vid}{suffisso}", args.timeout)
            if dati is not None:
                totale += scrivi(nome, dati)

    # L'istante della fotografia, che la pagina mostra invece di fingere che
    # i dati siano di adesso. Questo file lo scrive il registratore, non il
    # proxy: e' l'unico pezzo di stato che non e' una risposta dell'API.
    (DESTINAZIONE / "registrazione.json").write_text(json.dumps({
        "registrato_il": datetime.now(timezone.utc).isoformat(),
        "sorgente": args.da,
        "route": len(ROUTE) + len(VALVOLE) * 3,
        "nota": "Fotografia delle risposte GET di pipeline/api.py. "
                "I dati sono veri e fermi a questo istante.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Compressione. Le risposte sono serie numeriche: si comprimono al 3%,
    # e la pagina CARTA da sola ne chiede 35 da due megabyte l'una. Senza
    # questo passaggio la demo peserebbe 88 MB dentro il repository.
    # Il browser decomprime da se': `server_demo.py` manda l'intestazione.
    risparmiato = 0
    for f in sorted(DESTINAZIONE.glob("*.json")):
        if f.name == "registrazione.json":
            continue          # lo legge il server come testo, ed e' minuscolo
        grezzo = f.read_bytes()
        (f.with_suffix(".json.gz")).write_bytes(gzip.compress(grezzo, 9))
        risparmiato += len(grezzo)
        f.unlink()
    dopo = sum(x.stat().st_size for x in DESTINAZIONE.iterdir())
    if risparmiato:
        print(f"compresse: {risparmiato/1024/1024:.1f} MB -> "
              f"{dopo/1024/1024:.1f} MB")

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
