# Platforms

Everything that differs between operating systems lives in this folder, one subfolder per OS.
The control panel itself (`../control-panel/`) is the same on all of them.

| | [Windows](windows/README.md) | [macOS](macos/README.md) | [Linux](linux/README.md) |
|---|---|---|---|
| **Start it with** | `platforms\windows\run.ps1` | `bash platforms/macos/run.sh` | `bash platforms/linux/run.sh` |
| **Settings file** | `windows/config.env` | `macos/config.env` | `linux/config.env` |
| **Docker** | Docker Desktop (WSL2) | Docker Desktop (or OrbStack/Colima) | Docker Engine + compose plugin |
| **Local model runs** | NVIDIA GPU container (CPU container if no GPU) | Ollama installed on the Mac (uses the Mac GPU) | NVIDIA GPU container (CPU container if no GPU) |
| **API keys stored in** | Windows DPAPI (encrypted to your login) | macOS Keychain | Secret Service keyring, else a 0600 file (not encrypted) |
| **VM tier (`vm-sandbox`)** | VirtualBox + Vagrant | Intel only; Apple Silicon needs another VM tool | VirtualBox + Vagrant (x86) |
| **Tested?** | **Yes**, this is where it was built | **No**, not on a real Mac | Partly: code on real Linux, Docker side not on a native Linux host |

## What is OS-specific, and where it lives

| Thing | Where |
|---|---|
| Launcher script | `<os>/run.ps1` or `<os>/run.sh` |
| Per-OS defaults (model-server mode, ports) | `<os>/config.env` (edit this, or set the variable in your environment, which wins) |
| Setup instructions, gotchas, troubleshooting | `<os>/README.md` |
| Secret-store and Docker-discovery code | `../control-panel/platform_support.py` (picks the right behaviour automatically) |
| Model-server container setups | `../control-panel/templates/model-server/` (`mode-container.yml` CPU, `mode-nvidia.yml` GPU overlay, `mode-host.yml` bridge to host Ollama; `config.env` picks one) |
| Windows-only legacy single-agent sandbox | `windows/legacy-docker-sandbox/` |

Everything else (the web UI, per-agent compose templates, proxy and relay configs, tests) is shared and OS-neutral.

## Choosing the model-server mode
`OLLAMA_MODE` in your OS's `config.env` (or in your environment):

| Value | Meaning | Typical OS |
|---|---|---|
| `auto` | pick for me: macOS -> `host`; otherwise `nvidia` if Docker has the NVIDIA runtime, else `cpu` | any |
| `nvidia` | Ollama in a container with the NVIDIA GPU | Windows, Linux |
| `cpu` | Ollama in a CPU-only container | any (slow) |
| `host` | a one-port bridge to an Ollama installed on this computer | macOS |
