# V3 — Design del simulatore causale a layer

> Cristallizzazione della sessione di grilling (skill `grill-with-docs` + `domain-modeling`).
> Decisioni radice: ADR 0001-0013. Glossario: `CONTEXT.md`. Riferimento: la proposta di evoluzione IIoT/ML e il repository di comprensione del PLC V2, entrambi materiale locale fuori dal repository (read-only).
> Stato: **in attesa di conferma del committente** — non implementare prima dell'OK.

---

## 1. Visione e scope

Da "simulatore che genera KPI" (V2) a "simulatore che genera un processo": causa nascosta → fisica → sensori → PLC → KPI osservati → telemetria. La ground truth resta nascosta a PLC e ML.

**In scope (layer 1-6)**: scenario/fault engine, processo fisico, sensori virtuali, PLC virtuale, validazione KPI, telemetria.
**Fuori scope (progettati ma non implementati)**: pipeline IIoT real-time (OPC UA/Node-RED/MQTT). Analytics/feature engineering (layer 7) e ML (layer 8) sono IMPLEMENTATI (rispettivamente M5 e Track D — vedi §14 e ADR-0015). Architettura predisposta per agganciare IIoT senza ristrutturazioni.

## 2. Decisioni (ADR)

| # | Decisione | Sintesi |
|---|---|---|
| 0001 | Scope | Nucleo causale 1-6; analytics/ML/IIoT agganciabili |
| 0002 | Stack | Python puro (numpy + polars), event-loop a mano |
| 0003 | Repo | PLC Sim V attivo; Comprensione read-only; solo parametri copiati |
| 0004 | Fedeltà | Barra V2: medie ±1%, σ ±10%, bounds; statistica non seed-esatta |
| 0005 | Clock | Passo fisso 1 ms; scan PLC 10 ms; seed deterministico |
| 0006 | Stati macchina | Minimo (Idle/Starting/Running/Stopping/Stopped); OEE/consumi rinviati |
| 0007 | Giostra | 35 valvole (26 attive), offset angolari, ValveGroupMap |
| 0008 | StepOut | Emergente dalla geometria (emenda 0004: pattern zona pericolosa) |
| 0009 | State machine | Completa 9 stati + SAFE_DEPRESSURIZATION |
| 0010 | Fisica | Serbatoio condiviso debole, coupling calibrato |
| 0011 | Flag | Quartetto; fillingok emergente ≈100% sano |
| 0012 | Telemetria | 3 output separati; ground truth mai mescolata |
| 0013 | Scenari | YAML dichiarativo + stream RNG per componente |

## 3. Architettura e moduli

```
plcsim/
├── clock.py        SimulationClock: passo fisso (default 1 ms), timestamp virtuale,
│                   seed, modalità bulk/real-time (pacing), pausa/resume
├── scenario.py     Carica YAML → fault engine → parametri fisici alterati + ground truth
├── plant.py        Fisica: serbatoio (pressione condivisa lenta), giostra, cinematica,
│                   dinamica valvola (apertura/chiusura), portata, volume integrato
├── sensors.py      Flowmeter (impulsi 0,1 ml), encoder (posizione/slot), velocità, presenza
├── plc.py          State machine per valvola, 4 timer, contatori, interlock,
│                   ricetta, scan ciclico 10 ms
├── validation.py   Quartetto flag, step_out geometrico, record ciclo cristallizzato
├── telemetry.py    Cycle records + event log + ground truth out (3 parquet)
├── config.py       Ricetta, ValveGroupMap, parametri calibrati (da V2)
└── run.py          CLI: --days, --scenario, --seed, --selfcheck
```

Flusso dati per passo (1 ms): `clock.tick()` → `plant.step()` → `sensors.step()` → (ogni 10 ms) `plc.scan()` → `validation` alla chiusura ciclo → `telemetry` all'uscita da VALIDATE_FILL. La ground truth è scritta dal fault engine indipendentemente, mai nel percorso PLC.

## 4. SimulationClock

- Passo fisso configurabile, default **1 ms**; scan PLC ogni **10 ms** (1 scan ogni 10 passi).
- Deterministico: master seed → `SeedSequence` per componente (fisica, sensori, PLC) → stream RNG indipendenti.
- Bulk: clock accelerato (stessa logica); real-time: pacing sul wall clock (fase IIoT).
- Fasi vuote (es. macchina Stopped) ottimizzabili a passo più largo senza cambiare semantica.

## 5. State machine valvola (9 stati)

