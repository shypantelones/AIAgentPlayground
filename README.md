# AI Agent Playground

Run OpenClaw in isolated environments so it only gets the access you give it. Works on Windows, macOS and Linux.

## Quick start: pick your OS
| OS | Setup guide | Start the control panel | Status |
|---|---|---|---|
| **Windows** | [platforms/windows](platforms/windows/README.md) | `platforms\windows\run.ps1` | tested |
| **macOS** | [platforms/macos](platforms/macos/README.md) | `bash platforms/macos/run.sh` | tested |
| **Linux** | [platforms/linux](platforms/linux/README.md) | `bash platforms/linux/run.sh` | partly tested |

Each opens http://127.0.0.1:8765. The OS comparison table is in [platforms/README.md](platforms/README.md).

## Let Claude help: the `aiagentplayground` skill
`.claude/skills/aiagentplayground/` is a Claude skill that walks you (or anyone) through installing, running, monitoring and
troubleshooting this project, and knows where everything is stored. Open this folder in Claude Code and just ask
("set this up", "is it running?", "where are my models stored?"). It includes a health check you can also run yourself:

```
python .claude/skills/aiagentplayground/scripts/doctor.py        # add --sizes / --blocked / --json
```
`dist/aiagentplayground.skill` is the same skill as a single file for installing in Claude.ai or sharing.

## VM Labs
The control panel also has **VM Labs**: real, throwaway VMs (Vagrant/VirtualBox) for two kinds of exercises —
single-VM coding benchmarks, and multi-VM network-topology labs (routers, switches, hosts, load balancers,
firewalls, wired together and left unconfigured so an agent or a person has to actually do the networking). See
[control-panel/README.md](control-panel/README.md#vm-labs) for the full writeup, including the built-in topology
templates, the custom-topology builder, and how to add your own template.

Topology labs can also be worked in and kept: an agent or a person can check a lab against **intents** (reach, block
and path checks run from each node), write a **plan** that waits for approval before it touches anything, roll the
lab back to any earlier turn, record **packet captures** for Wireshark, save and resume, and export the lab as a
file. These are in the [Network workbench](control-panel/README.md#network-workbench) section.

## Layout
```
aiagentplayground/
├── README.md                    this file
├── control-panel/               the app: identical on every OS (web UI, compose templates, tests)
│   ├── app.py
│   ├── platform_support.py      the only OS-specific code (Docker discovery, key storage, model mode)
│   ├── static/  templates/  tests/
│   └── data/                    runtime data: agent settings, chats, key markers  (git-ignored)
├── platforms/                   EVERYTHING that differs per OS lives here
│   ├── README.md                comparison table and what is OS-specific
│   ├── windows/   run.ps1  config.env  README.md  legacy-docker-sandbox/
│   ├── macos/     run.sh   config.env  README.md
│   └── linux/     run.sh   config.env  README.md
├── vm-sandbox/                  optional stronger isolation: a VirtualBox VM via Vagrant (x86 only)
└── resources/                   what VM images VM Labs uses and why they aren't vendored in the repo
```

## Where things live
- Per-agent settings, proxy allowlists and chat history: `control-panel/data/instances/<name>/`.
- API keys: your OS secret store (Windows DPAPI / macOS Keychain / Linux keyring), with a marker file in `control-panel/data/secrets/`.
- Agent memory/workspaces and the downloaded model: Docker volumes (`aiagentplayground-i-<name>_state`, `openclaw-sandbox_ollama-models`).

## Rules of thumb
1. Use throwaway, spend-capped API keys; set a spend limit with your provider.
2. Keep allowlists minimal; watch the Logs tab for blocked requests.
3. Never run `platforms/windows/legacy-docker-sandbox/scripts/down.ps1 -Wipe`: it would delete the shared 9 GB model volume.
4. Never mount the Docker socket or use host networking for an agent; either defeats the sandbox.
5. Back up `control-panel/data` if you care about chat history; Docker volumes hold agent memory.
6. Do not share or commit `control-panel/data/` (it holds dashboard tokens and key markers); `.gitignore` already excludes it.

## Disclaimer
The isolation here (containers, private networks, allowlist proxy, key relay) is **best-effort defence in depth, not a security
guarantee**. Container isolation is not as strong as a VM, and software and configurations change. Don't use it to contain
anything you can't afford to lose, don't give agents credentials or data you can't replace, and review what each agent is
allowed to reach. The software is provided as-is, without warranty (see the license).

## License
[MIT](LICENSE) &copy; 2026 ShyPantelones. This project only orchestrates other software (OpenClaw, Ollama, Docker images and
more) that is downloaded at install/run time under its own license; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
Cloud model providers and downloaded language models have their own terms too.
