// Pagina OEE — perche' l'OEE e' quello che e'.
// MACCHINA dice QUANTO vale. Qui si vede DI COSA E' FATTO: il tempo della
// finestra sceso a gradini fino al tempo che ha prodotto pezzi buoni, e i
// tre ingredienti con i loro numeri veri.
//
// Unica sorgente dati: le route servite da server.py (specchio di
// pipeline/api.py). Nessun numero a schermo che non venga da li'.
//
// REGOLA DEL COLORE (LESSICO §1): il colore lo prende solo cio' che ha una
// gravita'. Un valore che sta bene resta neutro. Non esiste un verde.

import { api, pct, num, etaDato, scenarioCorrente, collegaNav } from '/comune/dati.js';

// Nona route, esposta dal server ma non ancora in comune/dati.js (che non
// posso modificare): stessa forma delle altre, stesso scenario.
const baselineValvole = () =>
  fetch(`/api/${scenarioCorrente()}/valves/baseline`, { cache: 'no-store' })
    .then(r => { if (!r.ok) throw new Error(`valves/baseline -> HTTP ${r.status}`);
                 return r.json(); });

/* ------------------------------------------------------------------ *
 * RIFERIMENTI — identici a quelli di MACCHINA, per non creare una
 * seconda verita' fra due pagine della stessa dashboard.
 *  - qualita'      -> CALCOLATA a runtime dalla route /valves/baseline:
 *                     media di fill_quality_ok_rate sulle 35 valvole
 *                     (0,7868: la qualita' sana, cioe' il 21,3% di scarto
 *                     di base). Il 100% non e' il riferimento di questa
 *                     macchina: non e' mai stato osservato.
 *  - prestazione   -> 1,000 per DEFINIZIONE: e' il rapporto fra cicli reali
 *                     e cicli possibili al target dichiarato dalla route.
 *  - disponibilita'
 *    e OEE         -> misurati sul run sano, etichettati "run sano".
 * ------------------------------------------------------------------ */
const RIF = {
  oee:          { v: 0.504, tol: 0.05, et: 'run sano' },
  availability: { v: 0.640, tol: 0.05, et: 'run sano' },
  performance:  { v: 1.000, tol: 0.02, alto: 0.02, morta: 0.005, et: 'target' },
  quality:      { v: null,  tol: 0.05, morta: 0.005, et: 'baseline' },
};

// Soglia dell'eta' del dato, misurata: a macchina in marcia le 35 valvole
// chiudono le finestre sfalsate su 89-157 s e il dato piu' recente ha fra
// 2 s e 2 min 39 s. Sopra i 5 minuti, in marcia, i dati non arrivano piu'.
const DATO_ATTESO_S = 300;

// Seconda forma del primario, per il confronto: ?forma=barra disegna la
// stessa scomposizione come UNA barra della giornata invece di otto colonne.
const FORMA = new URLSearchParams(location.search).get('forma') === 'barra'
  ? 'barra' : 'cascata';
if (FORMA === 'barra') document.documentElement.classList.add('forma-barra');

const FONDO = 1.10;   // fondoscala dei rapporti: la prestazione tocca 1,002
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
const ore = (s) => num(s / 3600, 1);

/* Il colore segue SOLO lo scostamento in basso dal riferimento.
   Dentro il riferimento -> neutro. Sopra il riferimento -> neutro. */
function tinta(v, rif) {
  if (v === null || v === undefined || !rif || rif.v === null) return 'dato';
  if (rif.alto !== undefined && v > rif.v + rif.alto) return 'sev2';
  const giu = rif.v - v;
  if (giu <= (rif.morta ?? 0)) return 'dato';
  const q = rif.tol;
  if (giu <= q)     return 'sev1';
  if (giu <= q * 2) return 'sev2';
  if (giu <= q * 3) return 'sev3';
  return 'sev4';
}
// il numerale segue l'arco solo quando la gravita' e' reale
const tintaNum = (t) => (t === 'dato' || t === 'sev1') ? 'ink' : t;

/* ---------------- suggerimento al passaggio del mouse ---------------- */
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

/* Hover a bersaglio generoso su un grafico a barre/righe: si cerca la
   colonna (o la riga) piu' vicina, non serve centrare il marchio.
   Accesso da tastiera: frecce, Home, Fine, Esc. */
function agganciaZone(s, o) {
  let idx = -1;
  const mostra = (i, ev) => {
    if (i < 0 || i >= o.n || !o.testo(i)) return;
    idx = i;
    const c = o.centro(i);
    o.mira.setAttribute(o.verticale ? 'x1' : 'y1', o.verticale ? c.x : c.y);
    o.mira.setAttribute(o.verticale ? 'x2' : 'y2', o.verticale ? c.x : c.y);
    o.mira.setAttribute('visibility', 'visible');
    const r = s.getBoundingClientRect(), vb = s.viewBox.baseVal;
    const k = Math.min(r.width / vb.width, r.height / vb.height);
    const ox = r.left + (r.width - vb.width * k) / 2;
    const oy = r.top + (r.height - vb.height * k) / 2;
    mostraTip(o.testo(i), ev ? ev.clientX : ox + c.x * k, oy + c.y * k);
  };
  const spegni = () => { o.mira.setAttribute('visibility', 'hidden'); nascondiTip(); };
  const daEvento = (ev) => {
    const r = s.getBoundingClientRect(), vb = s.viewBox.baseVal;
    const k = Math.min(r.width / vb.width, r.height / vb.height);
    const ox = r.left + (r.width - vb.width * k) / 2;
    const oy = r.top + (r.height - vb.height * k) / 2;
    const p = o.verticale ? (ev.clientX - ox) / k : (ev.clientY - oy) / k;
    let best = 0, bd = Infinity;
    for (let i = 0; i < o.n; i++) {
      const c = o.centro(i), d = Math.abs((o.verticale ? c.x : c.y) - p);
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  };
  s.addEventListener('mousemove', (ev) => mostra(daEvento(ev), ev));
  s.addEventListener('mouseleave', spegni);
  s.addEventListener('blur', spegni);
  s.addEventListener('focus', () => mostra(idx < 0 ? 0 : idx, null));
  s.addEventListener('keydown', (ev) => {
    const k = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 1, ArrowUp: -1 }[ev.key];
    if (k) { ev.preventDefault(); mostra(Math.max(0, Math.min(o.n - 1, idx + k)), null); }
    else if (ev.key === 'Home') { ev.preventDefault(); mostra(0, null); }
    else if (ev.key === 'End') { ev.preventDefault(); mostra(o.n - 1, null); }
    else if (ev.key === 'Escape') spegni();
  });
  s.setAttribute('class', 'interattivo');
  s.setAttribute('tabindex', '0');
  s.setAttribute('role', 'img');
}

