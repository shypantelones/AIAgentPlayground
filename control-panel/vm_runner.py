"""Vagrant/VirtualBox lifecycle for the code-creation benchmark VMs. Pure logic is separated from subprocess calls
so the logic (port allocation, template rendering, task validation) can be unit-tested without booting a real VM.

Isolation posture for every VM this module creates:
  - NAT networking only, no bridged adapter, no synced folder (nothing from the host filesystem is shared).
  - SSH is the only forwarded port, bound to host 127.0.0.1 only (never the LAN).
  - Provisioning (installing build tools) runs once with NAT internet; the LAST provisioning step installs a
    default-deny outbound firewall, so once set up the VM has no internet access at all - stricter than the agent
    containers, since these VMs don't need live internet to write/fix/run code.
  - Clipboard, drag-and-drop and audio are disabled (same as the original vm-sandbox/Vagrantfile).
A VM never gets a route to the host, to another VM, or to any agent other than the one it was created for.
"""
import base64, json, os, re, secrets, shlex, shutil, signal, socket, subprocess, sys, threading, time
from pathlib import Path

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOWS = sys.platform.startswith("win")
CANCELLED_RC = 130     # returned by vagrant_stream() when its `cancel` check asked it to stop
RUN_ID_RE = re.compile(r"^[a-f0-9]{8}$")
BOX = "ubuntu/jammy64"          # same box as vm-sandbox/Vagrantfile; Vagrant caches it once, shared across runs
SSH_PORT_RANGE = (62200, 62299)  # host-side forwarded ports this feature uses, one per concurrently-provisioned VM
TERM_PORT_RANGE = (62300, 62399)


def _write_lf(path, text):
    """Path.write_text(newline=...) needs Python 3.10+; this works on 3.8+ (e.g. macOS's stock python3 3.9)."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _run(args, cwd=None, input=None, timeout=60, env=None):
    """Same stdin-as-raw-bytes approach as app.run(): avoids Windows' text-mode '\\n' -> '\\r\\n' translation
    corrupting anything with a '#!/bin/sh' shebang (or any other Linux-side script) piped over stdin."""
    try:
        p = subprocess.run(args, cwd=cwd, input=input.encode("utf-8") if isinstance(input, str) else input,
                           capture_output=True, timeout=timeout, creationflags=NOWIN, env=env)
        return p.returncode, p.stdout.decode("utf-8", errors="replace"), p.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except FileNotFoundError as e:
        return 127, "", str(e)


def have_tools():
    """Which of the external tools this feature needs are present. All are commonly preinstalled or already
    required elsewhere in this project (Vagrant/VirtualBox for the existing VM tier, OpenSSH ships with modern
    Windows/macOS/Linux)."""
    return {"vagrant": bool(shutil.which("vagrant")), "ssh": bool(shutil.which("ssh")),
            "scp": bool(shutil.which("scp")), "ssh-keygen": bool(shutil.which("ssh-keygen")),
            "docker": bool(shutil.which("docker"))}


def port_free(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) != 0


def allocate_port(rng, taken):
    """First port in `rng` that is free on the host and not already claimed by another live run (`taken`)."""
    for p in range(rng[0], rng[1] + 1):
        if p not in taken and port_free(p):
            return p
    raise RuntimeError(f"no free port in {rng[0]}-{rng[1]}; stop or delete some benchmark runs")


# ---------------------------------------------------------------- SSH keypair (per run, never reused)
def gen_keypair(run_dir):
    priv = run_dir / "id_ed25519"
    rc, _, err = _run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "aiagentplayground-vmbench", "-f", str(priv), "-q"], timeout=20)
    if rc != 0:
        raise RuntimeError(f"ssh-keygen failed: {err.strip() or rc}")
    return priv, priv.with_suffix(priv.suffix + ".pub")


# ---------------------------------------------------------------- Vagrantfile + provisioning
def render_vagrantfile(run_dir, vm_name, ssh_host_port, pubkey_text, memory_mb, cpus, offline, extra_packages=()):
    pkgs = " ".join(["build-essential", "python3", "python3-pip", "python3-venv", "git", "curl", "ca-certificates",
                     "sqlite3", "make", *extra_packages])
    firewall = """
    # Lock the VM down to no outbound network at all, now that setup is done. These benchmark tasks are about
    # writing/fixing/running code locally, not fetching things from the internet, so default-deny is the safe
    # default (stricter than even the agent containers, which at least have an allowlist proxy for a reason).
    ufw default deny outgoing
    ufw default allow incoming
    ufw --force enable
""" if offline else "    # offline=False for this task: outbound network left enabled.\n"
    provision = f"""#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y {pkgs} ufw
pip3 install --no-input --quiet pytest requests flask 2>/dev/null || true
useradd -m -s /bin/bash bench || true
mkdir -p /home/bench/.ssh /home/bench/work
echo '{pubkey_text}' > /home/bench/.ssh/authorized_keys
chmod 700 /home/bench/.ssh && chmod 600 /home/bench/.ssh/authorized_keys
chown -R bench:bench /home/bench
echo 'bench ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/90-bench
{firewall}
"""
    _write_lf(run_dir / "provision.sh", provision)
    vagrantfile = f"""# Generated by the control panel. One VM per benchmark run; destroyed afterwards unless "keep" was set.
# Isolation: NAT only, no synced folder, no bridged adapter, SSH is the only forwarded port (127.0.0.1 only).
Vagrant.configure("2") do |config|
  config.vm.box = "{BOX}"
  config.vm.hostname = "{vm_name}"
  config.vm.synced_folder ".", "/vagrant", disabled: true
  config.vm.network "forwarded_port", guest: 22, host: {ssh_host_port}, host_ip: "127.0.0.1", id: "ssh", auto_correct: false
  config.ssh.insert_key = false

  config.vm.provider "virtualbox" do |vb|
    vb.name = "{vm_name}"
    vb.memory = {memory_mb}
    vb.cpus = {cpus}
    vb.gui = false
    vb.customize ["modifyvm", :id, "--clipboard-mode", "disabled"]
    vb.customize ["modifyvm", :id, "--draganddrop", "disabled"]
    vb.customize ["modifyvm", :id, "--audio-enabled", "off"]
    vb.customize ["modifyvm", :id, "--nic1", "nat"]
    vb.check_guest_additions = false
  end

  config.vm.provision "shell", path: "provision.sh"
