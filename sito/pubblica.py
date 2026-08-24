"""Raccoglie le pagine montate nella cartella che GitHub Pages pubblica.

`costruisci.py` e `viaggio/costruisci.py` montano le pagine in due `build/`
diverse, perche' sono due montatori distinti. Un sito vuole invece un albero
solo, con un ingresso.

    pubblica/
      index.html                    il viaggio del dato
      guida/01-simulatore.html      la guida tecnica, otto tappe
      ...

L'ingresso e' il viaggio, non la guida: chi arriva da un indirizzo non sa
ancora cos'e' il progetto, e la guida da' per scontato che lo sappia.

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
    if not (BUILD / "01-simulatore.html").exists():
        print("manca la guida montata. Lancia prima: python sito/costruisci.py")
        return 1
    viaggio = BUILD_VIAGGIO / "viaggio.html"
    if not viaggio.exists():
        print("manca il viaggio montato. "
              "Lancia prima: python sito/viaggio/costruisci.py")
        return 1

    if PUBBLICA.exists():
        shutil.rmtree(PUBBLICA)
    (PUBBLICA / "guida").mkdir(parents=True)

    shutil.copy2(viaggio, PUBBLICA / "index.html")
    n = 0
    for p in sorted(BUILD.glob("*.html")):
        shutil.copy2(p, PUBBLICA / "guida" / p.name)
        n += 1

    peso = sum(f.stat().st_size for f in PUBBLICA.rglob("*") if f.is_file())
    print(f"pubblica/: index.html + {n} tappe in guida/  ({peso/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
