// TEMPO — i due mesi sono sempre in vista, ci si sposta dentro trascinando.
// Sorgente unica: le route servite dal proxy sulla 8078 (specchio di
// pipeline/api.py). Nessun numero che non venga da li'.

import { scenarioCorrente, pct, num, collegaNav } from '/comune/dati.js';

/* ---- la navigazione conserva lo scenario ---- */
collegaNav();

const NS = 'http://www.w3.org/2000/svg';
const $ = (s) => document.querySelector(s);

// La striscia porta l'OEE di macchina. Sta sempre in vista, si trascina, si
// allunga dai bordi: e' la spina dorsale della pagina.

async function get(rotta) {
  const r = await fetch(`/api/${scenarioCorrente()}/${rotta}`, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${rotta} -> HTTP ${r.status}`);
  return r.json();
}

const ms = (iso) => new Date(iso).getTime();
const ORA = 3600e3, GIORNO = 24 * ORA;

const MESI = ['gen', 'feb', 'mar', 'apr', 'mag', 'giu',
              'lug', 'ago', 'set', 'ott', 'nov', 'dic'];
function fG(t) { const d = new Date(t); return `${d.getUTCDate()} ${MESI[d.getUTCMonth()]}`; }
function fO(t) {
  const d = new Date(t);
  return `${fG(t)} ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
}
function fDurata(x) {
  if (x >= 2 * GIORNO) return `${Math.round(x / GIORNO)} giorni`;
  if (x >= GIORNO) return `${(x / GIORNO).toFixed(1).replace('.', ',')} giorni`;
  return `${Math.round(x / ORA)} h`;
}
const iso = (t) => new Date(Math.round(t / 60000) * 60000).toISOString().replace('.000', '');

function el(p, tag, attr, testo) {
  const e = document.createElementNS(NS, tag);
  for (const k in attr) if (attr[k] !== undefined && attr[k] !== null) e.setAttribute(k, attr[k]);
  if (testo !== undefined) e.textContent = testo;
  p.appendChild(e);
  return e;
}
const vuota = (e) => { while (e.firstChild) e.removeChild(e.firstChild); };

// Il riquadro decide le dimensioni, non il contrario: viewBox in pixel reali,
// cosi' il disegno riempie la sua area e il testo non si deforma mai.
function misura(svg) {
  const r = svg.getBoundingClientRect();
  const w = Math.max(120, Math.round(r.width)), h = Math.max(60, Math.round(r.height));
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  return [w, h];
}

// ---------------------------------------------------------------- il colore
// Regola del LESSICO: la tinta la prende solo cio' che ha una gravita'.
function tinta(v, rif) {
  if (v == null || !rif) return 'var(--dato)';
  const d = rif.v - v;
  if (d <= (rif.morta || 0)) {
    if (rif.alto && v - rif.v > rif.alto) return 'var(--sev2)';
    return 'var(--dato)';
  }
  if (d <= rif.tol) return 'var(--sev1)';
  if (d <= 2 * rif.tol) return 'var(--sev2)';
  if (d <= 3 * rif.tol) return 'var(--sev3)';
  return 'var(--sev4)';
}
const grave = (t) => t === 'var(--sev3)' || t === 'var(--sev4)';

function mediana(a) {
  const v = a.filter((x) => x != null).slice().sort((x, y) => x - y);
  if (!v.length) return null;
  const m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}
function sigma(a) {
  const v = a.filter((x) => x != null);
  if (v.length < 2) return null;
  const m = v.reduce((s, x) => s + x, 0) / v.length;
  return Math.sqrt(v.reduce((s, x) => s + (x - m) ** 2, 0) / (v.length - 1));
}

// ------------------------------------------------------------------- stato
const S = {
  t0: null, t1: null,          // estremi della run
  tutto: [],                   // serie dell'intera run (la striscia)
  rif: {},                     // riferimenti con provenienza
  finBase: null,               // finestra di base dichiarata dall'API
  da: null, a: null,           // la finestra trascinabile
  dett: null,                  // serie del periodo in finestra
  valvBase: null,              // base per valvola
  valvSerie: null,             // qualita' per valvola nel periodo
  valvStato: 'attesa',         // attesa | ok | assente
  dettValv: null,              // valvola aperta nel pannello
  prof: null,                  // profilo del ciclo: { chiave, stato, dati }
  metrica: 'q',                // quale metro guardano le 35: 'q' o 't'
  tempo: null,                 // tempo di riempimento delle 35: { chiave, dati }
};

// ============================================================ avvio
avvia().catch((e) => { window.__errTB = String(e) + '|' + e.stack; console.error('TB', e); });

async function avvia() {
  montaTema();
  montaPannello();
  const base = await get('valves/baseline').catch(() => null);
  if (base && base.window) S.finBase = { da: ms(base.window.start), a: ms(base.window.end) };

  // La serie senza `from`/`to` costa 6-9 s (istante non allineato all'ora).
  // Gli estremi della run si leggono da due chiamate da 0,1 s e la serie
  // dell'intero storico, chiesta esplicita e allineata, ne costa 2.
  const ora = await get('machine/oee?window=day');
  const fine = ms(ora.at);
  const inizio = S.finBase ? S.finBase.da : fine - 60 * GIORNO;
  const all = (t) => Math.floor(t / ORA) * ORA;
  const s = await get('machine/oee/series?windows=day'
    + `&from=${encodeURIComponent(iso(all(inizio)))}&to=${encodeURIComponent(iso(all(fine) + ORA))}`);
  S.tutto = (s.day_ridotto || []).map((p) => ({
    t: ms(p.at), a: p.availability, p: p.performance, q: p.quality, o: p.oee,
  }));
  if (!S.tutto.length) return;
  // La finestra di 24 h dei primi punti sborda l'inizio dei dati: disponibilita'
  // 0,96 non e' un miglioramento, e' un bordo. Si scartano invece di scalarci sopra.
  S.primo = s.__meta && s.__meta.primo_ciclo_reale ? ms(s.__meta.primo_ciclo_reale) : S.tutto[0].t;
  const bordo = S.tutto.filter((x) => x.t < S.primo + GIORNO).length;
  if (bordo && S.tutto.length - bordo > 24) S.tutto = S.tutto.slice(bordo);
  S.bordo = bordo;
  S.t0 = S.tutto[0].t;
  S.t1 = S.tutto[S.tutto.length - 1].t;
  S.fine = s.__meta && s.__meta.at_corrente ? ms(s.__meta.at_corrente) : S.t1;

  // riferimenti: mediana della finestra di base dichiarata da /valves/baseline,
  // banda ±1σ misurata sulla stessa finestra. Provenienza scritta a schermo.
  const fb = S.finBase || { da: S.t0, a: S.t0 + 11 * GIORNO };
  const b = S.tutto.filter((x) => x.t >= fb.da && x.t <= fb.a);
  const mk = (k, minimo, morta, alto) => {
    const v = mediana(b.map((x) => x[k]));
    const sd = sigma(b.map((x) => x[k]));
    // 1σ su una base stabile vale pochi decimi di punto: userebbe la rampa
    // di gravita' tutta dentro il rumore. Il minimo e' dichiarato a schermo.
    return { v, tol: Math.max(sd || 0, minimo), morta: morta || 0, alto };
  };
  S.rif = { o: mk('o', .02), a: mk('a', .02), p: mk('p', .004, .004, .004), q: mk('q', .02) };
  S.rifProv = `base della run · ${fG(fb.da)} – ${fG(fb.a)}`;
  S.bandaProv = `banda ±1σ, minimo ±2 punti`;

  // finestra iniziale: gli ultimi 14 giorni
  S.a = S.t1;
  S.da = Math.max(S.t0, S.t1 - 14 * GIORNO);

  // La serie per valvola sull'intera run non blocca il primo disegno: serve a
  // coprire l'attesa durante il trascinamento, non alla prima schermata.
  const pVs = caricaVsTutto();
  disegnaStriscia();
  legaStriscia();
  await aggiorna();
  await pVs;
}

// ============================================================ il periodo
let tmr = null;
function muovi(da, a, subito) {
  const minW = 6 * ORA;
  let l = da, r = a;
  if (r - l < minW) r = l + minW;
  if (l < S.t0) { r += S.t0 - l; l = S.t0; }
  if (r > S.t1) { l -= r - S.t1; r = S.t1; }
  l = Math.max(l, S.t0); r = Math.min(r, S.t1);
  S.da = l; S.a = r;
  disegnaStriscia();
  titoli();
  // Il resto della schermata segue la finestra SUBITO, con i punti dei due mesi
  // gia' in memoria. La richiesta che affina il passo arriva dopo e sostituisce.
  S.dett = S.tutto.filter((x) => x.t >= S.da && x.t <= S.a);
  if (S.dett.length > 1) { disegnaOee(); disegnaComp(); }
  valvoleLocali();
  clearTimeout(tmr);
  tmr = setTimeout(aggiorna, subito ? 0 : 260);
}

// Anche le 35 valvole seguono la finestra SUBITO: si ritaglia la serie per
// valvola dell'intera run, gia' in memoria a grana giorno. Zero richieste,
// zero attesa. La grana fine arriva dopo e sostituisce. Dove il dato locale
// non copre la finestra, il riquadro dice «non misurata»: non si inventa nulla.
function valvoleLocali() {
  if (!S.vsTutto || !S.vsTutto.size) return;
  S.valvSerie = S.vsTutto;
  S.valvStato = 'ok';
  document.body.classList.remove('valv-assenti');
  disegnaValvole();
  disegnaPop();
  disegnaDettaglio();
}

// Trascinando si accavallano piu' richieste: quella che torna in ritardo
// disegnerebbe i punti di un periodo dentro l'asse di un altro. Vince
// l'ultima chiesta, le altre si buttano.
let gen = 0;
async function aggiorna() {
  const mio = ++gen;
  const q = `from=${encodeURIComponent(iso(S.da))}&to=${encodeURIComponent(iso(S.a))}`;
  // Le due richieste sono indipendenti e costano molto diverso (la serie OEE
  // ~460 ms, quella per valvola ~30 ms): partono INSIEME e ciascuna disegna
  // quando torna. In coda, le valvole aspettavano un dato che non usano.
  const pv = valvole(q, mio).catch(() => {});
  // Il tempo di riempimento delle 35 costa ~110 ms sull'intera run: parte
  // insieme agli altri e disegna per conto suo appena torna.
  const pt = caricaTempo(mio).catch(() => {});
  const s = await get(`machine/oee/series?windows=day&${q}`).catch(() => null);
  if (mio !== gen) return;
  const fine = s && s.day_ridotto && s.day_ridotto.length
    ? s.day_ridotto.map((p) => ({ t: ms(p.at), a: p.availability, p: p.performance, q: p.quality, o: p.oee }))
    : S.tutto.filter((x) => x.t >= S.da && x.t <= S.a);
  S.dett = fine.filter((x) => x.t >= S.primo + GIORNO);
  if (S.dett.length >= 2) { titoli(); disegnaOee(); disegnaComp(); }
  await pv; await pt;
}

function titoli() {
  const per = `<span class="per">${fG(S.da)} → ${fO(S.a)} · <b>${fDurata(S.a - S.da)}</b></span>`;
  $('#tit-oee').innerHTML = `OEE &nbsp; ${per}`;
  $('#tit-valv').innerHTML = `Qualità per valvola · ciascuna contro la propria base &nbsp; ${per}`;
  $('#tit-pop').innerHTML = `Le 35 sulla stessa scala &nbsp; ${per}`;
  // «dati fermi» era scritto qui: adesso lo dice la coda tratteggiata della
  // striscia, e questa riga porta solo il periodo che copre.
  $('#tit-str').innerHTML = `Due mesi &nbsp; <span class="per">${fG(S.t0)} → ${fO(S.fine)}</span>`;
}

// ============================================================ la striscia
let SW = 1490, SH = 78;
// `t` lascia posto all'intestazione della striscia, che dice cosa porta.
const SM = { l: 52, r: 60, t: 28, b: 16 };
// L'asse della striscia arriva OLTRE l'ultimo dato: quel margine e' il posto
// dove si vede che il tracciato finisce (vedi `codaMorta`). La scala tiene
// conto dell'allungamento, il trascinamento no: `muovi` resta chiuso in
// [t0, t1], quindi la finestra non puo' entrare nel vuoto.
const SBORDO = 0.055;
const stFine = () => S.t1 + (S.t1 - S.t0) * SBORDO;
// ...e ANCHE prima del primo dato confrontabile, quando l'inizio della run e'
// stato scartato: quel tratto occupa il posto che gli spetta sull'asse, reso
// con lo stesso tratteggio della coda (vedi `bordoMorto`). Anche qui la scala
// si allunga e il trascinamento no: `muovi` resta chiuso in [t0, t1].
const stIniz = () => (S.bordo ? S.primo : S.t0);
const sx = (t) => SM.l + (t - stIniz()) / (stFine() - stIniz()) * (SW - SM.l - SM.r);
const st = (x) => stIniz() + (x - SM.l) / (SW - SM.l - SM.r) * (stFine() - stIniz());

// La serie per valvola sull'intera run, a grana giorno. Si carica sempre:
// e' la copia locale che fa muovere i 35 riquadri MENTRE si trascina, senza
// aspettare la rete. La richiesta a grana fine arriva dopo e la sostituisce.
async function caricaVsTutto() {
  const j = await get('valves/quality/series'
    + `?from=${encodeURIComponent(iso(S.t0))}`
    + `&to=${encodeURIComponent(iso(S.fine + 60000))}&grain=day`).catch(() => null);
  if (!j) return;
  const m = normalizza(j);
  if (m.size) S.vsTutto = m;
}

function disegnaStriscia() {
  const g = $('#g-str');
  [SW, SH] = misura(g);
  vuota(g);
  const x0 = SM.l, x1 = SW - SM.r, y0 = SM.t, y1 = SH - SM.b, h = y1 - y0;
  el(g, 'rect', { x: x0, y: y0, width: x1 - x0, height: h, fill: 'var(--traccia)' });
  corpoA(g, x0, x1, y0, y1);

  // il velo su cio' che sta fuori dalla finestra
  const a = sx(S.da), b = sx(S.a);
  const velo = { fill: 'var(--pagina)', opacity: .62 };
  el(g, 'rect', { x: x0, y: y0, width: Math.max(0, a - x0), height: h, ...velo });
  el(g, 'rect', { x: b, y: y0, width: Math.max(0, x1 - b), height: h, ...velo });
  // dopo il velo: la fine del dato non si attenua quando la finestra e' altrove
  codaMorta(g, x0, x1, y0, y1);
  testaMorta(g, x0, x1, y0, y1);
  el(g, 'rect', {
    x: a, y: y0 - 3, width: Math.max(2, b - a), height: h + 6,
    fill: 'none', stroke: 'var(--ink)', 'stroke-width': 1.6, class: 'fin',
  });
  for (const hx of [a, b]) {
    el(g, 'rect', { x: hx - 3.5, y: y0 - 3, width: 7, height: h + 6, fill: 'var(--ink)', opacity: .85 });
    el(g, 'line', { x1: hx, x2: hx, y1: y0 + 3, y2: y1 - 3, stroke: 'var(--sup)', 'stroke-width': 1 });
  }

  // il calendario sotto: un segno per settimana, il mese scritto
  let t = Date.UTC(new Date(S.t0).getUTCFullYear(), new Date(S.t0).getUTCMonth(), new Date(S.t0).getUTCDate());
  for (; t <= S.t1; t += GIORNO) {
    const d0 = new Date(t);
    const primo = d0.getUTCDate() === 1;
    if (!primo && d0.getUTCDay() !== 1) continue;
    el(g, 'line', { x1: sx(t), x2: sx(t), y1: y1, y2: y1 + (primo ? 5 : 3), stroke: 'var(--muto)', 'stroke-width': 1 });
    if (primo) el(g, 'text', { x: sx(t) + 3, y: SH - 4, fill: 'var(--muto)', 'font-size': 10 }, fG(t));
  }
  el(g, 'text', { x: x0, y: SH - 4, fill: 'var(--muto)', 'font-size': 10 }, fG(stIniz()));
  el(g, 'text', { x: sx(S.fine) - 4, y: SH - 4, fill: 'var(--muto)', 'font-size': 10,
                  'text-anchor': 'end' }, fO(S.fine));
  // in fondo alla finestra, non in cima: sopra ci passa la tacca del riferimento
  el(g, 'text', { x: (a + b) / 2, y: y1 - 5, fill: 'var(--ink)', 'font-size': 10.5,
                  'text-anchor': 'middle', 'font-weight': 600 }, fDurata(S.a - S.da));
}

// ---------------------------------------------------------------- forma `a`
// La striscia porta l'andamento di MACCHINA. Il dominio verticale si stringe
// sui dati (piu' il riferimento) invece di allargarsi: su sessanta giorni l'OEE
// si muove di tre punti, e in una scala larga tre punti sono una riga piatta.
// Il divario dal riferimento e' riempito, cosi' la deriva si legge come un cuneo
// che si apre e non come una linea da inseguire.
function corpoA(g, x0, x1, y0, y1) {
  const h = y1 - y0;
  const vs = S.tutto.map((p) => p.o).filter((v) => v != null);
  if (!vs.length) return;
  const dl = Math.min(...vs), dh = Math.max(...vs);
  let lo = Math.min(dl, S.rif.o.v), hi = Math.max(dh, S.rif.o.v);
  const mar = Math.max((hi - lo) * 0.14, 0.004);
  lo -= mar; hi += mar;
  const y = (v) => y1 - (v - lo) / (hi - lo) * h;
  const yr = y(S.rif.o.v);

  let area = '', linea = '', giu = false, ultimo = null;
  for (const p of S.tutto) {
    if (p.o == null) { giu = false; continue; }
    const X = sx(p.t).toFixed(1), Y = y(p.o).toFixed(1);
    if (!giu) area += `${area ? 'Z' : ''}M${X},${yr.toFixed(1)}`;
    area += `L${X},${Y}`;
    linea += `${giu ? 'L' : 'M'}${X},${Y}`;
    giu = true; ultimo = p;
  }
  if (ultimo) area += `L${sx(ultimo.t).toFixed(1)},${yr.toFixed(1)}Z`;
  el(g, 'path', { d: area, fill: 'var(--dato)', opacity: .18, stroke: 'none' });
  el(g, 'line', { x1: x0, x2: sx(S.t1), y1: yr, y2: yr, stroke: 'var(--rif)',
                  'stroke-width': 1.5, 'stroke-dasharray': '5 4' });
  el(g, 'path', { d: linea, fill: 'none', stroke: 'var(--dato)', 'stroke-width': 1.8,
                  'stroke-linejoin': 'round' });

  el(g, 'text', { x: x0 - 6, y: yr + 3, fill: 'var(--rif)', 'font-size': 10,
                  'text-anchor': 'end' }, pct(S.rif.o.v));
  for (const v of [lo + (hi - lo) * .06, hi - (hi - lo) * .06]) {
    if (Math.abs(y(v) - yr) < 11) continue;
    el(g, 'text', { x: x0 - 6, y: y(v) + 3, fill: 'var(--muto)', 'font-size': 9.5,
                    'text-anchor': 'end' }, pct(v));
  }
  el(g, 'text', { x: x0, y: y0 - 12, fill: 'var(--muto)', 'font-size': 9.5,
                  'letter-spacing': '.08em' },
     `OEE DI MACCHINA · riferimento ${pct(S.rif.o.v)} · ${S.rifProv}`);
  S.strY = { y, lo, hi, yr };
}

// ------------------------------------------------------ dove finisce il dato
// La domanda «cio' che vedo ora e' il PLC live?» dev'essere chiusa da un segno,
// non da una frase. L'asse della striscia sborda oltre l'ultimo ciclo e quel
// margine resta vuoto, tratteggiato e sbarrato: il tracciato si interrompe
// contro un fondo invece di arrivare al bordo del riquadro, che e' come si
// legge un registro che non avanza piu'.
// La stessa cosa vale in TESTA: le prime 24 h della run hanno la finestra
// dell'OEE a cavallo dell'inizio dei dati, quindi non sono confrontabili con il
// resto e sono fuori dai calcoli. Era scritto a parole sotto l'asse; adesso quel
// tratto sta sull'asse, tratteggiato e chiuso dalla stessa sbarra. Un solo
// segno per un solo concetto: qui il registro confrontabile non c'e'.
function tratteggio(g) {
  if (g.querySelector('#bordo-morto')) return;
  const defs = el(g, 'defs', {});
  const p = el(defs, 'pattern', {
    id: 'bordo-morto', width: 6, height: 6, patternUnits: 'userSpaceOnUse',
    patternTransform: 'rotate(45)',
  });
  el(p, 'rect', { width: 6, height: 6, fill: 'var(--sup)' });
  el(p, 'line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: 'var(--bordo)', 'stroke-width': 1.4 });
}

function codaMorta(g, x0, x1, y0, y1) {
  const xf = sx(S.fine);
  if (xf >= x1 - 1) return;
  tratteggio(g);
  el(g, 'rect', { x: xf, y: y0, width: x1 - xf, height: y1 - y0, fill: 'url(#bordo-morto)' });
  el(g, 'line', { x1: xf, x2: xf, y1: y0 - 4, y2: y1 + 4, stroke: 'var(--ink)', 'stroke-width': 2 });
}

function testaMorta(g, x0, x1, y0, y1) {
  if (!S.bordo) return;
  const xi = sx(S.t0);
  if (xi <= x0 + 1) return;
  tratteggio(g);
  el(g, 'rect', { x: x0, y: y0, width: xi - x0, height: y1 - y0, fill: 'url(#bordo-morto)' });
  el(g, 'line', { x1: xi, x2: xi, y1: y0 - 4, y2: y1 + 4, stroke: 'var(--ink)', 'stroke-width': 2 });
}

function legaStriscia() {
  const g = $('#g-str');
  let modo = null, presa = 0;
  const pos = (ev) => {
    const r = g.getBoundingClientRect();
    return SM.l + ((ev.clientX - r.left) / r.width * SW - SM.l);
  };
  g.addEventListener('pointerdown', (ev) => {
    const x = pos(ev), a = sx(S.da), b = sx(S.a);
    if (Math.abs(x - a) < 7) modo = 'sx';
    else if (Math.abs(x - b) < 7) modo = 'dx';
    else if (x > a && x < b) { modo = 'muovi'; presa = st(x) - S.da; }
    else { const w = S.a - S.da; muovi(st(x) - w / 2, st(x) + w / 2); modo = 'muovi'; presa = w / 2; }
    g.setPointerCapture(ev.pointerId);
    g.classList.add('trascino');
    ev.preventDefault();
  });
  g.addEventListener('pointermove', (ev) => {
    if (!modo) { suggStriscia(ev, pos(ev)); return; }
    const t = st(pos(ev));
    if (modo === 'sx') muovi(Math.min(t, S.a - 6 * ORA), S.a);
    else if (modo === 'dx') muovi(S.da, Math.max(t, S.da + 6 * ORA));
    else { const w = S.a - S.da; muovi(t - presa, t - presa + w); }
  });
  const su = () => { modo = null; g.classList.remove('trascino'); };
  g.addEventListener('pointerup', su);
  g.addEventListener('pointercancel', su);
  g.addEventListener('pointerleave', () => $('#tip').hidden = true);
  g.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const c = st(pos(ev)), k = ev.deltaY > 0 ? 1.25 : 0.8;
    muovi(c - (c - S.da) * k, c + (S.a - c) * k);
  }, { passive: false });
  g.addEventListener('keydown', (ev) => {
    const w = S.a - S.da, p = Math.max(ORA, w / 8);
    const k = ev.key;
    const passo = ev.shiftKey ? w : p;
    if (k === 'ArrowLeft') muovi(S.da - passo, S.a - passo);
    else if (k === 'ArrowRight') muovi(S.da + passo, S.a + passo);
    else if (k === 'ArrowUp') muovi(S.da + w * .15, S.a - w * .15);
    else if (k === 'ArrowDown') muovi(S.da - w * .2, S.a + w * .2);
    else if (k === 'Home') muovi(S.t0, S.t0 + w);
    else if (k === 'End') muovi(S.t1 - w, S.t1);
    else return;
    ev.preventDefault();
  });
}

