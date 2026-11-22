@echo off
setlocal

REM ── Uruchamianie GUI odwolania2json ─────────────────────────────────────────
REM Uruchamia serwer HTTP GUI w bieżącym procesie i otwiera przeglądarkę.
REM Użycie: start_gui.bat [port]

set PORT=%1
if "%PORT%"=="" set PORT=8765

cd /d "%~dp0"

REM Spróbuj python, a następnie launcher py
where python >nul 2>&1
if %ERRORLEVEL%==0 (
    set PYTHON=python
) else (
    where py >nul 2>&1
    if %ERRORLEVEL%==0 (
        set PYTHON=py
    ) else (
        echo BŁĄD: Nie znaleziono Pythona w PATH. Zainstaluj Pythona 3.9+.
        pause
        exit /b 1
    )
)

echo.
echo  odwolania2json GUI
echo  ==================
echo  Serwer : http://127.0.0.1:%PORT%
echo  Ctrl+C, aby zatrzymać.
echo.

REM Otwórz przeglądarkę po krótkim opóźnieniu (działa w tle)
start "" /B cmd /c "timeout /t 2 >nul && start http://127.0.0.1:%PORT%"

%PYTHON% gui_server.py --port %PORT%

endlocal