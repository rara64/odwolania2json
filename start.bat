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
