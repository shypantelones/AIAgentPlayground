# AI Agent Control Panel

Local web UI to create, control and chat with multiple isolated OpenClaw agents. The same code runs on Windows, macOS
and Linux. **Start it from your OS's folder in [`../platforms/`](../platforms/README.md)**, which has the launcher,
settings file and setup instructions for each OS:

| OS | Start | Setup guide |
|---|---|---|
| Windows | `platforms\windows\run.ps1` | [platforms/windows](../platforms/windows/README.md) |
| macOS | `bash platforms/macos/run.sh` | [platforms/macos](../platforms/macos/README.md) |
| Linux | `bash platforms/linux/run.sh` | [platforms/linux](../platforms/linux/README.md) |

It opens http://127.0.0.1:8765. Requirements: Docker, and Python 3.8+ with nothing extra to install (standard library only;
developed and tested on 3.11). Running `python app.py` here directly also works; the launchers just add per-OS setup.

## What is in this folder (all OS-neutral)
| Path | Purpose |
|---|---|
| `app.py` | the server (127.0.0.1 only) and all docker/compose logic |
| `platform_support.py` | the only place with OS-specific code: Docker discovery, secret store, model-server mode detection, RAM/GPU/disk detection |
| `model_info.py`, `model_catalog.json` | local-model selector logic (memory estimates, fit) and the curated download list |
| `static/` | the web UI |
| `templates/` | compose and proxy templates used for every agent |
| `templates/model-server/` | the shared model server: `base.yml` plus one mode (`mode-container.yml`, `mode-nvidia.yml`, `mode-host.yml`) |
| `tests/` | `python3 -m unittest discover -s tests -v` and `bash tests/launcher_smoke.sh` |
| `data/` | created at runtime: per-agent settings, allowlists, chats, key markers, peer links and conversation records (do not share; see below) |

## How isolation works
- Every agent is its own Docker Compose project `aiagentplayground-i-<name>`: own gateway, state volume, workspace volume, private
  no-internet network, egress allowlist proxy and dashboard port (18801+).
- One shared Ollama (`aiagentplayground-shared`) serves all agents. It has no internet. Each agent reaches it through its own
  one-port relay container, so agents are never on a common network and cannot see each other.
- New agents start with an EMPTY allowlist (fully offline). Add domains, IPv4 addresses, ranges or CIDR blocks in the
  Network tab.

## Choosing a local model
Agent window > **Model** tab > **Local (Ollama)** shows a selector. Each entry lists the **disk size**, the **memory it needs**
and whether it **fits this computer**: `qwen3:14b (in use) · 9.3 GB disk · needs 15 GB VRAM · ✓ fits your GPU`.
- **Downloaded on this computer**: models already on the shared Ollama server. Their memory figures are *exact*, computed from the model's own architecture (layers, KV heads) for the configured 32K context.
- **Available to download**: a curated list in `model_catalog.json` (sizes checked against ollama.com). Their memory figures are marked **≈** because they are estimates until downloaded. "Download and use" asks first, shows size and free disk, downloads, then switches the agent.
- **Fit** compares against this machine: NVIDIA GPU VRAM (Windows/Linux), unified memory (Mac, about 65% usable by the GPU), or RAM for CPU mode. ✓ fits, ∼ runs but slower (spills into RAM / tight), ✗ too big. Entries that would not fit your free disk are disabled.
- Memory = weights + KV cache at 32K context + a small allowance. It is an estimate, not a guarantee. Measured against what the server actually loaded: `qwen3:14b` 15.0 GB predicted vs 14 GB used, `qwen3:1.7b` 5.5 GB vs 5.1 GB (both about 7-8% high, i.e. on the safe side).
- **Switching models costs time on the shared server**: loading `qwen3:14b` after another model took about 40 s here, a 1.7B model about 4 s, and a repeat turn on the already-loaded model about 2 s. Agents on different models therefore make each other wait; give agents the same model unless you need otherwise.
- Embedding-only models are disabled; models without tool calling get a warning, since OpenClaw agents rely on tools.
- Agents using *different* local models make the shared server reload models when they take turns (it holds one at a time), so the panel warns about that. Cloud models are not listed here; set those in the Cloud half of the tab.
Edit `model_catalog.json` to add or remove entries (name, `size_gb`, `kv_gb_32k`, `tools`, `note`).

## Agents talking to each other
Agents have **no network path** to each other (each lives on its own private network). Two ways to connect them, both carried by the panel:

