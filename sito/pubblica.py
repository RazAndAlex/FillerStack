"""Raccoglie le pagine montate nella cartella che GitHub Pages pubblica.

`costruisci.py` e `viaggio/costruisci.py` montano le pagine in due `build/`
diverse, perche' sono due montatori distinti. Un sito vuole invece un albero
solo, con un ingresso.

    pubblica/
      index.html      il viaggio del dato

Il sito e' il viaggio, e basta. La guida tecnica in otto tappe resta materiale
personale dell'autore: si monta in locale con `costruisci.py`, si legge da
`sito/build/`, e non viene pubblicata. E' sempre stata una cosa a parte.

Uso, dopo aver montato:

    python sito/costruisci.py
    python sito/viaggio/costruisci.py
    python sito/verifica.py
    python sito/pubblica.py

La cartella `pubblica/` non sta nel repository: si rifa' da sola, e tenerla
dentro vorrebbe dire avere due volte le stesse pagine, una delle quali
prima o poi resta indietro.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

QUI = Path(__file__).parent
BUILD = QUI / "build"
BUILD_VIAGGIO = QUI / "viaggio" / "build"
PUBBLICA = QUI / "pubblica"


def main() -> int:
    viaggio = BUILD_VIAGGIO / "viaggio.html"
    if not viaggio.exists():
        print("manca il viaggio montato. "
              "Lancia prima: python sito/viaggio/costruisci.py")
        return 1

    # Si svuota il contenuto invece di cancellare la cartella: mentre la si
    # guarda in locale un server la tiene aperta, e su Windows rimuoverla
    # fallisce. Rifare il sito non deve pretendere di chiudere l'anteprima.
    for x in PUBBLICA.iterdir() if PUBBLICA.exists() else []:
        shutil.rmtree(x) if x.is_dir() else x.unlink()
    PUBBLICA.mkdir(parents=True, exist_ok=True)
    shutil.copy2(viaggio, PUBBLICA / "index.html")

    peso = sum(f.stat().st_size for f in PUBBLICA.rglob("*") if f.is_file())
    print(f"pubblica/: index.html  ({peso/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
