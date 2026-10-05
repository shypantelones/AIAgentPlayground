"""Tests for container labs (container_lab.py): which nodes are containers, the networks and containers a lab makes,
the docker commands it runs, and the diagram's interface names for container nodes. Docker itself is mocked; a live
run on a Docker host is the real check (see the vm-lab-dev skill).
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import container_lab as cm  # noqa: E402
import vm_runner as vr  # noqa: E402

TOPO = {"id": "t", "title": "two routers", "nodes": [{"name": "h1", "role": "host"}, {"name": "r1", "role": "router"},
                                                     {"name": "h2", "role": "host"}],
        "links": [{"a": "h1", "b": "r1"}, {"a": "r1", "b": "h2"}]}
PORTS = {"h1": 2201, "r1": 2202, "h2": 2203}


class BackendTests(unittest.TestCase):
    def test_full_makes_every_node_a_container(self):
        for role in vr.NODE_ROLES:
            self.assertEqual(cm.node_backend("full", role), "container")

    def test_vm_makes_every_node_a_vm(self):
        for role in vr.NODE_ROLES:
            self.assertEqual(cm.node_backend("vm", role), "vm")

    def test_mixed_containers_only_the_network_devices(self):
        self.assertEqual(cm.node_backend("mixed", "router"), "container")
        self.assertEqual(cm.node_backend("mixed", "firewall"), "container")
        self.assertEqual(cm.node_backend("mixed", "host"), "vm")
        self.assertEqual(cm.node_backend("mixed", "server"), "vm")

    def test_validate_refuses_mixed_until_it_is_built_and_unknown_modes(self):
        with self.assertRaisesRegex(ValueError, "not built yet"):
            cm.validate_build_mode("mixed", TOPO)
        with self.assertRaisesRegex(ValueError, "unknown build mode"):
            cm.validate_build_mode("cloud", TOPO)
        cm.validate_build_mode("vm", TOPO)
        cm.validate_build_mode("full", TOPO)

    def test_a_container_node_with_too_many_links_is_refused(self):
        wide = {"nodes": [{"name": "r1", "role": "router"}] + [{"name": f"h{i}", "role": "host"} for i in range(17)],
                "links": [{"a": "r1", "b": f"h{i}"} for i in range(17)]}
        with self.assertRaisesRegex(ValueError, "at most 16 lab links"):
            cm.validate_build_mode("full", wide)


class PlanTests(unittest.TestCase):
    def test_the_only_network_is_management_and_links_are_not_networks(self):
        p = cm.plan("abc", TOPO, PORTS)
        self.assertEqual(p["management"], "aglab-abc-mgmt")
        self.assertEqual(len(p["links"]), 2)

    def test_each_container_gets_its_interfaces_in_link_order_and_its_port(self):
        by_node = {c["node"]: c for c in cm.plan("abc", TOPO, PORTS)["containers"]}
        self.assertEqual(by_node["r1"]["ifaces"], ["eth1", "eth2"])
        self.assertEqual(by_node["h1"]["ifaces"], ["eth1"])
        self.assertEqual(by_node["h2"]["port"], 2203)
        self.assertEqual(by_node["r1"]["container"], "aglab-abc-r1")

    def test_each_link_names_the_interface_at_each_end(self):
        links = cm.plan("abc", TOPO, PORTS)["links"]
        self.assertEqual(links[0], {"index": 0, "a": "aglab-abc-h1", "a_iface": "eth1",
                                    "b": "aglab-abc-r1", "b_iface": "eth1"})
        self.assertEqual(links[1], {"index": 1, "a": "aglab-abc-r1", "a_iface": "eth2",
                                    "b": "aglab-abc-h2", "b_iface": "eth1"})

    def test_interface_names_start_at_eth1_because_eth0_is_management(self):
        self.assertEqual(cm.lab_iface_names(2), ["eth1", "eth2"])
        self.assertEqual(cm.lab_iface_names(0), [])


class UpTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.links = []
        self.logged = []

        def fake_docker(*args, input=None, timeout=120):
            self.calls.append(list(args))
            return 0, "", ""

        def fake_link(rid, idx, ca, ia, cb, ib, helper=None):
            self.links.append((idx, ca, ia, cb, ib))
            return 0, ""

        self.forwarding = []

        def fake_forwarding(container, on, helper=None):
            self.forwarding.append((container, on))
            return 0, ""

        self.patches = [mock.patch.object(cm, "docker", side_effect=fake_docker),
                        mock.patch.object(cm, "make_link", side_effect=fake_link),
                        mock.patch.object(cm, "set_forwarding", side_effect=fake_forwarding)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_only_the_management_network_is_a_docker_network_and_it_has_no_masquerade(self):
        rc, _ = cm.up("abc", TOPO, PORTS, "ssh-ed25519 AAA lab", 512, 1, self.logged.append)
        self.assertEqual(rc, 0)
        nets = [c for c in self.calls if c[:2] == ["network", "create"]]
        self.assertEqual(len(nets), 1)
        self.assertIn("com.docker.network.bridge.enable_ip_masquerade=false", nets[0])
        self.assertIn("aglab-abc-mgmt", nets[0])

    def test_every_container_publishes_ssh_on_loopback_only_and_gets_the_key(self):
        cm.up("abc", TOPO, PORTS, "ssh-ed25519 AAA lab", 512, 1, self.logged.append)
        create_h1 = next(c for c in self.calls if c[0] == "create" and "aglab-abc-h1" in c)
        self.assertIn("127.0.0.1:2201:22", create_h1)
        self.assertIn("AG_PUBKEY=ssh-ed25519 AAA lab", create_h1)
        self.assertIn("NET_ADMIN", create_h1)
        self.assertIn("--memory", create_h1)
        self.assertIn("AG_ROLE=router", next(c for c in self.calls if c[0] == "create" and "aglab-abc-r1" in c))
        self.assertIn("AG_LAB_LINKS=2", next(c for c in self.calls if c[0] == "create" and "aglab-abc-r1" in c))

    def test_each_link_is_made_once_through_the_helper_with_both_ends(self):
        cm.up("abc", TOPO, PORTS, "k", 512, 1, self.logged.append)
        self.assertEqual(self.links, [(0, "aglab-abc-h1", "eth1", "aglab-abc-r1", "eth1"),
                                      (1, "aglab-abc-r1", "eth2", "aglab-abc-h2", "eth1")])

    def test_containers_start_before_links_and_wiring_comes_after_them(self):
        order = []
        with mock.patch.object(cm, "docker", side_effect=lambda *a, **k: (order.append(a[0]), (0, "", ""))[1]), \
                mock.patch.object(cm, "make_link", side_effect=lambda *a, **k: (order.append("link"), (0, ""))[1]):
            cm.up("abc", TOPO, PORTS, "k", 512, 1, self.logged.append)
        links = [i for i, x in enumerate(order) if x == "link"]
        starts = [i for i, x in enumerate(order) if x == "start"]
        execs = [i for i, x in enumerate(order) if x == "exec"]
        self.assertTrue(max(starts) < min(links))              # every container is running before any link
        self.assertTrue(max(links) < min(execs))               # every link exists before any node is wired

    def test_a_failed_link_stops_the_build_and_returns_its_output(self):
        with mock.patch.object(cm, "make_link", return_value=(2, "h1 already has eth1")):
            rc, out = cm.up("abc", TOPO, PORTS, "k", 512, 1, self.logged.append)
        self.assertEqual(rc, 2)
        self.assertIn("already has eth1", out)
        self.assertFalse(any(c[0] == "exec" for c in self.calls))

    def test_every_node_starts_with_forwarding_off_through_the_helper(self):
        cm.up("abc", TOPO, PORTS, "k", 512, 1, self.logged.append)
        self.assertEqual(sorted(self.forwarding), sorted([("aglab-abc-h1", False), ("aglab-abc-r1", False),
                                                          ("aglab-abc-h2", False)]))

    def test_the_image_is_built_once_when_missing(self):
        def no_image(*args, input=None, timeout=120):
            self.calls.append(list(args))
            return (1, "", "no such image") if args[:2] == ("image", "inspect") else (0, "", "")
        with mock.patch.object(cm, "docker", side_effect=no_image):
            cm.up("abc", TOPO, PORTS, "k", 512, 1, self.logged.append)
        self.assertIn(["build", "-t", cm.IMAGE, str(cm.IMAGE_DIR)], self.calls)
        self.assertTrue(any("first time" in line for line in self.logged))


class LinkHelperArgsTests(unittest.TestCase):
    def test_the_helper_gets_the_lab_and_both_ends_and_bad_nodes_never_reach_it(self):
        with mock.patch.object(cm, "_run_helper", return_value=(0, "")) as run, \
                mock.patch.object(cm.os, "geteuid", return_value=0, create=True):
            cm.make_link("abc12345", 0, "aglab-abc12345-h1", "eth1", "aglab-abc12345-r1", "eth1", helper="/x/link")
            self.assertEqual(run.call_args[0][0], ["/x/link", "abc12345", "0", "aglab-abc12345-h1", "eth1",
                                                   "aglab-abc12345-r1", "eth1"])
            with self.assertRaisesRegex(ValueError, "not a node of lab"):
                cm.make_link("abc12345", 0, "aglab-deadbeef-h1", "eth1", "aglab-abc12345-r1", "eth1")
            with self.assertRaisesRegex(ValueError, "not a node of lab"):
                cm.make_link("abc12345", 0, "aglab-abc12345-h1; id", "eth1", "aglab-abc12345-r1", "eth1")

    def test_a_non_root_panel_calls_the_link_helper_through_sudo(self):
        with mock.patch.object(cm, "_run_helper", return_value=(0, "")) as run, \
                mock.patch.object(cm.os, "geteuid", return_value=1000, create=True):
            cm.make_link("abc12345", 1, "aglab-abc12345-r1", "eth2", "aglab-abc12345-h2", "eth1", helper="/x/link")
        self.assertEqual(run.call_args[0][0][:3], ["sudo", "-n", "/x/link"])


class TeardownTests(unittest.TestCase):
    def test_removes_the_labelled_containers_and_networks_and_is_safe_to_repeat(self):
        calls = []

        def fake_docker(*args, input=None, timeout=120):
            calls.append(list(args))
            if args[:2] == ("ps", "-aq"):
                return 0, "c1\nc2\n", ""
            if args[:2] == ("network", "ls"):
                return 0, "n1\n", ""
            return 0, "", ""

        with mock.patch.object(cm, "docker", side_effect=fake_docker):
            cm.teardown("abc")
        self.assertIn(["ps", "-aq", "--filter", f"label={cm.LABEL}=abc"], calls)
        self.assertIn(["rm", "-f", "c1", "c2"], calls)
        self.assertIn(["network", "rm", "n1"], calls)

    def test_nothing_to_remove_runs_no_rm(self):
        calls = []
        with mock.patch.object(cm, "docker", side_effect=lambda *a, **k: (calls.append(list(a)), (0, "", ""))[1]):
            cm.teardown("abc")
        self.assertFalse(any(c[0] == "rm" or c[:2] == ["network", "rm"] for c in calls))


class DockerMissingTests(unittest.TestCase):
    def test_no_docker_on_path_is_a_clear_error_not_a_crash(self):
        with mock.patch.object(cm.subprocess, "run", side_effect=FileNotFoundError):
            rc, _, err = cm.docker("ps")
        self.assertEqual(rc, 127)
        self.assertIn("not installed", err)


class DiagramTests(unittest.TestCase):
    def test_container_nodes_are_labelled_eth_and_vm_nodes_enp0s(self):
        d = vr.topology_diagram(TOPO, None, container_nodes={"r1"})
        r1_end = next(l for l in d["links"] if l["a"] == "h1")
        self.assertEqual(r1_end["b_if"], "eth1")               # r1 is the second end of h1's link
        mid = d["links"][1]
        self.assertEqual((mid["a_if"], mid["b_if"]), ("eth2", "enp0s8"))   # r1's second link, then h2 (a VM)
        self.assertEqual(d["links"][0]["a_if"], "enp0s8")                 # h1 is a VM: its first lab NIC

    def test_without_container_nodes_the_diagram_is_unchanged(self):
        d = vr.topology_diagram(TOPO, None)
        self.assertEqual(d["links"][0]["a_if"], "enp0s8")


class CreateRefusalTests(unittest.TestCase):
    """create_topo_run's checks for a container lab, run up to the point they decide (no lab is started)."""

    def form(self, **extra):
        f = {"topology_id": "t1", "build_mode": "full", "keep": "1"}
        f.update(extra)
        return f

    def test_a_container_lab_refuses_a_team_and_extra_domains(self):
        topo = {"id": "t1", "title": "x", "nodes": TOPO["nodes"], "links": TOPO["links"]}
        with mock.patch.object(app.vr, "get_topology", return_value=topo), \
                mock.patch.object(app.threading, "Thread"):
            with self.assertRaisesRegex(ValueError, "can't take a team"):
                app.create_topo_run(self.form(team="alpha | h1 | 1 | net"))
            with self.assertRaisesRegex(ValueError, "no internet access"):
                app.create_topo_run(self.form(extra_domains="docs.example.org"))


