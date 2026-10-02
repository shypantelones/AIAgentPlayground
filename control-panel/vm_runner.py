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
import json, os, re, secrets, shutil, socket, subprocess, sys, time
from pathlib import Path

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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
    rc, _, err = _run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "openclaw-vmbench", "-f", str(priv), "-q"], timeout=20)
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
    return ["ssh", "-i", str(key_path), "-p", str(port), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=NUL" if sys.platform.startswith("win") else "/dev/null",
            "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]


def ssh_wait(port, key_path, tries=60, delay=2):
    """Poll until the VM accepts SSH (cloud-image boots can take a little while after VirtualBox reports 'running')."""
    for _ in range(tries):
        rc, out, _ = _run([*_ssh_base(port, key_path), "bench@127.0.0.1", "true"], timeout=8)
        if rc == 0:
            return True
        time.sleep(delay)
    return False


def ssh_run(port, key_path, command, timeout=120):
    return _run([*_ssh_base(port, key_path), "bench@127.0.0.1", command], timeout=timeout)


def scp_to(port, key_path, local_path, remote_path, timeout=60):
    return _run(["scp", "-i", str(key_path), "-P", str(port), "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=NUL" if sys.platform.startswith("win") else "/dev/null",
                "-r", str(local_path), f"bench@127.0.0.1:{remote_path}"], timeout=timeout)


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