function suggStriscia(ev, x) {
  const t = st(x);
  if (t < S.t0 || t > S.t1) { $('#tip').hidden = true; return; }
  let p = S.tutto[0];
  for (const q of S.tutto) if (Math.abs(q.t - t) < Math.abs(p.t - t)) p = q;
  const d = p.o == null ? null : p.o - S.rif.o.v;
  sugg(ev, `<span class="v">${pct(p.o)}</span> OEE<br><span class="m">${fO(p.t)}</span>`
    + (d == null ? '' : `<br><span class="${d < -S.rif.o.tol ? 'f' : 'm'}">`
       + `${d >= 0 ? '+' : '−'}${pct(Math.abs(d))} sul riferimento</span>`));
}

// ============================================================ il grafico
function grafico(g, cfg) {
  const { x0, x1, y0, y1, pts, rif, chiave, titolo, tint } = cfg;
  const h = y1 - y0;
  const vs = pts.map((p) => p[chiave]).filter((v) => v != null);
  if (!vs.length) {
    el(g, 'text', { x: (x0 + x1) / 2, y: (y0 + y1) / 2, fill: 'var(--muto)', 'font-size': 12,
                    'text-anchor': 'middle' }, 'serie non disponibile');
    return null;
  }
  // dominio: contiene banda e dati, con una quota minima ai dati
  let dl = Math.min(...vs), dh = Math.max(...vs);
  let lo = Math.min(dl, rif.v - rif.tol), hi = Math.max(dh, rif.v + rif.tol);
  const dsp = dh - dl, sp = hi - lo;
  if (dsp < sp * 0.16) {
    const need = Math.max(dsp / 0.16, (rif.tol * 2) / 0.6, 0.01);
    const c = (dh + dl) / 2;
    lo = Math.min(rif.v, c - need / 2); hi = Math.max(rif.v, c + need / 2);
  }
  const mar = (hi - lo) * 0.08; lo -= mar; hi += mar;
  const X = (t) => x0 + (t - S.da) / Math.max(1, S.a - S.da) * (x1 - x0);
  const Y = (v) => y1 - (v - lo) / (hi - lo) * h;

  el(g, 'rect', { x: x0, y: y0, width: x1 - x0, height: h, fill: 'var(--sup-2)' });
  // 1 banda
  el(g, 'rect', {
    x: x0, y: Y(rif.v + rif.tol), width: x1 - x0,
    height: Math.max(1, Y(rif.v - rif.tol) - Y(rif.v + rif.tol)), fill: 'var(--traccia)',
  });
  // 2 tacca
  el(g, 'line', { x1: x0, x2: x1, y1: Y(rif.v), y2: Y(rif.v), stroke: 'var(--rif)',
                  'stroke-width': 1.5, 'stroke-dasharray': '5 4' });
  // scala verticale
  for (const v of [lo + (hi - lo) * .04, hi - (hi - lo) * .04]) {
    el(g, 'text', { x: x0 - 5, y: Y(v) + 3, fill: 'var(--muto)', 'font-size': 10,
                    'text-anchor': 'end' }, pct(v));
  }
  // giorni sull'asse
  const passo = (S.a - S.da) > 20 * GIORNO ? 7 * GIORNO : (S.a - S.da) > 4 * GIORNO ? GIORNO : 6 * ORA;
  const d0 = Math.ceil(S.da / passo) * passo;
  for (let t = d0; t <= S.a; t += passo) {
    el(g, 'line', { x1: X(t), x2: X(t), y1: y0, y2: y1, stroke: 'var(--bordo)', 'stroke-width': .5, opacity: .5 });
    if (cfg.assex) el(g, 'text', { x: X(t), y: y1 + 12, fill: 'var(--muto)', 'font-size': 10,
                                   'text-anchor': 'middle' }, passo >= GIORNO ? fG(t) : fO(t).slice(-5));
  }
  // 3 la traiettoria, sopra tutto
  let d = '', giu = false;
  for (const p of pts) {
    const v = p[chiave];
    if (v == null) { giu = false; continue; }
    d += `${giu ? 'L' : 'M'}${X(p.t).toFixed(1)},${Y(v).toFixed(1)}`;
    giu = true;
  }
  const ult = [...pts].reverse().find((p) => p[chiave] != null);
  const tn = tint === false ? 'var(--dato)' : tinta(ult[chiave], rif);
  el(g, 'path', { d, fill: 'none', stroke: tn, 'stroke-width': 1.8,
                  'stroke-linejoin': 'round' });
  // 4 marcatori solo sugli attraversamenti della banda
  let dentro = null;
  for (const p of pts) {
    const v = p[chiave]; if (v == null) { dentro = null; continue; }
    const dd = Math.abs(v - rif.v) <= rif.tol;
    if (dentro !== null && dd !== dentro) {
      el(g, 'circle', { cx: X(p.t), cy: Y(v), r: 2.6, fill: 'var(--sup)', stroke: tn, 'stroke-width': 1.4 });
    }
    dentro = dd;
  }
  if (titolo) {
    el(g, 'text', { x: x0, y: y0 - 6, fill: 'var(--muto)', 'font-size': 10.5,
                    'letter-spacing': '.09em' }, titolo.toUpperCase());
    const t2 = el(g, 'text', { x: x1, y: y0 - 6, 'text-anchor': 'end', 'font-size': 13,
                               'font-weight': 600, fill: grave(tn) ? tn : 'var(--ink)' }, pct(ult[chiave]));
    t2.setAttribute('font-variant-numeric', 'tabular-nums');
  }
  return { X, Y, lo, hi, chiave, rif, pts, tn };
}

