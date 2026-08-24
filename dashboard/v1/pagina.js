/* Pagina VALVOLE — LA GIOSTRA.
 *
 * Principio: le 35 valvole disposte come stanno sulla macchina, in cerchio,
 * nelle loro posizioni di numerazione sul carosello. "Quale valvola apro"
 * diventa una domanda spaziale: il tecnico legge una posizione, non un rango.
 *
 * Unica sorgente dati: le route servite da server.py (specchio di
 * pipeline/api.py). Nessun numero a schermo che non venga da li'.
 *
 * COSTO DICHIARATO: la pagina carica le 35 serie /valves/<id>/kpi
 * (400 cicli ciascuna, ~8,6 MB per scenario) in parallelo.
 *
 * ------------------------------------------------------------------
 * IL RAGGIO — LA CANDELA, unico modo. Scelto dall'utente fra due forme
 * messe a confronto in pagina: «mi sa che modo 2 mi dice di piu'... l'altro
 * cambia poco, quindi direi candela». Il modo a fondo corsa e' stato
 * rimosso, non lasciato dietro un ramo spento.
 *
 * La candela porta UNA grandezza sola, nominata a schermo: il TEMPO DI
 * RIEMPIMENTO. Non e' un indice composto, e la chiave in basso a sinistra
 * dice da se' cosa sono le sue tre parti (banda, corpo, tacca).
 *
 * REGOLA DEL COLORE: colore solo dove c'e' gravita'. Il resto e' neutro.
 * ------------------------------------------------------------------ */

import { api, pct, num, etaDato, scenarioCorrente, collegaNav, nomeGuasto } from '/comune/dati.js';

/* ---- la navigazione conserva lo scenario ---- */
collegaNav();

const baselineValvole = () =>
  fetch(`/api/${scenarioCorrente()}/valves/baseline`, { cache: 'no-store' })
    .then(r => { if (!r.ok) throw new Error(`valves/baseline -> HTTP ${r.status}`);
                 return r.json(); });

/* ------------------------------------------------------------------ *
 * LA CANDELA — dentro o fuori.
 *
 * Per ogni valvola: dove stanno i suoi 400 tempi di riempimento rispetto
 * alla banda normale della PROPRIA base (media ±3σ della baseline — non i
 * limiti XmR della route, che segnalano 165-316 cicli su 400 fuori limite su
 * valvole sane). Il segno va dal 10° al 90° percentile, con una tacca sulla
 * mediana. Non c'e' una lunghezza da interpretare: c'e' dentro o fuori.
 *
 * Misurato (10°-90° percentile, in σ della base della valvola):
 *   a-sana / e / f : da −1,4 a +1,5 su tutte e 35 → tutte dentro
 *   b-guasto       : la 13 sta interamente a +3,0 → un segno sottile al bordo
 *   c-multi        : 1, 2, 3, 10, 11, 13 escono; le altre no
 *   d-deriva       : 33 valvole si allargano fino a +3,0/+3,2 insieme
 *   valvole 9 e 21 : +1,2 al 90° percentile in ogni scenario → sempre dentro
 *
 * SOGLIE DELLA TINTA, lette sullo scenario sano, non scelte: li' lo
 * scostamento massimo della mediana e' 0,2 σ e la quota di cicli fuori banda
 * e' 0. Sotto la zona morta c'e' rumore, e il rumore non e' un segnale.
 * ------------------------------------------------------------------ */
const SOGLIA_SPOSTA = { morta: 0.40, tol: 0.60 };   // in σ della base
const SOGLIA_FUORI  = { morta: 0.03, tol: 0.08 };   // frazione dei cicli
const SIGMA_MAX = 4;                                // fondoscala radiale, ±4σ

/* ------------------------------------------------------------------ *
 * LE GRANDEZZE DEL SUGGERIMENTO.
 *
 * La candela dice una cosa sola, ed e' per questo che si legge. Ma proprio
 * per questo fa nascere una domanda: sulla valvola 9 di d-deriva-diffusa ci
 * sono due allarmi attivi e la candela sta dentro la banda. Il disaccordo
 * non va tolto — dice che il problema non e' nel tempo, e quindi restringe
 * il campo. Al passaggio del mouse compaiono tutte le grandezze che la
 * baseline consente di confrontare, ciascuna contro la PROPRIA banda ±3σ.
 *
 * Ordine fisso, la candela per prima. Non e' una classifica di sospetti:
 * l'ordine non cambia mai con i dati, e non c'e' nessun punteggio.
 *
 * Misurato — valvola 9, d-deriva-diffusa (mediana in σ · cicli fuori/400):
 *   filling_time_ms +0,08 · 10    tail_time_ms −0,17 · 0
 *   pulse_count     +0,33 · 88    delta_pulse  −0,31 · 88
 *   tail_pulse      −0,19 · 3
 * cioe': tempo normale, 22 cicli su 100 con la quantita' fuori banda.
 *
 * Sulla stessa valvola in a-sana: delta_pulse 7 cicli su 400 (1,8%), sotto
 * la zona morta -> nessuna tinta. Il confronto e' contro la base DELLA
 * valvola, che gia' contiene l'anomalia di costruzione delle 9 e 21
 * (diagnostic_suspect_rate 0,679): su macchina sana non sembrano guaste.
 * ------------------------------------------------------------------ */
const CAMPI = [
  ['filling_time_ms', 'tempo di riempimento', true],
  ['tail_time_ms',    'tempo di coda',        false],
  ['pulse_count',     'impulsi contati',      false],
  ['delta_pulse',     'scarto impulsi',       false],
  ['tail_pulse',      'impulsi di coda',      false],
];

/* Riferimento dei riempimenti conformi d'insieme: media dei tassi di
   baseline delle 35 valvole, cioe' la qualita' sana di questa macchina. */
const SOGLIA_CONF = { morta: 0.010, tol: 0.040 };

/* Scarto dei riempimenti non conformi contro la base della singola valvola.
   Misurato su a-sana: tutte e 35 stanno fra −4 e +4 punti. Il rumore e'
   quindi ±5 punti, e sotto quella banda non c'e' segnale. */
const RUMORE = 0.05;

/* Soglia dell'eta' del dato, la stessa di MACCHINA: misurata, non inventata.
   A macchina in marcia il dato piu' recente ha fra 2 s e 2 min 39 s. */
const DATO_ATTESO_S = 300;
const IN_MARCIA = new Set(['Running', 'Starting']);

const V = (n) => `var(--${n})`;
const SVG = 'http://www.w3.org/2000/svg';
const CLASSE = ['dato', 'sev1', 'sev2', 'sev3', 'sev4'];
const el = (t, a = {}, txt) => {
  const n = document.createElementNS(SVG, t);
  for (const k in a) if (a[k] !== null && a[k] !== undefined) n.setAttribute(k, a[k]);
  if (txt !== undefined) n.textContent = txt;
  return n;
};
const svg = (id) => { const s = document.getElementById(id); s.replaceChildren(); return s; };
const div = (cls, style) => {
  const d = document.createElement('div'); d.className = cls;
  if (style) d.setAttribute('style', style);
  return d;
};
const ora = (iso) => new Date(iso).toLocaleTimeString('it-IT',
  { hour: '2-digit', minute: '2-digit' });
