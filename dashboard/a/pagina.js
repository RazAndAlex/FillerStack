// Pagina MACCHINA — versione A, revisione 2.
// Unica sorgente dati: le route servite da server.py (specchio di pipeline/api.py),
// compresa la nona route GET /valves/baseline, ora esposta.
//
// REGOLA DEL COLORE: colore solo dove c'e' gravita'. Un valore che sta bene
// resta neutro (--dato, un grigio). Le sole tinte sono --attenz e --grave.

import { api, pct, num, etaDato, scenarioCorrente, nomeGuasto } from '/comune/dati.js';

// La nona route e' ora esposta dal server ma non e' ancora in comune/dati.js,
// che non posso modificare: la chiamo qui, sullo stesso scenario e con la
// stessa forma delle altre.
const baselineValvole = () =>
  fetch(`/api/${scenarioCorrente()}/valves/baseline`, { cache: 'no-store' })
    .then(r => { if (!r.ok) throw new Error(`valves/baseline -> HTTP ${r.status}`);
                 return r.json(); });

/* ------------------------------------------------------------------ *
 * RIFERIMENTI
 * Il 100% non e' il riferimento di questa macchina: non e' mai stato
 * osservato. Ogni grandezza disegna il proprio riferimento.
 *
 *  - qualita'   -> CALCOLATA a runtime da GET /valves/baseline:
 *                  media di fill_quality_ok_rate sulle 35 valvole.
 *                  Verificato: 0,7868, cioe' la qualita' sana della
 *                  macchina (0,787) e il 21,3% di scarto di base.
 *  - prestazione-> 1,000 per DEFINIZIONE: e' il rapporto fra cicli reali e
 *                  cicli teorici al target di velocita' dichiarato dalla
 *                  route (performance_detail.speed_target). Non e' una
 *                  costante congelata: e' l'unita' del rapporto.
 *  - disponibilita' e OEE -> NON derivabili dalla baseline per valvola,
 *                  che non contiene tempi di stato ne' velocita'.
 *                  Restano misurati sul run sano e sono etichettati a
 *                  schermo come "run sano". E' il residuo dichiarato.
 * ------------------------------------------------------------------ */
const RIF = {
  oee:          { v: 0.504, tol: 0.05, et: 'run sano' },
  availability: { v: 0.640, tol: 0.05, et: 'run sano' },
  performance:  { v: 1.000, tol: 0.02, alto: 0.02, morta: 0.005, et: 'target' },
  quality:      { v: null,  tol: 0.05, morta: 0.005, et: 'baseline' },  // riempita dalla route
};

// Soglia dell'eta' del dato, MISURATA (non inventata): con la macchina in
// marcia le 35 valvole chiudono le loro finestre sfalsate su 89-157 s, e il
// dato piu' recente ha fra 2 s e 2 min 39 s. Sopra i 5 minuti, a macchina in
// marcia, i dati non stanno piu' arrivando.
const DATO_ATTESO_S = 300;

/* Quale riferimento usa il gauge OEE primario. Confronto a una variabile
   sola: e' l'UNICA cosa che ?rif= cambia in tutta la pagina.
     sano (predefinito) -> OEE misurato sul run sano (0,504)
     oggi               -> disponibilita' osservata x 1,000 x qualita' base,
                           interamente derivato dall'API. */
const MODO_RIF = new URLSearchParams(location.search).get('rif') === 'oggi'
  ? 'oggi' : 'sano';
const FONDO = 1.10;   // fondoscala dei gauge: 110%, cosi' 100,2% non sfonda

const OMAC = ['Starting', 'Running', 'Stopping', 'Stopped', 'Idle'];
const IN_MARCIA = new Set(['Running', 'Starting']);

const V = (n) => `var(--${n})`;
const SVG = 'http://www.w3.org/2000/svg';
const el = (t, a = {}, txt) => {
  const n = document.createElementNS(SVG, t);
  for (const k in a) if (a[k] !== null && a[k] !== undefined) n.setAttribute(k, a[k]);
  if (txt !== undefined) n.textContent = txt;
  return n;
};
const svg = (id) => { const s = document.getElementById(id); s.replaceChildren(); return s; };
const ora = (iso) => new Date(iso).toLocaleTimeString('it-IT',
  { hour: '2-digit', minute: '2-digit' });
const giornoOra = (iso) => new Date(iso).toLocaleString('it-IT',
  { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });

/* Il colore segue SOLO lo scostamento in basso dal riferimento.
   Dentro il riferimento -> neutro. Sopra il riferimento -> neutro. */