end
"""
    _write_lf(run_dir / "Vagrantfile", vagrantfile)


def vagrant(run_dir, *args, timeout=120):
    return _run(["vagrant", *args], cwd=str(run_dir), timeout=timeout)


def _kill_tree(p):
    """Kill a process and everything it started. Vagrant runs as a launcher plus Ruby (and VBoxManage) children, so
    killing only the top process leaves the real work running - on Windows taskkill /T walks the tree; elsewhere
    the process was started in its own session/process group, which is killed as a whole."""
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=30,
                           creationflags=NOWIN)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:  # noqa - already gone, or no permission: fall back to the process itself
        pass
    try:
        p.kill()
    except Exception:  # noqa
        pass


def vagrant_stream(run_dir, *args, on_line=None, timeout=120, cancel=None):
    """Like vagrant(), but calls on_line(line) as each line of output is produced instead of only returning the
    full output once the command finishes - for a long multi-machine `up`, a caller that wants to show live
    progress (not just a post-hoc tail dump once everything has already finished or failed) needs this. A
    threading.Timer enforces the overall timeout independently of the read loop, so a single long-hanging line
    (e.g. a \\r-only progress bar with no newline for a while) can't defeat it.
    `cancel`, if given, is polled every second; when it returns True the whole vagrant process tree is killed and
    CANCELLED_RC is returned - this is what makes Stop take effect mid-build instead of after it."""
    try:
        p = subprocess.Popen(["vagrant", *args], cwd=str(run_dir), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                             creationflags=NOWIN, start_new_session=not WINDOWS)
    except FileNotFoundError as e:
        return 127, "", str(e)
    timed_out, cancelled, done = threading.Event(), threading.Event(), threading.Event()

    def kill_on_timeout():
        timed_out.set()
        _kill_tree(p)

    def watch_cancel():
        while not done.wait(1.0):
            if cancel():
                cancelled.set()
                _kill_tree(p)
                return
    timer = threading.Timer(timeout, kill_on_timeout)
    timer.start()
    if cancel:
        threading.Thread(target=watch_cancel, daemon=True).start()
    lines = []
    try:
        for raw in p.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if on_line:
                on_line(line)
        p.wait()
    finally:
        done.set()
        timer.cancel()
        p.stdout.close()
    if cancelled.is_set():
        return CANCELLED_RC, "\n".join(lines), "cancelled"
    if timed_out.is_set():
        return 124, "\n".join(lines), "timed out"
    return p.returncode, "\n".join(lines), ""


PORT_COLLISION_RE = re.compile(r"forwarded port to (\d+) is already in use")
PORT_COLLISION_RETRIES = 3


def port_collision(output):
    """The host port Vagrant refused because something else was answering on it, or None. The port was free when
    allocated (allocate_port checks), but on a desktop something can take it before the VM boots - on Windows,
    for example, a port forwarded from WSL by an editor. The caller re-allocates that VM's port and runs `up` again."""
    m = PORT_COLLISION_RE.search(output or "")
    return int(m.group(1)) if m else None


def vagrant_retry(run_dir, *args, tries=3, delay=5, timeout=180):
    """A vagrant command right after an interrupted `up`: VirtualBox can still hold the session lock of the operation
    that was killed for a few seconds, so retry a little before giving up."""
    rc, out, err = 1, "", ""
    for i in range(tries):
        rc, out, err = vagrant(run_dir, *args, timeout=timeout)
        if rc == 0:
            break
        time.sleep(delay)
    return rc, out, err


def destroy_after_cancel(run_dir, tries=3, delay=5, timeout=180):
    return vagrant_retry(run_dir, "destroy", "-f", tries=tries, delay=delay, timeout=timeout)


def write_seed_files(run_dir, seed):
    """`seed`: {relative_path: content}. Staged on the host, copied into the VM after boot (see scp_to)."""
    d = run_dir / "seed"
    d.mkdir(exist_ok=True)
    for rel, content in (seed or {}).items():
        # checked as plain strings, not just Path(rel).is_absolute(): that call is OS-dependent and on Windows
        # does NOT treat a leading "/" (a POSIX absolute path) as absolute, which would under-reject here.
        if ".." in Path(rel).parts or Path(rel).is_absolute() or rel.startswith(("/", "\\")):
            raise ValueError(f"bad seed path: {rel}")
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_lf(p, content)
    return d


