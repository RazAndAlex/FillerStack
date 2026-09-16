"""Il verdetto 6c dentro `pipeline/` — adattatore, come `features.py`.

Perche' esiste: la politica «costo atteso» e' stata studiata nel laboratorio
`work/policy-lab/`, che pero' non e' importabile da qui. Tre motivi, verificati:

  1. `work/policy-lab/banco.py` righe 67-78 riscrive `comune.baseline_da_serie`
     con un memo a chiave `id()`. Gli `id()` si riusano dopo una deallocazione,
     quindi dentro un processo che serve piu' richieste quel memo puo'
     restituire la baseline della valvola sbagliata, in silenzio.
  2. `comune.API` e' cablato a `127.0.0.1:8123`: il laboratorio parla con
     l'API via HTTP, mentre qui si e' dentro l'API.
  3. `costi.PREZZO_VALVOLA_EUR` vale `None`: nel laboratorio il prezzo e' la
     variabile del banco, qui e' un parametro fissato.

Dentro `pipeline/` non esiste un solo import da `work/` e deve restare cosi'.
Il calcolo e' quindi RISCRITTO, leggendo il laboratorio come riferimento. La
cache della baseline e' locale alla singola richiesta e a chiave esplicita
`(run_id, valve_id)`, mai a chiave `id()`.

Che cosa fa, in una frase: ora per ora, per ogni valvola, confronta quanto
costa aspettare un'ora in piu' (`D`) con quanto fa risparmiare aspettare
un'ora in piu' (`R`), e quando aspettare costa di piu' per `K` ore di fila fa
partire la chiamata. La squadra arriva `L` ore dopo.

La forma dell'oggetto restituito e' quella di `work/pezzo7/CONTRATTO.md`.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timedelta
from typing import Any

# --------------------------------------------------------------------------
#  I parametri fissi
# --------------------------------------------------------------------------
# Da `work/pezzo7/fixture.py` righe 31-36: sono i valori con cui sono state
# registrate le fixture di `work/pezzo7/dati/`, il metro di paragone di questa
# rotta.
PREZZO_VALVOLA_EUR = 1500.0      # prezzo di una valvola nuova, euro
F_U = 3700.0                     # fermo NON pianificato, euro/h
L_H = 24.0                       # ore fra la chiamata e l'arrivo della squadra
K_CONFERMA = 2                   # «intervieni» di fila prima della chiamata
CANALE = "mean_filling_time_ms"  # il canale su cui si legge il degrado
CLASSE = "flowmeter_dropout"     # classe di ripiego (`banco.CLASSE_DI_RIPIEGO`)

# Da `work/policy-lab/costi.py`, ogni numero con la sua fonte.
VITA_UTILE_H = 500.0                      # ore di esercizio di una valvola
LATTINA_SCARTATA_EUR = 0.06               # aluminum-can.com 2025-26; IMARC 2025
DURATA_FERMO_NON_PIANIFICATO_MIN = 81.0   # Siemens Senseye 2024
DURATA_FERMO_PIANIFICATO_MIN = 6.0        # KHS
QUOTA_FERMO_PIANIFICATO = 0.47            # Oxmaint 2025, STIMATO

# Da `work/policy-lab/comune.py`.
MIN_TOT = 100        # un secchiello sotto i 100 cicli non e' un'ora di esercizio
REGIME_MIN_H = 24    # un regime sopra/sotto soglia dura almeno 24 h
K_SIGMA = 3          # mu +- 3 sigma
ORE_PRIMA_DI_PARLARE = 24.0   # la regola del segnale pretende 24 h di regime

# Da `work/policy-lab/stima.py`: il valore del canale nell'ora del crollo,
# misurato sulle corse dove il crollo esiste, per tipo di guasto.
SOGLIE_MISURATE = {
    "restriction": (2025.858, 2034.204),
    "flowmeter_dropout": (1997.645, 2002.954),
    "closing_delay": (),
    "pressure_instability": (),
}
CANALE_GUASTO = {
    "restriction": "mean_filling_time_ms",
    "opening_delay": "mean_filling_time_ms",
    "flowmeter_dropout": "mean_filling_time_ms",
    "flowmeter_glitch": "mean_filling_time_ms",
    "closing_delay": "mean_tail_time_ms",
    "pressure_instability": "sigma_filling_time_ms",
}


# I nomi delle costanti che entrano nel conto, elencati una volta sola.
# Chi aggiunge una costante alla politica la aggiunge **qui**: e' questo elenco
# che decide se un precalcolo e' ancora valido.
PARAMETRI_IMPRONTA = (
    "PREZZO_VALVOLA_EUR", "F_U", "L_H", "K_CONFERMA", "CANALE", "CLASSE",
    "VITA_UTILE_H", "LATTINA_SCARTATA_EUR",
    "DURATA_FERMO_NON_PIANIFICATO_MIN", "DURATA_FERMO_PIANIFICATO_MIN",
    "QUOTA_FERMO_PIANIFICATO", "MIN_TOT", "REGIME_MIN_H", "K_SIGMA",
    "ORE_PRIMA_DI_PARLARE", "SOGLIE_MISURATE", "CANALE_GUASTO",
)


def impronta_parametri(finestra: dict | None) -> str:
    """Lo sha256 di tutto cio' che, cambiando, cambia il verdetto.

    Perche' esiste. Il verdetto e' precalcolato in `decision_rollup_hour`, e
    la freschezza di quella tabella si misurava solo confrontando `MAX(ora_ts)`
    con l'ultimo secchiello dei cicli: vedeva allungarsi la corsa e **non
    vedeva nient'altro**. Se cambiava una costante qui dentro, o cambiava la
    finestra sana che arriva dal KV `baseline_window`, la tabella restava
    formalmente fresca e le rotte servivano i numeri vecchi senza dirlo.

    Non e' un rischio teorico: `PREZZO_VALVOLA_EUR` e' in attesa di un
    preventivo vero da un fornitore, quindi il primo cambiamento previsto su
    questo file e' proprio uno di quelli che non si vedevano in pagina.

    L'impronta e' quindi lo sha256 di un JSON canonico (`sort_keys=True`) che
    contiene i valori **correnti** delle costanti di `PARAMETRI_IMPRONTA` piu'
    gli estremi della finestra sana ricevuta. I valori si leggono dal modulo a
    ogni chiamata, non si congelano all'import: cosi' un `decision.X = ...` a
    caldo si vede come lo vedrebbe il conto.
    """
    import hashlib  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    g = globals()
    corpo: dict[str, Any] = {n: g[n] for n in PARAMETRI_IMPRONTA}
    corpo["_finestra"] = {
        "start": (finestra or {}).get("start"),
        "end": (finestra or {}).get("end"),
    }
    canonico = _json.dumps(corpo, sort_keys=True, ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def fermo_pianificato(f_u: float) -> float:
    """Quota di fermo di un intervento pianificato, euro."""
    return QUOTA_FERMO_PIANIFICATO * f_u * (DURATA_FERMO_PIANIFICATO_MIN / 60.0)


def fermo_non_pianificato(f_u: float) -> float:
    """Costo del fermo non pianificato, euro."""
    return f_u * (DURATA_FERMO_NON_PIANIFICATO_MIN / 60.0)


# --------------------------------------------------------------------------
#  tempo
# --------------------------------------------------------------------------

def ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def ore(a: str, b: str) -> float:
    return (ts(b) - ts(a)).total_seconds() / 3600.0


def piu(iso: str, h: float) -> str:
    return (ts(iso) + timedelta(hours=float(h))).isoformat()


def z(iso: str) -> str:
    """L'istante in forma `2026-07-04T07:00:00Z`, come dice il contratto."""
    return ts(iso).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
