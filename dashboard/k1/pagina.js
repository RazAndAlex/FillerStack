// CARTA — le due carte di controllo sulla stessa valvola, impilate e
// allineate sull'asse dei cicli. Nessun numero che non venga dall'API.

import { num, scenarioCorrente, collegaNav } from '/comune/dati.js';

/* ---- la navigazione conserva lo scenario ---- */
collegaNav();

const N_FIN = 46;                 // periodo dell'oscillazione, dall'API
const LIMITE = 5000;              // massimo ammesso dalla route
const GRANDEZZA = 'filling_time_ms';

const SVG = 'http://www.w3.org/2000/svg';
const el = (id) => document.getElementById(id);

let base = null;                  // /valves/baseline
let valvola = 1;                  // non si parte dalla 21
let serie = null;                 // cicli ordinati per cycle_id
let mira = null;                  // indice sotto il puntatore
let ac = { uno: null, due: null, str: null };

// ---------------------------------------------------------------- utilita'
function e(tag, attr, testo) {
  const n = document.createElementNS(SVG, tag);
  for (const k in attr) if (attr[k] !== null && attr[k] !== undefined)
    n.setAttribute(k, attr[k]);
  if (testo !== undefined) n.textContent = testo;
  return n;
}
function svuota(n) { while (n.firstChild) n.removeChild(n.firstChild); }

// Il riquadro decide le dimensioni: il viewBox si riscrive in pixel reali
// a ogni disegno, cosi' il testo non si deforma mai.
function misura(svg) {
  const r = svg.getBoundingClientRect();
  const w = Math.max(320, Math.round(r.width));
  const h = Math.max(60, Math.round(r.height));
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  return { w, h };
}

// La tinta la prende solo cio' che ha una gravita': dentro la banda, niente.
// La rampa segue il picco di scostamento misurato, in sigma della carta.
function tinta(zmax) {
  if (!(zmax > 3)) return null;
  if (zmax <= 4) return 'var(--sev2)';
  if (zmax <= 6) return 'var(--sev3)';
  return 'var(--sev4)';
}

const ms = (v, d = 0) => num(v, d) + ' ms';
// Un numero di sigma si scrive sempre con una cifra: "3" e "3,0" dicono cose
// diverse su una carta di controllo.
const dec = (v, d = 1) => v === null || v === undefined ? '—'
  : v.toLocaleString('it-IT', { minimumFractionDigits: d, maximumFractionDigits: d });

const qpc = (f) => f == null ? 'non calc.'
  : (f * 100).toLocaleString('it-IT',
      { maximumFractionDigits: f > 0 && f < 0.01 ? 2 : 1 }) + '%';

// ---------------------------------------------------------------- dati
async function caricaBase() {
  const r = await fetch(`/api/${scenarioCorrente()}/valves/baseline`,
                        { cache: 'no-store' });
  if (!r.ok) throw new Error(`valves/baseline -> HTTP ${r.status}`);
  base = await r.json();
}