function tinta(v, rif) {
  if (v === null || v === undefined || !rif || rif.v === null) return 'dato';
  if (rif.alto !== undefined && v > rif.v + rif.alto) return 'sev2';
  const giu = rif.v - v;
  // Zona morta: sotto questo scarto la grandezza e' ferma nel proprio rumore.
  // Sulla prestazione lo scarto osservato e' 0,1-0,4 punti su tutti e sei gli
  // scenari e non vale mai esattamente 1,000: senza zona morta sarebbe tinta
  // sempre, e un colore sempre acceso non e' un segnale.
  if (giu <= (rif.morta ?? 0)) return 'dato';  // sopra, sulla tacca, o nel rumore
  const q = rif.tol;                            // un gradino ogni "tol"
  if (giu <= q)     return 'sev1';              // appena sotto: colore tenue
  if (giu <= q * 2) return 'sev2';
  if (giu <= q * 3) return 'sev3';
  return 'sev4';                                // molto sotto
}
// di quanto e' sotto, in punti percentuali (per il suggerimento)
function scostamento(v, rif) {
  if (v == null || !rif || rif.v === null) return null;
  return v - rif.v;
}

/* ---------------- geometria dell'arco ---------------- */
const A0 = 216, A1 = -36;
const pol = (cx, cy, r, a) => [cx + r * Math.cos(a * Math.PI / 180),
                               cy - r * Math.sin(a * Math.PI / 180)];
function arco(cx, cy, r, da, a) {
  if (Math.abs(da - a) < 0.01) return '';
  const [x0, y0] = pol(cx, cy, r, da), [x1, y1] = pol(cx, cy, r, a);
  return `M ${x0} ${y0} A ${r} ${r} 0 ${(da - a) > 180 ? 1 : 0} 1 ${x1} ${y1}`;
}
const ang = (f) => A0 - (A0 - A1) * Math.max(0, Math.min(1, f));

/* ---------------- suggerimento al passaggio del mouse ----------------
   Bersaglio generoso: si cerca la X piu' vicina, non serve centrare il
   punto. Il riquadro e' in position:fixed, quindi non sposta mai il
   contenuto. Raggiungibile da tastiera: frecce destra/sinistra, Home/Fine. */
const TIP = () => document.getElementById('tip');

function mostraTip(html, cx, cy) {
  const t = TIP();
  t.innerHTML = html;
  t.hidden = false;
  const r = t.getBoundingClientRect();
  let x = cx + 14, yv = cy - r.height - 12;
  if (x + r.width > innerWidth - 8) x = cx - r.width - 14;
  if (yv < 8) yv = cy + 16;
  t.style.left = Math.max(8, x) + 'px';
  t.style.top = yv + 'px';
}
const nascondiTip = () => { TIP().hidden = true; };

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
    // dal sistema di coordinate del viewBox a quello dello schermo
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

/* ---------------- gauge ad arco ---------------- */
function gauge(s, o) {
  const { cx, cy, r, sp } = o;
  const g = el('g');
  g.appendChild(el('path', { d: arco(cx, cy, r, A0, A1), fill: 'none',
    'stroke-width': sp, style: `stroke:${V('traccia')}` }));

  if (o.rif && o.rif.v !== null) {
    const b0 = ang((o.rif.v - o.rif.tol) / o.fondo);
    const b1 = ang((o.rif.v + (o.rif.alto ?? o.rif.tol)) / o.fondo);
    g.appendChild(el('path', { d: arco(cx, cy, r, b0, b1), fill: 'none',
      'stroke-width': sp, style: `stroke:${V('banda')}` }));
  }

  if (o.vuoto) {
    g.appendChild(el('path', { d: arco(cx, cy, r, A0, A1), fill: 'none',
      'stroke-width': sp, 'stroke-dasharray': '3 6',
      style: `stroke:${V('banda')}` }));
  } else if (o.valore !== null && o.valore !== undefined) {
    g.appendChild(el('path', { d: arco(cx, cy, r, A0, ang(o.valore / o.fondo)),
      fill: 'none', 'stroke-width': sp, style: `stroke:${V(o.tinta || 'dato')}` }));
  }

  if (o.rif && o.rif.v !== null) {
    const a = ang(o.rif.v / o.fondo);
    const [x0, y0] = pol(cx, cy, r - sp / 2 - 2, a);
    const [x1, y1] = pol(cx, cy, r + sp / 2 + 2, a);
    g.appendChild(el('line', { x1: x0, y1: y0, x2: x1, y2: y1,
      'stroke-width': 2, style: `stroke:${V('rif')}` }));
  }

  g.appendChild(el('text', { x: cx, y: cy + (o.numSize || 34) * 0.30,
    'text-anchor': 'middle',
    'font-size': o.vuoto ? Math.round((o.numSize || 34) * 0.42) : (o.numSize || 34),
    'font-weight': 600,
    style: `fill:${V(o.vuoto ? 'muto' : (o.tintaNum || o.tinta || 'ink'))}` }, o.testo));
  if (o.etichetta)
    g.appendChild(el('text', { x: cx, y: cy + r * 0.62 + 16, 'text-anchor': 'middle',
      'font-size': o.labSize || 12, 'letter-spacing': '.1em',
      style: `fill:${V('muto')}` }, o.etichetta.toUpperCase()));
  if (o.rifTesto)
    g.appendChild(el('text', { x: cx, y: cy + r * 0.62 + (o.etichetta ? 32 : 16),
      'text-anchor': 'middle', 'font-size': o.rifSize || 11,
      style: `fill:${V('rif')}` }, o.rifTesto));
  s.appendChild(g);
}