#  baseline e rilevamento del segnale
# --------------------------------------------------------------------------

def mu_sigma(valori: list[float]) -> dict[str, Any]:
    """mu/sigma di POPOLAZIONE (ddof=0), come la pagina PREDITTIVA."""
    n = len(valori)
    if not n:
        return {"n": 0, "mu": None, "sigma": None}
    mu = sum(valori) / n
    sigma = (math.sqrt(sum((x - mu) ** 2 for x in valori) / n) if n > 1
             else 0.0)
    return {"n": n, "mu": mu, "sigma": sigma}


def baseline_da_serie(serie_v, finestra, canali=(CANALE, "quality_rate")):
    """mu/sigma per canale sulla finestra sana, secchielli `total >= 100`.

    Il confronto e' sugli istanti `[window.start, window.end)`.
    """
    inizio, fine = ts(finestra["start"]), ts(finestra["end"])
    acc: dict[str, list[float]] = {c: [] for c in canali}
    for p in serie_v:
        t = ts(p["at"])
        if not (inizio <= t < fine):
            continue
        if p["total"] < MIN_TOT:
            continue
        for c in canali:
            if p.get(c) is not None:
                acc[c].append(p[c])
    return {c: mu_sigma(v) for c, v in acc.items()}


def inizio_ultimo_regime(serie_v, ch, sopra, mu, sigma):
    """Inizio dell'ultimo regime oltre soglia, o `None`.

    L'ultimo secchiello misurato che NON supera la soglia chiude cio' che e'
    rientrato. Il regime e' cio' che sta dopo, fino a fine serie, e deve durare
    almeno `REGIME_MIN_H`. Un rientro in banda annulla il regime precedente.
    """
    if mu is None or not sigma:
        return None
    soglia = mu + K_SIGMA * sigma if sopra else mu - K_SIGMA * sigma
    pts = [p for p in serie_v
           if p["total"] >= MIN_TOT and p.get(ch) is not None]
    if not pts:
        return None

    def oltre(v):
        return v > soglia if sopra else v < soglia

    ultimo_dentro = -1
    for i, p in enumerate(pts):
        if not oltre(p[ch]):
            ultimo_dentro = i
    if ultimo_dentro + 1 >= len(pts):
        return None
    capo = pts[ultimo_dentro + 1]
    if not oltre(capo[ch]):
        return None
    return (capo["at"] if ore(capo["at"], pts[-1]["at"]) >= REGIME_MIN_H
            else None)


def segnali_per_ora(serie_v, ch, sopra, mu, sigma):
    """Il segnale come lo conosce chi guarda, a ogni ora, in una passata sola.

    Ritorna `[(at, segnale)]` per ogni secchiello utile, dove `segnale` e'
    quello che `inizio_ultimo_regime` restituirebbe sulla serie tagliata a
    quell'`at`. E' la stessa regola, letta in avanti invece che una volta
    sola in fondo.

    Serve al precalcolo di `decision_rollup.py`, che per costare 5 secondi
    fa una passata sola per valvola e poi legge ogni ora della corsa da
    quella. Senza questa mappa ogni ora userebbe il segnale di fine corsa,
    cioe' una data che a quell'ora non e' ancora accaduta.
    """
    if mu is None or not sigma:
        return []
    soglia = mu + K_SIGMA * sigma if sopra else mu - K_SIGMA * sigma
    pts = [p for p in serie_v
           if p["total"] >= MIN_TOT and p.get(ch) is not None]

    def oltre(v):
        return v > soglia if sopra else v < soglia

    fuori = []
    ultimo_dentro = -1
    for k, p in enumerate(pts):
        if not oltre(p[ch]):
            ultimo_dentro = k
        i = ultimo_dentro + 1
        seg = None
        if (i <= k and oltre(pts[i][ch])
                and ore(pts[i]["at"], p["at"]) >= REGIME_MIN_H):
            seg = pts[i]["at"]
        fuori.append((p["at"], seg))
    return fuori


