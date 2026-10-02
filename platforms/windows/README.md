# Windows

**Status: fully tested** (Windows 11 Home, Docker Desktop with WSL2, NVIDIA RTX GPU). This is where the project was built.

## 1. Install
You need Docker Desktop and Python 3. From PowerShell (each command downloads from the publisher; accept the UAC prompt):

```powershell
winget install --id Docker.DockerDesktop --exact
winget install --id Python.Python.3.11 --exact      # skip if `python --version` already works
```
Start Docker Desktop once and accept its terms; wait until it says "Engine running". Reboot if the installer asks.

**NVIDIA GPU (optional, much faster):** install a current NVIDIA driver. Docker Desktop's WSL2 backend then exposes the
GPU automatically. Check with:
```powershell
docker info --format "{{json .Runtimes}}"      # should list "nvidia"
```
No NVIDIA GPU? It still works; the model runs on the CPU (slower, so prefer smaller models).

Docker Desktop is free for personal use and small businesses; larger companies need a paid plan.

## 2. Run
```powershell
cd platforms\windows
.\run.ps1                 # opens http://127.0.0.1:8765
.\run.ps1 -NoBrowser      # start without opening a browser
```
If PowerShell blocks the script: `powershell -ExecutionPolicy Bypass -File .\run.ps1`

Then in the page: **Download model** (for a 16 GB GPU, `qwen3:14b` fits), create an agent, tick it, and chat.

## 3. Settings: `config.env`
`OLLAMA_MODE` (`auto`/`nvidia`/`cpu`), `OLLAMA_NUM_PARALLEL`, `PANEL_PORT`. See the comments in the file.

## Windows specifics
- **API keys** are encrypted with DPAPI, tied to your Windows login (useless on another account or PC). If you reinstall
  Windows or move PCs, re-enter them.
- **Keep the project under your user folder** (for example `C:\Users\<you>\openclaw-playground`). Docker Desktop failed to
  bind-mount files from a temporary `AppData\Roaming` location during development.
- **Agent workspaces are Docker volumes**, not folders on disk. Windows bind mounts do not support the atomic file renames
  OpenClaw uses, which broke it. To get files out: `docker cp openclaw-i-<name>-gateway-1:/home/node/.openclaw/workspace .`
- **Windows 11 Home** has no Hyper-V and no Windows Sandbox; that is fine for Docker Desktop (WSL2). The optional VM tier
  uses VirtualBox + Vagrant (`winget install Oracle.VirtualBox Hashicorp.Vagrant`); VirtualBox runs slower alongside WSL2.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Red banner "Docker is not running" | Start Docker Desktop, wait for "Engine running", reload the page |
| Model is slow / "CPU-only container" shown | No NVIDIA runtime detected: update the driver, restart Docker Desktop, re-check `docker info` |
| `docker` not recognised in a new terminal | Close and reopen the terminal (PATH refresh) or reboot |
| Port 8765 in use | change `PANEL_PORT` in `config.env` |
| Agent says it cannot reach a site | by design: add the domain/IP in that agent's **Network** tab |

## Legacy
`legacy-docker-sandbox/` is the original single-agent version (PowerShell scripts, Windows only). The control panel
replaces it. **Do not run its `down.ps1 -Wipe`**: it would delete the shared downloaded model volume (about 9 GB).
