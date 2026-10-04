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