// Un allarme aperto il 3 luglio, scritto col solo orario, si legge come
// stamattina. Sugli allarmi serve il giorno. Stessa forma di a/pagina.js.
const giornoOra = (iso) => new Date(iso).toLocaleString('it-IT',
  { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
const punti = (v, d = 1) => (v >= 0 ? '+' : '−') + num(Math.abs(v) * 100, d);
const quantile = (ord, q) => ord[Math.min(ord.length - 1,
  Math.max(0, Math.round(q * (ord.length - 1))))];

/* grado di gravita' 0..4 con la scala del lessico: zona morta, poi un
   gradino ogni "tol". */
function grado(scarto, s) {
  if (scarto == null || !(scarto > s.morta)) return 0;
  const g = scarto - s.morta;
  if (g <= s.tol) return 1;
  if (g <= s.tol * 2) return 2;
  if (g <= s.tol * 3) return 3;
  return 4;
}
/* Dove stanno i valori di UNA grandezza rispetto alla banda ±3σ della base
   di QUELLA valvola. Restituisce null se la base o la serie non danno modo
   di fare il confronto: chi chiama deve dichiarare l'assenza, non mostrare
   zero. Non si usano i limiti XmR della route (segnalano 165-316 cicli su
   400 fuori limite su valvole sane). */
function quoteZ(serie, base, campo) {
  const b = base && base[campo];
  if (!b || !(b.std > 0) || typeof b.mean !== 'number') return null;
  const z = serie.map(c => c[campo])
                 .filter(x => typeof x === 'number')
                 .map(x => (x - b.mean) / b.std);
  if (!z.length) return null;
  const ord = z.slice().sort((a, c) => a - c);
  return {
    z10: quantile(ord, 0.10), z50: quantile(ord, 0.50), z90: quantile(ord, 0.90),
    fuori: z.filter(x => Math.abs(x) > 3).length, n: z.length,
  };
}
/* Grado 0..4 di una grandezza: il piu' grave fra lo spostamento della
   mediana e la quota di cicli fuori dalla propria banda. */
const gradoCampo = (q) => q === null ? 0
  : Math.max(grado(Math.abs(q.z50), SOGLIA_SPOSTA),
             grado(q.fuori / q.n, SOGLIA_FUORI));
/* σ con due decimali finche' il numero e' leggibile; oltre ±10σ i decimali
   non dicono piu' niente e allungherebbero soltanto la riga. */
const sigma = (z) => {
  const d = Math.abs(z) < 10 ? 2 : 0;   // oltre ±10σ i decimali non dicono piu' niente
  return (z >= 0 ? '+' : '−') + Math.abs(z).toLocaleString('it-IT',
    { minimumFractionDigits: d, maximumFractionDigits: d }) + 'σ';
};

/* Tinta dello scarto dei riempimenti non conformi dalla propria base. */
function tintaScarto(e) {
  if (e === null || e === undefined) return 'dato';
  if (e <= RUMORE) return 'dato';
  if (e <= 0.10) return 'sev1';
  if (e <= 0.20) return 'sev2';
  if (e <= 0.35) return 'sev3';
  return 'sev4';
}

/* ---------------- geometria polare ----------------
   Valvola 1 a ore 12, numerazione in senso orario: e' l'ordine in cui le
   valvole sono numerate sul carosello. */
const NV = 35, PASSO = 360 / NV;
const CX = 360, CY = 366;
const R_MOZZO = 142;                    // mozzo centrale
const R_IN = 150, R_OUT = 276;          // area del grafico radiale
const R_BERS = 281;                     // fine del bersaglio di clic/fuoco
const R_RIM0 = 286, R_RIM1 = 297;       // corona degli allarmi attivi
const R_NUM = 318;                      // numero della valvola

const angolo = (i) => (i - 1) * PASSO;  // gradi, orari da ore 12
const pol = (r, gradi) => {
  const t = gradi * Math.PI / 180;
  return [CX + r * Math.sin(t), CY - r * Math.cos(t)];
};
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
/* −SIGMA_MAX..+SIGMA_MAX, con lo zero a meta' dell'intervallo radiale. */
const rrSigma = (z) => R_IN + (clamp(z, -SIGMA_MAX, SIGMA_MAX) + SIGMA_MAX)
                             / (2 * SIGMA_MAX) * (R_OUT - R_IN);

function cerchio(r) {
  return `M ${CX} ${CY - r} A ${r} ${r} 0 1 1 ${CX} ${CY + r} `
       + `A ${r} ${r} 0 1 1 ${CX} ${CY - r}`;
}
function settore(r0, r1, g0, g1) {
  const [ax, ay] = pol(r1, g0), [bx, by] = pol(r1, g1);
  const [cx2, cy2] = pol(r0, g1), [dx, dy] = pol(r0, g0);
  const big = (g1 - g0) > 180 ? 1 : 0;
  return `M ${ax} ${ay} A ${r1} ${r1} 0 ${big} 1 ${bx} ${by} `
       + `L ${cx2} ${cy2} A ${r0} ${r0} 0 ${big} 0 ${dx} ${dy} Z`;
}

/* ---------------- suggerimento ---------------- */
const TIP = () => document.getElementById('tip');
function mostraTip(html, cx, cy, cls) {
  const t = TIP();
  t.innerHTML = html;
  // la classe PRIMA di misurare: cambia la larghezza. Si aggiunge a .tip,
  // non la sostituisce — className = ... cancellava la grammatica del lessico.
  t.classList.toggle('tip-valvola', cls === 'tip-valvola');
  t.hidden = false;
  const r = t.getBoundingClientRect();
  let x = cx + 14, yv = cy - r.height - 12;
  if (x + r.width > innerWidth - 8) x = cx - r.width - 14;
  if (yv < 8) yv = cy + 16;
  t.style.left = Math.max(8, x) + 'px';
  t.style.top = yv + 'px';
}
const nascondiTip = () => { TIP().hidden = true; };

/* ---------------- il suggerimento della valvola ----------------
 * Risponde alla domanda che la candela fa nascere: se il tempo di
 * riempimento sta dentro la banda ma la valvola ha allarmi attivi, dove sta
 * allora lo scostamento? Si mostrano le grandezze con il loro spostamento e
 * quanti cicli escono dalla propria banda — non un verdetto, non un perche',
 * non una classifica: l'ordine e' fisso e non dipende dai dati.
 * Una grandezza senza base o senza serie si DICHIARA assente, non vale 0. */
function tipValvola(v) {
  let h = `<span class="v">Valvola ${v.id}</span> `
        + `<span class="m">posizione ${v.id}/35</span><br>`;
  // Il nome del guasto viaggia sulla riga della valvola (`nomeAtt`), non
  // sull'allarme: `fault_type` vale sempre `score_aggregation`.
  h += v.nAtt
    ? `<span class="f">${v.nomeAtt}</span>`
    : '<span class="m">nessun allarme attivo</span>';

  h += '<div class="tg"><span class="th">grandezza</span>'
     + '<span class="th tn">mediana</span>'
     + '<span class="th tn">cicli fuori banda</span>';
  for (const c of v.campi) {
    const et = c.et + (c.candela ? ' <span class="cd">candela</span>' : '');
    if (c.q === null) {
      h += `<span class="tl">${et}</span>`
         + '<span class="tn m" style="grid-column:span 2">non disponibile</span>';
      continue;
    }
    const cl = gradoCampo(c.q) > 0 ? 'f' : 'm';
    h += `<span class="tl">${et}</span>`
       + `<span class="tn ${cl}">${sigma(c.q.z50)}</span>`
       + `<span class="tn ${cl}">${c.q.fuori} su ${c.q.n}</span>`;
  }
  h += '</div>';
  h += '<span class="m tf">scostamento della mediana e cicli fuori dalla banda'
     + ' ±3σ della base di questa valvola</span>';
  return h;
}

/* ================================================================== */
let DATI = null;

async function main() {
  const ids = Array.from({ length: NV }, (_, i) => i + 1);
  const [stato, oeeG, valvole, allarmi, base, ...kpiTutte] = await Promise.all([
    api.stato(), api.oee('day'), api.valvole(), api.allarmi(), baselineValvole(),
    ...ids.map(i => api.valvolaKpi(i).catch(() => null)),
  ]);

  const kpi = new Map();
  ids.forEach((id, k) => {
    const r = kpiTutte[k];
    kpi.set(id, (r && Array.isArray(r.series) && r.series.length) ? r.series : null);
  });

  /* --- una riga per valvola, tutta derivata dalle route --- */
  const alerts = allarmi.alerts || [];
  const perValvola = new Map();
  for (const id of ids) {
    const s = kpi.get(id);
    const b = (base.valves || {})[String(id)] || null;
    const att = alerts.filter(a => a.valve_id === id);
    let osservato = null, scarto = null, n = 0;
    let z10 = null, z50 = null, z90 = null, fuoriBanda = null, spostamento = null;
    if (s) {
      n = s.length;
      const nonOk = s.filter(c => c.fill_quality_ok === false).length;
      osservato = nonOk / n;
      if (b && typeof b.fill_quality_ok_rate === 'number')
        scarto = osservato - (1 - b.fill_quality_ok_rate);
      // dove stanno i tempi di riempimento rispetto alla banda della propria base
      const q = quoteZ(s, b, 'filling_time_ms');
      if (q) {
        z10 = q.z10; z50 = q.z50; z90 = q.z90;
        fuoriBanda = q.fuori / q.n;
        spostamento = Math.abs(z50);
      }
    }
    /* tutte le grandezze confrontabili, ciascuna contro la PROPRIA banda */
    const campi = CAMPI.map(([k, et, candela]) => {
      const q = s ? quoteZ(s, b, k) : null;
      return { k, et, candela, q };
    });
    const gradoDentro = Math.max(grado(spostamento, SOGLIA_SPOSTA),
                                 grado(fuoriBanda, SOGLIA_FUORI));
    perValvola.set(id, {
      id, base: b, serie: s, n, osservato, scarto, campi,
      z10, z50, z90, fuoriBanda, spostamento, gradoDentro,
      att, nAtt: att.length,
      // Il nome del guasto NON sta nell'allarme: `fault_type` vale sempre
      // `score_aggregation`. Sta nella predizione servita da /valves.
      nomeAtt: nomeGuasto((valvole.valves || {})[String(id)]),
      baseNonOk: b && typeof b.fill_quality_ok_rate === 'number'
        ? 1 - b.fill_quality_ok_rate : null,
    });
  }

  /* --- eta' del dato: elemento di prima classe --- */
  const eta = etaDato(valvole, oeeG.at);
  const tsUltimo = ultimoTs(valvole);
  const vecchio = !!(eta && eta.secondi > DATO_ATTESO_S);
  const inMarcia = IN_MARCIA.has(stato.label);

  DATI = { valvole, base, allarmi, kpi, perValvola, eta, vecchio, stato };

  disegnaGiostra(perValvola, { eta, tsUltimo, vecchio, inMarcia, stato,
                               nBase: base.n_cicli_per_valvola,
                               nAllarmi: alerts.length });
  disegnaConformi(perValvola, base, vecchio);
  disegnaChiusure(perValvola, vecchio);
  disegnaNonConformi(perValvola, vecchio);

}

function ultimoTs(valvole) {
  const ts = Object.values(valvole.valves || {})
    .map(v => v.last_prediction && v.last_prediction.prediction_ts)
    .filter(Boolean).sort();
  return ts.length ? ts[ts.length - 1] : null;
}

/* ================= LA GIOSTRA =================
   Un solo grafico polare, convenzionale: 35 posizioni, un anello di
   riferimento, un marchio per valvola. La disposizione circolare porta
   un'informazione che una fila non porta — se le valvole fuori dal proprio
   normale sono vicine sulla giostra, si vedono come un arco. */
function disegnaGiostra(pv, ctx) {
  const s = svg('giostra');
  const opaco = ctx.vecchio ? 0.42 : 1;   // dato vecchio: si vede che non e' adesso

  /* ---- riferimento radiale: cambia con il modo, e' l'unica differenza ---- */
  const gRif = el('g', { 'fill-opacity': opaco, 'stroke-opacity': opaco });
  // l'anello grigio E' il normale della singola valvola: dentro o fuori
  gRif.appendChild(el('path', {
    d: `${cerchio(rrSigma(3))} ${cerchio(rrSigma(-3))}`,
    'fill-rule': 'evenodd', style: `fill:${V('traccia')}` }));
  gRif.appendChild(el('path', { d: cerchio(rrSigma(0)), fill: 'none',
    'stroke-width': 1.5, 'stroke-dasharray': ctx.vecchio ? '2 5' : '5 4',
    style: `stroke:${V('rif')}` }));
  s.appendChild(gRif);

  /* ---- etichette dell'asse, sul raggio a ore 6 (confine fra due celle).
         Si costruiscono qui ma si appendono DOPO le celle: i marchi sono
         larghi 11 px e attraversano il confine, e coprivano le etichette. --- */
  const gAsse = (() => {
    const g = el('g');
    const tacche = [[rrSigma(0), 'normale'], [rrSigma(3.4), 'fuori']];
    for (const [r, t] of tacche) {
      const [x, y] = pol(r, 180);
      const w = t.length * 5.6 + 9;
      g.appendChild(el('rect', { x: x - w / 2, y: y - 7, width: w, height: 13, rx: 1,
        style: `fill:${V('sup')}` }));
      g.appendChild(el('text', { x, y: y + 3.5, 'text-anchor': 'middle',
        'font-size': 10, style: `fill:${V('rif')}` }, t));
    }
    return g;
  })();

  /* ---- le 35 celle ---- */
  const gap = 1.1;
  for (const v of pv.values()) {
    const g0 = angolo(v.id) - PASSO / 2 + gap, g1 = angolo(v.id) + PASSO / 2 - gap;
    const gc = angolo(v.id);

    const tin = CLASSE[v.gradoDentro];
    const acceso = v.gradoDentro > 0;
    const rim = v.nAtt === 0 ? 'traccia' : (v.nAtt === 1 ? 'attenz' : 'grave');

    const etAtt = v.nAtt === 0 ? 'nessun allarme attivo'
      : `${v.nAtt} ${v.nAtt === 1 ? 'allarme attivo' : 'allarmi attivi'}`
        + ` (${v.nomeAtt})`;
    const etRag = v.z50 === null
      ? 'tempo di riempimento non confrontabile con la propria base'
      : (v.gradoDentro > 0
         ? 'il tempo di riempimento esce dal normale di questa valvola'
         : 'il tempo di riempimento sta dentro il normale di questa valvola');

    const g = el('g', {
      class: 'cella-valvola', tabindex: '0', role: 'button',
      'aria-label': `Valvola ${v.id}, posizione ${v.id} di 35 sulla giostra. `
        + `${etAtt}. ${etRag}. Apri il dettaglio.`,
    });
    g.appendChild(el('title', {}, `Valvola ${v.id} · ${etAtt}`));

    /* Bersaglio generoso, ma che si ferma PRIMA del numero della valvola:
       il contorno del fuoco/hover arrivava sotto le cifre e le toccava,
       soprattutto sui numeri a due cifre. */
    g.appendChild(el('path', { class: 'sfondo', d: settore(R_MOZZO + 2, R_BERS, g0, g1) }));

    const gm = el('g', { 'fill-opacity': opaco, 'stroke-opacity': opaco });

    // corona degli allarmi attivi: la stessa lettura della striscia di MACCHINA
    gm.appendChild(el('path', { d: settore(R_RIM0, R_RIM1, g0, g1),
      style: `fill:${V(rim)}` }));

    {
      /* Dentro o fuori: dal 10° al 90° percentile dei suoi riempimenti,
         con una tacca sulla mediana. Nessuna lunghezza da interpretare. */
      if (v.z10 === null) {
        const [x0, y0] = pol(rrSigma(-0.6), gc), [x1, y1] = pol(rrSigma(0.6), gc);
        gm.appendChild(el('line', { x1: x0, y1: y0, x2: x1, y2: y1,
          'stroke-width': 11, 'stroke-dasharray': '2 3',
          style: `stroke:${V('banda')}` }));
      } else {
        const [x0, y0] = pol(rrSigma(v.z10), gc), [x1, y1] = pol(rrSigma(v.z90), gc);
        gm.appendChild(el('line', { x1: x0, y1: y0, x2: x1, y2: y1,
          'stroke-width': 11, 'stroke-linecap': 'butt',
          style: `stroke:${V(tin)}` }));
        // la mediana: una tacca trasversale, come la candela
        const rm = rrSigma(v.z50);
        const [mx0, my0] = pol(rm, gc - PASSO / 2 + gap + 0.4);
        const [mx1, my1] = pol(rm, gc + PASSO / 2 - gap - 0.4);
        gm.appendChild(el('line', { x1: mx0, y1: my0, x2: mx1, y2: my1,
          'stroke-width': 3, 'stroke-linecap': 'round',
          style: `stroke:${V(v.gradoDentro >= 2 ? tin : 'ink')}` }));
      }
    }

    g.appendChild(gm);

    // il numero della valvola, sempre in chiaro
    const [nx, ny] = pol(R_NUM, gc);
    g.appendChild(el('text', { class: 'num', x: nx, y: ny + 4.5,
      'text-anchor': 'middle', 'font-size': 13,
      'font-weight': (v.nAtt > 0 || acceso) ? 700 : 400,
      'fill-opacity': ctx.vecchio ? 0.6 : 1,
      style: `fill:${V((v.nAtt > 0 || acceso) ? 'ink' : 'muto')}` },
      String(v.id)));

    g.addEventListener('click', () => apriPannello(v.id, g));
    g.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); apriPannello(v.id, g); }
    });
    const testo = () => tipValvola(v);
    g.addEventListener('mousemove', (ev) => mostraTip(testo(), ev.clientX, ev.clientY, 'tip-valvola'));
    g.addEventListener('mouseleave', nascondiTip);
    g.addEventListener('focus', () => {
      const [px, py] = pol((R_MOZZO + R_BERS) / 2, gc);
      const r = s.getBoundingClientRect(), vb = s.viewBox.baseVal;
      const k = Math.min(r.width / vb.width, r.height / vb.height);
      mostraTip(testo(), r.left + (r.width - vb.width * k) / 2 + px * k,
                         r.top + (r.height - vb.height * k) / 2 + py * k,
                'tip-valvola');
    });
    g.addEventListener('blur', nascondiTip);

    s.appendChild(g);
  }
  s.appendChild(gAsse);

  /* ---- mozzo: quante valvole, e di quando e' il dato ---- */
  const inAllarme = [...pv.values()].filter(v => v.nAtt > 0).length;
  const segnalate = [...pv.values()].filter(v => v.gradoDentro > 0).length;
  const h = el('g');
  h.appendChild(el('circle', { cx: CX, cy: CY, r: R_MOZZO,
    style: `fill:${V('sup')}` }));

  h.appendChild(el('text', { x: CX, y: CY - 72, 'text-anchor': 'middle',
    'font-size': 10.5, 'letter-spacing': '.11em',
    style: `fill:${V('muto')}` }, 'VALVOLE IN ALLARME'));
  h.appendChild(el('text', { x: CX, y: CY - 20, 'text-anchor': 'middle',
    'font-size': 52, 'font-weight': 600,
    style: `fill:${V('ink')}` }, `${inAllarme}`));
  h.appendChild(el('text', { x: CX, y: CY - 2, 'text-anchor': 'middle',
    'font-size': 12.5, style: `fill:${V('muto')}` },
    `su ${NV}${ctx.nAllarmi ? ` · ${ctx.nAllarmi} allarmi attivi` : ''}`));

  h.appendChild(el('line', { x1: CX - 78, x2: CX + 78, y1: CY + 11, y2: CY + 11,
    'stroke-width': 1, style: `stroke:${V('traccia')}` }));

  h.appendChild(el('text', { x: CX, y: CY + 28, 'text-anchor': 'middle',
    'font-size': 10.5, 'letter-spacing': '.09em',
    style: `fill:${V('muto')}` },
    'TEMPO DI RIEMPIMENTO FUORI NORMA'));
  h.appendChild(el('text', { x: CX, y: CY + 52, 'text-anchor': 'middle',
    'font-size': 26, 'font-weight': 600,
    style: `fill:${V('ink')}` }, `${segnalate}`));

  // eta' del dato: si accende solo se la macchina gira e i dati non arrivano
  const rotto = ctx.inMarcia && ctx.vecchio;
  h.appendChild(el('text', { x: CX, y: CY + 76, 'text-anchor': 'middle',
    'font-size': 11,
    style: `fill:${V(rotto ? 'attenz' : 'muto')}` },
    ctx.tsUltimo
      ? `ultimo dato ${ora(ctx.tsUltimo)}${ctx.eta ? ` · ${ctx.eta.testo} fa` : ''}`
      : 'ultimo dato non disponibile'));

  /* Lo stato della macchina. In marcia e' gia' scritto sulla prima pagina:
     qui resta, per chi arriva diretto, ma ridotto — la sola parola, piccola.
     A macchina NON in marcia torna per esteso: li' non e' una ripetizione,
     e' informazione. Uno stato non e' una gravita': resta neutro. */
  h.appendChild(el('text', { x: CX, y: CY + (ctx.inMarcia ? 92 : 94),
    'text-anchor': 'middle',
    'font-size': ctx.inMarcia ? 10 : 12.5,
    'letter-spacing': ctx.inMarcia ? '.06em' : '0',
    style: `fill:${V('muto')}` },
    ctx.inMarcia ? ctx.stato.label
                 : `macchina ${ctx.stato.label} · OMAC ${ctx.stato.state}`));
  s.appendChild(h);

  /* ---- provenienza del riferimento, leggibile a schermo ---- */
  const cap = [
    'raggio: tempo di riempimento \u2014 dove stanno gli ultimi 400 riempimenti'
    + ' di ogni valvola',
    'anello grigio: il normale della singola valvola \u00b7 base su '
    + num(ctx.nBase) + ' cicli sani \u00b13\u03c3',
  ];
  s.appendChild(el('text', { x: 10, y: 18, 'font-size': 11.5,
    style: `fill:${V('rif')}` }, cap[0]));
  s.appendChild(el('text', { x: 10, y: 34, 'font-size': 11.5,
    style: `fill:${V('rif')}` }, cap[1]));

  /* ---- la chiave: una candela di riferimento che si spiega da se'.
     Sta in basso a sinistra, FUORI dal cerchio dei numeri (r 318): l'angolo
     in alto a destra del riquadro, x 190 y 624, sta a r 337 dal centro. ---- */
  {
    const kx = 26, ky0 = 640, ky1 = 694;                 // asse della mini-candela
    const zk = (z) => ky0 + (3 - z) / 6 * (ky1 - ky0);   // +3\u03c3 in alto
    const k = el('g');
    k.appendChild(el('text', { x: 8, y: 630, 'font-size': 10.5,
      'letter-spacing': '.07em', style: `fill:${V('muto')}` },
      'LA CANDELA'));
    // la banda: l'intervallo normale di QUELLA valvola
    k.appendChild(el('rect', { x: kx - 11, y: zk(3), width: 22,
      height: zk(-3) - zk(3), style: `fill:${V('traccia')}` }));
    // il corpo: dal 10\u00b0 al 90\u00b0 percentile dei suoi 400 cicli
    k.appendChild(el('line', { x1: kx, y1: zk(1.7), x2: kx, y2: zk(-1.3),
      'stroke-width': 11, 'stroke-linecap': 'butt',
      style: `stroke:${V('dato')}` }));
    // la tacca: la mediana
    k.appendChild(el('line', { x1: kx - 7, y1: zk(0.4), x2: kx + 7, y2: zk(0.4),
      'stroke-width': 3, 'stroke-linecap': 'round', style: `stroke:${V('ink')}` }));
    for (const [y, t] of [[zk(2.5), 'banda = normale della valvola'],
                          [zk(0.4), 'corpo = 10\u00b0\u201390\u00b0 percentile'],
                          [zk(-2.0), 'tacca = mediana dei 400 cicli']]) {
      k.appendChild(el('line', { x1: kx + 13, y1: y, x2: 44, y2: y,
        'stroke-width': 1, style: `stroke:${V('rif')}` }));
      k.appendChild(el('text', { x: 48, y: y + 3.5, 'font-size': 9.5,
        style: `fill:${V('rif')}` }, t));
    }
    s.appendChild(k);
  }

  // legenda della corona: le chiavi sono legenda, non dati
  const leg = [[0, 'traccia', 'nessun allarme'], [128, 'attenz', '1 allarme'],
               [232, 'grave', '2 o più']];
  for (const [lx, c, t] of leg) {
    s.appendChild(el('rect', { x: 10 + lx, y: 702, width: 10, height: 9, rx: 1,
      style: `fill:${V(c)}` }));
    s.appendChild(el('text', { x: 24 + lx, y: 710, 'font-size': 10.5,
      style: `fill:${V('rif')}` }, t));
  }
  s.appendChild(el('text', { x: 710, y: 710, 'text-anchor': 'end', 'font-size': 10.5,
    style: `fill:${V('rif')}` },
    'corona esterna: allarmi attivi · 35 posizioni in ordine di numerazione'));

  if (ctx.vecchio)
    s.appendChild(el('text', { x: 710, y: 18, 'text-anchor': 'end', 'font-size': 11.5,
      style: `fill:${V('rif')}` },
      `dato di ${ctx.eta ? ctx.eta.testo : '—'} fa`));
}

