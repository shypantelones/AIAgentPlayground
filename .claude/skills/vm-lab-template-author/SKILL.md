---
name: vm-lab-template-author
description: Turn a plain-language network scenario into a new, permanent, scoreable VM Lab topology template (a catalog entry in control-panel/vm_topologies.json plus a task in control-panel/vm_topology_tasks.json) — not code changes. Use when someone wants to add a new named network-lab template with real automated scoring, as opposed to the in-product "build a custom topology" builder (which is for one-off labs with no file changes and no automated score).
---

# VM Lab template authoring

This skill turns a description like *"I want a lab with two firewalls in an
active/passive pair in front of one web server"* into two new JSON catalog
entries that show up in the VM Labs UI exactly like the built-in templates
(`s1h2`, `r1s2h2`, `fw1s2h2`, ...) — including a real, automatically-scored
task. It does **not** touch any Python or JavaScript; everything happens in
`control-panel/vm_topologies.json` and `control-panel/vm_topology_tasks.json`.

**This is the permanent, reviewed path.** For a one-off lab with no commit
and no automated score, use the in-product "build a custom topology"
builder in the VM Labs UI instead (role counts + a wiring pattern) — see
`.claude/skills/vm-lab-dev/SKILL.md`'s custom-topology section. Reach for
*this* skill when the person wants the template to stick around and be
scoreable by anyone who picks it from the dropdown later.

## 1. Pin down the scenario

Ask (or infer from what was given) until you can state, concretely:
- **The roles and rough count**: which of `host`, `router`, `switch`,
  `loadbalancer`, `firewall` appear, and how many of each.
- **The shape**: how are they wired together — one flat LAN, a chain of
  subnets, something asymmetric? Sketch it as a list of pairs.
- **The goal**: what does success look like? (e.g. "host A can reach host
  B", "requests to the load balancer get a response", "traffic is blocked
  except for one allowed protocol"). This becomes the task's `check`.

## 2. Write the topology entry

Append one object to `topologies` in `control-panel/vm_topologies.json`:

```json
{
  "id": "<kebab-id>",
  "title": "<human-readable title, e.g. '2 firewalls, 1 switch, 1 host'>",
  "nodes": [
    {"name": "<name>", "role": "host|router|switch|loadbalancer|firewall"}
  ],
  "links": [
    {"a": "<name>", "b": "<name>"}
  ]
}
```

Rules (checked by `vm_runner.validate_topology` at load time — get these
right and the loader is your validator):
- `id` matches `^[a-z][a-z0-9-]{1,40}$` and isn't already used by another
  topology.
- Every `name` matches `^[a-z][a-z0-9]{0,14}$` and is unique within this
  topology. Follow the existing naming convention — `h` for host, `r` for
  router, `sw` for switch, `lb` for loadbalancer, `fw` for firewall,
  numbered from 1 (`h1`, `h2`, `sw1`, ...) — it's not enforced, but every
  built-in template uses it and the task prompts assume it.
- Every `role` is one of the five listed above (exactly those strings).
- Every link's `a`/`b` names an actual node, and `a != b` (no self-links).
- Keep it reasonably small — the built-in templates top out at 6 nodes;
  there's no hard limit on a catalog entry (unlike the in-product builder's
  `MAX_CUSTOM_NODES` cap), but every node is a real VM that has to boot.

A `switch` node needs no further thought — it's always a config-free L2
bridge. Every other role boots the same `ubuntu/jammy64` box with
role-specific tooling already installed (see `resources/README.md` and
`vm_runner._topo_provision_script`) — you're only placing it in the graph,
not deciding what's on it.

## 3. Write the task

Append one object to `tasks` in `control-panel/vm_topology_tasks.json`:

```json
{
  "id": "<kebab-id>",
  "title": "<short title>",
  "topology_id": "<the id from step 2>",
  "difficulty": "easy|medium|hard",
  "est_minutes": <int>,
  "prompt": "<the full instructions — see below>",
  "check_node": "<a node name from step 2>",
  "check": "<a bash one-or-few-liner; exit 0 = pass>"
}
```

**Writing the `prompt`** — look at the existing tasks in
`vm_topology_tasks.json` for the established voice, but the load-bearing
facts every prompt must include:
- The **exact addressing plan** (which node gets which IP/CIDR, which
  gateway) — don't make the agent guess at an acceptable scheme.
- That **every node's first network interface is NAT/setup-only and
  already has an address — never touch it**; the *other* interfaces are
  the lab links, with no address until configured (find them with
  `ip link`).
- That a `switch` node needs no configuration — it's already working.
- Any **non-addressing step** the scenario needs: enabling IP forwarding
  (`sysctl -w net.ipv4.ip_forward=1`) on a router/firewall, nftables rules
  on a firewall, nginx `stream`/`server` blocks on a loadbalancer, starting
  a backend service on a host, etc. — be as explicit as the existing tasks
  are (they spell out literal commands, not just goals).

**Writing the `check`** — same contract as every existing task: the panel
SSHes into `check_node` and runs `check`; exit code 0 is a pass. Keep it a
simple, deterministic proof of the goal (`ping -c3 -W2 <addr>`, `curl -fsS
-m3 <url> -o /dev/null`), the same style already used throughout. This is
the one piece of shell that will actually run on a real VM at scoring time,
so keep it narrow and obviously tied to the stated goal — this is reviewed,
committed code, not a runtime free-form script.

## 4. Validate and verify

From `control-panel/`:

```
python3 -c "import vm_runner as vr; vr.load_topologies(); vr.load_topology_tasks(); print('catalogs load cleanly')"
python3 -m unittest discover -s tests -v
```

The first command re-runs exactly the validation `app.py` runs on every
`GET /api/vmtopo` — any mistake from step 2 or 3 raises there with a clear
message. The second confirms the existing suite (which iterates every
catalog entry generically) still passes with your addition included.

Then, if you want to see it live: restart the control panel
(`platforms/<os>/run.sh`) and confirm the new template appears in VM Labs →
Network topologies' dropdown with the right task listed underneath it.
Creating a real run boots real VirtualBox VMs and takes a few minutes —
worth doing at least once before calling the template done, the same way
the built-in templates were boot-tested for real during their own
development (see `.claude/skills/vm-lab-dev/SKILL.md` for two real bugs
that real boot testing caught that the mocked test suite didn't).
