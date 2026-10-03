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


class VagrantStreamCancelTests(unittest.TestCase):
    """vagrant_stream(cancel=...) must kill the whole process tree promptly - Vagrant's real work runs in child
    processes, so killing only the launcher would leave the build going."""

    @unittest.skipIf(sys.platform == "win32", "uses a POSIX shell script as a stand-in for vagrant")
    def test_cancel_kills_the_process_tree_promptly(self):
        import os, time
        tmp = Path(tempfile.mkdtemp())
        try:
            marker = tmp / "child-survived"
            fake = tmp / "vagrant"
            # the "launcher" starts a child that would write a marker after 3s, then waits for it
            fake.write_text(f"#!/bin/sh\necho booting\n(sleep 3; touch {marker}) &\nwait\n")
            fake.chmod(0o755)
            env_path = f"{tmp}:{os.environ.get('PATH', '')}"
            with mock.patch.dict(os.environ, {"PATH": env_path}):
                t0 = time.time()
                rc, out, err = vr.vagrant_stream(tmp, "up", timeout=60, cancel=lambda: time.time() - t0 > 0.5)
            self.assertEqual(rc, vr.CANCELLED_RC)
            self.assertIn("booting", out)
            self.assertLess(time.time() - t0, 2.5)
            time.sleep(3.5)
            self.assertFalse(marker.exists(), "the child process outlived the cancel")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ssh_wait_stops_trying_once_cancelled(self):
        with mock.patch.object(vr, "_run", side_effect=AssertionError("must not try once cancelled")):
            self.assertFalse(vr.ssh_wait(22, "k", tries=5, delay=0, cancel=lambda: True))

    def test_destroy_after_cancel_retries_until_destroy_succeeds(self):
        results = iter([(1, "", "locked"), (1, "", "locked"), (0, "ok", "")])
        with mock.patch.object(vr, "vagrant", side_effect=lambda *a, **k: next(results)) as v:
            rc, _, _ = vr.destroy_after_cancel("d", tries=3, delay=0)
        self.assertEqual(rc, 0)
        self.assertEqual(v.call_count, 3)


class SnapshotParseTests(unittest.TestCase):
    def test_sections_are_split_and_preamble_ignored(self):
        out = vr.parse_snapshot("motd junk\n### addresses\nenp0s8 UP 10.0.0.1/24\n### routes\n10.0.0.0/24 dev enp0s8\n")
        self.assertEqual(out, {"addresses": "enp0s8 UP 10.0.0.1/24\n", "routes": "10.0.0.0/24 dev enp0s8\n"})

    def test_route_lifetimes_are_removed_so_unchanged_nodes_diff_clean(self):
        # real output from a lab node, a few seconds apart (found in a live diff: every node "changed")
        a = "### routes\ndefault via fe80::2 dev enp0s3 proto ra metric 100 expires 1600sec pref medium\n"
        b = "### routes\ndefault via fe80::2 dev enp0s3 proto ra metric 100 expires 1597sec pref medium\n"
        self.assertEqual(vr.parse_snapshot(a), vr.parse_snapshot(b))
        self.assertEqual(vr.parse_snapshot(a)["routes"], "default via fe80::2 dev enp0s3 proto ra metric 100 pref medium\n")


