"""OS-specific pieces of the control panel: Docker discovery, secret storage, Ollama mode.

Everything here degrades gracefully: the panel works on Windows, macOS and Linux, and always tells the user
which secret store / Ollama mode it ended up with.
"""
import ctypes, json, os, platform, shutil, subprocess, sys
from pathlib import Path

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
OS_NAME = "Windows" if IS_WIN else "macOS" if IS_MAC else "Linux" if IS_LINUX else sys.platform
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------- docker discovery
_EXTRA_PATHS = {
    "mac": ["/usr/local/bin", "/opt/homebrew/bin", "/Applications/Docker.app/Contents/Resources/bin",
            os.path.expanduser("~/.docker/bin"), os.path.expanduser("~/.rd/bin"), os.path.expanduser("~/.orbstack/bin")],
    "linux": ["/usr/local/bin", "/usr/bin", "/snap/bin"],
    "win": [r"C:\Program Files\Docker\Docker\resources\bin"],
}


def _extend_path():
    key = "win" if IS_WIN else "mac" if IS_MAC else "linux"
    cur = os.environ.get("PATH", "").split(os.pathsep)
    for p in _EXTRA_PATHS[key]:
        if p not in cur and os.path.isdir(p):
            cur.append(p)      # appended, so the user's own PATH order wins
    os.environ["PATH"] = os.pathsep.join(cur)   # docker needs helpers like docker-credential-* on PATH too


_extend_path()
DOCKER = shutil.which("docker") or "docker"


def _run(args, input=None, timeout=20):
    try:
        p = subprocess.run(args, input=input, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NOWIN)
        return p.returncode, p.stdout, p.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return 127, "", str(e)


# ---------------------------------------------------------------- Ollama mode
def detect_ollama_mode():
    """nvidia: GPU container. cpu: plain container. host: bridge to an Ollama installed on this computer.
    Override with OLLAMA_MODE=nvidia|cpu|host."""
    forced = os.environ.get("OLLAMA_MODE", "").strip().lower()
    if forced in ("nvidia", "cpu", "host"):
        return forced
    if IS_MAC:
        return "host"          # Docker on macOS cannot reach the Mac GPU; native Ollama uses Metal (CPU on Intel Macs)
    rc, out, _ = _run([DOCKER, "info", "--format", "{{json .Runtimes}}"])
    if rc == 0 and "nvidia" in out.lower():
        return "nvidia"
    return "cpu"


MODE_LABEL = {"nvidia": "NVIDIA GPU container", "cpu": "CPU-only container", "host": "Ollama on this computer"}


# ---------------------------------------------------------------- machine capabilities (for model fit estimates)
# All sizes are decimal GB (1e9 bytes), the same unit Ollama uses for model sizes.
def total_ram_gb():
    try:
        if IS_WIN:
            from ctypes import wintypes as wt

            class MEMSTAT(ctypes.Structure):
                _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEMSTAT()
            m.dwLength = ctypes.sizeof(MEMSTAT)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 1e9
        if IS_MAC:
            rc, out, _ = _run(["sysctl", "-n", "hw.memsize"])
            return int(out.strip()) / 1e9 if rc == 0 else None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024 / 1e9
    except Exception:
        pass
    return None


def gpu_info():
    """(name, VRAM in decimal GB) for the first NVIDIA GPU, else None. macOS GPUs share system memory (see machine_info)."""
    if not shutil.which("nvidia-smi"):
        return None
    rc, out, _ = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    if rc != 0 or not out.strip():
        return None
    try:
        name, mib = [x.strip() for x in out.strip().splitlines()[0].rsplit(",", 1)]
        return name, int(mib) * 1048576 / 1e9
    except ValueError:
        return None


def apple_silicon():
    """True on an Apple Silicon Mac (GPU shares system memory). Intel Macs have no unified memory and Ollama runs
    models on the CPU there. Asks the kernel, not platform.machine(): an x86 python under Rosetta reports x86_64."""
    if not IS_MAC:
        return False
    rc, out, _ = _run(["sysctl", "-n", "hw.optional.arm64"])
    return rc == 0 and out.strip() == "1"


