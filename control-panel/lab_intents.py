"""Intents: what a network lab must and must not do, checked from the nodes themselves.

One intent per line:
    h1 -> h2 icmp reach            h1 can ping h2
    client -> lb1 tcp/80 reach     client can open TCP port 80 on lb1
    h2 -> web1 tcp/22 block        h2 can't open TCP port 22 on web1
    h1 -> 10.20.0.10 path via r1, r2    traffic from h1 to 10.20.0.10 goes through r1 then r2
A destination is a node name (any of its addresses counts) or an IPv4 address. Checks run over SSH from the source
node (see app.run_intent_checks), so they see what that node actually sees. Lines starting with # are comments.
UDP is not supported yet: a closed UDP port and a filtered one look the same to a probe.
"""
import ipaddress
import re

MAX_INTENTS = 50
PING_TIMEOUT = 15
TCP_TIMEOUT = 5
TRACE_TIMEOUT = 40
INTENT_RE = re.compile(r"^(\S+)\s*->\s*(\S+)\s+(.+)$")
PROTO_RE = re.compile(r"^(icmp|tcp/(\d{1,5}))\s+(reach|block)$")
PATH_RE = re.compile(r"^path\s+via\b\s*(.*)$")
# Addresses that aren't part of the lab: the setup network every VM has, loopback and link-local.
SETUP_NET = ipaddress.ip_network("10.0.2.0/24")
PROXY_NET = ipaddress.ip_network("192.168.56.0/24")   # the host-only link to the lab's internet proxy


def parse_intent(line, node_names=None):
    """One intent line -> a dict {text, src, dst, kind, proto, port, verdict, via}. Raises ValueError with a message
    a person can act on. With node_names, every node named in the line must be one of them."""
    text = " ".join(line.split())
    m = INTENT_RE.match(text)
    if not m:
        raise ValueError(f"'{line.strip()}': write it as 'source -> destination icmp|tcp/<port> reach|block' or "
                         "'source -> destination path via node, node'")
    src, dst, rest = m.groups()
    intent = {"src": src, "dst": dst, "text": None}
    pm = PATH_RE.match(rest)
    if pm:
        via = [v.strip() for v in pm.group(1).split(",") if v.strip()]
        if not via:
            raise ValueError(f"'{text}': 'path via' needs at least one node")
        intent.update(kind="path", via=via)
        text_rest = f"path via {', '.join(via)}"
    else:
        km = PROTO_RE.match(rest)
        if not km:
            raise ValueError(f"'{text}': the check must be 'icmp reach', 'icmp block', 'tcp/<port> reach' or "
                             "'tcp/<port> block' (udp isn't supported yet)")
        proto, port, verdict = km.groups()
        if port is not None and not 1 <= int(port) <= 65535:
            raise ValueError(f"'{text}': port {port} is out of range")
        intent.update(kind="reach" if verdict == "reach" else "block", proto=proto.split("/")[0],
                      port=int(port) if port else None, verdict=verdict)
        text_rest = f"{proto} {verdict}"
        via = []
    intent["text"] = f"{src} -> {dst} {text_rest}"
    if "/" in dst:
        raise ValueError(f"'{text}': give one address or node as the destination, not a subnet (yet)")
    if is_address(src):
        raise ValueError(f"'{text}': the source has to be a node")
    if node_names is not None:
        for name in [src, dst] + via:
            if not is_address(name) and name not in node_names:
                raise ValueError(f"'{text}': no node called '{name}' in this lab")
    return intent


def is_address(name):
    try:
        ipaddress.IPv4Address(name)
        return True
    except ValueError:
        return False


def parse_intents(lines, node_names=None):
    """Lines (a string or a list) -> a list of intent dicts. Blank lines and # comments are skipped."""
    if isinstance(lines, str):
        lines = lines.splitlines()
    out = []
    for line in lines or []:
        if not isinstance(line, str):
            raise ValueError("each intent has to be a line of text")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        out.append(parse_intent(line, node_names))
    if len(out) > MAX_INTENTS:
        raise ValueError(f"at most {MAX_INTENTS} intents per lab")
    return out


