"""Container labs: a lab whose nodes are Docker containers instead of VirtualBox VMs.

Build modes (a lab's `build_mode`):
  vm      every node is a VM (the default, and what every lab was before this module)
  full    every node is a container; no VirtualBox at all. Runs anywhere Docker runs: Linux, WSL2, Docker Desktop
          on Windows, Docker Desktop on macOS.
  mixed   network devices (router, switch, firewall, loadbalancer) are containers, hosts and servers are VMs. Not
          built yet: the containers and VMs have to share L2 links, which needs a Docker host VM bridged onto the
          VirtualBox internal networks (see the container-router spike). Refused at create time for now.

A container node looks like a VM node to everything else: it runs sshd, its port 22 is published on 127.0.0.1 at the
node's ssh port, and the panel reaches it exactly as it reaches a VM (ssh_wait, relay, terminals, vmrun wrappers,
captures, the change log). Only creation and teardown differ.

Networking mirrors a VM lab:
  eth0  management network: no masquerade, so the node has no route out. It exists so the published ssh port works.
  ethN  one point-to-point link per lab link, in link order - the same order as a VM's lab NICs. Each link is a veth
        pair created by the root helper lab_node/link.sh, with one end in each node's network namespace. There is no
        Docker network or bridge for a link, so frames can't leak between links (see the container-router notes).
        A switch node bridges its own links (lab-wire), as the VM switch does.

Trade-offs, recorded in control-panel/README.md and the vm-lab-dev skill:
  - no internet: the egress proxy and ufw need a VM. Container labs have none.
  - no suspend/save or VirtualBox snapshot rollback points; change-log rollback is off for container labs.
  - every container of a build shares the image's host keys (lab only, same as StrictHostKeyChecking off for VMs).
  - no FRR in the image: routing protocols are installed on VM routers only.
  - a container's memory cap is the lab's memory setting per node; CPU is the lab's cpus setting per node.
"""
import os, re, subprocess
from pathlib import Path

BUILD_MODES = ("vm", "mixed", "full")
MIXED_CONTAINER_ROLES = ("router", "switch", "firewall", "loadbalancer")   # used once mixed labs can be built
IMAGE = "aiagentplayground/lab-node:1"
IMAGE_DIR = Path(__file__).resolve().parent / "lab_node"
LABEL = "aiagentplayground-lab"       # every container and network a container lab creates carries this label
BUILD_TIMEOUT = 1800                  # first build pulls ubuntu and installs packages; later labs reuse the image
# Root helper for forwarding (lab_node/forward.sh), installed root-owned at this path. The panel calls it through
# sudo -n; a sudoers line must allow exactly this path (see the README). Env override for a different install.
FORWARD_HELPER = os.environ.get("AG_LAB_FORWARD_HELPER", "/usr/local/sbin/ag-lab-forward")
# Root helper for links (lab_node/link.sh): creates one point-to-point veth between two node containers.
LINK_HELPER = os.environ.get("AG_LAB_LINK_HELPER", "/usr/local/sbin/ag-lab-link")
NODE_CONTAINER_RE = re.compile(r"^aglab-[0-9a-f]{8}-[a-z][a-z0-9]{0,14}$")


def node_backend(build_mode, role):
    """'vm' or 'container' for one node of a lab built this way."""
    if build_mode == "full":
        return "container"
    if build_mode == "mixed" and role in MIXED_CONTAINER_ROLES:
        return "container"
    return "vm"


def validate_build_mode(build_mode, topology):
    """Raises ValueError for a build mode this panel can't build yet, or a topology a container lab can't run."""
    if build_mode not in BUILD_MODES:
        raise ValueError(f"unknown build mode '{build_mode}'")
    if build_mode == "mixed":
        raise ValueError("a mixed lab (containers for network devices, VMs for hosts) is not built yet; "
                         "use vm or full")
    if build_mode == "full":
        too_many = [n["name"] for n in topology["nodes"] if len(links_of(topology, n["name"])) > 16]
        if too_many:
            raise ValueError(f"a container node can have at most 16 lab links; {', '.join(too_many)} has more")


def links_of(topology, name):
    return [idx for idx, link in enumerate(topology["links"]) if name in (link["a"], link["b"])]


def container_name(rid, node):
    return f"aglab-{rid}-{node}"


def mgmt_network(rid):
    return f"aglab-{rid}-mgmt"


def lab_iface_names(count):
    """The lab NICs of a container node in link order: eth1, eth2, ... (eth0 is management)."""
    return [f"eth{i + 1}" for i in range(count)]


def plan(rid, topology, node_ports):
    """What a container lab needs, as plain data: the management network, one container per node (its role, published
    ssh port and lab interfaces in link order), and one entry per link saying which interface each end gets. Pure, so
    tests can check it without Docker."""
    containers = []
    for node in topology["nodes"]:
        name = node["name"]
        idxs = links_of(topology, name)
        containers.append({"node": name, "role": node["role"], "container": container_name(rid, name),
                           "port": node_ports[name], "ifaces": lab_iface_names(len(idxs))})
    links = []
    for idx, link in enumerate(topology["links"]):
        ends = {}
        for side in ("a", "b"):
            node = link[side]
            pos = links_of(topology, node).index(idx)
            ends[side] = (container_name(rid, node), lab_iface_names(len(links_of(topology, node)))[pos])
        links.append({"index": idx, "a": ends["a"][0], "a_iface": ends["a"][1],
                      "b": ends["b"][0], "b_iface": ends["b"][1]})
    return {"management": mgmt_network(rid), "containers": containers, "links": links}