async function caricaSerie(id) {
  const p = `valves/${id}/kpi?limit=${LIMITE}`;
  const r = await fetch(`/api/${scenarioCorrente()}/${p}`, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${p} -> HTTP ${r.status}`);
  const d = await r.json();
  const s = (d.series || []).slice().sort((a, b) => a.cycle_id - b.cycle_id);
  return { righe: s, route: p, motivo: d.reason || null };
}

// Media mobile a passo 1 su finestre piene. Un null nella finestra rompe la
// finestra: il punto non si calcola, e la linea si spezza invece di scendere.
function mediaMobile(x, n) {
  const out = new Array(Math.max(0, x.length - n + 1)).fill(null);
  let somma = 0, buchi = 0;
  for (let i = 0; i < x.length; i++) {
    const v = x[i];
    if (v === null || v === undefined) buchi++; else somma += v;
    if (i >= n) {
      const u = x[i - n];
      if (u === null || u === undefined) buchi--; else somma -= u;
    }
    if (i >= n - 1) out[i - n + 1] = buchi ? null : somma / n;
  }
  return out;
}

// ---------------------------------------------------------------- carta
// Una sola convenzione grafica, disegnata due volte: stesso asse dei cicli,
// stesso verso, stessa costruzione. Cambia solo la scala verticale, perche'
// le due bande non sono commensurabili.
function carta(svg, opz) {
  const { w, h } = misura(svg);
  svuota(svg);
  if (ac[opz.chiave]) ac[opz.chiave].abort();
  ac[opz.chiave] = new AbortController();
  const seg = ac[opz.chiave].signal;

  const L = 66, R = 52, T = 12, B = opz.assex ? 26 : 12;
  const x0 = L, x1 = w - R, y0 = T, y1 = h - B;
  if (x1 <= x0 || y1 <= y0) return;

  const { media, sigma, punti, sfasa, nTot } = opz;

  // dominio verticale: contiene sempre banda + dati, con margine dell'8%,
  // cosi' la traiettoria non si schiaccia mai contro un bordo.
  const vis = punti.filter(v => v !== null && v !== undefined);
  if (!vis.length) {
    svg.appendChild(e('text', {
      x: w / 2, y: h / 2, 'text-anchor': 'middle',
      fill: 'var(--muto)', 'font-size': 12
    }, 'serie non disponibile'));
    return;
  }
  const dmin = Math.min(...vis), dmax = Math.max(...vis);
  let lo = Math.min(media - 3 * sigma, dmin);
  let hi = Math.max(media + 3 * sigma, dmax);
  const pad = (hi - lo) * 0.08 || 1;
  lo -= pad; hi += pad;

  const X = (i) => x0 + (nTot <= 1 ? 0 : (i / (nTot - 1)) * (x1 - x0));
  const Y = (v) => y1 - ((v - lo) / (hi - lo)) * (y1 - y0);
  const Z = (v) => (v - media) / sigma;

  // --- banda, poi tacca: sotto tutto il resto
  svg.appendChild(e('rect', {
    x: x0, y: Y(media + 3 * sigma), width: x1 - x0,
    height: Math.max(1, Y(media - 3 * sigma) - Y(media + 3 * sigma)),
    fill: 'var(--traccia)'
  }));

  for (const q of [3, -3]) svg.appendChild(e('line', {
    x1: x0, y1: Y(media + q * sigma), x2: x1, y2: Y(media + q * sigma),
    stroke: 'var(--banda)', 'stroke-width': 1
  }));

  // --- il tratto scartato dai calcoli occupa il posto che gli spetta
  if (sfasa > 0) {
    const d = e('defs');
    const p = e('pattern', {
      id: `h-${opz.chiave}`, width: 6, height: 6,
      patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)'
    });
    p.appendChild(e('line', {
      x1: 0, y1: 0, x2: 0, y2: 6, stroke: 'var(--bordo)', 'stroke-width': 1
    }));
    d.appendChild(p); svg.appendChild(d);
    svg.appendChild(e('rect', {
      x: x0, y: y0, width: Math.max(0, X(sfasa) - x0), height: y1 - y0,
      fill: `url(#h-${opz.chiave})`
    }));
    svg.appendChild(e('line', {
      x1: X(sfasa), y1: y0, x2: X(sfasa), y2: y1,
      stroke: 'var(--ink)', 'stroke-width': 2
    }));
  }

  svg.appendChild(e('line', {
    x1: x0, y1: Y(media), x2: x1, y2: Y(media),
    stroke: 'var(--rif)', 'stroke-width': 1.5, 'stroke-dasharray': '5 4'
  }));

  // --- il divario fra la traiettoria e il suo riferimento si riempie
  let area = '', dentro = false;
  for (let j = 0; j < punti.length; j++) {
    const v = punti[j];
    if (v === null || v === undefined) {
      if (dentro) { area += ` L ${X(j - 1 + sfasa)} ${Y(media)} Z`; dentro = false; }
      continue;
    }
    const px = X(j + sfasa);
    if (!dentro) { area += ` M ${px} ${Y(media)} L ${px} ${Y(v)}`; dentro = true; }
    else area += ` L ${px} ${Y(v)}`;
  }
  if (dentro) area += ` L ${X(punti.length - 1 + sfasa)} ${Y(media)} Z`;
  svg.appendChild(e('path', {
    d: area.trim(), fill: 'var(--dato)', 'fill-opacity': 0.14, stroke: 'none'
  }));

  // --- la traiettoria: marchio piu' in alto, mai coperta
  let linea = '', giu = true;
  for (let j = 0; j < punti.length; j++) {
    const v = punti[j];
    if (v === null || v === undefined) { giu = true; continue; }
    linea += `${giu ? 'M' : 'L'} ${X(j + sfasa)} ${Y(v)} `;
    giu = false;
  }
  const zmax = Math.max(...vis.map(v => Math.abs(Z(v))));
  const tin = tinta(zmax);
  svg.appendChild(e('path', {
    d: linea.trim(), fill: 'none',
    stroke: tin || 'var(--dato)', 'stroke-width': vis.length > 2000 ? 0.9 : 1.2,
    'stroke-linejoin': 'round'
  }));

  // --- marcatori solo sugli attraversamenti della banda
  let fuoriPrec = null, fuori = 0;
  for (let j = 0; j < punti.length; j++) {
    const v = punti[j];
    if (v === null || v === undefined) { fuoriPrec = null; continue; }
    const f = Math.abs(Z(v)) > 3;
    if (f) fuori++;
    if (fuoriPrec !== null && f !== fuoriPrec) {
      svg.appendChild(e('circle', {
        cx: X(j + sfasa), cy: Y(v), r: 3.2,
        fill: 'var(--sup)', stroke: tin || 'var(--dato)', 'stroke-width': 1.6
      }));
    }
    fuoriPrec = f;
  }

  // --- asse dei millisecondi a sinistra. Due etichette non si sovrappongono
  // mai: quella che perde non si scrive. Il riferimento ha la precedenza.
  const presi = [];
  const libero = (y) => presi.every(q => Math.abs(q - y) >= 12);
  for (const v of [media, media + 3 * sigma, media - 3 * sigma]) {
    const y = Y(v);
    if (!libero(y)) continue;
    presi.push(y);
    svg.appendChild(e('text', {
      x: x0 - 8, y: y + 3.5, 'text-anchor': 'end',
      fill: 'var(--muto)', 'font-size': 10.5
    }, ms(v, 1)));
  }

  // --- asse sigma a destra: e' lui a dire quanto e' fuori
  svg.appendChild(e('line', {
    x1: x1, y1: y0, x2: x1, y2: y1, stroke: 'var(--bordo)', 'stroke-width': 1
  }));
  const zpicco = Math.round(zmax * 10) / 10;
  const zt = zpicco > 4 ? [Z(dmax) > 0 ? zpicco : -zpicco, 0, 3, -3] : [0, 3, -3];
  const presiZ = [];
  for (const z of zt) {
    const v = media + z * sigma;
    if (v < lo || v > hi) continue;
    const y = Y(v);
    if (!presiZ.every(q => Math.abs(q - y) >= 12)) continue;
    presiZ.push(y);
    svg.appendChild(e('line', {
      x1: x1, y1: y, x2: x1 + 4, y2: y,
      stroke: 'var(--bordo)', 'stroke-width': 1
    }));
    const forte = Math.abs(z) > 3;
    svg.appendChild(e('text', {
      x: x1 + 7, y: y + 3.5, fill: forte ? (tin || 'var(--dato)') : 'var(--muto)',
      'font-size': 10.5, 'font-weight': forte ? 600 : 400
    }, z === 0 ? '0 σ' : `${z > 0 ? '+' : '−'}${dec(Math.abs(z), 1)} σ`));
  }

  // --- asse dei cicli, scritto una volta sola in fondo alla coppia
  if (opz.assex) {
    for (const i of [0, Math.round((nTot - 1) / 2), nTot - 1]) {
      const r = opz.righe[i];
      if (!r) continue;
      svg.appendChild(e('text', {
        x: Math.min(x1 - 2, Math.max(x0 + 2, X(i))), y: y1 + 15,
        'text-anchor': i === 0 ? 'start' : (i === nTot - 1 ? 'end' : 'middle'),
        fill: 'var(--muto)', 'font-size': 10.5
      }, `ciclo ${num(r.cycle_id)}`));
    }
  }

  // --- mira condivisa: lo stesso istante nelle due carte
  const g = e('g', { id: `mira-${opz.chiave}` });
  svg.appendChild(g);

  const disegnaMira = () => {
    svuota(g);
    if (mira === null) return;
    const px = X(mira);
    g.appendChild(e('line', {
      x1: px, y1: y0, x2: px, y2: y1,
      stroke: 'var(--ink)', 'stroke-width': 1, 'stroke-opacity': .55
    }));
    const j = mira - sfasa;
    const v = punti[j];
    if (v === null || v === undefined) return;
    g.appendChild(e('circle', {
      cx: px, cy: Y(v), r: 3.4, fill: 'var(--sup)',
      stroke: 'var(--ink)', 'stroke-width': 1.6
    }));
  };
  opz.disegnaMira = disegnaMira;
  disegnaMira();

  // --- interazione: bersaglio sulla X piu' vicina, non sul punto
  const indiceDa = (cx) => {
    const r = svg.getBoundingClientRect();
    const px = (cx - r.left) * (w / r.width);
    const t = (px - x0) / Math.max(1, x1 - x0);
    return Math.max(0, Math.min(nTot - 1, Math.round(t * (nTot - 1))));
  };
  svg.addEventListener('pointermove', (ev) => {
    mira = indiceDa(ev.clientX); aggiornaMira(ev.clientX, ev.clientY);
  }, { signal: seg });
  svg.addEventListener('pointerleave', () => {
    mira = null; aggiornaMira();
  }, { signal: seg });
  svg.addEventListener('keydown', (ev) => {
    let m = mira === null ? nTot - 1 : mira;
    if (ev.key === 'ArrowLeft') m -= ev.shiftKey ? 50 : 1;
    else if (ev.key === 'ArrowRight') m += ev.shiftKey ? 50 : 1;
    else if (ev.key === 'Home') m = 0;
    else if (ev.key === 'End') m = nTot - 1;
    else if (ev.key === 'Escape') { mira = null; aggiornaMira(); return; }
    else return;
    ev.preventDefault();
    mira = Math.max(0, Math.min(nTot - 1, m));
    const r = svg.getBoundingClientRect();
    aggiornaMira(r.left + (X(mira) / w) * r.width, r.top + r.height / 2);
  }, { signal: seg });
  svg.addEventListener('focus', () => {
    if (mira === null) { mira = nTot - 1; aggiornaMira(); }
  }, { signal: seg });

  svg.setAttribute('aria-label',
    `${opz.titolo}: ${num(vis.length)} punti, riferimento ${ms(media, 1)}, ` +
    `banda ±3σ = ±${ms(3 * sigma, 1)}, ${num(fuori)} punti fuori banda`);

  return { fuori, tot: vis.length, zmax, banda: 3 * sigma, Y, X, punti, sfasa };
}

