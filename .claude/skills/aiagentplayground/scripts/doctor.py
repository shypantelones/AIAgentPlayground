#!/usr/bin/env python3
"""Health and status report for the AI Agent Playground (Windows, macOS, Linux). Read-only.

    python doctor.py                 full report
    python doctor.py --sizes         also list Docker volume sizes (slower)
    python doctor.py --blocked       also show the destinations each running agent's proxy blocked
    python doctor.py --json          machine-readable output

Never prints tokens, API keys or key-store contents. Exit code: 0 all good, 1 warnings, 2 failures.
Standard library only.
"""
import argparse, json, os, platform, re, shutil, socket, subprocess, sys, tempfile, urllib.request
from collections import Counter
from pathlib import Path

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
RESULTS = []


def add(section, name, status, detail=""):
    RESULTS.append({"section": section, "name": name, "status": status, "detail": detail})


def sh(args, timeout=20):
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NOWIN)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return 127, "", str(e)


def find_root():
    cands = []
    if os.environ.get("AIAGENTPLAYGROUND"):
        cands.append(Path(os.environ["AIAGENTPLAYGROUND"]))
    cands += list(Path(__file__).resolve().parents) + [Path.cwd(), *Path.cwd().parents]
    for c in cands:
        if (c / "control-panel" / "app.py").exists():
            return c
    return None


def extend_path():
    extra = ["/usr/local/bin", "/opt/homebrew/bin", "/Applications/Docker.app/Contents/Resources/bin",
             r"C:\Program Files\Docker\Docker\resources\bin", r"C:\Program Files\Oracle\VirtualBox",
             r"C:\Program Files\Vagrant\bin", os.path.expanduser("~/.docker/bin")]
    cur = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(cur + [p for p in extra if p not in cur and os.path.isdir(p)])


