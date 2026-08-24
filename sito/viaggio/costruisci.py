"""Monta le varianti del viaggio del dato.

Stessa idea del montatore della guida: il corpo sta in tappe/, la
grammatica in comune/, e nessuna variante duplica tinte o barra.

Uso:  python .scratch/presentazione/viaggio/costruisci.py
"""

import sys
from pathlib import Path

QUI = Path(__file__).parent


def monta(sorgente: Path) -> Path:
    testa = (QUI / "comune" / "testa.html").read_text(encoding="utf-8")
    coda = (QUI / "comune" / "coda.html").read_text(encoding="utf-8")
    righe = sorgente.read_text(encoding="utf-8").split("\n")
    titolo, corpo = righe[0], "\n".join(righe[1:])
    if not titolo.startswith("<title>"):
        raise SystemExit(f"{sorgente.name}: la prima riga deve essere il <title>")
    esito = QUI / "build" / sorgente.name
    # La codifica va dichiarata dalla pagina: servita da un server che non la
    # manda, senza questa riga gli accenti si rompono. Sta prima di tutto il
    # resto, perche' il browser decide leggendo i primi byte.
    esito.write_text('<meta charset="utf-8">\n' + titolo + "\n"
                     + testa + corpo + coda, encoding="utf-8")
    return esito


def main() -> None:
    sorgenti = sorted((QUI / "tappe").glob("*.html"))
    if not sorgenti:
        raise SystemExit("nessuna variante da montare")
    for s in sorgenti:
        print("montata:", monta(s).relative_to(QUI))


if __name__ == "__main__":
    main()
