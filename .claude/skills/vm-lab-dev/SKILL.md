---
name: vm-lab-dev
description: Reference for the VM Lab feature in control-panel/ — the single-VM coding benchmarks (vm_runner.py, app.py's vmbench routes, static/vmbench.js), the multi-VM networking-topology labs (TOPO_RUNS, vm_topologies.json, static/vmtopo.js), and the custom-topology builder + free-form agent prompts. Covers the Vagrant/VirtualBox VM lifecycle, port/naming conventions, resource/slot accounting, and agent-attach mechanics. Load before adding VM Lab routes, tasks, UI, roles, or topology templates.
---

# VM Lab development reference

The VM Lab feature spins up throwaway Vagrant/VirtualBox Linux VMs for AI
agents (or a human, via a web terminal) to work coding or networking tasks
in — either one VM for a coding task ("vmbench") or a small group of VMs
wired together for a networking task ("vmtopo": routers/switches/hosts).
This skill is a map of both, so a new feature matches the project's
conventions instead of inventing parallel ones.

Read the actual code before editing — line numbers below are a starting
point, not a substitute. Re-grep if the file has moved on since this was
written.

## Mental model

One "run" = one throwaway VM (or, for a topology, one group of VMs) tied to
an optional task and an optional agent:

```
queued -> provisioning -> ready -> working -> scoring -> done
                              \-> (no agent: stays "ready" until user acts)
error / stopped / interrupted are the off-ramps at any point
```

- No agent attached: the VM stays `ready` indefinitely; a human drives it via
  the on-demand web terminal and triggers scoring manually ("Score now").
- Agent attached: the runner thread drives the whole lifecycle itself,
  including scoring, then tears the VM down unless `keep` is set.
- `interrupted`: set on panel restart for any run that was mid-flight
  (`load_vm_runs`, app.py) — the panel never silently resumes a live VM op.

## Backend (control-panel/app.py)

Section banner: `# ---- VM benchmarks (code-creation tasks in isolated Linux VMs)`.

Globals: `VM_RUNS` (in-memory dict, id -> record, also persisted one-JSON-
file-per-run under `data/vm-runs/<id>.json`), `VM_LOCK` (RLock guarding
`VM_RUNS`), `VM_TERM_CREDS` (terminal credentials, **memory-only, never
persisted or logged**), `VM_STOP` (id -> bool, polled by the runner thread at
phase boundaries to support cooperative stop).

Run record fields: `id`, `vm_name`, `state`, `reason`, `keep`, `memory_mb`,
`cpus`, `created`/`started`/`ended`, `agent`, `task_id`, `task_title`, `chat`,
`score` (`{passed, output, duration_s}`), `benchmark_id` (groups a task fanned
out across multiple agents), `ssh_port`, `vm_log` (rolling, capped ~40000
chars), `terminal` (`{active, port}`).

Routes (all under `/api/vmbench`):

| Method | Path | Handler |
|---|---|---|
| GET | `/api/vmbench` | list tasks + settings + last 40 runs |
| GET | `/api/vmbench/runs/<id>` | full run view incl. transcript |
| POST | `/api/vmbench/settings` | `update_vmb_settings` |
| POST | `/api/vmbench/runs` | `create_vm_run` — single run |
| POST | `/api/vmbench/benchmarks` | `create_benchmark` — one task, many agents, shared `benchmark_id` |
| POST | `/api/vmbench/runs/<id>/stop` | cooperative stop |
| POST | `/api/vmbench/runs/<id>/delete` | only when not live |
| POST | `/api/vmbench/runs/<id>/score` | on-demand scoring, runs as a background job (poll `/api/jobs/<id>`) |
| POST | `/api/vmbench/runs/<id>/terminal-start` / `terminal-stop` | on-demand ttyd web terminal |

`vm_run_runner(rid)` is the whole lifecycle in one daemon thread: wait for a
free slot -> provision (keypair, port, render Vagrantfile, `vagrant up`) ->
wait for SSH -> seed files -> `ready` -> (if agent) attach `vm-relay` overlay,
run the agent turn, detach -> (if task has a `check`) score -> finish. The
`finally` block only auto-destroys the VM if the run ended in
`done`/`error`/`stopped` **and** `keep` is false — a resting `ready` scratch
VM is never torn down from here.

Concurrency is tracked globally via `count_occupying_slots()` against
`max_concurrent` (every `queued`/`provisioning`/`working`/`scoring`/`ready`
run counts as one occupied slot) — not per-VM.

## Conventions to replicate

**ID / name regexes** (keep these in sync if you add a parallel one):
- Run id: `^[a-f0-9]{8}$` (`uuid.uuid4().hex[:8]`) — defined independently in
  both `app.py` (`VM_RUN_ID_RE`) and `vm_runner.py` (`RUN_ID_RE`). Duplicated
  on purpose (modules don't share a shared-constants file here) — if you add
  a new id shape, define it the same way in both places rather than
  importing one from the other, to stay consistent with the existing split.
- Task catalog id: `^[a-z][a-z0-9-]{1,40}$` (`vm_runner.TASK_ID_RE`), checked
  at catalog load time against every entry in `vm_tasks.json`.
- VM name: `f"aiagentplayground-vmbench-{rid}"`. Terminal compose project:
  `f"aiagentplayground-vmterm-{rid}"`.

**Port ranges** (`vm_runner.py`):
```python
SSH_PORT_RANGE  = (62200, 62299)   # one host-forwarded SSH port per concurrently-provisioning VM
TERM_PORT_RANGE = (62300, 62399)   # one per active on-demand web terminal
```
Allocated via `allocate_port(range, taken)`: first port in range that's
neither claimed by another live run (`taken_ports()`) nor already bound on
the host loopback (`port_free`). A multi-VM feature needs its own range(s),
sized for `max hosts per topology × max_concurrent topologies` — don't reuse
`SSH_PORT_RANGE` for per-node ports inside a topology, or a single-VM run and
a topology node could collide.

**Resource defaults** (`vmb_settings()`, persisted at
`data/vmbench-settings.json`): `max_concurrent` 1-6 (default 2), `memory_mb`
512-8192 (default 1536), `cpus` 1-4 (default 1), `keep_default` (bool,
default false).

**Vagrant/VirtualBox isolation posture** (`vm_runner.render_vagrantfile`):
box `ubuntu/jammy64`, synced folder disabled, only SSH forwarded and only to
`127.0.0.1`, clipboard/drag-drop/audio disabled, guest additions check
disabled. Provisioning creates a passwordless-sudo `bench` user, installs a
fixed package set, and — when `offline=True` (the default for the existing
coding tasks) — finishes by installing a default-deny-outbound UFW firewall.

**Per-run directory layout**: `data/vm-runs/<rid>/` holds `Vagrantfile`,
`provision.sh`, `id_ed25519[.pub]` (fresh keypair every run, never reused),
and `seed/` (staged files later `scp`'d in, with path-traversal checks on
every seed path).

## Frontend (control-panel/static/vmbench.js)

No dedicated HTML block — the whole "VM Labs" dialog is built in JS and
appended to `document.body`. Shares globals (`h`, `$`, `api`, `state`,
`action`, `refresh`) from `static/app.js`; loaded after it in
`static/index.html`.

- `openVB()` builds the dialog, calls `loadVB()`, and polls
  `GET /api/vmbench` every 2s while open; `loadVBDetail()` polls
  `GET /api/vmbench/runs/<id>` for the selected run. Both diff-render against
  a signature string to avoid DOM churn — follow this pattern rather than
  re-rendering unconditionally if you add a new polled panel.
- Runs are grouped by `benchmark_id` (falling back to the run's own id) so a
  multi-agent benchmark renders as one bordered group.
- The terminal URL embeds basic-auth creds directly
  (`http://bench:<cred>@127.0.0.1:<port>/`) opened via `window.open` — creds
  never touch `localStorage` or get logged; keep it that way for any new
  per-node terminal links.
- Action buttons (`terminal`, `score`, `stop`, `delete`) are enabled/disabled
  per run state (`canTerminal`, `canScore`, `canStop`, `canDelete`) — mirror
  this rather than relying on the backend to silently no-op a disallowed
  action.

## Tests

- `tests/test_vm_runner.py` — pure-logic unit tests (no Docker/VirtualBox
  needed): port allocation, generated-Vagrantfile isolation settings and
  injection-safety, seed-file path-traversal rejection, task catalog
  validation.
- `tests/test_vmbench.py` — integration-style tests against `app.py`'s
  orchestration (vagrant/docker/SSH calls mocked out): settings validation,
  full scratch-VM and agent-attached lifecycles, concurrency/slot limits,
  benchmarks, score-now, terminal start/stop idempotency, agent-deletion
  cascade, restart-recovery (`interrupted`).

Run both with `python3 -m unittest discover -s tests -v` from `control-panel/`
before considering a VM Lab change done — the mocked integration tests are
cheap to run and catch lifecycle regressions that are easy to miss by
inspection alone.

## Multi-VM networking topologies (implemented)

A second, parallel lab kind — routers/switches/hosts as real VMs wired
together — is implemented alongside the single-VM benchmarks above, reusing
every pattern in this skill rather than replacing them. Read these files for
the full implementation; this section is a map, not a replacement for them.

- `vm_runner.py`: `render_topology_vagrantfile()` renders one multi-machine
  Vagrantfile (one `config.vm.define` block per node) plus one
  `provision-<node>.sh` per node, role-dependent
  (`NODE_ROLES = (router, switch, host, loadbalancer, firewall)`):
  `host`/`router` get `iproute2`/`ping`/`traceroute`/`tcpdump`, deliberately
  no UFW lockdown and no pre-enabled `ip_forward`; `switch` gets
  `bridge-utils` and bridges every lab-facing NIC into `br0`, config-free
  (the candidate-interface list is snapshotted BEFORE `br0` is created, or
  `br0` ends up enslaved to itself — hit for real on first VirtualBox boot
  test); `firewall` gets `nftables` only (no `iptables`/`ufw`), enabled but
  with no pre-applied policy; `loadbalancer` gets `nginx` +
  `libnginx-mod-stream`, default site left in place but no upstream
  pre-written. Every role boots the *same* `ubuntu/jammy64` box — see
  `resources/README.md` for why that box isn't vendored in-repo and how to
  cache it once for offline use. `load_topologies`/`get_topology` and
  `load_topology_tasks`/`get_topology_task` mirror the single-task
  catalog loaders, reading `vm_topologies.json` / `vm_topology_tasks.json`.
  `TOPO_SSH_PORT_RANGE`/`TOPO_TERM_PORT_RANGE` are separate ranges from the
  single-VM ones (own range per the warning above). `intnet_name(rid, idx)`,
  `vm_name_for_node(rid, name)` and `relay_port_for_node(topology, name)` are
  the naming/port helpers; `links_for_node()` gives a node's NIC order.
  Adding a role is additive-only: a new `_topo_provision_script()` branch is
  the only required change — `render_topology_vagrantfile`, `app.py`'s
  lifecycle, and the frontend are all already role-agnostic.
- `app.py`: a parallel `TOPO_RUNS` dict (not a reshaped `VM_RUNS` — the
  record shape genuinely differs: `nodes: {name: {role, ssh_port,
  terminal}}`), with `topo_run_runner` mirroring `vm_run_runner`'s lifecycle.
  Uses its own `topo_log()`, **not** the single-VM `vm_log()` — `vm_log()`
  hardcodes `save_vm_run()`, so reusing it from `topo_run_runner` silently
  writes topology-shaped records into `data/vm-runs/` instead of
  `data/topo-runs/` (in-memory `TOPO_RUNS` still reads back correctly,
  masking the bug, until a restart's `load_vm_runs()` loads the phantom
  files and `count_occupying_slots()` counts them as real occupied VM-bench
  slots forever — this exact bug shipped once and was only caught by a real
  boot test, not the mocked unit tests, since they never read the real data
  dir). Any future per-run logger must save via that run's own save
  function, not borrow another lifecycle's.
  `count_occupying_slots()` now sums both pools, charging `len(nodes)` slots
  per topology run — **but never less than a run's own node count**, i.e.
  the wait loop compares against `max(max_concurrent, my_slots)`, not
  `max_concurrent` alone: otherwise a topology bigger than the configured
  `max_concurrent` would wait forever for a slot that can never free (a real
  deadlock hit during implementation — the fix still queues normally behind
  any *other* already-running work, it just never blocks on its own size).
  Agent attach uses **one relay container per topology run**, not one per
  node (`templates/vm-relay-topo.compose.yml`, service `vm-relay-topo`,
  distinct from the single-VM `vm-relay` so the two can never collide): a
  single `alpine/socat` container runs N listeners, one per node, built from
  `vr.relay_port_for_node()` into `VM_RELAY_TOPO_CMD`. One SSH keypair per
  topology run (not per node). Routes live under `/api/vmtopo`, including
  per-node terminal routes (`/api/vmtopo/runs/<id>/nodes/<node>/terminal-*`).
- `static/vmtopo.js`: a second section appended into the same VM Labs dialog
  (`vtBuildSection()`, called from `vmbench.js`'s `buildVBDialog()`; polled
  via `vtLoad()`, called from `vmbench.js`'s existing timer) — not a separate
  dialog. The combined "N running" badge lives in `vmbench.js`'s
  `refreshVBBadge()`, summing both `vbData` and `vtData`.
- `vm_topologies.json`: six named templates (`s1h2`, `r1s1h2`, `r1s2h2`,
  `r2s2h2`, `lb1s1h3`, `fw1s2h2`) rather than free-form topology editing,
  matching this project's fixed-validated-inputs philosophy. `lb1s1h3` (1
  loadbalancer, 1 switch, 3 hosts) and `fw1s2h2` (1 firewall, 2 switches, 2
  hosts) are the two appliance-role templates. `vm_topology_tasks.json`: one
  task per template, each with a `check_node` + `check` (reuses
  `score_run()` unchanged — no new scoring mechanism).
- Tests: `tests/test_vm_runner.py` (catalog validation, Vagrantfile
  rendering) and `tests/test_vmtopo.py` (lifecycle, mirroring
  `test_vmbench.py`'s mocking conventions) — including a regression test for
  the slot-accounting deadlock above.
- **Real VirtualBox boot testing catches things the mocked unit tests
  structurally cannot**, because every test mocks `vr.vagrant`/`vr.ssh_wait`
  etc. and never boots a real kernel. Two real bugs only surfaced this way:
  (1) the switch's bridge script ran `ls /sys/class/net` *after* creating
  `br0`, so it tried to enslave the bridge to itself ("Can not enslave a
  bridge to a bridge") — fixed by snapshotting interfaces first (see
  `_topo_provision_script`'s switch branch); (2) a tight `memory_mb` setting
  (512MB) made multi-VM provisioning flaky enough that one node could fail
  to accept SSH within the retry window — not a code bug, but a reminder
  that the default 1536MB headroom exists for a reason. After any change to
  `render_topology_vagrantfile`, `_topo_provision_script`, or the agent
  relay/wrapper plumbing, do at least one real `vagrant up` on a topology
  before calling it done — the test suite passing is necessary, not
  sufficient.

## Custom topologies (structured builder) and free-form prompts

Beyond the fixed catalog, `vr.build_custom_topology(counts, wiring, links)`
builds an ad hoc `{title, nodes, links}` dict from role counts (host,
router, switch, loadbalancer, firewall) and a wiring pattern (`star`:
everything off one switch; `chain`: switches/routers/firewalls interleaved
into a line, hosts/loadbalancers round-robined onto the nearest switch;
`manual`: literal `{a, b}` pairs) — capped at `MAX_CUSTOM_NODES` (12),
validated through the exact same `validate_topology()` as a catalog entry.
`app.create_topo_run` branches on `form["topology_id"]` (catalog) vs.
`form["custom"]` (builder); a custom run's full topology dict is embedded
on the run record as `r["topology"]` (no catalog id to re-look-up later) -
`topo_run_runner` fetches via `vr.get_topology(r["topology_id"]) if
r["topology_id"] else r["topology"]` and otherwise doesn't know or care
which path produced it.

Separately, **any** run (catalog or custom topology) can take a
`custom_prompt` instead of a `task_id` when an agent is attached — the
prompt goes straight to the agent, and the existing `if task and
task.get("check")` scoring block is already naturally skipped since there
is no `task`, so `Score now` just isn't available (same as a no-task
scratch lab) and no new scoring code exists. A custom *topology* has no
catalog task that could possibly match it, so it requires `custom_prompt`
specifically, not `task_id` (enforced in `create_topo_run`).

For a **permanent, reviewed, scoreable** custom template instead of an
ephemeral builder run, see the separate
`.claude/skills/vm-lab-template-author/` skill — it walks through adding a
real catalog entry (with a real `check` script) to
`vm_topologies.json`/`vm_topology_tasks.json` by hand, which then shows up
in the UI exactly like a built-in template.

## Network workbench features (implemented)

Everything below lives on the topology side (`TOPO_RUNS`, `/api/vmtopo`,
`static/vmtopo.js`) and is listed in the user-facing README under "Network
workbench". The design choices that aren't obvious from the code are noted
here.

**Modules.** Pure logic is kept out of `app.py` so it's testable without
mocks: `lab_intents.py` (parse and check intents), `lab_changes.py` (parse
the agent's session log into per-command entries). `app.py` wires them to
SSH, Vagrant and the lab record. `vm_runner.py` has `capture_command`,
`scp_from` (VM to host; `scp_to` is the other way), and `vmrun_script(...,
node=)`.

**Run record fields** (on top of the topology fields above):
- `intents`: parsed dicts, from the form or a lab file. `intent_results`:
  last check's `[{text, passed, detail}]`.
- `captures`: `[{id, node, iface, seconds, state, size, reason, ts}]`. Files
  live in `TOPOR_DIR/<rid>.captures/<cid>.pcap`, deleted with the lab.
- `changes`: one entry per agent turn, `{turn, point, rolled_back, commands,
  command_count}`. `point` is the snapshot name, or None if not every node
  snapshotted.
- `change_seq`: the lab's own turn counter. Use it for snapshot names, not
  `agent_turns`: attach resets `agent_turns` to 0, so names would collide.
- `restoring`: true during a rollback. Attach and another rollback are refused.
- `plan_first`, `plan`, `plan_approved`, and `pending_opening` (transient: the
  approval message, consumed by the next `topo_agent_phase`).

**Routes** (all under `/api/vmtopo/runs/<id>`): `POST .../intents` (check
now), `POST .../nodes/<node>/capture` `{iface, seconds}`, `GET
.../captures/<cid>` (the .pcap), `POST .../rollback` `{turn}`, `POST
.../approve-plan` `{interactive}`. Each runs as a `start_job` job except the
capture POST, which returns the capture record while its job runs.

**Intents** (`lab_intents.py`). Grammar: `src -> dst icmp|tcp/<port>
reach|block` and `src -> dst path via n1, n2`. `run_intent_checks(intents,
nodes, run_on)` takes an injected `run_on(node, cmd, timeout)`, so tests use a
fake. The app's `intent_run_on` turns SSH status 255 into `OSError`, so an
unreachable node fails its intents with a reason, not a failed probe (a
failed probe is a non-zero rc from the command itself). Path checks run
`traceroute -n` and require the via nodes' addresses in order, ending at the
destination; hosts and routers have `traceroute` from the provision script. A
block passes on any probe failure, so a firewall drop and a refusal both
count. UDP is refused at parse time: a closed and a filtered UDP port look the
same to a probe, so a result would be unreliable.

**Change log and rollback** (`lab_changes.py`, `topo_agent_turn`,
`topo_rollback`). `vmrun_script` writes `=== <ts> <node> $ <cmd>`. The
`=== ` prefix is what `count_vm_commands` greps, so keep it. A turn reads
the session log before and after, and `new_entries` gives its commands.
Rollback runs `vagrant snapshot restore --no-provision <node> <point>` for
each node, then `vagrant up --no-provision`, then `ssh_wait` on each node. A
failed restore marks nothing rolled back and sets `reason`. The lab may be
half-restored after a failure, and the reason says so.

**Packet captures** (`topo_capture_start`, `vr.capture_command`). `timeout -s
INT N tcpdump ... -c 20000` so tcpdump flushes on stop, then `chmod 644` and
`test -s` (a header-only file counts as a capture; an empty one fails).
Interface names are checked against a regex before anything runs. The
interface must exist (`ip -o link show dev`).

**Plan first** (`write_plan`, `topo_approve_plan`). The first turn runs with
nothing attached: no key copied into the agent, no relay, no vmrun wrapper.
`topo_agent_phase` returns `PLAN_WAITING`, and `topo_run_runner` returns
*without* `finish()`, so `finished` stays False and the `finally` block
doesn't tear the lab down. Approval sets `plan_approved` and calls
`attach_agent_to_lab` with `pending_opening`. A failed attach reverts the
approval. `attach_agent_to_lab` refuses a plan-first lab until it's approved.

**Testing notes:**
- `tests/test_lab_intents.py`, `test_packet_capture.py`, `test_change_log.py`,
  `test_plan_first.py`. The change-log tests run the real wrapper through
  `sh` with a fake `ssh` on PATH (skipped on Windows).
- Don't depend on local state. `test_plan_first` once passed only on a
  machine with an agent named `alpha`: it runs `env_file`, which calls
  `load_meta`. Mock `env_file` and `proj` when a test reaches
  `topo_agent_phase`.
- Mocked tests can't catch wrong `vagrant snapshot` / `restore` / `up`
  arguments, or a wrong `tcpdump` invocation. Do a real boot before calling
  rollback, captures or plan-first done.
- The static JS (`vmtopo.js`) isn't syntax-checked by CI (CI runs Python
  only). Load a lab's view in a browser after UI changes.
