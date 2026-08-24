@echo off
rem ============================================================
rem  Simulatore V3 - Server OPC UA (collaudo M6)
rem  Doppio clic: avvia il simulatore con il server OPC UA.
rem  Lascia questa finestra APERTA per tutto il collaudo.
rem  Per fermare: Ctrl+C, poi chiudi la finestra.
rem ============================================================

rem Passa il terminale alla codifica UTF-8 (titolo e messaggi corretti)
chcp 65001 >nul

rem Va nella cartella del progetto (quella del file .bat)
cd /d "%~dp0"

title Simulatore V3 — Server OPC UA

echo.
echo  ============================================
echo   Simulatore V3 - Server OPC UA (collaudo M6)
echo  ============================================
echo.
echo  Avvio del server... endpoint: opc.tcp://localhost:4840
echo  NON chiudere questa finestra durante il collaudo.
echo.

python -m plcsim.serve --mode realtime --seed 42 --scenario scenarios/m5_healthy.yaml
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE%==0 (
    echo  Server terminato in modo pulito. Puoi chiudere questa finestra.
) else (
    echo  [ATTENZIONE] Il server si e' fermato con codice %EXITCODE%.
    echo  - Se hai premuto Ctrl+C: e' normale, nessun problema.
    echo  - Altrimenti controlla:
    echo      * Python installato:  python --version
    echo      * dipendenze:         pip install -r requirements.txt
    echo      * porta 4840 libera:  nessun altro server OPC UA attivo
)
echo.
pause