```
IDLE → FLUSHING → PRESSURIZING → FILLING → TAIL → VALIDATE_FILL → PAUSE → SNIFT → DEAD_ZONE → IDLE
                        │
                        └→ SAFE_DEPRESSURIZATION (errore critico/timeout) → reject → DEAD_ZONE
```

| Stato | Uscita (condizione) |
|---|---|
| IDLE | presenza lattina in slot + macchina Running + zona utile → FLUSHING |
| FLUSHING | timer (0,2-0,3 s) → PRESSURIZING |
| PRESSURIZING | raggiunta pressione di equilibrio (o timer) → FILLING |
| FILLING | D1 — Aggiornato al gate M1 (verbale del gate, documento locale): target=normale; pseudologica IF TargetReached→CLOSE ELSE IF EncoderLimitReached→CLOSE (PositionLimit=TRUE, FillQualityOK=FALSE) ELSE IF SafetyTimeoutReached→SAFE_DEPRESSURIZATION (SequenceOK=FALSE) ELSE continua; FT>2000→FillingOvertime/SUSPECT; close_reason=target\|encoder_limit\|safety_timeout |
| TAIL | SilenceTimer scaduto (default 150 ms senza impulsi) → VALIDATE_FILL |
| VALIDATE_FILL | cristallizza il record ciclo (1 scan) → PAUSE |
| PAUSE | timer 300-500 ms → SNIFT |
| SNIFT | timer 200-250 ms → DEAD_ZONE |
| DEAD_ZONE | uscita dalla zona utile → IDLE |

Il PLC *vede*: impulsi, encoder, timer. Non vede la causa.

## 6. Timer (4 tipi distinti)

- **ElapsedRealTime**: tempo realmente trascorso (fisico).
- **ProcessControlTimer**: timer di controllo, congelabile sotto velocità minima confermata (rallentamento); il target check resta attivo anche congelato: se il target arriva durante il rallentamento → chiudi subito.
- **SafetyTimeout**: limite assoluto, mai congelato (protegge anche in rallentamento).
- **SilenceTimer**: fine coda (default 150 ms; Aggiornato al gate M1 — vedi §5/§6).

## 7. Modello fisico (causale, semplificato)

```
flow(t) = nominal_flow × pressure_factor(t) × valve_open_factor(t)
          × restriction_factor(t) × variability(t)

volume(t) += flow(t) × dt
```

- **Serbatoio condiviso debole** (ADR-0010): `pressure_factor` = fattore lento condiviso (oscillazione, driver della firma a due pile del FT) + pressione locale per-valvola con rumore. Coupling calibrato debole → valvole sane quasi indipendenti (rispetta baseline V2).
- **Dinamica valvola** — Aggiornato al gate M1 D2 (verbale del gate, documento locale): apertura/chiusura = ritardo (150-200 ms) + rampa; chiusura estesa da snap_ms=|N(0, settle_jitter_ms)| σ 33 ms fino a tau_ramp+snap_ms (σ_TT≈σ(snap), TP coerente — impulsi snap contati, ricalibrare tau_close/k_ramp per TT mean 301/TP mean 221, niente rumore additivo su TT); `valve_open_factor` tra 0 e 1.
- **Restriction** (fault): `restriction_factor` ↓ → portata ↓ → target più tardi → FT ↑, StepOut ↑, margine ↓.
- La grandezze interne (pressione, restrizione, portata reale) appartengono alla ground truth, non al PLC.

## 8. Sensori virtuali

- **Flowmeter**: 1 impulso ogni 0,1 ml di volume transitato (accumulo + soglia). Il PLC conta impulsi a ogni scan.
- **Encoder**: posizione giostra in conteggi (risoluzione configurabile, es. 10000/giro); slot derivati dalla geometria; vincolo di camma (limite geometrico a slot 26).
- **Velocità**: nominale 15110 cph ± 150 (da V2), derivata dallo stato macchina.
- **Presenza**: lattina nello slot (gating del ciclo).

## 9. Logica PLC