def parse_addresses(output):
    """`ip -4 -o addr show` output -> the node's lab addresses (setup network, loopback and link-local left out)."""
    found = []
    for m in re.finditer(r"\binet (\d+\.\d+\.\d+\.\d+)/\d+", output or ""):
        ip = m.group(1)
        addr = ipaddress.IPv4Address(ip)
        if addr in SETUP_NET or addr in PROXY_NET or addr.is_loopback or addr.is_link_local:
            continue
        found.append(ip)
    return found


def parse_traceroute(output):
    """`traceroute -n` output -> the hop addresses in order, skipping silent hops ('*')."""
    hops = []
    for line in (output or "").splitlines():
        m = re.match(r"^\s*\d+\s+(\S+)", line)
        if m and is_address(m.group(1)):
            hops.append(m.group(1))
    return hops


def check_command(intent, dst_addrs):
    """The shell command run on the source node for this intent, or None for a path check (see check_path).
    Exit status 0 means the probe got through."""
    if intent["kind"] == "path":
        return None
    if intent["proto"] == "icmp":
        probes = [f"ping -c 2 -W 2 -q {ip}" for ip in dst_addrs]
    else:
        probes = [f"timeout {TCP_TIMEOUT} bash -c 'exec 3<>/dev/tcp/{ip}/{intent['port']}'" for ip in dst_addrs]
    return " || ".join(probes)


def judge(intent, got_through):
    """Did this intent pass, given whether the probe got through?"""
    return got_through if intent["verdict"] == "reach" else not got_through


def run_intent_checks(intents, nodes, run_on):
    """Check every intent. `nodes` is the lab's node names; `run_on(node, command, timeout)` returns (rc, output)
    from that node, or raises OSError if the node can't be reached. Returns one result per intent:
    {text, passed, detail}. An intent whose node can't be reached fails with the reason, and the others still run."""
    addr_cache = {}

    def addresses(node):
        if node not in addr_cache:
            _, out = run_on(node, "ip -4 -o addr show", 20)
            addr_cache[node] = parse_addresses(out)
        return addr_cache[node]

    def dst_addrs(name):
        return [name] if is_address(name) else addresses(name)

    results = []
    for intent in intents:
        try:
            results.append(_check_one(intent, dst_addrs, run_on))
        except OSError as e:
            results.append({"text": intent["text"], "passed": False, "detail": f"could not run on a node: {e}"})
    return results


def _check_one(intent, dst_addrs, run_on):
    text = intent["text"]
    targets = dst_addrs(intent["dst"])
    if not targets:
        return {"text": text, "passed": False, "detail": f"{intent['dst']} has no lab address yet"}
    if intent["kind"] == "path":
        return _check_path(intent, targets, dst_addrs, run_on)
    rc, out = run_on(intent["src"], check_command(intent, targets), PING_TIMEOUT if intent["proto"] == "icmp" else
                     TCP_TIMEOUT * len(targets) + 5)
    got = rc == 0
    detail = ("probe got through" if got else "probe failed") + f" from {intent['src']}"
    return {"text": text, "passed": judge(intent, got), "detail": detail}


def _check_path(intent, targets, dst_addrs, run_on):
    text = intent["text"]
    if len(targets) > 1 and not is_address(intent["dst"]):
        targets = targets[:1]                       # a path check follows one address: a node's first
    dst = targets[0]
    _, out = run_on(intent["src"], f"traceroute -n -w 1 -q 1 -m 12 {dst}", TRACE_TIMEOUT)
    hops = parse_traceroute(out)
    if not hops or hops[-1] != dst:
        return {"text": text, "passed": False, "detail": f"traceroute from {intent['src']} did not reach {dst}"}
    pos = 0
    for via in intent["via"]:
        vaddrs = dst_addrs(via)
        found = [i for i, h in enumerate(hops) if h in vaddrs and i >= pos]
        if not found:
            return {"text": text, "passed": False,
                    "detail": f"path {' > '.join(hops)} does not go through {via}"}
        pos = found[0] + 1
    return {"text": text, "passed": True, "detail": f"path {' > '.join(hops)}"}


def summary_for_prompt(intents):
    """The intents as the agent sees them: one short line each."""
    return "\n".join(f"- {i['text']}" for i in intents)