/* ================= riempimenti conformi, sulle 35 valvole =================
   Portata dalla versione 3: una barra d'insieme, con il riferimento
   disegnato dentro la barra stessa. */
function disegnaConformi(pv, base, vecchio) {
  const c = document.getElementById('conf');
  c.replaceChildren();
  c.classList.toggle('vecchio', !!vecchio);

  let ok = 0, tot = 0;
  for (const v of pv.values()) {
    if (!v.serie) continue;
    for (const x of v.serie) { tot++; if (x.fill_quality_ok) ok++; }
  }
  const tassi = [...pv.values()].map(v => v.base && v.base.fill_quality_ok_rate)
                                .filter(x => typeof x === 'number');
  const rif = tassi.length ? tassi.reduce((a, b) => a + b, 0) / tassi.length : null;

  if (!tot) {
    const p = div('cifra'); p.textContent = 'non calcolabile';
    p.style.fontSize = '15px'; p.style.color = 'var(--muto)';
    c.appendChild(p);
    return;
  }
  const v = ok / tot;
  const g = rif === null ? 0 : grado(Math.max(0, rif - v), SOGLIA_CONF);

  const cifra = div('cifra' + (g >= 3 ? ' gra' : g >= 2 ? ' att' : ''));
  cifra.textContent = pct(v);
  const u = document.createElement('span'); u.className = 'u';
  u.textContent = `${num(ok)} su ${num(tot)} cicli`;
  cifra.appendChild(u);
  c.appendChild(cifra);

  const b = div('barra');
  b.appendChild(div('q', `width:${v * 100}%;background:var(--${CLASSE[g] || 'dato'})`));
  if (rif !== null) b.appendChild(div('tacca', `left:${rif * 100}%`));
  c.appendChild(b);

  const r = div('rif');
  r.textContent = rif === null ? 'riferimento non disponibile'
    : `riferimento ${pct(rif)} · baseline delle 35 valvole`;
  c.appendChild(r);
}

