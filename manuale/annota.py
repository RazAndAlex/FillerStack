"""Annota gli screenshot del manuale con riquadri rossi numerati.

Convenzione presa dalle guide operative Rilheva in FMT: un rettangolo rosso
attorno all'elemento di cui si parla, con un numero in un cerchio. Il testo
del manuale rimanda ai numeri.

Ogni casella e' (numero, x1, y1, x2, y2, bx, by): il rettangolo segue le
prime quattro coordinate, il cerchio col numero sta in (bx, by), scelto a
mano dove non copre testo. Coordinate su 1536x770.

Legge shots/NN-*.png e scrive shots/ann-NN-*.png.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

QUI = Path(__file__).parent
SHOTS = QUI / "shots"

ROSSO = (214, 24, 32)
BIANCO = (255, 255, 255)

FONT = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 26)

ANN = {
    "01-macchina": [
        (1, 22, 92, 496, 196, 470, 118),      # stato macchina + scala OMAC
        (2, 28, 198, 302, 218, 328, 208),     # ultimo dato
        (3, 105, 250, 405, 528, 82, 274),     # gauge OEE grande
        (4, 28, 618, 492, 762, 30, 594),      # tre gauge piccoli
        (5, 518, 55, 1256, 356, 542, 102),    # andamento OEE
        (6, 518, 360, 1256, 561, 542, 408),   # ripartizione del tempo
        (7, 518, 565, 1256, 761, 542, 736),   # allarmi per valvola
        (8, 1259, 55, 1521, 761, 1486, 108),  # turno + contatori
    ],
    "02-valvole": [
        (1, 16, 55, 1086, 761, 42, 102),      # giostra
        (2, 465, 340, 645, 535, 555, 314),    # mozzo centrale
        (3, 212, 652, 355, 752, 188, 628),    # legenda candela
        (4, 268, 268, 352, 332, 244, 244),    # corona allarmi (es. 29-30)
        (5, 1093, 55, 1527, 176, 1498, 102),  # riempimenti conformi
        (6, 1093, 180, 1527, 302, 1498, 226), # motivi di chiusura
        (7, 1093, 308, 1527, 761, 1498, 360), # non conformi 35
    ],
    "03-oee": [
        (1, 10, 55, 1221, 461, 36, 104),      # cascata
        (2, 10, 462, 406, 761, 376, 506),     # disponibilità
        (3, 410, 462, 811, 761, 782, 506),    # prestazione
        (4, 815, 462, 1221, 761, 1192, 506),  # qualità
        (5, 1225, 55, 1528, 761, 1498, 100),  # adesso
    ],
    "04-tempo": [
        (1, 10, 55, 1141, 281, 36, 106),      # OEE
        (2, 1145, 55, 1528, 281, 1172, 106),  # tre componenti
        (3, 10, 285, 1528, 426, 36, 412),     # 35 stessa scala
        (4, 10, 430, 1528, 601, 692, 446),    # qualità per valvola
        (5, 10, 605, 1528, 761, 36, 652),     # striscia due mesi
        (6, 1068, 658, 1392, 747, 1044, 634), # finestra trascinabile
    ],
    "05-carta": [
        (1, 10, 55, 1528, 346, 96, 104),      # ciclo singolo
        (2, 10, 350, 1528, 646, 96, 420),     # media mobile 46
        (3, 78, 378, 122, 632, 100, 500),     # primi 45 cicli
        (4, 1462, 95, 1526, 335, 1438, 72),   # asse sigma
        (5, 10, 652, 1528, 766, 36, 752),     # striscia valvole
    ],
    "06-predittiva": [
        (1, 12, 48, 562, 76, 588, 62),        # legenda
        (2, 14, 110, 208, 688, 232, 134),     # etichette
        (3, 443, 258, 1388, 292, 418, 234),   # striscia v8
        (4, 443, 256, 480, 294, 461, 318),    # tacche
        (5, 1448, 253, 1522, 297, 1424, 229), # margine
        (6, 1453, 306, 1527, 334, 1428, 352), # vuoto non vedibile
    ],
    "07-pannello-valvola": [
        (1, 974, 0, 1536, 48, 1150, 24),      # testata e chiusura
        (2, 984, 53, 1527, 107, 952, 92),     # allarmi attivi
        (3, 984, 124, 1527, 216, 952, 168),   # ultimo ciclo
        (4, 984, 228, 1527, 732, 952, 270),   # carte di controllo
    ],
    "08-pannello-predittiva": [
        (1, 984, 50, 1527, 146, 952, 96),     # chip dei momenti
        (2, 984, 164, 1527, 346, 952, 206),   # grafico del canale
        (3, 984, 364, 1527, 416, 952, 390),   # nota a piè
    ],
}

LARGO_R = 4
RAGGIO = 20


def annota(nome: str, caselle: list) -> None:
    img = Image.open(SHOTS / f"{nome}.png").convert("RGB")
    d = ImageDraw.Draw(img)
    for n, x1, y1, x2, y2, bx, by in caselle:
        for i in range(LARGO_R):
            d.rectangle([x1 - i, y1 - i, x2 + i, y2 + i], outline=ROSSO)
        d.ellipse([bx - RAGGIO, by - RAGGIO, bx + RAGGIO, by + RAGGIO],
                  fill=ROSSO, outline=BIANCO, width=2)
        t = str(n)
        bb = d.textbbox((0, 0), t, font=FONT)
        d.text((bx - (bb[2] - bb[0]) / 2, by - (bb[3] - bb[1]) / 2 - bb[1]),
               t, font=FONT, fill=BIANCO)
    img.save(SHOTS / f"ann-{nome}.png")
    print("annotata:", nome)


for nome, caselle in ANN.items():
    annota(nome, caselle)
