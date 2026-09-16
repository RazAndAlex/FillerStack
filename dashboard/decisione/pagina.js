// DECISIONE — giostra: le 35 valvole nell'ordine fisico della riempitrice, in
// anello. La valvola in «intervieni» esce dall'anello e porta la sua scheda
// accostata. In fondo la striscia dei due mesi sceglie l'ora della corsa.
//
// Ogni numero arriva dalle rotte del proxy, specchio di pipeline/api.py:
//   - la linea del tempo:  GET /valves/decision/timeline
//     una chiamata sola all'apertura, 51 kB, i conteggi di tutte le ore.
//     La striscia si disegna con quella e trascinare non chiede piu' niente
//     alla rete: un comando che risponde dopo tre secondi si legge come rotto.
//   - il verdetto:         GET /valves/decision?run_id=...&adesso=...
//     le 35 valvole a un'ora sola, chiesto al rilascio e mai a ogni scatto.
//
// La corsa e' quella che dichiara la linea del tempo: il suo `run_id` viaggia
// con ogni richiesta di verdetto, cosi' striscia e giostra non possono mai
// parlare di due corse diverse.

import { scenarioCorrente, collegaNav } from '/comune/dati.js';

const SVGNS = 'http://www.w3.org/2000/svg';
const N = 35;
const TEMA_KEY = 'tema-v7dec';
const HB = 52;          // altezza della striscia, in unita' del suo viewBox
const ORA_MS = 3600000;
const MESI = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno',
              'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre'];

const $ = (s) => document.querySelector(s);

// L'unico modo di leggere: il proxy della dashboard, come fanno le altre pagine.
async function get(percorso, signal) {
  const r = await fetch(`/api/${scenarioCorrente()}/${percorso}`,
                        { cache: 'no-store', signal });
  if (!r.ok) throw new Error(`${percorso} → HTTP ${r.status}`);
  return r.json();
}

// ---------- numeri e date, in italiano ----------
const num = (v, d = 2) =>
  (typeof v === 'number' && isFinite(v))
    ? v.toFixed(d).replace('.', ',')
    : null;

const eur = (v) => { const s = num(v, 2); return s === null ? null : s + ' €'; };
const ore = (v) => { const s = num(v, 1); return s === null ? null : s + ' h'; };

// Quante ore mancano da `dati.adesso` a un'ora ISO. Torna null se manca uno dei
// due estremi, e 0 se l'ora e' gia' passata: un'attesa non va all'indietro.
function quanteOreAll(iso) {
  if (!iso || !dati || !dati.adesso) return null;
  const a = Date.parse(dati.adesso), b = Date.parse(iso);
  if (!isFinite(a) || !isFinite(b)) return null;
  return Math.max(0, (b - a) / 3600000);
}

// La rotta scrive i numeri dentro `motivo` con il punto decimale inglese.
// Si riformattano qui, al momento di scriverli a schermo, con la virgola
// decimale e il punto delle migliaia. Gli accenti arrivano gia' giusti.
function itNumero(testo) {
  const [int, dec] = testo.split('.');
  const mille = int.replace(/\B(?=(\d{3})+(?!\d))/g, '.');
  return dec == null ? mille : mille + ',' + dec;
}
// Le fixture scrivono anche le vocali accentate come lettera piu' apostrofo
// (`pero'`, `e'`, `gia'`). A schermo si leggono come italiano rotto, quindi si
// rimettono gli accenti qui, sempre senza toccare le fixture.
function motivoIt(testo) {
  if (!testo) return testo;
  return testo.replace(/\d+(?:\.\d+)?/g, (m) => itNumero(m));
}

function giornoOra(iso) {
  if (!iso) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso);
  return m ? `${m[3]}-${m[2]} ${m[4]}:${m[5]}` : null;
}

function el(tag, cls, testo) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (testo != null) n.textContent = testo;
  return n;
}
function svg(tag, attr) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attr) n.setAttribute(k, attr[k]);
  return n;
}

// ---------- stato ----------
let dati = null;        // il verdetto dell'ora scelta
let linea = null;       // la linea del tempo di tutta la corsa
let ac = null;          // ascoltatori del disegno: si staccano a ogni ridisegno
let acRete = null;      // richieste in volo: la nuova annulla la precedente
let attesa = false;     // vero mentre il verdetto sta arrivando
let ORE = 0, T0 = 0, maxD = 1;
let MOMENTI = [], btnMom = [], curLinea = null, oraK = 0;

// ---------- tema ----------
function temaCorrente() {
  return document.documentElement.getAttribute('data-tema')
    || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'scuro' : 'chiaro');
}
function montaTema() {
  const b = $('#tema');
  const scrivi = () => { b.textContent = temaCorrente() === 'scuro' ? 'scuro' : 'chiaro'; };
  scrivi();
  b.addEventListener('click', () => {
    const t = temaCorrente() === 'scuro' ? 'chiaro' : 'scuro';
    document.documentElement.setAttribute('data-tema', t);
    try { localStorage.setItem(TEMA_KEY, t); } catch (e) {}
    scrivi();
    disegna();
  });
}