// ============================================================ le tre + OEE
function disegnaOee() {
  const g = $('#g-oee'); const [W, H] = misura(g); vuota(g);
  const c = grafico(g, {
    x0: 58, x1: W - 14, y0: 34, y1: H - 32, pts: S.dett, rif: S.rif.o, chiave: 'o', assex: true,
  });
  el(g, 'text', { x: 58, y: 22, fill: 'var(--muto)', 'font-size': 11, 'letter-spacing': '.09em' },
     `RIFERIMENTO ${pct(S.rif.o.v)} · ${S.rifProv} · ${S.bandaProv}`);
  if (!c) return;
  const ult = [...S.dett].reverse().find((p) => p.o != null);
  const t = el(g, 'text', { x: W - 14, y: 26, 'text-anchor': 'end', 'font-size': 30, 'font-weight': 700,
                            fill: grave(c.tn) ? c.tn : 'var(--ink)' }, pct(ult.o));
  t.setAttribute('font-variant-numeric', 'tabular-nums');
  el(g, 'text', { x: 58, y: H - 4, fill: 'var(--muto)', 'font-size': 10.5 },
     `${S.dett.length} punti nel periodo`);
  legaHover(g, c, 'OEE');
}

function disegnaComp() {
  const g = $('#g-comp'); const [W, H] = misura(g); vuota(g);
  const conf = [
    ['a', 'Disponibilità', S.rif.a],
    ['p', 'Prestazione', S.rif.p],
    ['q', 'Qualità', S.rif.q],
  ];
  const cs = [];
  const passo = (H - 12) / 3;
  conf.forEach(([k, tit, rif], i) => {
    const y0 = 26 + i * passo;
    const c = grafico(g, {
      x0: 54, x1: W - 10, y0, y1: y0 + passo - 30, pts: S.dett, rif, chiave: k, titolo: tit,
      assex: false, tint: false,
    });
    if (c) cs.push([c, tit]);
  });
  if (cs.length) legaHover(g, cs[0][0], 'Componenti', cs);
}

