# edge Node-RED — build step palette (M8, spec §7.3 — AZIONE M8 OBBLIGATORIA 1).
# La palette node-red-contrib-opcua è PINNATA nel layer dell'immagine (mai via
# admin API: ephemeral, persa alla ricreazione del container).
# Versione pinnata 0.2.354 = MIGLIORE disponibile: al 2026-08-12 il registry npm
# espone solo la serie 0.2.x; il range 2.x risponde 404 (verifica documentata in
# edge/README.md §M8). Conteggio: --no-audit --no-fund (build POC, niente rete esterna).
FROM nodered/node-red:4.0.9

RUN npm install node-red-contrib-opcua@0.2.354 --no-audit --no-fund
