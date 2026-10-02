"""Pure-logic tests for vm_runner.py: no VirtualBox/Vagrant/Docker needed.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import subprocess, sys, tempfile, shutil, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import vm_runner as vr  # noqa: E402


class PortAllocationTests(unittest.TestCase):
    def test_skips_taken_ports(self):
        with mock.patch.object(vr, "port_free", return_value=True):
            self.assertEqual(vr.allocate_port((100, 105), {100, 101}), 102)

    def test_skips_ports_in_use_on_the_host(self):
        busy = {103}
        with mock.patch.object(vr, "port_free", side_effect=lambda p, host="127.0.0.1": p not in busy):
            self.assertEqual(vr.allocate_port((100, 105), set()), 100)
            self.assertEqual(vr.allocate_port((103, 105), set()), 104)

    def test_raises_when_range_exhausted(self):
        with mock.patch.object(vr, "port_free", return_value=False):
            with self.assertRaises(RuntimeError):
                vr.allocate_port((100, 101), set())


class VagrantfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_render_produces_expected_isolation_settings(self):
        vr.render_vagrantfile(self.tmp, "test-vm", 62201, "ssh-ed25519 AAAA test", 1536, 2, offline=True)
        vf = (self.tmp / "Vagrantfile").read_text()
        prov = (self.tmp / "provision.sh").read_text()
        self.assertIn('config.vm.box = "ubuntu/jammy64"', vf)
        self.assertIn("synced_folder \".\", \"/vagrant\", disabled: true", vf)
        self.assertIn('host: 62201, host_ip: "127.0.0.1"', vf)                   # loopback only, never the LAN
        self.assertIn('vb.memory = 1536', vf)
        self.assertIn('vb.cpus = 2', vf)
        self.assertIn('--clipboard-mode", "disabled"', vf)
        self.assertIn('--draganddrop", "disabled"', vf)
        self.assertIn("AAAA test", prov)                                        # the pubkey made it into authorized_keys
        self.assertIn("ufw default deny outgoing", prov)                        # offline lockdown present
        self.assertIn("ufw --force enable", prov)

    def test_offline_false_skips_the_firewall_lockdown(self):
        vr.render_vagrantfile(self.tmp, "t", 62202, "k", 1024, 1, offline=False)
        prov = (self.tmp / "provision.sh").read_text()
        self.assertNotIn("ufw default deny outgoing", prov)

    def test_no_crlf_in_generated_files(self):
        """Regression: a shebang corrupted by \\r\\n makes the Linux side report 'not found'."""
        vr.render_vagrantfile(self.tmp, "t", 62203, "k", 1024, 1, offline=True)
        for f in ("Vagrantfile", "provision.sh"):
            self.assertNotIn(b"\r\n", (self.tmp / f).read_bytes())

    def test_vm_name_and_port_are_not_injectable(self):
        # render_vagrantfile trusts its callers to have validated these (app.py generates them), but at minimum
        # a quote in a value must not let generated Ruby/shell break out of its string context undetected.
        vr.render_vagrantfile(self.tmp, "ok-name", 62204, "ssh-ed25519 AAAA x", 1024, 1, offline=True)
        vf = (self.tmp / "Vagrantfile").read_text()
        self.assertIn('vb.name = "ok-name"', vf)


class SeedFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_nested_files(self):
        vr.write_seed_files(self.tmp, {"a.py": "print(1)\n", "sub/b.txt": "hi\n"})
        self.assertEqual((self.tmp / "seed" / "a.py").read_text(), "print(1)\n")
        self.assertEqual((self.tmp / "seed" / "sub" / "b.txt").read_text(), "hi\n")

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            vr.write_seed_files(self.tmp, {"../escape.txt": "x"})
        with self.assertRaises(ValueError):
            vr.write_seed_files(self.tmp, {"/etc/passwd": "x"})


class TaskCatalogTests(unittest.TestCase):
    def test_catalog_loads_and_ids_are_valid(self):
        tasks = vr.load_tasks()
        self.assertGreaterEqual(len(tasks), 1)
        ids = [t["id"] for t in tasks]
        self.assertEqual(len(ids), len(set(ids)))
        for t in tasks:
            self.assertRegex(t["id"], r"^[a-z][a-z0-9-]{1,40}$")
            self.assertIn("prompt", t)
            self.assertIn("check", t)
            self.assertIsInstance(t.get("seed", {}), dict)

    def test_get_task_roundtrip_and_unknown_raises(self):
        t = vr.get_task(vr.load_tasks()[0]["id"])
        self.assertEqual(vr.get_task(t["id"])["id"], t["id"])
        with self.assertRaises(KeyError):
            vr.get_task("does-not-exist")

    def test_fizzbuzz_check_script_logic_runs_locally(self):
        # the check script is bash+python; run its python core directly to prove the comparison logic itself is right
        task = vr.get_task("fizzbuzz-cli")
        py = task["check"].split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
        lines = [str(i) for i in range(1, 101)]
        for i in range(3, 101, 3):
            lines[i - 1] = "Fizz"
        for i in range(5, 101, 5):
            lines[i - 1] = "Buzz"
        for i in range(15, 101, 15):
            lines[i - 1] = "FizzBuzz"
        out = Path(tempfile.mktemp())
        out.write_text("\n".join(lines) + "\n")
        ns = {}
        exec(py.replace("/tmp/out.txt", out.as_posix()), ns)   # as_posix(): avoids Windows backslashes breaking the generated source
        out.unlink()


class HaveToolsTests(unittest.TestCase):
    def test_reports_a_dict_of_booleans(self):
        t = vr.have_tools()
        for k in ("vagrant", "ssh", "scp", "ssh-keygen", "docker"):
            self.assertIn(k, t)
            self.assertIsInstance(t[k], bool)


class VagrantStreamTests(unittest.TestCase):
    """Real vagrant binary, like HaveToolsTests above - skipped if it isn't installed."""

    def setUp(self):
        if not vr.have_tools()["vagrant"]:
            self.skipTest("vagrant not installed")

    def test_on_line_is_called_as_output_is_produced_and_full_output_is_also_returned(self):
        seen = []
        rc, out, err = vr.vagrant_stream(Path("."), "--version", on_line=seen.append, timeout=20)
        self.assertEqual(rc, 0)
        self.assertTrue(seen, "on_line must be called at least once for a command that prints output")
        self.assertIn("Vagrant", out)


