# macOS

**Status: NOT tested on a Mac.** Built and tested on Windows. The macOS-specific pieces were checked only indirectly:
the Keychain code against a fake `security` command, the launcher under bash on Linux, and the host-Ollama bridge
end to end on Windows with a stand-in Ollama. Expect to fix small things on a real Mac, and please report them.

## How the Mac differs
Docker on a Mac cannot use the Mac GPU. So instead of running the model in a container (CPU-only, slow), the Mac uses
**your native Ollama** (which uses Metal). A tiny bridge container forwards exactly one port (11434) from the isolated
agent network to the Ollama on your Mac; nothing else on the Mac is reachable from the agents. This is the
`host` mode, and it is the default in `config.env`.

## 1. Install
1. **Docker Desktop**: https://www.docker.com/products/docker-desktop/ (or `brew install --cask docker-desktop`; check
   Homebrew for the current cask name). OrbStack and Colima should also work (untested). Start it once.
2. **Python 3.8+**: `xcode-select --install` provides `python3`, or install from https://www.python.org/downloads/.
3. **Ollama**: https://ollama.com (the app), or `brew install ollama` then `ollama serve`. Leave it running.

Docker Desktop is free for personal use and small businesses; larger companies need a paid plan.

## 2. Run
```bash
cd platforms/macos
bash run.sh              # opens http://127.0.0.1:8765
```
The launcher warns you if Ollama is not running. In the page, use **Download model** (the panel asks your Ollama to pull
it). Choose a model that fits your Mac's memory: roughly an 8B-class model for 16 GB, a 14B-class model for 32 GB or
more (approximate; check the model's size on ollama.com). Then create an agent, tick it, and chat.

## 3. Settings: `config.env`
`OLLAMA_MODE` (`host` recommended; `cpu` runs a slow CPU-only container instead), `OLLAMA_NUM_PARALLEL`, `PANEL_PORT`.

## macOS specifics
- **API keys** go in the macOS Keychain (service `openclaw-panel`). macOS may show a Keychain permission prompt the first
  time; allow it. The key is passed to the `security` tool over stdin so it does not appear in a process list.
- **Docker file sharing:** the project folder must be somewhere Docker Desktop can mount. Your home folder is shared by
  default (Settings > Resources > File sharing).
- **Apple Silicon:** containers run as arm64 where the image publisher offers it. If the OpenClaw image has no arm64
  build, Docker emulates x86, which is slow. Not verified.
- **Intel Macs:** there is no unified memory and Ollama runs models on the CPU, so expect slower replies and pick
  smaller models (the panel's fit labels account for this). `host` mode is still the right choice.
- **VM tier (`vm-sandbox`)** uses VirtualBox with an x86 Ubuntu box: Intel Macs only. On Apple Silicon use another VM
  tool; this project does not provide that.
- **Agent workspaces are Docker volumes**, not folders. To copy files out:
  `docker cp openclaw-i-<name>-gateway-1:/home/node/.openclaw/workspace .`

## Troubleshooting
| Symptom | Fix |
|---|---|
| Banner "Docker is not running" | Start Docker Desktop and reload |
| Model bar says "Ollama is not running on this computer" | Start the Ollama app or run `ollama serve` |
| Start fails with "Ollama isn't running" | same as above |
| Want to avoid installing Ollama | set `OLLAMA_MODE=cpu` in `config.env` (slow) |
| Port 8765 in use | change `PANEL_PORT` in `config.env` |
| Agent says it cannot reach a site | by design: add the domain/IP in that agent's **Network** tab |
