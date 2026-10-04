"""Tests for lab internet access (lab_egress.py): which domains a lab may reach, the proxy and its config, the VM
scripts that point apt and pip at it, and the VM firewall. Nothing here touches Docker or VirtualBox; the real
boot test is a separate step (see the PR).
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import shutil, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import lab_egress as le  # noqa: E402
import vm_runner as vr  # noqa: E402


def without_role_logins(text):
    """A provision script minus the role logins' lines: their sudo lists name firewall tools, which only the
    firewall login may run, so keyword checks about a host's own firewall ignore them."""
    names = ("netadmin", "fwadmin", "webadmin", "clientdev", "tooldev", "member", "92-")
    return "\n".join(l for l in text.splitlines() if not any(n in l for n in names))


class DomainTests(unittest.TestCase):
    def test_domains_are_normalised_and_bad_ones_refused(self):
        self.assertEqual(le.normalize_domain("  Docs.Example.ORG "), "docs.example.org")
        self.assertEqual(le.normalize_domain(".example.org"), ".example.org")
        for bad in ["http://example.org", "10.0.0.1", "example", "a b.org", "*.example.org"]:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "doesn't look like a domain"):
                le.normalize_domain(bad)

    def test_upstream_gets_no_presets_and_host_gets_its_tools(self):
        self.assertEqual(le.preset_domains(["upstream"]), [])
        host = le.preset_domains(["host"])
        self.assertIn("docs.python.org", host)
        self.assertIn(".archive.ubuntu.com", host)
        self.assertNotIn("docs.frrouting.org", host)       # routing docs belong to routers

    def test_router_gets_routing_docs_but_not_python(self):
        router = le.preset_domains(["router"])
        self.assertIn("docs.frrouting.org", router)
        self.assertNotIn("pypi.org", router)

    def test_allowlist_adds_extra_domains_once(self):
        allow = le.allowlist_for(["host", "router"], ["Docs.Example.org", "docs.example.org", "  "])
        self.assertEqual(allow.count("docs.example.org"), 1)
        self.assertIn("docs.python.org", allow)

    def test_too_many_extra_domains_is_refused(self):
        with self.assertRaisesRegex(ValueError, "at most"):
            le.allowlist_for(["host"], [f"d{i}.example.org" for i in range(le.MAX_EXTRA_DOMAINS + 1)])


class ProxyTests(unittest.TestCase):
    def test_proxy_allows_listed_hosts_and_refuses_the_rest(self):
        conf = le.squid_conf()
        self.assertIn('acl allowed_domains dstdomain "/etc/squid/allowlist.txt"', conf)
        self.assertIn("http_access allow allowed_domains", conf)
        self.assertIn("http_access deny all", conf)
        self.assertIn("http_access deny CONNECT !SSL_ports", conf)       # only HTTPS may be tunnelled
        self.assertIn("cache deny all", conf)

    def test_proxy_is_published_on_the_host_only_address_only(self):
        comp = le.proxy_compose("aiagentplayground-egress-abc", 62500, "F:/x/allowlist.txt", "F:/x/squid.conf")
        self.assertIn('"192.168.56.1:62500:3128"', comp)
        self.assertNotIn('"62500:3128"', comp)
        self.assertNotIn("127.0.0.1", comp)
        self.assertIn('{type: bind, source: "F:/x/allowlist.txt", target: "/etc/squid/allowlist.txt", read_only: true}', comp)
        self.assertIn("name: aiagentplayground-egress-abc", comp)