/* ================================================================== */
let DATI = null;

async function main() {
  const [stato, oeeG, oeeT, serie, valvole, allarmi, storico, base] =
    await Promise.all([
      api.stato(), api.oee('day'), api.oee('shift'), api.oeeSerie(),
      api.valvole(), api.allarmi(), api.storico(), baselineValvole(),
    ]);
  DATI = { valvole, base, allarmi };

  /* --- riferimento di qualita' CALCOLATO dalla route baseline --- */
  const tassi = Object.values(base.valves || {})
    .map(v => v.fill_quality_ok_rate).filter(x => typeof x === 'number');
  if (tassi.length)
    RIF.quality.v = tassi.reduce((a, b) => a + b, 0) / tassi.length;

  /* ---------- stato OMAC ---------- */
  const inMarcia = IN_MARCIA.has(stato.label);
  const bn = document.getElementById('stato-grande');
  bn.className = 'stato-grande' + (inMarcia ? '' : ' fermo');
  bn.replaceChildren();
  const nn = document.createElement('span'); nn.className = 'n';
  nn.textContent = stato.label.toUpperCase();
  const dd = document.createElement('span'); dd.className = 'd';
  dd.textContent = `OMAC ${stato.state}`;
  bn.append(nn, dd);

  const ol = document.getElementById('omac');
  ol.replaceChildren();
  for (const s of (OMAC.includes(stato.label) ? OMAC : [...OMAC, stato.label])) {
    const li = document.createElement('li');
    li.textContent = s;
    if (s === stato.label) li.className = 'on';
    ol.appendChild(li);
  }

  /* ---------- ultimo dato: una riga di testo ----------
     Si accende SOLO se la macchina e' in marcia e il dato e' vecchio: vuol
     dire che gira ma i dati non arrivano. A macchina ferma resta neutra,
     perche' li' non direbbe niente che lo stato non dica gia'. */
  const eta = etaDato(valvole, oeeG.at);
  const tsUltimo = ultimoTs(valvole);
  const acquisizioneRotta = inMarcia && eta && eta.secondi > DATO_ATTESO_S;
  const rd = document.getElementById('riga-dato');
  rd.className = 'riga-dato' + (acquisizioneRotta ? ' vecchio' : '');
  rd.replaceChildren();
  if (acquisizioneRotta) {
    const p = document.createElement('span'); p.className = 'pallino';
    rd.appendChild(p);
  }
  const et = document.createElement('span'); et.className = 'et';
  et.textContent = 'Ultimo dato';
  const or = document.createElement('span'); or.className = 'or';
  or.textContent = tsUltimo ? ora(tsUltimo) : '—';
  const fa = document.createElement('span');
  fa.textContent = eta ? `${eta.testo} fa` : '';
  rd.append(et, or, fa);

  /* ---------- OEE giorno (primario) ----------
     Il riferimento di QUESTO gauge, e solo di questo, dipende da ?rif=.
     RIF.oee resta intatto: andamento e turno non si muovono. */
  {
    let rifOee = RIF.oee;
    if (MODO_RIF === 'oggi' && oeeG.availability != null && RIF.quality.v !== null)
      rifOee = { v: oeeG.availability * RIF.performance.v * RIF.quality.v,
                 tol: RIF.oee.tol, et: 'disponibilità di oggi × qualità base' };
    const t = tinta(oeeG.oee, rifOee);
    gauge(svg('g-oee'), {
      cx: 180, cy: 222, r: 150, sp: 32, fondo: FONDO,
      valore: oeeG.oee, rif: rifOee, tinta: t, tintaNum: (t === 'dato' || t === 'sev1') ? 'ink' : t,
      testo: pct(oeeG.oee), numSize: 78,
      etichetta: 'OEE · ultime 24 h', labSize: 15,
      rifTesto: `riferimento ${pct(rifOee.v)} · ${rifOee.et}`, rifSize: 13,
    });
  }

  /* ---------- i tre componenti ---------- */
  for (const [id, v, rif, lab] of [
    ['g-a', oeeG.availability, RIF.availability, 'Disponibilità'],
    ['g-p', oeeG.performance,  RIF.performance,  'Prestazione'],
    ['g-q', oeeG.quality,      RIF.quality,      'Qualità'],
  ]) {
    const t = tinta(v, rif);
    gauge(svg(id), {
      cx: 100, cy: 92, r: 62, sp: 16, fondo: FONDO,
      valore: v, rif, tinta: t, tintaNum: (t === 'dato' || t === 'sev1') ? 'ink' : t,
      vuoto: v === null || v === undefined,
      testo: v === null || v === undefined ? 'non calc.' : pct(v), numSize: 30,
      etichetta: lab, labSize: 11,
      rifTesto: rif.v === null ? '' : `rif. ${pct(rif.v)} · ${rif.et}`, rifSize: 10.5,
    });
  }

  disegnaSerie(serie.day_ridotto || []);
  disegnaTempo(oeeG.availability_detail && oeeG.availability_detail.by_state);
  disegnaValvole(valvole, allarmi.alerts || []);
  disegnaTurno(oeeT, stato);
  disegnaContatori(oeeG, allarmi, storico, valvole);
}

