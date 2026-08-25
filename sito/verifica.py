"""Controlla una pagina montata prima di pubblicarla.

Non giudica il contenuto: controlla le cose che sono gia' costate una
bocciatura o che rompono la pagina in silenzio. Ogni regola qui dentro
corrisponde a una riga di GRAMMATICA.md.

Uso:
    python sito/verifica.py            # tutto build/
    python sito/verifica.py 03         # solo la tappa 03
"""

import io
import re
import sys
from pathlib import Path

QUI = Path(__file__).parent
BUILD = QUI / "build"
BUILD_VIAGGIO = QUI / "viaggio" / "build"


def controlla(p: Path) -> list[str]:
    s = io.open(p, encoding="utf-8").read()
    errori: list[str] = []

    # 1. trattini lunghi: l'utente li conta (GRAMMATICA.md §6)
    n = s.count("—") + s.count("–")
    if n:
        errori.append(f"{n} em/en dash nel testo")

    # 2. segnaposto non sostituiti dal montatore
    for segno in ("__QUI__", "__NOME__"):
        if segno in s:
            errori.append(f"segnaposto {segno} rimasto")

    # 3. titolo in prima riga, e uno solo nella testa del file.
    #    Piu' avanti <title> ricompare dentro gli <svg> della mappa, dove e'
    #    il testo del suggerimento: quello va lasciato stare.
    # Dal 2026-08-24 la prima riga e' la dichiarazione di codifica: servite da
    # un server che non manda il charset, le pagine rompevano gli accenti. Dal
    # 2026-08-25 la precedono il doctype e la lingua, e il titolo resta subito
    # dopo, e resta unico.
    APERTURA = ('<!doctype html>' + chr(10)
                + '<html lang="it">' + chr(10)
                + '<meta charset="utf-8">' + chr(10)
                + '<title>')
    if not s.startswith(APERTURA):
        errori.append("in testa manca doctype, lingua, charset e <title>")
    if s.count("<title>", 0, 8192) != 1:
        errori.append("piu' di un <title> nella testa del file")

    # 3b. il viewport: senza, un telefono rimpicciolisce tutta la pagina.
    if 'name="viewport"' not in s[:2048]:
        errori.append("manca il <meta name=viewport> nella testa")

    # 4. ogni <svg> informativo va descritto (GRAMMATICA.md §7).
    #    Gli <svg> vuoti riempiti da JavaScript portano l'etichetta nel markup,
    #    quindi il controllo vale per tutti.
    for m in re.finditer(r"<svg\b[^>]*>", s):
        tag = m.group(0)
        if 'aria-hidden="true"' in tag:
            continue
        if "aria-label" not in tag and "aria-labelledby" not in tag:
            errori.append("un <svg> senza aria-label: " + tag[:70])

    # 5. ogni nota deve puntare a righe di codice che esistono davvero
    for blocco in re.finditer(r'data-cod="([^"]+)"', s):
        pre_id = blocco.group(1)
        m = re.search(r'<pre[^>]*id="%s"[^>]*>(.*?)</pre>' % re.escape(pre_id), s, re.S)
        if not m:
            errori.append(f"le note puntano a {pre_id}, che non esiste")
            continue
        righe = set(re.findall(r'class="r" data-r="(\d+)"', m.group(1)))
        # le note del blocco: cerco i bottoni dopo la dichiarazione data-cod
        coda = s[blocco.end():]
        fine = coda.find("</section>")
        note = set(re.findall(r'class="nota"[^>]*data-r="([\d,]+)"', coda[:fine]))
        for nota in note:
            for r in nota.split(","):
                if r not in righe:
                    errori.append(f"{pre_id}: una nota punta alla riga {r}, che non c'e'")

    # 6. la mappa in cima deve accendere una tappa sola, e valida.
    #    Vale per le pagine della guida; il viaggio del dato non ha mappa e
    #    usa invece il filo di avanzamento.
    if 'id="mappa"' in s:
        m = re.search(r"var QUI = (\d+);", s)
        if not m:
            errori.append("la mappa non sa a quale tappa sei")
        elif not 0 <= int(m.group(1)) <= 7:
            errori.append("la mappa punta a una tappa fuori dalla catena")
    elif 'id="filo"' not in s:
        errori.append("la pagina non ha ne' mappa ne' filo di avanzamento")

    # 7. niente risorse esterne oltre al carattere: un artefatto e' un file solo.
    #    Un <a> non e' una risorsa: non viene caricato, e la pagina regge lo
    #    stesso se l'indirizzo e' morto. La regola serve contro gli asset che
    #    fanno dipendere il file da un altro server, non contro i collegamenti,
    #    che fino al 2026-08-25 non c'erano e sono il modo di uscire da qui.
    for m in re.finditer(r"<([a-zA-Z][\w-]*)\b([^>]*)>", s):
        etichetta, attributi = m.group(1).lower(), m.group(2)
        if etichetta == "a":
            continue
        for url in re.findall(r'(?:src|href)="(https?://[^"]+)"', attributi):
            if not url.startswith(("https://fonts.googleapis.com",
                                   "https://fonts.gstatic.com")):
                errori.append("risorsa esterna non ammessa: " + url)

    # 8. i tre temi devono esserci tutti (GRAMMATICA.md §1)
    for atteso in (":root{", "prefers-color-scheme: dark",
                   ':root[data-theme="dark"]'):
        if atteso not in s:
            errori.append(f"manca il blocco tema: {atteso}")

    # 9. tag aperti e mai chiusi fra quelli che rompono l'impaginazione
    for tag in ("section", "div", "pre", "button"):
        ap = len(re.findall(r"<%s\b" % tag, s))
        ch = len(re.findall(r"</%s>" % tag, s))
        if ap != ch:
            errori.append(f"<{tag}>: {ap} aperti e {ch} chiusi")

    return errori


def main() -> None:
    filtro = sys.argv[1] if len(sys.argv) > 1 else ""
    tutte = list(BUILD.glob("*.html")) + list(BUILD_VIAGGIO.glob("*.html"))
    pagine = sorted((p for p in tutte if p.name.startswith(filtro)), key=lambda p: p.name)
    if not pagine:
        raise SystemExit("nessuna pagina da controllare")
    guasti = 0
    for p in pagine:
        errori = controlla(p)
        if errori:
            guasti += 1
            print(f"[NO] {p.name}")
            for e in errori:
                print("      -", e)
        else:
            print(f"[ok] {p.name}")
    print()
    print(f"{len(pagine) - guasti} su {len(pagine)} pagine pulite")
    if guasti:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
