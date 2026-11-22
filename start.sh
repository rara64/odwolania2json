#!/usr/bin/env bash

# ── Uruchamianie CLI odwolania2json ─────────────────────────────────────────
# Bierze PDF-y z folderu input/ (rekurencyjnie), przetwarza je i zapisuje
# wynikowe JSON-y do output/ w tej samej strukturze katalogów.
# Użycie: ./start.sh [-v] [--report-dir KATALOG]

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

# 1. Sprawdź lokalne środowisko wirtualne (.venv lub venv)
PYTHON=""
if [ -f "$DIR/.venv/bin/python" ]; then
    PYTHON="$DIR/.venv/bin/python"
elif [ -f "$DIR/venv/bin/python" ]; then
    PYTHON="$DIR/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
fi

if [ -z "$PYTHON" ]; then
    echo "BŁĄD: Nie znaleziono Pythona w PATH ani środowiska .venv." >&2
    echo "Aby skonfigurować środowisko, uruchom najpierw: ./setup_venv.sh" >&2
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