# ---------------------------------------------------------------- host-side SSH (scoring + seeding; the agent uses
# its OWN ssh client inside its own container for the actual task, this is only for panel-driven setup/scoring)
def _ssh_base(port, key_path):
    # Regression (found via real boot testing, see .claude/skills/vm-lab-dev/SKILL.md): the ternary used to be
    # "UserKnownHostsFile=NUL" if ... else "/dev/null" - the "UserKnownHostsFile=" prefix only applied to the
    # Windows branch, so on macOS/Linux this passed the bare string "/dev/null" as its own -o argument. ssh then
    # tried to parse "/dev/null" itself as a config keyword and failed every single attempt with "command-line
    # line 0: no argument after keyword /dev/null" - a 100% reproducible failure that looked like flaky VM
    # boot/SSH timing (always blamed on whichever node was checked first) until the actual stderr was logged.
    known_hosts = "NUL" if sys.platform.startswith("win") else "/dev/null"
    return ["ssh", "-i", str(key_path), "-p", str(port), "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]


def ssh_wait(port, key_path, tries=60, delay=2, on_attempt=None, cancel=None):
    """Poll until the VM accepts SSH (cloud-image boots can take a little while after VirtualBox reports 'running').
    `on_attempt(i, rc, err)`, if given, is called after every attempt (success or failure) so a caller can log
    *why* each attempt failed - the rc/stdout alone (what callers got before) discards the one piece of
    information (ssh's own stderr) that actually explains a failure, which made a real intermittent multi-VM
    failure undiagnosable until this was added. `cancel`, if given, is checked before each attempt (returns False)."""
    for i in range(tries):
        if cancel and cancel():
            return False
        rc, out, err = _run([*_ssh_base(port, key_path), "bench@127.0.0.1", "true"], timeout=8)
        if on_attempt:
            on_attempt(i, rc, err)
        if rc == 0:
            return True
        time.sleep(delay)
    return False


def ssh_run(port, key_path, command, timeout=120):
    return _run([*_ssh_base(port, key_path), "bench@127.0.0.1", command], timeout=timeout)


def ssh_script(port, key_path, script, timeout=60):
    """Run a multi-line script on the VM: sent on stdin to `bash -s`, so nothing in it is reinterpreted by a local
    shell or by command-line quoting (see vmrun_script for the same reason)."""
    return _run([*_ssh_base(port, key_path), "bench@127.0.0.1", "bash -s"], input=script, timeout=timeout)


# ---------------------------------------------------------------- config snapshots (what's configured on a lab node)
# One section per thing a network engineer would want to review or diff. Output that changes on its own is left out
# so a diff between two snapshots shows only real changes: no full `ip addr` (DHCP lease countdowns on the setup NIC),
# no iptables-save comment lines (timestamps). Service configs are read only if the file exists on that node.
SNAPSHOT_SECTION = "### "
SNAPSHOT_SCRIPT = r"""
sec() { echo "### $1"; }
sec addresses; ip -br addr
sec links; ip -br link
sec routes; ip route show; echo "# ipv6"; ip -6 route show
sec forwarding; sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding 2>/dev/null
if command -v bridge >/dev/null; then
  sec bridge
  for b in /sys/class/net/*/bridge; do [ -d "$b" ] && echo "$(basename "$(dirname "$b")") vlan_filtering $(cat "$b/vlan_filtering")"; done
  bridge link 2>/dev/null; echo "# vlans"; bridge vlan show 2>/dev/null
fi
sec "vlan interfaces"
ip -o -d link show type vlan 2>/dev/null | sed -E 's/^[0-9]+: ([^:]+):.* vlan protocol ([^ ]+) id ([0-9]+).*/\1 \2 \3/'
if command -v nft >/dev/null; then sec nftables; sudo -n nft list ruleset 2>&1; fi
if command -v iptables-save >/dev/null; then sec iptables; sudo -n iptables-save 2>&1 | grep -v '^#'; fi
if command -v vtysh >/dev/null; then
  sec frr; sudo -n vtysh -c 'show running-config' 2>&1 | grep -v -e '^Building configuration' -e '^Current configuration:'
fi
sec services
for s in dnsmasq nginx frr; do systemctl cat "$s" >/dev/null 2>&1 && echo "$s $(systemctl is-active "$s")"; done
for f in /etc/dnsmasq.conf /etc/dnsmasq.d/*.conf /etc/netplan/*.yaml /etc/frr/frr.conf /etc/frr/daemons /etc/nginx/nginx.conf /etc/nginx/conf.d/*.conf          /etc/nginx/sites-enabled/* /etc/nginx/streams-enabled/*; do
  [ -f "$f" ] && { sec "file $f"; sudo -n cat "$f" 2>&1; }
done
true
"""


# Countdowns the kernel prints and decrements on its own, e.g. on IPv6 routes learned from router advertisements on
# the setup NIC ("... proto ra metric 100 expires 86197sec"). Left in, every node would show a change in every diff.
# nftables rule counters ("counter packets 12 bytes 1008") count traffic, so they're reduced to plain "counter".
SNAPSHOT_VOLATILE = re.compile(r" expires \d+sec|(?<=counter) packets \d+ bytes \d+")


def parse_snapshot(text):
    """SNAPSHOT_SCRIPT output -> {section: text}. Anything before the first section header is ignored; volatile
    countdowns (SNAPSHOT_VOLATILE) are removed so two snapshots of an unchanged node are identical."""
    sections, name, lines = {}, None, []
    for line in (SNAPSHOT_VOLATILE.sub("", l) for l in text.splitlines()):
        if line.startswith(SNAPSHOT_SECTION):
            if name is not None:
                sections[name] = "\n".join(lines).strip() + "\n"
            name, lines = line[len(SNAPSHOT_SECTION):].strip(), []
        elif name is not None:
            lines.append(line.rstrip())
    if name is not None:
        sections[name] = "\n".join(lines).strip() + "\n"
    return sections


# ---------------------------------------------------------------- lab files (a lab you can save, share and rebuild)
# {"format", "version", "title", "topology": {"nodes": [{name, role}], "links": [{a, b}]}, "configs": {node:
# {snapshot section: text}}, "intents": [...]}. The node and link order is kept exactly: it decides each node's NIC
# order, so interface names in the configs (enp0s8, enp0s9, ...) mean the same thing in the rebuilt lab.
# "intents" is reserved for the verification work (reach/block checks) and carried through unchanged for now.
LABFILE_FORMAT = "aiagentplayground-lab"
LABFILE_VERSION = 1
LABFILE_MAX_LINKS = 64
LABFILE_MAX_TEXT = 200_000
SETUP_IFACES = ("lo", "enp0s3")          # loopback and the NAT/setup NIC: never part of a lab's config
APPLY_FILE_RE = re.compile(r"^/etc/((nginx|frr|dnsmasq\.d)/[A-Za-z0-9._/-]+|dnsmasq\.conf)$")   # service configs a lab file may restore
APPLY_SERVICES = ("dnsmasq", "nginx", "frr")
APPLY_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:/@%-]+$")                  # one word of an `ip` address/route
APPLY_SYSCTL_RE = re.compile(r"^(net\.[a-z0-9_.]+) = (-?\d+)$")
APPLY_SKIP_PROTOS = ("kernel", "dhcp", "ra")                           # routes the system creates on its own
# routes a routing daemon (FRR) installs; they come back from its restored config, so they're not restored by hand
DAEMON_PROTOS = ("ospf", "ospf6", "bgp", "isis", "rip", "ripng", "eigrp", "babel", "zebra", "static", "openfabric")


def build_labfile(title, topology, configs, intents=None, source=None):
    return {"format": LABFILE_FORMAT, "version": LABFILE_VERSION, "title": title,
            "topology": {"nodes": [{"name": n["name"], "role": n["role"]} for n in topology["nodes"]],
                         "links": [{"a": l["a"], "b": l["b"]} for l in topology["links"]]},
            "configs": configs, "intents": list(intents or []), "source": source or {}}


def parse_labfile(obj):
    """Validate a lab file (it may come from someone else) -> (title, topology, configs, intents)."""
    if not isinstance(obj, dict) or obj.get("format") != LABFILE_FORMAT:
        raise ValueError("not an AI Agent Playground lab file")
    if obj.get("version") != LABFILE_VERSION:
        raise ValueError(f"unsupported lab file version {obj.get('version')!r} (this panel reads version {LABFILE_VERSION})")
    t = obj.get("topology")
    if not isinstance(t, dict) or not isinstance(t.get("nodes"), list) or not isinstance(t.get("links"), list):
        raise ValueError("the lab file has no topology")
    try:
        nodes = [{"name": str(n["name"]), "role": str(n["role"])} for n in t["nodes"]]
        links = [{"a": str(l["a"]), "b": str(l["b"])} for l in t["links"]]
    except (KeyError, TypeError):
        raise ValueError("the lab file's topology is malformed")
    if not 2 <= len(nodes) <= MAX_CUSTOM_NODES:
        raise ValueError(f"a lab needs 2 to {MAX_CUSTOM_NODES} nodes (the file has {len(nodes)})")
    if len(links) > LABFILE_MAX_LINKS:
        raise ValueError(f"too many links ({len(links)})")
    title = str(obj.get("title") or "Lab from file")[:120]
    topology = validate_topology({"id": "custom", "title": f"From file: {title}", "nodes": nodes, "links": links})
    names = {n["name"] for n in nodes}
    configs = obj.get("configs") or {}
    if not isinstance(configs, dict) or not all(isinstance(v, dict) for v in configs.values()):
        raise ValueError("the lab file's configs are malformed")
    unknown = set(configs) - names
    if unknown:
        raise ValueError(f"configs for nodes not in the topology: {', '.join(sorted(unknown))}")
    for node, secs in configs.items():
        for sec, text in secs.items():
            if not isinstance(text, str) or len(text) > LABFILE_MAX_TEXT:
                raise ValueError(f"config section '{sec}' of {node} is not text or is too large")
    intents = obj.get("intents") or []
    if not isinstance(intents, list) or len(intents) > 500:
        raise ValueError("the lab file's intents are malformed")
    return title, topology, {n: {s: t for s, t in secs.items() if s != "_error"} for n, secs in configs.items()}, intents


def _route_proto(toks):
    return toks[toks.index("proto") + 1] if "proto" in toks and toks.index("proto") + 1 < len(toks) else None


def _usable(text):
    return text.strip() and not text.lstrip().startswith(("sudo:", "bash:")) and "command not found" not in text