// ---------------------------------------------------------------- hover
function legaHover(g, c, nome, tutte) {
  // Ogni ridisegno rilega gli ascoltatori: senza questo si accatastano e
  // quelli vecchi puntano a marchi che non stanno piu' nel documento.
  if (g.__ab) g.__ab.abort();
  g.__ab = new AbortController();
  const sg = { signal: g.__ab.signal };
  const gruppo = tutte || [[c, nome]];
  const mira = el(g, 'line', { y1: 0, y2: 0, stroke: 'var(--ink)', 'stroke-width': 1, opacity: .5, visibility: 'hidden' });
  const punti = gruppo.map(() => el(g, 'circle', { r: 3.4, fill: 'var(--sup)', stroke: 'var(--ink)',
                                                   'stroke-width': 1.6, visibility: 'hidden' }));
  const vb = g.viewBox.baseVal;
  let idx = c.pts.length - 1;
  const mostra = (ev) => {
    const p = c.pts[idx]; if (!p) return;
    mira.setAttribute('x1', c.X(p.t)); mira.setAttribute('x2', c.X(p.t));
    mira.setAttribute('y1', 24); mira.setAttribute('y2', vb.height - 34);
    mira.setAttribute('visibility', 'visible');
    let html = `<span class="m">${fO(p.t)}</span>`;
    gruppo.forEach(([cc, nn], i) => {
      const v = p[cc.chiave];
      punti[i].setAttribute('cx', cc.X(p.t));
      if (v == null) { punti[i].setAttribute('visibility', 'hidden'); }
      else { punti[i].setAttribute('cy', cc.Y(v)); punti[i].setAttribute('visibility', 'visible'); }
      const d = v == null ? null : v - cc.rif.v;
      html += `<br><span class="v">${pct(v)}</span> ${nn}`
        + (d == null ? '' : ` <span class="${d < -cc.rif.tol ? 'f' : 'm'}">`
           + `${d >= 0 ? '+' : '−'}${pct(Math.abs(d))}</span>`);
    });
    sugg(ev, html);
  };
  g.addEventListener('pointermove', (ev) => {
    const r = g.getBoundingClientRect();
    const xv = (ev.clientX - r.left) / r.width * vb.width;
    let best = 0, bd = Infinity;
    c.pts.forEach((p, i) => { const d = Math.abs(c.X(p.t) - xv); if (d < bd) { bd = d; best = i; } });
    idx = best; mostra(ev);
  }, sg);
  const spegni = () => {
    $('#tip').hidden = true; mira.setAttribute('visibility', 'hidden');
    punti.forEach((p) => p.setAttribute('visibility', 'hidden'));
  };
  g.addEventListener('pointerleave', spegni, sg);
  g.addEventListener('blur', spegni, sg);
  g.addEventListener('focus', () => { idx = c.pts.length - 1; mostraTastiera(); }, sg);
  const mostraTastiera = () => {
    const r = g.getBoundingClientRect(), p = c.pts[idx];
    mostra({ clientX: r.left + c.X(p.t) / vb.width * r.width, clientY: r.top + 40 });
  };
  g.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowLeft') idx = Math.max(0, idx - 1);
    else if (ev.key === 'ArrowRight') idx = Math.min(c.pts.length - 1, idx + 1);
    else if (ev.key === 'Home') idx = 0;
    else if (ev.key === 'End') idx = c.pts.length - 1;
    else if (ev.key === 'Escape') { spegni(); return; }
    else return;
    ev.preventDefault(); mostraTastiera();
  }, sg);
  g.setAttribute('aria-label',
    `${nome}: ${c.pts.length} punti dal ${fG(S.da)} al ${fG(S.a)}, riferimento ${pct(c.rif.v)}`);
}

function sugg(ev, html) {
  const t = $('#tip');
  t.innerHTML = html; t.hidden = false;
  const r = t.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  t.style.left = `${Math.max(6, x)}px`; t.style.top = `${Math.max(6, y)}px`;
}

// ============================================================ le valvole
function granulo() {
  const w = S.a - S.da;
  if (w <= 3 * GIORNO) return 'hour';
  if (w <= 32 * GIORNO) return 'day';
  return 'week';
}

// La forma della risposta di valves/quality/series non e' ancora fissata: si
// accettano le tre forme plausibili e nient'altro. Se non arriva, si dichiara.
function normalizza(j) {
  const out = new Map();
  // `total` e `good` viaggiano insieme alla qualita': il dettaglio di una
  // valvola mostra i cicli e gli scarti in unita' reali, e un rapporto da solo
  // non li sa ricostruire.
  const spingi = (id, t, q, tot, gd) => {
    id = Number(id); if (!Number.isFinite(id)) return;
    if (!out.has(id)) out.set(id, []);
    out.get(id).push({
      t: ms(t), q: q === undefined ? null : q,
      n: tot === undefined ? null : tot, g: gd === undefined ? null : gd,
    });
  };
  const leggi = (id, p) => spingi(id, p.at || p.t || p.ts,
    p.quality != null ? p.quality : p.q, p.total, p.good);
  if (j && j.valves && !Array.isArray(j.valves)) {
    for (const id in j.valves) {
      const v = j.valves[id];
      const arr = Array.isArray(v) ? v : (v.points || v.series || v.punti || []);
      for (const p of arr) leggi(id, p);
    }
  } else if (j && Array.isArray(j.series)) {
    for (const s of j.series) {
      for (const p of (s.points || s.series || [])) {
        leggi(s.valve_id != null ? s.valve_id : s.id, p);
      }
    }
  } else if (j && Array.isArray(j.points)) {
    for (const p of j.points) leggi(p.valve_id, p);
  }
  for (const a of out.values()) a.sort((x, y) => x.t - y.t);
  return out;
}

async function valvole(q, mio) {
  const g = $('#g-valv');
  // la base per valvola: la stessa finestra dichiarata da /valves/baseline
  if (S.valvBase === null && S.finBase) {
    const j = await get(`valves/quality/series?from=${encodeURIComponent(iso(S.finBase.da))}`
      + `&to=${encodeURIComponent(iso(S.finBase.a))}&grain=day`).catch(() => null);
    if (j) {
      const m = normalizza(j);
      S.valvBase = new Map();
      for (const [id, a] of m) {
        const md = mediana(a.map((p) => p.q));
        const sd = sigma(a.map((p) => p.q));
        // zona morta = 2σ misurati sulla base: l'oscillazione giornaliera di
        // una valvola sana non e' un segnale e non deve accendere una tinta.
        if (md != null) S.valvBase.set(id, {
          v: md, tol: Math.max(sd || 0, 0.02), morta: Math.max(2 * (sd || 0), 0.005),
        });
      }
    }
  }
  if (mio !== undefined && mio !== gen) return;
  const j = await get(`valves/quality/series?${q}&grain=${granulo()}`).catch(() => null);
  if (mio !== undefined && mio !== gen) return;
  const m = j ? normalizza(j) : null;
  // Se la grana fine non risponde si resta sulla copia locale a grana giorno,
  // che e' un dato vero: si dichiara «assente» solo quando non c'e' nemmeno quella.
  S.valvSerie = m && m.size ? m : (S.vsTutto && S.vsTutto.size ? S.vsTutto : null);
  S.valvStato = S.valvSerie && S.valvSerie.size ? 'ok' : 'assente';
  document.body.classList.toggle('valv-assenti', S.valvStato !== 'ok');
  disegnaValvole();
  disegnaPop();
  disegnaDettaglio();
}