function ultimoTs(valvole) {
  const ts = Object.values(valvole.valves || {})
    .map(v => v.last_prediction && v.last_prediction.prediction_ts)
    .filter(Boolean).sort();
  return ts.length ? ts[ts.length - 1] : null;
}

/* ---------------- contatori ---------------- */
function disegnaContatori(oeeG, allarmi, storico, valvole) {
  const q = oeeG.quality_detail || {};
  const scarti = (q.total != null && q.good != null) ? q.total - q.good : null;
  const att = (allarmi.alerts || []).length;
  const valvAtt = new Set((allarmi.alerts || []).map(a => a.valve_id)).size;
  const chiusi = (storico.alerts || []).filter(a => a.status === 'closed').length;
  const scartoBase = RIF.quality.v === null ? null : 1 - RIF.quality.v;
  const scartoOra = (oeeG.quality == null) ? null : 1 - oeeG.quality;

  const righe = [
    ['Cicli', num(q.total), '', ''],
    ['Buoni', num(q.good), '', ''],
    ['Scarti', num(scarti),
      scartoOra === null ? '' :
        `${pct(scartoOra)}${scartoBase === null ? '' : ` · base ${pct(scartoBase)}`}`,
      (scartoOra !== null && scartoBase !== null &&
       scartoOra - scartoBase > RIF.quality.tol)
        ? (scartoOra - scartoBase > RIF.quality.tol * 3 ? 'gra' : 'att') : ''],
    ['Allarmi attivi', num(att), '', att === 0 ? '' : (att > 10 ? 'gra' : 'att')],
    ['Valvole in allarme', num(valvAtt),
      `su ${Object.keys(valvole.valves || {}).length}`,
      valvAtt === 0 ? '' : (valvAtt > 5 ? 'gra' : 'att')],
    ['Allarmi chiusi · storico', num(chiusi), '', ''],
  ];
  const dl = document.getElementById('contatori');
  dl.replaceChildren();
  for (const [k, v, u, cls] of righe) {
    const d = document.createElement('div'); d.className = 'riga';
    const dt = document.createElement('dt'); dt.textContent = k;
    const dd = document.createElement('dd'); dd.className = cls; dd.textContent = v;
    if (u) { const sp = document.createElement('span'); sp.className = 'u';
             sp.textContent = u; dd.appendChild(sp); }
    d.append(dt, dd); dl.appendChild(d);
  }
}

