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
            mock.patch.object(app, "MODEL_EVIDENCE_FILE", self.tmp / "model-evidence.json"),
            mock.patch.object(app, "VM_RUNS", {}),
            mock.patch.object(app, "TOPO_RUNS", {}),
            mock.patch.object(app, "TOPO_STOP", {}),
            mock.patch.object(app, "TOPO_FOLLOWUPS", {}),
            mock.patch.object(app, "TOPO_END", {}),
            mock.patch.object(app, "VM_SESSION_POLL_S", 0.02),
            mock.patch.object(app, "TOPO_TERM_CREDS", {}),
            mock.patch.object(app, "agent_running", lambda n: n not in self.down),
            mock.patch.object(vr, "allocate_port", lambda rng, taken: next(p for p in range(*rng) if p not in taken)),
            mock.patch.object(vr, "gen_keypair", self.fake_gen_keypair),
            mock.patch.object(vr, "render_topology_vagrantfile", lambda *a, **k: None),
            mock.patch.object(vr, "vagrant", self.fake_vagrant),
            mock.patch.object(vr, "vagrant_stream", self.fake_vagrant_stream),
            mock.patch.object(vr, "destroy_after_cancel", lambda d, **k: self.calls.append(("destroy_after_cancel",)) or (0, "", "")),
            mock.patch.object(vr, "ssh_wait", lambda *a, **k: True),
            mock.patch.object(vr, "ssh_run", self.fake_ssh_run),
            mock.patch.object(vr, "ssh_script", self.fake_ssh_script),
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
        if args[0] == "suspend" and getattr(self, "suspend_fails", False):
            return 1, "", "suspend failed"
        return 0, "ok", ""

    def fake_vagrant_stream(self, run_dir, *args, on_line=None, timeout=120, cancel=None):
        self.calls.append(("vagrant", args))
        if on_line:
            on_line("==> fake: provisioning")
        if args[0] in ("up", "resume") and getattr(self, "up_blocks", False):     # a long build, until Stop cancels it
            while not (cancel and cancel()):
                time.sleep(0.02)
            return vr.CANCELLED_RC, "", "cancelled"
        if args[0] == "resume" and getattr(self, "resume_fails", False):
            return 1, "", "resume failed"
        if args[0] == "up" and self.vagrant_fails:
            return 1, "", "boom"
        return 0, "ok", ""

    def fake_ssh_script(self, port, priv, script, timeout=60):
        """A node's SNAPSHOT_SCRIPT output; per-port text can be set in self.node_config, a port in self.ssh_down fails.
        Any other script (a lab file's apply script) is recorded in self.applied and succeeds."""
        if script != vr.SNAPSHOT_SCRIPT:
            self.applied = getattr(self, "applied", []) + [(port, script)]
            return 0, "done\n", ""
        if port in getattr(self, "ssh_down", set()):
            return 255, "", "ssh: connect to host 127.0.0.1 port %d: Connection refused" % port
        cfg = getattr(self, "node_config", {}).get(port, "")
        return 0, f"### addresses\nlo UNKNOWN 127.0.0.1/8\n{cfg}\n### routes\n# ipv6\n", ""

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


