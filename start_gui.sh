#!/usr/bin/env bash

# ── Uruchamianie GUI odwolania2json ─────────────────────────────────────────
# Uruchamia serwer HTTP GUI (bez otwierania przeglądarki).
# Użycie: ./start_gui.sh [port]

PORT="${1:-8765}"

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

# Spróbuj python3, a następnie python
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "BŁĄD: Nie znaleziono Pythona w PATH. Zainstaluj Pythona 3.9+." >&2
    exit 1
fi

echo
echo " odwolania2json GUI"
echo " =================="
echo " Serwer : http://127.0.0.1:${PORT}"
echo " Ctrl+C, aby zatrzymać."
echo

exec "$PYTHON" gui_server.py --port "$PORT"