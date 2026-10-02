#!/usr/bin/env bash
# macOS launcher for the AI Agent control panel.
#   bash run.sh               start the panel and open http://127.0.0.1:8765
#   AIAGENTPLAYGROUND_NO_BROWSER=1 bash run.sh    start without opening a browser
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/../../control-panel"

# GUI-installed tools are often missing from PATH in a plain shell.
export PATH="$PATH:/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin"

# Load config.env (KEY=VALUE per line). Variables you already set in your environment win.
while IFS='=' read -r key value || [ -n "$key" ]; do
  case "$key" in ''|\#*) continue ;; esac
  key="${key//[[:space:]]/}"; value="${value%$'\r'}"
  [ -n "$key" ] && [ -z "${!key+x}" ] && export "$key=$value"
done < "$HERE/config.env"

command -v python3 >/dev/null 2>&1 || { echo "Python 3 not found. Run 'xcode-select --install' or install it from https://www.python.org/downloads/"; exit 1; }
command -v docker  >/dev/null 2>&1 || echo "Warning: 'docker' not found. Install and start Docker Desktop."
if [ "${OLLAMA_MODE:-host}" = "host" ] && ! curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "Note: Ollama is not running on this Mac. Install it (https://ollama.com or 'brew install ollama') and start it."
fi

URL="http://127.0.0.1:${PANEL_PORT:-8765}"
if [ -z "$AIAGENTPLAYGROUND_NO_BROWSER" ]; then ( sleep 1.5; open "$URL" ) & fi
cd "$APP"
exec python3 app.py