// ---------------------------------------------------------------- mira
let stato = { uno: null, due: null };

function aggiornaMira(cx, cy) {
  if (stato.uno && stato.uno.opz.disegnaMira) stato.uno.opz.disegnaMira();
  if (stato.due && stato.due.opz.disegnaMira) stato.due.opz.disegnaMira();
  const tip = el('tip');
  if (mira === null || cx === undefined) { tip.hidden = true; return; }

  const r = serie.righe[mira];
  const b = base.valves[String(valvola)][GRANDEZZA];
  const v1 = r ? r[GRANDEZZA] : null;
  const j = mira - (N_FIN - 1);
  const v2 = stato.due ? stato.due.opz.punti[j] : null;
  const z1 = v1 === null || v1 === undefined ? null : (v1 - b.mean) / b.sigma_full;
  const z2 = v2 === null || v2 === undefined ? null : (v2 - b.mean) / b.sigma_media_46;
  const rg = (z) => z === null ? '' :
    (Math.abs(z) > 3 ? ` <span class="f">${z > 0 ? '+' : '−'}${dec(Math.abs(z))} σ</span>`
                     : ` <span class="m">${z > 0 ? '+' : '−'}${dec(Math.abs(z))} σ</span>`);

  const t = r && r.event_ts
    ? new Date(r.event_ts).toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'medium' })
    : '—';
  tip.innerHTML =
    `<span class="m">ciclo ${r ? num(r.cycle_id) : '—'} · ${t}</span><br>` +
    `<span class="v">${v1 === null || v1 === undefined ? 'non disp.' : ms(v1)}</span>` +
    `${rg(z1)} <span class="m">ciclo singolo</span><br>` +
    `<span class="v">${v2 === null || v2 === undefined ? 'finestra non piena' : ms(v2, 1)}</span>` +
    `${rg(z2)} <span class="m">media di 46</span>`;
  tip.hidden = false;
  const q = tip.getBoundingClientRect();
  let x = cx + 14, y = cy + 14;
  if (x + q.width > innerWidth - 6) x = cx - q.width - 14;
  if (y + q.height > innerHeight - 6) y = cy - q.height - 14;
  tip.style.left = Math.max(6, x) + 'px';
  tip.style.top = Math.max(6, y) + 'px';
}