// ---------- suggerimento ----------
const tip = $('#tip');
function mostraTip(ev, righe) {
  tip.innerHTML = '';
  righe.forEach((r, i) => {
    const d = el('div', i === 0 ? 'v' : 'm', r);
    tip.appendChild(d);
  });
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}
const spegniTip = () => { tip.hidden = true; };

// le righe del suggerimento al passaggio del mouse
function righeDi(v) {
  return [
    `valvola ${v.valve_id} · ${v.azione}`,
    `costa ${eur(v.D)} · rende ${eur(v.R)} ogni ora di attesa`,
    v.stima && v.stima.esito === 'stimato'
      ? `crollo stimato fra ${ore(v.stima.ore_a_crollo_lo)} e ${ore(v.stima.ore_a_crollo_hi)}, centro ${ore(v.stima.ore_a_crollo_hat)}`
      : `stima: ${(v.stima && v.stima.nota) || 'assente'}`,
    motivoIt(v.motivo)
  ].filter(Boolean);
}

// ---------- la scelta ----------
// Un clic su una tessera porta quella valvola nella scheda grande. Senza
// nessun clic la scelta e' vuota e la scheda mostra la chiamata, come prima.
let scelta = null;

function scegli(id) {
  spegniTip();
  scelta = id;
  disegna();
}
function annullaScelta() {
  if (scelta === null) return;
  scelta = null;
  disegna();
}
addEventListener('keydown', (e) => { if (e.key === 'Escape') annullaScelta(); });

// ---------- il tempo della corsa ----------
const quando = (k) => new Date(T0 + k * ORA_MS);
const indiceDi = (iso) => Math.round((Date.parse(iso) - T0) / ORA_MS);
const isoDi = (k) => quando(k).toISOString().slice(0, 19) + 'Z';

function etichetta(k) {
  const t = quando(k);
  return t.getUTCDate() + ' ' + MESI[t.getUTCMonth()] + ', '
       + String(t.getUTCHours()).padStart(2, '0') + ':00';
}
function corto(k) {
  const t = quando(k);
  return t.getUTCDate() + ' ' + MESI[t.getUTCMonth()].slice(0, 3) + ' '
       + String(t.getUTCHours()).padStart(2, '0') + ':00';
}

// i conteggi di un'ora, presi dalla linea del tempo: non costano una richiesta
function conteggiDi(k) {
  const i = linea.i[k], d = linea.d[k];
  return { intervieni: i, continua_degradata: d, continua: N - i - d, senza_dati: null };
}

// ---------- avvisi ----------
function avvisa(testo) {
  const a = $('#avviso');
  if (!testo) { a.hidden = true; a.textContent = ''; return; }
  a.textContent = testo;
  a.hidden = false;
}

// ---------- dati ----------
async function carica() {
  collegaNav();
  montaTema();

  let motivo = null;
  try {
    const l = await get('valves/decision/timeline');
    if (l && l.degraded) motivo = l.reason || 'la rotta risponde in modo degradato';
    else if (!l || !Array.isArray(l.i) || !l.ore) motivo = 'risposta senza le ore della corsa';
    else linea = l;
  } catch (e) {
    motivo = e.message;
  }

  if (linea) montaBanda(); else bandaAssente(motivo);

  // L'ora di apertura: quella dell'indirizzo se c'e', altrimenti l'ultima ora
  // della corsa, che la rotta restituisce da sola quando non le si chiede niente.
  const chiesto = new URLSearchParams(location.search).get('adesso');
  if (!linea) { await chiediVerdetto(chiesto || null); return; }

  let k = ORE - 1;
  if (chiesto) {
    const ms = Date.parse(chiesto);
    if (!isFinite(ms)) {
      avvisa(`l’ora «${chiesto}» non si legge: aperta sull’ultima ora della corsa`);
    } else {
      const kk = indiceDi(chiesto);
      k = Math.max(0, Math.min(ORE - 1, kk));
      if (kk !== k) {
        avvisa(`l’ora chiesta sta fuori dalla corsa: portata a ${etichetta(k)}`);
      } else if (isoDi(k) !== chiesto) {
        avvisa(`l’ora chiesta non cade su un’ora piena: portata a ${etichetta(k)}`);
      }
    }
  }
  vaiOra(k);
  await chiediVerdetto(isoDi(k));
}

// La corsa e' quella della linea del tempo, e viaggia con ogni richiesta.
function percorsoVerdetto(iso) {
  const p = new URLSearchParams();
  if (linea && linea.run_id) p.set('run_id', linea.run_id);
  if (iso) p.set('adesso', iso);
  const q = p.toString();
  return 'valves/decision' + (q ? '?' + q : '');
}

