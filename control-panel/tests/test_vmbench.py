"""Tests for the VM-benchmark state machine in app.py. Vagrant/VirtualBox/SSH and docker compose are all mocked out,
so these run fast with no real VM. Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import json, shutil, sys, tempfile, threading, time, unittest
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


class VmBenchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(app, "DATA", self.tmp / "instances"),
            mock.patch.object(app, "VMR_DIR", self.tmp / "vm-runs"),
            mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs"),  # defense in depth; see topo_log() in app.py
            mock.patch.object(app, "VMB_SETTINGS_FILE", self.tmp / "vmbench-settings.json"),
            mock.patch.object(app, "VM_RUNS", {}),
            mock.patch.object(app, "VM_STOP", {}),
            mock.patch.object(app, "VM_FOLLOWUPS", {}),
            mock.patch.object(app, "VM_END", {}),
            mock.patch.object(app, "VM_SESSION_POLL_S", 0.02),
            mock.patch.object(app, "VM_TERM_CREDS", {}),
            mock.patch.object(app, "agent_running", lambda n: n not in self.down),
            mock.patch.object(vr, "allocate_port", lambda rng, taken: next(p for p in range(*rng) if p not in taken)),
            mock.patch.object(vr, "gen_keypair", self.fake_gen_keypair),
            mock.patch.object(vr, "render_vagrantfile", lambda *a, **k: None),
            mock.patch.object(vr, "write_seed_files", lambda d, seed: None),
            mock.patch.object(vr, "vagrant", self.fake_vagrant),
            mock.patch.object(vr, "ssh_wait", lambda *a, **k: True),
            mock.patch.object(vr, "ssh_run", self.fake_ssh_run),
            mock.patch.object(vr, "scp_to", lambda *a, **k: (0, "", "")),
            mock.patch.object(app, "run", self.fake_run),
            mock.patch.object(app, "dc", self.fake_dc),
            mock.patch.object(app, "run_turn", self.fake_run_turn),
        ]
        self._threads_before = set(threading.enumerate())
        for p in self.patches:
            p.start()
        app.VMR_DIR.mkdir(parents=True)
        (self.tmp / "instances").mkdir(parents=True)
        self.down = set()
        self.vagrant_fails = False
        self.ssh_check_passes = True
        self.calls = []

    def tearDown(self):
        # Every create_vm_run()/stop_vm_run() spawns a background thread. If one is still alive when the mocks
        # below are torn down, module-level names like VM_RUNS/VM_STOP/VMR_DIR flip back to the REAL (or the NEXT
        # test's) state mid-execution. Concretely this broke `stopped()`: it re-reads `app.VM_STOP` by module-level
        # lookup on every call, not a captured reference, so once a later test's setUp swaps in a fresh dict, an
        # orphaned thread from THIS test silently starts reading that dict instead and never sees its stop flag -
        # the exact cause of an intermittent failure found in test_deleting_an_agent_stops_its_live_vm_runs when
        # running the whole module (never reproduced running that one test class alone). Waiting for "state looks
        # finished" isn't enough either: `finish()` sets the state before the `finally` block's teardown work
        # (vagrant destroy, file cleanup) runs, so the thread hasn't actually returned yet. Join every thread this
        # test spawned, for real, before any patch is undone.
        for rid, r in list(app.VM_RUNS.items()):
            if r["state"] in app.VM_LIVE_STATES:
                app.VM_STOP[rid] = True
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

    def fake_ssh_run(self, port, priv, command, timeout=120):
        self.calls.append(("ssh_run", command[:40]))
        return (0, "pass output", "") if self.ssh_check_passes else (1, "fail output", "")

    def fake_run(self, args, input=None, timeout=120, env=None, redact=None):
        self.calls.append(("run", args[:3] if isinstance(args, list) else args))
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
        # with an agent attached, "ready" is only a momentary step on the way to "working" (see app.stop_vm_runs_for);
        # treating it as finished made agent-run tests flaky on slow CI runners (seen as 'ready' != 'done')
        settled = app.VM_OCCUPYING_STATES if app.VM_RUNS[rid].get("agent") else app.VM_LIVE_STATES
        return wait_for(lambda: app.VM_RUNS[rid]["state"] not in settled, timeout)


class SettingsTests(VmBenchBase):
    def test_defaults(self):
        s = app.vmb_settings()
        self.assertEqual(s, {"max_concurrent": 2, "memory_mb": 1536, "cpus": 1, "keep_default": False})

    def test_validation(self):
        for bad in ({"max_concurrent": 0}, {"max_concurrent": 7}, {"memory_mb": 100}, {"memory_mb": 99999}, {"cpus": 0}, {"cpus": 5}):
            with self.assertRaises(ValueError, msg=str(bad)):
                app.update_vmb_settings(bad)

    def test_update_persists(self):
        app.update_vmb_settings({"max_concurrent": 4, "memory_mb": 2048})
        self.assertEqual(app.vmb_settings()["max_concurrent"], 4)
        self.assertEqual(app.vmb_settings()["memory_mb"], 2048)


class ScratchVmTests(VmBenchBase):
    """A run with no task and no agent: just a VM the user opens a terminal into."""

    def test_full_lifecycle_no_task_no_agent(self):
        # No agent means there is no "turn" to finish: the run settles at "ready" and STAYS there, VM alive, so the
        # user can open a terminal / do the task themselves / Score now whenever they like. It must not be torn
        # down automatically - only Stop or Delete do that.
        rid = app.create_vm_run({})
        self.assertTrue(wait_for(lambda: app.VM_RUNS[rid]["state"] == "ready"))
        r = app.VM_RUNS[rid]
        self.assertIsNone(r["task_id"])
        self.assertIsNone(r["agent"])
        self.assertIsNone(r["score"])
        self.assertTrue(app.vm_run_dir(rid).exists(), "the VM's run directory must survive while state is 'ready'")
        app.stop_vm_run(rid)
        self.assertEqual(app.VM_RUNS[rid]["state"], "stopped")

    def test_cannot_delete_while_live(self):
        with mock.patch.object(vr, "ssh_wait", lambda *a, **k: (time.sleep(1), True)[1]):
            rid = app.create_vm_run({})
            with self.assertRaises(ValueError):
                app.delete_vm_run(rid)
            self.assertTrue(self.finished(rid, timeout=5))
        app.delete_vm_run(rid)
        self.assertNotIn(rid, app.VM_RUNS)

    def test_unknown_task_rejected(self):
        with self.assertRaises(KeyError):
            app.create_vm_run({"task_id": "no-such-task"})

    def test_vagrant_failure_marks_error_not_crash(self):
        self.vagrant_fails = True
        rid = app.create_vm_run({})
        self.assertTrue(self.finished(rid))
        self.assertEqual(app.VM_RUNS[rid]["state"], "error")


class StopRaceTests(VmBenchBase):
    def test_stop_right_after_ready_destroys_the_vm_once(self):
        """Regression: a no-agent run's thread returns at "ready" and its `finally` used to tear down whenever the
        state LOOKED finished. A Stop landing between that return and the `finally` (stop_vm_run destroys the VM
        itself and marks it "stopped") made the thread destroy it a second time. Stopping from inside the runner's
        "ready" log line hits that window every time instead of by chance (it surfaced as a flaky CI failure)."""
        real_log = app.vm_log

        def log_then_stop(r, line):
            real_log(r, line)
            if line.startswith("ready"):
                app.stop_vm_run(r["id"])
        with mock.patch.object(app, "vm_log", log_then_stop):
            rid = app.create_vm_run({})
            for t in set(threading.enumerate()) - self._threads_before:
                t.join(timeout=8)
        self.assertEqual(app.VM_RUNS[rid]["state"], "stopped")
        destroy_calls = [c for c in self.calls if c[0] == "vagrant" and c[1][0] == "destroy"]
        self.assertEqual(len(destroy_calls), 1, "Stop and the runner thread must not both destroy the VM")


class TaskWithAgentTests(VmBenchBase):
    def test_agent_must_exist_and_be_running(self):
        with self.assertRaises(KeyError):
            app.create_vm_run({"task_id": "fizzbuzz-cli", "agent": "nope"})
        self.add_agent("alpha")
        self.down = {"alpha"}
        with self.assertRaises(ValueError):
            app.create_vm_run({"task_id": "fizzbuzz-cli", "agent": "alpha"})

    def test_full_run_attaches_relay_calls_agent_and_scores(self):
        self.add_agent("alpha")
        rid = app.create_vm_run({"task_id": "fizzbuzz-cli", "agent": "alpha"})
        self.assertTrue(self.finished(rid))
        r = app.VM_RUNS[rid]
        self.assertEqual(r["state"], "done", r.get("reason"))
        self.assertEqual(r["chat"], f"vmbench-{rid}")
        self.assertIsNotNone(r["score"])
        self.assertTrue(r["score"]["passed"])                         # fake_ssh_run defaults to passing
        kinds = [c[0] for c in self.calls]
        self.assertIn("run_turn", kinds)
        self.assertIn("ssh_run", kinds)
        relay_ups = [c for c in self.calls if c[0] == "run" and "vm-relay" not in str(c)]
        # the vm-relay container is brought up and later removed via app.run(); just confirm run() was used at all
        self.assertTrue(any(c[0] == "run" for c in self.calls))

    def test_agent_gets_the_stdin_capable_wrapper_and_is_told_how_to_keep_dollar_signs(self):
        self.add_agent("alpha")
        inputs, prompts = [], []
        real_dc = self.fake_dc

        def dc(name, *args, input=None, timeout=120):
            inputs.append(input or "")
            return real_dc(name, *args, input=input, timeout=timeout)

        def run_turn(name, chat_id, message, *a, **k):
            prompts.append(message)
            return {"reply": "done", "ok": True}
        with mock.patch.object(app, "dc", dc), mock.patch.object(app, "run_turn", run_turn):
            rid = app.create_vm_run({"task_id": "fizzbuzz-cli", "agent": "alpha"})
            self.assertTrue(self.finished(rid))
        wrapper = next(i for i in inputs if i.startswith("#!/bin/sh"))
        self.assertIn("'bash -s'", wrapper)
        self.assertIn("bench@vm-relay", wrapper)
        self.assertEqual(len(prompts), 1)
        self.assertIn("./vmrun <<'EOF'", prompts[0])
        self.assertIn("single quotes", prompts[0])
        self.assertIn("run what you built", prompts[0])

    def test_failing_check_script_gives_a_failed_score_not_an_error(self):
        self.add_agent("alpha")
        self.ssh_check_passes = False
        rid = app.create_vm_run({"task_id": "fizzbuzz-cli", "agent": "alpha"})
        self.assertTrue(self.finished(rid))
        r = app.VM_RUNS[rid]
        self.assertEqual(r["state"], "done", r.get("reason"))                          # a bad solution is still a completed run
        self.assertFalse(r["score"]["passed"])


class AgentSessionTests(VmBenchBase):
    """Your own prompt for an agent in a VM, and interactive sessions: the agent stays attached after its first turn
    and you send it more guidance in the same conversation until you end the session."""

    def setUp(self):
        super().setUp()
        self.add_agent("alpha")
        self.turns = []                                          # (chat_id, message) per agent turn
        self.detached = []
        p1 = mock.patch.object(app, "run_turn", self.record_turn)
        p2 = mock.patch.object(app, "detach_vm_agent", lambda agent: self.detached.append(agent))
        for p in (p1, p2):
            p.start()
            self.patches.append(p)

    def record_turn(self, name, chat_id, message, meta=None, log=lambda s: None, title=None, timeout=700):
        self.turns.append((chat_id, message))
        return {"reply": "ok", "ok": True}

    def state(self, rid):
        return app.VM_RUNS[rid]["state"]

    def destroys(self):
        return [c for c in self.calls if c[0] == "vagrant" and c[1][0] == "destroy"]

    # ---- your own prompt
    def test_custom_prompt_is_sent_with_vmrun_instructions_and_not_scored(self):
        rid = app.create_vm_run({"agent": "alpha", "custom_prompt": "Install nginx and serve hello on port 80."})
        self.assertTrue(self.finished(rid))
        r = app.VM_RUNS[rid]
        self.assertEqual(r["state"], "done", r.get("reason"))
        self.assertIsNone(r["score"])
        self.assertEqual(len(self.turns), 1)
        msg = self.turns[0][1]
        self.assertTrue(msg.startswith("Install nginx and serve hello on port 80."))
        self.assertIn("./vmrun <<'EOF'", msg)
        self.assertNotIn("more guidance", msg)                   # not interactive
        self.assertIn("Your own prompt", app.VM_RUNS[rid]["vm_log"])
        self.assertEqual(self.detached, ["alpha"])
        self.assertFalse(any(c[0] == "ssh_run" for c in self.calls), "no check script to run for a custom prompt")
        self.assertEqual(app.vm_run_view(r)["custom_prompt"], "Install nginx and serve hello on port 80.")

    def test_prompt_validation(self):
        for form, why in (({"agent": "alpha"}, "task or prompt"),
                          ({"agent": "alpha", "task_id": "fizzbuzz-cli", "custom_prompt": "x"}, "not both"),
                          ({"custom_prompt": "x"}, "prompt without an agent"),
                          ({"interactive": True}, "session without an agent"),
                          ({"agent": "alpha", "custom_prompt": "x" * (app.VM_PROMPT_MAX + 1)}, "too long")):
            with self.subTest(why), self.assertRaises(ValueError):
                app.create_vm_run(form)
        self.assertEqual(app.VM_RUNS, {})

    def test_an_agent_can_only_be_attached_to_one_vm_at_a_time(self):
        rid = app.create_vm_run({"agent": "alpha", "custom_prompt": "first", "interactive": True})
        self.assertTrue(wait_for(lambda: self.state(rid) == "attached"))
        with self.assertRaises(ValueError):
            app.create_vm_run({"agent": "alpha", "custom_prompt": "second"})
        app.end_vm_session(rid)
        self.assertTrue(self.finished(rid))
        rid2 = app.create_vm_run({"agent": "alpha", "custom_prompt": "second"})   # free again once it ended
        self.assertTrue(self.finished(rid2))

    # ---- interactive sessions
    def test_session_relays_follow_ups_into_the_same_conversation_then_ends_cleanly(self):
        rid = app.create_vm_run({"agent": "alpha", "custom_prompt": "Set up a Flask app.", "interactive": True})
        self.assertTrue(wait_for(lambda: self.state(rid) == "attached"))
        self.assertIn("more guidance", self.turns[0][1])
        self.assertEqual(self.detached, [], "the agent stays attached while the session is open")
        self.assertIsNotNone(app.vm_run_view(app.VM_RUNS[rid])["idle_deadline"])

        app.send_vm_followup(rid, "Use port 8080 instead.")
        self.assertTrue(wait_for(lambda: len(self.turns) == 2))
        self.assertTrue(wait_for(lambda: self.state(rid) == "attached"))
        self.assertEqual(self.turns[1], (f"vmbench-{rid}", "Use port 8080 instead." + app.VM_FOLLOWUP_REMINDER))
        self.assertIn("./vmrun", app.VM_FOLLOWUP_REMINDER)
        self.assertEqual(self.turns[0][0], self.turns[1][0], "follow-ups continue the same conversation")
        self.assertIn("you: Use port 8080 instead.", app.VM_RUNS[rid]["vm_log"])

        app.end_vm_session(rid)
        self.assertTrue(self.finished(rid))
        for t in set(threading.enumerate()) - self._threads_before:
            t.join(timeout=8)                # "done" is set before the runner's teardown (vagrant destroy) runs
        self.assertEqual(self.state(rid), "done")
        self.assertEqual(self.detached, ["alpha"])
        self.assertEqual(len(self.destroys()), 1, "not kept, so the VM is destroyed once the session ends")
        with self.assertRaises(ValueError):
            app.send_vm_followup(rid, "too late")

    def test_session_on_a_catalog_task_is_scored_when_it_ends(self):
        rid = app.create_vm_run({"agent": "alpha", "task_id": "fizzbuzz-cli", "interactive": True})
        self.assertTrue(wait_for(lambda: self.state(rid) == "attached"))
        self.assertFalse(any(c[0] == "ssh_run" for c in self.calls), "not scored while the session is open")
        app.end_vm_session(rid)
        self.assertTrue(self.finished(rid))
        self.assertTrue(app.VM_RUNS[rid]["score"]["passed"])

    def test_score_now_during_a_session_keeps_it_open(self):
        rid = app.create_vm_run({"agent": "alpha", "task_id": "fizzbuzz-cli", "interactive": True})
        self.assertTrue(wait_for(lambda: self.state(rid) == "attached"))
        jid = app.score_now(rid)
        self.assertTrue(wait_for(lambda: app.JOBS[jid]["done"]))
        self.assertEqual(self.state(rid), "attached")

    def test_stop_during_a_session_detaches_without_scoring(self):
        rid = app.create_vm_run({"agent": "alpha", "task_id": "fizzbuzz-cli", "interactive": True})
        self.assertTrue(wait_for(lambda: self.state(rid) == "attached"))
        app.stop_vm_run(rid)
        self.assertTrue(self.finished(rid))
        self.assertEqual(self.state(rid), "stopped")
        self.assertEqual(self.detached, ["alpha"])
        self.assertIsNone(app.VM_RUNS[rid]["score"])

    def test_idle_session_ends_by_itself(self):
        with mock.patch.object(app, "VM_SESSION_IDLE_S", 0.2):
            rid = app.create_vm_run({"agent": "alpha", "custom_prompt": "x", "interactive": True})
            self.assertTrue(self.finished(rid))
        self.assertEqual(self.state(rid), "done")
        self.assertIn("ending the session", app.VM_RUNS[rid]["vm_log"])
        self.assertEqual(self.detached, ["alpha"])

    def test_follow_ups_and_end_are_refused_without_an_open_session(self):
        rid = app.create_vm_run({"agent": "alpha", "custom_prompt": "x"})        # not interactive
        self.assertTrue(self.finished(rid))
        with self.assertRaises(ValueError):
            app.send_vm_followup(rid, "hello")
        with self.assertRaises(ValueError):
            app.end_vm_session(rid)
        rid2 = app.create_vm_run({"agent": "alpha", "custom_prompt": "x", "interactive": True})
        self.assertTrue(wait_for(lambda: self.state(rid2) == "attached"))
        for bad in ("", "   ", "x" * 100_001):
            with self.subTest(len=len(bad)), self.assertRaises(ValueError):
                app.send_vm_followup(rid2, bad)
        with self.assertRaises(KeyError):
            app.send_vm_followup("deadbeef", "hello")

    def test_run_view_includes_the_conversation(self):
        rid = app.create_vm_run({"agent": "alpha", "custom_prompt": "x"})
        self.assertTrue(self.finished(rid))
        app.append_message("alpha", f"vmbench-{rid}", "user", "x")
        app.append_message("alpha", f"vmbench-{rid}", "agent", "done it")
        conv = app.vm_run_view(app.VM_RUNS[rid], full=True)["conversation"]
        self.assertEqual([(m["role"], m["text"]) for m in conv], [("user", "x"), ("agent", "done it")])

    def test_restart_marks_open_sessions_interrupted_and_detaches_the_agent(self):
        rec = {"id": "abcd1234", "state": "attached", "agent": "alpha", "reason": "", "created": 1.0}
        app.vm_run_path("abcd1234").write_text(json.dumps(rec))
        before = set(threading.enumerate())
        app.load_vm_runs()
        for t in set(threading.enumerate()) - before:
            t.join(timeout=5)
        self.assertEqual(app.VM_RUNS["abcd1234"]["state"], "interrupted")
        self.assertEqual(self.detached, ["alpha"])


class ConcurrencyTests(VmBenchBase):
    def test_second_run_waits_for_a_slot_and_an_idle_ready_vm_keeps_holding_it(self):
        app.update_vmb_settings({"max_concurrent": 1})
        hold = {"go": False}

        def slow_ssh_wait(*a, **k):
            while not hold["go"]:
                time.sleep(0.02)
            return True
        with mock.patch.object(vr, "ssh_wait", slow_ssh_wait):
            rid1 = app.create_vm_run({})
            self.assertTrue(wait_for(lambda: app.VM_RUNS[rid1]["state"] == "provisioning"))
            rid2 = app.create_vm_run({})
            time.sleep(0.3)
            self.assertEqual(app.VM_RUNS[rid2]["state"], "queued")     # still waiting: slot 1 is taken
            hold["go"] = True
            self.assertTrue(wait_for(lambda: app.VM_RUNS[rid1]["state"] == "ready"))
        time.sleep(0.3)
        # rid1 is idle but its VM is still up, so it still occupies the one available slot - rid2 must NOT proceed
        self.assertEqual(app.VM_RUNS[rid2]["state"], "queued")
        app.stop_vm_run(rid1)                                          # explicitly releasing rid1 frees the slot
        self.assertTrue(wait_for(lambda: app.VM_RUNS[rid2]["state"] == "ready", timeout=6))

    def test_stop_while_queued(self):
        app.update_vmb_settings({"max_concurrent": 1})
        with mock.patch.object(vr, "ssh_wait", lambda *a, **k: (time.sleep(2), True)[1]):
            rid1 = app.create_vm_run({})
            self.assertTrue(wait_for(lambda: app.VM_RUNS[rid1]["state"] == "provisioning"))
            rid2 = app.create_vm_run({})
            self.assertTrue(wait_for(lambda: app.VM_RUNS[rid2]["state"] == "queued"))
            app.stop_vm_run(rid2)
            self.assertTrue(self.finished(rid2, timeout=5))
            self.assertEqual(app.VM_RUNS[rid2]["state"], "stopped")
            app.stop_vm_run(rid1)
            self.assertTrue(self.finished(rid1, timeout=5))


class BenchmarkTests(VmBenchBase):
    def test_same_task_across_several_agents_shares_a_benchmark_id(self):
        self.add_agent("alpha")
        self.add_agent("beta")
        bid, ids = app.create_benchmark({"task_id": "fizzbuzz-cli", "agents": ["alpha", "beta"]})
        self.assertEqual(len(ids), 2)
        for rid in ids:
            self.assertTrue(self.finished(rid))
            self.assertEqual(app.VM_RUNS[rid]["benchmark_id"], bid)
        self.assertEqual({app.VM_RUNS[r]["agent"] for r in ids}, {"alpha", "beta"})

    def test_requires_at_least_one_agent(self):
        with self.assertRaises(ValueError):
            app.create_benchmark({"task_id": "fizzbuzz-cli", "agents": []})


class ScoreNowTests(VmBenchBase):
    def test_score_now_on_a_ready_scratch_vm(self):
        rid = app.create_vm_run({"task_id": "fizzbuzz-cli"})   # no agent: just provisions then sits "ready"
        self.assertTrue(wait_for(lambda: app.VM_RUNS[rid]["state"] in ("ready", "done")))
        jid = app.score_now(rid)
        self.assertTrue(wait_for(lambda: app.JOBS[jid]["done"]))
        self.assertTrue(app.JOBS[jid]["ok"])
        self.assertTrue(app.VM_RUNS[rid]["score"]["passed"])

    def test_score_now_requires_a_task(self):
        rid = app.create_vm_run({})
        self.assertTrue(self.finished(rid))
        with self.assertRaises(ValueError):
            app.score_now(rid)

    def test_score_now_unknown_run(self):
        with self.assertRaises(KeyError):
            app.score_now("deadbeef")


class TerminalTests(VmBenchBase):
    def test_cannot_open_terminal_before_vm_is_ready(self):
        app.VM_RUNS["x"] = {"id": "x", "state": "provisioning", "terminal": {"active": False}}
        with self.assertRaises(ValueError):
            app.start_terminal("x")

    def test_start_is_idempotent_and_stop_clears_state(self):
        rid = app.create_vm_run({})
        self.assertTrue(self.finished(rid))
        app.vm_run_dir(rid).mkdir(parents=True, exist_ok=True)
        (app.vm_run_dir(rid) / "id_ed25519").write_text("k")
        port1, cred1 = app.start_terminal(rid)
        port2, cred2 = app.start_terminal(rid)                 # already active: same port/cred, no second container
        self.assertEqual((port1, cred1), (port2, cred2))
        self.assertTrue(app.VM_RUNS[rid]["terminal"]["active"])
        app.stop_terminal(rid)
        self.assertFalse(app.VM_RUNS[rid]["terminal"]["active"])
        self.assertNotIn(rid, app.VM_TERM_CREDS)

    def test_running_terminal_whose_credential_was_lost_gets_a_fresh_one(self):
        """Regression: credentials live in memory only, so after a panel restart a still-running terminal came back
        with an empty credential and the browser's login prompt could never be answered."""
        rid = app.create_vm_run({})
        self.assertTrue(self.finished(rid))
        (app.vm_run_dir(rid)).mkdir(parents=True, exist_ok=True)
        (app.vm_run_dir(rid) / "id_ed25519").write_text("k")
        app.start_terminal(rid)
        app.VM_TERM_CREDS.clear()                               # what a panel restart leaves behind
        with mock.patch.object(app, "stop_terminal", wraps=app.stop_terminal) as stop:
            port, cred = app.start_terminal(rid)
        stop.assert_called_once_with(rid, quiet=True)           # the old, unusable terminal is replaced
        self.assertEqual(len(cred), 32)
        self.assertEqual(app.VM_TERM_CREDS[rid], cred)
        self.assertTrue(app.VM_RUNS[rid]["terminal"]["active"])

    def test_terminal_image_has_ssh_and_never_hands_ssh_the_mounted_key(self):
        """Regression: the terminal ran ttyd's official image, which has no ssh client ("execvp failed" in the
        browser), and pointed ssh at the bind-mounted key, which Docker Desktop on Windows mounts 0777 and ssh
        then refuses. Checked end to end against a real VM when fixed; this guards the compose file's shape."""
        tpl = (app.TPL / "vm-terminal.compose.yml").read_text()
        self.assertIn("apk add --no-cache openssh-client", tpl)
        self.assertNotIn("image: tsl0922/ttyd", tpl)
        self.assertIn("install -m 600 /keys/id_ed25519 /tmp/id_ed25519", tpl)
        self.assertIn('"-i", "/tmp/id_ed25519"', tpl)
        self.assertNotIn('"-i", "/keys/id_ed25519"', tpl)

    def test_stop_passes_env_so_compose_down_can_parse_the_file(self):
        """Regression: stop_terminal used to call `docker compose down` without the env vars the compose file's
        ${VAR} interpolation needs, so compose failed to even parse the file (empty volume/port spec) and the
        container was never actually stopped, while the code still reported success."""
        rid = app.create_vm_run({})
        self.assertTrue(self.finished(rid))
        app.vm_run_dir(rid).mkdir(parents=True, exist_ok=True)
        (app.vm_run_dir(rid) / "id_ed25519").write_text("k")
        app.start_terminal(rid)
        self.calls.clear()
        app.stop_terminal(rid)
        run_calls = [c for c in self.calls if c[0] == "run"]
        self.assertTrue(run_calls, "stop_terminal must actually invoke docker compose down")

    def test_delete_stops_an_active_terminal_first(self):
        """Regression: delete_vm_run used to remove the run record without stopping an active terminal container,
        leaking it forever (no longer visible in the panel, but still running and bound to a host port)."""
        rid = app.create_vm_run({})
        self.assertTrue(self.finished(rid))
        app.vm_run_dir(rid).mkdir(parents=True, exist_ok=True)
        (app.vm_run_dir(rid) / "id_ed25519").write_text("k")
        app.start_terminal(rid)
        self.assertTrue(app.VM_RUNS[rid]["terminal"]["active"])
        self.calls.clear()
        app.delete_vm_run(rid)
        self.assertTrue(any(c[0] == "run" for c in self.calls), "deleting a run with an active terminal must stop it")

    def test_credential_is_never_persisted_to_disk(self):
        rid = app.create_vm_run({})
        self.assertTrue(self.finished(rid))
        app.vm_run_dir(rid).mkdir(parents=True, exist_ok=True)
        (app.vm_run_dir(rid) / "id_ed25519").write_text("k")
        _, cred = app.start_terminal(rid)
        on_disk = app.vm_run_path(rid).read_text()
        self.assertNotIn(cred, on_disk)