function disegnaValvole() {
  const g = $('#g-valv'); const [W, H] = misura(g); vuota(g);
  if (S.valvStato !== 'ok') {
    el(g, 'text', { x: W / 2, y: H / 2 + 4, fill: 'var(--muto)', 'font-size': 11.5, 'text-anchor': 'middle' },
       'serie non disponibile — valves/quality/series risponde 404 su questo server');
    return;
  }
  const ids = [...S.valvSerie.keys()].sort((a, b) => a - b);
  const COL = 12, cw = (W - 4) / COL, RIGHE = Math.ceil(ids.length / COL), ch = (H - 6) / RIGHE;
  ids.forEach((id, i) => {
    const cx = 2 + (i % COL) * cw, cy = 3 + Math.floor(i / COL) * ch;
    const a = S.valvSerie.get(id).filter((p) => p.t >= S.da - GIORNO && p.t <= S.a + GIORNO);
    const rif = (S.valvBase && S.valvBase.get(id)) || null;
    const vs = a.map((p) => p.q).filter((v) => v != null);
    const gg = el(g, 'g', { class: 'cella-valvola', tabindex: 0, role: 'button' });
    // Il bersaglio del clic e' la CELLA INTERA, e va disegnato per primo cosi'
    // sta sotto tutto il resto. Prima l'unica superficie sensibile era il
    // riquadro dei dati piu' il riquadro del glifo «V8»: la colonna di 22 px
    // che porta l'etichetta era vuota. Chi premeva sul numero e si spostava di
    // un pixel faceva `mousedown` sul testo e `mouseup` sull'svg nudo: il
    // browser manda allora il `click` all'antenato comune — l'svg, che non
    // ascolta — mentre la pressione aveva gia' acceso la cella. Da qui il
    // difetto: la cella si illumina e il pannello non si apre.
    el(gg, 'rect', { x: cx, y: cy, width: cw, height: ch, fill: 'transparent' });
    gg.addEventListener('click', () => apriValvola(id, gg));
    gg.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); apriValvola(id, gg); }
    });
    el(gg, 'text', { x: cx + 2, y: cy + 10, fill: 'var(--muto)', 'font-size': 9.5, class: 'num' }, `V${id}`);
    const x0 = cx + 22, x1 = cx + cw - 8, y0 = cy + 3, y1 = cy + ch - 9;
    el(gg, 'rect', { x: x0, y: y0, width: x1 - x0, height: y1 - y0, fill: 'var(--sup-2)',
                     class: 'sfondo' });
    if (!vs.length) {
      el(gg, 'text', { x: (x0 + x1) / 2, y: (y0 + y1) / 2 + 3, fill: 'var(--muto)', 'font-size': 8.5,
                       'text-anchor': 'middle' }, 'non misurata');
      gg.setAttribute('aria-label', `Valvola ${id}: non misurata nel periodo. Apri il dettaglio.`);
      return;
    }
    // Ogni valvola si legge contro la PROPRIA base (LESSICO §2), quindi ha il
    // proprio dominio: contiene banda e dati, e i dati non si schiacciano mai
    // contro un bordo, nemmeno quando escono interi dalla banda.
    const dl = Math.min(...vs), dh = Math.max(...vs);
    // L'altezza di ogni riquadro vale quattro bande: cosi' i 35 riquadri sono
    // confrontabili fra loro (tutti in unita' della propria banda) e una
    // valvola sana resta un filo dentro la banda invece di gonfiarsi a pieno
    // riquadro. Chi esce, esce e si vede.
    let lo = rif ? Math.min(dl, rif.v - 4 * rif.tol) : dl - 0.02;
    let hi = rif ? Math.max(dh, rif.v + 4 * rif.tol) : dh + 0.02;
    const mar = (hi - lo) * 0.08; lo -= mar; hi += mar;
    const Y = (v) => y1 - (v - lo) / (hi - lo) * (y1 - y0);
    const X = (t) => x0 + (t - S.da) / Math.max(1, S.a - S.da) * (x1 - x0);
    if (rif) {
      el(gg, 'rect', { x: x0, y: Y(rif.v + rif.tol), width: x1 - x0,
                       height: Math.max(1, Y(rif.v - rif.tol) - Y(rif.v + rif.tol)), fill: 'var(--traccia)' });
      el(gg, 'line', { x1: x0, x2: x1, y1: Y(rif.v), y2: Y(rif.v), stroke: 'var(--rif)',
                       'stroke-width': 1, 'stroke-dasharray': '4 3' });
    }
    let d = '', giu = false;
    for (const p of a) {
      if (p.q == null) { giu = false; continue; }
      d += `${giu ? 'L' : 'M'}${X(p.t).toFixed(1)},${Y(p.q).toFixed(1)}`; giu = true;
    }
    const ult = [...a].reverse().find((p) => p.q != null);
    const tn = tinta(ult.q, rif);
    el(gg, 'path', { d, fill: 'none', stroke: tn, 'stroke-width': 1.4 });
    const testo = `Valvola ${id} — ${pct(ult.q)} il ${fO(ult.t)}`
      + ` · minimo del periodo ${pct(Math.min(...vs))}`
      + (rif ? ` · base ${pct(rif.v)} ±${pct(rif.tol)}` : ' · base non disponibile');
    el(gg, 'title', {}, `${testo} · apri il dettaglio`);
    gg.setAttribute('aria-label', `${testo}. Apri il dettaglio.`);
  });
  el(g, 'text', { x: 2, y: H - 1, fill: 'var(--muto)', 'font-size': 9.5 },
     `base della singola valvola · ${S.rifProv} · ${S.bandaProv} · altezza del riquadro = 4 bande`);
}

// Il gruppo e' la mediana delle 35 con una fascia di CINQUE punti di qualita'.
// E' una soglia dichiarata a schermo, non derivata: sulla base sana della run
// lo scarto fra la valvola piu' bassa del gruppo e la mediana vale 3,3 punti,
// e le due valvole costantemente anomale ne distano venti.
const POP_FASCIA = 0.05;

// Il tempo di riempimento ha la sua fascia, dichiarata allo stesso modo:
// OTTANTA millisecondi attorno alla mediana delle 35. Misurata sulla finestra
// sana della run, la valvola piu' lenta del gruppo dista 55 ms dalla mediana e
// le due anomale ne distano 108. Non e' derivata da una sigma: e' scritta qui
// e scritta a schermo.
const TEMPO_FASCIA = 80;

// la distanza fra due valvole si dice in PUNTI di qualita', non in percento
// di percento: «20 punti sotto» e non «20% sotto il 80%».
const punti = (v) => `${(v * 100).toLocaleString('it-IT', { maximumFractionDigits: 1 })} punti`;
const millis = (v) => `${num(v, 0)} ms`;

// Le due grandezze NON si fondono: non esiste un punteggio che le somma. Si
// guarda una per volta, ciascuna nella sua unita' vera, sullo stesso disegno.
// `verso` dice da che parte sta il male: la qualita' bassa e' un guasto, il
// tempo di riempimento ALTO e' un guasto.
const METRICHE = {
  q: {
    et: 'QUALITÀ DEL PERIODO', verso: -1, fascia: POP_FASCIA,
    val: (v) => pct(v), dist: punti,
    fascTesto: '5 punti', peggio: 'sotto', meglio: 'sopra',
  },
  t: {
    et: 'TEMPO DI RIEMPIMENTO', verso: +1, fascia: TEMPO_FASCIA,
    val: millis, dist: millis,
    fascTesto: '80 ms', peggio: 'sopra', meglio: 'sotto',
  },
};

const chiaveFin = () => `${iso(S.da)}|${iso(S.a)}`;

// Il tempo di riempimento di tutte e trentacinque su un periodo arriva da una
// chiamata sola. Non si ricava dai dati gia' in memoria: non ci sono.
async function caricaTempo(mio) {
  const ch = chiaveFin();
  if (S.tempo && S.tempo.chiave === ch) return;
  const j = await get(`valves/profile?from=${encodeURIComponent(iso(S.da))}`
    + `&to=${encodeURIComponent(iso(S.a))}`).catch(() => null);
  if (mio !== undefined && mio !== gen) return;
  let m = null;
  if (j && j.valves && !Array.isArray(j.valves)) {
    m = new Map();
    for (const id in j.valves) {
      const p = j.valves[id] && j.valves[id].periodo
        && j.valves[id].periodo.filling_time_ms;
      // media null vuol dire NON MISURATA. Mai zero, mai un riempitivo.
      m.set(Number(id), {
        v: p && p.media != null ? p.media : null,
        n: p && p.n != null ? p.n : null,
      });
    }
    if (!m.size) m = null;
  }
  S.tempo = { chiave: ch, dati: m };
  disegnaPop();
}

// ========================================== le 35 sulla stessa scala
// La fascia qui sopra legge ogni valvola contro la PROPRIA base: trova cio'
// che e' peggiorato, ed e' cieca su cio' che e' sempre stato storto, perche'
// quel difetto e' entrato nella base stessa. Questo riquadro fa l'altra
// lettura, e non la mescola con la prima: un solo metro per tutte e
// trentacinque. Il metro si sceglie — quanto scarto fa, quanto ci mette a
// riempire — e resta uno solo per volta, nell'unita' che l'API restituisce.

function popDati(k) {
  const m = METRICHE[k];
  const righe = [];
  if (k === 't') {
    if (!S.tempo || !S.tempo.dati) return null;
    for (const [id, x] of S.tempo.dati) righe.push({ id, v: x.v, n: x.n, g: null });
  } else {
    if (!S.valvSerie || !S.valvSerie.size) return null;
    for (const [id, a] of S.valvSerie) {
      const p = a.filter((x) => x.t >= S.da - GIORNO && x.t <= S.a + GIORNO);
      let n = 0, g = 0, misurata = false;
      for (const x of p) {
        if (x.n != null && x.g != null) { n += x.n; g += x.g; misurata = true; }
      }
      righe.push({ id, v: misurata && n > 0 ? g / n : null, n, g });
    }
  }
  const vs = righe.map((r) => r.v).filter((v) => v != null);
  if (!vs.length) return null;
  const med = mediana(vs);
  // Le valvole restano in ordine di macchina: la posizione sull'asse
  // orizzontale e' l'identita', non il giudizio. Il posto in classifica
  // esiste, ma si chiede passandoci sopra: non ordina la vista.
  const cls = righe.filter((r) => r.v != null).slice()
    .sort((a, b) => (a.v - b.v) * m.verso * -1);
  const posto = new Map(cls.map((r, i) => [r.id, i + 1]));
  righe.sort((a, b) => a.id - b.id);
  const fuori = vs.filter((v) => (v - med) * m.verso > m.fascia).length;
  return {
    righe, med, posto, npos: cls.length, fuori, misurate: vs.length,
    min: Math.min(...vs), max: Math.max(...vs),
  };
}