def machine_info(path="."):
    gpu = gpu_info()
    try:
        free = shutil.disk_usage(path).free / 1e9
    except OSError:
        free = None
    return {"ram_gb": total_ram_gb(), "gpu_name": gpu[0] if gpu else None, "vram_gb": gpu[1] if gpu else None,
            "unified_memory": apple_silicon(), "free_disk_gb": free}


# ---------------------------------------------------------------- secret store
# Windows: DPAPI-encrypted file. macOS: Keychain. Linux: Secret Service (libsecret). Otherwise a 0600 file
# (clearly NOT encrypted). In all cases the panel never returns a key to the browser or writes it to a log.
SERVICE = "openclaw-panel"
_ENTROPY = b"openclaw-control-panel/v1"


def _dpapi(data, protect):
    from ctypes import wintypes as wt

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def mk(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    inb, k1 = mk(data)
    ent, k2 = mk(_ENTROPY)
    out = BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = (ctypes.byref(inb), "openclaw-panel", ctypes.byref(ent), None, None, 0, ctypes.byref(out)) if protect else \
           (ctypes.byref(inb), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out))
    if not fn(*args):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


class SecretStore:
    def __init__(self, directory: Path, force=None):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.kind = force or self._pick()

    def _pick(self):
        if IS_WIN:
            return "dpapi"
        if IS_MAC and shutil.which("security"):
            return "keychain"
        if IS_LINUX and shutil.which("secret-tool"):
            # works only with a running session keyring; rc 1 + no stderr means "reachable, not found"
            rc, _, err = _run(["secret-tool", "lookup", "service", SERVICE, "account", "__probe__"])
            if rc == 1 and not err.strip():
                return "libsecret"
        return "file"

    @property
    def label(self):
        return {"dpapi": "encrypted with your Windows login (DPAPI)",
                "keychain": "stored in the macOS Keychain",
                "libsecret": "stored in the Linux Secret Service keyring",
                "file": "stored in a file readable only by your user (NOT encrypted: install libsecret-tools / use a keyring for better protection)"
                }[self.kind]

    def _marker(self, name):
        return self.dir / f"{name}.token"

    def exists(self, name):
        return self._marker(name).exists()

    def put(self, name, token):
        m = self._marker(name)
        if self.kind == "dpapi":
            m.write_bytes(_dpapi(token.encode(), True))
        elif self.kind == "keychain":
            # commands arrive on stdin, so the key never appears in a process listing
            rc, _, err = _run(["security", "-i"], input=f"add-generic-password -U -a {name} -s {SERVICE} -w {token}\n")
            if rc != 0:
                raise RuntimeError("could not write to the macOS Keychain: " + err.strip()[:200])
            m.write_text(json.dumps({"store": "keychain"}))
        elif self.kind == "libsecret":
            rc, _, err = _run(["secret-tool", "store", f"--label={SERVICE} {name}", "service", SERVICE, "account", name], input=token)
            if rc != 0:
                raise RuntimeError("could not write to the Secret Service keyring: " + err.strip()[:200])
            m.write_text(json.dumps({"store": "libsecret"}))
        else:
            fd = os.open(m, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps({"store": "file", "token": token}))
            os.chmod(m, 0o600)

    def get(self, name):
        m = self._marker(name)
        try:
            if self.kind == "dpapi":
                return _dpapi(m.read_bytes(), False).decode()
            info = json.loads(m.read_text())
            if info["store"] == "keychain":
                rc, out, _ = _run(["security", "find-generic-password", "-a", name, "-s", SERVICE, "-w"])
                return out.strip() if rc == 0 and out.strip() else None
            if info["store"] == "libsecret":
                rc, out, _ = _run(["secret-tool", "lookup", "service", SERVICE, "account", name])
                return out.strip() if rc == 0 and out.strip() else None
            return info.get("token")
        except Exception:
            return None

    def delete(self, name):
        m = self._marker(name)
        if not m.exists():
            return
        try:
            if self.kind == "keychain":
                _run(["security", "delete-generic-password", "-a", name, "-s", SERVICE])
            elif self.kind == "libsecret":
                _run(["secret-tool", "clear", "service", SERVICE, "account", name])
        finally:
            m.write_bytes(b"\0" * max(1, m.stat().st_size))   # overwrite before unlinking
            m.unlink()
