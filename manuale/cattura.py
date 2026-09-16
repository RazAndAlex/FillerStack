"""Screenshot delle sei pagine della dashboard per il manuale.

Viewport 1536x770 px CSS: il viewport vero dell'utente.
Salva in manuale/shots/.
"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

QUI = Path(__file__).parent
OUT = QUI / "shots"
OUT.mkdir(parents=True, exist_ok=True)

BASE = "http://127.0.0.1:8078"
# (nome file, percorso, attesa extra in ms dopo il load)
PAGINE = [
    ("01-macchina", "/a/", 6000),
    ("02-valvole", "/v1/", 12000),
    ("03-oee", "/oee/", 6000),
    ("04-tempo", "/pc/", 9000),
    ("05-carta", "/k1/", 15000),
    ("06-predittiva", "/predittiva/", 20000),
]

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    pagina = browser.new_page(viewport={"width": 1536, "height": 770})
    errori = []
    pagina.on("console", lambda m: errori.append(m.text) if m.type == "error" else None)

    for nome, percorso, attesa in PAGINE:
        pagina.goto(BASE + percorso, wait_until="networkidle")
        pagina.wait_for_timeout(attesa)
        pagina.screenshot(path=str(OUT / f"{nome}.png"))
        print("fatto:", nome)

    # pannello valvola su MACCHINA: clic sulla cella della valvola 8
    pagina.goto(BASE + "/a/", wait_until="networkidle")
    pagina.wait_for_timeout(6000)
    pagina.evaluate(
        "document.querySelectorAll('#valvole .cella-valvola')[7]"
        ".dispatchEvent(new MouseEvent('click', {bubbles: true}))"
    )
    pagina.wait_for_timeout(4000)
    pagina.screenshot(path=str(OUT / "07-pannello-valvola.png"))
    print("fatto: 07-pannello-valvola")

    # pannello di dettaglio su PREDITTIVA: clic sulla prima riga
    pagina.goto(BASE + "/predittiva/", wait_until="networkidle")
    pagina.wait_for_timeout(20000)
    pagina.evaluate(
        "var r = document.querySelector('.riga');"
        "if (r) r.dispatchEvent(new MouseEvent('click', {bubbles: true}))"
    )
    pagina.wait_for_timeout(5000)
    pagina.screenshot(path=str(OUT / "08-pannello-predittiva.png"))
    print("fatto: 08-pannello-predittiva")

    browser.close()

if errori:
    print("ERRORI CONSOLE:")
    for e in errori:
        print(" -", e)
else:
    print("console pulita")
