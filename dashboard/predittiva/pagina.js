// PREDITTIVA — forma «strisce» (approvata al passo 6, guscio wb): una
// striscia sottile di colore per valvola lungo i 60 giorni della run. La
// deriva si legge come cambiamento progressivo di colore dentro la striscia;
// i momenti misurati sono tacche piantate sopra; il margine e' un numero
// dove esiste, un vuoto dichiarato dove non esiste.
//
// Differenza dal guscio: li' i dati erano fixture; qui OGNI numero arriva
// dalle route del proxy (specchio di pipeline/api.py), mai dal database,
// mai dalla verita' di scenario:
//   - serie orarie:      GET /valves/progression/series?valve_id=N
//                        (una chiamata per valvola: 3-9 s misurati; senza
//                        valve_id la sigma costa ~3 minuti);
//   - finestra sana:     GET /valves/baseline (echo del KV, window.source);
//   - allarmi:           GET /alerts?status=all (apertura = opened_ts);
//   - nomi dei guasti:   GET /valves -> last_prediction.predicted_label
//                        (LESSICO §6bis: fault_type vale sempre
//                        score_aggregation, il nome sta nella predizione).
//
// Le valvole in vista: quelle con un allarme ATTIVO (open/sustained) nel
// run corrente — l'insieme «guaste» che l'operativita' dichiara da sola —
// piu' le prime tre senza NESSUNA riga in /alerts (campione sano, stesso
// criterio delle fixture del passo 6).
//
// I momenti misurati, tutti derivati dai dati dell'API (definizioni del
// passo 3/4, dichiarate nel pannello):
//   - segnale:  inizio dell'ultimo regime del canale oltre μ+3σ della
//               finestra sana (mai piu' rientro, durata >= 24 h);
//   - allarme:  opened_ts della riga /alerts della valvola;
//   - degrado:  inizio dell'ultimo regime di quality_rate sotto μ−3σ della
//               finestra sana (mai piu' rientro, durata >= 24 h);
//   - margine:  ore fra segnale e degrado, solo dove entrambi esistono.
// Secchielli con total < 100 scartati ovunque (passo 4 §1).
//
// Gradini di tinta (dichiarati in testata): la sigma della serie oraria e'
// cosi' stretta (~0,86 ms su v8) che uno scarto di +214 ms vale ~250σ — una
// scala lineare in σ saturerebbe in due giorni. I gradini sono spaziati per
// ~×3,3: 3 · 10 · 30 · 100 σ sopra la base. Sotto la tacca o dentro banda:
// neutro, sempre (LESSICO §1).

import { scenarioCorrente, collegaNav, NOME_GUASTO, nomeGuasto } from '/comune/dati.js';

/* ---- la navigazione conserva lo scenario ---- */
collegaNav();

const TEMA_KEY = 'tema-v7pred';
const GRADINI = [3, 10, 30, 100];
const MIN_TOT = 100;          // secchielli sottili fuori dai conti (passo 4 §1)
const REGIME_MIN_H = 24;      // un regime sotto/sopra soglia dura almeno 24 h
const PARALLELO = 4;          // chiamate progression in volo insieme

// Il canale che gradua il degrado, per guasto PREDETTO (passo 4):
// restriction, opening_delay e flowmeter_dropout gradano sulla media oraria
// del riempimento; closing_delay sulla coda; pressure_instability sulla
// dispersione oraria del riempimento (le medie restano in banda). Una
// valvola in allarme che il modello dice sana (oggi la 21) cade sul canale
// sano: riempimento — che e' anche il canale dove il suo scalino si vede.
const CANALE_GUASTO = {
  restriction: 'mean_filling_time_ms',
  opening_delay: 'mean_filling_time_ms',
  flowmeter_dropout: 'mean_filling_time_ms',
  flowmeter_glitch: 'mean_filling_time_ms',
  closing_delay: 'mean_tail_time_ms',
  pressure_instability: 'sigma_filling_time_ms',
};
const CANALE_SANA = 'mean_filling_time_ms';
const NOME_CAN = {
  mean_filling_time_ms: 'riempimento',
  mean_tail_time_ms: 'coda',
  mean_tail_pulse: 'impulso di coda',
  sigma_filling_time_ms: 'dispersione',
  quality_rate: 'qualità',
};

