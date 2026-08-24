"""Utilità condivise dei test della pipeline.

## Perché esiste

I moduli che toccano PostgreSQL creano un database dedicato **per processo
pytest**, con un nome casuale: i worker paralleli condividono lo stesso server e
un nome fisso produce corse DDL. La scelta è giusta, ma mancava la pulizia.

Effetto misurato il 2026-08-18 sul Postgres di sviluppo: **50 database residui,
423 MB**, e l'avvio del container arrivato a ~2,5 minuti perché PostgreSQL fa
l'fsync dell'intera data directory. Il costo cresce a ogni esecuzione della
suite.

Qui vive la rimozione a fine sessione, condivisa dai moduli che creano un
database effimero.

## La guardia

`drop_db_if_ephemeral` cancella **solo** nomi che corrispondono esattamente al
pattern dei database effimeri generati dai test: prefisso noto più otto cifre
esadecimali. Il database operazionale `plcsim`, i database di test a nome fisso
(`plcsim_test`, `plcsim_test_hermetic`) e qualunque URL fornito dall'esterno con
un nome diverso non corrispondono, quindi non vengono toccati. La cancellazione
è irreversibile: la guardia è deliberatamente stretta, non permissiva.
"""
from __future__ import annotations

import re

# prefissi dei database effimeri, uno per modulo che ne crea
_PREFISSI_EFFIMERI = ("plcsim_test_fix_", "plcsim_test_oee_",
                      # piu' lungo prima: `plcsim_test_baseline_` da solo non
                      # copre i database di test_baseline_cache.py, il cui nome
                      # continua con `cache_` e non con le otto cifre esadecimali.
                      "plcsim_test_baseline_cache_", "plcsim_test_baseline_",
                      "plcsim_test_run_",
                      "plcsim_test_rollup_", "plcsim_test_qser_",
                      "plcsim_test_prof_")

_EFFIMERO = re.compile(
    r"^(?:%s)[0-9a-f]{8}$" % "|".join(re.escape(p) for p in _PREFISSI_EFFIMERI))


def nome_db(url: str) -> str:
    """Ultimo segmento dell'URL SQLAlchemy, senza query string."""
    return url.rsplit("/", 1)[-1].split("?", 1)[0]


def e_effimero(url: str) -> bool:
    """Vero solo per un database creato dai test con nome casuale."""
    return bool(_EFFIMERO.match(nome_db(url)))


def drop_db_if_ephemeral(url: str, admin_url: str | None = None) -> bool:
    """Cancella il database di `url` se e solo se è effimero.

    Ritorna True se ha cancellato. Non solleva mai: una pulizia fallita non
    deve far fallire una suite verde — al massimo lascia un database in più,
    che è la situazione di partenza.
    """
    if not e_effimero(url):
        return False
    nome = nome_db(url)
    admin = admin_url or url.rsplit("/", 1)[0] + "/plcsim"
    try:
        from sqlalchemy import text

        from pipeline.storage import make_engine

        eng = make_engine(admin)
        try:
            with eng.connect() as conn:
                conn.execution_options(isolation_level="AUTOCOMMIT")
                conn.execute(text(f'DROP DATABASE IF EXISTS "{nome}" WITH (FORCE)'))
            return True
        finally:
            eng.dispose()
    except Exception:
        return False