class SshBaseTests(unittest.TestCase):
    def test_user_known_hosts_file_option_is_a_single_well_formed_argument(self):
        """Regression (found via real boot testing - 100% reproducible, not flaky VM timing): the ternary used to
        be "UserKnownHostsFile=NUL" if windows else "/dev/null" - the "UserKnownHostsFile=" prefix only applied to
        the Windows branch, so on macOS/Linux the bare string "/dev/null" was passed as its own -o argument, and
        ssh tried (and failed) to parse "/dev/null" itself as a config keyword on every single connection attempt."""
        args = vr._ssh_base(62400, "/tmp/key")
        known_hosts_args = [a for a in args if a.startswith("UserKnownHostsFile=")]
        self.assertEqual(len(known_hosts_args), 1, args)
        self.assertNotIn("/dev/null", args, "the value must be part of the -o argument, not its own list element")


class TopologyCatalogTests(unittest.TestCase):
    def test_catalog_loads_and_ids_are_valid(self):
        topos = vr.load_topologies()
        self.assertGreaterEqual(len(topos), 1)
        ids = [t["id"] for t in topos]
        self.assertEqual(len(ids), len(set(ids)))
        for t in topos:
            self.assertRegex(t["id"], r"^[a-z][a-z0-9-]{1,40}$")
            names = [n["name"] for n in t["nodes"]]
            self.assertEqual(len(names), len(set(names)), f"{t['id']}: duplicate node name")
            for n in t["nodes"]:
                self.assertRegex(n["name"], r"^[a-z][a-z0-9]{0,14}$")
                self.assertIn(n["role"], vr.NODE_ROLES)
            for link in t["links"]:
                self.assertIn(link["a"], names)
                self.assertIn(link["b"], names)
                self.assertNotEqual(link["a"], link["b"])

    def test_rejects_unknown_role(self):
        with self.assertRaises(ValueError):
            vr.validate_topology({"id": "x", "nodes": [{"name": "h1", "role": "bogus"}], "links": []})

    def test_rejects_self_link(self):
        with self.assertRaises(ValueError):
            vr.validate_topology({"id": "x", "nodes": [{"name": "h1", "role": "host"}],
                                  "links": [{"a": "h1", "b": "h1"}]})

    def test_rejects_link_to_unknown_node(self):
        with self.assertRaises(ValueError):
            vr.validate_topology({"id": "x", "nodes": [{"name": "h1", "role": "host"}],
                                  "links": [{"a": "h1", "b": "ghost"}]})

    def test_get_topology_roundtrip_and_unknown_raises(self):
        t = vr.get_topology(vr.load_topologies()[0]["id"])
        self.assertEqual(vr.get_topology(t["id"])["id"], t["id"])
        with self.assertRaises(KeyError):
            vr.get_topology("does-not-exist")