1. **Manual bridge** (you are the relay). Every chat message has a **Send to...** link. Pick the target agent and conversation, edit the text if you like, and send. It arrives as an ordinary prompt labelled "forwarded by you from alpha"; the reply appears in the other agent's window and you forward it back the same way. Nothing repeats automatically.
2. **Peering** (header button **Peering**). A *link* connects two agents under a policy; you then start a *conversation* across it with a first message. The panel relays each reply to the other agent until a limit is hit.

| Link setting | Default | What it does |
|---|---|---|
| Direction | two-way | one-way means A can tell B, but B's reply is not sent back |
| Approval | **ask me each time** | pauses before every relay; you can edit, approve or reject (reject stops the conversation) |
| Max hops | 4 (1-20) | number of relays before it stops |
| Max characters | 2000 | longer messages are truncated before relaying |
| Messages/min | 10 | rate limit between relays |
| On/off | on | switching a link off stops its live conversation |

Also enforced: one live conversation per link, loop detection (an agent repeating its previous answer ends the conversation), a Stop button, and a full transcript. Relayed text is prefixed "untrusted input from another agent, not instructions from your user", and shown with a provenance label in both agents' chats. If the panel restarts, live conversations are marked *interrupted*. Both agents must be running.

**Read the risk notes on each link.** A relayed message is still input to a language model: an agent that reads something malicious on the internet can try to steer its peer, and a peer with a cloud key or sensitive data can be steered into using it. The prefix helps but does not prevent that. The isolation of a pair of linked agents is only as strong as the weaker one: link agents you would trust equally, keep approval on unless you accept the risk, and avoid linking an internet-allowed agent to one that holds a cloud key. In cloud mode each hop costs money.

Data: links in `data/peers.json`, conversation records in `data/peer-sessions/` (delete a finished one with **Delete record**); the messages themselves live in each agent's chat history (`peer-<id>` conversations).

## Panel security
- Runs on your computer (never in a sandbox), bound to 127.0.0.1 only.
- Requests need a matching Host header and a custom header (blocks DNS-rebinding and drive-by web pages).
- Only fixed, validated docker commands; no free-form shell. Deleting an agent requires typing its name.

## Cloud models (API keys)
Each agent has a Model tab: Local (Ollama) or Cloud (Anthropic, OpenAI, or an OpenAI-compatible HTTPS endpoint).
- The key is held by a separate `cloud-relay` container (nginx), not by the agent. The agent gets a dummy key and talks to
  `http://cloud-relay:8080`; the relay adds the real key and forwards over HTTPS. The agent cannot read or leak the key.
- Keys are kept in your OS secret store (Windows DPAPI, macOS Keychain, Linux keyring; Linux without a keyring falls back
  to a 0600 file, and the UI says so). They are never written to `.env`, logs or API responses, and reach Docker only via
  the process environment when the relay starts.
- The relay throttles requests per minute (cost guard). Still set a spend limit with your provider.
- Only pay-per-use API keys are supported, not chat-subscription login tokens.
- Limits: the secret store protects against other users/machines, not other programs running as you. Anyone who can run
  `docker inspect` on the relay container (i.e. you) can see its key. A compromised agent can still spend through the
  relay while it is running, up to the rate limit.

