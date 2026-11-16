@echo off
chcp 65001 >nul
title Установка «Узнать товар»
cd /d "%~dp0"
rem Установка в магазине (scripts/install.py). Повторный запуск продолжает с того, что не сделано.
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY call :findpy
if not defined PY (
    echo Python не найден. Устанавливаю Python 3.14 из каталога программ Windows ^(winget^), это бесплатно...
    winget install -e --id Python.Python.3.14 --scope user --silent --accept-package-agreements --accept-source-agreements
    call :findpy
)
if not defined PY (
    echo.
    echo Не удалось установить Python. Установите Python 3.14 с сайта https://www.python.org/downloads/
    echo ^(галочка «Add python.exe to PATH»^) и запустите Установить.bat ещё раз.
    goto end
)
"%PY%" -m scripts.install %*
:end
echo.
echo Нажмите любую клавишу, чтобы закрыть окно...
pause >nul
exit /b

:findpy
rem Сначала самый новый Python через py, потом обычные папки установки (сразу после winget py в этом окне ещё не виден).
for %%V in (3.14 3.13 3.12) do (
    if not defined PY for /f "usebackq delims=" %%P in (`py -%%V -c "import sys; print(sys.executable)" 2^>nul`) do set "PY=%%P"
)
if defined PY exit /b
for %%V in (314 313 312) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
)
for %%V in (3.14 3.13 3.12) do (
    if not defined PY if exist "%LOCALAPPDATA%\Python\pythoncore-%%V-64\python.exe" set "PY=%LOCALAPPDATA%\Python\pythoncore-%%V-64\python.exe"
)
exit /b