/* ================= motivi di chiusura del ciclo =================
   Portata dalla versione 3. E' una ripartizione fra categorie, non una
   gravita': resta tutta neutra, differenziata per opacita', come la
   ripartizione del tempo di MACCHINA. */
function disegnaChiusure(pv, vecchio) {
  const c = document.getElementById('chius');
  c.replaceChildren();
  c.classList.toggle('vecchio', !!vecchio);

  const conta = new Map();
  let tot = 0;
  for (const v of pv.values()) {
    if (!v.serie) continue;
    for (const x of v.serie) {
      const k = x.close_reason || 'non dichiarato';
      conta.set(k, (conta.get(k) || 0) + 1); tot++;
    }
  }
  if (!tot) {
    const p = div('rif'); p.textContent = 'serie non disponibile';
    c.appendChild(p);
    return;
  }
  const voci = [...conta.entries()].sort((a, b) => b[1] - a[1]);
  const opac = { target: 1, encoder_limit: 0.42 };

  const cifra = div('cifra');
  cifra.textContent = pct((conta.get('target') || 0) / tot);
  const u = document.createElement('span'); u.className = 'u';
  u.textContent = 'chiusi a target';
  cifra.appendChild(u);
  c.appendChild(cifra);

  const b = div('barra');
  let x = 0;
  for (const [k, n] of voci) {
    const w = (n / tot) * 100;
    b.appendChild(div('q', `left:${x}%;width:${w}%;background:var(--dato);`
                         + `opacity:${opac[k] ?? 0.3}`));
    x += w;
  }
  c.appendChild(b);

  const r = div('rif');
  r.textContent = voci.map(([k, n]) => `${k} ${pct(n / tot)}`).join(' · ')
    + ` · ${num(tot)} cicli`;
  c.appendChild(r);
}

