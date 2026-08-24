# ADR-0016: Server OPC UA embedded con asyncua; modalità real-time fuori dal fingerprint di determinismo

Data: 2026-08-12 · Stato: proposto (da ratificare con M6)

## Contesto

La roadmap IIoT (`docs/roadmap-iiot.md`) aggancia la pipeline OPC UA → Node-RED → MQTT al simulatore V3
(contesto di progetto §4-§13). Servono: (a) una libreria OPC UA server Python; (b) una modalità di avanzamento
wall-clock del SimulationClock (oggi solo bulk accelerato); (c) una collocazione del real-time rispetto
all'invariante di determinismo (ADR-0005, fingerprint bit-identico).

## Decisione

1. **Libreria: `asyncua`** (fork mantenuto di python-opcua, asyncio-nativo, supporto security policy per M11).
   Scartato `python-opcua` (FreeOpcUa): manutenzione ferma. Il server gira embedded nel processo simulatore,
   in un thread asyncio separato, con bridge thread-safe (queue comandi in ingresso, snapshot tag in uscita).
2. **Modalità clock aggiuntive in moduli nuovi** (`plcsim/realtime.py`, `plcsim/serve.py`): `realtime`
   (pacing wall-clock), `accelerated F`, `stepped` (avanzamento comandato). `plcsim/run.py` e il core
   congelato non vengono toccati: M6 è solo additivo.
3. **Il fingerprint di determinismo resta definito SOLO sulla modalità bulk** (`plcsim.run`). La modalità
   real-time è fuori fingerprint per definizione (il timing dipende dal wall-clock). I test automatici della
   pipeline usano la modalità `stepped`, che è riproducibile (stesso seed + stessa sequenza di passi ⇒
   stessa evoluzione di stato).

## Conseguenze

- `requirements.txt` acquisisce il pin `asyncua`.
- L'invariante "seed ⇒ output bit-identico" continua a valere per tutto ciò che esisteva prima di M6;
  i report di accettazione M6+ devono dichiarare la modalità clock usata.
- Il namespace OPC UA diventa l'API stabile della macchina (contesto §86): la pipeline a valle non deve
  dipendere dal fatto che dietro ci sia Python (§84-85); un futuro PLC reale sostituisce solo il server.
- Sicurezza OPC UA (certificati, policy) esplicitamente rimandata a M11: M6-M10 girano anonymous su
  localhost, dichiarato POC (§69).