class VmScriptTests(unittest.TestCase):
    def test_apt_and_pip_use_the_proxy_before_the_first_update(self):
        script = le.egress_script(62500, "192.168.56.20")
        self.assertIn("Acquire::http::Proxy \"http://192.168.56.1:62500/\";", script)
        self.assertIn("grep -q ' 192.168.56.20/'", script)           # waits for its own host-only address
        self.assertIn("/etc/pip.conf", script)
        self.assertIn('"http://192.168.56.1:62500"', script)
        self.assertNotIn("/etc/environment", script)         # no global proxy: it would catch the lab's own traffic
        self.assertNotIn("profile.d", script)

    def test_firewall_is_default_deny_out_with_only_the_proxy_and_lab_links(self):
        fw = le.firewall_script(62500, ["enp0s8", "enp0s9"], "192.168.56.20")
        self.assertIn("ufw default deny outgoing", fw)
        self.assertIn("ufw allow out on enp0s8", fw)
        self.assertIn("ufw allow out on \"$HO\" to 192.168.56.1 port 62500 proto tcp", fw)
        self.assertIn("HO=$(ip -o -4 addr show | awk -v ip='192.168.56.20'", fw)   # finds its adapter by address
        self.assertIn("ufw deny in on \"$HO\"", fw)                 # no inbound on the proxy network
        self.assertIn("ufw route deny out on enp0s3", fw)            # routers can't forward out past the proxy
        self.assertIn("ufw route deny out on \"$HO\"", fw)
        # ICMP is accepted by ufw before its user rules, so the drop for new connections must sit ahead of ufw.
        self.assertIn('iptables -I INPUT 1 -i "$HO" -m conntrack --ctstate NEW -j DROP', fw)
        self.assertGreater(fw.index("ufw --force enable"), fw.index("ufw deny in"))
        self.assertIn("ufw default allow routed", fw)
        self.assertLess(fw.index("ufw default deny outgoing"), fw.index("ufw --force enable"))

    def test_provision_script_gets_the_egress_block_only_when_asked(self):
        plain = vr._topo_provision_script("host", "ssh-ed25519 KEY")
        self.assertNotIn("95lab-proxy", plain)
        self.assertNotIn("ufw", without_role_logins(plain))
        with_eg = vr._topo_provision_script("host", "ssh-ed25519 KEY", egress_port=62500, lab_ifaces=["enp0s8"],
                                            vm_ip="192.168.56.20")
        self.assertLess(with_eg.index("95lab-proxy"), with_eg.index("apt-get update"))
        self.assertIn("ufw", with_eg.split("apt-get install")[1].splitlines()[0])   # ufw is installed
        self.assertGreater(with_eg.index("ufw --force enable"), with_eg.index("apt-get install"))

    def test_rendered_lab_gives_each_node_a_host_only_adapter_with_its_own_address(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            topo = app.vr.get_topology("r1s1h2")
            vr.render_topology_vagrantfile(tmp, "abc12345", topo, {n["name"]: 2200 + i for i, n in enumerate(topo["nodes"])},
                                           "ssh-ed25519 KEY", 1536, 1, egress_port=62500)
            vagrantfile = (tmp / "Vagrantfile").read_text()
            for i, n in enumerate(topo["nodes"]):
                self.assertIn("62500", (tmp / f"provision-{n['name']}.sh").read_text(), n["name"])
                self.assertIn(f'ip: "192.168.56.{20 + i}"', vagrantfile)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_proxy_slots_give_distinct_addresses_across_labs(self):
        seen = {le.vm_host_only_ip(p, i) for p in range(*[le.PROXY_PORT_RANGE[0], le.PROXY_PORT_RANGE[1] + 1])
                for i in range(le.MAX_NODES_PER_LAB)}
        self.assertEqual(len(seen), (le.PROXY_PORT_RANGE[1] - le.PROXY_PORT_RANGE[0] + 1) * le.MAX_NODES_PER_LAB)
        self.assertTrue(all(ip.startswith("192.168.56.") and 20 <= int(ip.split(".")[-1]) <= 254 for ip in seen))

    def test_a_lab_with_more_than_six_links_on_a_node_is_refused_when_it_has_internet(self):
        hub = {"title": "hub", "nodes": [{"name": f"n{i}", "role": "host"} for i in range(8)] + [{"name": "sw", "role": "switch"}],
               "links": [{"a": "sw", "b": f"n{i}"} for i in range(8)]}
        with mock.patch.object(app.vr, "get_topology", return_value=hub), mock.patch.object(app.threading, "Thread") as thread:
            with self.assertRaisesRegex(ValueError, "6 links per node"):
                app.create_topo_run({"topology_id": "hub"})
        thread.assert_not_called()


class LabFileFirewallTests(unittest.TestCase):
    def test_a_ufw_ruleset_in_a_lab_file_is_not_restored(self):
        ruleset = "table ip filter {\n\tchain ufw-before-input {\n\t}\n}\n"
        script, skipped = vr.render_apply_script({"nftables": ruleset})
        self.assertNotIn("nft -f", script)
        self.assertTrue(any("ufw" in x for x in skipped), skipped)

    def test_a_firewall_role_ruleset_without_ufw_is_still_restored(self):
        ruleset = "table inet filter {\n\tchain forward {\n\t\tdrop\n\t}\n}\n"
        script, _ = vr.render_apply_script({"nftables": ruleset})
        self.assertIn("nft -f", script)


class CreateLabEgressTests(unittest.TestCase):
    def test_a_bad_extra_domain_stops_the_lab_before_anything_is_built(self):
        with mock.patch.object(app.threading, "Thread") as thread:
            with self.assertRaisesRegex(ValueError, "doesn't look like a domain"):
                app.create_topo_run({"topology_id": "s1h2", "extra_domains": "not a domain!"})
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
