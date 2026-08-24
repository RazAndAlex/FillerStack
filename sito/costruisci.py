"""Monta una tappa della guida a partire dai pezzi comuni.

Una tappa e' un file in tappe/NN-nome.html che contiene soltanto il proprio
<title> (prima riga) e il proprio <div class="foglio">. Barra, mappa, tinte e
comportamenti stanno in comune/ e non si duplicano: e' l'unico modo per cui
otto tappe restano la stessa pagina invece di diventare otto pagine simili.

Uso:
    python .scratch/presentazione/costruisci.py            # tutte le tappe
    python .scratch/presentazione/costruisci.py 02         # solo la tappa 02

L'esito finisce in build/NN-nome.html, pronto da pubblicare come artefatto
(un solo file, nessuna risorsa esterna a parte il carattere da Google Fonts).
"""

import io
import sys
from pathlib import Path

QUI = Path(__file__).parent

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

    esito = QUI / "build" / sorgente.name
    # La codifica va dichiarata dalla pagina: servita da un server che non la
    # manda, senza questa riga gli accenti si rompono. Sta prima di tutto il
    # resto, perche' il browser decide leggendo i primi byte.
    esito.write_text('<meta charset="utf-8">\n' + titolo + "\n"
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
