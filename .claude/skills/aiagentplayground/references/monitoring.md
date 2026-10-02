# Monitoring

Start with the doctor: `python .claude/skills/aiagentplayground/scripts/doctor.py [--sizes] [--blocked] [--json]`
(exit code 0 all good, 1 warnings, 2 failures; read-only; prints no secrets).

## What healthy looks like
| Check | Healthy | Not healthy -> look at |
|---|---|---|
| Docker daemon | engine version shown | Docker not running (start it) |
| Panel | `http://127.0.0.1:8765` OK | start the launcher in `platforms/<os>/` |
| Shared model server | running; `loaded model: ...` after the first prompt | stopped is fine (auto-starts); host mode: Ollama must be running |
| Agent | status `healthy`, dashboard "listening", 4/4 containers (5/5 cloud) | `starting` > 1 min or a container `Exited (1)`: Logs tab > Agent gateway |
| Containers | `N/N running` for each `aiagentplayground-i-<name>` | a service restarting/exited with a non-zero code |
| Disk | > 15 GB free (warn), > 3 GB (fail threshold) | prune images or free disk before pulling more |

## Using the panel to watch
- **Top bar**: model-server state, which model is loaded, mode (NVIDIA GPU / CPU / Ollama on this computer), models available.
- **Sidebar dots**: green healthy, orange starting/partial, grey stopped.
- **Logs tab** per agent: *Network requests (proxy)*, *Agent gateway*, *Model relay*. In the proxy log `TCP_TUNNEL` = allowed, `TCP_DENIED` = blocked.
- **Security tab > Verify isolation**: every line must say PASS (11 checks in cloud mode, 9 in local). Run after changing network or model settings.
- The page polls every 5 seconds; the job tray (bottom right) shows long operations and their errors.

## Blocked-request triage
`python .../doctor.py --blocked` lists the top denied destinations per running agent. Expected on every start: `github.com`, `api.github.com`,
`chatgpt.com`, `telemetry.openclaw.ai` (OpenClaw probing; keep blocked). If a destination the user *wants* is denied, add the
domain/IP in the Network tab (domain, IPv4, range or CIDR). Remember: a hostname that resolves to an allowed IP is also allowed.

## Model and GPU
```bash
docker exec aiagentplayground-shared-ollama-1 ollama ps     # PROCESSOR column: want "100% GPU"
docker exec aiagentplayground-shared-ollama-1 ollama list   # installed models and sizes
nvidia-smi                                         # VRAM used/total (Windows/Linux)
```
A CPU/GPU split (e.g. `30%/70% CPU/GPU`) means the model plus its context doesn't fit VRAM: keep `OLLAMA_NUM_PARALLEL=1`, use a smaller model, or accept slower replies. Host mode (macOS): use the native `ollama ps`.
Replies for a loaded model take seconds; the first prompt after idle (model unloaded after 30 min) takes longer.

## Cost watching (cloud mode)
The relay throttles each agent (default 30 requests/min, set in the Model tab) and returns HTTP 429 beyond that. The relay does not count tokens or money: have the user set a **spend limit with the provider** and check the provider's usage page. A 401 in chat means the key is wrong/revoked; 429 can be the relay's throttle or the provider's.

## Disk and growth
- Docker's data grows: images (~15 GB), models (~9 GB each), agent state (~65 MB each).
- `doctor.py --sizes` lists each AI Agent Playground volume. On Windows the single `docker_data.vhdx` file only shrinks via Docker Desktop's clean/compact tools.

## Raw docker, read-only inspection
```bash
docker ps --filter "name=aiagentplayground"                                   # what's running
docker ps -a --format "table {{.Names}}\t{{.Status}}" | grep aiagentplayground  # (PowerShell: | Select-String aiagentplayground)
docker stats --no-stream $(docker ps -q --filter "name=aiagentplayground")    # CPU / memory per container
# PowerShell: docker stats --no-stream (docker ps -q --filter "name=aiagentplayground")   (docker stats itself has no --filter flag)
docker logs --tail 100 aiagentplayground-i-<name>-gateway-1
docker exec aiagentplayground-i-<name>-egress-proxy-1 tail -n 50 /var/log/squid/access.log
docker volume ls | grep aiagentplayground ; docker system df
```
The gateway runs with a read-only filesystem and no capabilities by design; `docker exec` into it is for inspection, not repair.

## Stopping everything
The panel (Ctrl+C) and the agents are separate: stopping the panel leaves agents running. To stop all agents: panel Stop on each, or
`docker ps -q --filter "name=aiagentplayground-i-" | xargs docker stop` (PowerShell: `docker ps -q --filter "name=aiagentplayground-i-" | ForEach-Object { docker stop $_ }`). Quitting Docker stops everything. Containers don't auto-restart; after a reboot start Docker, the launcher, then each agent.
