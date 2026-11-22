#!/usr/bin/env bash

# ── Uruchamianie CLI odwolania2json ─────────────────────────────────────────
# Bierze PDF-y z folderu input/ (rekurencyjnie), przetwarza je i zapisuje
# wynikowe JSON-y do output/ w tej samej strukturze katalogów.
# Użycie: ./start.sh [-v] [--report-dir KATALOG]

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

PYTHON="$DIR/.venv/bin/python"

# 1. Jeśli brak .venv, utwórz je automatycznie przez setup.
if [ ! -f "$PYTHON" ]; then
    echo "Nie znaleziono .venv. Uruchamiam automatyczny setup..."
    "$DIR/setup_venv.sh"
fi

if [ ! -f "$PYTHON" ]; then
    echo "BŁĄD: Po setupie nie znaleziono interpretera .venv: $PYTHON" >&2
    exit 1
fi

# Foldery wejściowy i wynikowy zgodnie ze specyfikacją zadania
mkdir -p input output

echo
echo " odwolania2json CLI"
echo " =================="
echo " Wejście : $(pwd)/input"
echo " Wyjście : $(pwd)/output"
echo

export PYTHONUTF8=1
exec "$PYTHON" main.py input output "$@"