/* ================================================================== */
async function main() {
  const [oeeG, oeeT, stato, valvole, base] = await Promise.all([
    api.oee('day'), api.oee('shift'), api.stato(), api.valvole(), baselineValvole(),
  ]);

  const tassi = Object.values(base.valves || {})
    .map(v => v.fill_quality_ok_rate).filter(x => typeof x === 'number');
  if (tassi.length)
    RIF.quality.v = tassi.reduce((a, b) => a + b, 0) / tassi.length;

  if (FORMA === 'barra') disegnaBarra(oeeG); else disegnaCascata(oeeG);
  disegnaDisponibilita(oeeG);
  disegnaPrestazione(oeeG);
  disegnaQualita(oeeG);
  disegnaAdesso(stato, oeeG, oeeT, valvole);
}

/* ==================================================================
   PRIMARIO — la cascata del tempo.

   Ogni gradino e' una moltiplicazione della catena OEE, quindi l'altezza
   finale E' l'OEE: A x P x Q. Le altezze sono calcolate dai rapporti
   pubblicati dalla route (availability, performance, quality) sul tempo
   coperto da storia, cosi' l'ultimo gradino coincide con l'OEE pubblicato
   invece di discostarsene per arrotondamento.

   Il primo gradino, "senza storia", e' la differenza fra la finestra
   (end - start) e planned_s. NON e' un fermo pianificato: planned_s somma
   gia' Idle, Stopping e Stopped, quindi questo tratto e' esattamente la
   parte di finestra priva di storia di stato macchina, che l'API adesso
   misura e dichiara (`uncovered_s`, `coverage`, `window_partial`).
   Non e' una perdita e non prende colore: e'
   il tempo che la route non conta nel denominatore. Negli scenari di
   guasto vale 8,5 h su 24 ed e' l'intera ragione per cui il loro OEE
   legge piu' alto di quello della macchina sana.
   ================================================================== */