class LabSessionTests(VmTopoBase):
    """Interactive sessions for topology labs: the agent keeps its ./vmrun-<node> access to every node after its first
    reply and you send it more guidance in the same conversation until you end the session (same as single VMs)."""

    def setUp(self):
        super().setUp()
        self.add_agent("alpha")
        self.turns = []
        self.detached = []
        p1 = mock.patch.object(app, "run_turn", self.record_turn)
        p2 = mock.patch.object(app, "detach_topo_agent", lambda agent: self.detached.append(agent))
        for p in (p1, p2):
            p.start()
            self.patches.append(p)

    def record_turn(self, name, chat_id, message, meta=None, log=lambda s: None, title=None, timeout=700):
        self.turns.append((chat_id, message))
        return {"reply": "ok", "ok": True}

    def state(self, rid):
        return app.TOPO_RUNS[rid]["state"]

    def open_session(self, **form):
        rid = app.create_topo_run(dict({"topology_id": "s1h2", "agent": "alpha", "interactive": True,
                                         "custom_prompt": "Give h1 and h2 addresses so they can ping each other."}, **form))
        self.assertTrue(wait_for(lambda: self.state(rid) == "attached"))
        return rid

    def test_session_relays_follow_ups_then_ends_cleanly(self):
        rid = self.open_session()
        self.assertIn("more guidance", self.turns[0][1])
        self.assertIn("./vmrun-h1", self.turns[0][1])
        self.assertIn("ready. Your own prompt", app.TOPO_RUNS[rid]["vm_log"])
        self.assertEqual(self.detached, [], "the agent keeps its lab access while the session is open")
        self.assertIsNotNone(app.topo_run_view(app.TOPO_RUNS[rid])["idle_deadline"])

        app.send_topo_followup(rid, "Use 192.168.50.0/24 instead.")
        self.assertTrue(wait_for(lambda: len(self.turns) == 2))
        self.assertTrue(wait_for(lambda: self.state(rid) == "attached"))
        self.assertEqual(self.turns[1], (f"vmtopo-{rid}", "Use 192.168.50.0/24 instead." + app.TOPO_FOLLOWUP_REMINDER))
        self.assertIn("./vmrun-<node>", app.TOPO_FOLLOWUP_REMINDER)

        app.end_topo_session(rid)
        self.assertTrue(self.finished(rid))
        for t in set(threading.enumerate()) - self._threads_before:
            t.join(timeout=8)                # "done" is set before the runner's teardown (vagrant destroy) runs
        self.assertEqual(self.state(rid), "done")
        self.assertEqual(self.detached, ["alpha"])
        destroys = [c for c in self.calls if c[0] == "vagrant" and c[1][0] == "destroy"]
        self.assertEqual(len(destroys), 1, "not kept, so the lab is destroyed once the session ends")
        with self.assertRaises(ValueError):
            app.send_topo_followup(rid, "too late")

    def test_session_on_a_catalog_task_is_scored_when_it_ends(self):
        rid = self.open_session(task_id="s1h2-connectivity", custom_prompt=None)
        self.assertFalse(any(c[0] == "ssh_run" for c in self.calls), "not scored while the session is open")
        app.end_topo_session(rid)
        self.assertTrue(self.finished(rid))
        self.assertTrue(app.TOPO_RUNS[rid]["score"]["passed"])

    def test_score_now_during_a_session_keeps_it_open(self):
        rid = self.open_session(task_id="s1h2-connectivity", custom_prompt=None)
        jid = app.topo_score_now(rid)
        self.assertTrue(wait_for(lambda: app.JOBS[jid]["done"]))
        self.assertEqual(self.state(rid), "attached")

    def test_stop_during_a_session_detaches_without_scoring(self):
        rid = self.open_session(task_id="s1h2-connectivity", custom_prompt=None)
        app.stop_topo_run(rid)
        self.assertTrue(self.finished(rid))
        self.assertEqual(self.state(rid), "stopped")
        self.assertEqual(self.detached, ["alpha"])
        self.assertIsNone(app.TOPO_RUNS[rid]["score"])

    def test_idle_session_ends_by_itself(self):
        with mock.patch.object(app, "VM_SESSION_IDLE_S", 0.2):
            rid = self.open_session()
            self.assertTrue(self.finished(rid))
        self.assertEqual(self.state(rid), "done")
        self.assertIn("ending the session", app.TOPO_RUNS[rid]["vm_log"])

    def test_terminals_work_during_a_session(self):
        rid = self.open_session()
        (app.topo_run_dir(rid) / "id_ed25519").write_text("k")
        port, cred = app.start_topo_terminal(rid, "h1")
        self.assertTrue(port and cred)

    def test_an_agent_can_only_be_attached_to_one_lab_at_a_time(self):
        rid = self.open_session()
        with self.assertRaises(ValueError):
            app.create_topo_run({"topology_id": "s1h2", "agent": "alpha", "custom_prompt": "second lab"})
        app.end_topo_session(rid)
        self.assertTrue(self.finished(rid))
        rid2 = app.create_topo_run({"topology_id": "s1h2", "agent": "alpha", "custom_prompt": "second lab"})
        self.assertTrue(self.finished(rid2))

    def test_validation(self):
        with self.assertRaises(ValueError):
            app.create_topo_run({"topology_id": "s1h2", "interactive": True})               # no agent
        with self.assertRaises(ValueError):
            app.create_topo_run({"topology_id": "s1h2", "agent": "alpha", "custom_prompt": "x" * (app.VM_PROMPT_MAX + 1)})
        rid = app.create_topo_run({"topology_id": "s1h2", "agent": "alpha", "custom_prompt": "x"})   # not interactive
        self.assertTrue(self.finished(rid))
        for call in (lambda: app.send_topo_followup(rid, "hi"), lambda: app.end_topo_session(rid)):
            with self.assertRaises(ValueError):
                call()
        with self.assertRaises(KeyError):
            app.send_topo_followup("deadbeef", "hi")

    def test_run_view_includes_the_conversation(self):
        rid = app.create_topo_run({"topology_id": "s1h2", "agent": "alpha", "custom_prompt": "x"})
        self.assertTrue(self.finished(rid))
        app.append_message("alpha", f"vmtopo-{rid}", "user", "x")
        app.append_message("alpha", f"vmtopo-{rid}", "agent", "addressed both hosts")
        conv = app.topo_run_view(app.TOPO_RUNS[rid], full=True)["conversation"]
        self.assertEqual([m["role"] for m in conv], ["user", "agent"])

    def test_restart_marks_open_sessions_interrupted_and_detaches_the_agent(self):
        rec = {"id": "abcd1234", "state": "attached", "agent": "alpha", "reason": "", "created": 1.0, "nodes": {}}
        app.topo_run_path("abcd1234").write_text(json.dumps(rec))
        before = set(threading.enumerate())
        app.load_topo_runs()
        for t in set(threading.enumerate()) - before:
            t.join(timeout=5)
        self.assertEqual(app.TOPO_RUNS["abcd1234"]["state"], "interrupted")
        self.assertEqual(self.detached, ["alpha"])


class LabModelEvidenceTests(VmTopoBase):
    def test_lab_turns_are_counted_from_the_lab_session_log(self):
        self.add_agent("alpha")
        logs = []

        def count(agent, log):
            logs.append(log)
            return 5 if len(logs) % 2 == 0 else 2       # 3 commands in the turn
        with mock.patch.object(app, "count_vm_commands", count):
            rid = app.create_topo_run({"topology_id": "s1h2", "agent": "alpha", "custom_prompt": "address the hosts"})
            self.assertTrue(self.finished(rid))
            for t in set(threading.enumerate()) - self._threads_before:
                t.join(timeout=8)
        self.assertEqual(set(logs), {"vm-session-topo.log"})
        e = app.load_model_evidence()[app.TOPO_RUNS[rid]["agent_model"]]
        self.assertEqual((e["runs"], e["turns"], e["turns_with_commands"]), (1, 1, 1))
        self.assertEqual(app.topo_run_view(app.TOPO_RUNS[rid])["agent_commands"], 3)