def render_apply_script(sections):
    """A bash script that puts a node's saved config (snapshot sections) back: addresses and link state on lab
    interfaces, static routes, forwarding sysctls, the nftables ruleset (or iptables rules) and nginx/FRR config
    files. Everything the system creates by itself (setup NIC, kernel/DHCP/RA routes, link-local addresses) is left
    alone. Every value is checked and quoted, and file and ruleset contents travel base64-encoded, so a lab file from
    someone else can't inject shell commands. Returns (script, skipped lines)."""
    q, cmds, skipped = shlex.quote, [], []
    cmds += _vlan_commands(sections, skipped)
    for line in sections.get("addresses", "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        iface = parts[0].split("@")[0]
        if iface in SETUP_IFACES or not APPLY_TOKEN_RE.match(iface):
            continue
        for addr in parts[2:]:
            if "/" not in addr or addr.lower().startswith("fe80:"):
                continue
            if APPLY_TOKEN_RE.match(addr):
                cmds.append(f"sudo -n ip addr replace {q(addr)} dev {q(iface)}")
            else:
                skipped.append(f"address {addr} on {iface}")
        if parts[1] in ("UP", "UNKNOWN"):
            cmds.append(f"sudo -n ip link set {q(iface)} up")
    v6 = False
    for line in sections.get("routes", "").splitlines():
        if line.startswith("# ipv6"):
            v6 = True
            continue
        toks = line.split()
        if not toks or line.startswith("#") or toks[0] in ("broadcast", "local", "multicast", "anycast"):
            continue
        if any(t in SETUP_IFACES for t in toks) or _route_proto(toks) in APPLY_SKIP_PROTOS + DAEMON_PROTOS:
            continue
        if not all(APPLY_TOKEN_RE.match(t) for t in toks):
            skipped.append(f"route: {line.strip()}")
            continue
        cmds.append(f"sudo -n ip {'-6 ' if v6 else ''}route replace " + " ".join(q(t) for t in toks))
    for line in sections.get("forwarding", "").splitlines():
        m = APPLY_SYSCTL_RE.match(line.strip())
        if m:
            cmds.append(f"sudo -n sysctl -q -w {q(m.group(1) + '=' + m.group(2))}")
    b64 = lambda text: base64.b64encode(text.encode()).decode()
    nft, ipt = sections.get("nftables", ""), sections.get("iptables", "")
    if _usable(nft):
        cmds.append(f"echo {b64('flush ruleset' + chr(10) + nft)} | base64 -d | sudo -n nft -f -")
    elif _usable(ipt):
        cmds.append(f"echo {b64(ipt)} | base64 -d | sudo -n iptables-restore")
    reload_nginx = restart_frr = False
    frr = sections.get("frr", "")
    if _usable(frr):                                      # FRR's running config: what was really in effect
        cmds.append(f"sudo -n mkdir -p /etc/frr && echo {b64(frr)} | base64 -d | sudo -n tee /etc/frr/frr.conf >/dev/null")
        restart_frr = True
    for sec, text in sections.items():
        if not sec.startswith("file "):
            continue
        path = sec[5:].strip()
        if path == "/etc/frr/frr.conf" and _usable(frr):
            continue                                      # the saved file can be stale; the running config wins
        if not APPLY_FILE_RE.match(path) or ".." in path:
            if not path.startswith("/etc/netplan/"):           # netplan is setup NIC config: deliberately not restored
                skipped.append(f"file {path}")
            continue
        cmds.append(f"sudo -n mkdir -p {q(os.path.dirname(path))} && echo {b64(text)} | base64 -d | sudo -n tee {q(path)} >/dev/null")
        reload_nginx |= path.startswith("/etc/nginx/")
        restart_frr |= path.startswith("/etc/frr/")
    if reload_nginx:
        cmds.append("sudo -n nginx -t -q && sudo -n systemctl reload nginx")
    if restart_frr:
        cmds.append("sudo -n systemctl restart frr")
    for line in sections.get("services", "").splitlines():         # services that were running are running again
        toks = line.split()
        if len(toks) == 2 and toks[0] in APPLY_SERVICES and toks[1] == "active":
            cmds.append(f"sudo -n systemctl enable -q {toks[0]} && sudo -n systemctl restart {toks[0]}")
    body = "".join(f"{c} || echo {q('FAILED: ' + c[:200])}\n" for c in cmds)
    return f"{body}echo done\n", skipped


VLAN_PROTOS = ("802.1Q", "802.1ad")
VLAN_IF_RE = re.compile(r"^([A-Za-z0-9._-]+)@([A-Za-z0-9._-]+) (802\.1Q|802\.1ad) (\d+)$")


def parse_bridge_vlans(text):
    """The "bridge" snapshot section -> ({bridge: vlan_filtering 0/1}, {port: [(vid, pvid, untagged), ...]}).
    `bridge vlan show` prints a port's first VLAN on its own line and each further one on an indented line."""
    filtering, table, port, in_vlans = {}, {}, None, False
    for line in text.splitlines():
        toks = line.split()
        if not toks:
            continue
        if line.startswith("# vlans"):
            in_vlans = True
            continue
        if not in_vlans:
            if len(toks) == 3 and toks[1] == "vlan_filtering" and toks[2] in ("0", "1"):
                filtering[toks[0]] = int(toks[2])
            continue
        if toks[0] == "port":                              # the table's header
            continue
        if not line[0].isspace():
            port, toks = toks[0], toks[1:]
        if port is None or not toks or not toks[0].isdigit():
            continue
        table.setdefault(port, []).append((int(toks[0]), "PVID" in toks, "Untagged" in toks))
    return filtering, table


def _vlan_commands(sections, skipped):
    """Commands that recreate 802.1Q subinterfaces (e.g. enp0s8.10 on a router) and a bridge's VLAN setup (filtering
    on, then each port's VLANs: an access port is "pvid untagged", a trunk carries tagged VLANs)."""
    q, cmds = shlex.quote, []
    for line in sections.get("vlan interfaces", "").splitlines():
        if not line.strip():
            continue
        m = VLAN_IF_RE.match(line.strip())
        if not m or not 1 <= int(m.group(4)) <= 4094 or m.group(2) in SETUP_IFACES:
            skipped.append(f"vlan interface: {line.strip()}")
            continue
        name, parent, proto, vid = m.groups()
        cmds.append(f"ip link show {q(name)} >/dev/null 2>&1 || sudo -n ip link add link {q(parent)} name {q(name)} "
                    f"type vlan protocol {proto} id {vid}")
        cmds.append(f"sudo -n ip link set {q(name)} up")
    filtering, table = parse_bridge_vlans(sections.get("bridge", ""))
    for br, on in filtering.items():
        if APPLY_TOKEN_RE.match(br):
            cmds.append(f"sudo -n ip link set {q(br)} type bridge vlan_filtering {on}")
    if any(filtering.values()):                            # with filtering off the table is the default, nothing to do
        for port, vlans in table.items():
            if not APPLY_TOKEN_RE.match(port) or port in SETUP_IFACES:
                continue
            self_ = " self" if port in filtering else ""     # the bridge's own entry
            for vid, pvid, untagged in vlans:
                if 1 <= vid <= 4094:
                    cmds.append(f"sudo -n bridge vlan add dev {q(port)} vid {vid}" + (" pvid" if pvid else "")
                                + (" untagged" if untagged else "") + self_)
            if 1 not in [v[0] for v in vlans]:
                cmds.append(f"sudo -n bridge vlan del dev {q(port)} vid 1{self_}")
    return cmds


def _compare_view(sec, text):
    """What has to match between the lab file and the rebuilt node: lab interfaces and static routes only (MACs,
    link-local and setup-NIC addresses legitimately differ between two VMs)."""
    lines = []
    for line in SNAPSHOT_VOLATILE.sub("", text).splitlines():
        toks = line.split()
        if not toks:
            continue
        if sec == "addresses":
            if toks[0].split("@")[0] in SETUP_IFACES:
                continue
            toks = [t for t in toks if not t.lower().startswith("fe80:")]
        elif sec == "routes":
            if any(t in SETUP_IFACES for t in toks) or "fe80::/64" in toks or _route_proto(toks) in DAEMON_PROTOS:
                continue                                  # daemon-learned routes reconverge on their own time
        lines.append(" ".join(toks))
    if sec == "bridge":                                   # the VLAN setup only (port state/cost lines can vary)
        filtering, table = parse_bridge_vlans(text)
        return sorted([f"{b} vlan_filtering {v}" for b, v in filtering.items()] +
                      [f"{p} {vid} {pv} {un}" for p, vl in table.items() for vid, pv, un in vl])
    return sorted(lines)


def config_mismatches(wanted, live):
    """Sections of a node's lab-file config that the rebuilt node doesn't match (addresses, routes, forwarding,
    firewall). [] means the rebuild is faithful."""
    out = []
    for sec in ("addresses", "routes", "forwarding", "nftables", "frr", "bridge", "vlan interfaces", "services"):
        if sec in wanted and _usable(wanted[sec]) and _compare_view(sec, wanted[sec]) != _compare_view(sec, live.get(sec, "")):
            out.append(sec)
    return out


def scp_to(port, key_path, local_path, remote_path, timeout=60):
    return _run(["scp", "-i", str(key_path), "-P", str(port), "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=NUL" if sys.platform.startswith("win") else "/dev/null",
                "-r", str(local_path), f"bench@127.0.0.1:{remote_path}"], timeout=timeout)


def scp_from(port, key_path, remote_path, local_path, timeout=60):
    return _run(["scp", "-i", str(key_path), "-P", str(port), "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=NUL" if sys.platform.startswith("win") else "/dev/null",
                f"bench@127.0.0.1:{remote_path}", str(local_path)], timeout=timeout)


CAPTURE_MAX_PACKETS = 20000


def capture_command(iface, seconds, remote_file):
    """The shell that captures `iface` for `seconds` into `remote_file` on a node. tcpdump gets SIGINT from timeout so
    it flushes the file; the file is then made readable for scp, and the caller copies it out. The caller checks the
    interface exists first (see the capture code in app.py), so `iface` here is already a safe name."""
    return (f"sudo -n rm -f {remote_file}; "
            f"sudo -n timeout -s INT {int(seconds)} tcpdump -i {iface} -nn -s 0 -c {CAPTURE_MAX_PACKETS} "
            f"-w {remote_file} >/dev/null 2>&1; sudo -n chmod 644 {remote_file} 2>/dev/null; test -s {remote_file}")


# ---------------------------------------------------------------- task catalog
TASK_FILE = Path(__file__).resolve().parent / "vm_tasks.json"
TASK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


def load_tasks():
    data = json.loads(TASK_FILE.read_text(encoding="utf-8"))
    for t in data["tasks"]:
        if not TASK_ID_RE.match(t["id"]):
            raise ValueError(f"bad task id in catalog: {t['id']}")
    return data["tasks"]


def get_task(task_id):
    t = next((x for x in load_tasks() if x["id"] == task_id), None)
    if not t:
        raise KeyError(f"unknown task '{task_id}'")
    return t


# ---------------------------------------------------------------- network-topology labs (routers/switches/hosts)
# A topology is a small GROUP of VMs wired together: each link is its own VirtualBox internal network ("intnet"),
# scoped to one run and never bridged to the host LAN or shared with any other run or agent - same isolation
# invariant as the single-VM benchmarks above, just applied to a group. A "switch" node is a real VM too (not a
# bare intnet standing in for one): it gets one extra NIC per link it terminates and bridges them together in the
# kernel, config-free by default. Routers and hosts get one NIC per link they're party to, left with no address
# so the task is actually configuring them, not finding them pre-configured.
TOPO_SSH_PORT_RANGE = (62400, 62599)    # one per node, across all concurrently-provisioning topology runs
TOPO_TERM_PORT_RANGE = (62600, 62799)   # one per active per-node web terminal
NODE_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,14}$")
NODE_ROLES = ("router", "switch", "host", "loadbalancer", "firewall", "server", "upstream")
TOPOLOGY_ID_RE = TASK_ID_RE             # same shape; kept as a separate name so the two catalogs can diverge later
TOPOLOGY_FILE = Path(__file__).resolve().parent / "vm_topologies.json"
TOPOLOGY_TASK_FILE = Path(__file__).resolve().parent / "vm_topology_tasks.json"


