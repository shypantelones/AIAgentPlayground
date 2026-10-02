---
name: aiagentplayground
description: Install, run, monitor and troubleshoot the AI Agent Playground - a local control panel that runs OpenClaw AI agents inside isolated Docker containers (with a local Ollama model or a cloud API key) on Windows, macOS or Linux. Use this skill whenever the user mentions OpenClaw, the control panel, the aiagentplayground folder, sandboxed or isolated agents, the egress allowlist, running Ollama or a local model for agents, "is it running", "where is X installed/stored", Docker Desktop / VirtualBox / Vagrant setup for this project, agent dashboards or ports 8765/18801, the cloud-relay or API-key toggle, or any error from this app - even if they don't name the skill. Also use it when someone new wants to get the playground working on their machine.
---

# AI Agent Playground

A local web control panel (`control-panel/`) that creates and manages multiple **isolated OpenClaw agents**. Each agent runs in its own Docker containers with no internet except a domain/IP allowlist you control, and uses either a local Ollama model or a cloud API key that the agent itself can never read (a relay container holds the key). Everything binds to `127.0.0.1`.

The point of this skill is to get a person from "nothing installed" to "chatting with a sandboxed agent" with as little friction as possible, and to keep it healthy afterwards. Be the patient guide: detect their situation first, then do only the steps that apply.

## Step 0: Find out where they are (always do this first)

Run the health check. It is read-only, prints no secrets, and works on all three OSes:

```bash
python .claude/skills/aiagentplayground/scripts/doctor.py          # add --sizes or --blocked for more
```
(On Windows use `python` or `py`; on macOS/Linux `python3`.) If the project folder isn't the working directory, set `AIAGENTPLAYGROUND` to it. Read the `[WARN]`/`[FAIL]` lines: each carries the fix. Then jump to the matching section below instead of walking through steps that are already done.

## Step 1: Install (only what's missing)

The OS table lives in `platforms/README.md`; the repo's `platforms/<os>/README.md` is the source of truth for steps. Quick reference sheets with exact commands, sizes and checks:

| OS | Reference | Status of testing |
|---|---|---|
| Windows | `references/install-windows.md` | fully tested |
| macOS | `references/install-macos.md` | NOT tested on a Mac - say so, and expect small fixes |
| Linux | `references/install-linux.md` | code tested on Linux; Docker side not on a native Linux host |

Plan for disk: a first run downloads about **25 GB** (OpenClaw image ~5 GB, Ollama image ~9 GB, a 14B model ~9 GB). Recommend at least 40 GB free; the doctor warns below 15 GB.

**Installing software downloads files and may need admin rights.** Tell the user what, from where, and roughly how big, and get a clear yes before running installers. They must click UAC/sudo prompts and accept Docker's license themselves. Don't accept licenses for them.

## Step 2: Run the panel