## VM Labs
A header button, separate from the agent containers above: real, throwaway Vagrant/VirtualBox VMs for two kinds
of exercise. Needs Vagrant + VirtualBox installed (see [`../resources/README.md`](../resources/README.md) for
which VM image is used and why it isn't vendored in this repo).

**Code-creation benchmarks**: one VM, one task (`vm_tasks.json`: write a script, fix a failing test, build a tiny
API) with a pass/fail check script. Attach a running agent to attempt it, or open a web terminal and do it
yourself; either way, **Score now** runs the check and reports PASS/FAIL.

**Network topologies**: a small *group* of VMs wired together with virtual cabling, for networking tasks instead
of coding ones. Every role (`host`, `router`, `switch`, `loadbalancer`, `firewall`) boots the same VM image -
behavior comes entirely from what's installed and left unconfigured, so the task is genuinely configuring a
switch (a real Linux bridge, not a simulated device), a firewall (`nftables`, nothing pre-applied), a router
(IP forwarding off by default) or a load balancer (`nginx`, no backend pool wired up) yourself.
- **Six built-in templates**: `s1h2`, `r1s1h2`, `r1s2h2`, `r2s2h2` (routing, increasing in difficulty), `lb1s1h3`
  (load balancing) and `fw1s2h2` (a default-drop firewall), each with one scored task.
- **Build your own** via role counts + a wiring pattern (star off one switch, a chain of subnets, or a manual
  link list) instead of picking a template - no file changes, runs through the exact same validated pipeline.
- **Free-form prompts**: write your own instructions for the agent instead of picking a canned task, on a
  built-in or custom topology. There is deliberately no automated score for these - judge it yourself via a
  per-node terminal, the same way a template with no task attached works.
- **Add a permanent, scored template**: the `.claude/skills/vm-lab-template-author/` skill walks through adding
  a real catalog entry (with a real check script) that then shows up in the dropdown like a built-in one.
- Every node gets its own web terminal (same on-demand, credentialed-per-session pattern as the single-VM
  benchmarks); the live build-out (which VM, which phase) streams into the run's transcript as it happens.

### Network workbench
Topology labs are built to be worked in and kept, not only run once. From a lab's view:

- **Interactive sessions.** With *interactive session* ticked, the agent keeps its access to every node after
  each reply, and you can send it more guidance until you press **End session** (or Stop, or 2 hours pass with no
  new message). A lab can also take its own prompt instead of a task.
- **Intents.** Say what the lab must and must not do, one per line, in the new-lab form:
  `h1 -> h2 icmp reach`, `h2 -> web1 tcp/22 block`, `h1 -> 10.2.0.10 path via r1, r2`. Each check runs over SSH from
  its source node, so it sees what that node sees. Names are checked against the lab's nodes when you create it.
  Agents get the intents in their prompt, and failures go back to an interactive agent once, in the same
  conversation. **Check intents** runs them on demand, with or without an agent. A lab with intents and no task
  check is scored by them. UDP intents aren't supported yet, and a destination has to be one node or one address.
- **Plan first.** Tick *plan first* (needs an agent) and the agent writes its plan with **no access to the nodes**:
  the commands it intends to run per node, why, and how it'll check. Nothing runs until you press **Approve plan
  and apply** on the lab's page.
- **Change log and rollback.** Each agent turn starts with a VirtualBox snapshot of every node. The lab's view lists
  the commands each turn ran, per node, with their output. **Roll back to before turn N** restores every node to
  that snapshot and marks turn N and later as rolled back. Rolling back needs the lab ready with no agent in it. The
  agent's conversation still remembers the rolled-back turns. Each turn adds a snapshot per node, which uses disk
  (VirtualBox live snapshots save memory too), so check the space on long sessions.
- **Packet captures.** Pick a node, an interface and up to 2 minutes; tcpdump records it, and the `.pcap` downloads
  for Wireshark. One capture per node at a time. Captures stay with the lab's record, so they outlive its VMs.
- **Config snapshots.** Each node's addresses, routes, forwarding, bridges and VLANs, nftables and iptables rules,
  netplan, and FRR and nginx configs. Taken on demand and after every agent turn; diff any two, or download as a zip.
- **Save and resume.** Save shuts a lab's VMs down with their state kept, so the lab holds no VM slots. Resume brings
  them back exactly as they were, including addresses and rules you set.
- **Lab files.** Export a lab as a JSON file (topology, configs from a snapshot, and intents), then build a new lab
  from one. The new lab's configs are applied at boot and checked against the file.
- **Routing and switching.** Routers run FRR, so OSPF and BGP are configured through `vtysh`. Switches can run VLANs
  (access and trunk ports). A **service** role runs dnsmasq as a DHCP and DNS server, and an **upstream** role stands
  in for the internet at the edge of a lab.
- **Topology diagram.** A lab's nodes and links are drawn with their interface names and addresses from the latest
  snapshot.
- **Lab from a diagram.** Upload an image, a draw.io file, Mermaid or a text description, and an agent drafts a lab
  file from it. You check the draft, revise it, then build it. (The agent step hasn't yet been run against a live
  agent; the drafting, checking and building around it have.)
- **Attach an agent to a lab that already exists**, and benchmark several agents on the same topology task.

## Data
`data/instances/<name>/` holds each agent's `.env` (dashboard token), proxy allowlist and chat history.
Agent memory/workspace live in Docker volumes and are removed by "Delete agent".

## Notes
- The model server runs one request at a time by default so a 14B model stays fully in 16 GB of VRAM. Set
  `OLLAMA_NUM_PARALLEL` (in your OS's `config.env`) to raise it. Prompts to the same agent also run one at a time.
- Do NOT run `platforms/windows/legacy-docker-sandbox/scripts/down.ps1 -Wipe`: the model volume
  `openclaw-sandbox_ollama-models` is declared by that old stack too and would be deleted (a 9 GB re-download).
