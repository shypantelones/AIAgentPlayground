# Linux

**Status: partly tested.** The Python code, key-storage logic and both launchers pass automated tests on real Linux
(Debian). The Docker side has not been run on a native Linux Docker host (it was built on Windows). Please report issues.

## 1. Install
1. **Docker Engine + the compose plugin**: https://docs.docker.com/engine/install/ . Then let your user run Docker
   without sudo and log out/in:
   ```bash
   sudo usermod -aG docker "$USER"
   ```
2. **Python 3.8+** (usually preinstalled): `sudo apt install python3` (or your distro's equivalent).
3. **Optional, NVIDIA GPU:** install the NVIDIA driver and the NVIDIA Container Toolkit
   (https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/), then check:
   ```bash
   docker info --format '{{json .Runtimes}}'      # should list "nvidia"
   ```
4. **Optional, encrypted key storage:** `sudo apt install libsecret-tools` and have a desktop keyring running
   (GNOME Keyring / KWallet via Secret Service). Without it the panel stores keys in a 0600 file and says so in the UI.

## 2. Run
```bash
cd platforms/linux
bash run.sh              # opens http://127.0.0.1:8765 if a desktop is available
```
In the page: **Download model**, create an agent, tick it, chat. No NVIDIA GPU means the model runs on the CPU:
prefer small models.

**Headless server?** The panel only listens on 127.0.0.1. Tunnel it from your laptop:
`ssh -L 8765:127.0.0.1:8765 -L 18801:127.0.0.1:18801 you@server`, then browse http://127.0.0.1:8765 locally
(add one `-L` per agent dashboard port you want).

## 3. Settings: `config.env`
`OLLAMA_MODE` (`auto`/`nvidia`/`cpu`/`host`), `OLLAMA_NUM_PARALLEL`, `PANEL_PORT`.

## Linux specifics
- **API keys:** Secret Service keyring if `secret-tool` works, otherwise a file readable only by your user (**not
  encrypted**). The UI always tells you which.
- **`host` mode** (bridge to a native Ollama) needs that Ollama to listen beyond 127.0.0.1 (`OLLAMA_HOST=0.0.0.0`), which
  exposes it to your network unless firewalled. Prefer `nvidia` or `cpu`.
- **SELinux (Fedora/RHEL):** the panel bind-mounts a few small config files into containers. If a container cannot read
  them, SELinux labelling is the likely cause. Not tested.
- **Rootless Docker / Podman:** not tested.
- **Agent workspaces are Docker volumes**, not folders. To copy files out:
  `docker cp openclaw-i-<name>-gateway-1:/home/node/.openclaw/workspace .`
- **VM tier (`vm-sandbox`):** VirtualBox + Vagrant on x86 (untested).

## Troubleshooting
| Symptom | Fix |
|---|---|
| Launcher warns "cannot talk to Docker" | start the daemon (`sudo systemctl start docker`) and check your user is in the `docker` group |
| Panel banner "Docker is not running" | same as above, then reload |
| Model is slow / shows "CPU-only container" | NVIDIA runtime not detected: install the Container Toolkit, restart Docker |
| Port 8765 in use | change `PANEL_PORT` in `config.env` |
| Agent says it cannot reach a site | by design: add the domain/IP in that agent's **Network** tab |