function disegnaCascata(d) {
  const s = svg('casc');
  const W = 1186, H = 367, L = 62, R = 10, T = 44, B = 74;
  const iw = W - L - R, ih = H - T - B;

  const fin = (new Date(d.end) - new Date(d.start)) / 1000;
  const pl = d.availability_detail ? d.availability_detail.planned_s : null;
  if (!(fin > 0) || !(pl > 0)) {
    s.appendChild(el('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle',
      'font-size': 15, style: `fill:${V('muto')}` }, 'finestra non disponibile'));
    return;
  }

  const A = d.availability, P = d.performance, Q = d.quality;
  const p = pl / fin;
  const m = A == null ? null : p * A;
  const u = (m == null || P == null) ? null : m * P;
  const g = (u == null || Q == null) ? null : u * Q;

  // livello espresso in frazione della finestra -> y
  const y = (f) => T + ih * (1 - f);
  const h = (a, b) => Math.max(1.5, Math.abs(y(b) - y(a)));

  // ---- assi: ore, cinque tacche ----
  for (let k = 0; k <= 4; k++) {
    const f = k / 4;
    s.appendChild(el('line', { x1: L, x2: L + iw, y1: y(f), y2: y(f),
      'stroke-width': 1, style: `stroke:${V('traccia')}` }));
    s.appendChild(el('text', { x: L - 9, y: y(f) + 4, 'text-anchor': 'end',
      'font-size': 11, style: `fill:${V('muto')}` }, `${ore(fin * f)} h`));
  }

  const col = [
    { k: 'anc', et: 'Finestra',           da: 0, a: 1,  tin: 'dato' },
    { k: 'per', et: 'Senza storia',       da: p, a: 1,  tin: 'dato' },
    { k: 'anc', et: 'Con storia',         da: 0, a: p,  tin: 'dato' },
    { k: 'per', et: 'Fermate e attesa',   da: m, a: p,  tin: tinta(A, RIF.availability) },
    { k: 'anc', et: 'In marcia',          da: 0, a: m,  tin: 'dato' },
    { k: 'per', et: 'Perdita di velocità', da: u, a: m, tin: tinta(P, RIF.performance) },
    { k: 'per', et: 'Scarti',             da: g, a: u,  tin: tinta(Q, RIF.quality) },
    { k: 'anc', et: 'Produttivo',         da: 0, a: g,  tin: tinta(d.oee, RIF.oee) },
  ];
  const n = col.length, passo = iw / n, bw = Math.min(96, passo - 34);
  const cx = (i) => L + passo * i + passo / 2;

  // ---- riferimento sull'ultima colonna: il produttivo del run sano ----
  // La provenienza sta sotto la colonna, non accanto alla tacca: li' si
  // scontrerebbe con il numerale dell'OEE, che cade alla stessa altezza.
  if (RIF.oee.v !== null) {
    const yr = y(p * RIF.oee.v);
    s.appendChild(el('line', { x1: cx(7) - bw / 2 - 12, x2: cx(7) + bw / 2 + 12,
      y1: yr, y2: yr, 'stroke-width': 1.5, 'stroke-dasharray': '5 4',
      style: `stroke:${V('rif')}` }));
  }

  // ---- connettori a gradino ----
  for (let i = 0; i < n - 1; i++) {
    const liv = col[i].k === 'anc' ? col[i].a : col[i].da;
    if (liv == null) continue;
    s.appendChild(el('line', { x1: cx(i) - bw / 2, x2: cx(i + 1) + bw / 2,
      y1: y(liv), y2: y(liv), 'stroke-width': 1, 'stroke-dasharray': '2 3',
      style: `stroke:${V('rif')};stroke-opacity:.65` }));
  }

  // ---- colonne ----
  col.forEach((c, i) => {
    const x = cx(i) - bw / 2;
    if (c.a == null || c.da == null) {
      s.appendChild(el('rect', { x, y: y(0.06), width: bw, height: h(0, 0.06),
        fill: 'none', 'stroke-width': 1, 'stroke-dasharray': '3 3',
        style: `stroke:${V('banda')}` }));
      s.appendChild(el('text', { x: cx(i), y: y(0.06) - 8, 'text-anchor': 'middle',
        'font-size': 12, style: `fill:${V('muto')}` }, 'non calc.'));
    } else if (c.k === 'anc') {
      // gli ancoraggi sono contenitori, non perdite: contorno e velatura
      s.appendChild(el('rect', { x, y: y(c.a), width: bw, height: h(c.da, c.a),
        rx: 1, 'stroke-width': 1,
        style: `fill:${V(c.tin === 'dato' ? 'dato' : c.tin)};`
             + `fill-opacity:${c.tin === 'dato' ? .17 : .9};stroke:${V('dato')}` }));
    } else {
      const vuota = Math.abs(c.a - c.da) * fin < 60;   // meno di un minuto
      s.appendChild(el('rect', { x, y: y(c.a), width: bw,
        height: vuota ? 3 : h(c.da, c.a), rx: 1,
        fill: vuota ? 'none' : V(c.tin),
        'stroke-width': vuota ? 1 : 0,
        'stroke-dasharray': vuota ? '3 3' : null,
        style: vuota ? `stroke:${V('banda')}` : null }));
    }

    // valore sopra la colonna
    if (c.a != null && c.da != null) {
      const val = (c.k === 'anc' ? c.a : c.a - c.da) * fin;
      const nulla = val < 60;                       // meno di un minuto: niente segno
      const grande = (i === 0 || i === 7);
      s.appendChild(el('text', { x: cx(i), y: y(c.a) - (i === 7 ? 34 : 9),
        'text-anchor': 'middle', 'font-size': grande ? 14 : 12.5,
        'font-weight': 600,
        style: `fill:${V(c.k === 'per' && c.tin !== 'dato' && c.tin !== 'sev1'
                        ? c.tin : 'ink')}` },
        `${c.k === 'per' && !nulla ? '−' : ''}${ore(val)} h`));
      if (i === 7 && d.oee != null)
        s.appendChild(el('text', { x: cx(i), y: y(c.a) - 8, 'text-anchor': 'middle',
          'font-size': 27, 'font-weight': 600,
          style: `fill:${V(tintaNum(c.tin))}` }, pct(d.oee)));
    }

    // etichette sotto
    s.appendChild(el('text', { x: cx(i), y: T + ih + 19, 'text-anchor': 'middle',
      'font-size': 10.5, 'letter-spacing': '.07em',
      style: `fill:${V(c.k === 'anc' ? 'ink' : 'muto')};`
           + `font-weight:${c.k === 'anc' ? 600 : 400}` }, c.et.toUpperCase()));
    if (c.k === 'per' && c.a != null && c.da != null)
      s.appendChild(el('text', { x: cx(i), y: T + ih + 34, 'text-anchor': 'middle',
        'font-size': 11, style: `fill:${V('muto')}` },
        `${pct(c.a - c.da)} della finestra`));
    if (i === 7) {
      s.appendChild(el('text', { x: cx(i), y: T + ih + 34, 'text-anchor': 'middle',
        'font-size': 11, style: `fill:${V('muto')}` }, 'OEE della finestra'));
      if (RIF.oee.v !== null)
        s.appendChild(el('text', { x: cx(i), y: T + ih + 49, 'text-anchor': 'middle',
          'font-size': 11, style: `fill:${V('rif')}` },
          `rif. ${pct(RIF.oee.v)} · ${RIF.oee.et}`));
    }
  });

  // ---- strato interattivo ----
  const mira = el('line', { x1: 0, x2: 0, y1: T - 6, y2: T + ih + 4,
    'stroke-width': 1, style: `stroke:${V('rif')}`, visibility: 'hidden' });
  s.appendChild(mira);
  const nomi = ['availability', 'performance', 'quality'];
  agganciaZone(s, {
    n, verticale: true, mira,
    centro: (i) => ({ x: cx(i), y: T + ih / 2 }),
    testo: (i) => {
      const c = col[i];
      if (c.a == null || c.da == null)
        return `<span class="v">non calcolabile</span>`;
      const val = c.k === 'anc' ? c.a * fin : (c.a - c.da) * fin;
      let riga = `<span class="v">${ore(val)} h</span> `
        + `<span class="m">${c.et.toLowerCase()}</span><br>`
        + `<span class="m">${pct(c.k === 'anc' ? c.a : c.a - c.da)} della finestra</span>`;
      const comp = { 3: 'availability', 5: 'performance', 6: 'quality' }[i];
      if (comp) {
        const v = d[comp], r = RIF[comp];
        const sc = (v == null || r.v == null) ? null : v - r.v;
        riga += `<br><span class="${sc != null && sc < 0 ? 'f' : 'm'}">`
          + `${comp === 'availability' ? 'disponibilità'
             : comp === 'performance' ? 'prestazione' : 'qualità'} ${pct(v)}`
          + (sc == null ? '' : ` · ${sc >= 0 ? '+' : '−'}`
             + `${num(Math.abs(sc) * 100, 1)} punti dal riferimento ${pct(r.v)}`)
          + `</span>`;
      }
      if (i === 1)
        riga += `<br><span class="m">fuori dal denominatore della route</span>`;
      if (i === 7 && d.oee != null)
        riga += `<br><span class="m">OEE ${pct(d.oee)} · riferimento `
          + `${pct(RIF.oee.v)}</span>`;
      return riga;
    },
  });
  s.setAttribute('aria-label',
    `Cascata del tempo su ${ore(fin)} ore: con storia ${ore(pl)} ore, `
    + `in marcia ${m == null ? 'non calcolabile' : ore(m * fin) + ' ore'}, `
    + `produttivo ${g == null ? 'non calcolabile' : ore(g * fin) + ' ore'}, `
    + `OEE ${pct(d.oee)}, riferimento ${pct(RIF.oee.v)}`);
  void nomi;
}