// ---------------------------------------------------------------- striscia
const CIECHE = [13, 14, 15, 16, 17, 18];

// La striscia porta il verdetto di ogni valvola: due quote di punti fuori
// banda, una per carta. Stanno su una riga sola sotto il numero, divisa a
// meta': sinistra la carta del ciclo singolo, destra quella della media di
// 46. Tenerle accostate e' una richiesta dell'utente — una sopra e una sotto
// separava troppo due verdetti che vanno confrontati fra loro.
const quote = {};   // id -> {f1, f46} misurata | {errore} | assente = non misurata

// Rampa di gravita' sulla quota fuori banda (innestata da k3): nulla resta
// neutra, non c'e' un verde.
function tintaQuota(f) {
  if (f == null || f <= 0) return null;
  if (f <= 0.05) return 'var(--sev1)';
  if (f <= 0.25) return 'var(--sev2)';
  if (f <= 0.60) return 'var(--sev3)';
  return 'var(--sev4)';
}

// Le due quote di una valvola, dalla sua serie: stessa banda ±3σ delle carte.
function quoteDa(righe, id) {
  const b = base.valves[String(id)] && base.valves[String(id)][GRANDEZZA];
  const x = righe.map(r => r[GRANDEZZA]).filter(v => v !== null && v !== undefined);
  if (!b || !x.length) return { errore: true };
  let f1 = null, f46 = null;
  if (b.mean != null && b.sigma_full) {
    let k = 0;
    for (const v of x) if (Math.abs(v - b.mean) > 3 * b.sigma_full) k++;
    f1 = k / x.length;
  }
  if (b.mean != null && b.sigma_media_46) {
    const mm = mediaMobile(x, N_FIN).filter(v => v !== null);
    let k = 0;
    for (const v of mm) if (Math.abs(v - b.mean) > 3 * b.sigma_media_46) k++;
    f46 = mm.length ? k / mm.length : null;
  }
  return { f1, f46 };
}