/* ---------------- serie temporale ---------------- */
function disegnaSerie(pts) {
  const s = svg('serie');
  const W = 760, H = 250, L = 46, R = 118, T = 16, B = 26;
  const iw = W - L - R, ih = H - T - B;
  const y = (v) => T + ih - (v / FONDO) * ih;

  for (const t of [0, 0.25, 0.5, 0.75, 1.0]) {
    s.appendChild(el('line', { x1: L, x2: L + iw, y1: y(t), y2: y(t),
      'stroke-width': 1, style: `stroke:${V('traccia')}` }));
    s.appendChild(el('text', { x: L - 8, y: y(t) + 4, 'text-anchor': 'end',
      'font-size': 11, style: `fill:${V('muto')}` }, pct(t)));
  }

  const b = RIF.oee;
  s.appendChild(el('rect', { x: L, y: y(b.v + b.tol), width: iw,
    height: Math.max(1, y(b.v - b.tol) - y(b.v + b.tol)),
    style: `fill:${V('traccia')}` }));
  s.appendChild(el('line', { x1: L, x2: L + iw, y1: y(b.v), y2: y(b.v),
    'stroke-width': 1.5, 'stroke-dasharray': '5 4', style: `stroke:${V('rif')}` }));
  s.appendChild(el('text', { x: L + iw + 6, y: y(b.v) + 4, 'font-size': 11,
    style: `fill:${V('rif')}` }, `riferimento ${pct(b.v)}`));

  const n = pts.length;
  if (!pts.some(p => p.oee != null)) {
    s.appendChild(el('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle',
      'font-size': 14, style: `fill:${V('muto')}` }, 'serie non disponibile'));
    return;
  }
  const x = (i) => n === 1 ? L + iw / 2 : L + (i / (n - 1)) * iw;

  let d = '', su = false;
  pts.forEach((p, i) => {
    if (p.oee == null) { su = false; return; }
    d += (su ? ' L ' : ' M ') + x(i) + ' ' + y(p.oee); su = true;
  });
  s.appendChild(el('path', { d, fill: 'none', 'stroke-width': 2,
    'stroke-linejoin': 'round', style: `stroke:${V('dato')}` }));

  pts.forEach((p, i) => {
    if (p.oee == null) return;
    s.appendChild(el('circle', { cx: x(i), cy: y(p.oee), r: n > 14 ? 3 : 4.5,
      'stroke-width': 2, style: `fill:${V('sup')};stroke:${V('dato')}` }));
  });

  const iu = pts.map((p, i) => [p, i]).filter(([p]) => p.oee != null).pop();
  if (iu) {
    const [p, i] = iu, t = tinta(p.oee, RIF.oee);
    s.appendChild(el('circle', { cx: x(i), cy: y(p.oee), r: 5, 'stroke-width': 2,
      style: `fill:${V(t)};stroke:${V('sup')}` }));
    s.appendChild(el('text', { x: Math.min(x(i) + 9, W - 92), y: y(p.oee) - 9,
      'font-size': 13, 'font-weight': 600,
      style: `fill:${V(t === 'dato' || t === 'sev1' ? 'ink' : t)}` }, pct(p.oee)));
  }

  // ---- strato interattivo: valore, momento, scostamento dal riferimento ----
  {
    const idx = pts.map((p, i) => i).filter(i => pts[i].oee != null);
    const mira = el('line', { x1: 0, x2: 0, y1: T, y2: T + ih, 'stroke-width': 1,
      style: `stroke:${V('rif')}`, visibility: 'hidden' });
    const seg = el('circle', { r: 6, 'stroke-width': 2,
      style: `fill:${V('sup')};stroke:${V('ink')}`, visibility: 'hidden' });
    s.append(mira, seg);
    s.setAttribute('class', 'interattivo');
    s.setAttribute('tabindex', '0');
    s.setAttribute('role', 'img');
    s.setAttribute('aria-label', `Andamento OEE, ${idx.length} punti, `
      + `riferimento ${pct(b.v)}`);
    agganciaHover(s, {
      n: idx.length, L, iw, T, ih, mira, seg,
      x: (k) => x(idx[k]),
      puntoY: (k) => y(pts[idx[k]].oee),
      testo: (k) => {
        const p = pts[idx[k]], sc = p.oee - b.v;
        const fuori = sc < 0;
        return `<span class="v">${pct(p.oee)}</span> `
          + `<span class="m">${giornoOra(p.at)}</span><br>`
          + `<span class="m">A ${pct(p.availability)} · P ${pct(p.performance)} `
          + `· Q ${pct(p.quality)}</span><br>`
          + `<span class="${fuori ? 'f' : 'm'}">${sc >= 0 ? '+' : '−'}`
          + `${num(Math.abs(sc) * 100, 1)} punti dal riferimento ${pct(b.v)}</span>`;
      },
    });
  }

  s.appendChild(el('text', { x: L, y: H - 8, 'font-size': 11,
    style: `fill:${V('muto')}` }, giornoOra(pts[0].at)));
  s.appendChild(el('text', { x: L + iw, y: H - 8, 'text-anchor': 'end',
    'font-size': 11, style: `fill:${V('muto')}` }, giornoOra(pts[n - 1].at)));
  s.appendChild(el('text', { x: L + iw / 2, y: H - 8, 'text-anchor': 'middle',
    'font-size': 11, style: `fill:${V('muto')}` }, `${n} punti`));
}

/* ---------------- ripartizione del tempo ----------------
   NON e' una linea del tempo: le route danno i TOTALI per stato
   (availability_detail.by_state) e il NUMERO di transizioni
   (source.state_transitions), mai i loro istanti. Mettere un asse dei tempi
   sotto questi segmenti significherebbe inventare l'ordine cronologico.
   Resta quindi una ripartizione, ordinata per stato, senza asse dei tempi. */
