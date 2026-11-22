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

set "PYTHON=%~dp0.venv\Scripts\python.exe"

REM 1. Jeśli brak .venv, utwórz je automatycznie przez setup.
if not exist "%PYTHON%" (
    echo Nie znaleziono .venv. Uruchamiam automatyczny setup...
    call "%~dp0setup_venv.bat" --no-pause
    if errorlevel 1 (
        echo BŁĄD: Nie udało się utworzyć środowiska .venv.
        pause
        exit /b 1
    )
)

if not exist "%PYTHON%" (
    echo BŁĄD: Po setupie nie znaleziono interpretera .venv: %PYTHON%
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