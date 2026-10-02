# Troubleshooting (symptom -> cause -> fix)

Run the doctor first; it usually names the problem. Fixes below are ordered safest-first. Anything destructive needs the user's explicit OK.

## Contents
Startup & Docker - Agents & chat - Model & performance - Network & allowlist - Cloud mode - Per-OS quirks

## Startup & Docker
| Symptom | Cause | Fix |
|---|---|---|
| Red banner "Docker is not running" | Docker Desktop/daemon down | Start Docker, wait for "Engine running", reload the page |
| `docker` not recognised (new terminal) | PATH not refreshed | reopen the terminal or reboot (Windows launcher refreshes PATH itself) |
| Port 8765 in use / panel won't start | another program, or a second panel | change `PANEL_PORT` in `platforms/<os>/config.env`, or stop the other one |
| Panel reachable but a page says 403 | accessed via another hostname/IP | by design (blocks DNS-rebinding). Use `http://127.0.0.1:8765` or `localhost`; for remote use an SSH tunnel |
| Container creation fails with `not a directory ... mounting ".../squid.conf"` | project is in a folder Docker can't mount (e.g. `AppData\Roaming` temp location) | keep the project under your user folder (`C:\Users\<you>\...`, `~/...`) |
| An allowlist/relay file is a *directory* | Docker auto-creates a directory when a bind-mount source file doesn't exist | stop the agent, delete the empty directory, restart the panel (it regenerates the file), start the agent |
| After reboot nothing is running | containers use `restart: "no"` by design | start Docker, run the launcher, press Start on each agent |
| First start is slow | pulling ~5 GB image (+ ~9 GB Ollama image) | wait; watch the job tray |

## Agents & chat
| Symptom | Cause | Fix |
|---|---|---|
| Agent stuck `starting` | gateway still booting, or crashed | Logs tab > Agent gateway; Restart once; if it persists read the last lines |
| Gateway log: `Gateway start blocked: existing config is missing gateway.mode` | agent created but config patch didn't apply | Restart; else delete and re-create the agent |
| Gateway log: `Unable to create fallback OpenClaw temp dir` | needs writable cache dir on a read-only filesystem | already handled by the compose template (tmpfs `/home/node/.cache`); if seen on a custom edit, restore the template |
| Chat: `No API key found for provider "ollama"` | missing placeholder credential | template sets `OLLAMA_API_KEY: ollama-local`; restore it if edited |
| Chat shows `(no reply)` + a JSON blob | reply parsing didn't find text | check the Agent gateway log; usually the model errored or returned empty |
| Agent can't save files / `EINVAL rename` | a Windows bind-mounted workspace | workspaces are Docker volumes in this project; don't switch them to bind mounts |
| Agent forgets context | new conversation or deleted history | each conversation has its own session; use the same one |
| Two prompts to one agent are slow | prompts run one at a time per agent | expected |

## Model & performance
| Symptom | Cause | Fix |
|---|---|---|
| Replies very slow | model on CPU (`ollama ps` shows CPU%) | NVIDIA runtime missing (check `docker info` runtimes), model too big, or `OLLAMA_NUM_PARALLEL` > 1; use a smaller model, keep parallel = 1 |
| Top bar says "CPU-only container" on a GPU PC | NVIDIA runtime not detected | update NVIDIA driver, restart Docker Desktop, check `docker info --format "{{json .Runtimes}}"`; force with `OLLAMA_MODE=nvidia` |
| Model bar: "Ollama is not running on this computer" (macOS/host mode) | native Ollama off | start the Ollama app / `ollama serve`, or set `OLLAMA_MODE=cpu` |
| "Download model" fails | no internet for the pull, disk full, or typo in the tag | check disk (`doctor.py`), model name on ollama.com |
| Model unloads / first prompt slow | 30-minute idle unload | normal; first prompt reloads it |
| Selector says "memory ?" or no downloaded models listed | model server stopped, or the model's metadata couldn't be read | start the server (top bar); reopen the Model tab |
| "X is not downloaded yet" when switching | model isn't on the server | use "Download and use" in the selector, or the top-bar Download |
| Option greyed out in the selector | embedding-only model, or not enough free disk (needs size + 2 GB) | free disk space, or pick another model |
| Verdict says "spills into system RAM" / "too big" | model + 32K context exceeds VRAM (or RAM on CPU/Mac) | pick a smaller model; the figures are estimates (exact once downloaded) |
| Replies pause whenever two agents take turns | agents use different local models and the server holds one at a time | give agents the same model |

