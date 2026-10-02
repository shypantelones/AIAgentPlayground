# VM images used by VM Labs

Every VM that VM Labs creates — single-VM coding benchmarks and multi-VM
network-topology labs alike, across every role (`host`, `router`, `switch`,
`loadbalancer`, `firewall`) — boots the **same** Vagrant box:

| Box | Provider | Source |
|---|---|---|
| `ubuntu/jammy64` | VirtualBox | [Vagrant Cloud](https://app.vagrantup.com/ubuntu/boxes/jammy64) |

A node's role comes entirely from its *provisioning script*
(`vm_runner._topo_provision_script`/`render_vagrantfile` in
`control-panel/`), not from a different image — a `firewall` node is the
same box as a `host` node, just with `nftables` installed instead of
nothing extra. Adding network gear is additive: see
`.claude/skills/vm-lab-dev/SKILL.md`.

## Why the box file itself isn't in this repo

The box is a compressed VM disk image, roughly 500-700MB. This repo is
public on GitHub, which hard-blocks any single file over 100MB without Git
LFS — a plain commit of the box simply wouldn't push. Git LFS would make it
push, but at the cost of a permanent, ongoing storage/bandwidth line item
and an extra `git-lfs` dependency for every contributor, for something
Vagrant already solves on its own: it downloads a box once and caches it
locally, so every VM Lab run after the first reuses that same cached copy
with no further network access. Vendoring the binary here would duplicate
that caching with none of the benefit.

If you want this pinned for stricter reproducibility later (rather than
floating to whatever `ubuntu/jammy64` currently resolves to), set
`config.vm.box_version` in the generated Vagrantfile
(`vm_runner.render_vagrantfile`/`render_topology_vagrantfile`) to a specific
version from the [version list](https://app.vagrantup.com/ubuntu/boxes/jammy64),
and record that version here.

## Getting it once, offline thereafter

```
vagrant box add ubuntu/jammy64 --provider virtualbox
```

This downloads and caches the box under `~/.vagrant.d/boxes/`. Every VM Lab
run after that — any node, any role, single-VM or topology — reuses the
cached copy; `vagrant up` never re-downloads it unless the cache is cleared
(`vagrant box remove ubuntu/jammy64`) or a different version is requested.