class SaveResumeTests(VmTopoBase):
    """Save suspends a lab's VMs (full running state to disk, no VM slots held); Resume restores them exactly. A saved
    lab is never destroyed by anything but Delete - not by Stop, a failed resume, or a panel restart."""

    def ready_lab(self, **form):
        rid = app.create_topo_run(dict({"topology_id": "s1h2"}, **form))
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        (app.topo_run_dir(rid) / "Vagrantfile").write_text("# fake")     # the real one is rendered by vagrant setup
        return rid

    def vagrant_calls(self, verb):
        return [c[1] for c in self.calls if c[0] == "vagrant" and c[1][0] == verb]

    def save(self, rid):
        app.save_topo_lab(rid)
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] in ("saved", "stopped")))
        for t in set(threading.enumerate()) - self._threads_before:
            t.join(timeout=8)

    def resume(self, rid, want=("ready", "saved")):
        app.resume_topo_lab(rid)
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] in want))
        for t in set(threading.enumerate()) - self._threads_before:
            t.join(timeout=8)

    def test_save_suspends_without_destroying_and_frees_the_slots(self):
        rid = self.ready_lab()
        self.assertEqual(app.count_occupying_slots(), 3)
        self.calls.clear()
        self.save(rid)
        r = app.TOPO_RUNS[rid]
        self.assertEqual(r["state"], "saved")
        self.assertTrue(r["keep"], "a saved lab is always kept")
        self.assertTrue(r["saved_at"])
        self.assertEqual(len(self.vagrant_calls("suspend")), 1)
        self.assertFalse(self.vagrant_calls("destroy"))
        self.assertTrue(app.topo_run_dir(rid).exists())
        self.assertEqual(app.count_occupying_slots(), 0, "a saved lab holds no VM slots")
        self.assertTrue(app.topo_run_view(r)["has_vms"])

    def test_resume_restores_the_suspended_vms_without_reprovisioning(self):
        rid = self.ready_lab()
        self.save(rid)
        self.calls.clear()
        self.resume(rid)
        r = app.TOPO_RUNS[rid]
        self.assertEqual(r["state"], "ready", r.get("reason"))
        self.assertTrue(r["resumed"])
        self.assertFalse(self.vagrant_calls("up"), "resumed from saved state, not booted fresh")
        resume = self.vagrant_calls("resume")
        self.assertEqual(len(resume), 1)
        self.assertIn("--no-provision", resume[0])
        self.assertFalse(self.vagrant_calls("destroy"))
        self.assertIn("ready again", r["vm_log"])
        port, cred = app.start_topo_terminal(rid, "h1")       # terminals work on the resumed lab
        self.assertTrue(port and cred)

    def test_stop_on_a_resumed_lab_keeps_its_vms_and_it_can_be_saved_again(self):
        rid = self.ready_lab()
        self.save(rid)
        self.resume(rid)
        self.calls.clear()
        app.stop_topo_run(rid)
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "stopped")
        self.assertFalse(self.vagrant_calls("destroy"), "Stop must not destroy a saved lab's VMs")
        self.save(rid)
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "saved")

    def test_stop_while_resuming_shuts_down_again_and_stays_saved(self):
        rid = self.ready_lab()
        self.save(rid)
        self.calls.clear()
        self.up_blocks = True
        app.resume_topo_lab(rid)
        self.assertTrue(wait_for(lambda: self.vagrant_calls("resume")))
        app.stop_topo_run(rid)
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "saved", timeout=5))
        for t in set(threading.enumerate()) - self._threads_before:
            t.join(timeout=8)
        self.assertEqual(app.TOPO_RUNS[rid]["reason"], "resume stopped by you")
        self.assertTrue(self.vagrant_calls("suspend"))
        self.assertFalse(self.vagrant_calls("destroy"))

    def test_failed_resume_stays_saved(self):
        rid = self.ready_lab()
        self.save(rid)
        self.calls.clear()
        self.resume_fails = True
        self.resume(rid, want=("saved",))
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["reason"]))
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "saved")
        self.assertIn("resume failed", app.TOPO_RUNS[rid]["reason"])
        self.assertFalse(self.vagrant_calls("destroy"))

    def test_failed_save_keeps_the_vms_and_can_be_retried(self):
        rid = self.ready_lab()
        self.suspend_fails = True
        self.save(rid)
        r = app.TOPO_RUNS[rid]
        self.assertEqual(r["state"], "stopped")
        self.assertIn("could not save", r["reason"])
        self.assertTrue(r["keep"])
        self.assertFalse(self.vagrant_calls("destroy"))
        self.suspend_fails = False
        self.save(rid)
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "saved")

    def test_what_can_and_cannot_be_saved_or_resumed(self):
        rid = self.ready_lab()
        with self.assertRaises(ValueError):
            app.resume_topo_lab(rid)                                     # not saved
        (app.topo_run_dir(rid) / "Vagrantfile").unlink()
        with self.assertRaises(ValueError):
            app.save_topo_lab(rid)                                       # VMs gone
        app.TOPO_RUNS[rid]["state"] = "provisioning"
        with self.assertRaises(ValueError):
            app.save_topo_lab(rid)
        app.TOPO_RUNS[rid]["state"] = "ready"
        with self.assertRaises(KeyError):
            app.save_topo_lab("deadbeef")

    def test_delete_destroys_a_saved_lab(self):
        rid = self.ready_lab()
        self.save(rid)
        self.calls.clear()
        app.delete_topo_run(rid)
        self.assertEqual(len(self.vagrant_calls("destroy")), 1)
        self.assertNotIn(rid, app.TOPO_RUNS)

    def test_restart_during_save_or_resume_leaves_the_lab_saved(self):
        for st in ("saving", "resuming"):
            with self.subTest(state=st):
                rec = {"id": "abcd1234", "state": st, "agent": None, "reason": "", "created": 1.0, "nodes": {}, "keep": True}
                app.topo_run_path("abcd1234").write_text(json.dumps(rec))
                app.load_topo_runs()
                self.assertEqual(app.TOPO_RUNS["abcd1234"]["state"], "saved")
                self.assertIn("Resume", app.TOPO_RUNS["abcd1234"]["reason"])

    def test_a_resumed_lab_does_not_block_its_old_agent(self):
        self.add_agent("alpha")
        rid = app.create_topo_run({"topology_id": "s1h2", "agent": "alpha", "custom_prompt": "x", "keep": True})
        self.assertTrue(self.finished(rid))
        for t in set(threading.enumerate()) - self._threads_before:
            t.join(timeout=8)
        (app.topo_run_dir(rid) / "Vagrantfile").write_text("# fake")
        self.save(rid)
        self.resume(rid)
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "ready")
        # creating doesn't raise "already attached"; the new lab then just queues for VM slots behind the resumed one
        rid2 = app.create_topo_run({"topology_id": "s1h2", "agent": "alpha", "custom_prompt": "a new lab"})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid2]["state"] == "queued"))
        app.stop_topo_run(rid2)
        self.assertTrue(self.finished(rid2))