const $ = (s) => document.querySelector(s);
const SVGNS = 'http://www.w3.org/2000/svg';

const fmtI = (x, d = 1) =>
  x.toLocaleString('it-IT', { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtMs = (x) =>
  x.toLocaleString('it-IT', { maximumFractionDigits: 0 });
const p2 = (n) => String(n).padStart(2, '0');
const ddmm = (ts) => {
  const d = new Date(ts);
  return p2(d.getUTCDate()) + '-' + p2(d.getUTCMonth() + 1);
};
const ddmmhhmm = (ts) => {
  const d = new Date(ts);
  return ddmm(ts) + ' ' + p2(d.getUTCHours()) + ':' + p2(d.getUTCMinutes());
};
const mediana = (a) => {
  const s = [...a].sort((x, y) => x - y);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
// mu/sigma di popolazione (ddof=0), stessa semantica del passo 4 e delle
// fixture (statistics.pstdev).
function muSigma(valori) {
  const n = valori.length;
  if (!n) return { n: 0, mu: null, sigma: null };
  const mu = valori.reduce((s, x) => s + x, 0) / n;
  const sigma = n > 1
    ? Math.sqrt(valori.reduce((s, x) => s + (x - mu) * (x - mu), 0) / n)
    : 0;
  return { n, mu, sigma };
}

function svgEl(nome, attrs) {
  const e = document.createElementNS(SVGNS, nome);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}

async function get(rotta) {
  const r = await fetch(`/api/${scenarioCorrente()}/${rotta}`, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${rotta} -> HTTP ${r.status}`);
  return r.json();
}

/* ------------------------------------------------------------------ */
/*  caricamento                                                        */
/* ------------------------------------------------------------------ */

const base = await get('valves/baseline');
const bw = base.window; // {start, end, source, run_id} — la finestra sana dichiarata
const allarmi = (await get('alerts?status=all')).alerts;
const valvole = (await get('valves')).valves;

const ATTIVI = new Set(['open', 'sustained']);
const apertePer = {};   // valve_id -> primo opened_ts fra le righe del run
const conAllarmeAttivo = new Set();
const conRiga = new Set();
for (const a of allarmi) {
  const v = String(a.valve_id);
  conRiga.add(v);
  if (ATTIVI.has(a.status)) conAllarmeAttivo.add(v);
  if (a.opened_ts && (!apertePer[v] || a.opened_ts < apertePer[v]))
    apertePer[v] = a.opened_ts;
}
const guaste = [...conAllarmeAttivo].map(Number).sort((a, b) => a - b);
const sane = [];
for (let v = 1; v <= 35 && sane.length < 3; v++)
  if (!conRiga.has(String(v))) sane.push(v);
const ids = [...guaste, ...sane].sort((a, b) => a - b);

// asse dei giorni: dalla mezzanotte UTC del primo giorno della serie fino
// all'ultimo secchiello. Gli estremi arrivano dalla prima serie caricata.
let t0 = null, nGiorni = 0, serieDa = null, serieA = null;
const GIORNO = 86400000;

/* ------------------------------------------------------------------ */
/*  testata                                                            */
/* ------------------------------------------------------------------ */

$('#leg-rampa').innerHTML =
  '<span class="quattro"><i class="s1"></i><i class="s2"></i><i class="s3"></i><i class="s4"></i></span>' +
  '<span>σ ' + GRADINI.join('·') + '</span>';

$('#prov').textContent =
  'base valvola · ' + ddmm(bw.start) + '→' + ddmm(bw.end) + ' · run ' + (bw.run_id || base.run_id);
$('#h-pop').textContent = ids.length + ' valvole';

/* ------------------------------------------------------------------ */
/*  suggerimento                                                       */
/* ------------------------------------------------------------------ */

const tip = $('#tip');
function mostraTip(html, x, y) {
  tip.innerHTML = html;
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let tx = x + 14, ty = y + 14;
  if (tx + r.width > innerWidth - 8) tx = x - r.width - 14;
  if (ty + r.height > innerHeight - 8) ty = y - r.height - 14;
  tip.style.left = tx + 'px';
  tip.style.top = ty + 'px';
}
const nascondiTip = () => { tip.hidden = true; };

/* ------------------------------------------------------------------ */
/*  momenti misurati: segnale e degrado dai dati di serie              */
/* ------------------------------------------------------------------ */

// Inizio dell'ultimo regime oltre soglia: l'ultimo secchiello misurato che
// NON supera la soglia chiude ciò che è rientrato; il regime è ciò che sta
// dopo, fino a fine run, e deve durare almeno REGIME_MIN_H. Mai un ritorno
// dentro banda dopo l'uscita: un'escursione che rientra non è un segnale di
// regime (passo 4: «progressione = uscita definitiva ≥ 24 h»).
function inizioUltimoRegime(serie, ch, sopra, mu, sigma) {
  if (mu == null || !sigma) return null;
  const soglia = sopra ? mu + 3 * sigma : mu - 3 * sigma;
  const pts = serie.filter((p) => p.total >= MIN_TOT && p[ch] != null);
  if (!pts.length) return null;
  const oltre = (v) => (sopra ? v > soglia : v < soglia);
  let ultimoDentro = -1;
  pts.forEach((p, i) => { if (!oltre(p[ch])) ultimoDentro = i; });
  const capo = pts[ultimoDentro + 1];
  if (!capo || !oltre(capo[ch])) return null;
  const spanH = (Date.parse(pts[pts.length - 1].at) - Date.parse(capo.at)) / 3600000;
  return spanH >= REGIME_MIN_H ? capo.at : null;
}

// mu/sigma per canale sulla finestra sana dichiarata, secchielli total>=100.
function baselineDaSerie(serie) {
  const valori = {
    mean_filling_time_ms: [], mean_tail_time_ms: [], mean_tail_pulse: [],
    sigma_filling_time_ms: [], quality_rate: [],
  };
  for (const p of serie) {
    if (!(bw.start <= p.at && p.at < bw.end)) continue;
    if (p.total < MIN_TOT) continue;
    for (const c in valori) if (p[c] != null) valori[c].push(p[c]);
  }
  const canali = {};
  for (const c in valori) canali[c] = muSigma(valori[c]);
  return canali;
}

/* ------------------------------------------------------------------ */
/*  le strisce                                                         */
/* ------------------------------------------------------------------ */

const classeTinta = (z) =>
  z <= GRADINI[0] ? 's0' : z <= GRADINI[1] ? 's1' : z <= GRADINI[2] ? 's2' : z <= GRADINI[3] ? 's3' : 's4';

const posGiorno = (ts) => (Date.parse(ts) - t0) / GIORNO; // in giorni, frazionario

function tacche(svg, m) {
  const H = 10;
  const pianta = (ts, cls, y, h, w) => {
    if (!ts) return;
    const p = posGiorno(ts);
    if (p < 0 || p > nGiorni) return;
    svg.appendChild(svgEl('rect', { class: cls, x: p - w / 2, y, width: w, height: h }));
  };
  pianta(m.segnale, 'tk', H * 0.62, H * 0.38, 0.14);  // segnale: tacca bassa
  pianta(m.allarme, 'tk', H * 0.30, H * 0.70, 0.14);  // allarme: tacca alta
  pianta(m.degrado, 'tk-d', 0, H, 0.20);              // degrado: sbarra piena
}

function tipGiorno(r, di) {
  const g = r.giorni[di];
  const dd = ddmm(t0 + di * GIORNO);
  const nomeCan = NOME_CAN[r.ch];
  if (!g) {
    return '<div class="v">' + dd + '</div><div class="m">' + nomeCan + ' · 0 cicli</div>';
  }
  const devBase = g.v - r.bl.mu;
  const segno = devBase >= 0 ? '+' : '−';
  let riga3;
  if (g.z > GRADINI[0]) {
    const oltre = g.v - (r.bl.mu + GRADINI[0] * r.bl.sigma);
    riga3 = '<div class="f">+' + fmtI(oltre, 1) + ' ms oltre la banda</div>';
  } else {
    riga3 = '<div class="m">' + segno + fmtI(Math.abs(devBase), 1) + ' ms dalla base</div>';
  }
  return (
    '<div class="v">' + fmtMs(g.v) + ' ms</div>' +
    '<div class="m">' + dd + ' · ' + nomeCan + '</div>' + riga3
  );
}

const elenco = $('#elenco');

// Le righe esistono subito, nell'ordine di macchina, con la sola identità;
// la striscia e il margine arrivano quando torna la chiamata della valvola
// (3-9 s l'una, a ondate di PARALLELO). Un riquadro vuoto non dichiarato è
// un difetto: l'attesa si dichiara.
function rigaVuota(id) {
  const riga = document.createElement('div');
  riga.className = 'riga';
  riga.tabIndex = 0;
  riga.setAttribute('role', 'button');
  riga.setAttribute('aria-busy', 'true');

  const lab = document.createElement('div');
  lab.className = 'vlab';
  const vid = document.createElement('span');
  vid.className = 'vid';
  vid.textContent = 'v' + id;
  lab.appendChild(vid);
  const attiva = conAllarmeAttivo.has(String(id));
  if (attiva) {
    const s = document.createElement('span');
    s.className = 'vsub';
    s.textContent = nomeGuasto(valvole[String(id)]);
    s.dataset.ruolo = 'guasto';
    lab.appendChild(s);
  }
  const c = document.createElement('span');
  c.className = 'vch';
  c.dataset.ruolo = 'canale';
  lab.appendChild(c);
  riga.appendChild(lab);

  const wrap = document.createElement('div');
  wrap.className = 'strip-w';
  const att = document.createElement('span');
  att.className = 'attesa';
  att.textContent = '…';
  att.setAttribute('aria-label', 'dati della valvola in caricamento');
  wrap.appendChild(att);
  riga.appendChild(wrap);

  const mg = document.createElement('div');
  mg.className = 'margine';
  riga.appendChild(mg);
  return riga;
}

function riempiRiga(riga, r) {
  riga.removeAttribute('aria-busy');
  const nomeG = r.attiva ? nomeGuasto(valvole[String(r.id)]) : null;
  riga.setAttribute(
    'aria-label',
    'Valvola ' + r.id + (nomeG ? ', ' + nomeG : ', nessun allarme nel run') +
    ', canale ' + NOME_CAN[r.ch] + ', ' + nGiorni + ' giorni'
  );
  riga.querySelector('[data-ruolo="canale"]').textContent = NOME_CAN[r.ch];

  // striscia
  const wrap = riga.querySelector('.strip-w');
  wrap.textContent = '';
  const svg = svgEl('svg', {
    class: 'strip', viewBox: '0 0 ' + nGiorni + ' 10',
    preserveAspectRatio: 'none', tabindex: '0', role: 'img',
    'aria-label': 'Striscia dei ' + nGiorni + ' giorni, valvola ' + r.id +
      ', riferimento base della valvola, banda 3 sigma',
  });
  svg.appendChild(svgEl('rect', { class: 'hit', x: 0, y: 0, width: nGiorni, height: 10 }));
  r.giorni.forEach((g, di) => {
    svg.appendChild(
      g === null
        ? svgEl('rect', { class: 'morto', x: di, y: 0.4, width: 1, height: 9.2 })
        : svgEl('rect', { class: classeTinta(g.z), x: di, y: 0.4, width: 1, height: 9.2 })
    );
  });
  tacche(svg, r.momenti);
  // `hidden` non si applica ai rect SVG: si usa `visibility`.
  const mirino = svgEl('rect', { class: 'mirino', x: 0, y: 0, width: 0.12, height: 10, visibility: 'hidden' });
  svg.appendChild(mirino);
  wrap.appendChild(svg);

  // interazione sulla striscia: bersaglio sul giorno più vicino (la X)
  let cursore = null;
  const spegni = () => { mirino.setAttribute('visibility', 'hidden'); nascondiTip(); };
  const accendi = (di, cx, cy) => {
    cursore = di;
    mirino.setAttribute('visibility', 'visible');
    mirino.setAttribute('x', di + 0.44);
    mostraTip(tipGiorno(r, di), cx, cy);
  };
  svg.addEventListener('mousemove', (e) => {
    const b = svg.getBoundingClientRect();
    const di = Math.max(0, Math.min(nGiorni - 1, Math.floor(((e.clientX - b.left) / b.width) * nGiorni)));
    accendi(di, e.clientX, e.clientY);
  });
  svg.addEventListener('mouseleave', spegni);
  svg.addEventListener('keydown', (e) => {
    const b = svg.getBoundingClientRect();
    const vai = (di) => {
      di = Math.max(0, Math.min(nGiorni - 1, di));
      accendi(di, b.left + ((di + 0.5) / nGiorni) * b.width, b.top);
    };
    if (e.key === 'ArrowLeft') { vai(cursore === null ? nGiorni - 2 : cursore - 1); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { vai(cursore === null ? nGiorni - 1 : cursore + 1); e.preventDefault(); }
    else if (e.key === 'Home') { vai(0); e.preventDefault(); }
    else if (e.key === 'End') { vai(nGiorni - 1); e.preventDefault(); }
    else if (e.key === 'Escape') spegni();
  });
  svg.addEventListener('focus', () => {
    const b = svg.getBoundingClientRect();
    accendi(nGiorni - 1, b.left + b.width, b.top); // il fuoco entra sull'ultimo giorno
  });
  svg.addEventListener('blur', spegni);

  // margine: numero misurato dove esiste, vuoto dichiarato dove no
  const mg = riga.querySelector('.margine');
  if (r.attiva && r.momenti.degrado && r.momenti.segnale) {
    const ore = (Date.parse(r.momenti.degrado) - Date.parse(r.momenti.segnale)) / 3600000;
    // innesto da wa «mosaico»: il margine e' UN numero grande che domina
    // la riga — il numero e' il conto; l'unita' resta piccola e muta.
    const n = document.createElement('span');
    n.className = 'mnum';
    n.textContent = fmtI(ore, 1);
    const u = document.createElement('small');
    u.textContent = ' h';
    n.appendChild(u);
    mg.appendChild(n);
  } else if (r.attiva) {
    const v = document.createElement('span');
    v.className = 'vuoto';
    v.tabIndex = -1;
    const perche = r.momenti.degrado && !r.momenti.segnale
      ? 'il canale non lascia il regime sano'
      : 'la qualità non lascia il regime sano';
    v.setAttribute('aria-label', 'Margine non vedibile: ' + perche);
    v.addEventListener('mousemove', (e) => {
      mostraTip('<div class="v">non vedibile</div><div class="m">' + perche + '</div>', e.clientX, e.clientY);
      e.stopPropagation();
    });
    v.addEventListener('mouseleave', nascondiTip);
    mg.appendChild(v);
  }

  // dettaglio sopra la pagina
  const apri = () => apriPannello(r, riga);
  riga.addEventListener('click', (e) => {
    if (e.target.closest('.vuoto')) return;
    apri();
  });
  riga.addEventListener('keydown', (e) => {
    if (e.target !== riga) return;
    if (e.key === 'Enter' || e.key === ' ') { apri(); e.preventDefault(); }
  });
}

// una riga che non arriva: dichiarata, non finta (LESSICO §6)
function rigaFallita(riga, id, motivo) {
  riga.removeAttribute('aria-busy');
  const wrap = riga.querySelector('.strip-w');
  wrap.textContent = '';
  const s = document.createElement('span');
  s.className = 'attesa';
  s.textContent = 'non disponibile';
  s.title = motivo;
  wrap.appendChild(s);
  riga.setAttribute('aria-label', 'Valvola ' + id + ', serie non disponibile');
}

/* ------------------------------------------------------------------ */
/*  pannello di dettaglio                                              */
/* ------------------------------------------------------------------ */

const pan = $('#pan'), velo = $('#velo'), panCorpo = $('#pan-corpo');
let ultimoFuoco = null;

function chip(k, v, cls) {
  return '<span class="chip' + (cls ? ' ' + cls : '') + '"><span class="k">' + k + '</span>' + v + '</span>';
}

function graficoCanale(r) {
  const W = 520, H = 170, L = 6, R = 52, T = 12, B = 20;
  const vals = r.giorni.map((g) => (g ? g.v : null));
  const datiV = vals.filter((v) => v !== null);
  const mu = r.bl.mu, sg = r.bl.sigma;
  const dmin = Math.min(...datiV), dmax = Math.max(...datiV);
  let lo = Math.min(mu - 3 * sg, dmin), hi = Math.max(mu + 3 * sg, dmax);
  const hiDati = hi; // tetto dei dati: la tinta arriva solo fino a qui
  const span = hi - lo;
  lo -= span * 0.08; hi += span * 0.08; // margine: la traiettoria non si schiaccia
  const X = (di) => L + ((di + 0.5) / nGiorni) * (W - L - R);
  const Xts = (ts) => L + (posGiorno(ts) / nGiorni) * (W - L - R);
  const Y = (v) => T + ((hi - v) / (hi - lo)) * (H - T - B);

  const svg = svgEl('svg', {
    viewBox: '0 0 ' + W + ' ' + H, class: 'interattivo', role: 'img',
    'aria-label': 'Traiettoria del canale ' + NOME_CAN[r.ch] + ', ' + datiV.length +
      ' giorni, riferimento base della valvola ' + fmtI(mu, 1) + ' ms, banda più o meno 3 sigma',
  });
  // zone di deriva oltre la banda (innesto da wc «banda»): il territorio
  // che la traiettoria attraversa si tinge ai gradini σ gia' dichiarati in
  // testata (3·10·30·100); sopra i dati il territorio resta neutro.
  for (let k = 0; k < GRADINI.length; k++) {
    const zLo = mu + GRADINI[k] * sg;
    if (zLo >= hiDati) continue;
    const zHi = k + 1 < GRADINI.length ? mu + GRADINI[k + 1] * sg : Infinity;
    const alto = Math.min(zHi, hiDati);
    svg.appendChild(svgEl('rect', {
      class: 'z' + (k + 1), x: L, y: Y(alto),
      width: W - L - R, height: Y(zLo) - Y(alto),
    }));
  }
  // banda e tacca prima, traiettoria sopra tutto
  svg.appendChild(svgEl('rect', { class: 'banda', x: L, y: Y(mu + 3 * sg), width: W - L - R, height: Y(mu - 3 * sg) - Y(mu + 3 * sg) }));
  svg.appendChild(svgEl('line', { class: 'tacca', x1: L, x2: W - R, y1: Y(mu), y2: Y(mu) }));
  // momenti misurati
  const pianta = (ts, cls) => {
    if (!ts) return;
    const x = Xts(ts);
    svg.appendChild(svgEl('rect', { class: cls, x: x - 0.7, y: T, width: 1.4, height: H - T - B }));
  };
  pianta(r.momenti.segnale, 'tk');
  pianta(r.momenti.allarme, 'tk');
  pianta(r.momenti.degrado, 'tk-d');
  // traiettoria: ricolorata per la zona che attraversa (innesto da wc),
  // segmenti retti spezzati sui giorni morti, sopra tutto
  const zonaLinea = (v) => {
    const z = (v - mu) / sg;
    return z <= GRADINI[0] ? 0 : z <= GRADINI[1] ? 1 : z <= GRADINI[2] ? 2 : z <= GRADINI[3] ? 3 : 4;
  };
  let run = null, prec = null;
  const chiudiRun = () => {
    if (run && run.n > 0)
      svg.appendChild(svgEl('path', { class: 'linea ln' + run.k, d: run.d }));
    run = null;
  };
  vals.forEach((v, di) => {
    if (v === null) { chiudiRun(); prec = null; return; }
    const k = zonaLinea(v);
    const px = X(di), py = Y(v);
    if (!run || run.k !== k) {
      chiudiRun();
      // la nuova corsa riparte dal punto precedente: continuita' visiva
      run = {
        k,
        d: prec
          ? 'M' + prec[0].toFixed(1) + ' ' + prec[1].toFixed(1)
          : 'M' + px.toFixed(1) + ' ' + py.toFixed(1),
        n: prec ? 1 : 0,
      };
    }
    run.d += 'L' + px.toFixed(1) + ' ' + py.toFixed(1);
    run.n++;
    prec = [px, py];
  });
  chiudiRun();
  // etichette: base a sinistra, +3σ a destra, estremi dei giorni sotto
  const t1 = svgEl('text', { x: L + 2, y: Y(mu) - 4 });
  t1.textContent = 'base ' + fmtI(mu, 1) + ' ms';
  svg.appendChild(t1);
  const t2 = svgEl('text', { x: W - R + 4, y: Y(mu + 3 * sg) + 4 });
  t2.textContent = '+3σ';
  svg.appendChild(t2);
  const t3 = svgEl('text', { x: W - R + 4, y: Y(dmax) + 4 });
  t3.textContent = fmtMs(dmax);
  svg.appendChild(t3);
  const t4 = svgEl('text', { x: L, y: H - 5 });
  t4.textContent = ddmm(serieDa);
  svg.appendChild(t4);
  const t5 = svgEl('text', { x: W - R, y: H - 5, 'text-anchor': 'end' });
  t5.textContent = ddmm(serieA);
  svg.appendChild(t5);
  return svg;
}

function apriPannello(r, origine) {
  ultimoFuoco = origine;
  $('#pan-tit').textContent = 'Valvola ' + r.id;
  const m = r.momenti;

  const sezGuasto = document.createElement('div');
  sezGuasto.className = 'pan-sez';
  if (r.attiva) {
    const ore = (m.degrado && m.segnale)
      ? (Date.parse(m.degrado) - Date.parse(m.segnale)) / 3600000
      : null;
    sezGuasto.innerHTML =
      '<h3>' + nomeGuasto(valvole[String(r.id)]) + '</h3>' +
      (m.segnale ? chip('1° segnale', ddmmhhmm(m.segnale)) : '') +
      (m.allarme ? chip('allarme', ddmmhhmm(m.allarme)) : '') +
      (m.degrado
        ? chip('degrado qualità', ddmmhhmm(m.degrado), 'att') +
          (ore !== null ? chip('segnale → degrado', fmtI(ore, 1) + ' h', 'att') : '')
        : '<p class="pan-nota">non vedibile: la qualità non lascia il regime sano</p>');
  } else {
    sezGuasto.innerHTML = '<h3>nessun allarme nel run</h3><p class="pan-nota">valvola sana di campione</p>';
  }

  const sezGraf = document.createElement('div');
  sezGraf.className = 'pan-sez';
  const h3 = document.createElement('h3');
  h3.textContent = NOME_CAN[r.ch] + ' · ms';
  const pg = document.createElement('div');
  pg.className = 'pan-graf';
  pg.appendChild(graficoCanale(r));
  const nota = document.createElement('p');
  nota.className = 'pan-nota';
  nota.textContent =
    'base della valvola · finestra sana ' + ddmm(bw.start) + ' → ' + ddmm(bw.end) +
    ' · banda μ±3σ della serie oraria (' + r.bl.n + ' secchielli)' +
    ' · mediane giornaliere · segnale = prima uscita definitiva del canale oltre μ+3σ (≥ 24 h)' +
    ' · degrado = ultimo regime qualità sotto μ−3σ (≥ 24 h)';
  sezGraf.appendChild(h3);
  sezGraf.appendChild(pg);
  sezGraf.appendChild(nota);

  panCorpo.innerHTML = '';
  panCorpo.appendChild(sezGuasto);
  panCorpo.appendChild(sezGraf);

  velo.hidden = false;
  pan.hidden = false;
  $('#pan-x').focus();
}

function chiudiPannello() {
  velo.hidden = true;
  pan.hidden = true;
  if (ultimoFuoco) ultimoFuoco.focus();
}
$('#pan-x').addEventListener('click', chiudiPannello);
velo.addEventListener('click', chiudiPannello);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !pan.hidden) chiudiPannello();
});

/* ------------------------------------------------------------------ */
/*  caricamento per valvola: ondate di PARALLELO, la riga si riempie   */
/*  quando la sua chiamata torna                                       */
/* ------------------------------------------------------------------ */

const righeDom = {};
for (const id of ids) {
  const riga = rigaVuota(id);
  righeDom[id] = riga;
  elenco.appendChild(riga);
}

async function caricaValvola(id) {
  const riga = righeDom[id];
  try {
    const payload = await get('valves/progression/series?valve_id=' + id);
    const serie = payload.valves[String(id)];
    if (!serie) throw new Error(payload.reason || 'serie assente');
    if (t0 === null) {
      const d = new Date(payload.from);
      t0 = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
      nGiorni = Math.ceil((Date.parse(payload.to) - t0) / GIORNO);
      serieDa = payload.from;
      serieA = payload.to;
      $('#est-a').textContent = ddmm(payload.from);
      $('#est-b').textContent = ddmm(payload.to);
    }
    const attiva = conAllarmeAttivo.has(String(id));
    const lab = attiva && valvole[String(id)] && valvole[String(id)].last_prediction;
    const ch = (lab && CANALE_GUASTO[valvole[String(id)].last_prediction.predicted_label])
      || CANALE_SANA;
    const canali = baselineDaSerie(serie);
    const bl = canali[ch];
    const perGiorno = Array.from({ length: nGiorni }, () => []);
    for (const b of serie) {
      if (b.total === 0 || b[ch] == null) continue; // ore morte: esistono, non si interpolano
      const di = Math.floor((Date.parse(b.at) - t0) / GIORNO);
      if (di >= 0 && di < nGiorni) perGiorno[di].push(b[ch]);
    }
    const sigmaOk = bl.mu != null && bl.sigma;
    const giorni = perGiorno.map((vals) =>
      vals.length
        ? { v: mediana(vals), z: sigmaOk ? (mediana(vals) - bl.mu) / bl.sigma : 0, n: vals.length }
        : null
    );
    const momenti = {
      segnale: attiva ? inizioUltimoRegime(serie, ch, true, bl.mu, bl.sigma) : null,
      allarme: apertePer[String(id)] || null,
      degrado: attiva
        ? inizioUltimoRegime(serie, 'quality_rate', false,
            canali.quality_rate.mu, canali.quality_rate.sigma)
        : null,
    };
    riempiRiga(riga, { id, attiva, ch, bl, giorni, momenti, payload });
  } catch (e) {
    rigaFallita(riga, id, String(e && e.message || e));
  }
}

for (let i = 0; i < ids.length; i += PARALLELO)
  await Promise.all(ids.slice(i, i + PARALLELO).map(caricaValvola));

/* ------------------------------------------------------------------ */
/*  tema                                                               */
/* ------------------------------------------------------------------ */

(function montaTema() {
  const b = $('#tema');
  const scuro = () => (document.documentElement.getAttribute('data-tema')
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'scuro' : 'chiaro')) === 'scuro';
  // Minuscolo come nelle altre pagine: il pulsante e' lo stesso. Nessun
  // ridisegno al cambio: ogni colore di questa pagina e' una var del tema.
  const dip = () => { b.textContent = scuro() ? 'chiaro' : 'scuro'; };
  dip();
  b.addEventListener('click', () => {
    const n = scuro() ? 'chiaro' : 'scuro';
    document.documentElement.setAttribute('data-tema', n);
    try { localStorage.setItem(TEMA_KEY, n); } catch (e) {}
    dip();
  });
})();
