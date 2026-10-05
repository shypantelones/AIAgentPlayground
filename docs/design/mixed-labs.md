# Mixed labs: container routers, VM hosts, one Docker host VM

Status: design only. No code in this PR. Stacked on the team-labs design (merge order: #45, then the team design,
then this one). Depends on a spike that has not been run (see "Risks": the spike comes first).

## Goal

A lab where some nodes are containers (routers, switches, firewalls) and some are VMs (hosts, servers), all on the
same links. Two results follow:

- It removes the Docker Desktop requirement for container nodes. Containers run in a Linux guest, so the panel works
  on Windows (VirtualBox) with no Docker Desktop, and on Linux.
- Containers keep their cost advantage (seconds to start, small memory) for the nodes that are mostly config.

## Why a Docker host VM

Containers and VMs can't share a link directly: a VirtualBox internal network only reaches VM NICs. So one Linux
guest, the **Docker host VM** (`dockerhost`), sits on the lab links. It runs Docker Engine, and bridges its NICs
to the containers:

```
  VM h1 ---- intnet link0 ---- [dockerhost: NIC enp0s8 -- bridge br0 -- veth --> container r1 eth1]
                                                                          |
  VM h2 ---- intnet link1 ---- [dockerhost: NIC enp0s9 -- bridge br1 -- veth --> container r1 eth2]
```

- A container-to-container link stays a veth pair, as now (`link.sh`), both ends in the dockerhost's containers.
- A container-to-VM link is a Linux bridge in the dockerhost: the VM's intnet NIC is one port, the container's veth
  is another. The bridge is the same kind of L2 domain the VM switch role builds (`br0`).
- Every dockerhost NIC on a VirtualBox intnet needs `nicpromisc allow-all` (set at VM creation, like the switch
  role's NICs). Without it, the bridge doesn't see frames addressed to other MACs.

## Components

1. **Topology split.** A mixed lab's topology keeps its nodes. `node_backend(mode, role)` already says which nodes are
   containers (`MIXED_CONTAINER_ROLES`). The dockerhost is an extra VM, not a node the user sees.
2. **Vagrantfile.** `render_topology_vagrantfile` gets one extra VM (`dockerhost`) with: one intnet NIC per lab link
   that has a container end (promiscuous), one NAT NIC, and forwarded ports for each container's ssh (see 4). The VMs
   for hosts stay as they are.
3. **Provisioning.** The dockerhost's provision script installs Docker Engine (the same `get.docker.com` route used
   for WSL), creates the lab image (`lab_node/`), and installs `link.sh` and `forward.sh` as root-owned helpers
   under `/usr/local/sbin`, with the sudoers line that allows only those paths for the `bench` user.
4. **Container ssh.** Each container publishes its sshd on a fixed port of the dockerhost (`-p 22<N>:22`).
   Vagrant forwards the host's ssh port to it, so the panel's `ssh_wait`, the relay (`relay_command` with
   `host.docker.internal:<port>`) and member keys all work unchanged, just as they do for VMs.
5. **Link and wire.** The panel runs the link helper and `lab-wire` inside the dockerhost over ssh (as `bench`, with
   sudo for the two helper paths only), not on the host. A bridge is created per link that has a container end and a
   VM end; its VM NIC is the bridge port.
6. **Forwarding.** `forward.sh` runs in the dockerhost, through the same ssh path. The request path from #43 is
   unchanged; only the place the helper runs changes.
7. **Build order.** Vagrant boots the dockerhost first (it has the longest provisioning: Docker and the image), then
   the VMs, then `up()` creates containers and links inside the dockerhost, then ssh readiness is checked as now.

## Risks (test before building)

1. **Frame loss.** The loss we measured earlier (about 13% first-link ping loss, hub VM, Docker bridge networks) was
   never explained. Linux bridges with veth ports inside a VM are a different setup, so they may not have the
   problem, but nothing here has tested it. **Spike first:** one dockerhost, one VM on one intnet, one container on
   a bridge with the VM's NIC; 30 trials each way. Go only if the spike shows no loss.
2. **Promiscuous NICs.** The spike must confirm that `allow-all` on the dockerhost's intnet NICs is enough, with the
   guest's NIC also set promiscuous (the hub test needed both).
3. **First-run time.** Docker plus the image is several minutes on first build. The lab page should say so (the
   dockerhost is shown as a provisioning step, like the VM boots).
4. **Memory.** The dockerhost adds one VM's memory on top of the lab's VMs. The lab's memory setting applies to it.
5. **One dockerhost per lab** (proposed). Labs don't share containers, and teardown stays `vagrant destroy`. A shared
   host would need label-based cleanup on top.

## Platforms

- **Windows:** VirtualBox runs the lab and the dockerhost. The panel stays on Windows; the Docker Engine runs in the
  guest, so Docker Desktop isn't needed for mixed labs.
- **Linux:** the same, on VirtualBox. Pure container labs (`full`) still need a Linux Docker host.
- **macOS:** VirtualBox doesn't run on Apple Silicon. Intel Macs can run this design; Apple Silicon can't (not
  tested here).

## Trade-offs

- A mixed lab has the cost of VMs plus one extra VM. It's slower to start than a full container lab.
- No internet for container nodes (same as `full`). VM nodes keep the lab's egress proxy as they do now.
- Save/suspend: a mixed lab can't be saved yet, since containers aren't suspended. VM-only saves stay refused for
  mixed labs for the same reason.

## Tests

- Unit: `render_topology_vagrantfile` adds the dockerhost with promisc NICs only for links with a container end;
  the ssh port for each container is forwarded; the helper paths in the sudoers line are exactly the two helpers.
- Live: the spike (above), then a three-node lab: a VM host, a container router, a VM host, all on links, with the
  same checks as the full-lab live test (ssh, both links, routed path, forwarding, internet isolation).

## Open questions for review

1. Is one dockerhost per lab right, or should several labs share one?
2. Should the dockerhost be shown to the user as a node, or hidden as infrastructure?
3. Spike first, then build: agreed?