/* ==================================================================
   VARIANTE — la stessa scomposizione come UNA barra della giornata.
   In unita' di tempo la scomposizione e' additiva: la finestra e' la
   somma di non pianificato + fermate + velocita' + scarti + produttivo.
   Piu' compatta della cascata, ma perde l'ordine delle divisioni.
   ================================================================== */
function disegnaBarra(d) {
  const s = svg('casc');
  const W = 1186, H = 200, L = 10, R = 10, T = 58, B = 62;
  s.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const iw = W - L - R, bh = H - T - B;

  const fin = (new Date(d.end) - new Date(d.start)) / 1000;
  const pl = d.availability_detail ? d.availability_detail.planned_s : null;
  if (!(fin > 0) || !(pl > 0)) {
    s.appendChild(el('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle',
      'font-size': 15, style: `fill:${V('muto')}` }, 'finestra non disponibile'));
    return;
  }
  const A = d.availability, P = d.performance, Q = d.quality;
  const p = pl / fin;
  const m = A == null ? null : p * A;
  const u = (m == null || P == null) ? null : m * P;
  const g = (u == null || Q == null) ? null : u * Q;
  if (g == null) {
    s.appendChild(el('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle',
      'font-size': 15, style: `fill:${V('muto')}` }, 'scomposizione non calcolabile'));
    return;
  }

  const seg = [
    { et: 'Produttivo',        f: g,     tin: tinta(d.oee, RIF.oee),          pieno: 'dato' },
    { et: 'Scarti',            f: u - g, tin: tinta(Q, RIF.quality) },
    { et: 'Perdita di velocità', f: m - u, tin: tinta(P, RIF.performance) },
    { et: 'Fermate e attesa',  f: p - m, tin: tinta(A, RIF.availability) },
    // NON e' una scelta di pianificazione: `planned_s` somma gia' Idle,
    // Stopping e Stopped, quindi questo tratto e' esattamente la parte di
    // finestra senza storia di stato macchina. L'API adesso la misura e la
    // dichiara (`availability_detail.uncovered_s`, `source.window_partial`):
    // si legge da li' invece di ricavarla, e si chiama col suo nome.
    { et: 'Senza storia',      f: 1 - p, tin: 'dato', tratteggio: true },
  ];
  let x = L;
  seg.forEach((c, i) => {
    const w = c.f * iw;
    if (c.tratteggio) {
      s.appendChild(el('rect', { x, y: T, width: Math.max(1.5, w), height: bh,
        fill: 'none', 'stroke-width': 1, 'stroke-dasharray': '4 4',
        style: `stroke:${V('banda')}` }));
    } else {
      s.appendChild(el('rect', { x, y: T, width: Math.max(1.5, w), height: bh,
        style: `fill:${V(c.tin)};fill-opacity:${c.tin === 'dato'
          ? (i === 0 ? .85 : .38) : 1}` }));
    }
    const cxs = x + w / 2;
    if (w > 96) {
      s.appendChild(el('text', { x: cxs, y: T - 30, 'text-anchor': 'middle',
        'font-size': 10.5, 'letter-spacing': '.07em',
        style: `fill:${V('muto')}` }, c.et.toUpperCase()));
      s.appendChild(el('text', { x: cxs, y: T - 10, 'text-anchor': 'middle',
        'font-size': 16, 'font-weight': 600,
        style: `fill:${V(c.tin === 'dato' || c.tin === 'sev1' ? 'ink' : c.tin)}` },
        `${ore(c.f * fin)} h`));
      s.appendChild(el('text', { x: cxs, y: T + bh + 18, 'text-anchor': 'middle',
        'font-size': 11, style: `fill:${V('muto')}` }, pct(c.f)));
    }
    x += w;
  });

  // riferimento: dove finirebbe il produttivo sul run sano
  const xr = L + p * RIF.oee.v * iw;
  s.appendChild(el('line', { x1: xr, x2: xr, y1: T - 4, y2: T + bh + 4,
    'stroke-width': 1.5, 'stroke-dasharray': '5 4', style: `stroke:${V('rif')}` }));
  s.appendChild(el('text', { x: xr + 5, y: T + bh + 52, 'font-size': 11,
    style: `fill:${V('rif')}` },
    `riferimento ${pct(RIF.oee.v)} della finestra · ${RIF.oee.et}`));

  s.appendChild(el('text', { x: L, y: T + bh + 34, 'font-size': 27,
    'font-weight': 600, style: `fill:${V(tintaNum(tinta(d.oee, RIF.oee)))}` },
    pct(d.oee)));
  s.appendChild(el('text', { x: L, y: T - 30, 'font-size': 10.5,
    'letter-spacing': '.07em', style: `fill:${V('muto')}` },
    `FINESTRA ${ore(fin)} H · CON STORIA ${ore(pl)} H`));

  const mira = el('line', { x1: 0, x2: 0, y1: T - 4, y2: T + bh + 4, 'stroke-width': 1,
    style: `stroke:${V('rif')}`, visibility: 'hidden' });
  s.appendChild(mira);
  const centri = []; let xx = L;
  for (const c of seg) { centri.push(xx + c.f * iw / 2); xx += c.f * iw; }
  agganciaZone(s, {
    n: seg.length, verticale: true, mira,
    centro: (i) => ({ x: centri[i], y: T + bh / 2 }),
    testo: (i) => `<span class="v">${ore(seg[i].f * fin)} h</span> `
      + `<span class="m">${seg[i].et.toLowerCase()}</span><br>`
      + `<span class="m">${pct(seg[i].f)} della finestra</span>`,
  });
  s.setAttribute('aria-label', `La giornata di ${ore(fin)} ore divisa in cinque parti; `
    + `produttivo ${ore(g * fin)} ore, OEE ${pct(d.oee)}`);
}