class AgentDeletionTests(VmBenchBase):
    def test_deleting_an_agent_stops_its_live_vm_runs(self):
        self.add_agent("alpha")
        with mock.patch.object(vr, "ssh_wait", lambda *a, **k: (time.sleep(2), True)[1]):
            rid = app.create_vm_run({"task_id": "fizzbuzz-cli", "agent": "alpha"})
            self.assertTrue(wait_for(lambda: app.VM_RUNS[rid]["state"] in ("provisioning", "ready", "working")))
            app.stop_vm_runs_for("alpha")
            # Not self.finished()/VM_LIVE_STATES here: for an agent-attached run "ready" is only ever a momentary
            # transit state on the way to "working", never a resting one, so treating it as "finished" is racy
            # (the thread briefly sets state="ready" and does a real file write via save_vm_run() BEFORE its very
            # next line re-checks stopped() and flips to "stopped" - on Windows that file write occasionally takes
            # long enough for a poll to land in between). Wait for the actual terminal state instead.
            self.assertTrue(wait_for(lambda: app.VM_RUNS[rid]["state"] == "stopped", timeout=8))


class RestartRecoveryTests(VmBenchBase):
    def test_live_run_on_disk_becomes_interrupted_on_load(self):
        rid = "abcdef12"
        rec = {"id": rid, "vm_name": "x", "state": "working", "reason": "", "keep": False, "memory_mb": 1024,
               "cpus": 1, "created": time.time(), "started": time.time(), "ended": None, "agent": "alpha",
               "task_id": "fizzbuzz-cli", "task_title": "t", "chat": "c", "score": None, "benchmark_id": None,
               "ssh_port": 62200, "vm_log": "", "terminal": {"active": False, "port": None}}
        app.vm_run_path(rid).write_text(json.dumps(rec))
        app.load_vm_runs()
        self.assertEqual(app.VM_RUNS[rid]["state"], "interrupted")


if __name__ == "__main__":
    unittest.main()