/* ================= riempimenti non conformi, per valvola =================
   Il dettaglio delle 35 sotto la barra d'insieme che sta sopra: stessa
   grandezza, stessa lettura. Ogni barra ha accanto la propria tacca di
   riferimento — la base di QUELLA valvola — quindi le valvole 9 e 21, che
   nella loro base sbagliano gia' il 40% dei riempimenti, hanno una barra
   alta che arriva esattamente alla propria tacca: normali, e si vede. */
function disegnaNonConformi(pv, vecchio) {
  const s = svg('nonconf');
  const W = 400, L = 30, R = 8, T = 32, h = 226;
  const iw = W - L - R, passo = iw / NV;
  const op = vecchio ? 0.42 : 1;
  const vals = [...pv.values()];

  s.appendChild(el('text', { x: 0, y: 12, 'font-size': 10.5,
    style: `fill:${V('muto')}` },
    'quota dei riempimenti non conformi, valvola per valvola'));

  for (const q of [0, 0.25, 0.5, 0.75, 1]) {
    const y = T + h - q * h;
    s.appendChild(el('line', { x1: L, x2: L + iw, y1: y, y2: y, 'stroke-width': 1,
      style: `stroke:${V('traccia')}` }));
    s.appendChild(el('text', { x: L - 6, y: y + 3.5, 'text-anchor': 'end',
      'font-size': 9.5, style: `fill:${V('muto')}` }, pct(q)));
  }

  const g = el('g', { 'fill-opacity': op, 'stroke-opacity': op });
  const cx = (v) => L + (v.id - 1) * passo, bw = Math.max(1, passo - 1.8);
  for (const v of vals) {
    const x = cx(v);
    if (v.osservato === null) {
      g.appendChild(el('rect', { x, y: T + h - 12, width: bw, height: 12,
        fill: 'none', 'stroke-width': 1, 'stroke-dasharray': '2 2',
        style: `stroke:${V('banda')}` }));
    } else {
      const bh = Math.max(1.5, v.osservato * h);
      g.appendChild(el('rect', { x, y: T + h - bh, width: bw, height: bh, rx: 1,
        style: `fill:${V(tintaScarto(v.scarto))}` }));
    }
    // la tacca: la base di QUESTA valvola, disegnata dentro la sua colonna
    if (v.baseNonOk !== null) {
      const yb = T + h - v.baseNonOk * h;
      g.appendChild(el('line', { x1: x - 1, x2: x + bw + 1, y1: yb, y2: yb,
        'stroke-width': 1.5, 'stroke-dasharray': '3 2',
        style: `stroke:${V('rif')}` }));
    }
    g.appendChild(el('text', { x: x + bw / 2, y: T + h + 13, 'text-anchor': 'middle',
      'font-size': 8.5, style: `fill:${V('muto')}` }, String(v.id)));
  }
  s.appendChild(g);

  s.appendChild(el('line', { x1: L, x2: L + iw, y1: T + h, y2: T + h,
    'stroke-width': 1, style: `stroke:${V('bordo')}` }));
  s.appendChild(el('text', { x: L + iw / 2, y: T + h + 28, 'text-anchor': 'middle',
    'font-size': 9.5, 'letter-spacing': '.05em',
    style: `fill:${V('muto')}` }, 'VALVOLA'));
  s.appendChild(el('text', { x: 0, y: T + h + 52, 'font-size': 10.5,
    style: `fill:${V('rif')}` },
    'tacca tratteggiata: la base di ciascuna valvola'));

  /* hover con bersaglio sulla X piu' vicina, come ogni altro grafico del
     progetto: non si deve centrare la barra. Piu' accesso da tastiera. */
  const mira = el('line', { y1: T, y2: T + h, 'stroke-width': 1,
    style: `stroke:${V('rif')}`, visibility: 'hidden' });
  const seg = el('circle', { r: 3.5, 'stroke-width': 2,
    style: `fill:${V('sup')};stroke:${V('ink')}`, visibility: 'hidden' });
  s.append(mira, seg);
  s.setAttribute('tabindex', '0');
  s.setAttribute('role', 'img');
  s.setAttribute('class', 'interattivo');
  s.setAttribute('aria-label',
    `Riempimenti non conformi delle 35 valvole, ciascuna contro la propria base.`);

  let idx = -1;
  const mostra = (i, ev) => {
    if (i < 0 || i >= NV) return;
    idx = i;
    const v = vals[i], x = cx(v) + bw / 2;
    const yTopV = v.osservato === null ? T + h : T + h - v.osservato * h;
    mira.setAttribute('x1', x); mira.setAttribute('x2', x);
    mira.setAttribute('visibility', 'visible');
    seg.setAttribute('cx', x); seg.setAttribute('cy', yTopV);
    seg.setAttribute('visibility', v.osservato === null ? 'hidden' : 'visible');
    const r = s.getBoundingClientRect(), vb = s.viewBox.baseVal;
    const k = Math.min(r.width / vb.width, r.height / vb.height);
    const ox = r.left + (r.width - vb.width * k) / 2;
    const oy = r.top + (r.height - vb.height * k) / 2;
    mostraTip(
      `<span class="v">Valvola ${v.id}</span><br>`
      + (v.osservato === null
          ? '<span class="m">serie dei cicli non disponibile</span>'
          : `<span class="${tintaScarto(v.scarto) === 'dato' ? 'm' : 'f'}">`
            + `${pct(v.osservato)} non conformi</span>`
            + ` <span class="m">su ${v.n} cicli</span><br>`
            + `<span class="m">base della valvola ${pct(v.baseNonOk)}`
            + ` · scarto ${punti(v.scarto)} punti</span>`),
      ev ? ev.clientX : ox + x * k, oy + yTopV * k);
  };
  const spegni = () => {
    mira.setAttribute('visibility', 'hidden');
    seg.setAttribute('visibility', 'hidden');
    nascondiTip();
  };
  s.addEventListener('mousemove', (ev) => {
    const r = s.getBoundingClientRect(), vb = s.viewBox.baseVal;
    const k = Math.min(r.width / vb.width, r.height / vb.height);
    const ox = r.left + (r.width - vb.width * k) / 2;
    const vx = (ev.clientX - ox) / k;
    mostra(Math.max(0, Math.min(NV - 1, Math.floor((vx - L) / passo))), ev);
  });
  s.addEventListener('mouseleave', spegni);
  s.addEventListener('blur', spegni);
  s.addEventListener('focus', () => mostra(idx < 0 ? NV - 1 : idx, null));
  s.addEventListener('keydown', (ev) => {
    const k = { ArrowRight: 1, ArrowLeft: -1 }[ev.key];
    if (k) { ev.preventDefault(); mostra(Math.max(0, Math.min(NV - 1, idx + k)), null); }
    else if (ev.key === 'Home') { ev.preventDefault(); mostra(0, null); }
    else if (ev.key === 'End') { ev.preventDefault(); mostra(NV - 1, null); }
    else if (ev.key === 'Escape') spegni();
  });
}