function striscia() {
  const svg = el('g-str');
  const { w, h } = misura(svg);
  svuota(svg);
  if (ac.str) ac.str.abort();
  ac.str = new AbortController();
  const seg = ac.str.signal;

  const ids = Object.keys(base.valves).map(Number).sort((a, b) => a - b);
  const L = 4, R = 4, T = 4;
  const alt = 34;
  const pas = (w - L - R) / ids.length;
  const filo = 5;                 // la riga tinta sotto il numero

  const d = e('defs');
  const p = e('pattern', {
    id: 'h-str', width: 5, height: 5,
    patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)'
  });
  p.appendChild(e('line', { x1: 0, y1: 0, x2: 0, y2: 5, stroke: 'var(--bordo)', 'stroke-width': 1 }));
  d.appendChild(p);
  // una cella non ancora misurata si dichiara: tratteggio fitto, che non
  // somiglia al fondo di una cella misurata e sana.
  const p2 = e('pattern', {
    id: 'h-att', width: 4, height: 4,
    patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)'
  });
  p2.appendChild(e('rect', { x: 0, y: 0, width: 4, height: 4, fill: 'var(--sup)' }));
  p2.appendChild(e('line', { x1: 0, y1: 0, x2: 0, y2: 4, stroke: 'var(--bordo)', 'stroke-width': 1.2 }));
  d.appendChild(p2);
  svg.appendChild(d);

  ids.forEach((id, k) => {
    const x = L + k * pas;
    const q = quote[id];
    const t1 = q && !q.errore ? tintaQuota(q.f1) : null;
    const t46 = q && !q.errore ? tintaQuota(q.f46) : null;
    const dett = !q ? 'non ancora misurata'
      : q.errore ? 'non misurabile'
      : `ciclo singolo ${qpc(q.f1)} fuori banda, media di 46 ${qpc(q.f46)}`;
    const g = e('g', {
      class: 'cella-pop', tabindex: 0, role: 'button',
      'aria-label': `Valvola ${id}${id === valvola ? ', in vista' : ''}: ${dett}`
    });
    g.appendChild(e('rect', {
      class: 'sfondo', x, y: T, width: pas, height: alt,
      fill: !q ? 'url(#h-att)' : (id === valvola ? 'var(--sup-2)' : 'transparent'),
      stroke: id === valvola ? 'var(--ink)' : 'none',
      'stroke-width': id === valvola ? 2 : 0
    }));
    // Una riga sola sotto il numero, divisa a meta': il colore sta sul bordo
    // e non addosso alla cifra, che resta l'identita' della valvola.
    if (q && !q.errore) {
      const mez = (pas - 8) / 2;
      if (t1) g.appendChild(e('rect', {
        x: x + 4, y: T + alt - filo - 2, width: mez, height: filo, fill: t1
      }));
      if (t46) g.appendChild(e('rect', {
        x: x + 4 + mez, y: T + alt - filo - 2, width: mez, height: filo, fill: t46
      }));
    }
    if (q && q.errore) g.appendChild(e('text', {
      x: x + pas / 2, y: T + 11, 'text-anchor': 'middle',
      fill: 'var(--muto)', 'font-size': 9
    }, '—'));
    g.appendChild(e('text', {
      class: 'num', x: x + pas / 2, y: T + alt / 2 + 4.5, 'text-anchor': 'middle',
      fill: 'var(--ink)', 'font-size': 12,
      'font-weight': id === valvola ? 700 : 400
    }, String(id)));
    const vai = () => { if (id !== valvola) { valvola = id; apri(); } };
    g.addEventListener('click', vai, { signal: seg });
    g.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); vai(); }
    }, { signal: seg });
    svg.appendChild(g);
  });

  // le due carte sono cieche sull'instabilita' di pressione delle 13-18:
  // si dichiara con un segno sull'asse, dove stanno quelle valvole.
  const a = ids.indexOf(CIECHE[0]), b = ids.indexOf(CIECHE[CIECHE.length - 1]);
  if (a >= 0 && b >= 0) {
    const xa = L + a * pas, xb = L + (b + 1) * pas;
    svg.appendChild(e('rect', {
      x: xa, y: T + alt + 3, width: xb - xa, height: 8, fill: 'url(#h-str)'
    }));
    svg.appendChild(e('line', {
      x1: xa, y1: T + alt + 3, x2: xa, y2: T + alt + 11,
      stroke: 'var(--ink)', 'stroke-width': 2
    }));
    svg.appendChild(e('line', {
      x1: xb, y1: T + alt + 3, x2: xb, y2: T + alt + 11,
      stroke: 'var(--ink)', 'stroke-width': 2
    }));
    svg.appendChild(e('text', {
      x: (xa + xb) / 2, y: T + alt + 25, 'text-anchor': 'middle',
      fill: 'var(--muto)', 'font-size': 10.5
    }, 'instabilità di pressione: nessuna delle due carte la vede'));
  }
}