async function chiediVerdetto(iso) {
  if (acRete) acRete.abort();
  acRete = new AbortController();
  const mio = acRete;

  if (iso) {
    const u = new URL(location.href);
    u.searchParams.set('adesso', iso);
    history.replaceState(null, '', u);
  }

  attesa = true;
  scelta = null;      // un'altra ora e' un'altra macchina: la scelta non vale piu'
  segnaAttesa(iso);

  try {
    const d = await get(percorsoVerdetto(iso), mio.signal);
    if (mio.signal.aborted) return;
    dati = d;
    attesa = false;
    segnaAttesa(iso);
    disegna();
  } catch (e) {
    if (e.name === 'AbortError') return;
    attesa = false;
    segnaAttesa(null);
    console.error(e);
    $('#prov').textContent = 'il verdetto non è arrivato: ' + e.message;
    $('#prov').classList.add('rotto');
    if (!dati) {
      const c = $('#centro');
      c.innerHTML = '';
      c.appendChild(el('div', 'guasto', 'verdetto non disponibile'));
      c.appendChild(el('div', 'nota', e.message));
    }
  }
}

// L'attesa si dichiara: i numeri vecchi restano leggibili ma spenti, e la
// testata dice che il verdetto sta arrivando. I conteggi dell'ora, invece,
// sono gia' giusti perche' vengono dalla linea del tempo.
function segnaAttesa(iso) {
  $('#anello').classList.toggle('attesa', attesa);
  $('#schede').classList.toggle('attesa', attesa);
  $('#prov').classList.remove('rotto');
  if (attesa) {
    const q = linea ? etichetta(oraK) : (iso ? giornoOra(iso) : 'ultima ora');
    $('#prov').textContent = `adesso ${q} · verdetto in arrivo`;
  }
}

// ---------- disegno ----------
function statoDi(v) {
  if (v.azione === 'intervieni') return 'grave';
  if (v.azione === 'continua degradata') return 'attenz';
  return 'neutro';
}
// La scheda scrive «nessuna stima» ogni volta che l'esito non e' `stimato`
// (vedi `stimato` in scheda()). La marca sulla tessera deve rispondere alla
// stessa domanda, altrimenti le due meta' dello schermo dicono cose diverse:
// elencare due esiti su nove lasciava senza marca proprio `nessun_segnale`,
// cioe' il caso piu' frequente da quando il segnale si cerca solo fino a adesso.
const senzaStima = (v) => !!v.stima && v.stima.esito !== 'stimato';

function disegna() {
  if (!dati) return;
  if (ac) ac.abort();
  ac = new AbortController();
  spegniTip();

  $('#prov').textContent =
    `adesso ${giornoOra(dati.adesso)} · corsa ${dati.run_id}`;

  const valvole = dati.valvole.slice().sort((a, b) => a.valve_id - b.valve_id);
  // la piu' urgente per D decrescente apre la fila: e' lei che prende la
  // scheda intera e che l'anello porta a ore tre
  const fuori = valvole
    .filter((v) => v.azione === 'intervieni')
    .sort((a, b) => b.D - a.D);

  // la scheda grande porta la valvola scelta. Senza scelta porta la chiamata
  // piu' urgente, che e' quello che si vede all'apertura della pagina.
  const vScelta = scelta === null ? null : valvole.find((v) => v.valve_id === scelta);
  const inScheda = vScelta || fuori[0] || null;

  disegnaAnello(valvole, fuori, inScheda);
  disegnaCentro(dati.conteggi);
  disegnaSchede(fuori, inScheda, !!vScelta);
}

// Le quattro squadre del segno della scelta. Restano fuori dalla tessera di
// 6 px e coprono solo gli angoli, quindi non si sommano mai al contorno del
// verdetto. Il raggio dell'anello e la posizione della tessera non cambiano.
function squadre(hh, ww) {
  const x = hh / 2 + 6, y = ww / 2 + 6;
  const a = Math.min(hh, ww) * 0.38;
  const p = [];
  [[-1, -1], [1, -1], [-1, 1], [1, 1]].forEach(([sx, sy]) => {
    const px = (x * sx).toFixed(1), py = (y * sy).toFixed(1);
    p.push(`M${(x * sx - a * sx).toFixed(1)},${py}L${px},${py}L${px},${(y * sy - a * sy).toFixed(1)}`);
  });
  return p.join(' ');
}

