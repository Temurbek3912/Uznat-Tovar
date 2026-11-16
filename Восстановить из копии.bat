@echo off
chcp 65001 >nul
title Узнать товар — восстановить из копии
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
set "PYTHONIOENCODING=utf-8"
"%PY%" -m scripts.lite_restore %*
echo.
echo Нажмите любую клавишу, чтобы закрыть окно...
pause >nul
