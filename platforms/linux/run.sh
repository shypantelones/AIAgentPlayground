#!/usr/bin/env bash
# Linux launcher for the OpenClaw control panel.
#   bash run.sh               start the panel and open http://127.0.0.1:8765 (if a desktop is available)
#   OPENCLAW_NO_BROWSER=1 bash run.sh    start without opening a browser
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/../../control-panel"

# Load config.env (KEY=VALUE per line). Variables you already set in your environment win.
while IFS='=' read -r key value || [ -n "$key" ]; do
  case "$key" in ''|\#*) continue ;; esac
  key="${key//[[:space:]]/}"; value="${value%$'\r'}"
  [ -n "$key" ] && [ -z "${!key+x}" ] && export "$key=$value"
done < "$HERE/config.env"

command -v python3 >/dev/null 2>&1 || { echo "Python 3 not found. Install it with your package manager (for example: sudo apt install python3)."; exit 1; }
command -v docker  >/dev/null 2>&1 || echo "Warning: 'docker' not found. Install Docker Engine and the compose plugin: https://docs.docker.com/engine/install/"
docker info >/dev/null 2>&1 || echo "Warning: cannot talk to Docker. Is the daemon running, and is your user in the 'docker' group?"

URL="http://127.0.0.1:${PANEL_PORT:-8765}"
if [ -z "$OPENCLAW_NO_BROWSER" ] && command -v xdg-open >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  ( sleep 1.5; xdg-open "$URL" >/dev/null 2>&1 ) &
fi
cd "$APP"
exec python3 app.py