function disegnaAnello(valvole, fuori, inScheda) {
  const box = $('#anello').getBoundingClientRect();
  const W = Math.max(360, Math.round(box.width));
  const H = Math.max(360, Math.round(box.height));
  const s = $('#svg-anello');
  s.setAttribute('viewBox', `0 0 ${W} ${H}`);
  while (s.firstChild) s.removeChild(s.firstChild);

  const cx = W / 2, cy = H / 2;
  const passo = 360 / N;
  const hRad = 30, wTan = 38, sporgenza = 34;
  const R = Math.min(W, H) / 2 - hRad / 2 - sporgenza - 10;

  // la giostra indicizza: la valvola che deve uscire si porta davanti alla scheda,
  // a ore tre. Senza nessuna uscita, la valvola 1 sta in cima.
  const iFuori = fuori.length ? valvole.findIndex((v) => v.valve_id === fuori[0].valve_id) : -1;
  const start = iFuori >= 0 ? -iFuori * passo : -90;

  // indice fisso della posizione 1, per leggere l'orientamento
  const a1 = (start * Math.PI) / 180;
  const rIdx = R + hRad / 2 + 14;
  s.appendChild(svg('line', {
    class: 'indice-seg',
    x1: cx + Math.cos(a1) * (R + hRad / 2 + 4), y1: cy + Math.sin(a1) * (R + hRad / 2 + 4),
    x2: cx + Math.cos(a1) * rIdx, y2: cy + Math.sin(a1) * rIdx
  }));
  const et1 = svg('text', {
    class: 'indice', x: cx + Math.cos(a1) * (rIdx + 12), y: cy + Math.sin(a1) * (rIdx + 12),
    'text-anchor': 'middle', 'dominant-baseline': 'central'
  });
  et1.textContent = 'pos. 1';
  s.appendChild(et1);

  valvole.forEach((v, i) => {
    const ang = start + i * passo;
    const rad = (ang * Math.PI) / 180;
    const esce = v.azione === 'intervieni';
    const rr = R + (esce ? sporgenza : 0);
    const x = cx + Math.cos(rad) * rr;
    const y = cy + Math.sin(rad) * rr;

    // Una tessera uscita resta diritta: e' la sua posizione sull'anello che
    // cambia, non il suo orientamento. Alla posizione indicizzata, a ore tre,
    // l'angolo vale zero e il disegno e' identico a prima.
    const g = svg('g', {
      class: 'cella' + (esce ? ' fuori' : '')
        + (scelta !== null && v.valve_id === scelta ? ' scelta' : ''),
      transform: esce
        ? `translate(${x.toFixed(1)},${y.toFixed(1)})`
        : `translate(${x.toFixed(1)},${y.toFixed(1)}) rotate(${ang.toFixed(2)})`,
      tabindex: '0', role: 'button',
      'aria-pressed': scelta !== null && v.valve_id === scelta ? 'true' : 'false',
      'aria-label': `valvola ${v.valve_id}, ${v.azione}. La mostra nella scheda`
    });
    const cls = statoDi(v);
    const hh = esce ? hRad + 12 : hRad;
    const ww = esce ? wTan + 12 : wTan;
    const r = svg('rect', {
      class: 'sfondo' + (cls !== 'neutro' ? ' ' + cls : ''),
      x: -hh / 2, y: -ww / 2, width: hh, height: ww, rx: 3
    });
    g.appendChild(r);
    // La stima che manca e' un asse diverso dal verdetto e porta un segno suo,
    // appoggiato all'angolo in alto a destra della tessera. Compare solo dove
    // c'e' qualcosa da fare, quindi su una valvola sana non compare mai. Il
    // gruppo annidato annulla la rotazione dell'anello come fa il numero: la
    // marca resta un quadrato dritto in ogni posizione, altrimenti in alto si
    // leggerebbe come un rombo e sembrerebbe un secondo segno.
    if (senzaStima(v) && v.azione !== 'continua') {
      const gm = svg('g', {
        transform: `translate(${(hh / 2).toFixed(1)},${(-ww / 2).toFixed(1)})`
          + ` rotate(${(esce ? 0 : -ang).toFixed(2)})`
      });
      gm.appendChild(svg('rect', { class: 'marca', x: -3.5, y: -3.5, width: 7, height: 7 }));
      g.appendChild(gm);
    }
    // Il segno della scelta non usa il canale del verdetto e non e' un anello:
    // quattro squadre agli angoli, staccate dalla tessera. Un secondo anello
    // concentrico si legge come il contorno del verdetto ingrossato.
    g.appendChild(svg('path', { class: 'alone', d: squadre(hh, ww) }));

    const gi = svg('g', { transform: `rotate(${(esce ? 0 : -ang).toFixed(2)})` });
    const t = svg('text', { class: 'id', x: 0, y: 0 });
    t.textContent = v.valve_id;
    gi.appendChild(t);
    g.appendChild(gi);

    const righe = righeDi(v);
    const o = { signal: ac.signal };
    g.addEventListener('click', () => scegli(v.valve_id), o);
    g.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); scegli(v.valve_id); }
    }, o);
    g.addEventListener('mousemove', (e) => mostraTip(e, righe), o);
    g.addEventListener('mouseleave', spegniTip, o);
    g.addEventListener('focus', (e) => {
      const b = g.getBoundingClientRect();
      mostraTip({ clientX: b.left + b.width / 2, clientY: b.bottom }, righe);
    }, o);
    g.addEventListener('blur', spegniTip, o);
    g.addEventListener('keydown', (e) => { if (e.key === 'Escape') spegniTip(); }, o);

    s.appendChild(g);

    // La guida accompagna fino alla scheda solo la valvola indicizzata a ore
    // tre. Tirarla anche dalle altre farebbe attraversare l'anello a una riga
    // orizzontale, e la scheda intera e' una sola.
    if (esce && v.valve_id === fuori[0].valve_id) {
      s.appendChild(svg('line', {
        class: 'guida',
        x1: x + hRad / 2 + 8, y1: y, x2: W, y2: y
      }));
    }
  });
}

