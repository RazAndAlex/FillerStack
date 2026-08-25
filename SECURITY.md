# Segnalare un problema di sicurezza

FillerStack e' un progetto personale. Non e' software di produzione, non gira
su nessun impianto e non ha utenti da proteggere: il README lo dice fin
dall'inizio, nella sezione *Not production-ready*.

Se trovi comunque qualcosa che vale la pena segnalare, per esempio una
credenziale finita nella cronologia o una dipendenza con una falla nota, apri
una **Security advisory** privata dalla scheda *Security* del repository.
Rispondo appena posso, senza garanzie di tempi.

## Cose che non sono una falla

- Le credenziali `plcsim:plcsim` in `edge/docker-compose.yml`. Sono di prova, il
  servizio ascolta solo su `localhost` e non esiste un dispiegamento.
- L'API di `pipeline/api.py` non ha autenticazione. E' di sola lettura e nasce
  per girare sulla macchina di chi la lancia.
- La dashboard non valida l'input. Non riceve input da nessuno che non sia chi
  la sta guardando.

Queste tre cose sono note e dichiarate. Se un giorno il progetto uscisse dal
banco di prova, sarebbero le prime tre da chiudere.