def os_label():
    return {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux" if sys.platform.startswith("linux") else sys.platform)


def launcher_for(root):
    osdir = {"Windows": "windows", "macOS": "macos"}.get(os_label(), "linux")
    f = "run.ps1" if IS_WIN else "run.sh"
    return f"platforms/{osdir}/{f}", osdir


# ------------------------------------------------------------------ checks
def check_system(root):
    s = "System"
    add(s, "Operating system", "info", f"{os_label()} ({platform.platform()})")
    v = sys.version_info
    add(s, "Python", "ok" if v >= (3, 8) else "fail", f"{v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 8) else " (need 3.8+)"))
    add(s, "Project folder", "ok" if root else "fail", str(root) if root else "not found (set AIAGENTPLAYGROUND)")
    if root:
        free = shutil.disk_usage(root).free / 1e9
        st = "fail" if free < 3 else "warn" if free < 15 else "ok"
        hint = "" if st == "ok" else "  -> low: models are ~9 GB and Docker images ~15 GB; free space before pulling more"
        add(s, "Free disk (project drive)", st, f"{free:.0f} GB{hint}")


def check_docker():
    s = "Docker"
    docker = shutil.which("docker")
    if not docker:
        add(s, "docker CLI", "fail", "not found. Install Docker Desktop (Windows/macOS) or Docker Engine (Linux)")
        return False
    rc, out, err = sh([docker, "version", "--format", "{{.Server.Version}}"])
    if rc != 0:
        add(s, "Docker daemon", "fail", "not reachable. Start Docker Desktop / the docker service (Linux: is your user in the 'docker' group?)")
        return False
    add(s, "Docker daemon", "ok", f"engine {out}")
    rc, out, _ = sh([docker, "compose", "version", "--short"])
    add(s, "docker compose plugin", "ok" if rc == 0 else "fail", out if rc == 0 else "missing (Linux: install docker-compose-plugin)")
    rc, out, _ = sh([docker, "info", "--format", "{{json .Runtimes}}"])
    has_nv = rc == 0 and "nvidia" in out.lower()
    add(s, "NVIDIA runtime in Docker", "ok" if has_nv else "info", "yes: GPU model server possible" if has_nv else "no: model runs on CPU (or host Ollama on macOS)")
    return True


def check_optional_tools():
    s = "Optional tools"
    for label, exe in (("Ollama (native)", "ollama"), ("nvidia-smi", "nvidia-smi"), ("Vagrant", "vagrant"), ("VirtualBox", "VBoxManage")):
        p = shutil.which(exe)
        add(s, label, "info", p if p else "not installed (only needed for the matching feature)")
    if shutil.which("nvidia-smi"):
        rc, out, _ = sh(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"])
        if rc == 0:
            add(s, "GPU", "info", out)


def check_mode_and_keys(root):
    s = "Configuration"
    if not root:
        return
    sys.path.insert(0, str(root / "control-panel"))
    try:
        import platform_support as ps
        mode = ps.detect_ollama_mode()
        add(s, "Model-server mode", "info", f"{mode} ({ps.MODE_LABEL[mode]})")
        tmp = Path(tempfile.mkdtemp())
        try:
            store = ps.SecretStore(tmp / "x")
            add(s, "API-key storage", "ok" if store.kind != "file" else "warn", store.label)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if mode == "host":
            try:
                urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3)
                add(s, "Ollama on this computer", "ok", "reachable on 127.0.0.1:11434")
            except Exception:
                add(s, "Ollama on this computer", "fail", "host mode needs Ollama running (install from ollama.com, then start it)")
    except Exception as e:  # noqa
        add(s, "platform_support", "warn", f"could not load: {e}")
    osdir = launcher_for(root)[1]
    cfg = root / "platforms" / osdir / "config.env"
    add(s, "Settings file", "info", str(cfg) if cfg.exists() else f"missing: {cfg}")


def panel_state(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=4) as r:
            return json.loads(r.read())
    except Exception:
        return None


def check_panel(root):
    s = "Control panel"
    port = int(os.environ.get("PANEL_PORT", "8765"))
    st = panel_state(port)
    if st is None:
        launch = launcher_for(root)[0] if root else "the launcher in platforms/<os>/"
        add(s, "Panel", "warn", f"not running on 127.0.0.1:{port}. Start it: {launch}")
        return None
    add(s, "Panel", "ok", f"http://127.0.0.1:{port}")
    if st.get("docker") is False:
        add(s, "Panel -> Docker", "fail", st.get("error", "Docker unavailable"))
        return st
    sh_ = st.get("shared", {})
    add(s, "Shared model server", "ok" if sh_.get("running") else "warn",
        f"{sh_.get('modeLabel', '')}; " + ("running" if sh_.get("running") else "stopped (starts automatically with an agent)") +
        (f"; loaded model: {sh_['loaded']}" if sh_.get("loaded") else "") +
        (f"; models: {', '.join(m['name'] for m in sh_.get('models', []))}" if sh_.get("models") else ""))
    if sh_.get("note"):
        add(s, "Model server note", "warn", sh_["note"])
    if st.get("legacy"):
        add(s, "Legacy sandbox", "warn", "the old single sandbox is running and competes for the GPU and port 18789; stop it")
    return st


def check_agents(root, st):
    s = "Agents"
    agents = {}
    if root:
        d = root / "control-panel" / "data" / "instances"
        if d.exists():
            for m in sorted(d.glob("*/meta.json")):
                try:
                    j = json.loads(m.read_text())
                    agents[j["name"]] = {"port": j.get("port"), "backend": j.get("backend", "local"),
                                         "provider": j.get("provider"), "key": (root / "control-panel" / "data" / "secrets" / f"{j['name']}.token").exists()}
                except Exception:
                    pass
    if st is not None:                                    # peer links and live conversations, from the running panel
        try:
            port = int(os.environ.get("PANEL_PORT", "8765"))
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/peers", timeout=4) as r:
                pd = json.loads(r.read())
            live_s = [x for x in pd["sessions"] if x["state"] in ("running", "awaiting")]
            waiting = [x for x in live_s if x["state"] == "awaiting"]
            add("Peering", "Links", "info", f"{len(pd['links'])} link(s)" + (": " + ", ".join(
                f"{l['a']}{'->' if l['mode'] == 'one-way' else '<->'}{l['b']}{'' if l['enabled'] else ' (off)'}{'' if l['approval'] else ' [AUTO]'}" for l in pd["links"]) if pd["links"] else ""))
            add("Peering", "Live conversations", "warn" if waiting else "info",
                f"{len(live_s)} live" + (f"; {len(waiting)} waiting for your approval (open Peering in the panel)" if waiting else ""))
        except Exception:
            pass
    live = {i["name"]: i for i in (st or {}).get("instances", [])}
    if not agents:
        add(s, "Agents", "info", "none created yet (use the panel's Create box)")
    for name, a in agents.items():
        status = live.get(name, {}).get("status", "unknown (panel not running)")
        st_ok = "ok" if status == "healthy" else "warn" if status in ("starting", "stopped", "partial") or status.startswith("unknown") else "info"
        extra = f", cloud: {a['provider']}, key {'stored' if a['key'] else 'MISSING'}" if a["backend"] == "cloud" else ", local model"
        port = a["port"]
        listening = False
        if port:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close(); listening = True
            except OSError:
                pass
        add(s, name, st_ok, f"{status}; dashboard http://127.0.0.1:{port}/ ({'listening' if listening else 'not listening'}){extra}")
    return agents


def check_containers():
    s = "Containers"
    rc, out, _ = sh(["docker", "ps", "-a", "--format",
                     '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}|{{.State}}|{{.Status}}'])
    if rc != 0:
        return {}
    projects = {}
    for line in out.splitlines():
        p = line.split("|", 3)
        if len(p) == 4 and p[0].startswith(("aiagentplayground", "openclaw")):
            projects.setdefault(p[0], []).append((p[1], p[2], p[3]))
    if not projects:
        add(s, "AI Agent Playground containers", "info", "none")
    normal_stop = ("Exited (0)", "Exited (137)", "Exited (143)")      # clean exit or stopped by docker/the panel
    for proj, svcs in sorted(projects.items()):
        bad = [f"{n} ({st})" for n, state, st in svcs
               if (state == "exited" and not st.startswith(normal_stop)) or "unhealthy" in st]
        running = sum(1 for _, state, _ in svcs if state == "running")
        legacy = proj == "openclaw-sandbox"
        if bad:
            status = "warn"
        elif running == len(svcs):
            status = "ok"
        else:
            status = "info"                                            # stopped on purpose
        add(s, proj + (" (legacy single sandbox)" if legacy else ""), status,
            f"{running}/{len(svcs)} running" + ("; stopped" if running == 0 and not bad else "") +
            (f"; problems: {', '.join(bad)}" if bad else ""))
    return projects


def check_storage(show_sizes):
    s = "Storage"
    rc, out, _ = sh(["docker", "volume", "ls", "--format", "{{.Name}}"])
    vols = [v for v in out.splitlines() if v.startswith(("aiagentplayground", "openclaw"))] if rc == 0 else []
    add(s, "AI Agent Playground Docker volumes", "info", f"{len(vols)}: " + ", ".join(vols) if vols else "none")
    rc, out, _ = sh(["docker", "system", "df"], timeout=60)
    if rc == 0:
        for line in out.splitlines()[1:]:
            cols = re.split(r"\s{2,}", line.strip())          # TYPE  TOTAL  ACTIVE  SIZE  RECLAIMABLE
            if len(cols) >= 5:
                add(s, f"Docker {cols[0].lower()}", "info", f"{cols[1]} total, {cols[2]} active, {cols[3]} on disk, {cols[4]} reclaimable")
    if show_sizes:
        rc, out, _ = sh(["docker", "system", "df", "-v"], timeout=120)
        for line in out.splitlines():
            if line.startswith(("aiagentplayground", "openclaw")):
                parts = line.split()
                add(s, "volume " + parts[0], "info", parts[-1])
    if IS_WIN:
        v = Path(os.environ.get("LOCALAPPDATA", "")) / "Docker" / "wsl" / "disk" / "docker_data.vhdx"
        if v.exists():
            add(s, "Docker disk image (Windows)", "info", f"{v} = {v.stat().st_size / 1e9:.1f} GB (grows; shrink via Docker Desktop > Troubleshoot > Clean / Compact)")


def check_blocked(agents, st):
    s = "Blocked requests"
    names = [n for n in agents if (st or {}).get("instances") and any(i["name"] == n and i["status"] in ("healthy", "starting") for i in st["instances"])]
    if not names:
        add(s, "Blocked requests", "info", "no running agents")
    for n in names:
        rc, cid, _ = sh(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project=aiagentplayground-i-{n}", "--filter", "label=com.docker.compose.service=egress-proxy"])
        if rc != 0 or not cid:
            continue
        rc, out, _ = sh(["docker", "exec", cid.split()[0], "tail", "-n", "500", "/var/log/squid/access.log"], timeout=20)
        c = Counter()
        for line in out.splitlines():
            f = line.split()
            if len(f) > 6 and "DENIED" in f[3]:
                c[f[6]] += 1
        add(s, n, "info", ", ".join(f"{d} x{k}" for d, k in c.most_common(5)) if c else "none in the recent log")


# ------------------------------------------------------------------ output
ICON = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "info": "[ .. ]"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true"); ap.add_argument("--sizes", action="store_true"); ap.add_argument("--blocked", action="store_true")
    a = ap.parse_args()
    extend_path()
    root = find_root()
    check_system(root)
    docker_ok = check_docker()
    check_optional_tools()
    check_mode_and_keys(root)
    st = check_panel(root)
    agents = check_agents(root, st)
    if docker_ok:
        check_containers()
        check_storage(a.sizes)
        if a.blocked:
            check_blocked(agents, st)
    if a.json:
        print(json.dumps(RESULTS, indent=2))
    else:
        last = None
        for r in RESULTS:
            if r["section"] != last:
                print(f"\n== {r['section']}")
                last = r["section"]
            print(f"{ICON[r['status']]} {r['name']}: {r['detail']}")
        n_fail = sum(r["status"] == "fail" for r in RESULTS); n_warn = sum(r["status"] == "warn" for r in RESULTS)
        print(f"\nSummary: {n_fail} failure(s), {n_warn} warning(s).")
    sys.exit(2 if any(r["status"] == "fail" for r in RESULTS) else 1 if any(r["status"] == "warn" for r in RESULTS) else 0)


if __name__ == "__main__":
    main()
