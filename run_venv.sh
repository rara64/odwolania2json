#!/usr/bin/env bash

# ── Uruchamianie wirtualnego środowiska (.venv) dla odwolania2json ───────────
# Skrypt do aktywacji venv lub bezpośredniego uruchamiania poleceń w środowisku.
#
# Użycie:
#   ./run_venv.sh                   - uruchamia powłokę shell z aktywowanym .venv
#   ./run_venv.sh [polecenie/skrypt] - wykonuje polecenie w środowisku .venv
#
# Przykłady:
#   ./run_venv.sh python main.py input output
#   ./run_venv.sh pytest tests/
#   ./run_venv.sh pip list

set -eu

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$DIR/.venv"
ACTIVATE_SCRIPT="$VENV_DIR/bin/activate"
PYTHON_BIN="$VENV_DIR/bin/python"

if [ ! -f "$PYTHON_BIN" ]; then
    echo "" >&2
    echo "=====================================================================" >&2
    echo " [BŁĄD] Środowisko .venv nie zostało jeszcze utworzone!" >&2
    echo "=====================================================================" >&2
    echo " Nie znaleziono: $PYTHON_BIN" >&2
    echo " Aby skonfigurować środowisko na pierwsze użycie, uruchom:" >&2
    echo "" >&2
    echo "     ./setup_venv.sh" >&2
    echo "" >&2
    echo "=====================================================================" >&2
    echo "" >&2
    exit 1
fi

# Jeśli brak argumentów, uruchom podpowłokę z aktywowanym venv
if [ $# -eq 0 ]; then
    echo "Aktywowanie środowiska odwolania2json (.venv)..."
    echo "Wpisz 'exit', aby opuścić środowisko."
    exec bash --init-file <(echo ". \"$ACTIVATE_SCRIPT\"; PS1=\"(.venv) \$PS1\"")
fi

# Jeśli pierwszym argumentem jest "python", użyj interpretera z venv
if [ "$1" = "python" ]; then
    shift
    exec "$PYTHON_BIN" "$@"
fi

# W przeciwnym razie aktywuj venv i wykonaj przekazane polecenie
# shellcheck disable=SC1090
source "$ACTIVATE_SCRIPT"
exec "$@"
