@echo off
chcp 65001 >nul
setlocal

REM -------------------------------------------------------------------------
REM  Uruchamianie wirtualnego srodowiska (.venv) dla odwolania2json
REM -------------------------------------------------------------------------
REM Skrypt do aktywacji venv lub bezposredniego uruchamiania polecen w srodowisku.
REM
REM Uzycie:
REM   run_venv.bat                   - otwiera nowa powloke cmd z aktywowanym .venv
REM   run_venv.bat [polecenie/skrypt] - wykonuje polecenie w srodowisku .venv
REM
REM Przyklady:
REM   run_venv.bat python main.py input output
REM   run_venv.bat pytest tests/
REM   run_venv.bat pip list

cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "ACTIVATE_BAT=%VENV_DIR%\Scripts\activate.bat"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo.
    echo =====================================================================
    echo  [BLAD] Srodowisko .venv nie zostalo jeszcze utworzone!
    echo =====================================================================
    echo  Nie znaleziono pliku: %PYTHON_EXE%
    echo  Aby skonfigurowac srodowisko na pierwsze uzycie, uruchom:
    echo.
    echo      setup_venv.bat
    echo.
    echo =====================================================================
    echo.
    if "%~1"=="" pause
    exit /b 1
)

REM Jesli nie przekazano zadnych argumentow, otworz interaktywna konsole z venv
if "%~1"=="" (
    echo.
    echo Aktywowanie srodowiska odwolania2json (.venv)...
    echo Wpisz 'exit', aby opuscic wirtualne srodowisko.
    echo.
    cmd.exe /k ""%ACTIVATE_BAT%""
    exit /b 0
)

REM Jesli pierwszym argumentem jest "python", przekaz reszte argumentow
if /i "%~1"=="python" (
    call "%ACTIVATE_BAT%"
    set "ARGS=%*"
    set "ARGS=!ARGS:*python =!"
    "%PYTHON_EXE%" !ARGS!
    exit /b !ERRORLEVEL!
)

REM W przeciwnym razie aktywuj venv i uruchom przekazane polecenie
call "%ACTIVATE_BAT%"
%*
exit /b %ERRORLEVEL%
