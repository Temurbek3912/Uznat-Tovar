@echo off
chcp 65001 >nul
title Узнать товар (облегчённый режим)
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" -m scripts.start_demo --lite %*
echo.
echo Нажмите любую клавишу, чтобы закрыть окно...
pause >nul