class AttachAgentTests(VmTopoBase):
    """Attaching an agent to a lab that already exists (built by you, or saved and resumed). When the agent is done
    the lab goes back to "ready" instead of finishing, and re-attaching continues the lab's conversation."""

    def setUp(self):
        super().setUp()
        self.add_agent("alpha")
        self.add_agent("beta")
        self.turns, self.detached = [], []
        for p in (mock.patch.object(app, "run_turn", self.record_turn),
                  mock.patch.object(app, "detach_topo_agent", lambda agent: self.detached.append(agent))):
            p.start()
            self.patches.append(p)

    def record_turn(self, name, chat_id, message, meta=None, log=lambda s: None, title=None, timeout=700):
        self.turns.append((name, chat_id, message))
        return {"reply": "ok", "ok": True}

    def scratch_lab(self, **form):
        rid = app.create_topo_run(dict({"topology_id": "s1h2"}, **form))
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        (app.topo_run_dir(rid) / "Vagrantfile").write_text("# fake")
        return rid

    def attach(self, rid, **form):
        app.attach_agent_to_lab(rid, dict({"agent": "alpha", "custom_prompt": "Address h1 and h2."}, **form))

    def back_to_ready(self, rid):
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid].get("agent_done") and app.TOPO_RUNS[rid]["state"] == "ready"))
        for t in set(threading.enumerate()) - self._threads_before:
            t.join(timeout=8)

    def destroys(self):
        return [c for c in self.calls if c[0] == "vagrant" and c[1][0] == "destroy"]

    def test_attach_runs_the_agent_then_hands_the_lab_back(self):
        rid = self.scratch_lab()
        self.attach(rid)
        self.back_to_ready(rid)
        r = app.TOPO_RUNS[rid]
        self.assertEqual(len(self.turns), 1)
        name, chat, msg = self.turns[0]
        self.assertEqual((name, chat), ("alpha", f"vmtopo-{rid}"))
        self.assertTrue(msg.startswith("Address h1 and h2."))
        self.assertIn("./vmrun-h1", msg)
        self.assertEqual(self.detached, ["alpha"])
        self.assertFalse(self.destroys(), "the lab is still yours: not torn down when the agent finishes")
        self.assertTrue(app.topo_run_dir(rid).exists())
        self.assertEqual(r["agent_model"], f"ollama/{app.DEFAULT_MODEL}")
        self.assertEqual(app.load_model_evidence()[r["agent_model"]]["turns"], 1)
        self.assertFalse(app.topo_run_view(r)["agent_holds"])

    def test_reattaching_continues_the_same_conversation(self):
        rid = self.scratch_lab()
        self.attach(rid)
        self.back_to_ready(rid)
        self.attach(rid, custom_prompt="Now add a route.")
        self.back_to_ready(rid)
        self.assertEqual([t[1] for t in self.turns], [f"vmtopo-{rid}"] * 2)

    def test_interactive_session_on_an_existing_lab(self):
        rid = self.scratch_lab()
        self.attach(rid, interactive=True)
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "attached"))
        app.send_topo_followup(rid, "and ping h2")
        self.assertTrue(wait_for(lambda: len(self.turns) == 2))
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "attached"))
        app.end_topo_session(rid)
        self.back_to_ready(rid)
        self.assertFalse(self.destroys())

    def test_using_the_labs_task_scores_it_and_stays_ready(self):
        rid = self.scratch_lab(task_id="s1h2-connectivity")
        self.attach(rid, custom_prompt=None, use_task=True)
        self.back_to_ready(rid)
        r = app.TOPO_RUNS[rid]
        self.assertTrue(r["score"]["passed"])
        task = app.vr.get_topology_task("s1h2-connectivity")
        self.assertTrue(self.turns[0][2].startswith(task["prompt"]))

    def test_a_resumed_lab_can_get_an_agent(self):
        rid = self.scratch_lab()
        app.save_topo_lab(rid)
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "saved"))
        app.resume_topo_lab(rid)
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        for t in set(threading.enumerate()) - self._threads_before:
            t.join(timeout=8)
        self.attach(rid)
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "working" or self.turns))
        self.back_to_ready(rid)
        self.assertEqual(len(self.turns), 1)
        app.save_topo_lab(rid)                      # and it can be saved again afterwards
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "saved"))

    def test_stop_while_the_agent_works_is_the_usual_stop(self):
        for keep, destroyed in ((False, 1), (True, 0)):
            with self.subTest(keep=keep):
                self.calls.clear()
                rid = self.scratch_lab(keep=keep)
                self.attach(rid, interactive=True)
                self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "attached"))
                app.stop_topo_run(rid)
                self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "stopped"))
                for t in set(threading.enumerate()) - self._threads_before:
                    t.join(timeout=8)
                self.assertEqual(len(self.destroys()), destroyed)

    def test_validation(self):
        app.update_vmb_settings({"max_concurrent": 6})           # room for two 3-node labs at once
        rid = self.scratch_lab(task_id="s1h2-connectivity")
        bad = [({}, "no prompt"), ({"agent": ""}, "no agent"), ({"agent": "nope"}, "unknown agent"),
               ({"use_task": True}, "task and prompt together"),
               ({"custom_prompt": "x" * (app.VM_PROMPT_MAX + 1)}, "too long")]
        for form, why in bad:
            with self.subTest(why):
                full = dict({"agent": "alpha", "custom_prompt": None if why == "no prompt" else "x"}, **form)
                with self.assertRaises((ValueError, KeyError)):
                    app.attach_agent_to_lab(rid, full)
        self.down = {"beta"}
        with self.assertRaises(ValueError):
            self.attach(rid, agent="beta")                       # not running
        self.attach(rid, interactive=True)
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "attached"))
        with self.assertRaises(ValueError):
            self.attach(rid)                                     # an agent is already at work here
        rid2 = self.scratch_lab()
        with self.assertRaises(ValueError):
            self.attach(rid2)                                    # alpha is busy in the other lab
        app.end_topo_session(rid)
        self.back_to_ready(rid)
        (app.topo_run_dir(rid2) / "Vagrantfile").unlink()
        with self.assertRaises(ValueError):
            self.attach(rid2)                                    # VMs gone
        with self.assertRaises(KeyError):
            app.attach_agent_to_lab("deadbeef", {"agent": "alpha", "custom_prompt": "x"})