function disegnaCentro(c) {
  const n = $('#centro');
  n.innerHTML = '';

  const capo = el('div', 'capo');
  // la cifra prende la gravita' solo quando c'e' qualcosa da fermare
  capo.appendChild(el('span', 'n-big' + (c.intervieni > 0 ? ' grave' : ''),
    String(c.intervieni)));
  capo.appendChild(el('span', 'w-big', c.intervieni === 1 ? 'intervieni' : 'intervieni'));
  n.appendChild(capo);

  const r1 = el('div', 'riga');
  // stessa regola della cifra grande: la tinta compare solo se c'e' qualcosa da contare
  r1.appendChild(el('b', c.continua_degradata > 0 ? 'attenz' : null,
    String(c.continua_degradata)));
  r1.appendChild(document.createTextNode(' continua degradata'));
  n.appendChild(r1);

  const r2 = el('div', 'riga');
  r2.appendChild(el('b', null, String(c.continua)));
  r2.appendChild(document.createTextNode(` su ${N} continua`));
  n.appendChild(r2);

  const nota = el('div', 'nota',
    c.senza_dati ? `${c.senza_dati} senza dati · ordine di macchina`
                 : 'ordine di macchina');
  n.appendChild(nota);
}

function disegnaSchede(fuori, inScheda, conScelta) {
  const box = $('#schede');
  box.innerHTML = '';
  if (!inScheda) {
    const d = el('div', 'niente');
    d.appendChild(el('div', 'g', 'nessuna valvola esce'));
    d.appendChild(el('div', 'p', 'nessuna squadra da chiamare a quest’ora'));
    box.appendChild(d);
    return;
  }
  box.appendChild(scheda(inScheda, conScelta, fuori));
  // ogni chiamata che non sta nella scheda scende nella fila compatta, cosi'
  // la squadra da chiamare resta sempre sotto gli occhi
  const resto = fuori.filter((v) => v.valve_id !== inScheda.valve_id);
  if (resto.length) box.appendChild(altre(resto));
}

// le chiamate oltre la prima: una riga per valvola, colonne allineate
function altre(resto) {
  const w = el('div', 'altre');
  const cap = el('div', 'altre-cap');
  ['valvola', 'verdetto', 'costa un’ora', 'squadra fra'].forEach((t) =>
    cap.appendChild(el('span', null, t)));
  w.appendChild(cap);

  resto.forEach((v) => {
    const r = el('button', 'riga-c');
    r.type = 'button';
    r.setAttribute('aria-label',
      `valvola ${v.valve_id}, ${v.azione}. La mostra nella scheda`);
    r.appendChild(el('span', 'v', 'v' + v.valve_id));
    r.appendChild(el('span', 'a', v.azione));
    r.appendChild(el('span', 'd', eur(v.D) || '—'));
    r.appendChild(el('span', 'l', ore(v.squadra ? v.squadra.ore : dati.parametri.L_h) || '—'));
    r.addEventListener('click', () => scegli(v.valve_id));
    w.appendChild(r);
  });
  return w;
}

