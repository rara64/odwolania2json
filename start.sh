#!/usr/bin/env bash

# ── Uruchamianie CLI odwolania2json ─────────────────────────────────────────
# Bierze PDF-y z folderu input/ (rekurencyjnie), przetwarza je i zapisuje
# wynikowe JSON-y do output/ w tej samej strukturze katalogów.
# Użycie: ./start.sh [-v] [--report-dir KATALOG]

set -u

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

# Spróbuj python3, a następnie python
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "BŁĄD: Nie znaleziono Pythona w PATH. Zainstaluj Pythona 3.13+." >&2
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
