@echo off
rem Apre la dashboard della riempitrice nel browser.
rem Chiudendo questa finestra si spegne tutto: non resta niente acceso.
title Dashboard riempitrice
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dashboard.ps1"