function scheda(v, conScelta, fuori) {
  const st0 = statoDi(v);
  const s = el('section', 'scheda' + (st0 === 'grave' ? '' : ' v-' + st0));

  const capo = el('div', 'scheda-capo');
  capo.appendChild(el('span', 'vv', 'v' + v.valve_id));
  capo.appendChild(el('span', 'az', v.azione));
  if (conScelta) {
    const chiam = (fuori || []).find((f) => f.valve_id !== v.valve_id);
    const b = el('button', 'torna',
      chiam ? 'torna alla chiamata v' + chiam.valve_id : 'togli la scelta');
    b.type = 'button';
    b.title = 'anche il tasto Esc';
    b.addEventListener('click', annullaScelta);
    capo.appendChild(b);
  }
  s.appendChild(capo);

  // D accostato a R: due numeri adiacenti sulla stessa scala
  const co = el('div', 'coppia');
  const max = Math.max(v.D, v.R, 0.01);
  co.appendChild(quadro(eur(v.D), 'costa un’ora di attesa', v.D / max));
  co.appendChild(quadro(eur(v.R), 'rende un’ora di attesa', v.R / max));
  s.appendChild(co);

  // le due ore, accostate, e la loro striscia
  const st = v.stima || {};
  const stimato = st.esito === 'stimato';
  // Per una valvola in «intervieni» l'ora della squadra c'e' sempre: o e' quella
  // della squadra gia' partita, o e' il ritardo nominale fra chiamata e arrivo,
  // che e' un parametro vero. Per una valvola che nessuno sta chiamando quel
  // numero sarebbe un'ipotesi, quindi al suo posto va la parola.
  const conSquadra = !!(v.squadra && v.squadra.ore != null) || v.azione === 'intervieni';
  const co2 = el('div', 'coppia');
  const chiamata = !!(v.conferma && v.conferma.chiamata);
  // `squadra.ore` e' il ritardo nominale fra chiamata e arrivo, cioe' sempre L.
  // Quando la squadra e' gia' partita quel numero non e' l'attesa di adesso: si
  // conta da `arrivo` meno `adesso`, altrimenti la scheda dice 24 h a undici ore
  // dalla partenza. Le due ore sono gia' tutte e due nella risposta.
  const mancano = quanteOreAll(v.squadra && v.squadra.arrivo);
  const oreSquadra = !conSquadra ? null
    : (chiamata && mancano !== null ? mancano
      : (v.squadra && v.squadra.ore != null ? v.squadra.ore : dati.parametri.L_h));
  // le caselle senza valore restano al loro posto e dicono a parole cosa manca
  co2.appendChild(conSquadra
    ? quadro(ore(oreSquadra),
        chiamata ? 'la squadra arriva fra' : 'dalla chiamata all’arrivo', null)
    : parole('nessuna squadra chiamata', 'niente arrivo da contare'));
  co2.appendChild(stimato
    ? quadro(ore(st.ore_a_crollo_lo), 'crollo stimato, al più presto', null)
    : parole('nessuna stima', st.nota || 'il canale non ha un regime sopra banda'));
  s.appendChild(co2);

  // senza ora della squadra e senza stima non c'e' niente da mettere sull'asse
  if (conSquadra || stimato) {
    const t = el('div', 'tempi');
    t.appendChild(striscia(oreSquadra, stimato ? st : null));
    s.appendChild(t);
  } else {
    s.appendChild(el('div', 'senza-asse',
      'nessuna ora da mettere sull’asse: niente arrivo e niente crollo stimato'));
  }

  // lo stato della chiamata: in viaggio, oppure conferme di fila
  const sr = el('div', 'stato-riga');
  if (chiamata) {
    const g = svg('svg', { width: '54', height: '14', class: 'pista', 'aria-hidden': 'true' });
    g.appendChild(svg('line', { class: 'tl-tratto', x1: 2, y1: 7, x2: 40, y2: 7 }));
    g.appendChild(svg('path', { class: 'tl-mark', d: 'M40 2 L50 7 L40 12', fill: 'none' }));
    sr.appendChild(g);
    sr.appendChild(el('span', null,
      `squadra in viaggio · arriva ${giornoOra(v.squadra && v.squadra.arrivo)}`));
  } else {
    const p = el('span', 'pips');
    for (let i = 0; i < (v.conferma ? v.conferma.K : 2); i++) {
      p.appendChild(el('i', i < (v.conferma ? v.conferma.di_fila : 0) ? 'on' : null));
    }
    sr.appendChild(p);
    sr.appendChild(el('span', null,
      `conferme ${v.conferma.di_fila} di ${v.conferma.K} · squadra non chiamata`));
  }
  s.appendChild(sr);

  // Canale e classe vengono dalla configurazione della politica e sono uguali
  // per tutte e 35 le valvole, sane comprese. Scriverli come «classe: X» fa
  // credere che quella valvola abbia quel guasto. Si dichiarano come cio' che
  // il sistema sorveglia.
  if (conScelta) {
    const sg = el('div', 'segnale');
    const c1 = el('div');
    c1.appendChild(el('span', null, 'sorvegliata sul canale '));
    c1.appendChild(el('b', null, v.canale || 'non dichiarato'));
    const c2 = el('div');
    c2.appendChild(el('span', null, 'per la classe di guasto '));
    c2.appendChild(el('b', null, v.classe || 'non dichiarata'));
    sg.appendChild(c1);
    sg.appendChild(c2);
    s.appendChild(sg);
  }
  return s;
}

// la casella che non ha un numero: dichiara l'assenza e tiene il suo posto
function parole(titolo, motivo) {
  const q = el('div', 'q');
  q.appendChild(el('div', 'parole', titolo));
  q.appendChild(el('div', 'et', motivo));
  return q;
}

function quadro(numero, etichetta, quota, vuoto) {
  const q = el('div', 'q');
  const n = el('div', 'num', numero == null ? '—' : numero);
  if (vuoto) n.style.color = 'var(--muto)';
  q.appendChild(n);
  q.appendChild(el('div', 'et', etichetta));
  if (quota != null) {
    const b = el('span', 'barra');
    const i = el('i');
    i.style.width = Math.max(2, Math.round(quota * 100)) + '%';
    b.appendChild(i);
    q.appendChild(b);
  }
  return q;
}

