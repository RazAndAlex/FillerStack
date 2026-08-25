"""Monta una tappa della guida a partire dai pezzi comuni.

Una tappa e' un file in tappe/NN-nome.html che contiene soltanto il proprio
<title> (prima riga) e il proprio <div class="foglio">. Barra, mappa, tinte e
comportamenti stanno in comune/ e non si duplicano: e' l'unico modo per cui
otto tappe restano la stessa pagina invece di diventare otto pagine simili.

Uso:
    python sito/costruisci.py            # tutte le tappe
    python sito/costruisci.py 02         # solo la tappa 02

L'esito finisce in build/NN-nome.html, pronto da pubblicare come artefatto
(un solo file, nessuna risorsa esterna a parte il carattere da Google Fonts).
"""

import io
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

# Senza viewport un telefono finge di essere largo 980 px e rimpicciolisce
# tutto: la pagina si legge solo con le dita.
VIEWPORT = (
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
)


# Le otto tappe, nell'ordine della catena vera del progetto. L'indice di
# posizione serve alla mappa in cima per accendere il riquadro giusto.
TAPPE = [
    "Simulatore",
    "OPC UA",
    "Node-RED",
    "MQTT",
    "Mosquitto",
    "Docker",
    "Machine learning",
    "API e dashboard",
]


def monta(sorgente: Path) -> Path:
    testa = (QUI / "comune" / "testa.html").read_text(encoding="utf-8")
    coda = (QUI / "comune" / "coda.html").read_text(encoding="utf-8")
    righe = sorgente.read_text(encoding="utf-8").split("\n")

    titolo, corpo = righe[0], "\n".join(righe[1:])
    if not titolo.startswith("<title>"):
        raise SystemExit(f"{sorgente.name}: la prima riga deve essere il <title>")

    # 02-opcua.html -> indice 1 (la seconda tappa della catena)
    indice = int(sorgente.name.split("-")[0]) - 1
    if not 0 <= indice < len(TAPPE):
        raise SystemExit(f"{sorgente.name}: numero di tappa fuori dalla catena")

    testa = testa.replace("__NOME__", TAPPE[indice])
    coda = coda.replace("__QUI__", str(indice))

    # `build/` e' ignorata da git, quindi su un clone fresco non esiste e la
    # scrittura falliva con FileNotFoundError. In locale il difetto restava
    # invisibile, perche' la cartella c'era gia' dalle corse precedenti: se ne
    # e' accorta la prima esecuzione su GitHub Actions.
    (QUI / "build").mkdir(parents=True, exist_ok=True)
    esito = QUI / "build" / sorgente.name
    # La codifica va dichiarata dalla pagina: servita da un server che non la
    # manda, senza questa riga gli accenti si rompono. Sta quasi prima di tutto,
    # perche' il browser decide leggendo i primi byte, e il doctype con la
    # lingua che la precedono occupano quaranta byte.
    esito.write_text(TESTA_HTML + titolo + "\n" + VIEWPORT
                     + testa + corpo + coda, encoding="utf-8")
    return esito


def main() -> None:
    filtro = sys.argv[1] if len(sys.argv) > 1 else ""
    sorgenti = sorted(p for p in (QUI / "tappe").glob("*.html")
                      if p.name.startswith(filtro))
    if not sorgenti:
        raise SystemExit("nessuna tappa da montare")
    for s in sorgenti:
        print("montata:", monta(s).relative_to(QUI))


if __name__ == "__main__":
    main()
