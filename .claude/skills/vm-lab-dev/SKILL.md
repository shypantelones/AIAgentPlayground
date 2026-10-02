---
name: vm-lab-dev
description: Reference for extending the VM Lab / benchmark feature in control-panel/ (vm_runner.py, app.py's vmbench routes, static/vmbench.js) — covers the Vagrant/VirtualBox VM lifecycle, port/naming conventions, resource defaults, and how to extend it to multi-VM networking topologies (routers/switches/hosts). Load before adding VM Lab routes, tasks, UI, or topology types.
---

# VM Lab development reference

The VM Lab ("vmbench") feature spins up throwaway Vagrant/VirtualBox Linux VMs
for AI agents (or a human, via a web terminal) to work coding or networking
tasks in. This skill is a map of the existing single-VM implementation plus
guidance for extending it to multi-VM topologies, so a new feature matches the
project's conventions instead of inventing parallel ones.

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
- VM name: `f"openclaw-vmbench-{rid}"`. Terminal compose project:
  `f"openclaw-vmterm-{rid}"`.

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

## Extending to multi-VM networking topologies

This is the gap between what exists and a router/switch/host lab:

1. **Vagrantfile**: `render_vagrantfile()` renders one VM today. A topology
   needs a multi-machine Vagrantfile (Vagrant supports several `config.vm.define`
   blocks in one file) instead of one file per host — keep topology state
   under one `data/vm-runs/<rid>/` directory per the existing layout, not one
   per node.
2. **Networking**: the existing default-deny-outbound-UFW-after-provisioning
   policy assumes a VM never needs to reach anything post-setup. A topology's
   whole point is nodes reaching each other, so this needs a *different*
   policy, not a weaker one: give the topology's own VMs a private VirtualBox
   internal network (`private_network`, `virtualbox__intnet: true`) scoped to
   that one run — this still satisfies the project's existing invariant that
   a VM never routes to the host, another agent, or another lab's VMs, it
   just adds "except the other VMs in its own topology, which is the point."
   Keep SSH-from-host as the only host-facing path, exactly as today.
3. **Slot accounting**: `count_occupying_slots()` currently counts one VM as
   one slot. A topology with N nodes should count as N slots (or get its own
   separate concurrency limit) — don't let a 4-node topology quietly cost the
   same capacity as a 1-node scratch run.
4. **Task catalog**: `vm_tasks.json`'s `check` script pattern (SSH in, run a
   script, exit code = pass/fail) extends naturally — have `check` SSH into
   whichever node should prove connectivity (e.g. ping/curl from host2 to
   host1) rather than inventing a new scoring mechanism.
5. **Templated topologies**: expose a small set of named topology shapes
   (e.g. "1 router, 1 switch, 2 hosts", "2 routers, 2 switches, 2 hosts")
   rather than free-form topology editing in v1 — each template maps to a
   fixed `config.vm.define` set + a fixed set of internal networks, which
   keeps the Vagrantfile generator simple and keeps the attack surface the
   same as today's fixed, validated-inputs-only design philosophy (no
   free-form shell, same as the Docker side of this app).
6. **UI**: a topology selector replaces (or sits alongside) the existing task
   `<select>` in `buildVBDialog()`; a topology's "run" still fits the current
   run-list/detail-pane pattern if you treat it as one run record with
   multiple `{node_name: {ssh_port, ...}}` entries instead of a single
   `ssh_port` — keeps `vmbench.js`'s polling/diff-render logic reusable.
