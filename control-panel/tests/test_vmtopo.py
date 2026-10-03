"""Tests for the network-topology-lab state machine in app.py. Vagrant/VirtualBox/SSH and docker compose are all
mocked out, so these run fast with no real VM. Mirrors tests/test_vmbench.py's conventions throughout.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import json, shutil, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import vm_runner as vr  # noqa: E402


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class VmTopoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(app, "DATA", self.tmp / "instances"),
            mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs"),
            # Defense in depth, not strictly needed by topo_run_runner itself (it only ever touches TOPOR_DIR via
            # topo_log()/save_topo_run()): mocked anyway so a future reintroduction of the vm_log()-vs-topo_log()
            # mixup (see topo_log()'s docstring in app.py) fails loudly in a temp dir instead of silently writing
            # real topology-shaped records into the real data/vm-runs/ - which is exactly what happened here before
            # topo_log() existed, and which then made count_occupying_slots() see dozens of phantom "ready" VMs on
            # the next real server restart.
            mock.patch.object(app, "VMR_DIR", self.tmp / "vm-runs"),
            mock.patch.object(app, "VMB_SETTINGS_FILE", self.tmp / "vmbench-settings.json"),
            mock.patch.object(app, "VM_RUNS", {}),
            mock.patch.object(app, "TOPO_RUNS", {}),
            mock.patch.object(app, "TOPO_STOP", {}),
            mock.patch.object(app, "TOPO_TERM_CREDS", {}),
            mock.patch.object(app, "agent_running", lambda n: n not in self.down),
            mock.patch.object(vr, "allocate_port", lambda rng, taken: next(p for p in range(*rng) if p not in taken)),
            mock.patch.object(vr, "gen_keypair", self.fake_gen_keypair),
            mock.patch.object(vr, "render_topology_vagrantfile", lambda *a, **k: None),
            mock.patch.object(vr, "vagrant", self.fake_vagrant),
            mock.patch.object(vr, "vagrant_stream", self.fake_vagrant_stream),
            mock.patch.object(vr, "ssh_wait", lambda *a, **k: True),
            mock.patch.object(vr, "ssh_run", self.fake_ssh_run),
            mock.patch.object(app, "run", self.fake_run),
            mock.patch.object(app, "dc", self.fake_dc),
            mock.patch.object(app, "run_turn", self.fake_run_turn),
        ]
        self._threads_before = set(threading.enumerate())
        for p in self.patches:
            p.start()
        app.TOPOR_DIR.mkdir(parents=True)
        (self.tmp / "instances").mkdir(parents=True)
        self.down = set()
        self.vagrant_fails = False
        self.ssh_check_passes = True
        self.calls = []
        self.run_envs = []

    def tearDown(self):
        # Same background-thread hazard as test_vmbench.py's VmBenchBase.tearDown: join every thread this test
        # spawned, for real, before any patch is undone, or an orphaned thread silently starts reading the NEXT
        # test's mocked state.
        for rid, r in list(app.TOPO_RUNS.items()):
            if r["state"] in app.TOPO_LIVE_STATES:
                app.TOPO_STOP[rid] = True
        leaked = []
        for t in set(threading.enumerate()) - self._threads_before:
            t.join(timeout=8)
            if t.is_alive():
                leaked.append(t.name)
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        if leaked:
            raise AssertionError(f"a background thread was still alive after tearDown's 8s join: {leaked} "
                                 "(it will now read/write the NEXT test's mocked state - fix the test, not this guard)")

    def fake_gen_keypair(self, d):
        priv = d / "id_ed25519"
        d.mkdir(parents=True, exist_ok=True)
        priv.write_text("fake-key")
        pub = d / "id_ed25519.pub"
        pub.write_text("ssh-ed25519 AAAA fake")
        return priv, pub

    def fake_vagrant(self, run_dir, *args, timeout=120):
        self.calls.append(("vagrant", args))
        if args[0] == "up" and self.vagrant_fails:
            return 1, "", "boom"
        return 0, "ok", ""

    def fake_vagrant_stream(self, run_dir, *args, on_line=None, timeout=120):
        self.calls.append(("vagrant", args))
        if on_line:
            on_line("==> fake: provisioning")
        if args[0] == "up" and self.vagrant_fails:
            return 1, "", "boom"
        return 0, "ok", ""

    def fake_ssh_run(self, port, priv, command, timeout=120):
        self.calls.append(("ssh_run", port, command[:40]))
        return (0, "pass output", "") if self.ssh_check_passes else (1, "fail output", "")

    def fake_run(self, args, input=None, timeout=120, env=None, redact=None):
        self.calls.append(("run", args[:3] if isinstance(args, list) else args))
        self.run_envs.append(env)
        return 0, "", ""

    def fake_dc(self, name, *args, input=None, timeout=120):
        self.calls.append(("dc", name, args[:2]))
        return 0, "", ""

    def fake_run_turn(self, name, chat_id, message, meta=None, log=lambda s: None, title=None, timeout=700):
        self.calls.append(("run_turn", name, chat_id))
        return {"reply": "did the task", "ok": True}

    def add_agent(self, name):
        (app.DATA / name).mkdir(parents=True, exist_ok=True)
        (app.DATA / name / "meta.json").write_text(json.dumps({"name": name, "port": 18801, "token": "t", "backend": "local"}))

    def finished(self, rid, timeout=8):
        settled = app.TOPO_OCCUPYING_STATES if app.TOPO_RUNS[rid].get("agent") else app.TOPO_LIVE_STATES
        return wait_for(lambda: app.TOPO_RUNS[rid]["state"] not in settled, timeout)


class ScratchTopoTests(VmTopoBase):
    """A run with no task and no agent: just a lab the user opens terminals into."""

    def test_full_lifecycle_no_task_no_agent(self):
        rid = app.create_topo_run({"topology_id": "s1h2"})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        r = app.TOPO_RUNS[rid]
        self.assertIsNone(r["task_id"])
        self.assertIsNone(r["agent"])
        self.assertIsNone(r["score"])
        self.assertEqual(set(r["nodes"]), {"h1", "sw1", "h2"})
        ports = {n["ssh_port"] for n in r["nodes"].values()}
        self.assertEqual(len(ports), 3, "every node must get a distinct ssh_port")
        self.assertTrue(app.topo_run_dir(rid).exists(), "the lab's run directory must survive while state is 'ready'")
        app.stop_topo_run(rid)
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "stopped")

    def test_vagrant_up_runs_sequentially_not_in_parallel(self):
        """Regression: VirtualBox's provider parallelizes multi-machine `up` by default, which on real hardware
        testing made N VMs boot/apt-get simultaneously contend for host CPU/disk hard enough that one routinely
        missed the SSH-readiness window - reproduced in isolation (same node, alone, always came up fine)."""
        rid = app.create_topo_run({"topology_id": "s1h2"})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        up_calls = [c for c in self.calls if c[0] == "vagrant" and c[1][0] == "up"]
        self.assertEqual(len(up_calls), 1)
        self.assertIn("--no-parallel", up_calls[0][1])

    def test_loadbalancer_appliance_reaches_ready(self):
        rid = app.create_topo_run({"topology_id": "lb1s1h3"})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        r = app.TOPO_RUNS[rid]
        self.assertEqual(set(r["nodes"]), {"client", "web1", "web2", "lb1", "sw1"})
        self.assertEqual(r["nodes"]["lb1"]["role"], "loadbalancer")

    def test_firewall_appliance_reaches_ready(self):
        rid = app.create_topo_run({"topology_id": "fw1s2h2"})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        r = app.TOPO_RUNS[rid]
        self.assertEqual(set(r["nodes"]), {"h1", "sw1", "fw1", "sw2", "h2"})
        self.assertEqual(r["nodes"]["fw1"]["role"], "firewall")

    def test_unknown_topology_rejected(self):
        with self.assertRaises(KeyError):
            app.create_topo_run({"topology_id": "no-such-topology"})

    def test_missing_topology_id_rejected(self):
        with self.assertRaises(ValueError):
            app.create_topo_run({})

    def test_cannot_delete_while_live(self):
        with mock.patch.object(vr, "ssh_wait", lambda *a, **k: (time.sleep(1), True)[1]):
            rid = app.create_topo_run({"topology_id": "s1h2"})
            with self.assertRaises(ValueError):
                app.delete_topo_run(rid)
            self.assertTrue(self.finished(rid, timeout=5))
        app.delete_topo_run(rid)
        self.assertNotIn(rid, app.TOPO_RUNS)

    def test_vagrant_failure_marks_error_not_crash(self):
        self.vagrant_fails = True
        rid = app.create_topo_run({"topology_id": "s1h2"})
        self.assertTrue(self.finished(rid))
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "error")

    def test_never_writes_under_vmr_dir(self):
        """Regression: topo_run_runner used to log progress via the single-VM vm_log(), which hardcodes
        save_vm_run() - silently persisting topology-shaped records into data/vm-runs/ instead of
        data/topo-runs/. In-memory TOPO_RUNS still read back correctly (masking the bug), but a real server
        restart would load_vm_runs() those files and either crash in vm_run_view() or, worse, have
        count_occupying_slots() count them as real occupied VM-bench slots forever. topo_run_runner must use
        topo_log() exclusively."""
        rid = app.create_topo_run({"topology_id": "r2s2h2"})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        self.assertFalse(list(app.VMR_DIR.glob("*")), "topo_run_runner wrote a file under VMR_DIR")


class SlotAccountingTests(VmTopoBase):
    def test_topology_charges_one_slot_per_node(self):
        app.update_vmb_settings({"max_concurrent": 2})
        hold = {"go": False}

        def slow_ssh_wait(*a, **k):
            while not hold["go"]:
                time.sleep(0.02)
            return True
        with mock.patch.object(vr, "ssh_wait", slow_ssh_wait):
            rid1 = app.create_topo_run({"topology_id": "r2s2h2"})     # 6 nodes - bigger than max_concurrent=2,
            self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid1]["state"] == "provisioning"))
            # ... but still allowed to run alone (see topo_run_runner's effective-cap comment).
            rid2 = app.create_topo_run({"topology_id": "s1h2"})       # 3 more nodes: now genuinely over budget
            time.sleep(0.3)
            self.assertEqual(app.TOPO_RUNS[rid2]["state"], "queued")
            hold["go"] = True
            self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid1]["state"] == "ready"))
        time.sleep(0.3)
        # rid1 is idle but its 6 VMs are still up, so it still occupies those slots - rid2 must NOT proceed
        self.assertEqual(app.TOPO_RUNS[rid2]["state"], "queued")
        app.stop_topo_run(rid1)                                        # explicitly releasing rid1 frees the slots
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid2]["state"] == "ready", timeout=6))

    def test_resting_ready_topology_still_occupies_its_full_node_count(self):
        app.update_vmb_settings({"max_concurrent": 3})
        rid1 = app.create_topo_run({"topology_id": "s1h2"})           # 3 nodes, fills the budget on its own
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid1]["state"] == "ready"))
        rid2 = app.create_topo_run({"topology_id": "s1h2"})
        time.sleep(0.3)
        self.assertEqual(app.TOPO_RUNS[rid2]["state"], "queued")
        app.stop_topo_run(rid1)
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid2]["state"] == "ready", timeout=6))

    def test_vm_bench_and_topology_slots_share_the_same_budget(self):
        app.update_vmb_settings({"max_concurrent": 2})
        hold = {"go": False}

        def slow_ssh_wait(*a, **k):
            while not hold["go"]:
                time.sleep(0.02)
            return True
        with mock.patch.object(vr, "ssh_wait", slow_ssh_wait), \
             mock.patch.object(vr, "render_vagrantfile", lambda *a, **k: None), \
             mock.patch.object(vr, "write_seed_files", lambda d, seed: None), \
             mock.patch.object(app, "VM_STOP", {}), mock.patch.object(app, "VM_TERM_CREDS", {}):
            app.VMR_DIR.mkdir(parents=True, exist_ok=True)            # VMR_DIR itself is already mocked in setUp
            vm_rid = app.create_vm_run({})                            # 1 slot
            self.assertTrue(wait_for(lambda: app.VM_RUNS[vm_rid]["state"] == "provisioning"))
            topo_rid = app.create_topo_run({"topology_id": "s1h2"})   # would need 3 more, only 1 left
            time.sleep(0.3)
            self.assertEqual(app.TOPO_RUNS[topo_rid]["state"], "queued")
            hold["go"] = True
            self.assertTrue(wait_for(lambda: app.VM_RUNS[vm_rid]["state"] == "ready"))
        time.sleep(0.3)
        self.assertEqual(app.TOPO_RUNS[topo_rid]["state"], "queued")
        app.stop_vm_run(vm_rid)                                       # synchronous teardown: state was "ready"
        self.assertEqual(app.VM_RUNS[vm_rid]["state"], "stopped")
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[topo_rid]["state"] == "ready", timeout=6))


class TaskWithAgentTests(VmTopoBase):
    def test_agent_must_exist_and_be_running(self):
        with self.assertRaises(KeyError):
            app.create_topo_run({"topology_id": "s1h2", "task_id": "s1h2-connectivity", "agent": "nope"})
        self.add_agent("alpha")
        self.down = {"alpha"}
        with self.assertRaises(ValueError):
            app.create_topo_run({"topology_id": "s1h2", "task_id": "s1h2-connectivity", "agent": "alpha"})

    def test_task_for_a_different_topology_rejected(self):
        self.add_agent("alpha")
        with self.assertRaises(ValueError):
            app.create_topo_run({"topology_id": "s1h2", "task_id": "r2s2h2-basic-routing", "agent": "alpha"})

    def test_full_run_attaches_one_relay_not_n_calls_agent_and_scores(self):
        self.add_agent("alpha")
        rid = app.create_topo_run({"topology_id": "s1h2", "task_id": "s1h2-connectivity", "agent": "alpha"})
        self.assertTrue(self.finished(rid))
        r = app.TOPO_RUNS[rid]
        self.assertEqual(r["state"], "done", r.get("reason"))
        self.assertEqual(r["chat"], f"vmtopo-{rid}")
        self.assertIsNotNone(r["score"])
        self.assertTrue(r["score"]["passed"])
        kinds = [c[0] for c in self.calls]
        self.assertIn("run_turn", kinds)
        self.assertIn("ssh_run", kinds)
        wrapper_writes = [c for c in self.calls if c[0] == "dc" and c[2][0] == "exec"]
        # one vmrun-<node> wrapper write per node (3 nodes in s1h2) plus the key-copy exec - at least 3 dc execs
        self.assertGreaterEqual(len(wrapper_writes), 3)
        relay_up_down = [c for c in self.calls if c[0] == "run"]
        self.assertTrue(relay_up_down, "exactly one relay container is brought up/down via app.run(), not per-node")

    def test_relay_env_command_is_valid_shell_syntax(self):
        """Regression (found via a real agent-attach test, see vr.relay_command's docstring): the mocked `run`
        here never executes VM_RELAY_TOPO_CMD for real, so a shell-syntax bug in it was invisible to every other
        test in this file - this specifically inspects the env passed to the real `docker compose up` call."""
        self.add_agent("alpha")
        rid = app.create_topo_run({"topology_id": "r2s2h2", "task_id": "r2s2h2-basic-routing", "agent": "alpha"})
        self.assertTrue(self.finished(rid))
        cmds = [e["VM_RELAY_TOPO_CMD"] for e in self.run_envs if e and "VM_RELAY_TOPO_CMD" in e]
        self.assertTrue(cmds, "the relay-up call must set VM_RELAY_TOPO_CMD")
        for cmd in cmds:
            result = subprocess.run(["sh", "-n", "-c", cmd], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_scoring_targets_the_tasks_check_node_specifically(self):
        self.add_agent("alpha")
        rid = app.create_topo_run({"topology_id": "r1s2h2", "task_id": "r1s2h2-routing", "agent": "alpha"})
        self.assertTrue(self.finished(rid))
        r = app.TOPO_RUNS[rid]
        check_port = r["nodes"]["h1"]["ssh_port"]       # r1s2h2-routing's check_node is h1
        ssh_run_calls = [c for c in self.calls if c[0] == "ssh_run"]
        self.assertTrue(any(c[1] == check_port for c in ssh_run_calls),
                        "score_run must SSH into the task's check_node, not an arbitrary node")

    def test_failing_check_script_gives_a_failed_score_not_an_error(self):
        self.add_agent("alpha")
        self.ssh_check_passes = False
        rid = app.create_topo_run({"topology_id": "s1h2", "task_id": "s1h2-connectivity", "agent": "alpha"})
        self.assertTrue(self.finished(rid))
        r = app.TOPO_RUNS[rid]
        self.assertEqual(r["state"], "done", r.get("reason"))
        self.assertFalse(r["score"]["passed"])


class CustomTopologyRunTests(VmTopoBase):
    """The 'structured builder': role counts + a wiring pattern instead of a catalog topology_id."""

    def test_scratch_custom_topology_reaches_ready_with_the_expected_shape(self):
        rid = app.create_topo_run({"custom": {"counts": {"host": 2, "switch": 1}, "wiring": "star"}})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        r = app.TOPO_RUNS[rid]
        self.assertIsNone(r["topology_id"])
        self.assertEqual(set(r["nodes"]), {"h1", "h2", "sw1"})
        self.assertIn("Custom", r["topology_title"])

    def test_missing_both_topology_id_and_custom_rejected(self):
        with self.assertRaises(ValueError):
            app.create_topo_run({})

    def test_over_cap_custom_topology_rejected_before_any_vm_boots(self):
        with self.assertRaises(ValueError):
            app.create_topo_run({"custom": {"counts": {"host": 20}, "wiring": "star"}})
        self.assertEqual(self.calls, [], "nothing should have been provisioned")

    def test_agent_on_custom_topology_requires_a_custom_prompt(self):
        self.add_agent("alpha")
        with self.assertRaises(ValueError):
            app.create_topo_run({"custom": {"counts": {"host": 2, "switch": 1}, "wiring": "star"}, "agent": "alpha"})

    def test_agent_with_custom_prompt_on_custom_topology_runs_and_never_scores(self):
        self.add_agent("alpha")
        rid = app.create_topo_run({"custom": {"counts": {"host": 2, "switch": 1}, "wiring": "star"},
                                   "custom_prompt": "wire these two hosts together", "agent": "alpha"})
        self.assertTrue(self.finished(rid))
        r = app.TOPO_RUNS[rid]
        self.assertEqual(r["state"], "done", r.get("reason"))
        self.assertIsNone(r["score"], "a custom-prompt run must never be auto-scored")
        self.assertIsNone(r["task_id"])
        with self.assertRaises(ValueError):
            app.topo_score_now(rid)                    # Score now must refuse - no task attached
        run_turn_calls = [c for c in self.calls if c[0] == "run_turn"]
        self.assertTrue(run_turn_calls)

    def test_agent_with_custom_prompt_on_catalog_topology_also_never_scores(self):
        self.add_agent("alpha")
        rid = app.create_topo_run({"topology_id": "s1h2", "custom_prompt": "just get h1 to ping h2",
                                   "agent": "alpha"})
        self.assertTrue(self.finished(rid))
        r = app.TOPO_RUNS[rid]
        self.assertEqual(r["state"], "done", r.get("reason"))
        self.assertIsNone(r["score"])
        self.assertIsNone(r["task_id"])


class PerNodeTerminalTests(VmTopoBase):
    def test_cannot_open_terminal_before_lab_is_ready(self):
        app.TOPO_RUNS["x"] = {"id": "x", "state": "provisioning", "nodes": {"h1": {"terminal": {"active": False}}}}
        with self.assertRaises(ValueError):
            app.start_topo_terminal("x", "h1")

    def test_unknown_node_raises(self):
        rid = app.create_topo_run({"topology_id": "s1h2"})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        with self.assertRaises(KeyError):
            app.start_topo_terminal(rid, "no-such-node")

    def test_one_nodes_terminal_is_independent_of_another(self):
        rid = app.create_topo_run({"topology_id": "s1h2"})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        (app.topo_run_dir(rid) / "id_ed25519").write_text("k")
        port1, cred1 = app.start_topo_terminal(rid, "h1")
        port2, cred2 = app.start_topo_terminal(rid, "h2")
        self.assertNotEqual(port1, port2)
        self.assertNotEqual(cred1, cred2)
        app.stop_topo_terminal(rid, "h1")
        self.assertFalse(app.TOPO_RUNS[rid]["nodes"]["h1"]["terminal"]["active"])
        self.assertTrue(app.TOPO_RUNS[rid]["nodes"]["h2"]["terminal"]["active"], "stopping h1's terminal must not touch h2's")

    def test_running_terminal_whose_credential_was_lost_gets_a_fresh_one(self):
        rid = app.create_topo_run({"topology_id": "s1h2"})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        (app.topo_run_dir(rid) / "id_ed25519").write_text("k")
        app.start_topo_terminal(rid, "h1")
        app.TOPO_TERM_CREDS.clear()                             # what a panel restart leaves behind
        with mock.patch.object(app, "stop_topo_terminal", wraps=app.stop_topo_terminal) as stop:
            _, cred = app.start_topo_terminal(rid, "h1")
        stop.assert_called_once_with(rid, "h1", quiet=True)
        self.assertEqual(len(cred), 32)
        self.assertEqual(app.TOPO_TERM_CREDS[f"{rid}:h1"], cred)

    def test_credential_is_never_persisted_to_disk(self):
        rid = app.create_topo_run({"topology_id": "s1h2"})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        (app.topo_run_dir(rid) / "id_ed25519").write_text("k")
        _, cred = app.start_topo_terminal(rid, "h1")
        on_disk = app.topo_run_path(rid).read_text()
        self.assertNotIn(cred, on_disk)


class StopDeleteTests(VmTopoBase):
    def test_stop_tears_down_the_whole_group_in_one_vagrant_call(self):
        rid = app.create_topo_run({"topology_id": "r2s2h2"})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        self.calls.clear()
        app.stop_topo_run(rid)
        destroy_calls = [c for c in self.calls if c[0] == "vagrant" and c[1][0] == "destroy"]
        self.assertEqual(len(destroy_calls), 1, "one vagrant destroy -f call must tear down every node in the group")


class StopRaceTests(VmTopoBase):
    def test_stop_right_after_ready_destroys_the_lab_once(self):
        """Regression: same race as test_vmbench.StopRaceTests, for labs - it made
        StopDeleteTests.test_stop_tears_down_the_whole_group_in_one_vagrant_call flaky on CI (2 != 1 destroy calls)."""
        real_log = app.topo_log

        def log_then_stop(r, line):
            real_log(r, line)
            if line.startswith("ready"):
                app.stop_topo_run(r["id"])
        with mock.patch.object(app, "topo_log", log_then_stop):
            rid = app.create_topo_run({"topology_id": "r2s2h2"})
            for t in set(threading.enumerate()) - self._threads_before:
                t.join(timeout=8)
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "stopped")
        destroy_calls = [c for c in self.calls if c[0] == "vagrant" and c[1][0] == "destroy"]
        self.assertEqual(len(destroy_calls), 1, "Stop and the runner thread must not both destroy the lab")


class AgentDeletionTests(VmTopoBase):
    def test_deleting_an_agent_stops_its_live_topo_runs(self):
        self.add_agent("alpha")
        with mock.patch.object(vr, "ssh_wait", lambda *a, **k: (time.sleep(2), True)[1]):
            rid = app.create_topo_run({"topology_id": "s1h2", "task_id": "s1h2-connectivity", "agent": "alpha"})
            self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] in ("provisioning", "ready", "working")))
            app.stop_topo_runs_for("alpha")
            self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "stopped", timeout=8))


class RestartRecoveryTests(VmTopoBase):
    def test_live_run_on_disk_becomes_interrupted_on_load(self):
        rid = "abcdef12"
        rec = {"id": rid, "state": "working", "reason": "", "keep": False, "memory_mb": 1024, "cpus": 1,
               "created": time.time(), "started": time.time(), "ended": None, "agent": "alpha",
               "topology_id": "s1h2", "topology_title": "1 switch, 2 hosts", "task_id": "s1h2-connectivity",
               "task_title": "t", "chat": "c", "score": None, "benchmark_id": None,
               "nodes": {"h1": {"role": "host", "ssh_port": 62400, "terminal": {"active": False, "port": None}}},
               "vm_log": ""}
        app.topo_run_path(rid).write_text(json.dumps(rec))
        app.load_topo_runs()
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "interrupted")


if __name__ == "__main__":
    unittest.main()
