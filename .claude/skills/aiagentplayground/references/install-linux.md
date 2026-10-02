# Install on Linux (code tested on Linux; Docker side not run on a native Linux host)

The Python code, key-storage logic and launchers pass automated tests on real Debian (under WSL); the Docker/compose side was
built on Windows. Say so and ask the user to report issues. Repo source of truth: `platforms/linux/README.md`.

## What you need
| Tool | Needed for | Install |
|---|---|---|
| Docker Engine + compose plugin | everything | https://docs.docker.com/engine/install/ ; then `sudo usermod -aG docker "$USER"` and log out/in |
| Python 3.8+ | the control panel | usually preinstalled; `sudo apt install python3` |
| NVIDIA driver + NVIDIA Container Toolkit | GPU model (optional) | https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/ |
| `libsecret-tools` + a desktop keyring | encrypted API-key storage (optional) | `sudo apt install libsecret-tools` |
| VirtualBox + Vagrant | optional VM tier (x86) | your package manager / vendor sites |

Installing packages needs sudo: let the user run those commands, or get explicit approval first.

## Verify
```bash
docker --version && docker compose version
docker info >/dev/null && echo "daemon reachable"      # fails => daemon down or user not in 'docker' group
docker info --format '{{json .Runtimes}}'               # contains "nvidia" => GPU model works
python3 --version
```
Or: `python3 .claude/skills/aiagentplayground/scripts/doctor.py`.

## Run
```bash
cd platforms/linux
bash run.sh                      # opens http://127.0.0.1:8765 if a desktop exists
AIAGENTPLAYGROUND_NO_BROWSER=1 bash run.sh
```
Settings: `platforms/linux/config.env` (`OLLAMA_MODE` auto/nvidia/cpu/host, `OLLAMA_NUM_PARALLEL`, `PANEL_PORT`).
No NVIDIA GPU => CPU container: prefer small models.

**Headless server:** the panel only listens on 127.0.0.1. From your laptop:
`ssh -L 8765:127.0.0.1:8765 -L 18801:127.0.0.1:18801 you@server` (one `-L` per agent dashboard port), then browse locally.

## Linux-specific notes
- API keys: Secret Service keyring if `secret-tool` works, else a 0600 file that is **not encrypted** (the UI says which).
- `OLLAMA_MODE=host` needs the host Ollama to listen beyond 127.0.0.1 (`OLLAMA_HOST=0.0.0.0`): this exposes it to the LAN unless firewalled. Prefer `nvidia` or `cpu`.
- SELinux (Fedora/RHEL) may block the small bind-mounted config files; rootless Docker and Podman are untested.
- Agent workspaces are Docker volumes; `docker cp aiagentplayground-i-<name>-gateway-1:/home/node/.openclaw/workspace .` copies files out.

## Disk
~25 GB first run (OpenClaw image ~5 GB, Ollama image ~9 GB, a 14B model ~9 GB) under `/var/lib/docker`.

## Uninstall (ask first)
Delete agents in the panel; `docker image rm` the images listed in `locations.md`; remove Docker Engine with your package manager.
