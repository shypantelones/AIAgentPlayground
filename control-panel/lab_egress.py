"""Internet access for network labs: lab VMs reach only documentation and package sites, through a proxy that checks
the hostname of every request. Pure logic here (presets, allowlist, proxy and firewall config); app.py runs the proxy
container and the VMs use what this module generates.

Each lab gets its own squid proxy on the host. It listens on the VirtualBox host-only network's address, which only
the lab VMs (and the host) are attached to. The NAT gateway can't carry VM traffic to the host's loopback on this
setup, so host-only is the path. Each lab VM gets a second adapter on that network, with its own address, and its
outbound traffic is default-deny: only the proxy and the lab's own links are allowed. Inbound on the host-only
adapter is refused, so VMs of different labs can't reach each other through it.
"""
import json
import re

# Documentation and package sites, grouped by what they're for. Leading dot = the domain and its subdomains.
PRESETS = {
    "os": [".archive.ubuntu.com", ".security.ubuntu.com", ".ports.ubuntu.com", "manpages.ubuntu.com", "help.ubuntu.com"],
    "python": ["docs.python.org", "pypi.org", "files.pythonhosted.org", "flask.palletsprojects.com",
               "jinja.palletsprojects.com", "werkzeug.palletsprojects.com"],
    "node": ["nodejs.org", "registry.npmjs.org", "developer.mozilla.org"],
    "web": ["httpd.apache.org", "nginx.org", "docs.nginx.com", "developer.mozilla.org", "www.rfc-editor.org"],
    "database": ["www.postgresql.org", "sqlite.org", "dev.mysql.com"],
    "routing": ["docs.frrouting.org", "frrouting.org", "www.nftables.org", "wiki.nftables.org", "netfilter.org",
                "thekelleys.org.uk", "www.kernel.org", "docs.kernel.org"],
    "firewall": ["www.nftables.org", "wiki.nftables.org", "netfilter.org"],
}

# Which presets each lab role gets. An upstream node is the lab's internet stand-in, so it gets none.
ROLE_PRESETS = {
    "host": ["os", "python", "node", "web", "database"],
    "server": ["os", "python", "node", "web", "database"],
    "loadbalancer": ["os", "web"],
    "router": ["os", "routing"],
    "firewall": ["os", "firewall"],
    "switch": ["os"],
    "upstream": [],
}

