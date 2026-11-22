#!/usr/bin/env bash

# ── Uruchamianie GUI odwolania2json ─────────────────────────────────────────
# Uruchamia serwer HTTP GUI (bez otwierania przeglądarki).
# Użycie: ./start_gui.sh [port]

PORT="${1:-8765}"

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

echo
echo " odwolania2json GUI"
echo " =================="
echo " Serwer : http://127.0.0.1:${PORT}"
echo " Ctrl+C, aby zatrzymać."
echo

exec "$PYTHON" gui_server.py --port "$PORT"