// ---------------------------------------------------------------- disegno
function disegna() {
  if (!base || !serie) return;
  const b = base.valves[String(valvola)][GRANDEZZA];
  const righe = serie.righe;
  const x = righe.map(r => r[GRANDEZZA]);
  const nTot = x.length;

  // ---- carta in uso
  const o1 = {
    chiave: 'uno', titolo: 'Ciclo singolo', media: b.mean, sigma: b.sigma_full,
    punti: x, sfasa: 0, nTot, righe, assex: false
  };
  const r1 = carta(el('g-uno'), o1);
  stato.uno = { opz: o1, res: r1 };

  el('tit-uno').innerHTML = r1
    ? `base della valvola ${valvola} su ${num(base.valves[String(valvola)].n)} cicli` +
      `<span class="sep">·</span>banda ±3σ = ±${num(r1.banda, 1)} ms` +
      `<span class="sep">·</span><b>${num(r1.fuori)}</b> cicli su ${num(r1.tot)} fuori banda` +
      `<span class="sep">·</span>picco ${dec(r1.zmax)} σ`
    : '';

  // ---- carta proposta
  const tit2 = el('tit-due');
  const s46 = b.sigma_media_46;
  if (s46 === null || s46 === undefined) {
    svuota(el('g-due'));
    misura(el('g-due'));
    const svg = el('g-due');
    svg.appendChild(e('text', {
      x: 12, y: 22, fill: 'var(--muto)', 'font-size': 12
    }, `carta non disegnata: ${b.sigma_media_46_reason || 'σ della media di 46 non misurata'}`));
    tit2.innerHTML = `<span class="sep">·</span>non calcolabile`;
    stato.due = null;
  } else {
    const ma = mediaMobile(x, N_FIN);
    const o2 = {
      chiave: 'due', titolo: 'Media mobile di 46 cicli', media: b.mean, sigma: s46,
      punti: ma, sfasa: N_FIN - 1, nTot, righe, assex: true
    };
    const r2 = carta(el('g-due'), o2);
    stato.due = { opz: o2, res: r2 };
    tit2.innerHTML = r2
      ? `stesso riferimento<span class="sep">·</span>` +
        `banda ±3σ₄₆ = ±${num(r2.banda, 1)} ms, σ₄₆ misurata su ` +
        `${num(b.sigma_media_46_n_blocchi)} blocchi` +
        `<span class="sep">·</span><b>${num(r2.fuori)}</b> punti su ${num(r2.tot)} fuori banda` +
        `<span class="sep">·</span>picco ${dec(r2.zmax)} σ` +
        `<span class="sep">·</span>primi ${N_FIN - 1} cicli: finestra non piena`
      : '';
  }

  quote[valvola] = quoteDa(righe, valvola);

  const misurate = Object.keys(quote).length;
  el('tit-str').innerHTML =
    `<span class="sep">·</span>${num(nTot)} cicli dalla route ` +
    `<b>valves/${valvola}/kpi</b>` +
    (serie.motivo ? `<span class="sep">·</span>${serie.motivo}` : '') +
    `<span class="sep">·</span><span class="leg">` +
    `<i class="l1"></i>quota fuori banda del ciclo singolo` +
    `<i class="l2"></i>della media di 46` +
    `<i class="l0"></i>non ancora misurata</span>` +
    `<span class="sep">·</span>verdetto su ${num(LIMITE)} cicli, ` +
    `<span id="str-n">${num(misurate)}</span> valvole su 35`;

  striscia();
}

