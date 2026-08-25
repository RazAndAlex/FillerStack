# edge/flows/ — flow Node-RED versionati (M7)

Flow JSON versionati dell'edge integration layer (spec M7 §5):
caricati dall'immagine ufficiale Node-RED all'avvio del container (montati su
`/data/flows` in `edge/docker-compose.yml`). Formato importabile Node-RED v4:
array di nodi con `id`/`type`/`z`/`wires`/`x`/`y` + tab + commenti.

## File

| File | Tab | Ruolo |
|---|---|---|
| `main.json` | `M7 main` | Flow operativo: subscribe OPC UA + mapping loader + watermark §4.3 + builder envelope v1.0; uscita = **debug node** (roadmap §3) |
| `test-sink.json` | `M7 test-sink` | Variante di deploy per il collaudo: stessi nodi watermark+builder, uscita = **append JSON compatto su `/data/out/out.jsonl`** (host: `edge/out/out.jsonl`, una riga per record) |

Gli id dei nodi sono prefissati (`m7-*` nel main, `m7s-*` nel test-sink) per
evitare collisioni quando i due file sono caricati insieme nello stesso runtime.

## Catena del flow (identica nei due file)

```
inject(once, boot) -> mapping-loader -> subscription-setup -> OpcUa-Client
                                                              -> watermark-acquisition
                                                              -> builder-envelope
                                                              -> debug | test-sink
inject(repeat 30 s, watchdog) -> subscription-setup (delete + recreate subscription, T3)
```

### Mapping loader (`mapping-loader`, function node di init)

- Legge `/data/tag-mapping.js` (`fs.readFileSync`), GENERATO da
  `edge/scripts/build_tag_mapping.py` a partire da `edge/tag-mapping.yaml`
  (**unica fonte**, spec §4.4); estrae l'array `module.exports` (JSON tra
  `module.exports = [` e l'ultimo `]`, con rimozione della virgola finale del
  generatore) e lo espone nel flow context (`m7.tagMapping`, `m7.nodeIndex`
  node_id → logical_name).
- **Se il file manca/non è valido**: log errore chiaro e **no-op** (nessuna
  subscription, nessuna acquisizione).
- Log di riepilogo (`n voci`: trigger `machine.*` + blocco `valve01.*`).
- Ogni deploy/riavvio è un **nuovo epoch** (spec §10 Q1): resetta watermark e
  store nel context → niente backfill, niente duplicati.
- NB: la verifica browse dei node_id (spec §5) è delegata a **T0**
  (`edge/tests/parity_check.py`, browse asyncua) — da un function node non è
  possibile un browse sincrono.

### Subscription OPC UA (`OpcUa-Client` + `subscription-setup`)

- Endpoint: `opc.tcp://localhost:4840` (`OpcUa-Endpoint`, security None).
- **Nessun NodeId hard-coded (AC-M7-4)**: la subscription è costruita a runtime
  dal `subscription-setup` che legge il mapping dal context. Nel flow c'è solo
  un PLACEHOLDER documentato (mai inviato al client): se il mapping non è nel
  context, non si sottoscrive nulla (no-op).
- Subscription (group `multiple`, publishing/sampling 100 ms):
  - **trigger**: `machine.data_ready` + `machine.cycle_counter` (logica §4.3:
    trigger = `DataReady || CycleCounter`; CycleCounter è la fonte di verità —
    il pulse DataReady dura 1 scan ~10 ms e può andare perso, §4.1);
  - **blocco**: i 13 tag `valve01.*` (i valori arrivano via subscription e
    restano nello store del context per la lettura blocco).
- Output 0 = dati (`msg.topic` = node_id, `msg.payload` = DataValue con
  `value`, `statusCode`, `sourceTimestamp`, `serverTimestamp`), output 1 =
  stato client, output 2 = raw (non usato).
- **Watchdog (30 s)**: `deletesubscription` + nuova subscription — copre il
  caso T3 (restart del server con subscription non ripristinata). Il watermark
  previene duplicati dopo ogni re-subscribe.

### Watermark (`watermark-acquisition`, function node) — modello §4.3, NIENTE ack server

Watermark **in-memory nel flow context** (`m7.watermark`); a ogni riavvio si
re-inizializza al `CycleCounter` corrente (nessun backfill, nessun duplicato —
spec §10 Q1; la persistenza non aggiunge garanzie in v1):

1. **init**: alla prima notifica `watermark = CycleCounter` corrente (nessun
   backfill dei cicli chiusi durante il down);
2. **check**: `CycleCounter <= watermark` → ignora (nessun duplicato);
   `> watermark` → nuova chiusura ciclo;
