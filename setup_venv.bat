@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM -------------------------------------------------------------------------
REM  Instalacja i konfiguracja wirtualnego srodowiska (.venv) dla odwolania2json
REM -------------------------------------------------------------------------
REM Skrypt do pierwszego uruchomienia (First Use Setup).
REM Tworzy folder .venv, aktualizuje pip oraz instaluje wszystkie wymagane biblioteki.
REM
REM Opcje:
REM   setup_venv.bat        - instalacja podstawowych zaleznosci (requirements.txt)
REM   setup_venv.bat --dev  - instalacja wraz z pakietami testowymi (requirements-dev.txt)

cd /d "%~dp0"

echo.
echo =====================================================================
echo  odwolania2json -- Konfiguracja wirtualnego srodowiska (.venv)
echo =====================================================================
echo.

REM 1. Wykrywanie interpretera Python
set "PYTHON="

REM Sprawdz python bezposrednio w PATH
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

if not defined PYTHON (
    echo [BLAD] Nie znaleziono dzialajacego interpretera Python w systemie!
    echo Zainstaluj Python 3.13+ ze strony https://www.python.org/downloads/
    echo Upewnij sie, ze zaznaczono opcje "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo [1/4] Uzywany interpreter Python bazowy:
%PYTHON% --version
if errorlevel 1 (
    echo [BLAD] Nie udalo sie uruchomic wybranego Pythona.
    if not "%1"=="--no-pause" pause
    exit /b 1
)

%PYTHON% -c "import venv, ensurepip" >nul 2>&1
if errorlevel 1 (
    echo [BLAD] Wybrany Python nie ma modulu venv/ensurepip.
    echo Zainstaluj pelna wersje Pythona wraz z modulami venv i ensurepip.
    if not "%1"=="--no-pause" pause
    exit /b 1
)

REM 2. Tworzenie srodowiska .venv
echo.
if exist ".venv\Scripts\python.exe" (
    echo [2/4] Wirtualne srodowisko .venv juz istnieje. Pomijanie tworzenia.
) else (
    echo [2/4] Tworzenie wirtualnego srodowiska w folderze .venv...
    %PYTHON% -m venv .venv
    if errorlevel 1 (
        echo [BLAD] Nie udalo sie utworzyc wirtualnego srodowiska .venv.
        pause
        exit /b 1
    )
    echo       Wirtualne srodowisko zostalo pomyslnie utworzone.
)

set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
    echo [BLAD] Nie znaleziono interpretera %VENV_PYTHON%!
    pause
    exit /b 1
)

REM 3. Aktualizacja pip
echo.
echo [3/4] Aktualizacja pip w srodowisku .venv...
"%VENV_PYTHON%" -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo [OSTRZEZENIE] Aktualizacja pip nie powiodla sie, kontynuacja instalacji zaleznosci...
)

REM 4. Instalacja zaleznosci
echo.
echo [4/4] Instalacja bibliotek z requirements.txt...
"%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [BLAD] Wystapil problem podczas instalacji requirements.txt.
    pause
    exit /b 1
)

REM Opcjonalna instalacja requirements-dev.txt
if "%1"=="--dev" (
    echo.
    echo Instalacja zaleznosci deweloperskich z requirements-dev.txt...
    "%VENV_PYTHON%" -m pip install -r requirements-dev.txt
    if errorlevel 1 (
        echo [OSTRZEZENIE] Nie udalo sie zainstalowac requirements-dev.txt.
    )
)

echo.
echo =====================================================================
echo  SUKCES! Wirtualne srodowisko zostalo skonfigurowane pomyslnie.
echo =====================================================================
echo.
echo Aby rozpoczac prace:
echo   - Przetwarzanie dokumentow CLI : start.bat
echo   - Interfejs graficzny GUI      : start_gui.bat
echo   - Uruchomienie powloki w venv  : run_venv.bat
echo.
if not "%1"=="--no-pause" (
    pause
)

endlocal & exit /b 0