- Ricetta: `target=2500` impulsi (250 ml), `filling_time_limit=2000` ms (soglia diagnostica FillingOvertime, NON chiusura), SafetyTimeout=fill_time_limit+fill_safety_margin default 2500 ms, limite encoder geometrico ≈2123 ms.
- Chiusura FILLING D1 — Aggiornato al gate M1 (verbale del gate, documento locale): pseudologica IF TargetReached→CLOSE ELSE IF EncoderLimitReached→CLOSE (FillQualityOK=FALSE, PositionLimit=TRUE) ELSE IF SafetyTimeoutReached→SAFE_DEPRESSURIZATION (SequenceOK=FALSE) ELSE continua; IF FT>2000→FillingOvertime=TRUE/DiagnosticStatus=SUSPECT; close_reason target|encoder_limit|safety_timeout; step_out=clip(floor(FT/77),26).
- Coda: `PulseAtClose` al comando di chiusura; `FinalPulses` a fine coda (SilenceTimer); `TailPulses = FinalPulses − PulseAtClose`; `TailTime` misurato dal comando di chiusura a fine coda.
- **Late pulse**: dopo `CycleClosed` gli impulsi non modificano `FinalPulses`; generano `LatePulseError`/`LatePulseCount`. La ground truth distingue PHYSICAL_LATE_FLOW / FLOWMETER_GLITCH / DELAYED_DATA.
- Interlock: macchina non Running → nessuna valvola in FILLING; errori critici → SAFE_DEPRESSURIZATION.
- MachineStable ≠ MachineHealthy: la stabilità operativa è del PLC; la salute è di analytics (futuro layer 7).

## 10. Validazione del ciclo (VALDATE_FILL)

Record cristallizzato (mai modificato retroattivamente):
`CycleID, ValveID, FillingTime, TailTime, TailPulses, PulseCount, FinalPulses, DeltaPulse, FillingStepOut, FillQualityOK, SequenceOK, SampleValid, DiagnosticStatus, LatePulseCount, fault flags`

- **Quartetto** (ADR-0011): QualityOK = volume entro tolleranza (±1 g ≈ ±10 impulsi); SequenceOK = sequenza completata; SampleValid = record affidabile; DiagnosticStatus = NORMAL/SUSPECT (deviazione dalla baseline).
- **fillingok** (colonna compatibilità V2): emerge dalla logica → ≈100% nel V3 sano; divergenza dall'artefatto 28,5% documentata.
- **FillingStepOut**: slot al momento della chiusura (geometria, ADR-0008). La zona pericolosa 25-26 emerge dal limite di camma.

## 11. Telemetria (3 output, parquet in `work/`)

1. **Cycle records** (`valve_cycles.parquet`): schema V2 (`machine_code, ts_beg, fillingtime, tailtime, tailpulse, pulsecount, target, deltapulse, filling_step_out, fillingok`) + `sequence_ok, sample_valid, diagnostic_status, late_pulse_count, cycle_id, scenario_id`.
2. **Event log** (`events.parquet`): transizioni di stato (valvola/macchina), comandi, impulsi aggregati per scan — per tracciabilità 'dove è causato' e analytics futuri.
3. **Ground truth** (`ground_truth.parquet` + `fault_timeline.parquet`): per ciclo `fault_type, severity, valve_id, onset_cycle`; timeline con onset/end reali. Mai mescolata alla telemetria.

## 12. Fault engine (layer 1) — catalogo a incrementi