MAX_EXTRA_DOMAINS = 50
DOMAIN_RE = re.compile(r"^\.?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PROXY_PORT_RANGE = (62500, 62513)      # one per lab with internet: 14 at a time
PROXY_IMAGE = "ubuntu/squid:latest"
HOST_ONLY_NET = "192.168.56"           # VirtualBox's default host-only network; the host is .1 on it
PROXY_HOST = f"{HOST_ONLY_NET}.1"
MAX_NODES_PER_LAB = 12                 # see vm_host_only_ip
NAT_GATEWAY = "10.0.2.2"               # the host, as seen from a VirtualBox NAT adapter (DHCP only, here)
NAT_IFACE = "enp0s3"                   # the NAT nic, always the first adapter (its name in the guest)


def normalize_domain(text):
    """One user-entered domain -> its canonical lowercase form, or ValueError with what to fix."""
    d = text.strip().lower()
    if not DOMAIN_RE.match(d):
        raise ValueError(f"'{text.strip()}' doesn't look like a domain (e.g. docs.example.org or .example.org)")
    return d


def preset_domains(roles):
    """The documentation presets for a set of lab roles, de-duplicated and sorted."""
    names = sorted({p for role in roles for p in ROLE_PRESETS.get(role, [])})
    return sorted({d for p in names for d in PRESETS[p]})


def allowlist_for(roles, extra=()):
    """The domains a lab may reach: its roles' presets, plus any the user added for a niche tool."""
    extra_norm = []
    for text in extra or []:
        if not str(text).strip():
            continue
        extra_norm.append(normalize_domain(str(text)))
    if len(extra_norm) > MAX_EXTRA_DOMAINS:
        raise ValueError(f"at most {MAX_EXTRA_DOMAINS} extra domains per lab")
    return sorted(set(preset_domains(roles)) | set(extra_norm))


def squid_conf():
    """The proxy's config: allow listed hostnames over HTTP and HTTPS, refuse everything else. No caching (the
    point is to see who asks for what, not to speed up downloads), and client addresses are not forwarded."""
    return """http_port 3128
acl allowed_domains dstdomain "/etc/squid/allowlist.txt"
acl SSL_ports port 443
acl CONNECT method CONNECT
http_access deny CONNECT !SSL_ports
http_access allow allowed_domains
http_access deny all
forwarded_for delete
access_log stdio:/var/log/squid/access.log
cache deny all
"""


def proxy_compose(project, port, allowlist_path, conf_path):
    """Compose file for one lab's proxy. Published on the host-only address only, so nothing on the LAN can use it.
    Mounts use the long syntax with quoted paths: a Windows path has a colon in it, which the short syntax misreads."""
    def bind(src, dst):
        return f"      - {{type: bind, source: {json.dumps(str(src))}, target: {json.dumps(dst)}, read_only: true}}"
    return f"""name: {project}
services:
  egress-proxy:
    image: {PROXY_IMAGE}
    restart: "no"
    ports:
      - "{PROXY_HOST}:{port}:3128"
    volumes:
{bind(conf_path, "/etc/squid/squid.conf")}
{bind(allowlist_path, "/etc/squid/allowlist.txt")}
    cap_drop: [ALL]
    cap_add: [SETUID, SETGID, CHOWN, DAC_OVERRIDE]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 256m
"""


def vm_host_only_ip(port, node_index):
    """A lab VM's address on the host-only network. Unique across labs: each lab's port slot owns a block of
    MAX_NODES_PER_LAB addresses, starting at .20 (14 labs x 12 nodes fits below .255)."""
    slot = port - PROXY_PORT_RANGE[0]
    return f"{HOST_ONLY_NET}.{20 + slot * MAX_NODES_PER_LAB + node_index}"


def proxy_url(port):
    return f"http://{PROXY_HOST}:{port}"


def egress_script(port, vm_ip):
    """Shell run on a lab VM before its first apt-get: apt and pip go through the lab's proxy. Nothing else is set
    globally: a proxy in the environment would send the lab's own traffic to the internet proxy too. Anything else
    that needs documentation uses the proxy explicitly, e.g. `curl -x <proxy> https://docs.python.org/`."""
    url = proxy_url(port)
    return f"""# Lab internet: documentation and package sites only, through the lab's proxy ({url}).
# The host-only adapter gets its address from the same provisioning run: wait for it, and for the proxy to answer,
# before the first apt-get. Logged so a failed build shows what the VM could reach.
for i in $(seq 1 30); do ip -o -4 addr show | grep -q ' {vm_ip}/' && break; sleep 2; done
echo "lab egress: addresses:"; ip -br addr
cat > /etc/apt/apt.conf.d/95lab-proxy <<'APT'
Acquire::http::Proxy "{url}/";
APT
mkdir -p /etc/pip && printf '[global]\\nproxy = %s\\n' "{url}" > /etc/pip.conf
"""


def firewall_script(port, lab_ifaces, vm_ip):
    """Shell run on a lab VM: outbound is default-deny. Allowed: the lab's own links (`lab_ifaces`, their guest names),
    loopback, and the proxy on the host-only network. The host-only adapter is found by its address, since its guest
    name depends on how many lab links the node has. Inbound on it is refused. Inbound elsewhere is unchanged (SSH
    comes through the NAT port forward and is allowed as a reply)."""
    lines = [f"HO=$(ip -o -4 addr show | awk -v ip='{vm_ip}' 'index($4, ip \"/\") == 1 {{print $2; exit}}')",
             "ufw --force reset >/dev/null",
             "ufw default deny outgoing",
             "ufw default allow incoming",
             "ufw allow out on lo"]
    lines += [f"ufw allow out on {ifc}" for ifc in lab_ifaces]
    lines.append(f"ufw allow out on \"$HO\" to {PROXY_HOST} port {port} proto tcp")
    lines.append("ufw deny in on \"$HO\"")
    lines.append(f"ufw allow out on {NAT_IFACE} to {NAT_GATEWAY} port 67 proto udp")   # DHCP renewals on the NAT nic
    # Lab routers forward between their links. Forwarded traffic is not covered by the outbound default, so the
    # NAT and host-only interfaces are closed to forwarding too; otherwise a client behind a router could skip the proxy.
    lines += ["ufw default allow routed", f"ufw route deny out on {NAT_IFACE}", "ufw route deny out on \"$HO\""]
    lines.append("ufw --force enable")
    # ufw accepts ICMP echo requests before its own user rules, so "deny in" alone lets another lab ping this VM.
    # A rule ahead of ufw's chains drops new inbound connections on the host-only adapter. Only NEW is matched:
    # replies to this VM's own proxy requests arrive on the same adapter and must still get through.
    lines += ['iptables -I INPUT 1 -i "$HO" -m conntrack --ctstate NEW -j DROP',
              'ip6tables -I INPUT 1 -i "$HO" -m conntrack --ctstate NEW -j DROP 2>/dev/null || true']
    return "\n".join(lines) + "\n"
