# Install on macOS (NOT tested on a Mac)

The macOS pieces were checked only indirectly (Keychain code against a fake `security` command, launcher under bash on Linux,
the host-Ollama bridge end to end on Windows with a stand-in). Tell the user this up front and ask them to report breakage.
Repo source of truth: `platforms/macos/README.md`.

## Why the Mac differs
Docker on a Mac can't use the Mac GPU. So the Mac uses **your native Ollama** (Metal GPU) through a tiny one-port bridge
container (`OLLAMA_MODE=host`, the default in `platforms/macos/config.env`). Nothing else on the Mac is reachable by agents.

## What you need
| Tool | Needed for | Install |
|---|---|---|
| Docker Desktop (or OrbStack/Colima, untested) | everything | https://www.docker.com/products/docker-desktop/ or `brew install --cask docker-desktop` (check Homebrew for the current cask name) |
| Python 3.8+ | the control panel | `xcode-select --install` provides `python3`, or python.org |
| Ollama | the local model | https://ollama.com app, or `brew install ollama` then `ollama serve`; leave it running |
| VirtualBox + Vagrant | optional VM tier | Intel Macs only (x86 box); not for Apple Silicon |

Docker Desktop is free for personal use and small businesses. Installers need the user's password/approval.

## Verify
```bash
docker --version && docker compose version
python3 --version
curl -fsS http://127.0.0.1:11434/api/tags        # Ollama answering = host mode ready
```
Or: `python3 .claude/skills/aiagentplayground/scripts/doctor.py`.

## Run
```bash
cd platforms/macos
bash run.sh                      # opens http://127.0.0.1:8765; warns if Ollama isn't running
AIAGENTPLAYGROUND_NO_BROWSER=1 bash run.sh
```
In the page, **Download model** asks the native Ollama to pull it. Rough sizing (approximate; check ollama.com): ~8B model for
16 GB RAM, ~14B for 32 GB+. No Ollama? Set `OLLAMA_MODE=cpu` in `config.env` (slow CPU-only container).

## macOS-specific notes
- API keys go in the macOS Keychain (service `aiagentplayground-panel`); a Keychain prompt may appear the first time: allow it.
- Docker Desktop must be allowed to mount the project folder (home folder is shared by default: Settings > Resources > File sharing).
- Apple Silicon: images run as arm64 where published; if the OpenClaw image has no arm64 build Docker emulates x86 (slow). Unverified.
- Agent workspaces are Docker volumes; copy files out with `docker cp aiagentplayground-i-<name>-gateway-1:/home/node/.openclaw/workspace .`

## Disk
In host mode Docker only needs about 5.5 GB of images (OpenClaw ~5 GB plus small helpers); the Ollama container image isn't used. The model itself lives in `~/.ollama/models`, sized by the model you pick.

## Uninstall (ask first)
Delete agents in the panel, quit/remove Docker Desktop; remove models with `ollama rm <model>` or delete `~/.ollama`; Keychain entries: `security delete-generic-password -s aiagentplayground-panel -a <agent>`.
