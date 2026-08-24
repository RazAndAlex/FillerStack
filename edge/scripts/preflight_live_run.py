#!/usr/bin/env python3
"""Controlli da fare PRIMA di avviare un run live. Solo letture.

    python edge/scripts/preflight_live_run.py \
        --run-id live-2026-08-24 --date 2026-08-24 --client-id plcsim-ingest-20260824

Esce 0 se tutti i controlli passano, 1 se almeno uno fallisce, 2 se qualcosa
non è verificabile e quindi il preflight non può pronunciarsi.

## Perché esiste

Al 2026-08-22 la sequenza d'avvio di un run live era scritta in prosa, in due
file di memoria diversi:

    partizione raw nuova · Node-RED riavviato a server già in ascolto ·
    sessione broker pulita · --run-id esplicito e identico per backfill e
    inference

Ognuna delle quattro regole è nata da una corsa fallita, e ognuna è costata ore
per essere diagnosticata. Una regola che vive solo in un file di memoria viene
saltata: non perché qualcuno la ignori, ma perché al momento di lanciare nessuno
rilegge i file di memoria.

I fallimenti che questi controlli intercettano, nell'ordine in cui sono stati
scoperti:

- **Partizione riusata.** Cinque sessioni del simulatore appese alla stessa data
  hanno prodotto la tempesta di duplicati originale. Il backfill si ferma con
  «righe duplicate su (valve_id, cycle_id)».
- **Node-RED avviato prima del server OPC UA.** Il client OPC-UA dentro il
  container non trova l'endpoint, non ritenta in modo utile, e il run parte con
  la catena muta. Non dà errore: dà silenzio.
- **Sessione broker sporca.** La coda persistente (QoS 1, `clean_session=False`,
  client id fisso `plcsim-ingest-v1`) consegna alla riconnessione i messaggi
  della sessione precedente. Entrano nella partizione nuova con `cycle_id` che il
  run nuovo raggiungerà più tardi, e alla collisione il backfill si ferma. La coda
  serve e non va tolta: protegge l'esercizio normale mentre l'ingest è giù. Un
  run **nuovo** deve però partire senza arretrati, quindi con un client id suo.
- **`run_id` già usato.** Le tabelle hanno la chiave sul run dal 2026-08-22.
  Riusare un id mescola due corse dentro la stessa chiave.

## Cosa questo script NON fa

Non avvia niente, non scrive niente, non corregge niente. Se un controllo
fallisce, stampa il perché e cosa fare, e si ferma. La scelta di procedere lo
stesso resta di chi lancia: questo file non ha nessuna autorità per impedirlo.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

RAW = ROOT / "data" / "raw"
CONTAINER_NODERED = "plcsim-nodered"
CONTAINER_POSTGRES = "plcsim-postgres"
CONTAINER_MOSQUITTO = "plcsim-mosquitto"
PORTA_OPCUA = 4840
CLIENT_ID_CONDIVISO = "plcsim-ingest-v1"

OK, KO, BOH = "OK", "KO", "??"


class Esito:
    """Raccoglie i verdetti e decide il codice d'uscita.

    Tre stati e non due: un controllo che non si è potuto eseguire non è un
    controllo passato. Confonderli è il modo in cui un preflight diventa un
    timbro.
    """

    def __init__(self) -> None:
        self.righe: list[tuple[str, str, str]] = []

    def aggiungi(self, stato: str, titolo: str, dettaglio: str = "") -> None:
        self.righe.append((stato, titolo, dettaglio))

    @property
    def codice(self) -> int:
        if any(s == KO for s, _, _ in self.righe):
            return 1
        if any(s == BOH for s, _, _ in self.righe):
            return 2
        return 0

    def stampa(self) -> None:
        largh = max(len(t) for _, t, _ in self.righe) if self.righe else 0
        for stato, titolo, dettaglio in self.righe:
            print(f"  [{stato}]  {titolo.ljust(largh)}   {dettaglio}")
        print()
        if self.codice == 0:
            print("Preflight passato. La sequenza d'avvio è rispettata.")
        elif self.codice == 1:
            print("Preflight FALLITO. Le righe [KO] qui sopra dicono cosa correggere.")
        else:
            print("Preflight NON CONCLUSO. Le righe [??] non si sono potute verificare.")


def powershell(comando: str) -> str | None:
    """Esegue una riga di PowerShell e restituisce stdout, o None se fallisce."""
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", comando],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def docker_json(args: list[str]) -> object | None:
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def stato_container(nome: str) -> dict | None:
    dati = docker_json(["inspect", nome])
    if not isinstance(dati, list) or not dati:
        return None
    return dati[0].get("State") or {}


def avvio_container(nome: str) -> datetime | None:
    dati = docker_json(["inspect", nome])
    if not isinstance(dati, list) or not dati:
        return None
    grezzo = (dati[0].get("State") or {}).get("StartedAt")
    if not grezzo:
        return None
    # Docker usa nanosecondi; datetime ne regge sei cifre.
    testo = grezzo.replace("Z", "+00:00")
    if "." in testo:
        testa, coda = testo.split(".", 1)
        cifre = ""
        for c in coda:
            if not c.isdigit():
                break
            cifre += c
        frazione = cifre[:6].ljust(6, "0")
        fuso = coda[len(cifre):] or "+00:00"
        testo = f"{testa}.{frazione}{fuso}"
    try:
        return datetime.fromisoformat(testo).astimezone(timezone.utc)
    except ValueError:
        return None


def avvio_processo_su_porta(porta: int) -> datetime | None:
    """Ora d'avvio del processo in ascolto sulla porta, o None se non c'è."""
    pid = powershell(
        f"$c = Get-NetTCPConnection -LocalPort {porta} -State Listen "
        f"-ErrorAction SilentlyContinue; if ($c) {{ $c[0].OwningProcess }}"
    )
    if not pid or not pid.strip().isdigit():
        return None
    quando = powershell(
        f"(Get-Process -Id {pid.strip()} -ErrorAction SilentlyContinue)"
        f".StartTime.ToUniversalTime().ToString('o')"
    )
    if not quando:
        return None
    try:
        return datetime.fromisoformat(quando.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def controlla(args: argparse.Namespace) -> Esito:
    e = Esito()

    # --- 1. il motore e i tre container -------------------------------------
    for nome in (CONTAINER_POSTGRES, CONTAINER_MOSQUITTO, CONTAINER_NODERED):
        st = stato_container(nome)
        if st is None:
            e.aggiungi(BOH, f"container {nome}", "docker non risponde o il container non esiste")
        elif not st.get("Running"):
            e.aggiungi(KO, f"container {nome}", f"non in esecuzione (stato: {st.get('Status')})")
        else:
            salute = (st.get("Health") or {}).get("Status")
            if salute and salute != "healthy":
                e.aggiungi(KO, f"container {nome}", f"acceso ma salute «{salute}», aspetta che diventi healthy")
            else:
                e.aggiungi(OK, f"container {nome}", "acceso")

    # --- 2. il database risponde --------------------------------------------
    try:
        from sqlalchemy import text
        from pipeline.storage import make_engine
        with make_engine().connect() as c:
            c.execute(text("select 1"))
        e.aggiungi(OK, "database raggiungibile", "select 1 riuscito")
        db_vivo = True
    except Exception as exc:
        e.aggiungi(KO, "database raggiungibile", f"{type(exc).__name__}: {exc}")
        db_vivo = False

    # --- 3. il server OPC UA è in ascolto -----------------------------------
    avvio_opcua = avvio_processo_su_porta(PORTA_OPCUA)
    if avvio_opcua is None:
        e.aggiungi(KO, f"server OPC UA sulla {PORTA_OPCUA}",
                   "nessuno in ascolto: avvia `python -m plcsim.serve --mode realtime` PRIMA di Node-RED")
    else:
        e.aggiungi(OK, f"server OPC UA sulla {PORTA_OPCUA}",
                   f"in ascolto da {avvio_opcua.isoformat(timespec='seconds')}")

    # --- 4. Node-RED è stato riavviato DOPO il server ------------------------
    avvio_nr = avvio_container(CONTAINER_NODERED)
    if avvio_nr is None or avvio_opcua is None:
        e.aggiungi(BOH, "ordine Node-RED dopo OPC UA",
                   "non verificabile: manca l'ora d'avvio di uno dei due")
    elif avvio_nr < avvio_opcua:
        ritardo = (avvio_opcua - avvio_nr).total_seconds()
        e.aggiungi(KO, "ordine Node-RED dopo OPC UA",
                   f"Node-RED è partito {ritardo:.0f}s PRIMA del server: "
                   f"`docker restart {CONTAINER_NODERED}` e ricontrolla")
    else:
        e.aggiungi(OK, "ordine Node-RED dopo OPC UA",
                   f"riavviato {(avvio_nr - avvio_opcua).total_seconds():.0f}s dopo il server")

    # --- 5. sessione broker pulita -------------------------------------------
    if args.client_id == CLIENT_ID_CONDIVISO:
        e.aggiungi(KO, "sessione broker pulita",
                   f"«{CLIENT_ID_CONDIVISO}» è il client id condiviso: la sua coda porta "
                   f"gli arretrati della sessione precedente. Passa un --client-id dedicato")
    elif not args.client_id.strip():
        e.aggiungi(KO, "sessione broker pulita", "--client-id vuoto")
    else:
        e.aggiungi(OK, "sessione broker pulita", f"client id dedicato «{args.client_id}»")

    # --- 6. la partizione raw è nuova ---------------------------------------
    partizione = RAW / "machine=filler01" / f"date={args.date}"
    if not partizione.exists():
        e.aggiungi(OK, "partizione raw nuova", f"{partizione.relative_to(ROOT)} non esiste ancora")
    else:
        file = list(partizione.glob("*.parquet"))
        if not file:
            e.aggiungi(OK, "partizione raw nuova", "la cartella esiste ma è vuota")
        else:
            e.aggiungi(KO, "partizione raw nuova",
                       f"{len(file)} file già in {partizione.relative_to(ROOT)}: "
                       f"usa una data diversa o sposta la partizione")

    # --- 7. il run_id non è già stato usato ----------------------------------
    if not args.run_id.strip():
        e.aggiungi(KO, "run_id nuovo", "--run-id vuoto")
    elif not db_vivo:
        e.aggiungi(BOH, "run_id nuovo", "database non raggiungibile, non verificabile")
    else:
        try:
            from sqlalchemy import text
            from pipeline.storage import make_engine
            with make_engine().connect() as c:
                n = c.execute(
                    text("select count(*) from cycles where run_id = :r"),
                    {"r": args.run_id},
                ).scalar_one()
            if n:
                e.aggiungi(KO, "run_id nuovo",
                           f"«{args.run_id}» ha già {n} cicli in `cycles`: scegline un altro")
            else:
                e.aggiungi(OK, "run_id nuovo", f"«{args.run_id}» non compare in `cycles`")
        except Exception as exc:
            e.aggiungi(BOH, "run_id nuovo", f"{type(exc).__name__}: {exc}")

    return e


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Controlli di preflight per un run live. Solo letture.",
    )
    p.add_argument("--run-id", required=True,
                   help="id del run, esplicito e identico per backfill e inference")
    p.add_argument("--date", required=True,
                   help="data della partizione raw, YYYY-MM-DD")
    p.add_argument("--client-id", required=True,
                   help=f"client id MQTT dedicato a questo run (non «{CLIENT_ID_CONDIVISO}»)")
    args = p.parse_args(argv)

    print()
    print(f"Preflight run live · run-id «{args.run_id}» · data {args.date}")
    print()
    esito = controlla(args)
    esito.stampa()
    return esito.codice


if __name__ == "__main__":
    raise SystemExit(main())