// striscia dei tempi: adesso · arrivo squadra · crollo stimato
function striscia(oreSquadra, st) {
  const W = 600, H = 78, y = 44, x0 = 34, x1 = W - 34;
  const tmax = Math.max(oreSquadra || 0, st ? st.ore_a_crollo_hi : 0, 1) * 1.08;
  const X = (h) => x0 + (x1 - x0) * (h / tmax);
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-hidden': 'true' });

  s.appendChild(svg('line', { class: 'tl-asse', x1: x0, y1: y, x2: x1, y2: y }));
  // l'attesa fino alla squadra, quando una squadra c'e'
  if (oreSquadra != null) {
    s.appendChild(svg('line', { class: 'tl-tratto', x1: x0, y1: y, x2: X(oreSquadra), y2: y }));
  }

  const tacca = (h, cls, testo, sotto, ancora) => {
    s.appendChild(svg('line', { class: cls, x1: X(h), y1: y - 13, x2: X(h), y2: y + 13 }));
    const t = svg('text', {
      class: cls === 'tl-mark' ? 'tl-et' : 'tl-et-m',
      x: X(h), y: sotto ? y + 30 : y - 20, 'text-anchor': ancora || 'middle'
    });
    t.textContent = testo;
    s.appendChild(t);
  };

  tacca(0, 'tl-mark', 'adesso', true, 'start');
  if (oreSquadra != null) {
    tacca(oreSquadra, 'tl-mark', 'squadra ' + ore(oreSquadra), false);
  } else {
    const t = svg('text', { class: 'tl-et-m', x: x0, y: y - 20, 'text-anchor': 'start' });
    t.textContent = 'nessuna squadra chiamata';
    s.appendChild(t);
  }
  if (st) {
    // la banda della stima resta muta: il numero che decide è l'estremo basso
    s.appendChild(svg('line', {
      class: 'tl-mark-m', x1: X(st.ore_a_crollo_lo), y1: y, x2: X(st.ore_a_crollo_hi), y2: y,
      'stroke-dasharray': '4 4'
    }));
    tacca(st.ore_a_crollo_lo, 'tl-mark', 'crollo ' + ore(st.ore_a_crollo_lo), true);
    s.appendChild(svg('line', {
      class: 'tl-mark-m', x1: X(st.ore_a_crollo_hi), y1: y - 8,
      x2: X(st.ore_a_crollo_hi), y2: y + 8
    }));
  } else {
    // ancorata a destra e sotto l'asse: partendo dall'arrivo della squadra
    // la scritta usciva dal riquadro e si leggeva «crollo: nessi»
    const t = svg('text', {
      class: 'tl-et-m', x: x1, y: y + 30, 'text-anchor': 'end'
    });
    t.textContent = 'crollo: nessuna stima';
    s.appendChild(t);
  }
  return s;
}

// ---------- la banda che sceglie l'ora ----------
// Forma approvata dall'utente il 2026-09-15: la striscia dei due mesi da
// trascinare, con le tacche dei momenti che contano e, sotto, la fila di
// bottoni per quegli stessi momenti.
//
// Una colonna per ora. L'altezza grigia dice quante valvole sono «continua
// degradata»; un segno rosso pieno sta sulle ore con almeno un «intervieni».
// Niente giallo: le ore con almeno una degradata sono 909 su 1.407, e
// tingerle renderebbe la striscia gialla per due terzi senza dire piu' niente.
function disegnaStriscia(s, segna) {
  while (s.firstChild) s.removeChild(s.firstChild);
  const base = 41;

  let g = '';
  for (let k = 0; k < ORE; k++) {
    if (!linea.d[k]) continue;
    g += 'M' + k + ' ' + base + 'v' + -(3 + (linea.d[k] / maxD) * 23);
  }
  s.appendChild(svg('path', { class: 'st-degr', d: g }));
  s.appendChild(svg('path', { class: 'st-base', d: 'M0 ' + base + 'H' + ORE }));

  let r = '';
  for (let k = 0; k < ORE; k++) if (linea.i[k]) r += 'M' + k + ' 5v36';
  s.appendChild(svg('path', { class: 'st-grave', d: r }));

  // i nomi dei mesi, alla prima ora di ogni mese
  for (let k = 0; k < ORE; k++) {
    const t = quando(k);
    if (t.getUTCDate() !== 1 || t.getUTCHours() !== 0) continue;
    s.appendChild(svg('path', { class: 'st-base', d: 'M' + k + ' ' + base + 'v4' }));
    const tx = svg('text', { class: 'st-mese', x: k + 6, y: 50 });
    tx.textContent = MESI[t.getUTCMonth()];
    s.appendChild(tx);
  }

  if (segna) {
    MOMENTI.forEach((m) => {
      s.appendChild(svg('path', { class: 'st-tacca', d: 'M' + (m.k - 7) + ' 0h14l-7 7z' }));
      s.appendChild(svg('path', { class: 'st-guida', d: 'M' + m.k + ' 7v34' }));
    });
  }

  const cur = svg('path', { class: 'st-cursore' });
  s.appendChild(cur);
  return cur;
}

