// Switch di scenario, identico nelle tre versioni. Si inietta da solo in
// un elemento con id "switch-scenario". Volutamente neutro: nessuna scelta
// di layout, solo il controllo.
import { api, scenarioCorrente, cambiaScenario } from './dati.js';

export async function montaSwitch(el) {
  const scenari = await api.scenari();
  const corrente = scenarioCorrente();
  const sel = document.createElement('select');
  sel.setAttribute('aria-label', 'Scenario');
  for (const s of scenari) {
    const o = document.createElement('option');
    o.value = s.slug;
    o.textContent = s.titolo;
    if (s.slug === corrente) o.selected = true;
    sel.appendChild(o);
  }
  sel.addEventListener('change', () => cambiaScenario(sel.value));
  el.appendChild(sel);
  return sel;
}

document.addEventListener('DOMContentLoaded', () => {
  const el = document.getElementById('switch-scenario');
  if (el) montaSwitch(el);
});
