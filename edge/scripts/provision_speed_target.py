"""Persiste il KV `speed_target` nello storico operazionale.

PERCHE' ESISTE
L'API OEE calcola Performance = cicli osservati / (speed_target x ore di Running).
Se nessuno persiste `speed_target`, l'API usa la costante di default
`DEFAULT_SPEED_TARGET_CPH = 15500` — che NON e' coerente con l'impianto simulato:
la cadenza reale e' `rotation_ms = 3200` ms per valvola su 35 valvole, cioe'
35 x 3600 / 3,2 = 39.375 lattine/ora, 2,54 volte il valore di targa.
`docs/V3-DESIGN.md` sez. 188 registra il punto come aperto: "V2 usa 3,2 s/rotazione
ma il dato reale per-valvola suggerisce cadenze diverse [...] da riconciliare con
la cadenza osservata".

Da quando l'API dichiara la provenienza del target, un target di default che
produce un rapporto implausibile degrada con reason invece di fabbricare un OEE
(prima: 194%). Questo script chiude il caso dal lato giusto: rende esplicito il
target dell'impianto invece di modificare la geometria calibrata del simulatore,
che regge il limite encoder, la distribuzione di filling_step_out e la frequenza
di oscillazione del driver.

COSA SOSTITUISCE
In un impianto reale questo valore arriva dal PLC (tag `SpeedTarget`) attraverso
il percorso di ingest. Qui lo scrive uno script perche' quel writer non esiste
ancora: e' un provisioning, non un pezzo del data-plane.

USO
    python -m edge.scripts.provision_speed_target            # deriva e scrive
    python -m edge.scripts.provision_speed_target --dry-run  # mostra e basta
    python -m edge.scripts.provision_speed_target --value 15500   # forza un valore

Il valore derivato NON e' cablato: viene calcolato da `plcsim.config` a ogni
esecuzione, cosi' se la geometria cambia il target la segue.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

KEY = "speed_target"


def derive_cph() -> tuple[float, str]:
    """Portata nominale dell'impianto simulato, in lattine/ora.

    Ogni valvola completa un ciclo ogni `rotation_ms`; le valvole sono
    `n_valves`. La portata nominale e' quindi `n_valves * 3600 / rotation_s`.
    Ritorna anche la derivazione in chiaro, che finisce nel KV.
    """
    from plcsim.config import SimConfig

    cfg = SimConfig()
    rot_ms = float(cfg.recipe.rotation_ms)
    n_valves = 35
    cph = n_valves * 3600.0 / (rot_ms / 1000.0)
    derivazione = (f"{n_valves} valvole x 3600 s / (rotation_ms {rot_ms:.0f} ms) "
                   f"= {cph:.1f} lattine/ora")
    return cph, derivazione


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--value", type=float, default=None,
                    help="forza un valore in lattine/ora invece di derivarlo")
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra cosa scriverebbe, senza scrivere")
    ap.add_argument("--database-url", default=None,
                    help="URL SQLAlchemy; default PLCSIM_DATABASE_URL")
    args = ap.parse_args(argv)

    if args.value is not None:
        cph, derivazione = float(args.value), "valore forzato da riga di comando"
    else:
        cph, derivazione = derive_cph()

    payload = {
        "speed_target": round(cph, 1),
        "unit": "cph",
        "derived_from": derivazione,
        "set_by": "edge/scripts/provision_speed_target.py",
        "set_at": datetime.now(timezone.utc).isoformat(),
    }

    print("chiave :", KEY)
    for k, v in payload.items():
        print("  %-12s %s" % (k, v))

    if args.dry_run:
        print()
        print("dry-run: niente scritto")
        return 0

    from pipeline.storage import Storage, make_engine

    st = Storage(make_engine(args.database_url))
    if not st.ping():
        print()
        print("ERRORE: database non raggiungibile", file=sys.stderr)
        return 2
    st.set_machine_state(KEY, payload)

    riletto = st.get_machine_state(KEY)
    ok = isinstance(riletto, dict) and riletto.get("speed_target") == payload["speed_target"]
    print()
    print("scritto e riletto:", "OK" if ok else "DISCORDANTE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