class SnapshotTests(VmTopoBase):
    """Config snapshots: every node's config captured on demand and after every agent turn, diffable, downloadable
    as a zip, kept after the VMs are gone and deleted with the lab."""

    def ready_lab(self):
        rid = app.create_topo_run({"topology_id": "s1h2", "keep": True})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        return rid

    def port(self, rid, node):
        return app.TOPO_RUNS[rid]["nodes"][node]["ssh_port"]

    def test_snapshot_captures_every_node_and_lists_it(self):
        rid = self.ready_lab()
        self.node_config = {self.port(rid, "h1"): "enp0s8 UP 10.0.0.1/24"}
        snap = app.take_topo_snapshot(app.TOPO_RUNS[rid], "taken by you", "before routing")
        self.assertEqual(set(snap["nodes"]), {"h1", "h2", "sw1"})
        self.assertIn("10.0.0.1/24", snap["nodes"]["h1"]["addresses"])
        listed = app.list_topo_snapshots(rid)
        self.assertEqual([(s["id"], s["label"], s["errors"]) for s in listed], [(snap["id"], "before routing", [])])
        self.assertEqual(app.load_topo_snapshot(rid, snap["id"])["nodes"], snap["nodes"])

    def test_an_unreachable_node_is_recorded_not_fatal(self):
        rid = self.ready_lab()
        self.ssh_down = {self.port(rid, "h2")}
        snap = app.take_topo_snapshot(app.TOPO_RUNS[rid], "taken by you")
        self.assertIn("Connection refused", snap["nodes"]["h2"]["_error"])
        self.assertIn("addresses", snap["nodes"]["h1"])
        self.assertEqual(app.list_topo_snapshots(rid)[0]["errors"], ["h2"])

    def test_diff_shows_only_what_changed(self):
        rid = self.ready_lab()
        r = app.TOPO_RUNS[rid]
        a = app.take_topo_snapshot(r, "x")["id"]
        self.node_config = {self.port(rid, "h1"): "enp0s8 UP 10.0.0.1/24"}
        time.sleep(1.1)                                          # ids sort by time to the second
        b = app.take_topo_snapshot(r, "y")["id"]
        d = app.diff_topo_snapshots(rid, a, b)
        self.assertEqual(d["changed"], 1)
        self.assertEqual(list(d["nodes"]), ["h1"])
        self.assertIn("+enp0s8 UP 10.0.0.1/24", d["nodes"]["h1"]["addresses"])
        self.assertEqual(app.diff_topo_snapshots(rid, b, b)["changed"], 0)

    def test_diff_ignores_volatile_countdowns_even_in_snapshots_stored_with_them(self):
        rid = self.ready_lab()
        d = app.topo_snap_dir(rid)
        d.mkdir(parents=True)
        for sid, secs in (("20260101-000000-aaaa", "default via fe80::2 dev enp0s3 proto ra expires 1600sec pref medium\n"),
                          ("20260101-000005-bbbb", "default via fe80::2 dev enp0s3 proto ra expires 1597sec pref medium\n")):
            (d / f"{sid}.json").write_text(json.dumps({"id": sid, "ts": 1, "nodes": {"h1": {"routes": secs}}}))
        self.assertEqual(app.diff_topo_snapshots(rid, "20260101-000000-aaaa", "20260101-000005-bbbb")["changed"], 0)

    def test_zip_has_a_file_per_node_and_section(self):
        import io, zipfile
        rid = self.ready_lab()
        self.node_config = {self.port(rid, "h1"): "enp0s8 UP 10.0.0.1/24"}
        sid = app.take_topo_snapshot(app.TOPO_RUNS[rid], "x", "for review")["id"]
        name, data = app.topo_snapshot_zip(rid, sid)
        self.assertEqual(name, f"lab-{rid}-{sid}.zip")
        z = zipfile.ZipFile(io.BytesIO(data))
        names = set(z.namelist())
        self.assertIn(f"lab-{rid}-{sid}/h1/addresses.txt", names)
        self.assertIn(f"lab-{rid}-{sid}/sw1/routes.txt", names)
        self.assertIn("for review", z.read(f"lab-{rid}-{sid}/README.txt").decode())
        self.assertIn("10.0.0.1/24", z.read(f"lab-{rid}-{sid}/h1/addresses.txt").decode())

    def test_file_sections_get_safe_names_in_the_zip(self):
        import io, zipfile
        rid = self.ready_lab()
        self.node_config = {self.port(rid, "h1"): "x\n### file /etc/nginx/conf.d/lb.conf\nupstream b {}"}
        sid = app.take_topo_snapshot(app.TOPO_RUNS[rid], "x")["id"]
        names = zipfile.ZipFile(io.BytesIO(app.topo_snapshot_zip(rid, sid)[1])).namelist()
        self.assertIn(f"lab-{rid}-{sid}/h1/file_etc_nginx_conf.d_lb.conf.txt", names)

    def test_snapshots_in_the_same_clock_tick_keep_their_order(self):
        # regression: on Windows time.time() only advances every ~15 ms, so quick snapshots shared a timestamp
        rid = self.ready_lab()
        with mock.patch.object(app.time, "time", lambda: 1_800_000_000.0):
            ids = [app.take_topo_snapshot(app.TOPO_RUNS[rid], f"x{i}")["id"] for i in range(4)]
        self.assertEqual([s["id"] for s in app.list_topo_snapshots(rid)], ids[::-1])

    def test_only_the_newest_snapshots_are_kept(self):
        rid = self.ready_lab()
        with mock.patch.object(app, "TOPO_SNAP_KEEP", 3):
            ids = []
            for i in range(5):
                with mock.patch.object(app.time, "strftime", lambda f, t=None, i=i: f"2026010{i}-000000"):
                    ids.append(app.take_topo_snapshot(app.TOPO_RUNS[rid], "x")["id"])
        self.assertEqual([s["id"] for s in app.list_topo_snapshots(rid)], ids[:1:-1])

    def test_agent_turns_take_a_snapshot_each(self):
        self.add_agent("alpha")
        rid = app.create_topo_run({"topology_id": "s1h2", "agent": "alpha", "custom_prompt": "x", "keep": True})
        self.assertTrue(self.finished(rid))
        snaps = app.list_topo_snapshots(rid)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["trigger"], "after alpha's turn 1")

    def test_snapshots_outlive_the_vms_and_go_with_the_lab(self):
        rid = self.ready_lab()
        app.take_topo_snapshot(app.TOPO_RUNS[rid], "x")
        app.stop_topo_run(rid)
        shutil.rmtree(app.topo_run_dir(rid))                     # VMs gone
        self.assertEqual(len(app.list_topo_snapshots(rid)), 1)
        app.delete_topo_run(rid)
        self.assertFalse(app.topo_snap_dir(rid).exists())

    def test_refused_when_the_vms_are_not_running_and_bad_ids_rejected(self):
        rid = self.ready_lab()
        app.TOPO_RUNS[rid]["state"] = "saved"
        with self.assertRaises(ValueError):
            app.take_topo_snapshot(app.TOPO_RUNS[rid], "x")
        with self.assertRaises(ValueError):
            app.snapshot_topo_lab(rid)
        for bad in ("../../etc", "x", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.load_topo_snapshot(rid, bad)
        with self.assertRaises(KeyError):
            app.load_topo_snapshot(rid, "20260101-000000-abcd")


class LabFileRunTests(VmTopoBase):
    """Building a lab from a lab file: the file's topology, its configs applied before the lab is ready, a check of the
    rebuild against the file, and exporting a lab as a file that builds the same lab again."""

    TOPO = {"nodes": [{"name": "h1", "role": "host"}, {"name": "sw1", "role": "switch"}, {"name": "h2", "role": "host"}],
            "links": [{"a": "h1", "b": "sw1"}, {"a": "h2", "b": "sw1"}]}
    CFG = {"h1": {"addresses": "lo UNKNOWN 127.0.0.1/8\nenp0s8 UP 10.0.0.1/24\n", "forwarding": "net.ipv4.ip_forward = 0\n"},
           "h2": {"addresses": "lo UNKNOWN 127.0.0.1/8\nenp0s8 UP 10.0.0.2/24\n"}}

    def build(self, configs=None, **form):
        lf = vr.build_labfile("Two hosts", self.TOPO, configs if configs is not None else self.CFG)
        rid = app.create_topo_run(dict({"labfile": lf}, **form))
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] in ("ready", "error")))
        return rid

    def port(self, rid, node):
        return app.TOPO_RUNS[rid]["nodes"][node]["ssh_port"]

    def test_configs_are_applied_before_ready_and_the_rebuild_is_checked(self):
        # the rebuilt nodes report exactly the file's config, so the check finds no differences
        self.node_config = {}
        orig = self.fake_ssh_script

        def fake(port, priv, script, timeout=60):
            if script == vr.SNAPSHOT_SCRIPT:
                node = next(n for n, x in app.TOPO_RUNS[rid_box[0]]["nodes"].items() if x["ssh_port"] == port)
                text = "".join(f"### {s}\n{t}" for s, t in self.CFG.get(node, {}).items())
                return 0, text, ""
            return orig(port, priv, script, timeout)
        rid_box = [None]
        with mock.patch.object(vr, "ssh_script", fake):
            lf = vr.build_labfile("Two hosts", self.TOPO, self.CFG)
            rid_box[0] = rid = app.create_topo_run({"labfile": lf})
            self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        r = app.TOPO_RUNS[rid]
        self.assertEqual(r["topology_title"], "From file: Two hosts")
        self.assertEqual(list(r["nodes"]), ["h1", "sw1", "h2"])
        applied = {next(n for n, x in r["nodes"].items() if x["ssh_port"] == port): script for port, script in self.applied}
        self.assertEqual(set(applied), {"h1", "h2"}, "only nodes with configs get an apply script")
        self.assertIn("ip addr replace 10.0.0.1/24 dev enp0s8", applied["h1"])
        self.assertEqual(r["labfile_check"]["mismatches"], {})
        self.assertIn("every node matches the lab file", r["vm_log"])
        self.assertEqual(app.list_topo_snapshots(rid)[0]["trigger"], "applied from lab file")
        self.assertEqual(app.topo_run_view(r)["from_labfile"], "Two hosts")

    def test_a_rebuild_that_differs_is_reported(self):
        rid = self.build()                      # the default fake snapshot has no 10.0.0.x addresses
        check = app.TOPO_RUNS[rid]["labfile_check"]["mismatches"]
        self.assertEqual(check, {"h1": ["addresses", "forwarding"], "h2": ["addresses"]})
        self.assertIn("differs from the lab file", app.TOPO_RUNS[rid]["vm_log"])
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "ready", "a mismatch is reported, not fatal")

    def test_export_round_trips_into_the_same_lab(self):
        app.update_vmb_settings({"max_concurrent": 6})           # room for the original and the rebuilt lab at once
        rid = app.create_topo_run({"topology_id": "s1h2", "keep": True})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "ready"))
        with self.assertRaises(ValueError):
            app.labfile_for_lab(rid)                              # no snapshot yet
        self.node_config = {self.port(rid, "h1"): "enp0s8 UP 10.5.0.1/24"}
        sid = app.take_topo_snapshot(app.TOPO_RUNS[rid], "x", "for export")["id"]
        name, data = app.labfile_for_lab(rid)
        self.assertEqual(name, f"lab-{rid}-{sid}.json")
        lf = json.loads(data)
        self.assertEqual(lf["source"]["snapshot"], sid)
        title, topo, configs, _ = vr.parse_labfile(lf)
        cat = vr.get_topology("s1h2")
        self.assertEqual([n["name"] for n in topo["nodes"]], [n["name"] for n in cat["nodes"]], "node order kept: it sets NIC order")
        self.assertEqual(topo["links"], [{"a": l["a"], "b": l["b"]} for l in cat["links"]])
        self.assertIn("10.5.0.1/24", configs["h1"]["addresses"])
        self.applied = []
        rid2 = app.create_topo_run({"labfile": lf})
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid2]["state"] == "ready"))
        self.assertTrue(any("ip addr replace 10.5.0.1/24 dev enp0s8" in s for _, s in self.applied))

    def test_a_lab_from_a_file_has_no_catalog_task_and_bad_files_are_refused(self):
        lf = vr.build_labfile("x", self.TOPO, {})
        with self.assertRaises(ValueError):
            app.create_topo_run({"labfile": lf, "task_id": "s1h2-connectivity"})
        with self.assertRaises(ValueError):
            app.create_topo_run({"labfile": {"format": "nope"}})
        self.assertEqual(app.TOPO_RUNS, {})

    def test_an_agent_can_work_in_a_lab_built_from_a_file(self):
        self.add_agent("alpha")
        rid = app.create_topo_run({"labfile": vr.build_labfile("Two hosts", self.TOPO, self.CFG), "agent": "alpha",
                                   "custom_prompt": "add a route"})
        self.assertTrue(self.finished(rid))
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "done")
        self.assertTrue(self.applied, "configs were applied before the agent's turn")