def docker(*args, input=None, timeout=120):
    """Run the docker CLI. Returns (rc, stdout, stderr); a missing docker is rc 127, not an exception."""
    try:
        p = subprocess.run(["docker", *args], input=input, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "docker is not installed or not on PATH; container labs need Docker running"
    except subprocess.TimeoutExpired:
        return 124, "", f"docker {args[0]} took longer than {timeout}s"


def ensure_image(log):
    """Build the node image once. Returns (rc, output)."""
    rc, _, _ = docker("image", "inspect", IMAGE, timeout=30)
    if rc == 0:
        return 0, ""
    log("building the lab node image (first time only; installs the tools the nodes come with)...")
    rc, out, err = docker("build", "-t", IMAGE, str(IMAGE_DIR), timeout=BUILD_TIMEOUT)
    return rc, (out + err)


def make_link(rid, idx, ca, ia, cb, ib, helper=None):
    """One point-to-point link between two node containers, through the root helper. Returns (rc, output)."""
    for c in (ca, cb):
        if not NODE_CONTAINER_RE.match(c or "") or not c.startswith(f"aglab-{rid}-"):
            raise ValueError(f"not a node of lab {rid}: {c!r}")
    argv = _root_argv([helper or LINK_HELPER, rid, str(idx), ca, ia, cb, ib])
    return _run_helper(argv, "the link helper")


def up(rid, topology, node_ports, pubkey_text, memory_mb, cpus, log):
    """Create the management network, start one container per node, create each link (a veth pair between the two
    nodes, see lab_node/link.sh), then wire each node. Returns (rc, output). On failure the caller tears down whatever
    was made (teardown() is idempotent)."""
    rc, out = ensure_image(log)
    if rc != 0:
        return rc, out
    p = plan(rid, topology, node_ports)
    label = ["--label", f"{LABEL}={rid}"]
    rc, out, err = docker("network", "create", *label,
                          "--opt", "com.docker.network.bridge.enable_ip_masquerade=false", p["management"], timeout=60)
    if rc != 0:
        return rc, out + err
    for c in p["containers"]:
        log(f"  creating {c['node']} ({c['role']})...")
        args = ["create", "--name", c["container"], *label, "--hostname", c["node"], "--restart", "no",
                "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW", "--memory", f"{memory_mb}m", "--cpus", str(cpus),
                "--network", p["management"], "-p", f"127.0.0.1:{c['port']}:22",
                "-e", f"AG_PUBKEY={pubkey_text}", "-e", f"AG_ROLE={c['role']}",
                "-e", f"AG_LAB_LINKS={len(c['ifaces'])}", IMAGE]
        rc, out, err = docker(*args, timeout=120)
        if rc != 0:
            return rc, out + err
        rc, out, err = docker("start", c["container"], timeout=120)
        if rc != 0:
            return rc, out + err
    for ln in p["links"]:
        log(f"  linking {ln['a']}:{ln['a_iface']} <-> {ln['b']}:{ln['b_iface']}...")
        rc, out = make_link(rid, ln["index"], ln["a"], ln["a_iface"], ln["b"], ln["b_iface"])
        if rc != 0:
            return rc, out
    for c in p["containers"]:
        rc, out, err = docker("exec", c["container"], "/usr/local/bin/lab-wire", timeout=60)
        if rc != 0:
            return rc, out + err
    # Docker turns forwarding on in every container; a VM starts with it off. Set it off here, through the helper, so
    # the lab starts the way a VM lab does and the lab page's state is right from the start.
    for c in p["containers"]:
        rc, out = set_forwarding(c["container"], False)
        if rc != 0:
            return rc, out
    return 0, ""


def _root_argv(argv):
    """The command as root: as it is when the panel is root, else through sudo -n (no password prompt)."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:     # no geteuid on Windows: the helpers need Linux anyway
        return ["sudo", "-n", *argv]
    return argv


def _run_helper(argv, what):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return 127, f"sudo or {what} is not installed (see the container labs section of the README)"
    except subprocess.TimeoutExpired:
        return 124, f"{what} took longer than 30s"
    return p.returncode, (p.stdout + p.stderr).strip()


def set_forwarding(container, on, helper=None):
    """Turn IP forwarding on or off in one lab node's network namespace, through the root helper. Returns (rc, output).
    Refuses anything but a lab node container name and a boolean before anything runs."""
    if not NODE_CONTAINER_RE.match(container or ""):
        raise ValueError(f"not a lab node: {container!r}")
    return _run_helper(_root_argv([helper or FORWARD_HELPER, container, "1" if on else "0"]), "the forwarding helper")


def teardown(rid):
    """Remove every container and network this lab created. Safe to call twice or on a lab that never started."""
    rc, out, _ = docker("ps", "-aq", "--filter", f"label={LABEL}={rid}", timeout=60)
    ids = out.split()
    if ids:
        docker("rm", "-f", *ids, timeout=120)
    rc, out, _ = docker("network", "ls", "-q", "--filter", f"label={LABEL}={rid}", timeout=60)
    nets = out.split()
    if nets:
        docker("network", "rm", *nets, timeout=120)