/* ==================================================================
   DISPONIBILITA' — il tempo per stato OMAC.

   NON e' una linea del tempo: la route da' i TOTALI per stato e il
   NUMERO di transizioni, mai i loro istanti (LESSICO fatto 13). Resta
   una ripartizione, ordinata per stato OMAC, senza asse dei tempi.

   I cinque stati sono SEMPRE tutti e cinque in colonna. Negli scenari di
   guasto la route ne riporta solo due: gli altri tre restano righe vuote
   tratteggiate, ed e' quel vuoto a dire che quella run non si e' mai
   fermata. Uno stato non e' una gravita': tutte le righe sono neutre,
   differenziate solo per opacita'.
   ================================================================== */
function disegnaDisponibilita(d) {
  const s = svg('g-disp');
  const W = 372, H = 185, L = 66, R = 44, T = 18, B = 6;
  const iw = W - L - R, ih = H - T - B;
  const det = d.availability_detail || {};
  const by = det.by_state || {};
  const pl = det.planned_s;

  const cap = document.getElementById('cap-a');
  const t = tinta(d.availability, RIF.availability);
  cap.replaceChildren();
  cap.append(
    span('n', pct(d.availability), `color:${V(tintaNum(t))}`),
    span('r', `rif. ${pct(RIF.availability.v)} · ${RIF.availability.et}`));

  if (!(pl > 0)) {
    s.appendChild(el('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle',
      'font-size': 13, style: `fill:${V('muto')}` }, 'ripartizione non disponibile'));
    cifre('cifre-a', [['Con storia', '—', ''], ['In marcia', '—', ''],
                      ['Transizioni', num((d.source || {}).state_transitions), '']]);
    return;
  }

  const opac = { Running: 1, Starting: .62, Stopping: .5, Stopped: .42, Idle: .28 };
  const passo = ih / OMAC.length, bh = Math.min(20, passo - 8);
  const yc = (i) => T + passo * i + passo / 2;

  // tacca del riferimento di marcia, sulla riga Running
  const xr = L + RIF.availability.v * iw;
  const iRun = OMAC.indexOf('Running');
  s.appendChild(el('line', { x1: xr, x2: xr, y1: yc(iRun) - bh / 2 - 5,
    y2: yc(iRun) + bh / 2 + 5, 'stroke-width': 1.5, 'stroke-dasharray': '4 3',
    style: `stroke:${V('rif')}` }));
  s.appendChild(el('text', { x: xr, y: yc(iRun) - bh / 2 - 9, 'text-anchor': 'middle',
    'font-size': 10, style: `fill:${V('rif')}` }, `marcia rif. ${pct(RIF.availability.v)}`));

  OMAC.forEach((k, i) => {
    const v = by[k];
    s.appendChild(el('text', { x: L - 8, y: yc(i) + 4, 'text-anchor': 'end',
      'font-size': 11, style: `fill:${V(v > 0 ? 'ink' : 'muto')}` }, k));
    if (v > 0) {
      const w = Math.max(2, (v / pl) * iw);
      s.appendChild(el('rect', { x: L, y: yc(i) - bh / 2, width: w, height: bh, rx: 1,
        style: `fill:${V('dato')};fill-opacity:${opac[k] ?? .35}` }));
      s.appendChild(el('text', { x: L + w + 6, y: yc(i) + 4, 'font-size': 11,
        'font-weight': 600, style: `fill:${V('ink')}` }, `${ore(v)} h`));
    } else {
      s.appendChild(el('rect', { x: L, y: yc(i) - bh / 2, width: iw, height: bh,
        fill: 'none', 'stroke-width': 1, 'stroke-dasharray': '3 4',
        style: `stroke:${V('banda')}` }));
      s.appendChild(el('text', { x: L + 7, y: yc(i) + 4, 'font-size': 10.5,
        style: `fill:${V('muto')}` }, 'non osservato nella finestra'));
    }
  });

  const mira = el('line', { x1: L - 4, x2: L + iw + R - 6, y1: 0, y2: 0,
    'stroke-width': 1, style: `stroke:${V('rif')}`, visibility: 'hidden' });
  s.appendChild(mira);
  agganciaZone(s, {
    n: OMAC.length, verticale: false, mira,
    centro: (i) => ({ x: L + iw / 2, y: yc(i) }),
    testo: (i) => {
      const k = OMAC[i], v = by[k];
      if (!(v > 0)) return `<span class="v">${k}</span><br>`
        + `<span class="m">non osservato in questa finestra</span>`;
      return `<span class="v">${ore(v)} h</span> <span class="m">${k}</span><br>`
        + `<span class="m">${pct(v / pl)} del tempo con storia</span>`;
    },
  });
  s.setAttribute('aria-label', `Tempo per stato OMAC su ${ore(pl)} ore con storia, `
    + `cinque stati, marcia di riferimento ${pct(RIF.availability.v)}`);

  cifre('cifre-a', [
    ['Con storia', ore(pl), 'h'],
    ['In marcia', det.running_s == null ? '—' : ore(det.running_s), 'h'],
    ['Transizioni', num((d.source || {}).state_transitions), ''],
  ]);
}

/* ==================================================================
   PRESTAZIONE — cicli prodotti contro cicli possibili al target.
   Il target viene dalla route (speed_target, sorgente speed_target_source):
   la provenienza deve essere leggibile a schermo.
   ================================================================== */
