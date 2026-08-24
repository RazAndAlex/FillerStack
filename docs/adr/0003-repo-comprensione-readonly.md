# Repo: PLC Sim V attivo, Comprensione PLC Sim read-only

`PLC Sim V` è il repo di lavoro del V3 (package del simulatore, scenario files, `work/` per output, `CONTEXT.md` + `docs/adr/`). `Comprensione PLC Sim` resta intoccata come riferimento read-only (dati GB, spec, V2). Copiati nel nuovo repo solo i parametri V2 (`work/kpi_params_clean.csv`, `work/step_params.csv`) necessari a calibrare la baseline sana. Documenti e commenti in italiano, identificatori in inglese (coerente coi progetti esistenti).

## Considered Options
- Copiare tutto (dati inclusi) in PLC Sim V — rifiutato: duplica GB e i due progetti divergerebbero.
- Sviluppare dentro Comprensione — rifiutato: mescola reverse engineering concluso e nuova architettura.

## Consequences
Ogni fatto da riusare dal reverse engineering va copiato esplicitamente (parametri, numeri, criteri), mai letto a runtime dalla cartella Comprensione.
