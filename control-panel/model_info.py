"""Model metadata parsing, memory estimates and "does it fit this computer?" logic. No Docker or network access here.

Sizes are decimal GB (1e9 bytes), the unit Ollama shows. Memory estimate = weights + KV cache at the configured
context length (+ a small allowance). It is an estimate, not a guarantee: runtimes vary a little by version and driver.
"""
import json, re
from pathlib import Path

CATALOG_FILE = Path(__file__).resolve().parent / "model_catalog.json"
ALLOWANCE_GB = 0.3          # compute buffers etc.


def load_catalog():
    data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    return data["context_tokens"], data["models"]


# ---------------------------------------------------------------- reading what Ollama tells us about an installed model
_KEY = re.compile(r"^\s*([A-Za-z0-9_.-]+)\.(block_count|attention\.head_count_kv|attention\.head_count|attention\.key_length|"
                  r"attention\.value_length|embedding_length|context_length)\s+(\[?[\d, ]+\]?)\s*$")


def _num(s):
    s = str(s).strip().strip("[]")
    vals = [int(x) for x in re.findall(r"\d+", s)]
    return max(vals) if vals else None          # per-layer arrays: use the largest (conservative)


def parse_show_text(text):
    """`ollama show --verbose NAME` output -> info dict."""
    info = {"raw": {}, "params": None, "quant": None, "caps": []}
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s in ("Model", "Capabilities", "Parameters", "License"):
            section = s
            continue
        m = _KEY.match(line)
        if m:
            info["raw"][m.group(2)] = _num(m.group(3))
            continue
        if section == "Model":
            p = s.split(None, 1)
            if len(p) == 2 and p[0] == "parameters":
                info["params"] = p[1].strip()
            elif len(p) == 2 and p[0] == "quantization":
                info["quant"] = p[1].strip()
        elif section == "Capabilities" and s in ("completion", "tools", "thinking", "vision", "embedding", "insert"):
            info["caps"].append(s)
    return info


def parse_show_json(obj):
    """POST /api/show {"verbose": true} response -> info dict (same shape as parse_show_text)."""
    raw = {}
    for k, v in (obj.get("model_info") or {}).items():
        m = re.match(r"^[A-Za-z0-9_-]+\.(block_count|attention\.head_count_kv|attention\.head_count|attention\.key_length|"
                     r"attention\.value_length|embedding_length|context_length)$", k)
        if m and isinstance(v, (int, list)):
            raw[m.group(1)] = _num(v)
    d = obj.get("details") or {}
    return {"raw": raw, "params": d.get("parameter_size"), "quant": d.get("quantization_level"),
            "caps": list(obj.get("capabilities") or [])}


def kv_bytes_per_token(info):
    """fp16 K and V cache bytes per token, or None if the metadata is incomplete."""
    r = info.get("raw") or {}
    layers, kvh = r.get("block_count"), r.get("attention.head_count_kv") or r.get("attention.head_count")
    if not layers or not kvh:
        return None
    heads = r.get("attention.head_count")
    key = r.get("attention.key_length") or (r.get("embedding_length") // heads if r.get("embedding_length") and heads else None)
    if not key:
        return None
    val = r.get("attention.value_length") or key
    return layers * kvh * (key + val) * 2


# ---------------------------------------------------------------- memory need and fit
def need_gb_exact(size_gb, info, ctx):
    bpt = kv_bytes_per_token(info or {})
    if bpt is None:
        return None
    return size_gb + bpt * ctx / 1e9 + ALLOWANCE_GB


def need_gb_catalog(entry, ctx):
    return entry["size_gb"] + entry["kv_gb_32k"] * ctx / 32768 + ALLOWANCE_GB


def assess(need_gb, size_gb, machine, mode, already_installed):
    """How will this model run here? -> {fit, label, memory_kind, disk_ok}
    fit: gpu | partial | cpu | tight | toobig | unknown
    """
    ram, vram, free = machine.get("ram_gb"), machine.get("vram_gb"), machine.get("free_disk_gb")
    unified = machine.get("unified_memory")
    disk_ok = already_installed or free is None or size_gb + 2 <= free
    if need_gb is None:
        return {"fit": "unknown", "label": "memory need unknown", "memory_kind": "memory", "disk_ok": disk_ok}
    if unified and ram:                                         # Apple Silicon: GPU shares system memory
        usable = ram * 0.65                                      # macOS caps GPU-usable memory at roughly this
        if need_gb <= usable:
            return {"fit": "gpu", "label": "fits (Mac GPU, unified memory)", "memory_kind": "unified memory", "disk_ok": disk_ok}
        if need_gb <= ram * 0.8:
            return {"fit": "tight", "label": "tight: may be slow or swap", "memory_kind": "unified memory", "disk_ok": disk_ok}
        return {"fit": "toobig", "label": "too big for this Mac's memory", "memory_kind": "unified memory", "disk_ok": disk_ok}
    if vram and mode != "cpu":
        usable = vram * 0.92                                     # leave room for the desktop and driver
        if need_gb <= usable:
            return {"fit": "gpu", "label": "fits your GPU", "memory_kind": "VRAM", "disk_ok": disk_ok}
        if ram and need_gb <= usable + ram * 0.6:
            return {"fit": "partial", "label": "spills into system RAM (slower)", "memory_kind": "VRAM + RAM", "disk_ok": disk_ok}
        return {"fit": "toobig", "label": "too big for this GPU + RAM", "memory_kind": "VRAM", "disk_ok": disk_ok}
    if ram:
        if need_gb <= ram * 0.6:
            return {"fit": "cpu", "label": "runs on CPU/RAM (slow)", "memory_kind": "RAM", "disk_ok": disk_ok}
        return {"fit": "toobig", "label": "too big for this computer's RAM", "memory_kind": "RAM", "disk_ok": disk_ok}
    return {"fit": "unknown", "label": "could not detect memory", "memory_kind": "memory", "disk_ok": disk_ok}


def describe_installed(name, size_gb, info, machine, mode, ctx, catalog_entry=None):
    need = need_gb_exact(size_gb, info, ctx)
    a = assess(need, size_gb, machine, mode, True)
    caps = (info or {}).get("caps") or []
    e = catalog_entry or {}
    return {"name": name, "installed": True, "size_gb": round(size_gb, 1), "params": (info or {}).get("params"),
            "quant": (info or {}).get("quant"), "tools": ("tools" in caps) if caps else None,
            "embedding_only": bool(caps) and "completion" not in caps,
            "thinking": "thinking" in caps, "need_gb": None if need is None else round(need, 1), "exact": need is not None,
            "openclaw_tool_calling": e.get("openclaw_tool_calling", "untested"), "note": e.get("note", ""), **a}


def describe_catalog(entry, machine, mode, ctx):
    need = need_gb_catalog(entry, ctx)
    a = assess(need, entry["size_gb"], machine, mode, False)
    return {"name": entry["name"], "installed": False, "family": entry.get("family"), "size_gb": entry["size_gb"],
            "params": entry.get("params"), "quant": None, "tools": entry.get("tools"), "embedding_only": False,
            "thinking": None, "need_gb": round(need, 1), "exact": False,
            "openclaw_tool_calling": entry.get("openclaw_tool_calling", "untested"), "note": entry.get("note", ""), **a}