function disegnaPrestazione(d) {
  const s = svg('g-prest');
  const W = 372, H = 150, L = 8, R = 8, T = 22, B = 10;
  const iw = W - L - R, ih = H - T - B;
  const det = d.performance_detail || {};
  const P = d.performance;

  const t = tinta(P, RIF.performance);
  const cap = document.getElementById('cap-p');
  cap.replaceChildren();
  cap.append(
    P == null ? span('n muta', 'non calc.', '')
              : span('n', pct(P), `color:${V(tintaNum(t))}`),
    span('r', `target ${num(det.speed_target)} cicli/h · `
            + `sorgente ${det.speed_target_source || '—'}`));

  if (det.real == null || det.theoretical == null || P == null) {
    s.appendChild(el('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle',
      'font-size': 13, style: `fill:${V('muto')}` },
      'nessun ciclo nella finestra'));
    cifre('cifre-p', [['Prodotto', num(det.real), 'cicli'],
                      ['Producibile', '—', ''],
                      ['Marcia', det.running_h == null ? '—' : num(det.running_h, 1), 'h']]);
    return;
  }

  // due barre a confronto, scala comune 0..producibile x FONDO
  const scala = det.theoretical * FONDO;
  const bh = 40, y0 = T + 6, y1 = T + 6 + bh + 26;
  const xw = (v) => Math.max(2, (v / scala) * iw);

  s.appendChild(el('rect', { x: L, y: y0, width: iw, height: bh,
    style: `fill:${V('traccia')}` }));
  s.appendChild(el('rect', { x: L, y: y0, width: xw(det.real), height: bh,
    style: `fill:${V(t === 'dato' ? 'dato' : t)};fill-opacity:${t === 'dato' ? .8 : 1}` }));
  s.appendChild(el('text', { x: L, y: y0 - 6, 'font-size': 10.5,
    'letter-spacing': '.07em', style: `fill:${V('muto')}` }, 'PRODOTTO'));
  s.appendChild(el('text', { x: L + 8, y: y0 + bh / 2 + 5, 'font-size': 13,
    'font-weight': 600, style: `fill:${V('sup')}` }, num(det.real)));

  s.appendChild(el('rect', { x: L, y: y1, width: xw(det.theoretical), height: bh,
    fill: 'none', 'stroke-width': 1.5, 'stroke-dasharray': '5 4',
    style: `stroke:${V('rif')}` }));
  s.appendChild(el('text', { x: L, y: y1 - 6, 'font-size': 10.5,
    'letter-spacing': '.07em', style: `fill:${V('muto')}` }, 'PRODUCIBILE AL TARGET'));
  s.appendChild(el('text', { x: L + 8, y: y1 + bh / 2 + 5, 'font-size': 13,
    'font-weight': 600, style: `fill:${V('rif')}` }, num(det.theoretical)));

  // la tacca del riferimento attraversa entrambe le barre
  const xt = L + xw(det.theoretical);
  s.appendChild(el('line', { x1: xt, x2: xt, y1: y0 - 2, y2: y1 + bh + 2,
    'stroke-width': 1.5, 'stroke-dasharray': '5 4', style: `stroke:${V('rif')}` }));

  const mira = el('line', { x1: L - 2, x2: L + iw, y1: 0, y2: 0, 'stroke-width': 1,
    style: `stroke:${V('rif')}`, visibility: 'hidden' });
  s.appendChild(mira);
  const zone = [{ y: y0 + bh / 2, et: 'prodotto', v: det.real },
                { y: y1 + bh / 2, et: 'producibile al target', v: det.theoretical }];
  agganciaZone(s, {
    n: 2, verticale: false, mira,
    centro: (i) => ({ x: L + iw / 2, y: zone[i].y }),
    testo: (i) => `<span class="v">${num(zone[i].v)}</span> `
      + `<span class="m">cicli · ${zone[i].et}</span><br>`
      + `<span class="${P < RIF.performance.v - (RIF.performance.morta ?? 0) ? 'f' : 'm'}">`
      + `rapporto ${pct(P)} · scarto ${num(det.real - det.theoretical)} cicli</span>`,
  });
  s.setAttribute('aria-label', `Prodotto ${num(det.real)} cicli contro `
    + `${num(det.theoretical)} possibili al target di ${num(det.speed_target)} `
    + `cicli all'ora: rapporto ${pct(P)}`);

  const vel = det.running_h ? det.real / det.running_h : null;
  cifre('cifre-p', [
    ['Velocità reale', vel == null ? '—' : num(vel), 'cicli/h'],
    ['Mancanti', num(det.theoretical - det.real), 'cicli'],
    ['Marcia', det.running_h == null ? '—' : num(det.running_h, 1), 'h'],
  ]);
}

/* ==================================================================
   QUALITA' — buoni e scarti in cifre assolute.
   Il riferimento e' la base sana calcolata dalla route /valves/baseline
   (media di fill_quality_ok_rate sulle 35 valvole), non il 100%: il 100%
   non e' mai stato osservato su questa macchina.
   ================================================================== */
