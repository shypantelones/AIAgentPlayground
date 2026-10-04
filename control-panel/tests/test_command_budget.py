"""Tests for the command budget: what a dollar budget buys on each model, the budget a lab gets at creation, the
allowance the wrapper enforces, and how local agents are left out. Nothing here calls a model or a VM.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import shutil, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_costs as ac  # noqa: E402
import app  # noqa: E402
import vm_runner as vr  # noqa: E402

HAIKU = "anthropic/claude-haiku-4-5"
OPUS = "anthropic/claude-opus-5-5"


class CostTests(unittest.TestCase):
    def test_a_bigger_model_buys_fewer_commands_for_the_same_budget(self):
        self.assertGreater(ac.commands_for_budget(1.0, HAIKU), ac.commands_for_budget(1.0, OPUS))

    def test_a_budget_that_buys_too_few_commands_says_what_to_change(self):
        with self.assertRaisesRegex(ValueError, "raise the budget or use a cheaper model"):
            ac.commands_for_budget(0.01, OPUS)

    def test_an_unpriced_cloud_model_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no price is known"):
            ac.commands_for_budget(1.0, "anthropic/some-new-model")


class LabBudgetTests(unittest.TestCase):
    def test_local_agents_are_not_budgeted(self):
        self.assertIsNone(app.lab_budget({"budget_usd": 0.5}, [("alpha", "ollama/qwen3:14b")]))

    def test_each_paid_agent_gets_its_own_allowance(self):
        b = app.lab_budget({"budget_usd": 0.5}, [("alpha", HAIKU), ("beta", OPUS), ("gamma", "ollama/qwen3:4b")])
        self.assertEqual(b["commands"], {"alpha": ac.commands_for_budget(0.5, HAIKU),
                                         "beta": ac.commands_for_budget(0.5, OPUS)})

    def test_budget_must_be_a_positive_amount_within_the_cap(self):
        for bad in ["0", "-1", "21", "lots"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.lab_budget({"budget_usd": bad}, [("alpha", HAIKU)])

    def test_left_is_the_allowance_minus_what_was_used(self):
        r = {"budget": {"usd": 1, "commands": {"alpha": 40}, "used": {"alpha": 15}}}
        self.assertEqual(app.budget_left(r, "alpha"), 25)
        self.assertIsNone(app.budget_left(r, "gamma"))           # not budgeted
        self.assertIsNone(app.budget_left({}, "alpha"))          # no budget on the lab
        r["budget"]["used"]["alpha"] = 99
        self.assertEqual(app.budget_left(r, "alpha"), 0)         # never negative


class PerTurnCapTests(unittest.TestCase):
    def setUp(self):
        # topo_agent_turn saves the run record: keep it out of the real data folder
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "topo-runs").mkdir()
        self.patch = mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_per_turn_cap_defaults_and_is_recorded(self):
        self.assertEqual(app.lab_budget({"budget_usd": 0.5}, [("alpha", HAIKU)])["per_turn"], app.TOPO_COMMAND_CAP_DEFAULT)
        self.assertEqual(app.lab_budget({"budget_usd": 0.5, "command_cap": "12"}, [("alpha", HAIKU)])["per_turn"], 12)

    def test_the_cap_must_be_within_range(self):
        for bad in ["4", "201", "many"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.lab_budget({"budget_usd": 0.5, "command_cap": bad}, [("alpha", HAIKU)])

    def test_a_turn_gets_the_lower_of_the_budget_left_and_the_cap(self):
        captured = []
        r = {"id": "c1", "agent": "alpha", "chat": "c", "nodes": {}, "agent_turns": 0,
             "budget": {"usd": 1, "per_turn": 10, "commands": {"alpha": 100}, "used": {}}}
        with mock.patch.object(app, "dc", side_effect=lambda *a, **k: captured.append(a[-1]) or (0, "", "")), \
                mock.patch.object(app, "read_agent_log", return_value=""), \
                mock.patch.object(app, "counted_agent_turn", side_effect=lambda r_, m, log, turn: {"ok": True}), \
                mock.patch.object(app, "snapshot_lab_vms", return_value=True), \
                mock.patch.object(app, "take_topo_snapshot", return_value={"id": "s"}):
            app.topo_agent_turn(r, "go")
        self.assertIn(f"echo 10 > {app.TOPO_BUDGET_FILE}", captured[0])


class ModelPriceListTests(unittest.TestCase):
    def test_every_cloud_model_says_what_fifty_cents_buys(self):
        models = app.cloud_models_with_cost()["anthropic"]
        by_id = {m["id"]: m["commands_per_50c"] for m in models}
        self.assertEqual(by_id["claude-haiku-4-5"], ac.commands_for_budget(0.5, HAIKU))
        self.assertLess(by_id["claude-opus-5-5"], by_id["claude-haiku-4-5"])


class WrapperBudgetRaceTests(unittest.TestCase):
    @unittest.skipIf(sys.platform.startswith("win"), "runs the wrapper with sh, flock and a fake ssh")
    def test_parallel_commands_spend_exactly_the_budget(self):
        import os, subprocess
        tmp = Path(tempfile.mkdtemp())
        try:
            b = tmp / "bin"; b.mkdir()
            (b / "ssh").write_text("#!/bin/sh\ncat >/dev/null 2>&1\necho ran\n"); (b / "ssh").chmod(0o755)
            budget = tmp / "budget"; budget.write_text("5")
            w = tmp / "vmrun-h1"
            w.write_text(vr.vmrun_script("/k", 1, "h", str(tmp / "log"), node="h1", budget_file=str(budget)))
            env = dict(os.environ, PATH=f"{b}{os.pathsep}{os.environ['PATH']}")
            procs = [subprocess.Popen(["sh", str(w), f"echo {i}"], env=env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(20)]
            codes = [p.wait() for p in procs]
            self.assertEqual(codes.count(0), 5, codes)
            self.assertEqual(codes.count(3), 15, codes)
            self.assertEqual(budget.read_text().strip(), "0")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class WrapperBudgetTests(unittest.TestCase):
    def test_the_wrapper_takes_one_from_the_budget_and_refuses_at_zero(self):
        self.assertIn('echo $((n - 1)) > "/w/.vmrun-budget.tmp" && mv "/w/.vmrun-budget.tmp" "/w/.vmrun-budget"',
                      vr.vmrun_script("/k", 1, "h", "/l", node="h1", budget_file="/w/.vmrun-budget"))
        self.assertIn("flock 9", vr.vmrun_script("/k", 1, "h", "/l", node="h1", budget_file="/w/.vmrun-budget"))
        self.assertIn('"$n" -le 0', vr.vmrun_script("/k", 1, "h", "/l", node="h1", budget_file="/w/.vmrun-budget"))

    def test_a_wrapper_without_a_budget_file_is_unchanged(self):
        self.assertNotIn("budget", vr.vmrun_script("/k", 1, "h", "/l", node="h1"))

    @unittest.skipIf(sys.platform.startswith("win"), "runs the wrapper with sh and a fake ssh")
    def test_the_real_wrapper_refuses_once_the_budget_is_spent(self):
        import os, subprocess
        tmp = Path(tempfile.mkdtemp())
        try:
            bindir = tmp / "bin"; bindir.mkdir()
            (bindir / "ssh").write_text("#!/bin/sh\necho ran\n"); (bindir / "ssh").chmod(0o755)
            budget = tmp / "budget"; budget.write_text("1")
            w = tmp / "vmrun-h1"
            w.write_text(vr.vmrun_script("/k", 1, "h", str(tmp / "log"), node="h1", budget_file=str(budget)))
            env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
            first = subprocess.run(["sh", str(w), "uptime"], env=env, capture_output=True, text=True)
            second = subprocess.run(["sh", str(w), "uptime"], env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0)
            self.assertEqual(budget.read_text().strip(), "0")
            self.assertEqual(second.returncode, 3)
            self.assertIn("command budget used up", second.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CreateBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "topo-runs").mkdir()
        self.patches = [mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs"),
                        mock.patch.object(app, "load_meta", return_value={}),
                        mock.patch.object(app, "agent_running", return_value=True),
                        mock.patch.object(app, "agent_model_id", return_value=HAIKU)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        app.TOPO_RUNS.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_lab_with_a_paid_agent_gets_a_budget_record(self):
        with mock.patch.object(app.threading, "Thread"):
            rid = app.create_topo_run({"topology_id": "s1h2", "custom_prompt": "go", "agent": "alpha", "budget_usd": "0.50"})
        self.assertEqual(app.TOPO_RUNS[rid]["budget"]["commands"]["alpha"], ac.commands_for_budget(0.5, HAIKU))

    def test_a_lab_with_a_too_small_budget_is_refused_before_anything_is_built(self):
        with mock.patch.object(app.threading, "Thread") as thread:
            with self.assertRaisesRegex(ValueError, "raise the budget"):
                app.create_topo_run({"topology_id": "s1h2", "custom_prompt": "go", "agent": "alpha", "budget_usd": "0.01"})
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