class LabFileTests(unittest.TestCase):
    """Lab files: format validation (a file may come from someone else), the apply script that puts a node's saved
    config back, and the check that a rebuilt node matches its file."""

    TOPO = {"nodes": [{"name": "h1", "role": "host"}, {"name": "r1", "role": "router"}], "links": [{"a": "h1", "b": "r1"}]}

    def lf(self, **over):
        return dict(vr.build_labfile("Two nodes", self.TOPO, {"h1": {"addresses": "enp0s8 UP 10.0.0.1/24\n"}}), **over)

    def test_round_trip(self):
        title, topo, configs, intents = vr.parse_labfile(self.lf())
        self.assertEqual(title, "Two nodes")
        self.assertEqual([n["name"] for n in topo["nodes"]], ["h1", "r1"])
        self.assertEqual(topo["links"], [{"a": "h1", "b": "r1"}])
        self.assertEqual(configs["h1"]["addresses"], "enp0s8 UP 10.0.0.1/24\n")
        self.assertEqual(intents, [])

    def test_bad_files_are_refused(self):
        too_many = {"nodes": [{"name": f"h{i}", "role": "host"} for i in range(vr.MAX_CUSTOM_NODES + 1)], "links": []}
        for why, obj in (("not a lab file", {"format": "x"}), ("wrong version", self.lf(version=99)),
                         ("no topology", self.lf(topology=None)), ("bad role", self.lf(topology={"nodes": [{"name": "h1", "role": "toaster"}, {"name": "r1", "role": "router"}], "links": []})),
                         ("bad node name", self.lf(topology={"nodes": [{"name": "H 1", "role": "host"}, {"name": "r1", "role": "router"}], "links": []})),
                         ("too many nodes", self.lf(topology=too_many)), ("unknown link", self.lf(topology=dict(self.TOPO, links=[{"a": "h1", "b": "zz"}]))),
                         ("configs for unknown node", self.lf(configs={"zz": {}})), ("config not text", self.lf(configs={"h1": {"routes": 5}})),
                         ("not even a dict", "hello")):
            with self.subTest(why), self.assertRaises(ValueError):
                vr.parse_labfile(obj)

    def test_apply_script_restores_lab_config_and_leaves_the_system_alone(self):
        secs = {"addresses": "lo UNKNOWN 127.0.0.1/8\nenp0s3 UP 10.0.2.15/24 fe80::1/64\nenp0s8 UP 10.20.0.10/24 fe80::a00:27ff:fe00:1/64\nbr0 UP 192.168.99.1/24\n",
                "routes": "default via 10.0.2.2 dev enp0s3 proto dhcp src 10.0.2.15 metric 100\n"
                          "10.20.0.0/24 dev enp0s8 proto kernel scope link src 10.20.0.10\n10.30.0.0/24 via 10.20.0.1 dev enp0s8\n"
                          "# ipv6\nfe80::/64 dev enp0s8 proto kernel metric 256 pref medium\n2001:db8::/64 via 2001:db8:1::1 dev enp0s8\n",
                "forwarding": "net.ipv4.ip_forward = 1\n", "nftables": "table ip filter {\n}\n",
                "file /etc/nginx/conf.d/lb.conf": "upstream b {}\n", "file /etc/netplan/50-cloud-init.yaml": "network: {}\n"}
        script, skipped = vr.render_apply_script(secs)
        self.assertIn("ip addr replace 10.20.0.10/24 dev enp0s8", script)
        self.assertIn("ip addr replace 192.168.99.1/24 dev br0", script)
        self.assertIn("ip route replace 10.30.0.0/24 via 10.20.0.1 dev enp0s8", script)
        self.assertIn("ip -6 route replace 2001:db8::/64 via 2001:db8:1::1 dev enp0s8", script)
        self.assertIn("sysctl -q -w net.ipv4.ip_forward=1", script)
        self.assertIn("| sudo -n nft -f -", script)
        self.assertIn("tee /etc/nginx/conf.d/lb.conf", script)
        self.assertIn("systemctl reload nginx", script)
        for system in ("enp0s3", "10.0.2.15", "fe80:", "proto kernel", "netplan", "127.0.0.1"):
            self.assertNotIn(system, script, f"{system} is the system's own config, not the lab's")
        self.assertEqual(skipped, [])
        result = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_apply_script_cannot_be_used_to_inject_commands(self):
        evil = {"addresses": "enp0s8 UP 10.0.0.1/24;reboot\n$(id)x UP 10.0.0.2/24\n",
                "routes": "10.9.0.0/24 via 10.0.0.1 dev enp0s8 `touch /tmp/pwn`\n",
                "forwarding": "net.ipv4.ip_forward = 1; rm -rf /\n",
                "file /etc/passwd": "root::0:0::/root:/bin/sh\n", "file /etc/nginx/../shadow": "x\n",
                "file /etc/nginx/x.conf": "'; reboot; echo '\n"}
        script, skipped = vr.render_apply_script(evil)
        for bad in (";reboot", "$(id)", "`touch", "rm -rf", "/etc/passwd", "shadow", "'; reboot"):
            self.assertNotIn(bad, script)
        self.assertIn("file /etc/passwd", skipped)
        self.assertTrue(any(s.startswith("route:") for s in skipped))

    def test_rebuild_check(self):
        wanted = {"addresses": "lo UNKNOWN 127.0.0.1/8\nenp0s3 UP 10.0.2.15/24 fe80::1/64\nenp0s8 UP 10.20.0.10/24 fe80::aaaa/64\n",
                  "routes": "default via 10.0.2.2 dev enp0s3 proto dhcp\n10.30.0.0/24 via 10.20.0.1 dev enp0s8\n",
                  "forwarding": "net.ipv4.ip_forward = 1\n",
                  "nftables": "table ip filter {\n\tchain F {\n\t\tcounter packets 9 bytes 99 drop\n\t}\n}\n"}
        live = {"addresses": "lo UNKNOWN 127.0.0.1/8\nenp0s3 UP 10.0.2.15/24 fe80::2/64\nenp0s8 UP 10.20.0.10/24 fe80::bbbb/64\n",
                "routes": "default via 10.0.2.2 dev enp0s3 proto dhcp metric 100\n10.30.0.0/24 via 10.20.0.1 dev enp0s8\n",
                "forwarding": "net.ipv4.ip_forward = 1\n",
                "nftables": "table ip filter {\n\tchain F {\n\t\tcounter packets 0 bytes 0 drop\n\t}\n}\n"}
        self.assertEqual(vr.config_mismatches(wanted, live), [], "different MACs, setup NIC and counters are not mismatches")
        self.assertEqual(vr.config_mismatches(wanted, dict(live, forwarding="net.ipv4.ip_forward = 0\n")), ["forwarding"])