function disegnaQualita(d) {
  const s = svg('g-qual');
  const W = 372, H = 150, L = 8, R = 8, T = 22, B = 30;
  const iw = W - L - R, ih = H - T - B;
  const det = d.quality_detail || {};
  const Q = d.quality;

  const t = tinta(Q, RIF.quality);
  const cap = document.getElementById('cap-q');
  cap.replaceChildren();
  cap.append(
    Q == null ? span('n muta', 'non calc.', '')
              : span('n', pct(Q), `color:${V(tintaNum(t))}`),
    span('r', RIF.quality.v == null ? '' :
      `base ${pct(RIF.quality.v)} · ${RIF.quality.et} (35 valvole)`));

  if (Q == null || !(det.total > 0)) {
    s.appendChild(el('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle',
      'font-size': 13, style: `fill:${V('muto')}` }, 'nessun ciclo nella finestra'));
    cifre('cifre-q', [['Buoni', num(det.good), ''], ['Scarti', '—', ''],
                      ['Cicli', num(det.total), '']]);
    return;
  }

  const bh = 52, y0 = T + 12;
  const wg = (det.good / det.total) * iw;
  s.appendChild(el('rect', { x: L, y: y0, width: iw, height: bh,
    style: `fill:${V(t === 'dato' ? 'banda' : t)}` }));
  s.appendChild(el('rect', { x: L, y: y0, width: Math.max(2, wg), height: bh, rx: 1,
    style: `fill:${V('dato')};fill-opacity:.8` }));

  s.appendChild(el('text', { x: L + 8, y: y0 + bh / 2 + 5, 'font-size': 14,
    'font-weight': 600, style: `fill:${V('sup')}` }, num(det.good)));
  s.appendChild(el('text', { x: L, y: y0 - 8, 'font-size': 10.5,
    'letter-spacing': '.07em', style: `fill:${V('muto')}` }, 'BUONI'));
  if (iw - wg > 56) {
    s.appendChild(el('text', { x: L + iw - 8, y: y0 + bh / 2 + 5, 'text-anchor': 'end',
      'font-size': 13, 'font-weight': 600,
      style: `fill:${V(t === 'dato' ? 'ink' : 'su-att')}` }, num(det.total - det.good)));
    s.appendChild(el('text', { x: L + iw, y: y0 - 8, 'text-anchor': 'end',
      'font-size': 10.5, 'letter-spacing': '.07em',
      style: `fill:${V('muto')}` }, 'SCARTI'));
  }

  // tacca: dove cadrebbe il confine buoni/scarti alla qualita' di base
  if (RIF.quality.v != null) {
    const xr = L + RIF.quality.v * iw;
    s.appendChild(el('line', { x1: xr, x2: xr, y1: y0 - 4, y2: y0 + bh + 12,
      'stroke-width': 1.5, 'stroke-dasharray': '5 4', style: `stroke:${V('rif')}` }));
    s.appendChild(el('text', { x: Math.min(xr + 5, L + iw), y: y0 + bh + 24,
      'text-anchor': xr > L + iw - 110 ? 'end' : 'start', 'font-size': 10.5,
      style: `fill:${V('rif')}` }, `base ${pct(RIF.quality.v)} · ${RIF.quality.et}`));
  }

  const mira = el('line', { x1: 0, x2: 0, y1: y0 - 6, y2: y0 + bh + 6,
    'stroke-width': 1, style: `stroke:${V('rif')}`, visibility: 'hidden' });
  s.appendChild(mira);
  const zone = [{ x: L + wg / 2, et: 'buoni', v: det.good },
                { x: L + wg + (iw - wg) / 2, et: 'scarti', v: det.total - det.good }];
  agganciaZone(s, {
    n: 2, verticale: true, mira,
    centro: (i) => ({ x: zone[i].x, y: y0 + bh / 2 }),
    testo: (i) => {
      const sc = Q - RIF.quality.v;
      return `<span class="v">${num(zone[i].v)}</span> `
        + `<span class="m">cicli · ${zone[i].et}</span><br>`
        + `<span class="m">${pct(zone[i].v / det.total)} di ${num(det.total)}</span><br>`
        + `<span class="${sc < 0 ? 'f' : 'm'}">qualità ${pct(Q)} · `
        + `${sc >= 0 ? '+' : '−'}${num(Math.abs(sc) * 100, 1)} punti dalla base `
        + `${pct(RIF.quality.v)}</span>`;
    },
  });
  s.setAttribute('aria-label', `${num(det.good)} cicli buoni su ${num(det.total)}: `
    + `qualità ${pct(Q)}, base ${pct(RIF.quality.v)}`);

  cifre('cifre-q', [
    ['Buoni', num(det.good), ''],
    ['Scarti', num(det.total - det.good), ''],
    ['Cicli', num(det.total), ''],
  ]);
}

/* ==================================================================
   ADESSO — stato OMAC, eta' del dato, e le tre componenti sul TURNO.
   MACCHINA mostra l'OEE del turno; qui si vede di cosa e' fatto anche
   quello. In f-oee-degradato il turno non e' calcolabile: arco e barre
   tratteggiati, e il turno precedente accanto. Un valore nullo non e'
   uno zero e non e' rosso.
   ================================================================== */