/* ================= pannello valvola =================
   Stesso comportamento e stesso aspetto di MACCHINA: l'utente lo ha
   approvato, e due dettagli-valvola diversi nella stessa dashboard sono un
   difetto. L'unica differenza e' che la serie dei 400 cicli e' gia' in
   memoria, quindi non viene riscaricata. */
let tornaA = null;

async function apriPannello(id, origine) {
  tornaA = origine;
  nascondiTip();
  const pan = document.getElementById('pannello');
  const velo = document.getElementById('velo');
  const corpo = document.getElementById('pan-corpo');
  document.getElementById('pan-tit').textContent = `Valvola ${id}`;
  corpo.replaceChildren();
  pan.hidden = false; velo.hidden = false; pan.focus();

  const v = (DATI.valvole.valves || {})[String(id)] || {};
  const b = (DATI.base.valves || {})[String(id)] || null;

  // --- allarmi attivi ---
  const sez1 = sezione('Allarmi attivi');
  const att = (DATI.allarmi.alerts || []).filter(a => a.valve_id === id);
  if (!att.length) sez1.appendChild(vuoto('nessun allarme attivo'));
  else for (const a of att) {
    const c = document.createElement('span');
    c.className = 'chip ' + (att.length > 1 ? 'gra' : 'att');
    const k = document.createElement('span'); k.className = 'k';
    k.textContent = a.status;
    const n = document.createElement('span');
    // Vedi il commento in comune/dati.js: `fault_type` e' la lineage tecnica
    // dell'apertura, non il nome del guasto. E la data vuole il giorno.
    n.textContent = `${nomeGuasto(v)} · da ${giornoOra(a.opened_ts)}`;
    c.append(n, k); sez1.appendChild(c);
  }
  corpo.appendChild(sez1);

  // --- ultimo ciclo ---
  const k = v.last_kpi;
  const sez2 = sezione('Ultimo ciclo');
  if (!k) sez2.appendChild(vuoto('nessun ciclo disponibile per questa valvola'));
  else {
    for (const [et, val, grave] of [
      ['diagnostica', k.diagnostic_status, k.diagnostic_status !== 'NORMAL'],
      ['qualità', k.fill_quality_ok ? 'ok' : 'non ok', !k.fill_quality_ok],
      ['chiusura', k.close_reason, k.close_reason !== 'target'],
      ['slot', String(k.filling_step_out), k.close_reason === 'encoder_limit'],
      ['ciclo', String(k.cycle_id), false],
    ]) sez2.appendChild(chip(et, val, grave));
  }
  corpo.appendChild(sez2);

  // --- confronto con la base DELLA SINGOLA VALVOLA ---
  const sez3 = sezione('Ultimi cicli contro la base di questa valvola');
  corpo.appendChild(sez3);
  if (!b) { sez3.appendChild(vuoto('baseline non disponibile per questa valvola')); return; }
  const serie = DATI.kpi.get(id);
  if (!serie || !serie.length) {
    sez3.appendChild(vuoto(
      'serie dei cicli non disponibile per questa valvola in questo scenario'));
    return;
  }
  const ord = serie.slice().sort((a, c) => a.cycle_id - c.cycle_id);
  for (const campo of ['filling_time_ms', 'tail_time_ms', 'delta_pulse'])
    if (b[campo]) sez3.appendChild(carta(campo, ord, b[campo], id));

  const nota = document.createElement('p');
  nota.className = 'pan-nota';
  nota.textContent = `${ord.length} cicli · base della valvola ${id} su `
    + `${num(DATI.base.n_cicli_per_valvola)} cicli sani · banda ±3σ`;
  sez3.appendChild(nota);
}

