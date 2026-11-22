@echo off
setlocal

REM ── odwolania2json GUI launcher ──────────────────────────────────────────────
REM Starts the in-process HTTP GUI server and opens the browser.
REM Usage: start_gui.bat [port]

set PORT=%1
if "%PORT%"=="" set PORT=8765

cd /d "%~dp0"

REM Try python, then py launcher
where python >nul 2>&1
if %ERRORLEVEL%==0 (
    set PYTHON=python
) else (
    where py >nul 2>&1
    if %ERRORLEVEL%==0 (
        set PYTHON=py
    ) else (
        echo ERROR: Python not found on PATH. Please install Python 3.9+.
        pause
        exit /b 1
    )
)

echo.
echo  odwolania2json GUI
echo  ==================
echo  Server : http://127.0.0.1:%PORT%
echo  Ctrl+C  to stop.
echo.

REM Open browser after a short delay (runs in background)
start "" /B cmd /c "timeout /t 2 >nul && start http://127.0.0.1:%PORT%"

%PYTHON% gui_server.py --port %PORT%

endlocal