def _segnale_a(f: dict, adesso: str):
    """Il segnale che si conosceva a `adesso`, dall'ultimo secchiello utile."""
    t = f.get("segnali_t")
    if not t:
        return None
    i = bisect.bisect_right(t, ts(adesso)) - 1
    return f["segnali_v"][i] if i >= 0 else None


def taglia(serie_v, fino_a: str):
    """La serie come la si vedrebbe a `adesso`: nessun secchiello dopo."""
    lim = ts(fino_a)
    return [p for p in serie_v if ts(p["at"]) <= lim]


# --------------------------------------------------------------------------
#  t di Student, senza scipy
# --------------------------------------------------------------------------

def _betacf(a, b, x):
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        if abs(d) < tiny:
            d = tiny
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        if abs(d) < tiny:
            d = tiny
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-14:
            break
    return h


def _betai(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1.0 - x) / b


# Quante coppie distinte tiene la memoria delle due funzioni della t.
#
# Perche' una memoria: `t_cdf` e `t_quantile` sono funzioni PURE di `(t, df)` e
# `(p, df)`. Su una richiesta all'ultima ora del run `storico_60d`, misurato il
# 2026-09-16, ricevono 152.756 e 1.534 chiamate ma solo 46.200 e 751 coppie
# distinte: nove chiamate su dieci ripetono un conto gia' fatto, e `_betacf` e'
# la funzione piu' calda del profilo. Con la memoria l'ultima ora passa da
# 5,40 s a 2,52 s, con verdetto identico cifra per cifra.
#
# Perche' NON e' la trappola di `work/policy-lab/banco.py` righe 67-78. Quel
# memo e' indicizzato da `id()` di un oggetto vivo, e gli `id()` si riusano
# dopo una deallocazione: in un processo che serve piu' richieste puo'
# restituire in silenzio la baseline di un'altra valvola. Qui la chiave e' la
# coppia di numeri stessa, il valore dipende solo da quella coppia e non
# esiste stato condiviso fra valvole o fra richieste. Una cache su una
# funzione pura non puo' scambiare un soggetto con un altro perche' non ha
# soggetti.
#
# Perche' un tetto e non `None`: questo modulo gira dentro un processo API che
# resta acceso per giorni, e `maxsize=None` e' una perdita di memoria lenta.
# 65.536 tiene per intero le 46.200 coppie distinte della richiesta piu'
# pesante misurata, con margine, e si ferma a poche decine di MB. 4.096 sta
# oltre cinque volte sopra le 751 coppie di `t_quantile`.
CACHE_T_CDF = 65536
CACHE_T_QUANTILE = 4096


@lru_cache(maxsize=CACHE_T_CDF)
def t_cdf(t: float, df: float) -> float:
    """CDF della t di Student, via beta incompleta regolarizzata."""
    df = float(df)
    if df <= 0 or t != t:
        return float("nan")
    if t == float("inf"):
        return 1.0
    if t == float("-inf"):
        return 0.0
    x = df / (df + t * t)
    pr = 0.5 * _betai(df / 2.0, 0.5, x)
    return 1.0 - pr if t > 0 else pr


@lru_cache(maxsize=CACHE_T_QUANTILE)
def t_quantile(p: float, df: int) -> float:
    """Quantile della t di Student, per bisezione sulla CDF."""
    lo, hi = 0.0, 100.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def soglia_di(fault_type: str):
    """(valore, nota) — `None` quando il crollo non e' mai stato osservato."""
    oss = SOGLIE_MISURATE.get(fault_type)
    if not oss:
        return None, "nessun crollo osservato per questo tipo di guasto"
    return float(sum(oss) / len(oss)), f"media di {len(oss)} crolli osservati"


# --------------------------------------------------------------------------
#  la stima del crollo (6a)
# --------------------------------------------------------------------------

@dataclass
class Stima:
    esito: str
    adesso: str | None = None
    segnale: str | None = None
    ore_di_osservazione: float | None = None
    n: int = 0
    a: float | None = None
    b: float | None = None
    s: float | None = None
    soglia: float | None = None
    x_hat: float | None = None
    x_lo: float | None = None
    x_hi: float | None = None
    crollo_stimato: str | None = None
    crollo_lo: str | None = None
    crollo_hi: str | None = None
    nota: str = ""
    xbar: float | None = None
    sxx: float | None = None
    df: int | None = None


def _retta(x: list[float], y: list[float]):
    """Minimi quadrati a mano: (b, a, sse, xbar, sxx).

    Il laboratorio usa `np.polyfit(x, y, 1)`. Qui il conto e' scritto per
    esteso: su una retta le due strade danno lo stesso numero e non serve
    portare numpy dentro la rotta.
    """
    n = len(x)
    xbar = sum(x) / n
    ybar = sum(y) / n
    sxx = sum((v - xbar) ** 2 for v in x)
    sxy = sum((x[i] - xbar) * (y[i] - ybar) for i in range(n))
    b = sxy / sxx if sxx else 0.0
    a = ybar - b * xbar
    sse = sum((y[i] - (a + b * x[i])) ** 2 for i in range(n))
    return b, a, sse, xbar, sxx