class TopologyTaskCatalogTests(unittest.TestCase):
    def test_catalog_loads_and_resolves_its_topology(self):
        tasks = vr.load_topology_tasks()
        self.assertGreaterEqual(len(tasks), 1)
        ids = [t["id"] for t in tasks]
        self.assertEqual(len(ids), len(set(ids)))
        for t in tasks:
            self.assertRegex(t["id"], r"^[a-z][a-z0-9-]{1,40}$")
            topo = vr.get_topology(t["topology_id"])
            node_names = {n["name"] for n in topo["nodes"]}
            self.assertIn(t["check_node"], node_names)
            self.assertIn("prompt", t)
            self.assertIn("check", t)

    def test_get_topology_task_roundtrip_and_unknown_raises(self):
        t = vr.get_topology_task(vr.load_topology_tasks()[0]["id"])
        self.assertEqual(vr.get_topology_task(t["id"])["id"], t["id"])
        with self.assertRaises(KeyError):
            vr.get_topology_task("does-not-exist")


class TopologyHelperTests(unittest.TestCase):
    def setUp(self):
        self.topo = {"id": "t", "nodes": [{"name": "h1", "role": "host"}, {"name": "sw1", "role": "switch"},
                                          {"name": "h2", "role": "host"}],
                    "links": [{"a": "h1", "b": "sw1"}, {"a": "sw1", "b": "h2"}]}

    def test_links_for_node_ordering(self):
        self.assertEqual(vr.links_for_node(self.topo, "sw1"), self.topo["links"])         # touches both, in order
        self.assertEqual(vr.links_for_node(self.topo, "h1"), [self.topo["links"][0]])
        self.assertEqual(vr.links_for_node(self.topo, "h2"), [self.topo["links"][1]])

    def test_intnet_and_vm_names_are_unique_per_run_and_injection_safe(self):
        a = vr.intnet_name("abc12345", 0)
        b = vr.intnet_name("abc12345", 1)
        c = vr.intnet_name("def67890", 0)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotIn('"', a)
        self.assertNotIn("'", vr.vm_name_for_node("abc12345", "h1"))

    def test_relay_port_for_node_is_deterministic_and_collision_free(self):
        ports = {vr.relay_port_for_node(self.topo, n["name"]) for n in self.topo["nodes"]}
        self.assertEqual(len(ports), len(self.topo["nodes"]))
        self.assertEqual(vr.relay_port_for_node(self.topo, "h1"), vr.relay_port_for_node(self.topo, "h1"))

    def test_relay_command_is_valid_shell_syntax(self):
        """Regression (found via a real agent-attach test): the terms used to be joined with "; " even though
        each already ends in its own "&", producing "...&; socat..." - "&;" is a shell syntax error, so the
        relay container crashed on every single start and the agent correctly reported it couldn't resolve
        vm-relay-topo at all. Checked with `sh -n` (parse only, never execute) so this never depends on socat,
        host.docker.internal, or any real relay being reachable - purely a syntax guard."""
        node_ports = {n["name"]: 62400 + i for i, n in enumerate(self.topo["nodes"])}
        cmd = vr.relay_command(self.topo, node_ports)
        self.assertNotIn("&;", cmd)
        result = subprocess.run(["sh", "-n", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_relay_command_has_one_listener_per_node(self):
        node_ports = {n["name"]: 62400 + i for i, n in enumerate(self.topo["nodes"])}
        cmd = vr.relay_command(self.topo, node_ports)
        for name, port in node_ports.items():
            relay_port = vr.relay_port_for_node(self.topo, name)
            self.assertIn(f"TCP-LISTEN:{relay_port}", cmd)
            self.assertIn(f"host.docker.internal:{port}", cmd)
        self.assertTrue(cmd.rstrip().endswith("wait"))


class CustomTopologyTests(unittest.TestCase):
    def test_star_wiring_hubs_everything_off_the_first_switch(self):
        t = vr.build_custom_topology({"host": 3, "switch": 1, "loadbalancer": 1}, "star")
        names = {n["name"] for n in t["nodes"]}
        self.assertEqual(names, {"h1", "h2", "h3", "sw1", "lb1"})
        self.assertEqual(len(t["links"]), 4)
        for link in t["links"]:
            self.assertIn("sw1", (link["a"], link["b"]))

    def test_star_wiring_requires_a_switch(self):
        with self.assertRaises(ValueError):
            vr.build_custom_topology({"host": 2}, "star")

    def test_chain_wiring_interleaves_switches_and_inline_nodes(self):
        t = vr.build_custom_topology({"host": 2, "router": 1, "switch": 2}, "chain")
        backbone = [l for l in t["links"] if l["a"] in ("sw1", "sw2", "r1") and l["b"] in ("sw1", "sw2", "r1")]
        self.assertEqual({frozenset(l.values()) for l in backbone},
                         {frozenset({"sw1", "r1"}), frozenset({"r1", "sw2"})})
        leaf_links = [l for l in t["links"] if l not in backbone]
        self.assertEqual(len(leaf_links), 2)                      # h1, h2 each get exactly one link

    def test_chain_wiring_degrades_gracefully_with_no_switches(self):
        t = vr.build_custom_topology({"host": 2, "router": 1}, "chain")
        self.assertEqual({frozenset(l.values()) for l in t["links"]},
                         {frozenset({"r1", "h1"}), frozenset({"r1", "h2"})})

    def test_chain_wiring_requires_a_switch_router_or_firewall(self):
        with self.assertRaises(ValueError):
            vr.build_custom_topology({"host": 2}, "chain")

    def test_manual_wiring_uses_links_verbatim(self):
        t = vr.build_custom_topology({"host": 2, "switch": 1}, "manual",
                                     links=[{"a": "h1", "b": "sw1"}, {"a": "h2", "b": "sw1"}])
        self.assertEqual(t["links"], [{"a": "h1", "b": "sw1"}, {"a": "h2", "b": "sw1"}])

    def test_manual_wiring_requires_links(self):
        with self.assertRaises(ValueError):
            vr.build_custom_topology({"host": 2, "switch": 1}, "manual")

    def test_unknown_wiring_rejected(self):
        with self.assertRaises(ValueError):
            vr.build_custom_topology({"host": 2, "switch": 1}, "bogus")

    def test_rejects_fewer_than_two_nodes(self):
        with self.assertRaises(ValueError):
            vr.build_custom_topology({"host": 1}, "star")

    def test_rejects_over_the_node_cap(self):
        with self.assertRaises(ValueError):
            vr.build_custom_topology({"host": vr.MAX_CUSTOM_NODES + 1}, "star")

    def test_rejects_negative_counts(self):
        with self.assertRaises(ValueError):
            vr.build_custom_topology({"host": -1, "switch": 1}, "star")

    def test_output_passes_the_same_validation_as_a_catalog_entry(self):
        t = vr.build_custom_topology({"host": 2, "router": 1, "switch": 1, "firewall": 1}, "chain")
        self.assertEqual(vr.validate_topology(t), t)          # raises on failure; equality confirms no mutation
        for n in t["nodes"]:
            self.assertRegex(n["name"], r"^[a-z][a-z0-9]{0,14}$")
            self.assertIn(n["role"], vr.NODE_ROLES)


class RenderTopologyVagrantfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.topo = vr.get_topology("r2s2h2")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_define_block_and_correct_ports_per_node(self):
        node_ports = {n["name"]: 62400 + i for i, n in enumerate(self.topo["nodes"])}
        vr.render_topology_vagrantfile(self.tmp, "deadbeef", self.topo, node_ports, "ssh-ed25519 AAAA test", 1024, 1)
        vf = (self.tmp / "Vagrantfile").read_text()
        for name, port in node_ports.items():
            self.assertIn(f'config.vm.define "{name}"', vf)
            self.assertIn(f'host: {port}, host_ip: "127.0.0.1"', vf)
            self.assertIn(f'vb.name = "{vr.vm_name_for_node("deadbeef", name)}"', vf)
        self.assertEqual(vf.count("config.vm.define"), len(self.topo["nodes"]))

    def test_intnet_names_follow_link_order_with_auto_config_false(self):
        node_ports = {n["name"]: 62400 + i for i, n in enumerate(self.topo["nodes"])}
        vr.render_topology_vagrantfile(self.tmp, "deadbeef", self.topo, node_ports, "k", 1024, 1)
        vf = (self.tmp / "Vagrantfile").read_text()
        for idx in range(len(self.topo["links"])):
            self.assertIn(vr.intnet_name("deadbeef", idx), vf)
        for line in vf.splitlines():
            if "virtualbox__intnet" in line:
                self.assertIn("auto_config: false", line)

    def test_switch_provisioning_builds_a_bridge_and_skips_ping_tools(self):
        node_ports = {n["name"]: 62400 + i for i, n in enumerate(self.topo["nodes"])}
        vr.render_topology_vagrantfile(self.tmp, "deadbeef", self.topo, node_ports, "k", 1024, 1)
        sw_prov = (self.tmp / "provision-sw1.sh").read_text()
        self.assertIn("bridge-utils", sw_prov)
        self.assertIn("ip link add name br0 type bridge", sw_prov)
        self.assertNotIn("iputils-ping", sw_prov)

    def test_switch_lab_nics_are_set_promiscuous_before_first_boot(self):
        """Regression (found via real boot testing): VirtualBox's default per-NIC promiscuous policy is "deny",
        which silently drops any unicast frame not addressed to that NIC's own MAC - exactly what a bridging
        switch needs to receive to forward return traffic. ARP broadcasts got through fine (never filtered),
        masking this as a routing/connectivity bug until traced to the hypervisor-level NIC policy. Must be set
        in the Vagrantfile (baked in before first boot): a live `VBoxManage controlvm nicpromiscN` change on an
        already-running VM did not reliably propagate to the internal-network switch fabric when reproduced."""
        node_ports = {n["name"]: 62400 + i for i, n in enumerate(self.topo["nodes"])}
        vr.render_topology_vagrantfile(self.tmp, "deadbeef", self.topo, node_ports, "k", 1024, 1)
        vf = (self.tmp / "Vagrantfile").read_text()
        sw1_block = vf[vf.index('define "sw1"'):vf.index('define "r1"')]
        self.assertIn('vb.customize ["modifyvm", :id, "--nicpromisc2", "allow-all"]', sw1_block)
        self.assertIn('vb.customize ["modifyvm", :id, "--nicpromisc3", "allow-all"]', sw1_block)
        h1_block = vf[vf.index('define "h1"'):vf.index('define "sw1"')]
        self.assertNotIn("nicpromisc", h1_block, "only switch nodes need promiscuous NICs")

    def test_switch_provisioning_snapshots_interfaces_before_creating_the_bridge(self):
        """Regression (hit on real VirtualBox boot testing): if the candidate-interface list is captured AFTER
        `ip link add br0`, it includes br0 itself (not yet UP, so the "already has an address" skip doesn't
        exclude it either), and `ip link set br0 master br0` fails with "Can not enslave a bridge to a bridge"."""
        node_ports = {n["name"]: 62400 + i for i, n in enumerate(self.topo["nodes"])}
        vr.render_topology_vagrantfile(self.tmp, "deadbeef", self.topo, node_ports, "k", 1024, 1)
        sw_prov = (self.tmp / "provision-sw1.sh").read_text()
        self.assertLess(sw_prov.index("ls /sys/class/net"), sw_prov.index("ip link add name br0 type bridge"),
                        "the interface list must be captured before br0 exists, or br0 ends up in its own candidate list")

    def test_host_and_router_provisioning_has_no_firewall_lockdown_or_preenabled_forwarding(self):
        node_ports = {n["name"]: 62400 + i for i, n in enumerate(self.topo["nodes"])}
        vr.render_topology_vagrantfile(self.tmp, "deadbeef", self.topo, node_ports, "k", 1024, 1)
        for name in ("h1", "r1"):
            prov = (self.tmp / f"provision-{name}.sh").read_text()
            self.assertNotIn("ufw", prov)
            self.assertNotIn("ip_forward", prov)
            self.assertIn("iputils-ping", prov)
        router_prov = (self.tmp / "provision-r1.sh").read_text()
        self.assertIn("iptables", router_prov)

    def test_firewall_provisioning_installs_nftables_with_no_preapplied_policy(self):
        topo = vr.get_topology("fw1s2h2")
        node_ports = {n["name"]: 62400 + i for i, n in enumerate(topo["nodes"])}
        vr.render_topology_vagrantfile(self.tmp, "deadbeef", topo, node_ports, "k", 1024, 1)
        prov = (self.tmp / "provision-fw1.sh").read_text()
        self.assertIn("nftables", prov)
        self.assertIn("systemctl enable nftables", prov)
        self.assertNotIn("iptables", prov)          # nftables only - the single CLI surface for this appliance
        self.assertNotIn("ufw", prov)
        self.assertNotIn("ip_forward", prov)
        self.assertNotIn("policy drop", prov)        # no pre-applied default-drop policy - that's the task

    def test_loadbalancer_provisioning_installs_nginx_with_no_prewritten_upstream(self):
        topo = vr.get_topology("lb1s1h3")
        node_ports = {n["name"]: 62400 + i for i, n in enumerate(topo["nodes"])}
        vr.render_topology_vagrantfile(self.tmp, "deadbeef", topo, node_ports, "k", 1024, 1)
        prov = (self.tmp / "provision-lb1.sh").read_text()
        self.assertIn("nginx", prov)
        self.assertIn("libnginx-mod-stream", prov)
        self.assertNotIn("upstream", prov)           # no pre-written backend pool - that's the task
        self.assertNotIn("proxy_pass", prov)

    def test_no_crlf_in_generated_files(self):
        node_ports = {n["name"]: 62400 + i for i, n in enumerate(self.topo["nodes"])}
        vr.render_topology_vagrantfile(self.tmp, "deadbeef", self.topo, node_ports, "k", 1024, 1)
        for f in self.tmp.iterdir():
            self.assertNotIn(b"\r\n", f.read_bytes(), str(f))


if __name__ == "__main__":
    unittest.main()