// La tinta la prende solo chi sta FUORI dal gruppo dalla parte del guasto, e
// cresce con la distanza. Dentro la fascia: neutro. Dalla parte buona: neutro.
// Non c'e' un verde per chi sta bene.
function popTinta(v, med, m) {
  if (v == null) return 'var(--muto)';
  const d = (v - med) * m.verso;
  if (d <= m.fascia) return 'var(--dato)';
  if (d <= 2 * m.fascia) return 'var(--sev2)';
  if (d <= 5 * m.fascia) return 'var(--sev3)';
  return 'var(--sev4)';
}

function popPasso(k, span) {
  if (k === 'q') return span > 0.5 ? 0.2 : span > 0.25 ? 0.1 : 0.05;
  for (const p of [10, 20, 25, 50, 100, 200, 500, 1000]) if (span / p <= 6) return p;
  return 2000;
}

// L'interruttore fra le due grandezze. Sta nell'intestazione del riquadro,
// dove prima c'era la sola etichetta: la voce spenta porta quante valvole
// stanno fuori dal gruppo con QUEL metro — un fatto misurato, non un indice.
function popScelta(g, x, y, k, d) {
  const attiva = S.metrica === k;
  const m = METRICHE[k];
  const gg = el(g, 'g', { class: 'pop-scelta', tabindex: 0, role: 'button',
                          'aria-pressed': attiva ? 'true' : 'false' });
  const t = el(gg, 'text', {
    x, y, 'font-size': 10.5, 'letter-spacing': '.09em',
    fill: attiva ? 'var(--ink)' : 'var(--muto)',
    'font-weight': attiva ? 600 : 400,
  }, m.et + (attiva || !d ? '' : ` · ${d.fuori} fuori`));
  const w = t.getComputedTextLength ? t.getComputedTextLength() : 140;
  el(gg, 'rect', { x: x - 4, y: y - 11, width: w + 8, height: 15,
                   fill: 'transparent', class: 'sfondo' });
  if (attiva) el(gg, 'line', { x1: x, x2: x + w, y1: y + 3.5, y2: y + 3.5,
                               stroke: 'var(--ink)', 'stroke-width': 1.5 });
  gg.setAttribute('aria-label', `Guarda le 35 valvole sul ${m.et.toLowerCase()}`);
  const vai = () => { if (S.metrica !== k) { S.metrica = k; disegnaPop(); } };
  gg.addEventListener('click', vai);
  gg.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); vai(); }
  });
  return w + 26;
}

function disegnaPop() {
  const g = $('#g-pop'); if (!g) return;
  const [W, H] = misura(g); vuota(g);
  const k = S.metrica, m = METRICHE[k];
  const d = popDati(k);
  const altra = k === 'q' ? 't' : 'q';
  const dAltra = popDati(altra);

  // intestazione: le due grandezze, una accesa
  let x = 0;
  x += popScelta(g, x, 11, 'q', k === 'q' ? null : dAltra || popDati('q'));
  popScelta(g, x, 11, 't', k === 't' ? null : dAltra || popDati('t'));

  if (!d) {
    el(g, 'text', { x: W / 2, y: H / 2 + 6, fill: 'var(--muto)', 'font-size': 11.5,
                    'text-anchor': 'middle' },
       k === 't' ? 'tempo di riempimento in arrivo…'
                 : 'serie per valvola non disponibile');
    return;
  }

  // Fuori periodo: i numeri sono quelli dell'ultima finestra chiesta, non di
  // questa. Non si scrivono come se fossero suoi: il disegno si attenua finche'
  // la risposta non arriva.
  const vecchi = k === 't' && (!S.tempo || S.tempo.chiave !== chiaveFin());
  const gr = el(g, 'g', vecchi ? { opacity: .45 } : {});

  const M = { l: 46, r: 12, t: 24, b: 20 };
  const x0 = M.l, x1 = W - M.r, y0 = M.t, y1 = H - M.b;
  // Il dominio contiene tutti i dati e tutta la fascia del gruppo: chi e' a
  // zero sta a zero, non schiacciato contro un bordo scelto per comodita'.
  const pad = k === 'q' ? 0.02 : 20, minAmp = k === 'q' ? 0.08 : 120;
  let lo = Math.min(d.min, d.med - m.fascia) - pad;
  let hi = Math.max(d.max, d.med + m.fascia) + pad;
  if (k === 'q') { lo = Math.max(0, lo); hi = Math.min(1, hi); }
  else lo = Math.max(0, lo);
  if (hi - lo < minAmp) { const c = (hi + lo) / 2; lo = c - minAmp / 2; hi = c + minAmp / 2; }
  const Y = (v) => y1 - (v - lo) / (hi - lo) * (y1 - y0);

  // asse: pochi valori, quelli che servono a leggere la distanza
  const passo = popPasso(k, hi - lo);
  for (let v = Math.ceil(lo / passo) * passo; v <= hi + 1e-9; v += passo) {
    const y = Y(v);
    el(gr, 'line', { x1: x0 - 4, x2: x1, y1: y, y2: y, stroke: 'var(--traccia)',
                     'stroke-width': 1 });
    el(gr, 'text', { x: x0 - 8, y: y + 3.5, fill: 'var(--muto)', 'font-size': 9.5,
                     'text-anchor': 'end' }, m.val(v));
  }

  // il gruppo: una fascia e la sua mediana. Chi ci sta dentro e' il gruppo.
  el(gr, 'rect', { x: x0, y: Y(d.med + m.fascia), width: x1 - x0,
                   height: Math.max(2, Y(d.med - m.fascia) - Y(d.med + m.fascia)),
                   fill: 'var(--banda)', opacity: .5 });
  el(gr, 'line', { x1: x0, x2: x1, y1: Y(d.med), y2: Y(d.med), stroke: 'var(--rif)',
                   'stroke-width': 1, 'stroke-dasharray': '4 3' });

  const N = d.righe.length, pas = (x1 - x0) / N;
  d.righe.forEach((r, i) => {
    const cx = x0 + pas * (i + 0.5);
    const tn = popTinta(r.v, d.med, m);
    const scost = r.v == null ? null : (r.v - d.med) * m.verso;
    const fuori = scost != null && scost > m.fascia;
    const cel = el(gr, 'g', { class: 'cella-valvola cella-pop', tabindex: 0, role: 'button' });
    // area afferrabile: tutta la colonna, non il solo pallino
    // senza fessure fra una colonna e l'altra: un pixel scoperto e' un clic perso
    el(cel, 'rect', { x: cx - pas / 2, y: y0 - 8, width: pas,
                      height: y1 - y0 + 30, fill: 'transparent', class: 'sfondo' });
    if (r.v == null) {
      el(cel, 'circle', { cx, cy: Y(d.med), r: 3, fill: 'none', stroke: 'var(--muto)',
                          'stroke-width': 1, 'stroke-dasharray': '2 2' });
    } else {
      // il gambo parte dalla mediana del gruppo: la sua lunghezza E' la
      // distanza dagli altri trentaquattro, letta senza calcolare niente
      el(cel, 'line', { x1: cx, x2: cx, y1: Y(d.med), y2: Y(r.v), stroke: tn,
                        'stroke-width': Math.abs(r.v - d.med) > m.fascia ? 2 : 1,
                        opacity: Math.abs(r.v - d.med) > m.fascia ? 1 : .55 });
      el(cel, 'circle', { cx, cy: Y(r.v), r: Math.abs(r.v - d.med) > m.fascia ? 4 : 3,
                          fill: tn });
    }
    el(cel, 'text', { x: cx, y: y1 + 13, fill: tn === 'var(--dato)' ? 'var(--muto)' : tn,
                      'font-size': 9.5, 'text-anchor': 'middle', class: 'num' }, r.id);
    const testo = r.v == null
      ? `Valvola ${r.id} — non misurata nel periodo`
      : `Valvola ${r.id} — ${m.val(r.v)} · `
        + (Math.abs(r.v - d.med) <= m.fascia
            ? `nel gruppo`
            : `${m.dist(Math.abs(r.v - d.med))} ${r.v < d.med ? 'sotto' : 'sopra'} la mediana del gruppo`)
        + ` · posto ${d.posto.get(r.id)} su ${d.npos}`;
    el(cel, 'title', {}, `${testo} · apri il dettaglio`);
    cel.setAttribute('aria-label', `${testo}. Apri il dettaglio.`);
    cel.addEventListener('click', () => apriValvola(r.id, cel));
    cel.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); apriValvola(r.id, cel); }
    });
    cel.addEventListener('mousemove', (ev) => {
      const coda = k === 't'
        ? `<br><span class="m">${r.n ? num(r.n) : '—'} cicli misurati</span>`
        : `<br><span class="m">${r.n ? num(r.n) : '—'} cicli · ${r.n ? num(r.n - r.g) : '—'} scarti</span>`;
      sugg(ev, `<span class="v">Valvola ${r.id}</span> · ${r.v == null ? 'non misurata' : m.val(r.v)}`
        + `<br><span class="m">${r.v == null ? '' : `posto ${d.posto.get(r.id)} su ${d.npos} · `}mediana del gruppo ${m.val(d.med)}</span>`
        + (fuori
            ? `<br><span class="f">${m.dist(Math.abs(r.v - d.med))} ${r.v > d.med ? 'sopra' : 'sotto'} la mediana del gruppo</span>` : '')
        + coda);
    });
    cel.addEventListener('mouseleave', () => { $('#tip').hidden = true; });
  });

  el(g, 'text', { x: x1, y: 11, fill: 'var(--muto)', 'font-size': 9.5,
                  'text-anchor': 'end' },
     `una scala sola per tutte · gruppo = mediana ${m.val(d.med)} ± ${m.fascTesto}`
     + ` · ${d.misurate} misurate su ${N}`);
}