def stima_crollo(serie_v, baseline, fault_type: str, adesso: str,
                 alpha: float = 0.05) -> Stima:
    """La prognosi a un istante `adesso`, dai soli dati fino a quell'ora.

    `baseline` e' gia' calcolata sulla serie intera e sulla finestra sana: non
    dipende da `adesso`, quindi si passa da fuori invece di rifarla ogni ora.
    """
    canale = CANALE_GUASTO.get(fault_type)
    if canale is None:
        return Stima("nessuna_soglia", adesso=adesso,
                     nota=f"tipo di guasto sconosciuto: {fault_type}")

    vista = taglia(serie_v, adesso)
    if not vista:
        return Stima("dati_insufficienti", adesso=adesso,
                     nota="nessun secchiello fino ad adesso")

    bl = baseline[canale]
    seg = inizio_ultimo_regime(vista, canale, True, bl["mu"], bl["sigma"])
    if seg is None:
        return Stima("nessun_segnale", adesso=adesso,
                     nota="nessun regime oltre mu+3sigma di durata >= 24 h "
                          "nei dati fino ad adesso")

    soglia, nota_s = soglia_di(fault_type)
    t0 = ts(seg)
    pts = [p for p in vista
           if p["total"] >= MIN_TOT and p.get(canale) is not None
           and ts(p["at"]) >= t0]
    n = len(pts)
    base = Stima("dati_insufficienti", adesso=adesso, segnale=seg, n=n,
                 soglia=soglia, ore_di_osservazione=ore(seg, adesso))
    if n < 4:
        base.nota = f"solo {n} secchielli utili dal segnale (servono >= 4)"
        return base

    x = [ore(seg, p["at"]) for p in pts]
    y = [float(p[canale]) for p in pts]
    b, a, sse, xbar, sxx = _retta(x, y)
    df = n - 2
    s = math.sqrt(sse / df) if df > 0 else float("nan")
    base.a, base.b, base.s = float(a), float(b), s
    base.xbar, base.sxx, base.df = float(xbar), float(sxx), int(df)

    if soglia is None:
        base.esito = "nessuna_soglia"
        base.nota = (f"{nota_s} — nessun crollo previsto, soglia non definita "
                     f"(canale {canale}, pendenza {b:+.3f}/h)")
        return base

    if b <= 0:
        base.esito = "pendenza_sbagliata"
        base.nota = (f"pendenza {b:+.4f}/h: il canale non sta salendo verso "
                     f"la soglia, nessun crollo previsto")
        return base

    x_hat = (soglia - a) / b
    t = t_quantile(1.0 - alpha / 2.0, df)
    g = (t * t * s * s) / (b * b * sxx)
    base.x_hat = float(x_hat)
    base.crollo_stimato = piu(seg, x_hat)

    if g >= 1.0:
        base.esito = "banda_illimitata"
        base.nota = (f"g = {g:.3f} >= 1: la pendenza non è distinguibile da "
                     f"zero al {100 * (1 - alpha):.0f}%, l'intervallo di "
                     f"Fieller è illimitato")
        return base

    d = x_hat - xbar
    rad = (1.0 - g) * (1.0 + 1.0 / n) + (d * d) / sxx
    if rad < 0:
        base.esito = "banda_illimitata"
        base.nota = "radicando negativo: intervallo di Fieller vuoto"
        return base
    half = (t * s / b) * math.sqrt(rad)
    base.esito = "stimato"
    base.x_lo = float(xbar + (d - half) / (1.0 - g))
    base.x_hi = float(xbar + (d + half) / (1.0 - g))
    base.crollo_lo = piu(seg, base.x_lo)
    base.crollo_hi = piu(seg, base.x_hi)
    base.nota = nota_s
    return base


# --------------------------------------------------------------------------
#  la politica (6c), funzioni pure
# --------------------------------------------------------------------------

def p_retta(par: dict, x: float) -> float:
    """Probabilita' di crollo entro `x`, dai soli parametri della retta.

    La retta stimata e' y(x) = a + b x, con incertezza di PREVISIONE
    se(x) = s * sqrt(1 + 1/n + (x - xbar)^2 / sxx). «Crollo entro x» significa
    «la retta ha gia' raggiunto la soglia»: p(x) = 1 - F_t(t, df) con
    t = (soglia - a - b x) / se(x). E' la banda di Fieller letta al contrario.
    """
    s, sxx, df = par["s"], par["sxx"], par["df"]
    if s is None or sxx is None or df is None:
        return 0.0
    if not sxx or not s or s <= 0 or not df or df <= 0:
        return 0.0
    d = x - par["xbar"]
    se = s * math.sqrt(1.0 + 1.0 / par["n"] + (d * d) / sxx)
    if se <= 0:
        return 0.0
    t = (par["soglia"] - par["a"] - par["b"] * x) / se
    return 1.0 - t_cdf(t, df)


def p_crollo_entro(st: Stima | None, x: float) -> float:
    """Probabilita' che il crollo sia arrivato entro l'ora `x` dal segnale."""
    if st is None or st.esito != "stimato":
        return 0.0
    return p_retta({"soglia": st.soglia, "a": st.a, "b": st.b,
                    "xbar": st.xbar, "n": st.n, "s": st.s,
                    "sxx": st.sxx, "df": st.df}, x)