function disegnaTempo(byState) {
  const s = svg('tempo');
  const W = 760, yb = 20, h = 34;
  const voci = Object.entries(byState || {}).filter(([, v]) => v > 0)
    .sort((a, b) => OMAC.indexOf(a[0]) - OMAC.indexOf(b[0]));
  const tot = voci.reduce((a, [, v]) => a + v, 0);
  if (!tot) {
    s.appendChild(el('text', { x: W / 2, y: 50, 'text-anchor': 'middle',
      'font-size': 13, style: `fill:${V('muto')}` },
      'nessuna transizione di stato nella finestra'));
    return;
  }
  // tutti neutri: uno stato non e' una gravita'. Running e' il piu' scuro.
  const opac = { Running: 1, Starting: .62, Stopping: .5, Stopped: .42, Idle: .28 };
  let cx = 0;
  for (const [k, v] of voci) {
    const w = (v / tot) * W;
    s.appendChild(el('rect', { x: cx, y: yb, width: Math.max(1, w - 2), height: h,
      rx: 1, style: `fill:${V('dato')};fill-opacity:${opac[k] ?? .35}` }));
    if (w > 62) {
      s.appendChild(el('text', { x: cx + 6, y: yb + 14, 'font-size': 11,
        'letter-spacing': '.05em',
        style: `fill:${V(k === 'Running' ? 'sup' : 'ink')}` }, k));
      s.appendChild(el('text', { x: cx + 6, y: yb + 28, 'font-size': 12,
        'font-weight': 600,
        style: `fill:${V(k === 'Running' ? 'sup' : 'ink')}` }, `${num(v / 3600, 1)} h`));
    }
    if (w > 34)
      s.appendChild(el('text', { x: cx, y: yb + h + 15, 'font-size': 11,
        style: `fill:${V('muto')}` }, pct(v / tot)));
    cx += w;
  }
  const xb = RIF.availability.v * W;
  s.appendChild(el('line', { x1: xb, x2: xb, y1: yb - 8, y2: yb + h + 3,
    'stroke-width': 1.5, 'stroke-dasharray': '4 3', style: `stroke:${V('rif')}` }));
  s.appendChild(el('text', { x: xb + 5, y: yb - 10, 'font-size': 11,
    style: `fill:${V('rif')}` }, `marcia di riferimento ${pct(RIF.availability.v)}`));
}

/* ---------------- allarmi attivi per valvola ---------------- */
function disegnaValvole(valvole, alerts) {
  const s = svg('valvole');
  const ids = Object.keys(valvole.valves || {}).map(Number).sort((a, b) => a - b);
  const conta = new Map();
  for (const a of alerts) conta.set(a.valve_id, (conta.get(a.valve_id) || 0) + 1);

  const W = 760, yb = 14, h = 40, passo = W / ids.length;

  const leg = [[0, 'traccia', 'nessun allarme'], [110, 'attenz', '1 allarme'],
               [196, 'grave', '2 o più']];
  for (const [lx, c, t] of leg) {
    s.appendChild(el('rect', { x: lx, y: 0, width: 10, height: 9, rx: 1,
      style: `fill:${V(c)}` }));
    s.appendChild(el('text', { x: lx + 14, y: 8, 'font-size': 10.5,
      style: `fill:${V('rif')}` }, t));
  }

  ids.forEach((id, i) => {
    const c = conta.get(id) || 0;
    const tin = c === 0 ? 'traccia' : (c === 1 ? 'attenz' : 'grave');
    const g = el('g', {
      class: 'cella-valvola', tabindex: '0', role: 'button',
      'aria-label': `Valvola ${id}, ${c === 0 ? 'nessun allarme attivo'
        : c + (c === 1 ? ' allarme attivo' : ' allarmi attivi')}. Apri il dettaglio.`,
    });
    g.appendChild(el('title', {}, `Valvola ${id} · ${c === 0 ? 'nessun allarme attivo'
      : c + (c === 1 ? ' allarme attivo' : ' allarmi attivi')}`));
    g.appendChild(el('rect', { class: 'sfondo', x: i * passo, y: yb,
      width: passo - 2, height: h, rx: 1, style: `fill:${V(tin)}` }));
    if (c > 0)
      g.appendChild(el('text', { class: 'num', x: i * passo + (passo - 2) / 2,
        y: yb + h / 2 + 5, 'text-anchor': 'middle', 'font-size': 13,
        'font-weight': 600,
        style: `fill:${V(c === 1 ? 'su-att' : 'sup')}` }, String(c)));
    g.appendChild(el('text', { class: 'num', x: i * passo + (passo - 2) / 2,
      y: yb + h + 14, 'text-anchor': 'middle', 'font-size': 10,
      'font-weight': c > 0 ? 600 : 400,
      style: `fill:${V(c > 0 ? 'ink' : 'muto')}` }, String(id)));

    g.addEventListener('click', () => apriPannello(id, g));
    g.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); apriPannello(id, g); }
    });
    s.appendChild(g);
  });
}

/* ---------------- turno in corso: due barre VERTICALI ----------------
   precedente a sinistra, corrente a destra: la lettura segue il tempo. */