Launchers live in each OS folder (they load that folder's `config.env`, where the model-server mode and port are set):

- Windows: `platforms\windows\run.ps1` (if scripts are blocked: `powershell -ExecutionPolicy Bypass -File ...`)
- macOS: `bash platforms/macos/run.sh`   Linux: `bash platforms/linux/run.sh`

It serves http://127.0.0.1:8765. Stopping the panel (Ctrl+C) does **not** stop the agents; they keep running in Docker until stopped from the panel or Docker.

## Step 3: First agent (the happy path)

1. In the page's top bar use **Download model** (16 GB GPU: `qwen3:14b`; smaller machines: a 4B-8B model). One-time, ~9 GB for 14B.
2. Type a name in **Create** (lowercase letters/digits/dashes), wait for the job tray to finish (first time pulls images).
3. Tick the agent in the sidebar to open its window, type in **Chat**, send with Ctrl+Enter.
4. **Security tab -> Verify isolation**: all checks should pass.

An agent starts **fully offline**. Add what it may reach in the **Network** tab. Entries, one per line: domains (`example.com`, `.example.com` includes subdomains), IPv4 (`203.0.113.7`), ranges (`203.0.113.10-203.0.113.20`), CIDR (`203.0.113.0/24`). `0.0.0.0/0` is refused; private/LAN ranges save with a warning because they weaken isolation.

## Daily use

- **Agent windows** (tick several to see them side by side): Chat, Model, Network, Logs, Security, Info. Start/Stop/Restart buttons sit in each window header.
- **Choosing a local model**: Model tab > Local shows a selector of downloaded models plus a curated download list, each with disk
  size, memory needed and a fit verdict for *this* machine (exact numbers for downloaded models, "≈" estimates otherwise; "Download and
  use" asks first and shows free disk). Help the user pick by their hardware: ~16 GB GPU -> 14B class, 8-12 GB -> 8B, laptops/CPU -> 4B;
  Macs use unified memory. Mixing different models across agents makes the shared server reload models between prompts (slow).
  The list lives in `control-panel/model_catalog.json`.
- **Model tab**: toggle **Local (Ollama)** or **Cloud (API key)**. For cloud, the user pastes the key into the panel's password box themselves. Never ask them to paste a key into chat, never type one for them, and never print one. The key is stored in the OS secret store and held by a relay container; the agent only sees a dummy.
- **Agents talking to each other** (agents have no network path to each other; the panel carries messages):
  *Manual bridge*: **Send to...** under any chat message forwards it (editable) to another agent; nothing repeats on its own.
  *Peering*: header button **Peering** > create a link between two agents (direction, hops, max characters, rate, and
  **ask-me-each-time approval, on by default**) > start a conversation. The panel relays replies under those limits with
  loop detection, a Stop button and a transcript. Relayed text is untrusted input to the receiver, so each link shows risk
  notes: advise keeping approval on, linking only equally trusted agents, and never linking an internet-allowed agent to one
  holding a cloud key. If a conversation shows "waiting for you", the button reads "Peering (1 waiting)".
- **Dashboard & token**: each agent has its own OpenClaw dashboard at `http://127.0.0.1:<port>/` (ports from 18801); the login token is in the Info tab.
- **After a reboot**: containers don't auto-start. Start Docker, run the launcher, press Start on each agent.
- **Back up** `control-panel/data/` (settings, allowlists, chats); agent memory lives in Docker volumes.

## Monitoring

Run the doctor for the whole picture. For specific questions use `references/monitoring.md` (what healthy looks like, GPU/disk/cost watching, reading the Logs tab, blocked-request triage, raw docker commands). Quick signals:

- Agent status `healthy` + 4/4 containers running = good. `starting` for >1 min or a container `Exited (1)` = look at the Logs tab.
- Proxy log lines `TCP_DENIED` = the allowlist doing its job (OpenClaw tries github.com, chatgpt.com, telemetry.openclaw.ai on startup; that's expected).
- `ollama ps` showing a CPU split means the model doesn't fit the GPU: use a smaller model or keep `OLLAMA_NUM_PARALLEL=1`.

## Where things are installed / stored

Full table with per-OS paths, Docker object names, ports, secrets and how to remove things: `references/locations.md`. The short version: code and per-agent data in the project folder; models and agent memory in Docker volumes; Docker's own data in its disk image; API keys in the OS secret store.

## Troubleshooting

`references/troubleshooting.md` has symptom -> cause -> fix for every issue hit while building this (Docker banner, bind-mount errors, slow model, 401/429 from cloud, port conflicts, blocked sites, Mac/Linux quirks). Run the doctor first; it usually names the problem.

## Rules for Claude when helping with this project

- **Panel first, raw Docker second.** The panel validates and tracks everything; use docker commands for read-only inspection, or when the panel can't (it's down). Don't hand-edit files under `control-panel/data/instances/`.
- **Be careful with destructive actions.** `docker compose down -v`, `docker volume rm`, `docker system prune`, deleting an agent, and the legacy `down.ps1 -Wipe` can destroy agent memory or the 9 GB model volume `openclaw-sandbox_ollama-models` (shared by every agent). See `references/locations.md` for exact names. Explain what would be lost and get explicit confirmation first.
- **Keep the isolation intact.** Never mount the Docker socket into a container, use host networking, widen an allowlist without the user asking, or put a key in `.env`/compose/config files. If something "needs" that, find another way or explain the trade-off.
- **Secrets stay out of the conversation**: don't echo tokens, key-store contents or dashboard login tokens in replies.
- **Be honest about test status.** Windows is verified; macOS is not; Linux is partly. Say so when it matters and ask the user to report what breaks.
- Prefer the doctor's JSON (`--json`) when you need to reason about state programmatically.
