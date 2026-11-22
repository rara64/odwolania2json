@echo off
chcp 65001 >nul
setlocal

REM -------------------------------------------------------------------------
REM  Uruchamianie CLI odwolania2json
REM -------------------------------------------------------------------------
REM Bierze PDF-y z folderu input\ (rekurencyjnie), przetwarza je i zapisuje
REM wynikowe JSON-y do output\ w tej samej strukturze katalogów.
REM Użycie: start.bat [-v] [--report-dir KATALOG]

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

REM Foldery wejściowy i wynikowy zgodnie ze specyfikacją zadania
if not exist "input" mkdir "input"
if not exist "output" mkdir "output"

echo.
echo  odwolania2json CLI
echo  ==================
echo  Wejście : %CD%\input
echo  Wyjście : %CD%\output
echo.

set "PYTHONUTF8=1"
"%PYTHON%" main.py input output %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo Zakończono z kodem błędu %EXIT_CODE%.
    pause
) else (
    echo Gotowe. Wyniki JSON znajdują się w folderze output.
)

endlocal & exit /b %EXIT_CODE%