class RouterPromptTests(VmTopoBase):
    def prompt_for(self, topology_id):
        self.add_agent("alpha")
        prompts = []
        with mock.patch.object(app, "run_turn", lambda name, chat, msg, *a, **k: prompts.append(msg) or {"reply": "ok", "ok": True}):
            rid = app.create_topo_run({"topology_id": topology_id, "agent": "alpha", "custom_prompt": "x"})
            self.assertTrue(self.finished(rid))
        return prompts[0]

    def test_labs_with_routers_tell_the_agent_about_frr(self):
        self.assertIn("vtysh", self.prompt_for("r2s2h2"))

    def test_every_lab_prompt_warns_about_the_setup_network(self):
        self.assertIn("10.0.2.0/24", self.prompt_for("s1h2"))

    def test_lab_prompts_explain_vlans_on_switches(self):
        p = self.prompt_for("s1h2")
        self.assertIn("vlan_filtering 1", p)
        self.assertIn("pvid untagged", p)

    def test_labs_without_routers_do_not(self):
        self.assertNotIn("vtysh", self.prompt_for("s1h2"))


class PortCollisionTests(VmTopoBase):
    """Vagrant refusing a node's forwarded port because something else answered on it: that node gets a new port and
    `vagrant up` runs again, instead of the whole lab failing (found building a 6-node lab on Windows)."""

    def collide(self, node, times=1):
        state = {"left": times}
        real = self.fake_vagrant_stream
        renders = []

        def stream(run_dir, *args, on_line=None, timeout=120, cancel=None):
            if args[0] == "up" and state["left"]:
                state["left"] -= 1
                rid = run_dir.name
                port = app.TOPO_RUNS[rid]["nodes"][node]["ssh_port"]
                self.calls.append(("vagrant", args))
                return 1, f"Vagrant cannot forward the specified ports on this VM... The forwarded port to {port} is already in use\non the host machine.", ""
            return real(run_dir, *args, on_line=on_line, timeout=timeout, cancel=cancel)
        return stream, renders

    def test_the_colliding_node_gets_a_new_port_and_the_build_continues(self):
        stream, _ = self.collide("sw1")
        renders = []
        with mock.patch.object(vr, "vagrant_stream", stream), \
             mock.patch.object(vr, "render_topology_vagrantfile", lambda d, rid, topo, ports, *a, **k: renders.append(dict(ports))):
            rid = app.create_topo_run({"topology_id": "s1h2"})
            self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] in ("ready", "error")))
        r = app.TOPO_RUNS[rid]
        self.assertEqual(r["state"], "ready", r.get("reason"))
        self.assertEqual(len(renders), 2, "re-rendered once with the new port")
        self.assertNotEqual(renders[0]["sw1"], renders[1]["sw1"])
        self.assertEqual({n: p for n, p in renders[0].items() if n != "sw1"}, {n: p for n, p in renders[1].items() if n != "sw1"})
        self.assertEqual(r["nodes"]["sw1"]["ssh_port"], renders[1]["sw1"])
        self.assertIn("was taken by something else", r["vm_log"])
        self.assertEqual(len([c for c in self.calls if c[0] == "vagrant" and c[1][0] == "up"]), 2)

    def test_it_gives_up_after_a_few_tries(self):
        stream, _ = self.collide("sw1", times=99)
        with mock.patch.object(vr, "vagrant_stream", stream):
            rid = app.create_topo_run({"topology_id": "s1h2"})
            self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] in ("ready", "error")))
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "error")
        self.assertEqual(len([c for c in self.calls if c[0] == "vagrant" and c[1][0] == "up"]), vr.PORT_COLLISION_RETRIES + 1)


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


