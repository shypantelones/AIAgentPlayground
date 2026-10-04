"""Tests for a lab's change log and rollback: the session-log parser (lab_changes.py), the vmrun wrapper's log lines,
the per-turn record and rollback points (app.topo_agent_turn / snapshot_lab_vms / topo_rollback). VirtualBox, Vagrant,
SSH and the agent are mocked; the wrapper test runs the real wrapper script with a fake ssh.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import os, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import lab_changes as lc  # noqa: E402
import vm_runner as vr  # noqa: E402


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


SESSION_LOG = (
    "=== 2026-10-04T10:00:00Z h1 $ ip addr\n"
    "2: enp0s8: <BROADCAST>\n"
    "=== 2026-10-04T10:00:05Z r1 $ (script on stdin)\n"
    "sysctl -w net.ipv4.ip_forward=1\n"
    "=== 2026-10-04T10:00:09Z $ old style command\n"
    "output\n")


class ParseTests(unittest.TestCase):
    def test_node_command_and_output_per_entry(self):
        e = lc.parse_command_log(SESSION_LOG)
        self.assertEqual([(x["node"], x["command"]) for x in e],
                         [("h1", "ip addr"), ("r1", "(script on stdin)"), (None, "old style command")])
        self.assertIn("2: enp0s8", e[0]["output"])
        self.assertIn("ip_forward", e[1]["output"])

    def test_output_is_cut_to_the_excerpt_length(self):
        big = "=== 2026-10-04T10:00:00Z h1 $ cat big\n" + "x" * 5000
        (e,) = lc.parse_command_log(big)
        self.assertEqual(len(e["output"]), lc.OUTPUT_EXCERPT)

    def test_only_commands_after_the_earlier_read_are_new(self):
        before = "=== 2026-10-04T10:00:00Z h1 $ ip addr\nold\n"
        after = before + "=== 2026-10-04T10:01:00Z r1 $ ip route\nnew\n"
        self.assertEqual([x["command"] for x in lc.new_entries(before, after)], ["ip route"])

    def test_empty_or_missing_log_has_no_entries(self):
        self.assertEqual(lc.parse_command_log(""), [])
        self.assertEqual(lc.parse_command_log(None), [])


class WrapperTests(unittest.TestCase):
    def test_node_name_is_written_into_each_entry(self):
        script = vr.vmrun_script("/k", 2201, "bench@relay", "/log", node="h1")
        self.assertIn('echo "=== $ts h1 \\$ $*"', script)
        self.assertIn("printf '=== %s h1 $ (script on stdin)", script)

    def test_without_a_node_the_entries_are_as_before(self):
        script = vr.vmrun_script("/k", 2201, "bench@relay", "/log")
        self.assertIn('echo "=== $ts \\$ $*"', script)

    @unittest.skipIf(sys.platform.startswith("win"), "runs the wrapper with sh and a fake ssh")
    def test_the_real_wrapper_logs_lines_the_parser_reads_back(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            bindir = tmp / "bin"
            bindir.mkdir()
            fake_ssh = bindir / "ssh"
            fake_ssh.write_text("#!/bin/sh\necho \"answer from the VM: $*\"\n")
            fake_ssh.chmod(0o755)
            log = tmp / "session.log"
            wrapper = tmp / "vmrun-h1"
            wrapper.write_text(vr.vmrun_script("/k", 2201, "bench@relay", str(log), node="h1"))
            env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
            subprocess.run(["sh", str(wrapper), "ip -br addr"], env=env, check=True, capture_output=True)
            (entry,) = lc.parse_command_log(log.read_text())
            self.assertEqual((entry["node"], entry["command"]), ("h1", "ip -br addr"))
            self.assertIn("answer from the VM", entry["output"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RunBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs")
        self.patch.start()
        self.rid = "chg12345"
        self.run = {"id": self.rid, "state": "ready", "agent": "alpha", "agent_done": True, "chat": "vmtopo-chg12345", "agent_turns": 0,
                    "nodes": {"h1": {"ssh_port": 2201}, "r1": {"ssh_port": 2202}}, "changes": [], "vm_log": "",
                    "created": time.time()}
        app.TOPO_RUNS[self.rid] = self.run
        d = app.topo_run_dir(self.rid)
        d.mkdir(parents=True)
        (d / "Vagrantfile").write_text("")
        (d / "id_ed25519").write_text("key")

    def tearDown(self):
        app.TOPO_RUNS.pop(self.rid, None)
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def turn(self, n, point=True, rolled_back=False):
        self.run["changes"].append({"turn": n, "point": f"before-turn-{n}" if point else None,
                                    "rolled_back": rolled_back, "commands": [], "command_count": 0})


class TurnTests(RunBase):
    def test_a_turn_records_its_rollback_point_and_only_its_own_commands(self):
        before = "=== 2026-10-04T10:00:00Z h1 $ ip addr\nold\n"
        after = before + "=== 2026-10-04T10:02:00Z r1 $ sysctl -w net.ipv4.ip_forward=1\nok\n"
        with mock.patch.object(app, "snapshot_lab_vms", return_value=True) as snap, \
                mock.patch.object(app, "read_agent_log", side_effect=[before, after]), \
                mock.patch.object(app, "counted_agent_turn", side_effect=lambda r, m, log, turn: {"ok": True}), \
                mock.patch.object(app, "take_topo_snapshot", return_value={"id": "s1"}):
            app.topo_agent_turn(self.run, "configure it")
        snap.assert_called_once_with(self.run, "before-turn-1")
        (ch,) = self.run["changes"]
        self.assertEqual((ch["turn"], ch["point"], ch["rolled_back"]), (1, "before-turn-1", False))
        self.assertEqual([(c["node"], c["command"]) for c in ch["commands"]], [("r1", "sysctl -w net.ipv4.ip_forward=1")])

    def test_no_rollback_point_is_recorded_as_such_and_the_turn_still_runs(self):
        with mock.patch.object(app, "snapshot_lab_vms", return_value=False), \
                mock.patch.object(app, "read_agent_log", return_value=""), \
                mock.patch.object(app, "counted_agent_turn", side_effect=lambda r, m, log, turn: {"ok": True}), \
                mock.patch.object(app, "take_topo_snapshot", return_value={"id": "s1"}):
            app.topo_agent_turn(self.run, "go")
        self.assertIsNone(self.run["changes"][0]["point"])

    def test_unreadable_log_lists_no_commands_rather_than_all_of_them(self):
        with mock.patch.object(app, "snapshot_lab_vms", return_value=True), \
                mock.patch.object(app, "read_agent_log", side_effect=[None, "=== t h1 $ x\n"]), \
                mock.patch.object(app, "counted_agent_turn", side_effect=lambda r, m, log, turn: {"ok": True}), \
                mock.patch.object(app, "take_topo_snapshot", return_value={"id": "s1"}):
            app.topo_agent_turn(self.run, "go")
        self.assertEqual(self.run["changes"][0]["commands"], [])


class SnapshotTests(RunBase):
    def test_every_node_must_snapshot_for_the_point_to_count(self):
        results = [(0, "", ""), (1, "", "disk full")]
        with mock.patch.object(app.vr, "vagrant", side_effect=results) as vg:
            self.assertFalse(app.snapshot_lab_vms(self.run, "before-turn-1"))
        self.assertEqual(vg.call_args_list[0].args[1:], ("snapshot", "save", "h1", "before-turn-1"))

    def test_all_nodes_snapshot_is_a_usable_point(self):
        with mock.patch.object(app.vr, "vagrant", return_value=(0, "", "")) as vg:
            self.assertTrue(app.snapshot_lab_vms(self.run, "before-turn-1"))
        self.assertEqual(vg.call_count, 2)


class RollbackTests(RunBase):
    def test_refusals_say_why(self):
        self.turn(1)
        self.turn(2, point=False)
        self.turn(3, rolled_back=True)
        cases = [
            (dict(state="working"), 1, "ready, with no agent"),
            (dict(restoring=True), 1, "ready, with no agent"),
            (dict(), 9, "no turn 9"),
            (dict(), 2, "no rollback point"),
            (dict(), 3, "already rolled back"),
        ]
        for patch, turn, msg in cases:
            with self.subTest(turn=turn, patch=patch):
                saved = dict(self.run)
                self.run.update(patch)
                try:
                    with self.assertRaisesRegex(ValueError, msg):
                        app.topo_rollback(self.rid, turn)
                finally:
                    self.run.clear()
                    self.run.update(saved)

    def test_rollback_restores_every_node_and_marks_later_turns(self):
        self.turn(1)
        self.turn(2)
        self.turn(3)
        with mock.patch.object(app.vr, "vagrant", return_value=(0, "", "")) as vg, \
                mock.patch.object(app.vr, "ssh_wait", return_value=True), \
                mock.patch.object(app, "take_topo_snapshot", return_value={"id": "s9"}):
            app.topo_rollback(self.rid, 2)
            self.assertTrue(wait_for(lambda: not self.run.get("restoring")))
        restores = [c.args[1:] for c in vg.call_args_list if c.args[1] == "snapshot"]
        self.assertEqual(restores, [("snapshot", "restore", "--no-provision", "h1", "before-turn-2"),
                                    ("snapshot", "restore", "--no-provision", "r1", "before-turn-2")])
        self.assertEqual([c["rolled_back"] for c in self.run["changes"]], [False, True, True])

    def test_a_failed_restore_marks_nothing_and_says_why(self):
        self.turn(1)
        with mock.patch.object(app.vr, "vagrant", return_value=(1, "", "VBoxManage: error")):
            app.topo_rollback(self.rid, 1)
            self.assertTrue(wait_for(lambda: not self.run.get("restoring")))
        self.assertFalse(self.run["changes"][0]["rolled_back"])
        self.assertIn("could not restore h1", self.run["reason"])


if __name__ == "__main__":
    unittest.main()