**Milestone 2 (3 guasti meccanici per-valvola, tutti sulla stessa via d'iniezione — pochi, testabili, causa isolabile):**

1. **Restriction**: `restriction_factor ↓` → portata ↓ → FT ↑, StepOut ↑, margine ↓, poi overfill del turno.
2. **Closing delay**: ritardo attuatore chiusura ↑ → flusso continua → TT ↑, TP ↑, FinalPulses ↑, overfill possibile; FillQualityOK resta a lungo TRUE (degrado pre-scarto: il caso SUSPECT).
3. **Opening delay**: ritardo attuatore apertura ↑ → parte del tempo di apertura ricade nel FT (FT ↑), volume iniziale perso.

**Milestone 3**: **Instabilità pressione** (scope gruppo/globale) — variabilità del fattore serbatoio ↑ → σ_FT ↑ su più valvole; sfrutta la risorsa condivisa (ADR-0010).

**Milestone 4**: **Guasti flowmeter** (dropout/glitch impulsi) → PulseCount anomalo, mismatch col volume fisico; ground truth distingue guasto di processo da guasto di sensore.

Ogni fault: `cause parameter → physical consequence → sensor consequence → PLC consequence → KPI consequence`, con scenario YAML e ground truth. Scope local/group/global via ValveGroupMap.

## 13. Scenario YAML (esempio)

```yaml
scenario_id: 42
name: restriction valve12 graduale
seed: 42
faults:
  - fault_type: restriction
    scope: local
    valve_id: 12
    severity: 0.35            # 0-1, entra nel restriction_factor
    onset:
      mode: gradual           # gradual (rampa su N cicli) | abrupt
      start_cycle: 18000
      ramp_cycles: 2000
```

## 14. Milestone e criteri di accettazione

| M | Contenuto | Criterio di uscita |
|---|---|---|
| M0 | Scaffolding: package, clock, config, CLI, test harness | `--selfcheck` verde |
| M1 | Scheletro sano: giostra, fisica, sensori, PLC 9 stati, validazione, telemetria | **Validazione baseline**: run 5 giorni, medie per-valvola ±1%, σ ±10%, bounds rispettati, pattern zona pericolosa (valvole normali mai a 26; valve8/20 ~70% a 26 — con le anomalie V2 riprodotte come profili di calibrazione), fillingok ≈100% documentato, correlazione incrociata bassa |
| M2 | Fault engine: restriction/closing_delay/opening_delay + GT + YAML | Catena verificata event log+GT, KPI direzione attesa, detection possibile dai soli segnali |
| M3 | Instabilità pressione scope gruppo/globale | σ_FT ↑ su più valvole stesso gruppo, valvole fuori gruppo sane |
| M4 | Guasti flowmeter | PulseCount anomalo, GT distingue processo vs sensore |
| M5 | Analytics/feature engineering (layer 7) | rolling stats, baseline congelata, top-10, XmR (x̄ ± 2,66·MR̄), alert doppia semantica (+6% aggregato e per-valvola), detector reale (port D5), FP rate misurabile | falsi positivi misurabili su run healthy (seed ≠ baseline); bit-identità preservata; coda sana FT>2000 = soglia diagnostica (flag per-ciclo), alert FT/TT rate-based |
| ML | Layer ML: dataset/train/eval su telemetria, classificazione fault per finestra | Precision/recall per classe su test (seed separati), ≥ baseline 3σ·√2/√n cross-seed, anti-leakage e determinismo con assert (ADR-0015) |

Il test di accettazione è un test automatico (pytest) che replica la selfcheck V2 (`sim/simulator.py --selfcheck`) sui nuovi output.

## 15. Determinismo e testabilità

- Master seed (default fisso) → `SeedSequence` per componente: **stesso seed + scenario diverso ⇒ differenza attribuibile solo al fault**.
- Event log referenzia `scenario_id`/`cycle_id`: ogni anomalia riconducibile alla causa.
- Nessuna dipendenza dal wall clock in bulk; real-time solo come pacing.

## 16. Punti di calibrazione aperti (fatti da verificare in M1, non decisioni)

- **dt di rotazione**: V2 usa 3,2 s/rotazione ma il dato reale per-valvola suggerisce cadenze diverse (REPORT-DATI §6: conteggi per valvola eterogenei, valve0 −22%). In V3 il dt emerge dalla velocità (≈15110 cph) e dagli stati macchina; da riconciliare con la cadenza osservata.
- **Geometria slot**: FT limit 2000 ms ≈ 26 slot × 77 ms; da riconciliare con l'arco di riempimento della giostra (a velocità nominale 3,2 s/giro → 123 ms/slot: la differenza è il punto da calibrare, probabile arco di riempimento ≈ 2 s).
- **Firma a due pile (P=0,68)**: da riprodurre come effetto del driver lento condiviso (ADR-0010) — parametri di ampiezza/periodo da calibrare su FT lo/hi mediani (1805/1972).
- **Profilo valvole anomale** (valve8/20, 28/30, cluster TP 237/241, TT offset): in M1 come profili di calibrazione (stesso meccanismo di restrizione/ritardo applicato per-valvola), prima di diventare fault iniettati in M2.
- **SilenceTimer**: RISOLTO — default 150 ms (gate M1, vedi GATE-DECISIONS.md D2 e CALIBRATION-NOTES).
- **Volume ↔ impulsi**: 2500 impulsi = 250 ml; verifica che volume integrato e PulseCount coincidano entro la tolleranza.

## 17. Fuori scope (esplicito)

- OEE timeline e consumption (riuso del generatore V2 quando servirà — ADR-0006).
- ML (layer 8): IMPLEMENTATO (track D) — dataset seed-separati, feature signals-only, classificazione fault per finestra, baseline detector 3σ·√2/√n cross-seed; vedi §14 e ADR-0015.
- Pipeline IIoT real-time (OPC UA, Node-RED, MQTT).
- Stato CIP (rinviato, ADR-0009).
- Riproduzione dell'artefatto fillingok 28,5% (ADR-0011).
