# ADR-0023: i parametri di calibrazione entrano nel pacchetto

Data: 2026-08-24 · Stato: accettato · Tocca un file core (`plcsim/config.py`, invariante 1)

## Contesto

`plcsim/config.py` legge a ogni avvio un file di taratura: trentacinque righe,
una per valvola, con le medie e le deviazioni di FT, TT, TP e PC misurate sui
dataset del simulatore di riferimento. Da quelle medie deriva per problema
inverso le costanti fisiche di ogni valvola (ADR-0004). Senza quel file il
simulatore non parte.

Il file stava in `work/kpi_params_clean.csv` per ragioni storiche: l'ADR-0003 lo
aveva copiato lì insieme a `step_params.csv` quando il V3 e' nato accanto al
progetto vecchio, e `work/` e' la cartella degli output di lavorazione.

Preparando la pubblicazione del repository e' emerso che questo rompe il
progetto pubblicato. `work/` contiene corse di simulazione, catture di collaudo e
copie di sicurezza: e' gigantesca e non ha motivo di essere pubblicata, quindi
finisce in `.gitignore`. Chi clonasse il repository troverebbe un simulatore che
si ferma all'avvio per un file mancante — il difetto classico del progetto che
funziona solo sulla macchina di chi lo ha scritto.

La collocazione era gia' sbagliata prima, e la pubblicazione l'ha soltanto resa
visibile: un dato **di ingresso**, obbligatorio e versionato, stava nella
cartella dei dati **di uscita**, rigenerabili e usa e getta.

## Decisione

Il file diventa **`plcsim/valve_params.csv`**, dentro il pacchetto, accanto al
codice che lo legge. `DEFAULT_PARAMS` non e' piu' ancorato alla radice del
progetto ma alla cartella del modulo:

```python
DEFAULT_PARAMS = Path(__file__).resolve().parent / "valve_params.csv"
```

Il contenuto **non cambia di un byte**: e' lo stesso file, copiato. Cambia solo
dove sta e come lo si raggiunge.

Conseguenza voluta: `work/` puo' essere esclusa dalla pubblicazione senza
rompere niente, e il pacchetto diventa completo — chi importa `plcsim` ha tutto
quello che serve, da qualunque cartella lo lanci.

## Perche' questo tocca un file core

L'invariante 1 della roadmap congela `plc.py`, `validation.py`, `config.py`,
`plant.py` e `run.py`: nessuna modifica senza un ADR che la giustifichi. Questo
e' quell'ADR.

La modifica e' deliberatamente la piu' piccola possibile: una costante di
percorso e due righe di commento. Nessuna logica di calibrazione e' stata
toccata, nessun parametro riletto, nessuna formula cambiata.

**Verifica.** Con il file nella posizione nuova, `load_valve_params()` carica 35
valvole e l'impronta delle costanti derivate e' `10197b433edba12c`. Le due copie
del CSV sono identiche, quindi le costanti non possono differire: la firma serve
come ancora per un confronto futuro, non come prova di un cambiamento.

## Alternative scartate

- **Lasciarlo in `work/` e fare un'eccezione nel `.gitignore`.** Funziona, ma
  lascia in piedi la confusione che ha causato il problema: un ingresso
  obbligatorio in mezzo alle uscite, salvato dalla regola solo perche' qualcuno
  si e' ricordato di scrivere un'eccezione. La prossima cartella ignorata lo
  riporterebbe da capo.
- **Incorporarlo nel codice come tabella Python.** Toglie la dipendenza da un
  file, ma seppellisce trentacinque righe di dati misurati dentro un sorgente,
  dove nessuno le rivede piu' e dove un aggiornamento della taratura diventa una
  modifica al codice.
- **Rigenerarlo all'avvio dai dataset originali.** Impossibile: quei dataset non
  stanno in questo repository e non ci staranno mai.

## Debito collegato, non risolto qui

Resta aperto l'ADR mancante per la modifica del 2026-08-19 a `plcsim/run.py`
(aggiunta di `--start` e `--end`), registrato in `STATE.md`. La bit-identita' e'
stata misurata e regge, ma il documento che l'invariante 1 richiede non esiste.
Questo ADR non lo copre.
