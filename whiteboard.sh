#!/usr/bin/env bash
# Launcher: right venv, right folder (calibration + captures), OpenCV window via XWayland.
# All output (crashes included) is also written to logs/YYYY-MM-DD_HHhMMmSS.log
# Usage: ./whiteboard.sh http://PHONE_IP:8080/video [--size 120x90] [other options]
# The venv defaults to ~/.venvs/whiteboard; override with WHITEBOARD_VENV=/path/to/venv
cd "$(dirname "$(readlink -f "$0")")" || exit 1
mkdir -p logs
log="logs/$(date +%Y-%m-%d_%Hh%Mm%S).log"
echo "[log] $PWD/$log"
venv="${WHITEBOARD_VENV:-$HOME/.venvs/whiteboard}"
QT_QPA_PLATFORM=xcb PYTHONFAULTHANDLER=1 PYTHONUNBUFFERED=1 \
    "$venv/bin/python" whiteboard.py "$@" 2>&1 \
    | grep --line-buffered -v -e 'QFontDatabase' -e 'Qt no longer ships fonts' -e '^\[mjpeg @' \
    | tee "$log"
code=${PIPESTATUS[0]}
# launched from an app menu there is no terminal: show errors as a notification
if [ "$code" -ne 0 ] && command -v notify-send >/dev/null; then
    notify-send -u critical "Whiteboard scanner" "$(tail -n 3 "$log")"
fi
echo "[end] exit code $code" | tee -a "$log"
exit "$code"