class ForwardingTests(unittest.TestCase):
    NAME = "aglab-0a1b2c3d-r1"

    def test_only_a_lab_node_name_and_a_boolean_reach_the_helper(self):
        with mock.patch.object(cm.subprocess, "run") as run:
            for bad in ["", "docker", "aglab-xyz-r1", "aglab-0a1b2c3d-r1; reboot", "aglab-0a1b2c3d-R1", None]:
                with self.subTest(name=bad), self.assertRaisesRegex(ValueError, "not a lab node"):
                    cm.set_forwarding(bad, True)
            run.assert_not_called()

    def test_on_and_off_pass_1_and_0_to_the_helper(self):
        with mock.patch.object(cm.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run, \
                mock.patch.object(cm.os, "geteuid", return_value=0, create=True):
            cm.set_forwarding(self.NAME, True, helper="/x/forward")
            self.assertEqual(run.call_args[0][0], ["/x/forward", self.NAME, "1"])
            cm.set_forwarding(self.NAME, False, helper="/x/forward")
            self.assertEqual(run.call_args[0][0], ["/x/forward", self.NAME, "0"])

    def test_a_non_root_panel_goes_through_sudo_without_a_prompt(self):
        with mock.patch.object(cm.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run, \
                mock.patch.object(cm.os, "geteuid", return_value=1000, create=True):
            cm.set_forwarding(self.NAME, True, helper="/x/forward")
        self.assertEqual(run.call_args[0][0], ["sudo", "-n", "/x/forward", self.NAME, "1"])

    def test_a_missing_helper_or_sudo_is_a_message_not_a_crash(self):
        with mock.patch.object(cm.subprocess, "run", side_effect=FileNotFoundError), \
                mock.patch.object(cm.os, "geteuid", return_value=0, create=True):
            rc, out = cm.set_forwarding(self.NAME, True, helper="/nope")
        self.assertEqual(rc, 127)
        self.assertIn("not installed", out)


@unittest.skipIf(sys.platform.startswith("win"), "the helper is a shell script")
@unittest.skipIf(sys.platform.startswith("win"), "the helper is a shell script")
class LinkHelperScriptTests(unittest.TestCase):
    """lab_node/link.sh run for real against stub docker, ip and nsenter binaries. The stubs log what would have run."""

    RID = "abc12345"

    def run_link(self, *args, pids=("4001", "4002"), running="true", label=None, existing=()):
        import os, shutil, subprocess, tempfile
        d = Path(tempfile.mkdtemp())
        log = d / "log"
        try:
            (d / "docker").write_text(
                '#!/bin/sh\n'
                'case "$*" in *%s-h1*) pid=%s;; *) pid=%s;; esac\n'
                'echo "$pid %s %s"\n' % (self.RID, pids[0], pids[1], running, self.RID if label is None else label))
            (d / "ip").write_text(
                '#!/bin/sh\n'
                'echo "ip $*" >> %s\n'
                'case "$1 $2" in "link show") for e in %s; do [ "$3" = "$e" ] && exit 0; done; exit 1;; esac\n'
                'exit 0\n' % (log, " ".join(existing) or "none"))
            (d / "nsenter").write_text(
                '#!/bin/sh\n'
                'echo "nsenter $*" >> %s\n'
                'case "$*" in *"link show"*) for e in %s; do case "$*" in *" $e") exit 0;; esac; done; exit 1;; esac\n'
                'exit 0\n' % (log, " ".join(existing) or "none"))
            for f in d.iterdir():
                f.chmod(0o755)
            env = dict(os.environ, PATH=f"{d}{os.pathsep}{os.environ['PATH']}")
            p = subprocess.run(["sh", str(cm.IMAGE_DIR / "link.sh"), *args], capture_output=True, text=True, env=env)
            calls = log.read_text() if log.exists() else ""
            return p, calls
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def good_args(self):
        return [self.RID, "0", f"aglab-{self.RID}-h1", "eth1", f"aglab-{self.RID}-r1", "eth1"]

    def test_a_good_link_is_made_and_each_end_is_renamed_inside_its_node(self):
        p, calls = self.run_link(*self.good_args())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("ip link add agv" + self.RID + "a0 type veth peer name agv" + self.RID + "b0", calls)
        self.assertIn("nsenter -t 4001 -n ip link set agv" + self.RID + "a0 name eth1", calls)
        self.assertIn("nsenter -t 4002 -n ip link set agv" + self.RID + "b0 name eth1", calls)

    def test_the_name_of_a_node_from_another_lab_is_refused(self):
        args = self.good_args()
        args[2] = "aglab-deadbeef-h1"
        p, calls = self.run_link(*args)
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("veth", calls)

    def test_an_interface_that_is_not_a_lab_nic_is_refused(self):
        for bad in ["eth0", "enp0s8", "lo", "eth1;id"]:
            args = self.good_args()
            args[3] = bad
            p, _ = self.run_link(*args)
            self.assertEqual(p.returncode, 2, bad)

    def test_a_stopped_node_is_refused(self):
        p, calls = self.run_link(*self.good_args(), running="false")
        self.assertEqual(p.returncode, 2)
        self.assertIn("not running", p.stderr)
        self.assertNotIn("veth", calls)

    def test_an_interface_the_node_already_has_is_refused_before_anything_is_made(self):
        p, calls = self.run_link(*self.good_args(), existing=["eth1"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("already has eth1", p.stderr)
        self.assertNotIn("veth", calls)

    def test_both_ends_must_be_different_nodes(self):
        args = self.good_args()
        args[4] = args[2]
        p, _ = self.run_link(*args)
        self.assertEqual(p.returncode, 2)


@unittest.skipIf(sys.platform.startswith("win"), "the helper is a shell script")
class ForwardHelperScriptTests(unittest.TestCase):
    """lab_node/forward.sh run for real against stub docker and nsenter binaries: its own checks are what's tested."""

    NAME = "aglab-0a1b2c3d-r1"

    def run_helper(self, name, val, label="lab1", running="true", pid="4242"):
        import os, shutil, subprocess, tempfile
        d = Path(tempfile.mkdtemp())
        try:
            (d / "docker").write_text(f'#!/bin/sh\necho "{label} {running} {pid}"\n')
            (d / "nsenter").write_text('#!/bin/sh\necho "nsenter $*"\n')
            for f in d.iterdir():
                f.chmod(0o755)
            env = dict(os.environ, PATH=f"{d}{os.pathsep}{os.environ['PATH']}")
            return subprocess.run(["sh", str(cm.IMAGE_DIR / "forward.sh"), name, val], capture_output=True,
                                  text=True, env=env)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_running_lab_node_gets_the_sysctl_in_its_own_namespace(self):
        p = self.run_helper(self.NAME, "1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("nsenter -t 4242 -n sysctl -w net.ipv4.ip_forward=1", p.stdout)

    def test_a_container_without_the_lab_label_is_refused(self):
        p = self.run_helper(self.NAME, "1", label="<no value>")
        self.assertEqual(p.returncode, 2)
        self.assertIn("not a running lab container", p.stderr)

    def test_a_stopped_container_is_refused(self):
        self.assertEqual(self.run_helper(self.NAME, "1", running="false").returncode, 2)

    def test_bad_name_or_value_is_refused_before_docker_is_asked(self):
        self.assertEqual(self.run_helper("aglab-0a1b2c3d-r1; id", "1").returncode, 2)
        self.assertEqual(self.run_helper(self.NAME, "yes").returncode, 2)


class RelayAndTeardownTests(unittest.TestCase):
    def test_relay_targets_are_the_node_containers_on_port_22(self):
        self.assertEqual(cm.relay_targets("abc12345", ["h1", "r1"]),
                         {"h1": "aglab-abc12345-h1:22", "r1": "aglab-abc12345-r1:22"})

    def test_the_relay_command_uses_the_given_targets_and_defaults_to_the_host(self):
        cmd = vr.relay_command(TOPO, PORTS, cm.relay_targets("abc12345", ["h1", "r1", "h2"]))
        self.assertIn("TCP:aglab-abc12345-h1:22 &", cmd)
        self.assertNotIn("host.docker.internal", cmd)
        self.assertIn("TCP:host.docker.internal:2201 &", vr.relay_command(TOPO, PORTS))

    def test_connect_relay_joins_the_lab_network(self):
        with mock.patch.object(cm, "docker", return_value=(0, "", "")) as d:
            rc, _ = cm.connect_relay("abc12345", "relayid")
        self.assertEqual(rc, 0)
        d.assert_called_once()
        self.assertEqual(d.call_args[0], ("network", "connect", "aglab-abc12345-mgmt", "relayid"))

    def test_teardown_disconnects_a_leftover_relay_before_removing_the_network(self):
        calls = []

        def fake_docker(*args, input=None, timeout=120):
            calls.append(args)
            if args[:2] == ("ps", "-aq"):
                return 0, "", ""
            if args[:2] == ("network", "ls"):
                return 0, "net1\n", ""
            if args[:2] == ("network", "inspect"):
                return 0, "relayid \n", ""
            return 0, "", ""

        with mock.patch.object(cm, "docker", side_effect=fake_docker):
            cm.teardown("abc12345")
        i_disc = calls.index(("network", "disconnect", "-f", "net1", "relayid"))
        i_rm = calls.index(("network", "rm", "net1"))
        self.assertLess(i_disc, i_rm)


if __name__ == "__main__":
    unittest.main()