function disegnaAdesso(stato, oeeG, t, valvole) {
  const inMarcia = IN_MARCIA.has(stato.label);
  const bn = document.getElementById('stato-grande');
  bn.className = 'stato-grande' + (inMarcia ? '' : ' fermo');
  bn.replaceChildren(
    span('n', String(stato.label).toUpperCase(), ''),
    span('d', `OMAC ${stato.state}`, ''));

  const eta = etaDato(valvole, oeeG.at);
  const ts = Object.values(valvole.valves || {})
    .map(v => v.last_prediction && v.last_prediction.prediction_ts)
    .filter(Boolean).sort();
  const ultimo = ts.length ? ts[ts.length - 1] : null;
  const rotta = inMarcia && eta && eta.secondi > DATO_ATTESO_S;
  const rd = document.getElementById('riga-dato');
  rd.className = 'riga-dato' + (rotta ? ' vecchio' : '');
  rd.replaceChildren();
  if (rotta) { const p = document.createElement('span'); p.className = 'pallino';
               rd.appendChild(p); }
  rd.append(span('et', 'Ultimo dato', ''), span('or', ultimo ? ora(ultimo) : '—', ''),
            span('', eta ? `${eta.testo} fa` : '', ''));

  // --- le quattro grandezze del turno ---
  //
  // A macchina non in marcia la DISPONIBILITA' di turno mostra il proprio
  // numero senza prendere tinta. Non e' un'esenzione di comodo: con queste
  // route non e' possibile distinguere un fermo pianificato da un guasto
  // (in e-macchina-ferma il turno ha 6.120 s di marcia su 28.800 pianificati,
  // quindi il tempo ERA pianificato e il calo e' reale, ma nulla dice se sia
  // voluto). Tingere sarebbe emettere un verdetto di gravita' che i dati non
  // sostengono; lo stato OMAC e l'eta' del dato dicono gia', con certezza,
  // che la macchina non sta girando. Quando la macchina E' in marcia la
  // regola vale intera: una disponibilita' bassa a macchina in marcia si
  // tinge come prima, ed e' il caso in cui il segnale serve davvero.
  const s = svg('g-turno');
  const W = 272, L = 8, R = 8, iw = W - L - R;
  const inMarciaOra = stato.label === 'Running';
  const righe = [
    ['OEE turno',      t.oee,          RIF.oee,          false],
    ['Disponibilità',  t.availability, RIF.availability, !inMarciaOra],
    ['Prestazione',    t.performance,  RIF.performance,  false],
    ['Qualità',        t.quality,      RIF.quality,      false],
  ];
  const bh = 24, passo = 96;
  let y = 30;
  const zone = [];
  for (const [lab, v, rif, senzaTinta] of righe) {
    const ti = senzaTinta ? 'dato' : tinta(v, rif);
    s.appendChild(el('text', { x: L, y: y - 7, 'font-size': 10.5,
      'letter-spacing': '.07em', style: `fill:${V('muto')}` }, lab.toUpperCase()));
    s.appendChild(el('rect', { x: L, y, width: iw, height: bh,
      style: `fill:${V('traccia')}` }));
    if (v == null) {
      s.appendChild(el('rect', { x: L, y, width: iw, height: bh, fill: 'none',
        'stroke-width': 1, 'stroke-dasharray': '3 3', style: `stroke:${V('banda')}` }));
      s.appendChild(el('text', { x: L + 7, y: y + bh - 6, 'font-size': 12,
        style: `fill:${V('muto')}` }, 'non calcolabile'));
    } else {
      s.appendChild(el('rect', { x: L, y, width: Math.max(2, (v / FONDO) * iw),
        height: bh, rx: 1, style: `fill:${V(ti)};fill-opacity:${ti === 'dato' ? .8 : 1}` }));
      s.appendChild(el('text', { x: L + iw, y: y - 7, 'text-anchor': 'end',
        'font-size': 13, 'font-weight': 600,
        style: `fill:${V(tintaNum(ti))}` }, pct(v)));
    }
    if (rif.v != null) {
      const xr = L + (rif.v / FONDO) * iw;
      s.appendChild(el('line', { x1: xr, x2: xr, y1: y - 3, y2: y + bh + 3,
        'stroke-width': 1.5, 'stroke-dasharray': '4 3', style: `stroke:${V('rif')}` }));
      s.appendChild(el('text', { x: Math.min(xr + 4, L + iw), y: y + bh + 14,
        'text-anchor': xr > L + iw - 96 ? 'end' : 'start', 'font-size': 10,
        style: `fill:${V('rif')}` }, `rif. ${pct(rif.v)} · ${rif.et}`));
    }
    zone.push({ y: y + bh / 2, lab, v, rif, senzaTinta });
    y += passo;
  }

  // turno precedente: un confronto a una variabile sola
  const prev = (t.prev || {}).oee;
  y += 2;
  s.appendChild(el('line', { x1: L, x2: L + iw, y1: y - 12, y2: y - 12,
    'stroke-width': 1, style: `stroke:${V('traccia')}` }));
  s.appendChild(el('text', { x: L, y: y + 4, 'font-size': 10.5,
    'letter-spacing': '.07em', style: `fill:${V('muto')}` }, 'OEE TURNO PRECEDENTE'));
  const tp = tinta(prev, RIF.oee);
  s.appendChild(el('text', { x: L + iw, y: y + 5, 'text-anchor': 'end',
    'font-size': 15, 'font-weight': 600,
    style: `fill:${V(prev == null ? 'muto' : tintaNum(tp))}` },
    prev == null ? 'non disponibile' : pct(prev)));
  if (prev != null && t.oee != null) {
    const dpp = (t.oee - prev) * 100;
    s.appendChild(el('text', { x: L + iw, y: y + 21, 'text-anchor': 'end',
      'font-size': 11, style: `fill:${V('muto')}` },
      `${dpp >= 0 ? '+' : '−'}${num(Math.abs(dpp), 1)} punti sul turno`));
  } else if (prev != null && t.oee == null) {
    const qd = t.quality_detail || {};
    s.appendChild(el('text', { x: L + iw, y: y + 21, 'text-anchor': 'end',
      'font-size': 11, style: `fill:${V('muto')}` },
      `${num(qd.total)} cicli nel turno in corso`));
  }

  const mira = el('line', { x1: L - 2, x2: L + iw + 2, y1: 0, y2: 0, 'stroke-width': 1,
    style: `stroke:${V('rif')}`, visibility: 'hidden' });
  s.appendChild(mira);
  agganciaZone(s, {
    n: zone.length, verticale: false, mira,
    centro: (i) => ({ x: L + iw / 2, y: zone[i].y }),
    testo: (i) => {
      const z = zone[i];
      if (z.v == null) return `<span class="v">non calcolabile</span> `
        + `<span class="m">${z.lab.toLowerCase()}</span><br>`
        + `<span class="m">0 cicli nella finestra turno</span>`;
      const sc = z.rif.v == null ? null : z.v - z.rif.v;
      return `<span class="v">${pct(z.v)}</span> `
        + `<span class="m">${z.lab.toLowerCase()} · turno</span>`
        + (sc == null ? '' : `<br><span class="${sc < 0 && !z.senzaTinta ? 'f' : 'm'}">`
           + `${sc >= 0 ? '+' : '−'}${num(Math.abs(sc) * 100, 1)} punti dal `
           + `riferimento ${pct(z.rif.v)} · ${z.rif.et}</span>`);
    },
  });
  s.setAttribute('aria-label', `Turno in corso: OEE ${t.oee == null ? 'non calcolabile'
    : pct(t.oee)}, disponibilità ${pct(t.availability)}, prestazione `
    + `${pct(t.performance)}, qualità ${pct(t.quality)}`);
}

/* ---------------- utilita' ---------------- */
function span(cls, testo, stile) {
  const e = document.createElement('span');
  if (cls) e.className = cls;
  e.textContent = testo;
  if (stile) e.setAttribute('style', stile);
  return e;
}
function cifre(id, righe) {
  const dl = document.getElementById(id);
  dl.replaceChildren();
  for (const [k, v, u] of righe) {
    const d = document.createElement('div'); d.className = 'riga';
    const dt = document.createElement('dt'); dt.textContent = k;
    const dd = document.createElement('dd'); dd.textContent = v;
    if (u) { const sp = document.createElement('span'); sp.className = 'u';
             sp.textContent = u; dd.appendChild(sp); }
    d.append(dt, dd); dl.appendChild(d);
  }
}

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
  try { localStorage.setItem('tema-v7oee', nuovo); } catch (e) {}
  aggiornaBtn();
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', aggiornaBtn);
aggiornaBtn();

/* ---- la navigazione conserva lo scenario ---- */
collegaNav();

main().catch((e) => {
  console.error(e);
  const b = document.getElementById('box-casc');
  if (b) b.querySelector('.box-corpo').textContent = 'errore di caricamento: ' + e.message;
});
