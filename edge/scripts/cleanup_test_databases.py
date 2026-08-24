"""Rimuove i database di test effimeri accumulati sul Postgres di sviluppo.

## Perché

I moduli di test che toccano PostgreSQL creano un database per processo pytest,
con nome casuale, per evitare corse DDL fra worker paralleli. Fino al 2026-08-18
mancava la pulizia: se n'erano accumulati 50, per 423 MB, e l'avvio del container
era salito a ~2,5 minuti perché PostgreSQL fa l'fsync dell'intera data directory.

Il teardown ora esiste (`pipeline/tests/conftest.py`), quindi il problema non si
ripresenta. Questo script serve a smaltire l'arretrato.

## Cosa NON tocca

La guardia è quella condivisa con i test: cancella solo nomi che corrispondono
esattamente al pattern effimero (prefisso noto + otto cifre esadecimali).
Restano intatti:

- `plcsim`, il database operazionale;
- `postgres` e i template di sistema;
- `plcsim_test` e `plcsim_test_hermetic`, che hanno nome FISSO e sono riusati.

La cancellazione è irreversibile: il default è `--dry-run`, e serve `--apply`
per agire davvero.

## Uso

    python -m edge.scripts.cleanup_test_databases            # mostra e basta
    python -m edge.scripts.cleanup_test_databases --apply    # cancella
"""
from __future__ import annotations

import argparse
import sys

ADMIN_URL = "postgresql+psycopg://plcsim:plcsim@localhost:5432/plcsim"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="esegue le cancellazioni (default: solo elenco)")
    ap.add_argument("--admin-url", default=ADMIN_URL)
    args = ap.parse_args(argv)

    from sqlalchemy import text

    from pipeline.storage import make_engine
    from pipeline.tests.conftest import e_effimero

    eng = make_engine(args.admin_url)
    with eng.connect() as conn:
        righe = conn.execute(text(
            "SELECT datname, pg_database_size(datname) AS b,"
            "       (SELECT count(*) FROM pg_stat_activity a"
            "         WHERE a.datname = d.datname) AS conn "
            "FROM pg_database d WHERE datistemplate = false "
            "ORDER BY pg_database_size(datname) DESC")).fetchall()

    da_cancellare, da_tenere = [], []
    for nome, byte, connessioni in righe:
        # e_effimero lavora su un URL: gli si passa un URL sintetico col nome
        (da_cancellare if e_effimero("//x/" + nome) else da_tenere).append(
            (nome, int(byte), int(connessioni)))

    print("DA TENERE (%d):" % len(da_tenere))
    for nome, byte, _ in da_tenere:
        print("  %-40s %8.1f MB" % (nome, byte / 1e6))

    print()
    print("DA CANCELLARE (%d):" % len(da_cancellare))
    totale = 0
    occupati = []
    for nome, byte, connessioni in da_cancellare:
        totale += byte
        if connessioni:
            occupati.append(nome)
        print("  %-40s %8.1f MB  connessioni=%d" % (nome, byte / 1e6, connessioni))
    print()
    print("totale da liberare: %.0f MB" % (totale / 1e6))

    if occupati:
        print("ATTENZIONE: %d database hanno connessioni attive" % len(occupati),
              file=sys.stderr)

    if not args.apply:
        print()
        print("dry-run: niente cancellato. Usa --apply per procedere.")
        return 0

    fatti = falliti = 0
    eng2 = make_engine(args.admin_url)
    with eng2.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        for nome, _, _ in da_cancellare:
            try:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{nome}" WITH (FORCE)'))
                fatti += 1
            except Exception as exc:
                falliti += 1
                print("  FALLITO %s: %s" % (nome, str(exc)[:80]), file=sys.stderr)
    eng2.dispose()
    print()
    print("cancellati: %d   falliti: %d" % (fatti, falliti))
    return 0 if falliti == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
