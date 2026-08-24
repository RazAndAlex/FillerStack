# Simulatore PLC V — Riempitrice rotativa isobarica causale a layer

Progetto: simulatore a layer causali di una riempitrice rotativa isobarica, evoluzione del reverse engineering di un simulatore PLC preesistente (V1/V2 in `Comprensione PLC Sim`). Il simulatore genera un processo, i sensori producono segnali, il PLC produce i KPI, e la ground truth resta nascosta al PLC e al ML. Singolo contesto.

## Macchina e processo

**Rotary Filler / Riempitrice rotativa isobarica**: la macchina target; riempie lattine a pressione pari tra serbatoio e lattina. 35 valvole, 26 attive.
_Avoid_: "filler"/"riempitrice" da soli (ambigui).

**Valvola (filling valve / FV)**: singola unità di riempimento sulla giostra. Identificata `valve0`…`valve34`.
_Avoid_: "ugello", "rubinetto" (sono componenti).

**Giostra (carousel)**: struttura rotante che porta le 35 valvole e le lattine; la sua posizione angolare è la base geometrica di slot, zona utile e zona morta.

**Zona morta**: porzione di giostra sopra la camma dove 9 valvole non riempiono (oltre il 26° slot utile).
_Avoid_: "zona non-attiva".

**Impulso**: unità di volume del flussimetro magnetico = 0,1 ml. `target` in impulsi (2500 → 250 ml).
_Avoid_: "tick", "count" quando si intende il volume.

**Ricetta**: sequenziatore logico che fissa i parametri di processo per formato/prodotto. Riferimento: Maxima 6 g/l @18°C, `Speed_Target=15500`.

**Fasi del ciclo**: Flussaggio → Pressurizzazione → Riempimento → Pausa → Snift.
_Avoid_: "filling" per l'intero ciclo.

**CIP**: Cleaning In Place, lavaggio valvole con falsa lattina.

**Formato**: volume della lattina (150/250/330 ml). In giugno: solo 250 ml (`target=2500`).

## KPI

**Filling Time (FT)**: tempo effettivo di riempimento di un contenitore [ms]. Colonna `fillingtime`.

**Tail Time (TT)**: tempo di uscita del prodotto residuo dopo il comando di chiusura [ms]. Colonna `tailtime`.

**Tail Pulse (TP)**: impulsi contati dopo la chiusura della valvola. Colonna `tailpulse`.

**Pulse Count (PC)**: impulsi totali di riempimento (volume erogato / 0,1 ml). Colonna `pulsecount`.

**Target / K-Target**: impulsi teorici per il formato (2500 per 250 ml). Colonna `target`.

**Delta Pulse**: `target − pulsecount` (negativo = sovra-riempimento). Colonna `deltapulse`.

**Filling Step Out**: slot della giostra in cui si completa il riempimento ≈ `FT / 77`. Pericoloso negli slot 25-26. Colonna `filling_step_out`.

**Filling Ok**: booleano "ciclo rientrato nei parametri". Colonna `fillingok`.

## OEE

**OEE**: Overall Equipment Effectiveness = Availability × Performance × Quality.

**Stato macchina (OMAC)**: phase-state della macchina. Mappa: 1=Running, 2=Stopping, 3=Stopped, 4=Idle, 5=Resetting, 8=Suspended, 9=Suspending, 11=Starting.

**Turno (shift)**: finestra lavorativa; modello tesi 2 turni (960 min run + 480 min idle).

## Simulatore V3 (layer)

**Ground Truth**: ciò che il simulatore conosce ma non espone: scenario, fault e le loro cause, onset e severità. Mai input operativo di PLC o ML; serve solo a label ed evaluation.

**Scenario Engine / Fault Engine**: layer che introduce cause nascoste (fault) con scope, severità e onset, senza comunicarle al PLC.
_Avoid_: "generatore di anomalie" quando si intende la generazione diretta dei KPI anomali.

**Processo fisico simulato**: layer che calcola grandezze fisiche (portata, pressione, apertura valvola, volume) come conseguenze causali dello scenario.