def validate_topology(t):
    if not TOPOLOGY_ID_RE.match(t["id"]):
        raise ValueError(f"bad topology id: {t['id']}")
    names = [n["name"] for n in t["nodes"]]
    if len(names) != len(set(names)):
        raise ValueError(f"{t['id']}: duplicate node name")
    for n in t["nodes"]:
        if not NODE_NAME_RE.match(n["name"]):
            raise ValueError(f"{t['id']}: bad node name '{n['name']}'")
        if n["role"] not in NODE_ROLES:
            raise ValueError(f"{t['id']}: bad role '{n['role']}' for node '{n['name']}'")
    for link in t["links"]:
        if link["a"] not in names or link["b"] not in names:
            raise ValueError(f"{t['id']}: link references an unknown node")
        if link["a"] == link["b"]:
            raise ValueError(f"{t['id']}: self-link on '{link['a']}'")
    return t


def load_topologies():
    data = json.loads(TOPOLOGY_FILE.read_text(encoding="utf-8"))
    topos = [validate_topology(t) for t in data["topologies"]]
    ids = [t["id"] for t in topos]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate topology id in catalog")
    return topos


def get_topology(topology_id):
    t = next((x for x in load_topologies() if x["id"] == topology_id), None)
    if not t:
        raise KeyError(f"unknown topology '{topology_id}'")
    return t