// ================================================== il dettaglio di UNA valvola
// Si apre SOPRA la pagina (LESSICO §4): la striscia resta la spina dorsale e
// nessuno finisce in un secondo livello di navigazione. Niente punteggio di
// salute: grandezze NOMINATE, ognuna contro la base della PROPRIA valvola.
let daDove = null;

function apriValvola(id, origine) {
  daDove = origine || null;
  S.dettValv = id;
  $('#velo').hidden = false;
  $('#pan').hidden = false;
  S.prof = null;
  disegnaDettaglio();
  caricaProfilo(id);
  $('#pan-x').focus();
}

function chiudiValvola() {
  $('#velo').hidden = true;
  $('#pan').hidden = true;
  S.dettValv = null;
  S.prof = null;
  const go = $('#pan-onda');
  if (go && go.__ab) { go.__ab.abort(); go.__ab = null; }
  $('#tip').hidden = true;
  if (daDove && daDove.isConnected) daDove.focus();
  daDove = null;
}

function chip(dove, nome, valore) {
  const c = document.createElement('span');
  c.className = 'chip';
  const k = document.createElement('span');
  k.className = 'k'; k.textContent = nome;
  c.appendChild(k);
  c.appendChild(document.createTextNode(valore));
  dove.appendChild(c);
}

function disegnaDettaglio() {
  const id = S.dettValv;
  if (id == null || $('#pan').hidden) return;
  const serie = (S.valvSerie && S.valvSerie.get(id)) || [];
  const pts = serie.filter((p) => p.t >= S.da - GIORNO && p.t <= S.a + GIORNO);
  const rif = (S.valvBase && S.valvBase.get(id)) || null;
  $('#pan-tit').textContent = `Valvola ${id}`;
  $('#pan-per').textContent = `${fG(S.da)} → ${fO(S.a)} · ${fDurata(S.a - S.da)}`;
  caricaProfilo(id);
  disegnaProfilo();

  const g = $('#pan-g');
  const [W, H] = misura(g); vuota(g);
  const vs = pts.map((p) => p.q).filter((v) => v != null);
  let c = null;
  if (!rif || !vs.length) {
    el(g, 'text', { x: W / 2, y: H / 2, fill: 'var(--muto)', 'font-size': 12,
                    'text-anchor': 'middle' },
       vs.length ? 'base della valvola non disponibile' : 'serie non disponibile');
  } else {
    c = grafico(g, { x0: 56, x1: W - 12, y0: 30, y1: H - 30, pts,
                     rif, chiave: 'q', assex: true });
    el(g, 'text', { x: 56, y: 18, fill: 'var(--muto)', 'font-size': 10.5,
                    'letter-spacing': '.09em' },
       `QUALITÀ · base della valvola ${pct(rif.v)} · banda ±${pct(rif.tol)} · ${S.rifProv}`);
    if (c) legaHover(g, c, `Valvola ${id}`);
  }

  const box = $('#pan-chip');
  box.innerHTML = '';
  const ult = [...pts].reverse().find((p) => p.q != null);
  const cicli = pts.reduce((s, p) => s + (p.n || 0), 0);
  const buoni = pts.reduce((s, p) => s + (p.g || 0), 0);
  const fuori = rif ? pts.filter((p) => p.q != null && p.q < rif.v - rif.tol).length : null;
  chip(box, 'ultima misura', ult ? `${pct(ult.q)} · ${fO(ult.t)}` : 'non misurata');
  chip(box, 'base della valvola', rif ? `${pct(rif.v)} ±${pct(rif.tol)}` : 'non disponibile');
  chip(box, 'scostamento dalla base', ult && rif
    ? `${ult.q - rif.v >= 0 ? '+' : '−'}${pct(Math.abs(ult.q - rif.v))}` : '—');
  chip(box, 'minimo del periodo', vs.length ? pct(Math.min(...vs)) : '—');
  chip(box, 'cicli nel periodo', cicli ? num(cicli) : '—');
  chip(box, 'scarti nel periodo', cicli ? num(cicli - buoni) : '—');
  chip(box, 'secchielli sotto la banda', fuori == null ? '—' : `${num(fuori)} su ${num(pts.length)}`);
}

// ======================================================= il profilo del ciclo
// NON e' una forma d'onda registrata: il simulatore non campiona il segnale
// dentro il ciclo. Sono TRE punti misurati, uniti da segmenti retti, e i tre
// punti restano visibili perche' sono l'unica cosa che e' stata misurata:
//   (0, 0)                              apertura
//   (filling_time_ms, pulse_count)      fine riempimento
//   (+ tail_time_ms,  tail_pulse)       fine coda
// Ascissa = durata in ms, ordinata = conteggio impulsi. Nessuna interpolazione
// morbida, nessun punto intermedio: non esiste una misura che lo giustifichi.
const GRAND = [
  ['filling_time_ms', 'tempo di riempimento'],
  ['tail_time_ms', 'tempo di coda'],
  ['pulse_count', 'impulsi del ciclo'],
  ['tail_pulse', 'impulsi di coda'],
];

function ancore(p) {
  if (!p) return { pt: null, manca: null };
  const g = (k) => (p[k] && p[k].media != null) ? p[k].media : null;
  const manca = GRAND.filter(([k]) => g(k) == null).map(([, n]) => n);
  const cicli = (p.filling_time_ms && p.filling_time_ms.n != null)
    ? p.filling_time_ms.n : null;
  if (manca.length) return { pt: null, manca, cicli, tutte: manca.length === GRAND.length };
  const ft = g('filling_time_ms'), tt = g('tail_time_ms');
  const pc = g('pulse_count'), tp = g('tail_pulse');
  const n = (p.filling_time_ms && p.filling_time_ms.n) || null;
  return {
    ft, tt, pc, tp, n, manca: [],
    pt: [
      { t: 0, v: 0, nome: 'apertura' },
      { t: ft, v: pc, nome: 'fine riempimento' },
      { t: ft + tt, v: tp, nome: 'fine coda' },
    ],
  };
}

async function caricaProfilo(id) {
  const ch = `${id}|${iso(S.da)}|${iso(S.a)}`;
  if (S.prof && S.prof.chiave === ch) return;
  S.prof = { chiave: ch, stato: 'attesa', dati: null };
  disegnaProfilo();
  const j = await get(`valves/${id}/profile?from=${encodeURIComponent(iso(S.da))}`
    + `&to=${encodeURIComponent(iso(S.a))}`).catch(() => null);
  if (!S.prof || S.prof.chiave !== ch || S.dettValv !== id) return;
  S.prof.dati = j;
  S.prof.stato = j ? 'ok' : 'assente';
  disegnaProfilo();
}

function nota(g, W, H, testo) {
  el(g, 'text', { x: W / 2, y: H / 2 + 4, fill: 'var(--muto)', 'font-size': 11.5,
                  'text-anchor': 'middle' }, testo);
}

