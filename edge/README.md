# edge/ — Node-RED edge integration layer (M7)

Stack Docker dell'edge integration layer (spec M7 locale, non pubblicata;
roadmap `docs/roadmap-iiot.md` §3): Node-RED sottoscrive l'OPC UA del simulatore e
normalizza i cicli valvola nell'**envelope JSON v1.0** (`edge/schemas/envelope-v1.json`).

## Struttura

```
edge/
├── docker-compose.yml            # servizio nodered (M8: + mosquitto, commento TODO)
├── README.md
├── flows/                        # flow JSON versionati (M7-Phase2: flows/*.json)
│   └── README.md
├── tag-mapping.yaml              # UNICA fonte del mapping (spec §4.4)
├── scripts/build_tag_mapping.py  # yaml → tag-mapping.js (conversione deterministica)
├── tag-mapping.js                # GENERATO, montato nel container (mai editato a mano)
├── schemas/envelope-v1.json      # JSON Schema dell'envelope v1.0
├── out/                          # output del test-sink (out.jsonl)
└── tests/parity_check.py         # collaudo automatico (spec §6)
```

## Mapping: unica fonte

`edge/tag-mapping.yaml` è l'UNICA fonte del mapping (spec §2.4, §4.4): ogni voce
definisce `logical_name`, `node_id`, `datatype`, `unit`, `access`, `sampling_mode`.
Il build script genera `edge/tag-mapping.js` in modo **deterministico** (stesso yaml
⇒ stesso js); il file generato è montato read-only nel container
(`/data/tag-mapping.js`) e caricato una volta all'avvio dal mapping loader dei flow.

**Regola invariante (AC-M7-4): mai NodeId hard-coded nei flow** — nessuna
occorrenza `ns=2;s=` nei file JSON di `flows/`.

### Build (da rigenerare dopo ogni modifica al yaml, PRIMA del primo `up`)

```bash
python edge/scripts/build_tag_mapping.py
```

## Avvio

Prerequisiti: Docker Desktop attivo; server OPC UA avviato:

```bash
python -m plcsim.serve --mode realtime --seed 42   # endpoint opc.tcp://localhost:4840
```

Stack (da `edge/`):

```bash
docker compose up -d
```

- **UI Node-RED**: http://localhost:1880 (porta 1880)
- **Endpoint OPC UA**: `opc.tcp://localhost:4840` (server M6, `plcsim/opcua_server.py`)
- **Uscita operativa**: debug node; **test-sink** (variante di deploy): append su `edge/out/out.jsonl`
- Log: `docker compose logs -f nodered` · stop: `docker compose down`

## Flow non visibile nel canvas?

Node-RED carica **solo** `/data/flows.json` all'avvio: la cartella `flows/`
montata su `/data/flows` (con `main.json` e `test-sink.json`) **non basta** —
il bootstrap dell'immagine ufficiale importa i file di `/data/flows` SOLO se
`/data/flows.json` non esiste (primo avvio). Se il canvas risulta vuoto, il
flow versionato non è mai stato caricato come `flows.json`.

Due opzioni per caricare il flow (es. la variante di collaudo `test-sink.json`):

1. **Import manuale dalla UI**: menu → Import → selezionare
   `edge/flows/test-sink.json` → Deploy (il flow parte subito, nessun restart).
2. **docker cp + restart** (il flow diventa quello di default all'avvio):

   ```bash
   docker cp edge/flows/test-sink.json plcsim-nodered:/data/flows.json
   docker restart plcsim-nodered
   ```

   Verifica: `docker exec plcsim-nodered sh -c "cat /data/flows.json | head -c 300"`
   deve iniziare con l'array di nodi del flow M7 (tab `m7s-tab` per il
test-sink), e `curl http://localhost:1880/flows` deve elencare i nodi
(`m7s-wm` watermark-acquisition, `m7s-builder`, …).

> **Nota M8**: lo step di build del compose copierà i flow in
> `/data/flows.json` automaticamente (niente più cp/import manuale).

Nota ambiente (osservata il 2026-08-12): i flow usano i tipi custom
`OpcUa-Endpoint`/`OpcUa-Client` della palette `node-red-contrib-opcua`, che
l'immagine base NON include — senza palette installata il canvas mostra i nodi
come tipi mancanti e il log riporta `Waiting for missing types` (nessuna
acquisizione). Nel collaudo la palette è stata installata via admin API
(POST /nodes); a M8 verrà pinnata nello step di build del compose
(vedi la checklist manuale del collaudo M7, documento locale).

### Palette OPC UA (installazione nel container, ephemeral)

Se i nodi OPC UA sono grigi nel canvas (tipi mancanti), installa la palette
nel container e riavvia Node-RED:

```bash
docker exec plcsim-nodered npm install --prefix /data node-red-contrib-opcua
&& docker restart plcsim-nodered
```

Nota M8: lo step di build del compose pinnarà la palette (elimina
l'installazione manuale) — azione già documentata nel report di accettazione
M7 §7 (finding #1, documento locale) e nella checklist manuale.

## M8

### Broker MQTT locale (servizio `mosquitto`)

Servizio `mosquitto` attivo nel compose (sbloccato a M8, spec §7.2):
`eclipse-mosquitto:2`, container `plcsim-mosquitto`, porta `1883:1883`
(plaintext, POC dichiarato — hardening M11, roadmap §7). Config versionata in
`edge/mosquitto/config/mosquitto.conf` (listener 1883, allow_anonymous,
persistenza su `/mosquitto/data/`, log su stdout), montata read-only; i
volumi `data/` e `log/` sono locali a `edge/mosquitto/`.

Topic/QoS/retained: contratto ADR 0018 (spec §4) — il flow M8 pubblica su
`plant/filler01/telemetry/valve` (QoS 1, retain false); il consumer
(`pipeline/ingest.py`) si connette a `localhost:1883`.

### Palette OPC UA — build step pinnata (spec §7.3, AZIONE M8 OBBLIGATORIA 1)

**Verifica registry npm — 2026-08-12 (da rifare a ogni bump di versione):**

| Comando | Esito |
|---|---|
| `npm view node-red-contrib-opcua versions --json` | ultima versione: `0.2.354` (nessuna 2.x in lista) |
| `npm view node-red-contrib-opcua@2 version` | **E404** — `No match found for version 2` |
| `GET https://registry.npmjs.org/node-red-contrib-opcua/2` | HTTP **404** |
| `GET https://registry.npmjs.org/node-red-contrib-opcua/0.2.354` | HTTP **200** |

→ Il range 2.x (fix reconnect nativo) NON è disponibile su npmjs in questo
ambiente: pinnata la MIGLIORE versione disponibile **`0.2.354`** nello step di
build (`edge/Dockerfile`: `FROM nodered/node-red:4.0.9` + `RUN npm install
node-red-contrib-opcua@0.2.354 --no-audit --no-fund`); il compose usa
`nodered.build: .`. Niente più installazione via admin API (ephemeral).

**Nota collaudo (AC-M8-6 / riserva M7, AZIONE M8 OBBLIGATORIA 2):** il passo 8
della checklist M7 (reconnect OPC UA dopo kill/restart di `serve`) va
RI-COLLAUDATO con la palette del build step; se il reconnect nativo 2.x non è
disponibile (come da verifica sopra), il passo può restare FAIL VERO da
ambiente, mai mascherato.