def load_topology_tasks():
    data = json.loads(TOPOLOGY_TASK_FILE.read_text(encoding="utf-8"))
    topo_ids = {t["id"]: t for t in load_topologies()}
    tasks = data["tasks"]
    ids = [t["id"] for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate topology task id in catalog")
    for t in tasks:
        if not TASK_ID_RE.match(t["id"]):
            raise ValueError(f"bad topology task id: {t['id']}")
        topo = topo_ids.get(t["topology_id"])
        if not topo:
            raise ValueError(f"{t['id']}: unknown topology_id '{t['topology_id']}'")
        node_names = {n["name"] for n in topo["nodes"]}
        if t["check_node"] not in node_names:
            raise ValueError(f"{t['id']}: check_node '{t['check_node']}' is not a node of {t['topology_id']}")
    return tasks


def get_topology_task(task_id):
    t = next((x for x in load_topology_tasks() if x["id"] == task_id), None)
    if not t:
        raise KeyError(f"unknown topology task '{task_id}'")
    return t


def links_for_node(topology, name):
    """Links touching `name`, in catalog order - this order IS the node's NIC order (nic1 is always NAT/setup)."""
    return [link for link in topology["links"] if name in (link["a"], link["b"])]


# VirtualBox NIC n (n >= 2: lab links) -> its Linux name in the Ubuntu guests: adapters 1-4 sit at PCI slots 3, 8, 9,
# 10 and 5-8 at 16-19 (nic1 is always the NAT/setup enp0s3). Used only when a node's real interface names aren't known
# from a snapshot; nodes with up to 3 lab links (enp0s8-10) are what's been seen on real labs.
NIC_PCI_SLOTS = (3, 8, 9, 10, 16, 17, 18, 19)


def lab_iface_names(topology, name, snapshot_links=""):
    """Interface name for each of `name`'s links, in link order. From the node's snapshot (`ip -br link`: its
    physical lab NICs, sorted) when there is one, else from NIC_PCI_SLOTS."""
    n = len(links_for_node(topology, name))
    seen = sorted({int(m.group(1)) for m in re.finditer(r"^enp0s(\d+)(?:\s|$)", snapshot_links or "", re.M)} - {3})
    if len(seen) >= n:
        return [f"enp0s{i}" for i in seen[:n]]
    return [f"enp0s{NIC_PCI_SLOTS[i + 1]}" if i + 1 < len(NIC_PCI_SLOTS) else f"nic{i + 2}" for i in range(n)]


def topology_diagram(topology, snapshot_nodes=None):
    """What the panel draws: nodes, links with each end's interface name, and per-node interface addresses (lab ones
    only: no loopback, setup NIC or link-local) from a snapshot's "addresses" section, if given."""
    snapshot_nodes = snapshot_nodes or {}
    ifaces = {n["name"]: lab_iface_names(topology, n["name"], snapshot_nodes.get(n["name"], {}).get("links", ""))
              for n in topology["nodes"]}
    pos = {n["name"]: 0 for n in topology["nodes"]}
    links = []
    for l in topology["links"]:
        ends = {}
        for side in ("a", "b"):
            node = l[side]
            ends[f"{side}_if"] = ifaces[node][pos[node]] if pos[node] < len(ifaces[node]) else None
            pos[node] += 1
        links.append({"a": l["a"], "b": l["b"], **ends})
    addresses = {}
    for node, secs in snapshot_nodes.items():
        for line in secs.get("addresses", "").splitlines():
            toks = line.split()
            if len(toks) < 3 or toks[0].split("@")[0] in SETUP_IFACES:
                continue
            addrs = [t for t in toks[2:] if "/" in t and not t.lower().startswith("fe80:")]
            if addrs:
                addresses.setdefault(node, {})[toks[0].split("@")[0]] = addrs
    return {"nodes": [{"name": n["name"], "role": n["role"]} for n in topology["nodes"]], "links": links,
            "addresses": addresses}


def intnet_name(rid, idx):
    return f"aiagentplayground-topo-{rid}-link{idx}"


def vm_name_for_node(rid, name):
    return f"aiagentplayground-vmtopo-{rid}-{name}"


def vmrun_script(key_path, port, host, log_path):
    """The `vmrun` wrapper put in an agent's workspace: runs commands on a VM through its relay and logs them.

    Two ways to call it, because the agent's own shell expands $variables inside double quotes BEFORE the wrapper
    runs. A script passed as `./vmrun "echo $i"` arrives as `echo ` - found when an agent's FizzBuzz printed blank
    lines for every number:
      ./vmrun 'one-line command'      argument(s) form; single quotes keep $ for the VM's shell
      ./vmrun <<'EOF' ... EOF         no arguments: the script is read from stdin and run with `bash -s` on the VM;
                                      a quoted heredoc delimiter means nothing is expanded locally
    """
    ssh = (f"ssh -i {key_path} -p {port} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
           f"-o LogLevel=ERROR {host}")
    return ("#!/bin/sh\n"
            f"LOG={log_path}\n"
            "ts=\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"\n"
            "if [ $# -gt 0 ]; then\n"
            "  echo \"=== $ts \\$ $*\" >> \"$LOG\"\n"
            f"  {ssh} \"$@\" 2>&1 | tee -a \"$LOG\"\n"
            "  exit 0\n"
            "fi\n"
            "if [ -t 0 ]; then\n"
            "  echo \"usage: $0 'command'   or   $0 <<'EOF' (script lines) EOF\" >&2; exit 2\n"
            "fi\n"
            "script=\"$(cat)\"\n"
            "if [ -z \"$script\" ]; then\n"
            "  echo \"usage: $0 'command'   or   $0 <<'EOF' (script lines) EOF\" >&2; exit 2\n"
            "fi\n"
            "printf '=== %s $ (script on stdin)\\n%s\\n' \"$ts\" \"$script\" >> \"$LOG\"\n"
            f"printf '%s\\n' \"$script\" | {ssh} 'bash -s' 2>&1 | tee -a \"$LOG\"\n")


# Sent to the agent with every VM/lab prompt, so kept short (each word costs tokens on every run).
VMRUN_HOWTO = ("Use single quotes for one-liners; send longer scripts or anything with $ as a quoted heredoc "
               "(arrives unchanged): {cmd} <<'EOF' ... EOF")


def relay_port_for_node(topology, name):
    """Deterministic container-internal port (on the agent's own docker network) for this node's SSH relay -
    independent of the node's host-forwarded SSH port, and collision-free within one topology by construction."""
    names = [n["name"] for n in topology["nodes"]]
    return 2200 + names.index(name)


def relay_command(topology, node_ports):
    """The shell command the single vm-relay-topo container runs: one backgrounded socat listener per node,
    then `wait`. Each term already ends in its own "&", which already plays the separator role "; " would -
    joining with "; " instead of a plain space produces "...&; socat..." ("&;" is a shell syntax error) and
    crashes the whole relay container on every single start, regardless of node count. Found via a real agent
    test: the agent correctly reported "Could not resolve hostname vm-relay-topo", since a crashed,
    non-restarting container's network alias doesn't stay resolvable."""
    return " ".join(f"socat TCP-LISTEN:{relay_port_for_node(topology, name)},fork,reuseaddr "
                    f"TCP:host.docker.internal:{port} &" for name, port in node_ports.items()) + " wait"


# ---------------------------------------------------------------- user-built custom topologies ("structured
# builder": pick role counts + a wiring pattern, instead of a named catalog template). The output is a plain
# {id, title, nodes, links} dict - identical in shape to a catalog entry and run through the exact same
# validate_topology() - so render_topology_vagrantfile(), the lifecycle in app.py, and the frontend never need to
# know or care whether a topology came from vm_topologies.json or was built here.
MAX_CUSTOM_NODES = 12                   # safety cap: this project's "lightweight, bounded" posture applies here too
ROLE_PREFIX = {"switch": "sw", "router": "r", "firewall": "fw", "loadbalancer": "lb", "host": "h", "server": "srv",
               "upstream": "up"}
ROLE_ORDER = ("switch", "router", "firewall", "loadbalancer", "server", "upstream", "host")   # naming / summary order


def _named_nodes(counts):
    nodes = []
    for role in ROLE_ORDER:                                    # structural roles first, for readable names
        n = int(counts.get(role, 0) or 0)
        if n < 0:
            raise ValueError(f"count for '{role}' can't be negative")
        for i in range(1, n + 1):
            nodes.append({"name": f"{ROLE_PREFIX[role]}{i}", "role": role})
    return nodes


def _star_wiring(nodes):
    switches = [n["name"] for n in nodes if n["role"] == "switch"]
    if not switches:
        raise ValueError("star wiring needs at least one switch")
    hub = switches[0]
    return [{"a": n["name"], "b": hub} for n in nodes if n["name"] != hub]


def _chain_wiring(nodes):
    """Interleaves switches and "inline" nodes (router/firewall) into one line (sw-r-sw-fw-sw...); whichever kind
    runs out first just means the chain continues with the other. "Leaf" nodes (host/loadbalancer) are distributed
    round-robin across the available switches, or onto the end of the chain if there are none."""
    switches = [n["name"] for n in nodes if n["role"] == "switch"]
    inline = [n["name"] for n in nodes if n["role"] in ("router", "firewall")]
    leaves = [n["name"] for n in nodes if n["role"] in ("host", "loadbalancer", "server")]
    edges = [n["name"] for n in nodes if n["role"] == "upstream"]          # the outside world: off the last router
    if not switches and not inline:
        raise ValueError("chain wiring needs at least one switch, router or firewall")
    chain, si, ii = [], 0, 0
    while si < len(switches) or ii < len(inline):
        if si < len(switches):
            chain.append(switches[si]); si += 1
        if ii < len(inline):
            chain.append(inline[ii]); ii += 1
    links = [{"a": chain[i], "b": chain[i + 1]} for i in range(len(chain) - 1)]
    anchors = switches or chain[-1:]
    for i, leaf in enumerate(leaves):
        links.append({"a": leaf, "b": anchors[i % len(anchors)]})
    edge_anchor = ([n for n in chain if n in inline] or chain)[-1]
    for edge in edges:
        links.append({"a": edge_anchor, "b": edge})
    return links


def _manual_wiring(nodes, links):
    if not links:
        raise ValueError("manual wiring needs at least one link")
    return [{"a": l["a"], "b": l["b"]} for l in links]


def build_custom_topology(counts, wiring, links=None):
    """counts: {role: n} for any of NODE_ROLES. wiring: "star" | "chain" |
    "manual" (the last needs `links`, a list of {"a", "b"} pairs naming the generated nodes - the frontend shows
    the generated name list as soon as counts are entered, before the user types links)."""
    nodes = _named_nodes(counts)
    if len(nodes) < 2:
        raise ValueError("a topology needs at least 2 nodes")
    if len(nodes) > MAX_CUSTOM_NODES:
        raise ValueError(f"a custom topology can have at most {MAX_CUSTOM_NODES} nodes (asked for {len(nodes)})")
    if wiring == "star":
        gen_links = _star_wiring(nodes)
    elif wiring == "chain":
        gen_links = _chain_wiring(nodes)
    elif wiring == "manual":
        gen_links = _manual_wiring(nodes, links or [])
    else:
        raise ValueError(f"unknown wiring pattern '{wiring}'")
    summary = ", ".join(f"{sum(1 for n in nodes if n['role'] == role)} {role}"
                        for role in ("host",) + tuple(r for r in ROLE_ORDER if r != "host")
                        if any(n["role"] == role for n in nodes))
    topology = {"id": "custom", "title": f"Custom: {summary} ({wiring})", "nodes": nodes, "links": gen_links}
    return validate_topology(topology)


ROUTER_FRR_SETUP = """
sed -i -E 's/^(ospfd|ospf6d|bgpd)=no/\\1=yes/' /etc/frr/daemons
usermod -aG frrvty,frr bench
systemctl enable frr
systemctl restart frr
"""


SERVER_DNSMASQ_SETUP = """
mkdir -p /etc/dnsmasq.d
cat > /etc/dnsmasq.d/00-lab-defaults.conf <<'CONF'
# Written at lab setup: serve lab interfaces only - never the setup NIC (enp0s3) or loopback, where systemd-resolved
# already listens. Put your lab's DHCP/DNS settings in another file in this folder.
bind-interfaces
except-interface=lo
except-interface=enp0s3
# This node's /etc/hosts maps its own name to a loopback address (127.0.2.1); served over DNS (e.g. with
# expand-hosts) that would answer srv1.lab with 127.0.2.1. Define lab names with address= or host-record= instead.
no-hosts
CONF
apt-get install -y dnsmasq
systemctl disable --now dnsmasq
"""
UPSTREAM_ADDR, UPSTREAM_WEB = "198.51.100.1/30", "203.0.113.10"
UPSTREAM_SETUP = f"""
lab_if="$(ls /sys/class/net | grep -v -e '^lo$' -e '^enp0s3$' | sort -V | head -1)"
ip addr add {UPSTREAM_ADDR} dev "$lab_if"
ip link set "$lab_if" up
ip link add dummy0 type dummy
ip addr add {UPSTREAM_WEB}/32 dev dummy0
ip link set dummy0 up
echo 'upstream ok' > /var/www/html/index.html
"""


def _topo_provision_script(role, pubkey_text):
    common = """#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y {pkgs}
useradd -m -s /bin/bash bench || true
mkdir -p /home/bench/.ssh
echo '{pubkey}' > /home/bench/.ssh/authorized_keys
chmod 700 /home/bench/.ssh && chmod 600 /home/bench/.ssh/authorized_keys
chown -R bench:bench /home/bench
echo 'bench ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/90-bench
{extra}"""
    if role == "switch":
        # Config-free "unmanaged switch" by default: bridge every lab-facing NIC (anything but the NAT nic1,
        # which is always the interface already configured with an address) into one L2 broadcast domain.
        # Deliberately no net.ipv4.ip_forward / ufw here - a switch forwards frames, it has no business routing.
        pkgs = "bridge-utils iproute2 tcpdump"
        # The candidate interface list is captured BEFORE br0 is created, not inline in the for loop: `ls
        # /sys/class/net` run after `ip link add br0` would include br0 itself (it has no address yet, so the
        # "already UP" skip wouldn't catch it either), and `ip link set br0 master br0` fails with "Can not
        # enslave a bridge to a bridge" - hit for real on first VirtualBox boot testing.
        extra = """
ifaces="$(ls /sys/class/net | grep -v '^lo$')"
ip link add name br0 type bridge
for ifc in $ifaces; do
    ip addr show "$ifc" | grep -q ' UP ' && continue    # the NAT interface (nic1) already has an address; leave it
    ip link set "$ifc" master br0
    ip link set "$ifc" up
done
ip link set br0 up
"""
    elif role == "firewall":
        # A dedicated appliance, same footing as "router"/"switch" above - its own VM, its own one job. nftables
        # (the modern, in-kernel, fully CLI-driven Linux firewall - no GUI, no separate product to source) is the
        # only filtering tool installed; no iptables. The service is enabled so `nft` and systemd both work, but
        # its ruleset is left at Ubuntu's shipped (effectively permissive) default, and ip_forward is NOT enabled -
        # same "the task is the configuration" principle as "router": writing a real default-drop policy with
        # explicit accept rules, and enabling forwarding, is the point of the exercise, not a given.
        pkgs = "nftables iproute2 iputils-ping tcpdump"
        extra = "systemctl enable nftables\n"
    elif role == "loadbalancer":
        # Also its own dedicated appliance VM. nginx + the stream module cover both L7 (http) and L4 (tcp/udp)
        # balancing. The default site is left in place (confirms nginx is alive out of the box) but no upstream/
        # proxy config is pre-written - wiring a real backend pool is the task.
        pkgs = "nginx libnginx-mod-stream iproute2 iputils-ping tcpdump"
        extra = ""
    elif role == "server":
        # A service node: dnsmasq (DHCP + DNS), installed but NOT configured or running - serving a lab network is
        # the task. Its defaults file is written before the package is installed, so dnsmasq never binds the setup NIC
        # or loopback (where it would collide with systemd-resolved on 127.0.0.53) - only lab interfaces, once given
        # addresses. Lab settings go in another file in /etc/dnsmasq.d/, then `systemctl enable --now dnsmasq`.
        pkgs = "iproute2 iputils-ping tcpdump dnsutils"
        extra = SERVER_DNSMASQ_SETUP
    elif role == "upstream":
        # A stub internet edge ("the ISP"): its first lab interface is 198.51.100.1/30 (the ISP side of the link, from
        # a documentation range), and it answers on 203.0.113.10 (a dummy interface) with a web page, standing in for
        # a host on the internet. It has no route back to anyone's lab networks, so reaching it from behind a router
        # takes NAT - which is the point. No real internet is involved.
        pkgs = "nginx iproute2 iputils-ping tcpdump"
        extra = UPSTREAM_SETUP
    elif role == "router":
        # Same tooling as a host, plus FRR: zebra with the OSPF, OSPFv3 and BGP daemons enabled but NOT configured -
        # no router-id, no networks, no neighbors - so routing protocols are available through `vtysh` and choosing
        # what to run is the task. bench joins frrvty so `vtysh` works without sudo. Static routes with `ip route`
        # still work exactly as before (zebra leaves kernel routes alone), so existing static-routing tasks are
        # unchanged. Deliberately no pre-enabled ip_forward, same principle as a host.
        pkgs = "iproute2 iputils-ping traceroute tcpdump iptables frr"
        extra = ROUTER_FRR_SETUP
    else:
        # host: enough tooling to assign addresses, add routes, and prove connectivity. Deliberately NO ufw lockdown -
        # unlike the offline coding-task VMs above, this VM's whole point is reaching its neighbors.
        pkgs = "iproute2 iputils-ping traceroute tcpdump isc-dhcp-client curl dnsutils"   # DHCP client, web and DNS tests
        extra = ""
    return common.format(pkgs=pkgs, pubkey=pubkey_text, extra=extra)


def render_topology_vagrantfile(run_dir, rid, topology, node_ports, pubkey_text, memory_mb, cpus):
    """One multi-machine Vagrantfile (a config.vm.define block per node) plus one provision-<node>.sh per node.
    `node_ports`: {node_name: host_ssh_port}. Mirrors render_vagrantfile()'s isolation choices (NAT-only nic1,
    no synced folder, only SSH forwarded and only to 127.0.0.1, clipboard/dnd/audio disabled) for every node, and
    adds one `private_network`/intnet per lab link, with auto_config: false so Vagrant never assigns an address
    itself - the agent (or you) must."""
    links = topology["links"]
    blocks = []
    for node in topology["nodes"]:
        name = node["name"]
        vm_name = vm_name_for_node(rid, name)
        port = node_ports[name]
        prov_file = f"provision-{name}.sh"
        _write_lf(run_dir / prov_file, _topo_provision_script(node["role"], pubkey_text))
        my_link_idxs = [idx for idx, link in enumerate(links) if name in (link["a"], link["b"])]
        net_lines = "\n".join(
            f'    node.vm.network "private_network", virtualbox__intnet: "{intnet_name(rid, idx)}", auto_config: false'
            for idx in my_link_idxs)
        # A switch must receive frames addressed to OTHER MACs on each lab link to bridge them at all - VirtualBox
        # defaults every NIC's promiscuous policy to "deny", which silently drops exactly that return traffic at
        # the hypervisor level (confirmed on real hardware: ARP requests/broadcasts got through fine, since
        # broadcast is never filtered, but unicast replies never did, which looked like a switch/VM networking
        # bug until traced to this). Must be set here, before first boot - setting it live via `VBoxManage
        # controlvm ... nicpromiscN` on an already-running VM does NOT reliably propagate to the internal-network
        # switch fabric (reproduced: required a full VM poweroff/restart, not just a NIC link cycle, to take
        # effect). nic1 is always NAT and is never promiscuous; lab NICs start at nic2.
        promisc_lines = "\n".join(f'      vb.customize ["modifyvm", :id, "--nicpromisc{pos + 2}", "allow-all"]'
                                  for pos in range(len(my_link_idxs))) if node["role"] == "switch" else ""
        blocks.append(f"""
  config.vm.define "{name}" do |node|
    node.vm.hostname = "{name}"
    node.vm.box = "{BOX}"
    node.vm.synced_folder ".", "/vagrant", disabled: true
    node.vm.network "forwarded_port", guest: 22, host: {port}, host_ip: "127.0.0.1", id: "ssh", auto_correct: false
    node.ssh.insert_key = false
{net_lines}
    node.vm.provider "virtualbox" do |vb|
      vb.name = "{vm_name}"
      vb.memory = {memory_mb}
      vb.cpus = {cpus}
      vb.gui = false
      vb.customize ["modifyvm", :id, "--clipboard-mode", "disabled"]
      vb.customize ["modifyvm", :id, "--draganddrop", "disabled"]
      vb.customize ["modifyvm", :id, "--audio-enabled", "off"]
      vb.customize ["modifyvm", :id, "--nic1", "nat"]
{promisc_lines}
      vb.check_guest_additions = false
    end
    node.vm.provision "shell", path: "{prov_file}"
  end
""")
    vagrantfile = f"""# Generated by the control panel. One multi-machine Vagrantfile per network-topology run; destroyed
# afterwards unless "keep" was set. Isolation: every lab link is a VirtualBox internal network scoped to this run
# only (never bridged to the host LAN or shared with any other run/agent); nic1 on every node is NAT, used only
# for initial setup and the host-forwarded SSH port (127.0.0.1 only) - never for lab traffic.
Vagrant.configure("2") do |config|
{"".join(blocks)}
end
"""
    _write_lf(run_dir / "Vagrantfile", vagrantfile)
