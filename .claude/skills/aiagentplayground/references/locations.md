# Where everything is installed and stored

Sizes are from a real install (Windows, 3 agents). `<name>` is an agent's name; `<root>` is the project folder.

## Contents
1. Project folder  2. Docker objects  3. Tools per OS  4. Secrets  5. Ports  6. Logs  7. Removing things

## 1. Project folder (`<root>`, e.g. `C:\Users\<you>\aiagentplayground`)
```
README.md
.claude/skills/aiagentplayground/   this skill (+ scripts/doctor.py)
control-panel/
  app.py, platform_support.py, static/, templates/, tests/     the app (OS-neutral)
  templates/model-server/    base.yml + mode-container.yml | mode-nvidia.yml | mode-host.yml
  model_catalog.json         curated downloadable models for the Model-tab selector (editable)
  model_info.py              memory-estimate and fit logic for that selector
  data/                      RUNTIME DATA (git-ignored; back this up)
    instances/<name>/
      meta.json              port, backend (local/cloud), provider, model, rate limit
      .env                   dashboard port, dashboard LOGIN TOKEN, image, instance dir  (do not share)
      chats.json             chat history shown in the panel
      proxy/allowlist.src.txt   what the Network tab shows/edits
      proxy/allowlist.txt, allowlist-ips.txt, squid.conf   generated for the proxy
      relay/default.conf.template   cloud mode only (contains no key)
    secrets/<name>.token     API-key marker (see 4)
    peers.json               peer links between agents (direction, approval, limits)
    peer-sessions/<id>.json  one record per peering conversation (state, transcript); newest 50 kept
                             (the messages are also in each agent's chats.json as conversation `peer-<id>`)
platforms/<windows|macos|linux>/   launcher (run.ps1 / run.sh), config.env, README.md
platforms/windows/legacy-docker-sandbox/   old single-agent sandbox (Windows only)
vm-sandbox/Vagrantfile       optional VM tier
```
Settings you may edit: `platforms/<os>/config.env` (`OLLAMA_MODE`, `OLLAMA_NUM_PARALLEL`, `PANEL_PORT`); an environment variable overrides it.

## 2. Docker objects (names start with `aiagentplayground`, except the legacy single-sandbox stack below)
| Kind | Name | Notes |
|---|---|---|
| Compose project, per agent | `aiagentplayground-i-<name>` | containers `aiagentplayground-i-<name>-<service>-1`: `gateway`, `egress-proxy`, `llm-relay`, `ui-forward`, plus `cloud-relay` in cloud mode |
| Compose project, shared model | `aiagentplayground-shared` | container `aiagentplayground-shared-ollama-1` (container modes) or `aiagentplayground-shared-ollama-host-bridge-1` (host mode) |
| Legacy project | `openclaw-sandbox` | old single sandbox; normally stopped |
| Volumes, per agent | `aiagentplayground-i-<name>_state` (~65 MB), `aiagentplayground-i-<name>_workspace` | agent memory/config and its working files. Deleting the agent in the panel removes them |
| Volume, shared | `openclaw-sandbox_ollama-models` (~9.3 GB with qwen3:14b) | the downloaded models for ALL agents. Name is historical; do not delete casually |
| Legacy volumes | `openclaw-sandbox_openclaw-state`, `openclaw-sandbox_openclaw-workspace` | from the old sandbox |
| Networks | `aiagentplayground-i-<name>_internal` (no internet), `aiagentplayground-i-<name>_egress`, `aiagentplayground-llm` (shared, internal), `aiagentplayground-shared_hostnet` (host mode only) | agents are never on a common network |
| Images | `ghcr.io/openclaw/openclaw:latest` ~5.0 GB; `ollama/ollama:latest` ~9.3 GB (container modes only); `ubuntu/squid:latest` ~300 MB; `alpine/socat:latest` ~15 MB; `nginxinc/nginx-unprivileged:alpine` ~80 MB (cloud mode only) | `nginx:alpine` may also exist from testing and is not needed |

## 3. Tools by OS
| Tool | Windows | macOS | Linux |
|---|---|---|---|
| Docker app | `C:\Program Files\Docker\Docker` | `/Applications/Docker.app` | Docker Engine (system packages) |
| Docker data (images, volumes) | one disk image: `%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx` (~30 GB here; grows, doesn't auto-shrink) | `~/Library/Containers/com.docker.docker/Data/vms/0/data/` (a disk image) | `/var/lib/docker` |
| Docker config | `%USERPROFILE%\.docker` | `~/.docker` | `~/.docker` |
| Python | Store alias `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` (real install under `Program Files\WindowsApps`) | system `python3` / python.org | system `python3` |
| Ollama (native) | not used by default | `~/.ollama/models` (host mode) | only if `OLLAMA_MODE=host` |
| VirtualBox | `C:\Program Files\Oracle\VirtualBox` (not on PATH by default) | `/Applications/VirtualBox.app` | system packages |
| Vagrant | `C:\Program Files\Vagrant\bin` | `/opt/vagrant` or brew | system packages |
| VM disks / boxes (only after use) | `%USERPROFILE%\VirtualBox VMs`, `%USERPROFILE%\.vagrant.d` | `~/VirtualBox VMs`, `~/.vagrant.d` | same |

## 4. Secrets
| Secret | Where |
|---|---|
| Cloud API keys | Windows: DPAPI-encrypted blob at `control-panel/data/secrets/<name>.token`. macOS: Keychain, service `aiagentplayground-panel`, account `<name>` (marker file in `data/secrets/`). Linux: Secret Service keyring (same service/account) or, without a keyring, a 0600 file in `data/secrets/` that is NOT encrypted. The panel's Model tab states which is in use. |
| Agent dashboard login token | `control-panel/data/instances/<name>/.env` (`OPENCLAW_GATEWAY_TOKEN`), also on the panel's Info tab |
Never print these in chat. The key is passed to the relay container only through the process environment (visible to `docker inspect` on that container).

## 5. Ports (all bound to 127.0.0.1)
`8765` the control panel (`PANEL_PORT`). `18801+` one OpenClaw dashboard per agent (alpha 18801, beta 18802, ...). `11434` Ollama: only inside Docker networks in container modes; the host's own Ollama in host mode. Legacy sandbox used `18789`.

## 6. Logs
- Panel: its terminal output (and `control-panel/panel.log` / `panel.err` when started in the background).
- Per agent: panel **Logs** tab (proxy requests / gateway / model relay), or `docker logs aiagentplayground-i-<name>-gateway-1`.
- Proxy access log: inside the container at `/var/log/squid/access.log`.
- Model server: `docker logs aiagentplayground-shared-ollama-1`.

## 7. Removing things (always confirm with the user first)
| Goal | How | What is lost |
|---|---|---|
| Stop one agent | panel Stop, or `docker compose -p aiagentplayground-i-<name> stop` | nothing |
| Delete one agent | panel Info tab > Delete agent (type its name) | its memory, workspace, chats |
| Stop the model server | panel top bar Stop | nothing (models stay) |
| Free the 9 GB model | `docker volume rm openclaw-sandbox_ollama-models` (only with all agents stopped) | the downloaded models (re-download needed) |
| Remove images | `docker image rm <image>` for those in the table | re-pulled on next start |
| Reclaim Docker disk (Windows) | Docker Desktop > Troubleshoot > Clean/Purge or compact the VHDX | per option chosen |
| Remove a stored key | panel Model tab > Remove (only while the agent is on Local) | the key |
Never run the legacy `platforms/windows/legacy-docker-sandbox/scripts/down.ps1 -Wipe`: it also deletes the shared model volume.
