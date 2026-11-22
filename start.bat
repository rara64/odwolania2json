@echo off
setlocal

REM ── Uruchamianie CLI odwolania2json ─────────────────────────────────────────
REM Bierze PDF-y z folderu input\ (rekurencyjnie), przetwarza je i zapisuje
REM wynikowe JSON-y do output\ w tej samej strukturze katalogów.
REM Użycie: start.bat [-v] [--report-dir KATALOG]

chcp 65001 >nul
cd /d "%~dp0"

REM Spróbuj python, a następnie launcher py
set "PYTHON="
where python >nul 2>&1
if not errorlevel 1 set "PYTHON=python"
if not defined PYTHON (
    where py >nul 2>&1
    if not errorlevel 1 set "PYTHON=py"
)
if not defined PYTHON (
    echo BŁĄD: Nie znaleziono Pythona w PATH. Zainstaluj Pythona 3.13+.
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
%PYTHON% main.py input output %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo Zakończono z kodem błędu %EXIT_CODE%.
    pause
) else (
    echo Gotowe. Wyniki JSON znajdują się w folderze output.
)

endlocal & exit /b %EXIT_CODE%
