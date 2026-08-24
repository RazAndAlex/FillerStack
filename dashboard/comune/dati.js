// Accesso ai dati delle cinque pagine.
// Unica sorgente ammessa: il server locale, che rispecchia le route di
// pipeline/api.py — dai dati vivi con `server_api.py`, dalla fotografia
// registrata con `server_demo.py`. Nessun calcolo qui dentro che il backend
// non farebbe, nessun valore inventato.

export const SCENARIO_DEFAULT = 'registrato';

export function scenarioCorrente() {
  const p = new URLSearchParams(location.search).get('scn');
  return p || SCENARIO_DEFAULT;
}

export function cambiaScenario(slug) {
  const u = new URL(location.href);
  u.searchParams.set('scn', slug);
  location.href = u.toString();
}

// La navigazione conserva lo scenario: passare da una pagina all'altra non
// deve riportare l'utente sullo scenario predefinito.
//
// Il parametro si aggiunge SOLO se lo scenario non e' quello predefinito.
// Contro il proxy sui dati veri (`server_api.py`) lo scenario e' ignorato di
// proposito e resta sempre il predefinito: aggiungerlo comunque sporcava la
// barra dell'indirizzo con `?scn=a-sana` a ogni cambio pagina, senza che
// significasse niente. Contro il guscio a fixture, dove gli scenari esistono
// davvero, uno scenario scelto viene ancora portato di pagina in pagina.
export function collegaNav() {
  const corrente = scenarioCorrente();
  const scn = encodeURIComponent(corrente);
  for (const a of document.querySelectorAll('.nav-voci a[href^="/"]')) {
    const base = a.getAttribute('href');
    a.href = corrente === SCENARIO_DEFAULT ? base : `${base}?scn=${scn}`;
  }
}

async function get(percorso) {
  const r = await fetch(`/api/${scenarioCorrente()}/${percorso}`,
                        { cache: 'no-store' });
  if (!r.ok) throw new Error(`${percorso} -> HTTP ${r.status}`);
  return r.json();
}

export const api = {
  scenari:     () => fetch('/scenari').then(r => r.json()),
  manifest:    () => get('manifest'),
  stato:       () => get('machine/state'),
  oee:         (finestra = 'day') => get(`machine/oee?window=${finestra}`),
  oeeSerie:    () => get('machine/oee/series'),
  valvole:     () => get('valves'),
  valvola:     (id) => get(`valves/${id}`),
  valvolaKpi:  (id) => get(`valves/${id}/kpi`),
  allarmi:     () => get('alerts'),
  storico:     () => get('alerts/history'),
  pareto:      () => get('alerts/pareto'),
};

// --- fatti del dominio che ogni versione deve trattare allo stesso modo ----

// Eta' dell'ultimo dato rispetto all'istante di osservazione dello scenario.
// In e-macchina-ferma vale 6 h 18 min: e' un elemento di prima classe, non
// una nota a pie' di pagina. Ritorna { secondi, testo } oppure null.
export function etaDato(valvole, at) {
  const ts = Object.values(valvole.valves || {})
    .map(v => v.last_prediction && v.last_prediction.prediction_ts)
    .filter(Boolean)
    .sort();
  if (!ts.length || !at) return null;
  const secondi = (new Date(at) - new Date(ts[ts.length - 1])) / 1000;
  if (!(secondi >= 0)) return null;
  const h = Math.floor(secondi / 3600), m = Math.round((secondi % 3600) / 60);
  return { secondi, testo: h ? `${h} h ${m} min` : `${m} min` };
}

// Formattazione. Il separatore decimale e' la virgola: la lingua e' italiana.
export const pct = (v) => v === null || v === undefined
  ? '—' : (v * 100).toLocaleString('it-IT', { maximumFractionDigits: 1 }) + '%';
export const num = (v, d = 0) => v === null || v === undefined
  ? '—' : v.toLocaleString('it-IT', { maximumFractionDigits: d });

// --- I nomi dei guasti, in italiano e in un posto solo -------------------
//
// Le pagine stampavano `alert.fault_type` tale e quale, e a schermo si leggeva
// «score_aggregation» su tutte e nove le valvole in allarme. Non e' un difetto
// del motore: `fault_type` vale sempre `score_aggregation` per decisione presa
// il 2026-08-21, perche' l'apertura dell'allarme non guarda l'etichetta
// predetta. Il nome del guasto va chiesto alla predizione della valvola, che
// `/valves` porta gia' per tutte e trentacinque — nessuna chiamata in piu'.
export const NOME_GUASTO = {
  restriction:          'restringimento',
  opening_delay:        'ritardo in apertura',
  closing_delay:        'ritardo in chiusura',
  flowmeter_glitch:     'disturbo del flussometro',
  flowmeter_dropout:    'caduta del flussometro',
  pressure_instability: 'pressione instabile',
};

// Cosa scrivere sulla riga di un allarme aperto, data la voce di `/valves`.
//
// Il caso che conta e' `healthy`: una valvola in allarme che il modello dice
// sana. Succede oggi sulla valvola 21, e la riga lo DICHIARA invece di
// coprirlo. La contraddizione e' informativa — avverte che qui la diagnosi
// automatica non regge, e che l'allarme si regge sul solo punteggio. Vedi
// `.project/OPEN_QUESTIONS.md`, M11.
export function nomeGuasto(valvola) {
  const p = valvola && valvola.last_prediction;
  const lab = p && p.predicted_label;
  if (!lab) return 'guasto non diagnosticato';
  if (lab === 'healthy') return 'il modello la dice sana';
  return NOME_GUASTO[lab] || lab;
}