## Agent-to-agent (Send to... / Peering)
| Symptom | Cause | Fix |
|---|---|---|
| "X is not running; start it first" | both agents must be running to forward or start a conversation | press Start on the agent |
| "this link already has a live session" | one live conversation per link | Stop it (Peering > conversation > Stop), then start a new one |
| Conversation stuck at "waiting for you" | approval is on (the default) | open Peering, review the pending message, Approve (editable) or Reject; the button shows "(N waiting)" |
| Conversation ended "hop limit reached" / "one-way link" / "repetition detected" | the link's policy working as designed | raise max hops on the link, switch to two-way, or start a new conversation |
| State "interrupted" | the panel restarted while it was live | start a new conversation; the old transcript stays |
| "start with the sending agent" | one-way link: only the A side can start | pick agent A as the starter |
| Forwarded/relayed messages don't appear in a window | the window shows another conversation | use the conversation picker, or "Show in agent windows" in the Peering conversation view |
| Responses are slow or odd between agents | each hop is a full model reply (a few seconds), and small local models can drift or stall | lower max hops, keep prompts short, use approval |

## Network & allowlist
| Symptom | Cause | Fix |
|---|---|---|
| Agent says it can't reach a site | agents start fully offline | add the domain/IP in the Network tab; check the proxy log for `TCP_DENIED` |
| Save fails "not a domain, IPv4 address, range or CIDR block" | bad line | fix format: `example.com`, `.example.com`, `1.2.3.4`, `1.2.3.4-1.2.3.9`, `1.2.3.0/24`; no host bits in CIDR |
| Saved a LAN range and got a warning | private/local range weakens isolation | intended; remove it unless truly needed |
| Constant denied `github.com`, `chatgpt.com`, `telemetry.openclaw.ai` | OpenClaw probes on startup | expected; leave blocked |

## Cloud mode
| Symptom | Cause | Fix |
|---|---|---|
| Can't switch to cloud: "store an API key first" | no key stored | user pastes the key in the Model tab > Save key (do not take it in chat) |
| Chat error `Authentication failed ... 401` | key invalid/revoked or wrong provider | re-save the key; check provider/model name |
| HTTP 429 | relay throttle (requests/min) or provider limit | raise requests/min in the Model tab, or wait |
| Can't remove a key | agent still on Cloud | switch the agent back to Local first |
| Key store says "NOT encrypted" (Linux) | no keyring available | install `libsecret-tools` + a running keyring, or accept the 0600 file |
| macOS Keychain prompt | first access | allow it |

## Per-OS quirks
- **Windows:** PowerShell blocks scripts -> `powershell -ExecutionPolicy Bypass -File ...run.ps1`. VirtualBox next to WSL2 runs slower. `VBoxManage` isn't on PATH by default (`C:\Program Files\Oracle\VirtualBox`).
- **macOS (untested):** Docker needs file sharing for the project folder; Apple Silicon may emulate x86 images (slow); VM tier is Intel-only.
- **Linux (partly tested):** add the user to the `docker` group and re-login; SELinux may block bind mounts; `OLLAMA_MODE=host` exposes host Ollama to the LAN; headless: use an SSH tunnel.

## If all else fails
Collect: doctor output (`--json`), the failing agent's gateway log (last ~100 lines), `docker ps -a`, and the OS. Don't include dashboard tokens or keys. Last resort for one agent: delete and re-create it in the panel (loses that agent's memory, not the others, not the model).