function disegnaProfilo() {
  const g = $('#pan-onda');
  if (!g || $('#pan').hidden) return;
  const [W, H] = misura(g); vuota(g);
  if (g.__ab) { g.__ab.abort(); g.__ab = null; }
  const id = S.dettValv;
  const st = S.prof || { stato: 'attesa' };
  g.setAttribute('aria-label', `Profilo del ciclo medio della valvola ${id}`);

  if (st.stato === 'attesa') { nota(g, W, H, 'profilo in arrivo…'); return; }
  if (st.stato === 'assente') {
    nota(g, W, H, `profilo non disponibile — valves/${id}/profile non risponde su questo server`);
    return;
  }
  const d = st.dati || {};
  const per = ancore(d.periodo);
  const bas = ancore(d.base);
  // Il motivo di un dato ridotto si scrive sempre, anche quando non c'e'
  // niente da disegnare: e' il caso in cui serve di piu'.
  const motivo = () => {
    if (!d.degraded) return;
    el(g, 'text', { x: W - 16, y: 16, fill: 'var(--muto)', 'font-size': 10, 'text-anchor': 'end' },
       `dato ridotto: ${d.reason || 'motivo non dichiarato'}`);
  };
  if (!per.pt) {
    motivo();
    nota(g, W, H, per.tutte
      ? `profilo non calcolabile · ${num(per.cicli || 0)} cicli nel periodo`
      : (per.manca && per.manca.length
         ? `profilo non calcolabile: ${per.manca.join(', ')} non misurate nel periodo`
         : 'profilo non calcolabile nel periodo'));
    return;
  }

  const x0 = 60, x1 = W - 16, y0 = 42, y1 = H - 50;
  const Tmax = Math.max(per.ft + per.tt, bas.pt ? bas.ft + bas.tt : 0) * 1.06;
  const Pmax = Math.max(per.pc, bas.pt ? bas.pc : 0) * 1.10;
  const X = (t) => x0 + t / Tmax * (x1 - x0);
  const Y = (v) => y1 - v / Pmax * (y1 - y0);
  const via = (pt) => pt.map((p, i) => `${i ? 'L' : 'M'}${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`).join('');

  // assi
  el(g, 'line', { x1: x0, x2: x1, y1: y1, y2: y1, stroke: 'var(--bordo)', 'stroke-width': 1 });
  el(g, 'line', { x1: x0, x2: x0, y1: y0 - 8, y2: y1, stroke: 'var(--bordo)', 'stroke-width': 1 });

  // 1 il riferimento: la silhouette della PROPRIA valvola sulla finestra sana.
  //   La route serve medie senza dispersione: non c'e' banda da disegnare, e
  //   la sua assenza si dichiara invece di stimarla.
  //   Fra le due silhouette si riempie lo SCARTO: e' l'area chiusa da due
  //   traiettorie misurate, nessun bordo inventato. Serve perche' le grandezze
  //   grezze di due valvole guaste distano l'8%, mentre i loro scarti dalla
  //   base distano il 28%: la differenza sta nel divario, non nell'altezza.
  if (bas.pt) {
    const giu = bas.pt.slice().reverse();
    el(g, 'path', {
      d: `${via(per.pt)}${giu.map((p) => `L${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`).join('')}Z`,
      fill: 'var(--traccia)', stroke: 'none',
    });
    el(g, 'path', { d: via(bas.pt), fill: 'none', stroke: 'var(--rif)',
                    'stroke-width': 1.2, 'stroke-dasharray': '5 4' });
    el(g, 'circle', { cx: X(bas.ft), cy: Y(bas.pc), r: 2.4, fill: 'none',
                      stroke: 'var(--rif)', 'stroke-width': 1.2 });
    el(g, 'circle', { cx: X(bas.ft + bas.tt), cy: Y(bas.tp), r: 2.4, fill: 'none',
                      stroke: 'var(--rif)', 'stroke-width': 1.2 });
    // quando la valvola e' allineata alla sua base i due vertici coincidono:
    // l'etichetta sale invece di sovrapporsi al punto misurato.
    const vicino = Math.abs(Y(bas.pc) - Y(per.pc)) < 12;
    el(g, 'text', { x: X(bas.ft) - 8, y: Y(bas.pc) - (vicino ? 14 : 6), fill: 'var(--rif)',
                    'font-size': 9.5, 'text-anchor': 'end' },
       `base ${num(bas.pc)} a ${num(bas.ft)} ms`);
  }

  // 2 il confine fra le due fasi
  el(g, 'line', { x1: X(per.ft), x2: X(per.ft), y1: Y(per.pc) - 6, y2: y1,
                  stroke: 'var(--rif)', 'stroke-width': 1.2, 'stroke-dasharray': '4 3' });

  // 3 la traiettoria, sopra tutto. Neutra: nel pannello nessun marchio del
  //   profilo porta una gravita'.
  el(g, 'path', { d: via(per.pt), fill: 'none', stroke: 'var(--dato)',
                  'stroke-width': 2.2, 'stroke-linejoin': 'round' });

  // 4 i tre punti misurati restano visibili: sono l'unico dato.
  const dot = per.pt.map((p) => el(g, 'circle', {
    cx: X(p.t), cy: Y(p.v), r: 3.6, fill: 'var(--dato)',
    stroke: 'var(--dato)', 'stroke-width': 1.8,
  }));

  // 5 le scale: ogni tacca e' una grandezza misurata, non una griglia decorativa
  const eY = (v, testo) => el(g, 'text', { x: x0 - 6, y: Y(v) + 3.5, fill: 'var(--muto)',
                                           'font-size': 10, 'text-anchor': 'end' }, testo);
  eY(0, '0');
  eY(per.tp, num(per.tp));
  if (Math.abs(Y(per.pc) - Y(per.tp)) > 11) eY(per.pc, num(per.pc));
  const eX = (x, testo, anc) => el(g, 'text', { x, y: y1 + 13, fill: 'var(--muto)',
                                                'font-size': 10, 'text-anchor': anc || 'middle' }, testo);
  eX(x0, '0', 'start');
  // le due etichette di fine fase si accavallano quando la coda e' breve:
  // quella del riempimento arretra invece di sovrapporsi.
  eX(X(per.ft) - 4, `${num(per.ft)} ms`, 'end');
  eX(x1, `${num(per.ft + per.tt)} ms`, 'end');

  // 6 le due fasi, nominate sotto l'asse
  const fase = (a, b, testo, col) => {
    const xa = X(a), xb = X(b);
    el(g, 'line', { x1: xa + 1, x2: xb - 1, y1: y1 + 24, y2: y1 + 24, stroke: col,
                    'stroke-width': 2 });
    el(g, 'text', { x: (xa + xb) / 2, y: y1 + 37, fill: col, 'font-size': 9.5,
                    'letter-spacing': '.09em', 'text-anchor': 'middle' }, testo);
  };
  fase(0, per.ft, 'RIEMPIMENTO', 'var(--dato)');
  fase(per.ft, per.ft + per.tt, 'CODA', 'var(--dato)');

  // 7 intestazione e provenienza
  el(g, 'text', { x: x0, y: 16, fill: 'var(--muto)', 'font-size': 10.5,
                  'letter-spacing': '.09em' },
     `PROFILO DEL CICLO MEDIO · IMPULSI NEL TEMPO${per.n ? ` · ${num(per.n)} cicli` : ''}`);
  el(g, 'text', { x: x0, y: 30, fill: 'var(--muto)', 'font-size': 10 },
     bas.pt
       ? `base della valvola ${id}${bas.n ? ` su ${num(bas.n)} cicli` : ''}`
         + (d.base_from && d.base_to ? ` · ${fG(ms(d.base_from))} – ${fG(ms(d.base_to))}` : ' · finestra sana')
         + ' · medie senza dispersione: nessuna banda'
       : (bas.manca && bas.manca.length
          ? `base della valvola: ${bas.manca.join(', ')} non misurate`
          : 'base della valvola non disponibile'));
  motivo();

  // 8 hover e tastiera sui tre punti
  g.__ab = new AbortController();
  const sg = { signal: g.__ab.signal };
  const vb = g.viewBox.baseVal;
  let idx = 1;
  const scost = (v, b, u, dec) => b == null ? ''
    : ` <span class="m">${v - b >= 0 ? '+' : '−'}${num(Math.abs(v - b), dec)} ${u} vs base</span>`;
  const mostra = (ev) => {
    const p = per.pt[idx];
    dot.forEach((c, i) => c.setAttribute('r', i === idx ? 5.2 : 3.6));
    let html = `<span class="m">${p.nome}</span>`;
    if (idx === 0) {
      html += '<br><span class="v">0 ms</span> · <span class="v">0 impulsi</span>';
    } else if (idx === 1) {
      html += `<br><span class="v">${num(per.ft, 1)} ms</span> di riempimento`
        + scost(per.ft, bas.pt ? bas.ft : null, 'ms', 1)
        + `<br><span class="v">${num(per.pc)} impulsi</span>`
        + scost(per.pc, bas.pt ? bas.pc : null, 'impulsi', 0);
    } else {
      html += `<br><span class="v">${num(per.tt, 1)} ms</span> di coda`
        + scost(per.tt, bas.pt ? bas.tt : null, 'ms', 1)
        + `<br><span class="v">${num(per.tp)} impulsi</span> di coda`
        + scost(per.tp, bas.pt ? bas.tp : null, 'impulsi', 0);
    }
    sugg(ev, html);
  };
  g.addEventListener('pointermove', (ev) => {
    const r = g.getBoundingClientRect();
    const xv = (ev.clientX - r.left) / r.width * vb.width;
    let best = 0, bd = Infinity;
    per.pt.forEach((p, i) => { const dd = Math.abs(X(p.t) - xv); if (dd < bd) { bd = dd; best = i; } });
    idx = best; mostra(ev);
  }, sg);
  const spegni = () => { $('#tip').hidden = true; dot.forEach((c) => c.setAttribute('r', 3.6)); };
  g.addEventListener('pointerleave', spegni, sg);
  g.addEventListener('blur', spegni, sg);
  const daTastiera = () => {
    const r = g.getBoundingClientRect(), p = per.pt[idx];
    mostra({ clientX: r.left + X(p.t) / vb.width * r.width, clientY: r.top + 30 });
  };
  g.addEventListener('focus', () => { idx = 1; daTastiera(); }, sg);
  g.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowLeft') idx = Math.max(0, idx - 1);
    else if (ev.key === 'ArrowRight') idx = Math.min(2, idx + 1);
    else if (ev.key === 'Home') idx = 0;
    else if (ev.key === 'End') idx = 2;
    else if (ev.key === 'Escape') { spegni(); return; }
    else return;
    ev.preventDefault(); daTastiera();
  }, sg);

  g.setAttribute('aria-label',
    `Profilo del ciclo medio della valvola ${id}: tre punti misurati — apertura a zero,`
    + ` fine riempimento a ${num(per.ft, 1)} millisecondi e ${num(per.pc)} impulsi,`
    + ` fine coda a ${num(per.tt, 1)} millisecondi di coda e ${num(per.tp)} impulsi.`
    + (bas.pt ? ` Riferimento: la base della stessa valvola sulla finestra sana,`
        + ` ${num(bas.ft, 1)} millisecondi e ${num(bas.pc)} impulsi.`
      : ' Base della valvola non disponibile.'));
}

function montaPannello() {
  $('#pan-x').addEventListener('click', chiudiValvola);
  $('#velo').addEventListener('click', chiudiValvola);
  addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !$('#pan').hidden) chiudiValvola();
  });
}

let rtm = null;
addEventListener('resize', () => {
  clearTimeout(rtm);
  rtm = setTimeout(() => {
    if (!S.tutto.length) return;
    disegnaStriscia();
    if (S.dett) { disegnaOee(); disegnaComp(); disegnaValvole(); disegnaPop(); disegnaDettaglio(); }
  }, 120);
});

// ============================================================ tema
function montaTema() {
  const b = $('#tema');
  const scuro = () => (document.documentElement.getAttribute('data-tema')
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'scuro' : 'chiaro')) === 'scuro';
  const dip = () => { b.textContent = scuro() ? 'CHIARO' : 'SCURO'; };
  dip();
  b.addEventListener('click', () => {
    const n = scuro() ? 'chiaro' : 'scuro';
    document.documentElement.setAttribute('data-tema', n);
    try { localStorage.setItem('tema-v7pc', n); } catch (e) {}
    dip();
    if (S.tutto.length) {
      disegnaStriscia(); disegnaOee(); disegnaComp(); disegnaValvole(); disegnaPop(); disegnaDettaglio();
    }
  });
}
