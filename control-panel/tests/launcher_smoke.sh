#!/usr/bin/env bash
# Smoke test for the macOS/Linux launchers: starts each one, checks the panel answers, stops it.
#   bash control-panel/tests/launcher_smoke.sh
# Needs python3 and bash. Does not need Docker to be running (the panel then reports docker=false).
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
fail=0
for os in linux macos; do
  port=$((8790 + RANDOM % 50))
  # PANEL_PORT is set in the environment on purpose: it must win over config.env (which says 8765)
  log="$(mktemp)"
  PANEL_PORT=$port AIAGENTPLAYGROUND_NO_BROWSER=1 bash "$ROOT/platforms/$os/run.sh" >"$log" 2>&1 &
  pid=$!
  ok=""
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    out="$(python3 - "$port" <<'PY' 2>/dev/null
import json, sys, urllib.request
r = json.load(urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/state", timeout=5))
print("os=%s docker=%s" % ((r.get("platform") or {}).get("os", "?"), r.get("docker")))
PY
)" && { ok="$out"; break; }
  done
  pkill -P $pid 2>/dev/null; kill $pid 2>/dev/null; wait $pid 2>/dev/null
  if [ -n "$ok" ]; then echo "PASS  platforms/$os/run.sh -> panel answered on port $port ($ok)"; else echo "FAIL  platforms/$os/run.sh"; sed 's/^/      /' "$log"; fail=1; fi
  echo "      launcher output: $(tr '\n' ' ' < "$log" | cut -c1-200)"
  rm -f "$log"
done
exit $fail