function sezione(titolo) {
  const d = document.createElement('div'); d.className = 'pan-sez';
  const h = document.createElement('h3'); h.textContent = titolo;
  d.appendChild(h); return d;
}
function vuoto(t) {
  const p = document.createElement('p'); p.className = 'pan-vuoto';
  p.textContent = t; return p;
}
function chip(et, val, grave) {
  const c = document.createElement('span');
  c.className = 'chip' + (grave ? ' att' : '');
  const k = document.createElement('span'); k.className = 'k'; k.textContent = et;
  const n = document.createElement('span'); n.textContent = val;
  c.append(k, n); return c;
}

/* Carta di controllo, identica a quella di MACCHINA: banda mean ± 3σ della
   baseline (non i limiti XmR della route, che segnalano 165-316 cicli su 400
   fuori limite su valvole SANE), serie sempre sopra tutto, marcatori solo
   sugli attraversamenti. */
function carta(campo, serie, base, vid) {
  const wrap = document.createElement('div'); wrap.className = 'pan-graf';
  const W = 520, H = 138, L = 8, R = 52, T = 20, B = 18;
  const iw = W - L - R, ih = H - T - B;
  const s = document.createElementNS(SVG, 'svg');
  s.setAttribute('viewBox', `0 0 ${W} ${H}`);
  s.setAttribute('class', 'interattivo');
  s.setAttribute('tabindex', '0');
  s.setAttribute('role', 'img');

  const pts = serie.map(p => ({ v: p[campo], ts: p.event_ts, id: p.cycle_id }))
                   .filter(p => typeof p.v === 'number');
  if (!pts.length) { wrap.appendChild(vuoto(`${campo}: nessun valore`)); return wrap; }
  const vals = pts.map(p => p.v);

  const alto = base.mean + 3 * base.std, basso = base.mean - 3 * base.std;
  const dLo = Math.min(...vals), dHi = Math.max(...vals);
  let lo = Math.min(basso, dLo), hi = Math.max(alto, dHi);
  const minEsc = (hi - lo) * 0.16;
  if (dHi - dLo < minEsc) {
    const c = (dHi + dLo) / 2, m = minEsc / 2;
    lo = Math.min(lo, c - m); hi = Math.max(hi, c + m);
  }
  const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
  const y = (v) => T + ih - ((v - lo) / (hi - lo)) * ih;
  const x = (i) => L + (i / Math.max(1, vals.length - 1)) * iw;

  s.appendChild(el('text', { x: 0, y: 10, 'font-size': 10.5, 'letter-spacing': '.06em',
    style: `fill:${V('muto')}` }, campo.replace(/_/g, ' ')));

  s.appendChild(el('rect', { x: L, y: y(alto), width: iw,
    height: Math.max(2, y(basso) - y(alto)), style: `fill:${V('traccia')}` }));
  s.appendChild(el('line', { x1: L, x2: L + iw, y1: y(base.mean), y2: y(base.mean),
    'stroke-width': 1.5, 'stroke-dasharray': '5 4', style: `stroke:${V('rif')}` }));

  const stretta = Math.abs(y(basso) - y(alto)) < 26;
  const tacche = stretta ? [[base.mean, `${num(base.mean, 0)} ±3σ`]]
                         : [[alto, '+3σ'], [base.mean, num(base.mean, 0)], [basso, '−3σ']];
  for (const [v, t] of tacche) {
    if (y(v) < T - 2 || y(v) > T + ih + 2) continue;
    s.appendChild(el('line', { x1: L + iw, x2: L + iw + 4, y1: y(v), y2: y(v),
      'stroke-width': 1, style: `stroke:${V('rif')}` }));
    s.appendChild(el('text', { x: L + iw + 7, y: y(v) + 3.5, 'font-size': 9.5,
      style: `fill:${V('rif')}` }, t));
  }

  let d = '';
  vals.forEach((v, i) => { d += (i ? ' L ' : 'M ') + x(i) + ' ' + y(v); });
  s.appendChild(el('path', { d, fill: 'none', 'stroke-width': 1.6,
    'stroke-linejoin': 'round', style: `stroke:${V('ink')}` }));

  const dentro = (v) => v <= alto && v >= basso;
  let fuori = 0;
  vals.forEach((v, i) => {
    if (!dentro(v)) fuori++;
    if (i && dentro(vals[i - 1]) !== dentro(v))
      s.appendChild(el('circle', { cx: x(i), cy: y(v), r: 3, 'stroke-width': 1.5,
        style: `fill:${V('sev4')};stroke:${V('sup')}` }));
  });
  s.appendChild(el('text', { x: L + iw, y: 10, 'text-anchor': 'end', 'font-size': 9.5,
    style: `fill:${V(fuori ? 'sev4' : 'muto')}` },
    `${fuori} cicli su ${vals.length} fuori banda`));

  const mira = el('line', { x1: 0, x2: 0, y1: T, y2: T + ih, 'stroke-width': 1,
    style: `stroke:${V('rif')}`, visibility: 'hidden' });
  const seg = el('circle', { r: 4, 'stroke-width': 2,
    style: `fill:${V('sup')};stroke:${V('ink')}`, visibility: 'hidden' });
  s.append(mira, seg);
  agganciaHover(s, {
    n: vals.length, x, T, ih, L, iw, mira, seg,
    puntoY: (i) => y(vals[i]),
    testo: (i) => {
      const p = pts[i], z = (p.v - base.mean) / base.std;
      const f = !dentro(p.v);
      return `<span class="v">${num(p.v, 0)}</span> `
        + `<span class="m">ciclo ${p.id}</span><br>`
        + `<span class="m">base ${num(base.mean, 0)} · scarto `
        + `${z >= 0 ? '+' : ''}${num(z, 2)}σ</span>`
        + (f ? `<br><span class="f">fuori banda di `
               + `${num(Math.abs(p.v - (p.v > alto ? alto : basso)), 0)}</span>` : '');
    },
  });

  s.setAttribute('aria-label', `${campo.replace(/_/g, ' ')} della valvola ${vid}: `
    + `${vals.length} cicli, ${fuori} fuori dall'intervallo normale `
    + `${num(basso, 0)}–${num(alto, 0)}`);
  wrap.appendChild(s); return wrap;
}