**Sensore virtuale**: layer che trasforma grandezze fisiche in segnali osservabili (impulsi flowmeter, posizione encoder, velocità, presenza).

**PLC virtuale**: layer che osserva solo i segnali e applica logica di controllo: state machine, timer, contatori, interlock, ricetta. Non conosce la ground truth.
_Avoid_: "PLC" senza "virtuale" quando si parla del simulatore.

**Cycle Validation**: stato/logica in cui il PLC cristallizza il record di ciclo (FT/TT/TP/StepOut/flag) e lo chiude definitivamente.
_Avoid_: "validation" per la validazione del progetto (è la convalida dei KPI).

**Telemetria**: output osservabile dei cicli e dei segnali (raw PLC data, eventi, stati).

**Feature Engineering / Analytics**: layer sopra la telemetria: rolling statistics, baseline, trend, warning. Separato dal PLC.
_Avoid_: mettere "diagnostica" dentro il "PLC virtuale".

**MachineStable**: condizione operativa stabile (ricetta fissa, velocità stabilizzata, Running, nessun reset recente). Non implica salute.
_Avoid_: usare "stabile" per "sano".

**MachineHealthy**: giudizio di salute, valutato da analytics/baseline, separato dalla stabilità operativa.

**SimulationClock**: orologio virtuale esplicito del simulatore (timestamp virtuale, passo, modalità real-time/accelerata, seed, pausa). Passo fisso di default 1 ms, scan PLC a cadenza fissa (default 10 ms).

**ValveGroupMap**: configurazione che raggruppa le valvole per controller condiviso (un PLC ogni sei unità di riempimento). Scope naturale dei guasti di gruppo; spiega le anomalie a coppie identiche del V2.

**FillQualityOK**: flag di ciclo: la lattina è entro la tolleranza di volume.
_Avoid_: usarlo per giudicare l'intero ciclo.

**SequenceOK**: flag di ciclo: la sequenza macchina è stata completata correttamente.

**SampleValid**: flag di ciclo: il record è affidabile per analytics/training.

**DiagnosticStatus**: giudizio sul comportamento della valvola (es. NORMAL, SUSPECT), separato da qualità e validità del ciclo. Il caso QualityOK=TRUE + SUSPECT è il segnale del condition monitoring.

**SilenceTimer**: timer di fine coda: nessun impulso per la sua durata ⇒ la coda è finita. Default 150 ms (TT max reale 422 ms + margine), configurabile.

**Late pulse**: impulso che arriva dopo CycleClosed; non modifica FinalPulses, genera LatePulseError/LatePulseCount. La ground truth distingue PHYSICAL_LATE_FLOW / FLOWMETER_GLITCH / DELAYED_DATA.

**Scenario**: definizione dichiarativa (YAML) dei fault iniettati: tipo, severità, onset, scope, valvole. Versionabile e riproducibile con seed fisso.

## Diagnostica / deviazioni accettabili

**Healthy baseline**: riferimento statistico del comportamento sano, verificato; non deve auto-aggiornarsi durante un degrado.

**XmR**: Individuals & Moving Range control chart. Limiti UCL/LCL = x̄ ± 2,66·MR̄ (= ±3σ).

**Soglie fisse**: FT ≤ 2000 ms, TT ≤ 600 ms; alert a +6% sulla media globale del top-10.

**Top-10 baseline**: riferimento = media della σ delle 10 valvole più stabili (non la media globale).
_Avoid_: "soglia globale".

## Simulatore di riferimento (baseline)

**Simulatore di riferimento**: il simulatore PLC preesistente, oggetto di una tesi, da cui provengono i dataset di maggio/giugno. Non è la macchina reale; è la baseline che V2 riproduce statisticamente.
_Avoid_: "dati reali" per i CSV di maggio/giugno (sono output del simulatore di riferimento).

**PLC**: Siemens SIMATIC S7-1500, CPU 1512SP. Programmato in TIA Portal con FBD e ST.

**LCG**: Linear Congruential Generator, RNG pseudo-casuale deterministico del simulatore di riferimento.

**ESP**: pipeline Event-State-Period (elaborazione low-code: eventi → stati → periodi).