// ---------------------------------------------------------------- avvio
async function apri() {
  mira = null;
  el('tip').hidden = true;
  try {
    serie = await caricaSerie(valvola);
  } catch (err) {
    serie = { righe: [], route: `valves/${valvola}/kpi`, motivo: String(err.message) };
  }
  disegna();
}

function tema() {
  const b = el('tema');
  const leggi = () => document.documentElement.getAttribute('data-tema')
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'scuro' : 'chiaro');
  const scrivi = () => { b.textContent = leggi() === 'scuro' ? 'chiaro' : 'scuro'; };
  scrivi();
  b.addEventListener('click', () => {
    const n = leggi() === 'scuro' ? 'chiaro' : 'scuro';
    document.documentElement.setAttribute('data-tema', n);
    try { localStorage.setItem('tema-v7k1', n); } catch (e) {}
    scrivi(); disegna();
  });
}

let rid = null;
addEventListener('resize', () => {
  clearTimeout(rid);
  rid = setTimeout(() => disegna(), 120);
});

// Le altre 34 valvole arrivano dopo, e ogni cella si colora appena e'
// misurata. I grafici non aspettano la striscia.
async function caricaVerdetti() {
  const coda = Object.keys(base.valves).map(Number).sort((a, b) => a - b)
    .filter(id => !quote[id]);
  const lavora = async () => {
    while (coda.length) {
      const id = coda.shift();
      try { quote[id] = quoteDa((await caricaSerie(id)).righe, id); }
      catch (err) { quote[id] = { errore: true }; }
      if (base && serie) striscia();
      const c = el('str-n');
      if (c) c.textContent = num(Object.keys(quote).length);
    }
  };
  await Promise.all(Array.from({ length: 6 }, lavora));
}

(async function () {
  tema();
  await caricaBase();
  await apri();
  caricaVerdetti();
})();