def verdetto(adesso: str, st: Stima | None, tasso_scarti: float, q_es: float,
             prezzo: float | None = None, f_u: float | None = None,
             L: float | None = None) -> dict:
    """Il confronto di un'ora sola, con i suoi numeri.

    Gli orizzonti sono in ore dal segnale: se chiamo adesso la squadra arriva
    a x1, se aspetto un'ora arriva a x2 = x1 + 1.

        D = tasso_scarti * 1 h + delta_p * (fermo improvviso - fermo programmato)
        R = prezzo / VITA_UTILE_H * q_es

    Gli accenti delle frasi sono quelli veri. Il laboratorio li scrive
    traslitterati perche' stampa a terminale; qui il testo esce in JSON e la
    pagina non deve ripararlo.
    """
    # I default si leggono dal modulo **alla chiamata**, non all'import. Con
    # `prezzo=PREZZO_VALVOLA_EUR` nella firma il valore veniva congelato al
    # caricamento: cambiare la costante a caldo muoveva `parametri()` e non
    # muoveva il conto, cioe' la pagina dichiarava un prezzo e ne usava un
    # altro.
    prezzo = PREZZO_VALVOLA_EUR if prezzo is None else prezzo
    f_u = F_U if f_u is None else f_u
    L = L_H if L is None else L

    usabile = st is not None and st.esito == "stimato"
    if usabile:
        x1 = ore(st.segnale, st.adesso) + L
        x2 = x1 + 1.0
        p1 = p_crollo_entro(st, x1)
        delta_p = max(0.0, p_crollo_entro(st, x2) - p1)
    else:
        x1 = x2 = None
        p1 = delta_p = 0.0

    salto = fermo_non_pianificato(f_u) - fermo_pianificato(f_u)
    D = tasso_scarti * 1.0 + delta_p * salto
    R = prezzo / VITA_UTILE_H * q_es

    if D > R:
        azione = "intervieni"
        motivo = (f"aspettare un'ora costa {D:.2f} euro e fa risparmiare "
                  f"{R:.2f} euro di vita del pezzo")
    elif tasso_scarti > 0 or usabile:
        azione = "continua degradata"
        motivo = (f"aspettare un'ora costa {D:.2f} euro contro {R:.2f} euro "
                  f"di vita risparmiata, la valvola però è già fuori banda")
    else:
        azione = "continua"
        motivo = (f"nessun segno di degrado, aspettare un'ora costa "
                  f"{D:.2f} euro contro {R:.2f} euro di vita risparmiata")

    return {"adesso": adesso, "azione": azione, "p1": p1, "delta_p": delta_p,
            "D": D, "R": R, "x1": x1, "x2": x2,
            "tasso_scarti": tasso_scarti, "q_es": q_es, "motivo": motivo}


# --------------------------------------------------------------------------
#  la griglia oraria
# --------------------------------------------------------------------------

def scarti_fuori_banda(serie_v, baseline) -> list[tuple[str, float]]:
    """[(istante, euro di scarto in eccesso)] solo per le ore fuori banda.

    La politica non sa quali valvole siano guaste, quindi il tasso di scarti
    che legge deve essere cieco: un secchiello conta solo se ha almeno 100
    cicli e se la sua qualita' sta sotto mu - 3 sigma della finestra sana di
    quella valvola. Il rumore di una valvola sana resta fuori dal conto.
    """
    bl = baseline["quality_rate"]
    q0, sg = bl["mu"], bl["sigma"]
    if q0 is None or not sg:
        return []
    limite = q0 - K_SIGMA * sg
    fuori = []
    for p in serie_v:
        if p["total"] < MIN_TOT or p.get("quality_rate") is None:
            continue
        if p["quality_rate"] >= limite:
            fuori.append((p["at"], 0.0))
            continue
        ecc = p["total"] * max(0.0, q0 - p["quality_rate"])
        fuori.append((p["at"], ecc * LATTINA_SCARTATA_EUR))
    return fuori


def griglia(serie_v, baseline, fino_a: str) -> list[dict]:
    """Una riga per ogni ora di orologio, dalla prima ora + 24 h fino a `fino_a`.

    Ogni riga porta l'istante, il tasso di scarti in euro all'ora sulle 24 ore
    di orologio che finiscono li', e la quota di ore di esercizio della stessa
    finestra.
    """
    sf = scarti_fuori_banda(serie_v, baseline)
    ts_ = [ts(a) for a, _ in sf]
    pre, acc = [0.0], 0.0
    for _, e in sf:
        acc += e
        pre.append(acc)
    ex = [ts(p["at"]) for p in serie_v if p["total"] >= MIN_TOT]

    righe = []
    if not serie_v:
        return righe
    t = ts(serie_v[0]["at"]) + timedelta(hours=ORE_PRIMA_DI_PARLARE)
    fine = min(ts(serie_v[-1]["at"]), ts(fino_a))
    while t <= fine:
        a = t - timedelta(hours=24)
        i = bisect.bisect_right(ts_, a)
        j = bisect.bisect_right(ts_, t)
        i2 = bisect.bisect_right(ex, a)
        j2 = bisect.bisect_right(ex, t)
        righe.append({"adesso": t.isoformat(),
                      "tasso_scarti": (pre[j] - pre[i]) / 24.0,
                      "q_es": (j2 - i2) / 24.0, "_p": None})
        t += timedelta(hours=1)
    return righe


