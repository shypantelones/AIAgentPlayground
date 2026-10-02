# Install on Windows (fully tested)

Verified on Windows 11 Home, Docker Desktop (WSL2 backend), NVIDIA RTX GPU. Repo source of truth: `platforms/windows/README.md`.

## What you need
| Tool | Needed for | Download size | Install |
|---|---|---|---|
| Docker Desktop | everything | ~600 MB | `winget install --id Docker.DockerDesktop --exact` |
| Python 3.8+ (3.11 tested) | the control panel | ~30 MB | `winget install --id Python.Python.3.11 --exact` (skip if `python --version` works) |
| NVIDIA driver | GPU model (optional, much faster) | - | from nvidia.com / GeForce Experience |
| VirtualBox + Vagrant | optional VM tier only | ~100 MB + ~250 MB | `winget install Oracle.VirtualBox Hashicorp.Vagrant` |

Docker Desktop is free for personal use and small businesses; larger companies need a paid plan. Windows 11 Home is fine (WSL2, no Hyper-V needed); it has no Windows Sandbox, which this project doesn't use.

Installers ask for admin (UAC). Reboot if asked. Open Docker Desktop once, accept its terms, wait for "Engine running".

## Verify each piece
```powershell
docker --version ; docker compose version
docker info --format "{{json .Runtimes}}"        # contains "nvidia" => GPU model works
python --version                                  # 3.8+
nvidia-smi                                        # shows the GPU (optional)
```
Or run the doctor: `python .claude/skills/aiagentplayground/scripts/doctor.py`.

## Run
```powershell
cd platforms\windows
.\run.ps1                       # opens http://127.0.0.1:8765
.\run.ps1 -NoBrowser            # no browser
powershell -ExecutionPolicy Bypass -File .\run.ps1    # if scripts are blocked
```
Settings: `platforms\windows\config.env` (`OLLAMA_MODE` auto/nvidia/cpu, `OLLAMA_NUM_PARALLEL`, `PANEL_PORT`).

## Windows-specific notes
- Keep the project under your user folder (e.g. `C:\Users\<you>\aiagentplayground`). Docker failed to bind-mount files from a temporary `AppData\Roaming` location.
- Agent workspaces are Docker volumes, not folders: Windows bind mounts break OpenClaw's atomic file renames. Copy files out with `docker cp aiagentplayground-i-<name>-gateway-1:/home/node/.openclaw/workspace .`
- API keys are encrypted with DPAPI and tied to the Windows login; reinstalling Windows means re-entering them.
- A fresh PowerShell/terminal may need to be reopened after installs so PATH refreshes (the launcher refreshes it itself).

## Disk and first-run download
~25 GB: OpenClaw image ~5 GB, Ollama image ~9 GB, `qwen3:14b` ~9 GB. Docker keeps it all inside one growing disk image (`%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx`); it doesn't shrink on its own.

## Uninstall (ask before doing any of this)
Delete agents in the panel first (removes their volumes). Then `winget uninstall Docker.DockerDesktop` etc. The model volume and images go with Docker's disk image.