function disegnaTurno(t, stato) {
  const s = svg('g-turno');
  const nullo = t.oee == null;
  // Macchina non in marcia: il numero resta, la tinta no. Con queste route un
  // fermo voluto e un guasto non sono distinguibili (su e-macchina-ferma il
  // turno ha 6.120 s di marcia su 28.800 PIANIFICATI: il calo e' reale, ma
  // nulla dice se sia voluto). Colorare sarebbe un verdetto non ricavabile —
  // lo stato IDLE e l'eta' del dato dicono gia' cio' che si sa con certezza.
  // Decisione dell'utente 2026-08-19, presa sulla disponibilita' di turno
  // della pagina OEE e qui estesa all'OEE di turno per lo stesso argomento.
  const senzaTinta = stato && !IN_MARCIA.has(stato.label);
  const tt = senzaTinta ? 'dato' : tinta(t.oee, RIF.oee);
  gauge(s, {
    cx: 120, cy: 96, r: 68, sp: 17, fondo: FONDO,
    valore: t.oee, rif: RIF.oee, tinta: tt, tintaNum: tt === 'dato' ? 'ink' : tt,
    vuoto: nullo,
    testo: nullo ? 'non calcolabile' : pct(t.oee), numSize: 40,
    etichetta: 'OEE turno', labSize: 11,
    rifTesto: `riferimento ${pct(RIF.oee.v)}`, rifSize: 10.5,
  });

  const y0 = 194, hMax = 96, bw = 50;
  const xs = [62, 152];
  const righe = [['precedente', (t.prev || {}).oee], ['corrente', t.oee]];

  const yr = y0 + hMax - (RIF.oee.v / FONDO) * hMax;
  s.appendChild(el('line', { x1: 26, x2: 214, y1: yr, y2: yr, 'stroke-width': 1.5,
    'stroke-dasharray': '4 3', style: `stroke:${V('rif')}` }));

  righe.forEach(([lab, v], i) => {
    const cx = xs[i];
    s.appendChild(el('line', { x1: cx - bw / 2, x2: cx + bw / 2, y1: y0 + hMax,
      y2: y0 + hMax, 'stroke-width': 1, style: `stroke:${V('banda')}` }));
    if (v == null) {
      s.appendChild(el('rect', { x: cx - bw / 2, y: y0 + hMax - 14, width: bw,
        height: 14, fill: 'none', 'stroke-width': 1, 'stroke-dasharray': '3 3',
        style: `stroke:${V('banda')}` }));
      s.appendChild(el('text', { x: cx, y: y0 + hMax - 20, 'text-anchor': 'middle',
        'font-size': 13, 'font-weight': 600, style: `fill:${V('muto')}` }, '—'));
      s.appendChild(el('text', { x: cx, y: y0 + hMax + 26, 'text-anchor': 'middle',
        'font-size': 10, style: `fill:${V('muto')}` }, '0 cicli'));
    } else {
      const h = Math.max(2, (v / FONDO) * hMax), ti = tinta(v, RIF.oee);
      s.appendChild(el('rect', { x: cx - bw / 2, y: y0 + hMax - h, width: bw,
        height: h, rx: 1, style: `fill:${V(ti)}` }));
      s.appendChild(el('text', { x: cx, y: y0 + hMax - h - 6, 'text-anchor': 'middle',
        'font-size': 13, 'font-weight': 600,
        style: `fill:${V(ti === 'dato' || ti === 'sev1' ? 'ink' : ti)}` }, pct(v)));
    }
    s.appendChild(el('text', { x: cx, y: y0 + hMax + 14, 'text-anchor': 'middle',
      'font-size': 10, 'letter-spacing': '.06em',
      style: `fill:${V('muto')}` }, lab.toUpperCase()));
  });
}

/* ================= pannello valvola ================= */
let tornaA = null;

async function apriPannello(id, origine) {
  tornaA = origine;
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
    c.innerHTML = '';
    const k = document.createElement('span'); k.className = 'k';
    k.textContent = a.status;
    const n = document.createElement('span');
    // `a.fault_type` vale sempre `score_aggregation`: e' la lineage tecnica
    // dell'apertura, non il nome del guasto. Il nome sta nella predizione
    // della valvola. E la data vuole il giorno: un allarme aperto il 3
    // luglio, scritto col solo orario, si legge come stamattina.
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
      ['ciclo', String(k.cycle_id), false],
    ]) sez2.appendChild(chip(et, val, grave));
  }
  corpo.appendChild(sez2);

  // --- confronto con la base DELLA SINGOLA VALVOLA ---
  const sez3 = sezione('Ultimi cicli contro la base di questa valvola');
  corpo.appendChild(sez3);
  if (!b) {
    sez3.appendChild(vuoto('baseline non disponibile per questa valvola'));
    return;
  }
  let kpi;
  try { kpi = await api.valvolaKpi(id); } catch (e) { kpi = null; }
  const serie = (kpi && Array.isArray(kpi.series)) ? kpi.series : null;
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


/* ---------------------------------------------------------------------
   Carta di controllo di UNA valvola contro la base di QUELLA valvola.

   La banda e' mean +- 3*std della baseline. NON i limiti XmR della route:
   misurato, ucl/lcl = mean +- 2,66*MRbar segnalano 165-316 cicli su 400
   fuori limite su valvole SANE, perche' MRbar misura lo scarto fra cicli
   consecutivi (sigma 8,9-9,1 ms) mentre la dispersione vera e' sette volte
   piu' larga (std 70-72 ms): il processo deriva lentamente.

   Regole di disegno, dopo il difetto trovato sulla valvola 13:
   - la SERIE e' sempre il marchio piu' in alto e non viene mai coperta.
     Prima ogni ciclo fuori banda riceveva un pallino: con 400 su 400 i
     pallini formavano una fascia piena che seppelliva la linea.
   - i marcatori restano solo sugli ATTRAVERSAMENTI della banda (entra/esce),
     che sono pochi per costruzione. Il "quanto e' fuori" lo dicono l'asse
     sigma a destra, il contatore e il suggerimento al passaggio del mouse.
   - il dominio contiene sempre banda + dati, ma garantisce ai dati almeno
     una quota minima di altezza, cosi' una serie piatta non finisce
     incollata al bordo.
   --------------------------------------------------------------------- */