// I momenti si calcolano dai dati: la chiamata e l'arrivo di ogni squadra,
// piu' l'ultima ora della corsa. Su un'altra corsa saranno altri, e la fila
// scorre invece di andare a capo e rubare altezza all'anello.
function montaMomenti(box, alClic) {
  const btns = [];
  const voci = MOMENTI.slice();
  voci.push({ k: ORE - 1, t: 'ultima ora' });
  voci.forEach((m) => {
    const b = el('button', 'mom');
    b.type = 'button';
    b.dataset.k = m.k;
    b.setAttribute('aria-pressed', 'false');
    b.appendChild(document.createTextNode(corto(m.k)));
    b.appendChild(el('small', null, m.t));
    b.addEventListener('click', () => alClic(m.k));
    box.appendChild(b);
    btns.push(b);
  });
  return btns;
}

// Trascinare non chiede niente alla rete: cursore e conteggi dell'ora sono
// gia' nella linea del tempo. Il verdetto delle 35 valvole si chiede al
// rilascio, e la richiesta di prima si annulla.
function vaiOra(nuovo) {
  oraK = Math.max(0, Math.min(ORE - 1, Math.round(nuovo)));
  const s = $('#striscia');
  curLinea.setAttribute('d', 'M' + oraK + ' 0v43');
  s.setAttribute('aria-valuenow', oraK);
  s.setAttribute('aria-valuetext', etichetta(oraK));
  btnMom.forEach((b) =>
    b.setAttribute('aria-pressed', String(Number(b.dataset.k) === oraK)));
  disegnaCentro(conteggiDi(oraK));
  if (!attesa) $('#prov').textContent = `adesso ${etichetta(oraK)} · verdetto in arrivo`;
}

function montaBanda() {
  ORE = linea.ore;
  T0 = Date.parse(linea.prima_ora);
  maxD = Math.max.apply(null, linea.d) || 1;

  MOMENTI = [];
  (linea.chiamate || []).forEach((c) => {
    MOMENTI.push({ k: indiceDi(c.chiamata), t: 'chiamata v' + c.valvola });
    MOMENTI.push({ k: indiceDi(c.arrivo), t: 'squadra su v' + c.valvola });
  });
  MOMENTI.sort((a, b) => a.k - b.k);

  const s = $('#striscia');
  s.setAttribute('viewBox', `0 0 ${ORE} ${HB}`);
  s.setAttribute('aria-valuemin', 0);
  s.setAttribute('aria-valuemax', ORE - 1);
  curLinea = disegnaStriscia(s, true);

  btnMom = montaMomenti($('#momenti'), (k) => { vaiOra(k); chiediVerdetto(isoDi(k)); });

  const daEvento = (ev) => {
    const r = s.getBoundingClientRect();
    vaiOra(((ev.clientX - r.left) / r.width) * ORE);
  };
  s.addEventListener('pointerdown', (ev) => {
    s.setPointerCapture(ev.pointerId);
    daEvento(ev);
  });
  s.addEventListener('pointermove', (ev) => { if (ev.buttons) daEvento(ev); });
  s.addEventListener('pointerup', () => chiediVerdetto(isoDi(oraK)));
  s.addEventListener('pointercancel', () => chiediVerdetto(isoDi(oraK)));

  // da tastiera: un'ora, un giorno, gli estremi. Il verdetto parte quando i
  // tasti si fermano, cosi' tenere premuta una freccia non lancia cento richieste.
  let attesaTasti = null;
  s.addEventListener('keydown', (ev) => {
    const p = { ArrowLeft: -1, ArrowRight: 1, PageUp: 24, PageDown: -24,
                Home: -ORE, End: ORE }[ev.key];
    if (p === undefined) return;
    ev.preventDefault();
    vaiOra(oraK + p);
    clearTimeout(attesaTasti);
    attesaTasti = setTimeout(() => chiediVerdetto(isoDi(oraK)), 260);
  });
}

// L'assenza si dichiara: la giostra resta sull'ultima ora e al posto della
// striscia c'e' una riga che dice perche' la linea del tempo non c'e'.
function bandaAssente(motivo) {
  $('#striscia').hidden = true;
  $('#momenti').hidden = true;
  const b = $('#banda-assente');
  b.hidden = false;
  b.appendChild(el('b', null, 'la linea del tempo non è disponibile'));
  b.appendChild(el('span', null,
    motivo ? '· ' + motivo : '· la rotta non ha risposto'));
  b.appendChild(el('span', null, '· la giostra resta sull’ultima ora della corsa'));
}

// ---------- avvio ----------
let timer = null;
addEventListener('resize', () => { clearTimeout(timer); timer = setTimeout(disegna, 120); });
carica().catch((e) => {
  console.error(e);
  const c = $('#centro');
  c.innerHTML = '';
  c.appendChild(el('div', 'guasto', 'la pagina non è partita'));
  c.appendChild(el('div', 'nota', e.message));
});