def scansione_stime(serie_v, baseline, segnale, fine_iso) -> list[dict]:
    """Per ogni ora dal segnale + 24 h: cosa direbbe lo stimatore 6a.

    Si ferma a `fine_iso`, cioe' all'ora del verdetto: la stima di un'ora
    dipende solo dai secchielli fino a quell'ora, quindi le ore successive non
    cambierebbero niente di cio' che la rotta deve restituire.

    Ogni riga porta anche la `Stima` intera nel campo `st`, non solo `esito` e
    `_p`. Prima `stima_piena` richiamava `stima_crollo` da capo sull'ora
    bersaglio, cioe' rifaceva un conto che questa scansione aveva gia' fatto un
    momento prima. Sul run `storico_60d` la scansione tocca fino a 1407 ore per
    valvola e `stima_crollo` e' il 95% del tempo della richiesta: tenere
    l'oggetto invece di ricalcolarlo e' cio' che permette al precalcolo di
    riempire la colonna `stima` di ogni ora al prezzo di una passata sola.
    `st` vale `None` quando il conto e' esploso, ed e' il marcatore del caso
    `fuori_orizzonte`.
    """
    if not segnale or CLASSE not in CANALE_GUASTO:
        return []
    fuori = []
    h = ORE_PRIMA_DI_PARLARE
    fine = ts(fine_iso)
    while True:
        adesso = piu(segnale, h)
        if ts(adesso) > fine:
            break
        try:
            st = stima_crollo(serie_v, baseline, CLASSE, adesso)
        except (OverflowError, ValueError, ArithmeticError):
            # pendenza quasi nulla: la retta raggiunge la soglia fra secoli e
            # la data esce dall'intervallo rappresentabile. Non e' una stima.
            fuori.append({"adesso": adesso, "esito": "fuori_orizzonte",
                          "_p": None, "st": None})
            h += 1.0
            continue
        fuori.append({"adesso": adesso, "esito": st.esito, "st": st,
                      "_p": ({"a": st.a, "b": st.b, "s": st.s, "n": st.n,
                              "xbar": st.xbar, "sxx": st.sxx, "df": st.df,
                              "soglia": st.soglia, "segnale": st.segnale}
                             if st.esito == "stimato" else None)})
        if st.esito == "nessuna_soglia":
            break          # non cambiera' mai idea: la soglia non esiste
        h += 1.0
    return fuori


def _st_da_parametri(par, adesso) -> Stima | None:
    """Una `Stima` con i soli campi che `verdetto` legge."""
    if not par:
        return None
    return Stima(esito="stimato", adesso=adesso, segnale=par["segnale"],
                 a=par["a"], b=par["b"], s=par["s"], n=par["n"],
                 xbar=par["xbar"], sxx=par["sxx"], df=par["df"],
                 soglia=par["soglia"])


# --------------------------------------------------------------------------
#  i fatti di una valvola, e il suo stato a `adesso`
# --------------------------------------------------------------------------

def fatti_valvola(serie_v, finestra, adesso: str) -> dict:
    """Segnale, griglia e scansione della valvola, senza vedere lo scenario.

    Il segnale si cerca SOLO sui secchielli fino a `adesso`. Prima cercava
    sulla serie intera della corsa registrata, e la risposta a un'ora poteva
    quindi portare un `segnale` datato dopo quell'ora: a `2026-07-04T02:00Z`
    su `storico_60d` la valvola 21 dichiarava un segnale del 16 luglio e la 30
    del 12 agosto. Su una macchina in marcia quelle ore non esistono ancora.

    Il taglio vale per il solo segnale. La baseline resta calcolata sulla
    serie intera perche' la sua finestra e' fissa e sta all'inizio della
    corsa, e `griglia` e `scansione_stime` gia' si fermano a `adesso` da se'.

    `segnali_per_ora` porta il segnale di OGNI ora, non solo quello di
    `adesso`, perche' `decision_rollup.py` chiama questa funzione una volta
    per valvola e poi legge da li' tutte le 1407 ore della corsa.

    Costo del taglio, misurato su `storico_60d`: il segnale trovato e' lo
    stesso, e compare esattamente `REGIME_MIN_H` ore dopo essere avvenuto,
    perche' la regola del regime pretende 24 h di misure che confermano.
    """
    baseline = baseline_da_serie(serie_v, finestra)
    bl = baseline[CANALE]
    segnali = segnali_per_ora(serie_v, CANALE, True, bl["mu"], bl["sigma"])
    seg_t = [ts(a) for a, _ in segnali]
    seg_v = [x for _, x in segnali]
    i = bisect.bisect_right(seg_t, ts(adesso)) - 1
    segnale = seg_v[i] if i >= 0 else None
    # la scansione parte dal PRIMO momento in cui un segnale esisteva, non da
    # quello di fine corsa: e' cio' che rende giusta ogni ora del precalcolo.
    primo = next((x for x in seg_v if x is not None), None)
    scansione = scansione_stime(serie_v, baseline, primo, adesso)
    g = griglia(serie_v, baseline, adesso)
    par = {ts(r["adesso"]): r.get("_p") for r in scansione}
    for r in g:
        r["_p"] = par.get(ts(r["adesso"]))
    return {"segnale": segnale, "segnali_t": seg_t, "segnali_v": seg_v,
            "griglia": g, "serie": serie_v,
            "baseline": baseline,
            "esiti": {ts(r["adesso"]): r["esito"] for r in scansione},
            "stime": {ts(r["adesso"]): r["st"] for r in scansione},
            "esercizio": {ts(p["at"]) for p in serie_v
                          if p["total"] >= MIN_TOT}}