function carta(campo, serie, base, vid) {
  const wrap = document.createElement('div'); wrap.className = 'pan-graf';
  const W = 520, H = 138, L = 8, R = 52, T = 20, B = 18;
  const iw = W - L - R, ih = H - T - B;
  const s = document.createElementNS(SVG, 'svg');
  s.setAttribute('viewBox', `0 0 ${W} ${H}`);
  s.setAttribute('class', 'interattivo');
  s.setAttribute('tabindex', '0');
  s.setAttribute('role', 'img');

  const punti = serie.map(p => ({ v: p[campo], ts: p.event_ts, id: p.cycle_id }))
                     .filter(p => typeof p.v === 'number');
  if (!punti.length) { wrap.appendChild(vuoto(`${campo}: nessun valore`)); return wrap; }
  const vals = punti.map(p => p.v);

  const alto = base.mean + 3 * base.std, basso = base.mean - 3 * base.std;
  const dLo = Math.min(...vals), dHi = Math.max(...vals);
  let lo = Math.min(basso, dLo), hi = Math.max(alto, dHi);
  // quota minima ai dati: una serie costante non deve schiacciarsi sul bordo
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

  // intervallo normale
  s.appendChild(el('rect', { x: L, y: y(alto), width: iw,
    height: Math.max(2, y(basso) - y(alto)), style: `fill:${V('traccia')}` }));
  s.appendChild(el('line', { x1: L, x2: L + iw, y1: y(base.mean), y2: y(base.mean),
    'stroke-width': 1.5, 'stroke-dasharray': '5 4', style: `stroke:${V('rif')}` }));

  // asse sigma a destra: dice QUANTO si e' fuori senza toccare la serie
  // se la banda e' schiacciata, una etichetta sola invece di tre sovrapposte
  const strettaBanda = Math.abs(y(basso) - y(alto)) < 26;
  const tacche = strettaBanda
    ? [[base.mean, `${num(base.mean, 0)} ±3σ`]]
    : [[alto, '+3σ'], [base.mean, num(base.mean, 0)], [basso, '−3σ']];
  for (const [v, t] of tacche) {
    if (y(v) < T - 2 || y(v) > T + ih + 2) continue;
    s.appendChild(el('line', { x1: L + iw, x2: L + iw + 4, y1: y(v), y2: y(v),
      'stroke-width': 1, style: `stroke:${V('rif')}` }));
    s.appendChild(el('text', { x: L + iw + 7, y: y(v) + 3.5, 'font-size': 9.5,
      style: `fill:${V('rif')}` }, t));
  }

  // la serie, sempre sopra a tutto
  let d = '';
  vals.forEach((v, i) => { d += (i ? ' L ' : 'M ') + x(i) + ' ' + y(v); });
  s.appendChild(el('path', { d, fill: 'none', 'stroke-width': 1.6,
    'stroke-linejoin': 'round', style: `stroke:${V('ink')}` }));

  // solo gli attraversamenti della banda
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

  // ---- strato interattivo ----
  const mira = el('line', { x1: 0, x2: 0, y1: T, y2: T + ih, 'stroke-width': 1,
    style: `stroke:${V('rif')}`, visibility: 'hidden' });
  const seg = el('circle', { r: 4, 'stroke-width': 2,
    style: `fill:${V('sup')};stroke:${V('ink')}`, visibility: 'hidden' });
  s.append(mira, seg);
  agganciaHover(s, {
    n: vals.length, x, T, ih, L, iw, mira, seg,
    puntoY: (i) => y(vals[i]),
    testo: (i) => {
      const p = punti[i], z = (p.v - base.mean) / base.std;
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

function chiudiPannello() {
  nascondiTip();
  document.getElementById('pannello').hidden = true;
  document.getElementById('velo').hidden = true;
  if (tornaA && tornaA.focus) tornaA.focus();
}
document.getElementById('pan-chiudi').addEventListener('click', chiudiPannello);
document.getElementById('velo').addEventListener('click', chiudiPannello);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !document.getElementById('pannello').hidden)
    chiudiPannello();
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
  try { localStorage.setItem('tema-v7a', nuovo); } catch (e) {}
  aggiornaBtn();
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', aggiornaBtn);
aggiornaBtn();

main().catch((e) => {
  console.error(e);
  const b = document.getElementById('box-primario');
  if (b) b.querySelector('.box-corpo').textContent = 'errore di caricamento: ' + e.message;
});