class InstantStopTests(VmTopoBase):
    """Stop during a build used to wait for the whole `vagrant up` (every node, for a lab) to finish before
    tearing down. Now the build is cancelled and the half-built lab is destroyed right away."""

    def test_stop_mid_build_cancels_it_and_tears_down(self):
        self.up_blocks = True
        rid = app.create_topo_run({"topology_id": "r2s2h2"})
        self.assertTrue(wait_for(lambda: any(c[0] == "vagrant" and c[1][0] == "up" for c in self.calls)))
        t0 = time.time()
        app.stop_topo_run(rid)
        self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] == "stopped", timeout=5))
        self.assertLess(time.time() - t0, 3, "Stop should take effect within about a second, not after the build")
        self.assertEqual(app.TOPO_RUNS[rid]["reason"], "stopped during setup")
        for t in set(threading.enumerate()) - self._threads_before:
            t.join(timeout=8)
        self.assertIn(("destroy_after_cancel",), self.calls)
        self.assertFalse([c for c in self.calls if c[0] == "vagrant" and c[1][0] == "destroy"],
                         "already torn down after the cancel; the runner's cleanup must not destroy a second time")
        self.assertFalse(app.topo_run_dir(rid).exists(), "the run folder (Vagrantfile, SSH key) is still removed")

    def test_stop_while_waiting_for_ssh_stops_instead_of_erroring(self):
        waits = []

        def slow_ssh_wait(port, key, tries=60, delay=2, on_attempt=None, cancel=None):
            waits.append(port)
            while not (cancel and cancel()):
                time.sleep(0.02)
            return False
        with mock.patch.object(vr, "ssh_wait", slow_ssh_wait):
            rid = app.create_topo_run({"topology_id": "r2s2h2"})
            self.assertTrue(wait_for(lambda: waits))
            app.stop_topo_run(rid)
            self.assertTrue(wait_for(lambda: app.TOPO_RUNS[rid]["state"] in ("stopped", "error"), timeout=5))
        self.assertEqual(app.TOPO_RUNS[rid]["state"], "stopped")


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