def camminata(f: dict):
    """La camminata sulla griglia, ora per ora. E' scritta qui una volta sola.

    Chi la consuma decide dove fermarsi: `percorso` si ferma al bersaglio e
    guarda l'ultima riga, `linea_valvola` arriva in fondo e conta tutte le ore.
    Se questa regola venisse riscritta altrove, un giorno la striscia e il
    verdetto direbbero cose diverse sulla stessa ora.

    Le tre regole, in ordine:
    - `di_fila` conta gli «intervieni» consecutivi e si azzera a ogni azione
      diversa.
    - a `K_CONFERMA` scatta la chiamata e la squadra arriva `L_H` ore dopo.
    - dall'arrivo in poi la valvola e' nuova e l'azione e' `continua` per
      sempre: un solo intervento per valvola e per corsa.
    """
    di_fila = 0
    chiamata_a = None
    arrivo = None
    for r in f["griglia"]:
        t = ts(r["adesso"])
        st = _st_da_parametri(r.get("_p"), r["adesso"])
        v = verdetto(r["adesso"], st, r["tasso_scarti"], r["q_es"])
        sostituita = arrivo is not None and t >= ts(arrivo)
        nuova_chiamata = False
        if sostituita:
            v = {**v, "azione": "continua", "D": 0.0, "R": v["R"],
                 "p1": 0.0, "delta_p": 0.0, "tasso_scarti": 0.0,
                 "motivo": "valvola sostituita: la squadra è arrivata"}
        elif v["azione"] == "intervieni":
            di_fila += 1
            if di_fila >= K_CONFERMA and chiamata_a is None:
                chiamata_a = r["adesso"]
                arrivo = piu(r["adesso"], L_H)
                nuova_chiamata = True
        else:
            di_fila = 0
        yield {"t": t, "riga": r, "v": v, "di_fila": di_fila,
               "chiamata_a": chiamata_a, "arrivo": arrivo,
               "sostituita": sostituita, "nuova_chiamata": nuova_chiamata}


def percorso(f: dict, bersaglio: datetime) -> dict | None:
    """Lo stato della valvola all'ora `bersaglio`, ripercorrendo la griglia.

    La camminata e' quella di `camminata`. Qui si raccoglie solo la storia
    delle ultime 24 ore e si legge la riga del bersaglio.
    """
    ultima_esercizio = None
    righe = []
    trovato = None
    for passo in camminata(f):
        t, v = passo["t"], passo["v"]
        eser = t in f["esercizio"]
        if eser:
            ultima_esercizio = v["azione"]
        righe.append({"at": z(v["adesso"]), "azione": v["azione"],
                      "D": v["D"], "R": v["R"], "eser": eser,
                      "mostra": v["azione"] if eser
                      else (ultima_esercizio or v["azione"])})
        if t == bersaglio:
            trovato = {"v": v, "di_fila": passo["di_fila"],
                       "chiamata_a": passo["chiamata_a"],
                       "arrivo": passo["arrivo"],
                       "sostituita": passo["sostituita"]}
            break
    if trovato is None:
        return None
    fette = righe[max(0, len(righe) - 24):]
    trovato["storia"] = [
        {"at": x["at"], "azione": x["mostra"],
         "D": round(x["D"], 2), "R": round(x["R"], 2),
         **({} if x["eser"] else {"esercizio": False})}
        for x in fette]
    return trovato


def stima_piena(f: dict, adesso: str, sostituita: bool) -> dict:
    """Il blocco `stima` del contratto per un istante.

    Non ricalcola niente: la `Stima` dell'ora e' quella che `scansione_stime`
    ha gia' prodotto e conservato in `f["stime"]`. Ora assente dalla mappa
    significa «la scansione non arriva fin qui», cioe' meno di 24 h di regime
    dal segnale; valore `None` significa «il conto e' esploso», cioe'
    `fuori_orizzonte`. Sono gli stessi due casi che prima si scoprivano
    rifacendo `stima_crollo` e intercettandone l'eccezione.
    """
    seg = _segnale_a(f, adesso)
    if sostituita:
        return {"esito": "sostituita", "segnale": z(seg),
                "nota": "la squadra è arrivata, la valvola è nuova"}
    if not seg:
        return {"esito": "nessun_segnale", "segnale": None,
                "nota": "nessun regime di 24 h sopra banda sul canale"}
    chiave = ts(adesso)
    if chiave not in f["stime"]:
        return {"esito": "dati_insufficienti", "segnale": z(seg),
                "nota": "meno di 24 h di regime dal segnale"}
    st = f["stime"][chiave]
    if st is None:
        return {"esito": "fuori_orizzonte", "segnale": z(seg),
                "nota": "pendenza quasi nulla: la soglia non è raggiungibile"}
    d = {"esito": st.esito,
         "segnale": z(st.segnale) if st.segnale else None,
         "ore_di_osservazione": (round(st.ore_di_osservazione, 1)
                                 if st.ore_di_osservazione is not None
                                 else None),
         "n": st.n, "nota": st.nota}
    if st.esito == "stimato":
        d.update({
            "crollo_lo": z(st.crollo_lo), "crollo_hat": z(st.crollo_stimato),
            "crollo_hi": z(st.crollo_hi),
            "ore_a_crollo_lo": round(ore(adesso, st.crollo_lo), 1),
            "ore_a_crollo_hat": round(ore(adesso, st.crollo_stimato), 1),
            "ore_a_crollo_hi": round(ore(adesso, st.crollo_hi), 1),
        })
    return d


