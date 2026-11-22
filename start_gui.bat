@echo off
chcp 65001 >nul
setlocal

REM -------------------------------------------------------------------------
REM  Uruchamianie GUI odwolania2json
REM -------------------------------------------------------------------------
REM Uruchamia serwer HTTP GUI w bieżącym procesie i otwiera przeglądarkę.
REM Użycie: start_gui.bat [port]

set PORT=%1
if "%PORT%"=="" set PORT=8765

cd /d "%~dp0"

REM 1. Sprawdź lokalne środowisko wirtualne (.venv lub venv)
set "PYTHON="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON=%~dp0.venv\Scripts\python.exe"
) else if exist "%~dp0venv\Scripts\python.exe" (
    set "PYTHON=%~dp0venv\Scripts\python.exe"
)

REM 2. Jeśli brak venv, znajdź działający interpreter python w PATH
if not defined PYTHON (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=python"
    ) else (
        py -3.13 -c "import sys" >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON=py -3.13"
        ) else (
            py -3 -c "import sys" >nul 2>&1
            if not errorlevel 1 (
                set "PYTHON=py -3"
            ) else (
                py -c "import sys" >nul 2>&1
                if not errorlevel 1 set "PYTHON=py"
            )
        )
    )
)

if not defined PYTHON (
    echo BŁĄD: Nie znaleziono interpretera Python ani wirtualnego środowiska .venv!
    echo Aby skonfigurować środowisko, uruchom najpierw: setup_venv.bat
    pause
    exit /b 1
)

echo.
echo  odwolania2json GUI
echo  ==================
echo  Serwer : http://127.0.0.1:%PORT%
echo  Ctrl+C, aby zatrzymać.
echo.

REM Otwórz przeglądarkę po krótkim opóźnieniu (działa w tle)
start "" /B cmd /c "timeout /t 2 >nul && start http://127.0.0.1:%PORT%"

"%PYTHON%" gui_server.py --port %PORT%

endlocal