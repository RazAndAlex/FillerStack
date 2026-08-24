# ADR-0022: il primo livello dei topic MQTT diventa `plant/`

Data: 2026-08-24 · Stato: accettato · Modifica: ADR-0018 §Decisione 2

## Contesto

L'ADR-0018 ha congelato il contratto dei topic v1 nella forma
`<prefisso>/<machine_id>/<categoria>`, e nella motivazione dichiarava che il
primo livello era **un marchio commerciale**: l'azienda del settore da cui viene
la macchina che il simulatore riproduce.

Era l'unico punto del progetto in cui un identificativo di un terzo entrava in un
contratto tecnico, e ci entrava per restarci: nei flow Node-RED, nel compose, nel
consumer e in due suite di test. Il repository e' destinato alla pubblicazione, e
un contratto pubblicato che porta il marchio di qualcun altro e' un problema di
pubblicazione, non di stile.

Non si risolve dopo. Il topic e' la superficie che lega il bordo ai servizi IT:
cambiarlo a valle di una prima pubblicazione significherebbe rompere un contratto
che qualcuno potrebbe gia' aver letto.

## Decisione

Il primo livello e' **`plant`**:

| topic | uso | QoS | retained |
|---|---|---|---|
| `plant/filler01/telemetry/valve` | envelope v1.0 wire / v1.1 stored | 1 | false |
| `plant/filler01/state` | stato macchina OMAC | 1 | true |
| `plant/filler01/alarm` | riservato (M10) | 1 | false |
| `plant/filler01/prediction` | riservato (ADR-0020) | 1 | false |

QoS, retained, envelope e semantica **non cambiano**: cambia il primo segmento e
nient'altro. `plant` descrive il livello per quello che e' — l'impianto — e non
sostituisce un marchio con un altro. La gerarchia resta quella su cui
l'hardening previsto appoggia le ACL per-macchina.

## Cosa non e' stato toccato, e perche'

**I dati gia' persistiti.** La colonna `source` di `machine_state_history`
contiene il vecchio prefisso sulle righe scritte prima di oggi: `ingest.py` la
compone come `f"mqtt:{self.topic_state}"`, quindi registra il topic in vigore nel
momento della scrittura. Sono cronologia, e dicono da dove e' arrivata davvero
una transizione. Riscriverle le renderebbe coerenti col presente e false sul
passato.

Nessun consumatore confronta quella stringa con un valore atteso: e'
documentazione di provenienza, non una chiave.

Conseguenza accettata, e da sapere per chi legge quella tabella: nello storico
convivono due prefissi, e la data della riga dice quale.

## Portata

`pipeline/ingest.py` (`DEFAULT_TOPIC`, `DEFAULT_TOPIC_STATE`),
`pipeline/inference.py`, `pipeline/storage.py`,
`pipeline/state_history_backfill.py`, `pipeline/tests/test_oee.py`,
`edge/flows/main.json`, `edge/flows/test-sink.json`,
`edge/docker-compose.yml`, `edge/README.md`,
`edge/tests/mqtt_parity_check.py`, `docs/adr/0018`, `docs/adr/0020`,
`docs/roadmap-iiot.md`.

Verificato dopo la modifica: suite intera **567 passed**, zero skip, con
PostgreSQL `healthy`.

## Alternative scartate

- **Togliere il livello** (`filler01/telemetry/valve`): piu' corto, ma perde la
  gerarchia su cui l'ADR-0018 appoggia le ACL per-macchina.
- **`filler/filler01/…`**: ridondante, ripete la stessa parola due volte.
- **Tenere il prefisso e spiegarlo nel README**: una nota non toglie il marchio
  dal contratto, lo commenta soltanto.
