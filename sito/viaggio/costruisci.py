"""Monta le varianti del viaggio del dato.

Stessa idea del montatore della guida: il corpo sta in tappe/, la
grammatica in comune/, e nessuna variante duplica tinte o barra.

Uso:  python sito/viaggio/costruisci.py
"""

import sys
from pathlib import Path

QUI = Path(__file__).parent

# Il doctype mancava: senza, il browser sceglie la modalita' "quirks" e alcune
# misure cambiano in silenzio. La lingua serve a chi legge con un lettore di
# schermo, che altrimenti pronuncia l'italiano all'inglese.
TESTA_HTML = (
    '<!doctype html>\n'
    '<html lang="it">\n'
    '<meta charset="utf-8">\n'
)

# Questa e' l'unica pagina che il sito pubblica davvero, quindi e' l'unica che
# qualcuno incolla in una chat o in un messaggio. Senza queste righe l'anteprima
# mostra l'indirizzo nudo, e la descrizione la scrive il motore di ricerca
# pescando la prima frase che trova.
INDIRIZZO = "https://razandalex.github.io/FillerStack/"
DESCRIZIONE = (
    "Un numero nato dentro una lattina, seguito per undici passaggi fino "
    "alla schermata di un tecnico: la catena IIoT di una riempitrice "
    "rotativa a 35 valvole, raccontata per intero."
)


def testa_sociale(titolo: str) -> str:
    """Le righe che decidono l'anteprima quando l'indirizzo viene condiviso."""
    nome = titolo.replace("<title>", "").replace("</title>", "").strip()
    return (
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta name="description" content="{DESCRIZIONE}">\n'
        '<meta property="og:type" content="website">\n'
        f'<meta property="og:title" content="{nome}">\n'
        f'<meta property="og:description" content="{DESCRIZIONE}">\n'
        f'<meta property="og:url" content="{INDIRIZZO}">\n'
        '<meta property="og:locale" content="it_IT">\n'
        '<meta name="twitter:card" content="summary">\n'
    )



def monta(sorgente: Path) -> Path:
    testa = (QUI / "comune" / "testa.html").read_text(encoding="utf-8")
    coda = (QUI / "comune" / "coda.html").read_text(encoding="utf-8")
    righe = sorgente.read_text(encoding="utf-8").split("\n")
    titolo, corpo = righe[0], "\n".join(righe[1:])
    if not titolo.startswith("<title>"):
        raise SystemExit(f"{sorgente.name}: la prima riga deve essere il <title>")
    esito = QUI / "build" / sorgente.name
    # La codifica va dichiarata dalla pagina: servita da un server che non la
    # manda, senza questa riga gli accenti si rompono. Sta quasi prima di tutto,
    # perche' il browser decide leggendo i primi byte, e il doctype con la
    # lingua che la precedono occupano quaranta byte.
    esito.write_text(TESTA_HTML + titolo + "\n" + testa_sociale(titolo)
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
