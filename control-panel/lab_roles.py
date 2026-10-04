"""Roles for the agents in a team lab: what each one is for, and what its commands may not do. Pure logic; app.py
gives each member its role's guard, and vm_runner's vmrun wrapper enforces it.

The guard is a command pattern check at the vmrun wrapper. It stops the usual mistakes (a web admin reconfiguring the
firewall, a client changing routes) and leaves a record of the refusal. It is not a boundary: an agent holds the lab
key, so it could reach a node over SSH directly. The hard version is VM-side users with restricted sudo, listed as the
next step in the README.
"""

# Commands no lab role may run: power and disk operations that would end the lab, not configure it.
ALWAYS_DENIED = [r"\breboot\b", r"\bshutdown\b", r"\bpoweroff\b", r"\bhalt\b", r"\bmkfs\b", r"\bdd\b"]

# Firewall commands: only the firewall admin and the network admin change the firewall.
FIREWALL = [r"\bnft\b", r"\biptables\b", r"\bip6tables\b", r"\bufw\b"]

ROLES = {
    "network-admin": {
        "for": "routing, addressing and forwarding across the lab; makes sure the other members have the paths they need",
        "refuses": "power and disk operations only; it may change routes, addresses and the firewall",
        "denied": ALWAYS_DENIED,
    },
    "firewall-admin": {
        "for": "the firewall node(s): the rules that let the required traffic through and block the rest",
        "refuses": "power and disk operations, and routes and addresses (the network admin owns those)",
        "denied": ALWAYS_DENIED + [r"\bip\s+route\b", r"\bip\s+addr\b"],
    },
    "web-admin": {
        "for": "the web server(s): installs and runs the service, serves the content the client expects",
        "refuses": "power and disk operations, the firewall, routes and sysctl (the network and firewall admins own those)",
        "denied": ALWAYS_DENIED + FIREWALL + [r"\bip\s+route\b", r"\bsysctl\b"],
    },
    "client-dev": {
        "for": "the client side: the program that connects to the server and uses what it serves",
        "refuses": "power and disk operations, the firewall, routes and sysctl",
        "denied": ALWAYS_DENIED + FIREWALL + [r"\bip\s+route\b", r"\bsysctl\b"],
    },
    "tool-dev": {
        "for": "developing and testing a tool on a host, with apt and pip through the lab's proxy",
        "refuses": "power and disk operations, the firewall, routes and sysctl",
        "denied": ALWAYS_DENIED + FIREWALL + [r"\bip\s+route\b", r"\bsysctl\b"],
    },
}
NO_ROLE = {"for": "no role: may do anything the lab allows", "refuses": "power and disk operations", "denied": ALWAYS_DENIED}


def role_of(brief):
    """A team line's brief may start with its role in brackets: '[web-admin] serves the feed'. Returns
    (role or None, brief without the role). Raises ValueError for an unknown role."""
    brief = brief.strip()
    if not brief.startswith("["):
        return None, brief
    end = brief.find("]")
    if end < 0:
        raise ValueError("a role in brackets needs a closing ']'")
    name = brief[1:end].strip().lower()
    if name not in ROLES:
        raise ValueError(f"no role called '{name}'; roles are {', '.join(ROLES)}")
    return name, brief[end + 1:].strip()


def denied_patterns(role):
    """The command patterns a member with this role may not run (no role: the always-denied set)."""
    return (ROLES.get(role) or NO_ROLE)["denied"]


def describe(role):
    """Two lines for the member's prompt: what the role is for, and what its guard refuses."""
    spec = ROLES.get(role)
    if not spec:
        return ""
    return f"Your role is {role}: {spec['for']}. Your guard refuses {spec['refuses']}."


# VM-side users: each role has its own login on every lab VM. A member's key only opens its role's user, and that
# user's sudo is limited to the commands the role needs (below). The guard above still applies on top.
ROLE_USERS = {
    "network-admin": "netadmin",
    "firewall-admin": "fwadmin",
    "web-admin": "webadmin",
    "client-dev": "clientdev",
    "tool-dev": "tooldev",
}
NO_ROLE_USER = "member"                  # a member without a role: a login with no sudo at all

# Commands each role's user may run with sudo (absolute paths; sudoers NOPASSWD). Reading the network needs no sudo.
SUDO_ALLOW = {
    "network-admin": ["/usr/sbin/ip", "/usr/sbin/sysctl", "/usr/sbin/bridge", "/usr/bin/systemctl", "/usr/bin/vtysh",
                      "/usr/bin/tee"],
    "firewall-admin": ["/usr/sbin/nft", "/usr/sbin/iptables", "/usr/sbin/iptables-save", "/usr/sbin/iptables-restore",
                       "/usr/sbin/ufw", "/usr/sbin/sysctl", "/usr/bin/systemctl", "/usr/bin/tee", "/usr/sbin/ip"],
    "web-admin": ["/usr/bin/apt-get", "/usr/bin/systemctl", "/usr/bin/tee", "/usr/sbin/nginx"],
    "client-dev": [],
    "tool-dev": ["/usr/bin/apt-get"],
}


def user_for(role):
    """The VM login a member with this role uses (NO_ROLE_USER if it has no role)."""
    return ROLE_USERS.get(role, NO_ROLE_USER)


def provision_users_script():
    """Shell run on every lab VM at provision: each role's login, and its sudo list. A sudoers file that fails
    `visudo -c` is removed, so a bad entry can't break sudo for the VM."""
    lines = []
    for role, user in list(ROLE_USERS.items()) + [(None, NO_ROLE_USER)]:
        allow = SUDO_ALLOW.get(role, []) if role else []
        lines.append(f"id -u {user} >/dev/null 2>&1 || useradd -m -s /bin/bash {user}")
        lines.append(f"mkdir -p /home/{user}/.ssh && touch /home/{user}/.ssh/authorized_keys")
        lines.append(f"chmod 700 /home/{user}/.ssh && chmod 600 /home/{user}/.ssh/authorized_keys && chown -R {user}:{user} /home/{user}")
        if allow:
            f = f"/etc/sudoers.d/92-{user}"
            lines.append(f"echo '{user} ALL=(root) NOPASSWD: {', '.join(allow)}' > {f} && chmod 440 {f} && visudo -cf {f} >/dev/null || rm -f {f}")
    return "\n".join(lines) + "\n"