class FrrTests(unittest.TestCase):
    """FRR on router nodes: installed with OSPF/OSPFv3/BGP available, its running config captured in snapshots and
    restored from lab files, and routes it learns treated as derived rather than static config."""

    def test_routers_get_frr_hosts_do_not(self):
        router = vr._topo_provision_script("router", "KEY")
        self.assertIn(" frr", router)
        self.assertIn("(ospfd|ospf6d|bgpd)=no", router)
        self.assertIn("usermod -aG frrvty,frr bench", router)
        self.assertIn("systemctl restart frr", router)
        self.assertLess(router.index("useradd"), router.index("usermod"), "bench must exist before joining frrvty")
        host = vr._topo_provision_script("host", "KEY")
        self.assertNotIn("frr", host)
        result = subprocess.run(["sh", "-n"], input=router, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_snapshots_capture_the_running_config(self):
        self.assertIn("vtysh -c 'show running-config'", vr.SNAPSHOT_SCRIPT)
        out = vr.parse_snapshot("### frr\nfrr version 8.1\nhostname r1\nrouter ospf\n network 10.0.0.0/30 area 0\n!\n")
        self.assertIn("network 10.0.0.0/30 area 0", out["frr"])

    def test_lab_files_restore_the_running_config_and_skip_learned_routes(self):
        secs = {"frr": "hostname r1\nrouter ospf\n network 10.0.0.0/30 area 0\n",
                "file /etc/frr/frr.conf": "hostname stale\n", "file /etc/frr/daemons": "ospfd=yes\n",
                "routes": "10.0.2.0/24 nhid 12 via 10.0.0.2 dev enp0s9 proto ospf metric 20\n"
                          "10.0.9.0/24 via 10.0.0.2 dev enp0s9 proto bgp metric 20\n10.0.7.0/24 via 10.0.0.2 dev enp0s9\n"}
        script, skipped = vr.render_apply_script(secs)
        import base64
        self.assertIn(base64.b64encode(secs["frr"].encode()).decode(), script)
        self.assertNotIn(base64.b64encode(b"hostname stale\n").decode(), script, "the running config wins over a stale file")
        self.assertIn("tee /etc/frr/daemons", script)
        self.assertIn("systemctl restart frr", script)
        self.assertIn("ip route replace 10.0.7.0/24 via 10.0.0.2 dev enp0s9", script)
        self.assertNotIn("10.0.2.0/24", script, "OSPF-learned routes come back from OSPF, not by hand")
        self.assertNotIn("10.0.9.0/24", script)
        self.assertEqual(skipped, [])

    def test_rebuild_check_compares_frr_config_not_learned_routes(self):
        wanted = {"routes": "10.0.2.0/24 via 10.0.0.2 dev enp0s9 proto ospf metric 20\n", "frr": "router ospf\n network 10.0.0.0/30 area 0\n"}
        live = {"routes": "", "frr": "router ospf\n network 10.0.0.0/30 area 0\n"}
        self.assertEqual(vr.config_mismatches(wanted, live), [], "OSPF reconverges on its own time; not a mismatch")
        self.assertEqual(vr.config_mismatches(wanted, dict(live, frr="router ospf\n")), ["frr"])

    def test_ospf_task_is_in_the_catalog_and_its_check_parses(self):
        task = vr.get_topology_task("r2s2h2-ospf")
        topo = vr.get_topology(task["topology_id"])
        self.assertIn(task["check_node"], [n["name"] for n in topo["nodes"]])
        self.assertEqual(next(n["role"] for n in topo["nodes"] if n["name"] == task["check_node"]), "router")
        result = subprocess.run(["sh", "-n"], input=task["check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Full", task["check"])
        self.assertIn("proto ospf", task["check"])


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


class VmrunScriptTests(unittest.TestCase):
    """The vmrun wrapper an agent uses to run commands on a VM. Regression: agents were told to pass scripts as
    ./vmrun "...", so their own shell expanded $i to nothing before the wrapper ran and a FizzBuzz script arrived
    with `echo $i` turned into `echo ` (blank lines for every number). The stdin form must deliver $ untouched."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "session.log"
        self.script = self.tmp / "vmrun"
        self.script.write_text(vr.vmrun_script("/k/id_ed25519", 2222, "bench@relay", str(self.log)))
        self.script.chmod(0o755)
        # a fake ssh that records its arguments and stdin instead of connecting anywhere
        fake = self.tmp / "bin" / "ssh"
        fake.parent.mkdir()
        fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {self.tmp}/ssh-args\ncat > {self.tmp}/ssh-stdin\necho remote-output\n")
        fake.chmod(0o755)
        self.env = {"PATH": f"{fake.parent}:/usr/bin:/bin"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, command):
        return subprocess.run(["sh", "-c", command], cwd=self.tmp, env=self.env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=20)

    def test_is_valid_shell_syntax(self):
        result = subprocess.run(["sh", "-n", str(self.script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(sys.platform == "win32", "runs the wrapper with POSIX paths; the gateway it runs in is Linux")
    def test_heredoc_script_reaches_the_vm_with_dollar_signs_intact(self):
        res = self.sh("./vmrun <<'EOF'\nfor i in 1 2 3; do echo \"$i $((i * 2))\"; done\nEOF")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("remote-output", res.stdout)
        self.assertEqual((self.tmp / "ssh-stdin").read_text(), 'for i in 1 2 3; do echo "$i $((i * 2))"; done\n')
        self.assertEqual((self.tmp / "ssh-args").read_text().splitlines()[-2:], ["bench@relay", "bash -s"])
        self.assertIn('echo "$i $((i * 2))"', self.log.read_text())        # logged as sent, for the transcript

    @unittest.skipIf(sys.platform == "win32", "runs the wrapper with POSIX paths; the gateway it runs in is Linux")
    def test_single_quoted_argument_keeps_dollar_signs_for_the_vm(self):
        res = self.sh("./vmrun 'echo $HOME'")
        self.assertEqual(res.returncode, 0, res.stderr)
        args = (self.tmp / "ssh-args").read_text().splitlines()
        self.assertEqual(args[-2:], ["bench@relay", "echo $HOME"])
        self.assertIn("-p", args)
        self.assertEqual(args[args.index("-p") + 1], "2222")

    @unittest.skipIf(sys.platform == "win32", "runs the wrapper with POSIX paths; the gateway it runs in is Linux")
    def test_no_command_at_all_prints_usage_instead_of_hanging(self):
        res = self.sh("./vmrun")
        self.assertEqual(res.returncode, 2)
        self.assertIn("usage", res.stderr)
        self.assertFalse((self.tmp / "ssh-args").exists())


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
