@echo off
chcp 65001 >nul
title Обновление «Узнать товар»
cd /d "%~dp0"
rem Обновление установленной программы из этой (новой) папки: scripts/update.py. Данные магазина остаются.
set "PY="
set "PYTHONIOENCODING=utf-8"
call :findpy
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY (
    echo Python не найден. Обновлять можно только там, где программа уже установлена ^(Установить.bat^).
    goto end
)
"%PY%" -m scripts.update %*
:end
echo.
echo Нажмите любую клавишу, чтобы закрыть окно...
pause >nul
exit /b

:findpy
rem Тот же порядок поиска Python, что в Установить.bat.
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