3. **epoch reset**: `CycleCounter < watermark` (server riavviato, contatori da
   0) → `watermark = CycleCounter` corrente + log `epoch reset`;
4. **burst** (`delta > 1`): blocchi intermedi non più leggibili (limite
   documentato §4.3) → si pubblica **l'ultimo blocco leggibile**, si aggiorna
   il watermark e si **LOGGA il gap** (metrica §89);
5. **lettura blocco**: 13 tag `valve01.*` via mapping dal context (store);
   `watermark = CycleCounter` letto.

Stessa semantica di `watermark_check`/`WatermarkState` in
`edge/tests/parity_check.py` (riferimento della parità T1/T3-T6).

### Builder envelope (`builder-envelope`, function node) — spec §4.2

Envelope JSON **v1.0** conforme a `edge/schemas/envelope-v1.json`:

- `schema_version` `"1.0"`, `event_type` `"valve_cycle"`, `machine_id`
  `"filler01"` (costanti);
- `event_id`: uuid v4 (`crypto.randomUUID`, fallback v4 timestamp-based);
- `event_ts`: ISO8601 UTC, orologio edge (Node-RED) all'arrivo del dato;
- `source_ts`: **ServerTimestamp del blocco Valve01** (max dei ServerTimestamp
  dei 13 tag ≈ istante dell'ultima scrittura del blocco, spec §10 Q3) con
  **fallback `event_ts`**;
- `cycle_id` = `Valve01.LastCycleId` (fallback: CycleCounter corrente con log,
  lockstep M6 §4.1); `valve_id` derivata dal mapping (gruppo `valveNN`, in M7
  sempre 1);
- `data` = 12 campi del contratto (chiavi corte dello schema: `filling_time_ms`
  … `diagnostic_status`, dai logical_name `valve01.*` del mapping);
- `quality`: happy path `{valid: true, completeness: "complete"}`; tag non
  leggibile (qualità OPC UA cattiva o valore mancante) → campo `null` e
  `{valid: false, completeness: "partial"}` (**policy T6**: il record è emesso
  comunque, nessun ciclo sparisce dalla sequenza).

Nessuna logica di business/ML/analytics nei flow (spec §2 vincolo 3): solo
acquisizione, mappatura, normalizzazione, timestamp e impacchettamento.

## Test-sink (`test-sink.json`)

Stessi nodi watermark+builder del main, MA l'uscita è un function node che fa
**append del JSON compatto** (`JSON.stringify`, una riga per record) su
`/data/out/out.jsonl` (`fs.appendFileSync`, mode `'a'`); il mount
`./out:/data/out` (compose) lo espone come `edge/out/out.jsonl` lato host —
consumato da `edge/tests/parity_check.py` (T1/T3-T6).

## Regola invariante (AC-M7-4)

**MAI NodeId hard-coded nei flow**: nessuna occorrenza del pattern di NodeId
stringa (`ns=` + indice + `;s=` + browse path, es. `ns=…;s=Filler01.…`) nei
file JSON di questa directory (verificabile con un grep sul pattern nel repo,
come da collaudo). I NodeId vivono SOLO in `edge/tag-mapping.yaml` →
`edge/tag-mapping.js` (GENERATO).

## Avvio

Prerequisiti: Docker Desktop attivo; **build del mapping** (se non già fatto,
PRIMA del primo up):

```bash
python edge/scripts/build_tag_mapping.py      # edge/tag-mapping.js (GENERATO, unica fonte)
```

Server OPC UA (M6):

```bash
python -m plcsim.serve --mode realtime --seed 42   # opc.tcp://localhost:4840
```

Stack (da `edge/`):

```bash
docker compose up -d
```

- **UI Node-RED**: http://localhost:1880 — i flow di `/data/flows` sono caricati
  all'avvio del container; dopo un import/manual edit dei JSON, fare **Deploy**
  della tab (l'inject `once` riesegue loader + subscription; il watchdog 30 s
  tiene viva la subscription, T3).
- **Uscita operativa**: debug node `envelope v1.0` (tab `M7 main`).
- **Collaudo**: tab `M7 test-sink` → append su `edge/out/out.jsonl`, poi
  `python -m pytest edge/tests/parity_check.py -q` (o
  `python edge/tests/parity_check.py`).
- Log: `docker compose logs -f nodered` · stop: `docker compose down`.

> **Nota collaudo**: i nodi `OpcUa-*` richiedono il pacchetto
> `node-red-contrib-opcua` dentro il container (installato via package.json di
> Node-RED). Al momento della scrittura dei flow il compose non monta ancora un
> `package.json` — prerequisito da verificare al collaudo (vedi report M7).
