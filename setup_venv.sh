#!/usr/bin/env bash

# ── Instalacja i konfiguracja wirtualnego środowiska (.venv) dla odwolania2json ──
# Skrypt do pierwszego uruchomienia (First Use Setup) na systemach Linux / macOS.
# Tworzy folder .venv, aktualizuje pip oraz instaluje wszystkie wymagane biblioteki.
#
# Użycie:
#   ./setup_venv.sh        - instalacja podstawowych zależności (requirements.txt)
#   ./setup_venv.sh --dev  - instalacja wraz z pakietami testowymi (requirements-dev.txt)

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

echo ""
echo "====================================================================="
echo " odwolania2json -- Konfiguracja wirtualnego środowiska (.venv)"
echo "====================================================================="
echo ""

# 1. Wykrywanie Pythona
PYTHON=""
if command -v python3.13 >/dev/null 2>&1; then
    PYTHON="python3.13"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON="python"
fi

if [ -z "$PYTHON" ]; then
    echo "[BŁĄD] Nie znaleziono interpretera Python w PATH!" >&2
    echo "Zainstaluj Python 3.13+ (np. 'sudo apt install python3 python3-venv')." >&2
    exit 1
fi

echo "[1/4] Wykryto interpreter bazowy:"
"$PYTHON" --version

# 2. Tworzenie venv
VENV_DIR=".venv"
if [ -f "$VENV_DIR/bin/python" ]; then
    echo ""
    echo "[2/4] Wirtualne środowisko $VENV_DIR już istnieje. Pomijanie tworzenia."
else
    echo ""
    echo "[2/4] Tworzenie wirtualnego środowiska w katalogu $VENV_DIR..."
    "$PYTHON" -m venv "$VENV_DIR"
    echo "      Środowisko zostało pomyślnie utworzone."
fi

VENV_PYTHON="$VENV_DIR/bin/python"

# 3. Aktualizacja pip
echo ""
echo "[3/4] Aktualizacja pip w środowisku $VENV_DIR..."
"$VENV_PYTHON" -m pip install --upgrade pip --quiet || {
    echo "[OSTRZEŻENIE] Aktualizacja pip nie powiodła się, kontynuacja..."
}

# 4. Instalacja zależności
echo ""
echo "[4/4] Instalacja bibliotek z requirements.txt..."
"$VENV_PYTHON" -m pip install -r requirements.txt

# Opcjonalne zależności dev
if [ "${1:-}" = "--dev" ]; then
    echo ""
    echo "Instalacja pakietów deweloperskich z requirements-dev.txt..."
    "$VENV_PYTHON" -m pip install -r requirements-dev.txt
fi

echo ""
echo "====================================================================="
echo " SUKCES! Wirtualne środowisko zostało pomyślnie skonfigurowane."
echo "====================================================================="
echo ""
echo "Aby rozpocząć pracę:"
echo "  - Przetwarzanie dokumentów CLI : ./start.sh"
echo "  - Interfejs graficzny GUI      : ./start_gui.sh"
echo "  - Uruchomienie powłoki w venv  : ./run_venv.sh"
echo ""