# --------------------------------------------------------------------------
#  l'oggetto di risposta
# --------------------------------------------------------------------------

def _valvola_senza_dati(v: int) -> dict:
    return {"valve_id": v, "azione": "continua",
            "conferma": {"di_fila": 0, "K": K_CONFERMA, "chiamata": False},
            "D": 0.0, "R": 0.0, "p1": 0.0, "delta_p": 0.0,
            "tasso_scarti_eur_h": 0.0, "q_es": 0.0,
            "motivo": "nessun dato a quest'ora",
            "canale": CANALE, "classe": CLASSE,
            "stima": {"esito": "senza_dati",
                      "nota": "la corsa non copre quest'ora"},
            "storia_24h": []}


def parametri() -> dict:
    """I prezzi e le durate con cui e' stato fatto il conto."""
    return {
        "prezzo_valvola_eur": PREZZO_VALVOLA_EUR,
        "vita_utile_h": VITA_UTILE_H,
        "fermo_non_pianificato_eur_h": F_U,
        "fermo_pianificato_eur_h": round(fermo_pianificato(F_U), 1),
        "salto_fermi_eur": round(fermo_non_pianificato(F_U)
                                 - fermo_pianificato(F_U), 1),
        "L_h": int(L_H), "K": K_CONFERMA,
        "lattina_scartata_eur": LATTINA_SCARTATA_EUR,
    }


def decisione(serie: dict[int, list[dict]], finestra: dict, run_id: str | None,
              adesso: str) -> dict:
    """L'oggetto di `work/pezzo7/CONTRATTO.md` per l'ora `adesso`.

    `serie` e' `{valve_id: [secchielli orari]}` nella forma della pagina:
    `at`, `total`, `mean_filling_time_ms`, `quality_rate`.
    """
    bersaglio = ts(adesso)
    conteggi = {"intervieni": 0, "continua_degradata": 0,
                "continua": 0, "senza_dati": 0}
    valvole = []
    for v in sorted(serie):
        serie_v = serie[v]
        f = fatti_valvola(serie_v, finestra, adesso)
        s = percorso(f, bersaglio)
        if s is None:
            conteggi["senza_dati"] += 1
            valvole.append(_valvola_senza_dati(v))
            continue
        ve = s["v"]
        chiamata = s["chiamata_a"] is not None and not s["sostituita"]
        conteggi["continua_degradata"
                 if ve["azione"] == "continua degradata"
                 else ve["azione"]] += 1
        riga = {
            "valve_id": v,
            "azione": ve["azione"],
            "conferma": {"di_fila": s["di_fila"], "K": K_CONFERMA,
                         "chiamata": bool(chiamata)},
            "D": round(ve["D"], 2), "R": round(ve["R"], 2),
            "p1": round(ve["p1"], 6), "delta_p": round(ve["delta_p"], 6),
            "tasso_scarti_eur_h": round(ve["tasso_scarti"], 2),
            "q_es": round(ve["q_es"], 3),
            "motivo": ve["motivo"],
            "canale": CANALE, "classe": CLASSE,
            "stima": stima_piena(f, adesso, s["sostituita"]),
            "storia_24h": s["storia"],
        }
        if chiamata:
            riga["squadra"] = {"arrivo": z(s["arrivo"]), "ore": int(L_H)}
        valvole.append(riga)
    return {"run_id": run_id, "adesso": z(adesso),
            "parametri": parametri(), "conteggi": conteggi,
            "valvole": valvole}


def linea(serie: dict[int, list[dict]], finestra: dict, run_id: str | None,
          fine: str) -> dict:
    """I conteggi ora per ora di tutta la corsa, piu' le chiamate.

    Una passata sola sulla griglia di ogni valvola, con la camminata di
    `camminata`: la casella di un'ora qui dice lo stesso numero che
    `decisione` direbbe a quell'ora, perche' la regola e' la stessa funzione.

    `i[k]` sono le valvole in «intervieni» all'ora k, `d[k]` quelle in
    «continua degradata». Le ore non si mandano una per una: sono contigue e
    distano un'ora, quindi bastano `prima_ora` e l'indice.
    """
    per_ora: dict[datetime, list[int]] = {}
    chiamate: list[dict] = []
    for v in sorted(serie):
        f = fatti_valvola(serie[v], finestra, fine)
        for passo in camminata(f):
            if passo["nuova_chiamata"]:
                chiamate.append({"valvola": v,
                                 "chiamata": z(passo["chiamata_a"]),
                                 "arrivo": z(passo["arrivo"])})
            c = per_ora.setdefault(passo["t"], [0, 0])
            az = passo["v"]["azione"]
            if az == "intervieni":
                c[0] += 1
            elif az == "continua degradata":
                c[1] += 1

    if not per_ora:
        return {"run_id": run_id, "prima_ora": None, "ultima_ora": None,
                "ore": 0, "i": [], "d": [], "chiamate": []}

    prima, ultima = min(per_ora), max(per_ora)
    n = int((ultima - prima).total_seconds() // 3600) + 1
    ore_tutte = [prima + timedelta(hours=k) for k in range(n)]
    vuoto = [0, 0]
    chiamate.sort(key=lambda c: (c["chiamata"], c["valvola"]))
    return {
        "run_id": run_id,
        "prima_ora": z(prima.isoformat()),
        "ultima_ora": z(ultima.isoformat()),
        "ore": n,
        "i": [per_ora.get(t, vuoto)[0] for t in ore_tutte],
        "d": [per_ora.get(t, vuoto)[1] for t in ore_tutte],
        "chiamate": chiamate,
    }
