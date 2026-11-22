#!/usr/bin/env bash

# ── Uruchamianie GUI odwolania2json ─────────────────────────────────────────
# Uruchamia serwer HTTP GUI (bez otwierania przeglądarki).
# Użycie: ./start_gui.sh [port]

PORT="${1:-8765}"

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

echo
echo " odwolania2json GUI"
echo " =================="
echo " Serwer : http://127.0.0.1:${PORT}"
echo " Ctrl+C, aby zatrzymać."
echo

exec "$PYTHON" gui_server.py --port "$PORT"