/* hover con bersaglio sulla X piu' vicina, identico a MACCHINA */
function agganciaHover(s, o) {
  let idx = -1;
  const mostra = (i, ev) => {
    if (i < 0 || i >= o.n) return;
    idx = i;
    const xi = o.x(i), yi = o.puntoY(i);
    o.mira.setAttribute('x1', xi); o.mira.setAttribute('x2', xi);
    o.mira.setAttribute('visibility', 'visible');
    o.seg.setAttribute('cx', xi); o.seg.setAttribute('cy', yi);
    o.seg.setAttribute('visibility', 'visible');
    const r = s.getBoundingClientRect(), vb = s.viewBox.baseVal;
    const k = Math.min(r.width / vb.width, r.height / vb.height);
    const ox = r.left + (r.width - vb.width * k) / 2;
    const oy = r.top + (r.height - vb.height * k) / 2;
    mostraTip(o.testo(i), ev ? ev.clientX : ox + xi * k, oy + yi * k);
  };
  const spegni = () => {
    o.mira.setAttribute('visibility', 'hidden');
    o.seg.setAttribute('visibility', 'hidden');
    nascondiTip();
  };
  const daEvento = (ev) => {
    const r = s.getBoundingClientRect(), vb = s.viewBox.baseVal;
    const k = Math.min(r.width / vb.width, r.height / vb.height);
    const ox = r.left + (r.width - vb.width * k) / 2;
    const vx = (ev.clientX - ox) / k;
    const f = (vx - o.L) / o.iw;
    return Math.max(0, Math.min(o.n - 1, Math.round(f * (o.n - 1))));
  };
  s.addEventListener('mousemove', (ev) => mostra(daEvento(ev), ev));
  s.addEventListener('mouseleave', spegni);
  s.addEventListener('blur', spegni);
  s.addEventListener('focus', () => mostra(idx < 0 ? o.n - 1 : idx, null));
  s.addEventListener('keydown', (ev) => {
    const k = { ArrowRight: 1, ArrowLeft: -1 }[ev.key];
    if (k) { ev.preventDefault(); mostra(Math.max(0, Math.min(o.n - 1, idx + k)), null); }
    else if (ev.key === 'Home') { ev.preventDefault(); mostra(0, null); }
    else if (ev.key === 'End') { ev.preventDefault(); mostra(o.n - 1, null); }
    else if (ev.key === 'Escape') spegni();
  });
}

function chiudiPannello() {
  nascondiTip();
  document.getElementById('pannello').hidden = true;
  document.getElementById('velo').hidden = true;
  if (tornaA && tornaA.focus) tornaA.focus();
}
document.getElementById('pan-chiudi').addEventListener('click', chiudiPannello);
document.getElementById('velo').addEventListener('click', chiudiPannello);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !document.getElementById('pannello').hidden) chiudiPannello();
});

/* ================= tema ================= */
const btn = document.getElementById('tema');
function scuroAttivo() {
  const t = document.documentElement.getAttribute('data-tema');
  if (t) return t === 'scuro';
  return matchMedia('(prefers-color-scheme: dark)').matches;
}
function aggiornaBtn() { btn.textContent = scuroAttivo() ? 'chiaro' : 'scuro'; }
btn.addEventListener('click', () => {
  const nuovo = scuroAttivo() ? 'chiaro' : 'scuro';
  document.documentElement.setAttribute('data-tema', nuovo);
  try { localStorage.setItem('tema-v7v1', nuovo); } catch (e) {}
  aggiornaBtn();
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', aggiornaBtn);
aggiornaBtn();

main().catch((e) => {
  console.error(e);
  const b = document.getElementById('box-giostra');
  if (b) b.querySelector('.box-corpo').textContent = 'errore di caricamento: ' + e.message;
});
