"""Tests for plan-first labs: the agent writes a plan with no access to the nodes, and only an approved plan lets it act
(app.write_plan / topo_approve_plan / topo_agent_phase). Docker, SSH and the agent are mocked.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import shutil, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import vm_runner as vr  # noqa: E402


class PlanBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs")
        self.patch.start()
        self.rid = "plan1234"
        self.run = {"id": self.rid, "state": "ready", "agent": "alpha", "agent_done": True, "chat": "vmtopo-plan1234",
                    "agent_turns": 0, "plan_first": True, "plan": "1. look at h1", "plan_approved": False,
                    "custom_prompt": "make h1 reach h2", "task_id": None, "interactive": False,
                    "nodes": {"h1": {"role": "host", "ssh_port": 2201}, "h2": {"role": "host", "ssh_port": 2202}},
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


class PlanTurnTests(PlanBase):
    def test_the_plan_is_kept_and_the_lab_waits(self):
        self.run.update(plan=None, state="queued")
        with mock.patch.object(app, "topo_agent_turn", return_value={"ok": True, "reply": "  1. ip -br a on h1  "}):
            self.assertEqual(app.write_plan(self.run, None), app.PLAN_WAITING)
        self.assertEqual(self.run["plan"], "1. ip -br a on h1")

    def test_no_plan_back_is_a_failure_with_a_message(self):
        with mock.patch.object(app, "topo_agent_turn", return_value={"ok": False, "reply": ""}):
            self.assertEqual(app.write_plan(self.run, None), "the agent didn't return a plan (see the transcript)")

    def test_the_agent_has_no_access_during_the_plan_turn(self):
        # Nothing reaches the nodes until approval: no key is copied into the agent, no relay or vmrun wrapper is set up.
        self.run.update(plan=None)
        priv = app.topo_run_dir(self.rid) / "id_ed25519"
        topology = vr.get_topology("s1h2")
        with mock.patch.object(app, "topo_agent_turn", return_value={"ok": True, "reply": "the plan"}), \
                mock.patch.object(app, "dc") as dc, mock.patch.object(app, "run") as docker, \
                mock.patch.object(app.vr, "vmrun_script") as wrapper, mock.patch.object(app.vr, "relay_command") as relay:
            result = app.topo_agent_phase(self.run, topology, {"h1": 2201, "h2": 2202, "sw1": 2203}, priv, None, lambda: False)
        self.assertEqual(result, app.PLAN_WAITING)
        dc.assert_not_called()
        docker.assert_not_called()
        wrapper.assert_not_called()
        relay.assert_not_called()

    def test_the_plan_prompt_asks_for_a_plan_only(self):
        with mock.patch.object(app, "topo_agent_turn", return_value={"ok": True, "reply": "p"}) as turn:
            app.write_plan(self.run, None)
        prompt = turn.call_args.args[1]
        self.assertIn("make h1 reach h2", prompt)
        self.assertIn("- h1 (host)", prompt)
        self.assertIn("Reply with the plan only", prompt)


class ApprovalTests(PlanBase):
    def test_approval_attaches_the_agent_to_apply_the_plan(self):
        with mock.patch.object(app, "attach_agent_to_lab") as attach:
            app.topo_approve_plan(self.rid, {"interactive": True})
        self.assertTrue(self.run["plan_approved"])
        self.assertEqual(self.run["pending_opening"], app.PLAN_OPENING)
        attach.assert_called_once()
        args = attach.call_args.args[1]
        self.assertEqual((args["agent"], args["custom_prompt"], args["use_task"], args["interactive"]),
                         ("alpha", "make h1 reach h2", False, True))

    def test_a_failed_attach_leaves_the_plan_waiting(self):
        with mock.patch.object(app, "attach_agent_to_lab", side_effect=ValueError("alpha is not running")):
            with self.assertRaisesRegex(ValueError, "not running"):
                app.topo_approve_plan(self.rid, {})
        self.assertFalse(self.run["plan_approved"])
        self.assertNotIn("pending_opening", self.run)

    def test_only_a_waiting_plan_can_be_approved(self):
        for patch, msg in [(dict(plan=None), "no plan waiting"), (dict(plan_approved=True), "no plan waiting"),
                           (dict(state="working"), "no plan waiting"), (dict(plan_first=False), "no plan waiting")]:
            with self.subTest(patch=patch):
                saved = dict(self.run)
                self.run.update(patch)
                try:
                    with self.assertRaisesRegex(ValueError, msg):
                        app.topo_approve_plan(self.rid, {})
                finally:
                    self.run.clear()
                    self.run.update(saved)

    def test_attach_is_refused_until_the_plan_is_approved(self):
        with self.assertRaisesRegex(ValueError, "approve the lab's plan first"):
            app.attach_agent_to_lab(self.rid, {"agent": "alpha", "custom_prompt": "x"})

    def test_the_approved_opening_replaces_the_task_prompt_for_one_turn(self):
        # The apply turn opens with the approval, not the task prompt; the lab's chat already holds the plan.
        self.run.update(plan_approved=True, pending_opening=app.PLAN_OPENING, interactive=False)
        priv = app.topo_run_dir(self.rid) / "id_ed25519"
        topology = vr.get_topology("s1h2")
        with mock.patch.object(app, "topo_agent_turn", return_value={"ok": True, "reply": "done"}) as turn, \
                mock.patch.object(app, "dc", return_value=(0, "", "")), \
                mock.patch.object(app, "run", return_value=(0, "", "")), \
                mock.patch.object(app, "detach_topo_agent"), \
                mock.patch.object(app.vr, "relay_command", return_value="cmd"):
            app.topo_agent_phase(self.run, topology, {"h1": 2201, "h2": 2202, "sw1": 2203}, priv, None, lambda: False)
        prompt = turn.call_args.args[1]
        self.assertTrue(prompt.startswith(app.PLAN_OPENING))
        self.assertNotIn("make h1 reach h2", prompt)
        self.assertNotIn("pending_opening", self.run)


class CreateTests(unittest.TestCase):
    def test_a_plan_needs_an_agent(self):
        with mock.patch.object(app.threading, "Thread") as thread:
            with self.assertRaisesRegex(ValueError, "needs an agent"):
                app.create_topo_run({"topology_id": "s1h2", "plan_first": True, "custom_prompt": "x"})
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
