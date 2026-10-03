"""Tests for agent job safety and first-run UX: one lifecycle job per agent, a default model that fits the machine,
and the image download shown as progress. No Docker needed.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import os, sys, threading, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402


def wait_done(jid, timeout=5):
    ev = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if app.JOBS[jid]["done"]:
            return True
        ev.wait(0.02)
    return False


class AgentJobLockTests(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(app, "JOBS", {})
        self.patch.start()
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        self.patch.stop()

    def blocking(self, log):
        self.release.wait(5)

    def test_second_job_for_same_agent_is_refused(self):
        app.start_job("Create agent alpha", self.blocking, agent="alpha")
        with self.assertRaises(ValueError) as cm:
            app.start_job("Switch alpha to local", lambda log: None, agent="alpha")
        self.assertIn("Create agent alpha", str(cm.exception))

    def test_other_agents_and_agentless_jobs_still_run(self):
        app.start_job("Create agent alpha", self.blocking, agent="alpha")
        app.start_job("Create agent beta", lambda log: None, agent="beta")
        app.start_job("Pull llama3.1:8b", lambda log: None)

    def test_agent_is_free_again_after_its_job_finishes(self):
        first = app.start_job("Create agent alpha", lambda log: None, agent="alpha")
        self.assertTrue(wait_done(first))
        app.start_job("Switch alpha to local", lambda log: None, agent="alpha")

    def test_chat_is_refused_while_a_model_switch_runs(self):
        # regression: the switch's gateway restart killed the chat mid-turn (exit 137 / "OCI runtime exec failed")
        app.start_job("Switch alpha to local", self.blocking, agent="alpha")
        with self.assertRaises(ValueError) as cm:
            app.start_job("Chat with alpha", lambda log: None, agent="alpha", exclusive=False)
        self.assertIn("Switch alpha to local", str(cm.exception))

    def test_model_switch_is_refused_while_a_chat_runs(self):
        app.start_job("Chat with alpha", self.blocking, agent="alpha", exclusive=False)
        with self.assertRaises(ValueError):
            app.start_job("Switch alpha to local", lambda log: None, agent="alpha")

    def test_chats_can_run_side_by_side(self):
        app.start_job("Chat with alpha", self.blocking, agent="alpha", exclusive=False)
        app.start_job("Forward beta -> alpha", lambda log: None, agent="alpha", exclusive=False)

    def test_failed_job_also_frees_the_agent(self):
        first = app.start_job("Create agent alpha", lambda log: 1 / 0, agent="alpha")
        self.assertTrue(wait_done(first))
        self.assertFalse(app.JOBS[first]["ok"])
        app.start_job("Switch alpha to local", lambda log: None, agent="alpha")


def entry(name, need, fit, tc="untested", tools=True):
    return {"name": name, "need_gb": need, "fit": fit, "openclaw_tool_calling": tc, "tools": tools, "embedding_only": False}


class DefaultModelTests(unittest.TestCase):
    def pick(self, installed=(), catalog=()):
        ov = {"installed": list(installed), "catalog": list(catalog)}
        with mock.patch.dict(os.environ, {"OPENCLAW_MODEL": ""}), mock.patch.object(app, "models_overview", lambda: ov):
            return app.default_local_model()

    def test_intel_mac_16gb_gets_a_model_that_fits_not_qwen3_14b(self):
        # roughly what the panel reports on a 16 GB Intel Mac
        cat = [entry("qwen3:1.7b", 5.5, "cpu", "verified_broken"), entry("qwen2.5:7b", 6.9, "cpu"),
               entry("llama3.1:8b", 9.5, "cpu"), entry("qwen3:8b", 10.3, "toobig"), entry("qwen3:14b", 15.0, "toobig")]
        self.assertEqual(self.pick(catalog=cat), "llama3.1:8b")

    def test_downloaded_models_are_preferred(self):
        self.assertEqual(self.pick(installed=[entry("qwen2.5:7b", 6.9, "cpu")],
                                   catalog=[entry("llama3.1:8b", 9.5, "cpu")]), "qwen2.5:7b")

    def test_models_without_working_tool_calls_are_skipped(self):
        self.assertEqual(self.pick(installed=[entry("qwen3:4b", 7.6, "gpu", "verified_broken"),
                                              entry("embed", 1.0, "gpu", tools=False)],
                                   catalog=[entry("llama3.2:3b", 6.1, "gpu")]), "llama3.2:3b")

    def test_tight_or_too_big_models_are_skipped(self):
        self.assertEqual(self.pick(catalog=[entry("big", 12.8, "tight"), entry("small", 6.1, "cpu")]), "small")

    def test_full_gpu_fit_beats_a_bigger_model_that_spills_to_cpu(self):
        # regression: a 16 GB GPU got qwen3:32b (partial, ~29 GB) over qwen2.5:14b (fits the GPU)
        cat = [entry("qwen2.5:14b", 15.7, "gpu"), entry("gpt-oss:20b", 15.9, "partial"), entry("qwen3:32b", 28.9, "partial")]
        self.assertEqual(self.pick(catalog=cat), "qwen2.5:14b")

    def test_partial_fit_is_used_when_nothing_fits_the_gpu(self):
        self.assertEqual(self.pick(catalog=[entry("big", 28.9, "partial"), entry("small", 6.1, "cpu")]), "big")

    def test_downloaded_model_still_beats_a_catalog_gpu_fit(self):
        self.assertEqual(self.pick(installed=[entry("have", 20.0, "partial")], catalog=[entry("get", 9.5, "gpu")]), "have")

    def test_falls_back_when_nothing_fits_or_ollama_is_unreachable(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_MODEL": ""}):
            self.assertEqual(self.pick(catalog=[entry("big", 99, "toobig")]), app.DEFAULT_MODEL)
            with mock.patch.object(app, "models_overview", side_effect=OSError("down")):
                self.assertEqual(app.default_local_model(), app.DEFAULT_MODEL)

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_MODEL": "custom:1b"}), mock.patch.object(app, "DEFAULT_MODEL", "custom:1b"), \
             mock.patch.object(app, "models_overview", side_effect=AssertionError("should not be consulted")):
            self.assertEqual(app.default_local_model(), "custom:1b")


class FakePull:
    def __init__(self, lines, rc=0):
        self.stdout, self.rc = iter(lines), rc

    def wait(self):
        return self.rc


PULL_OUTPUT = ["latest: Pulling from openclaw/openclaw\n", "aaa: Pulling fs layer\n", "bbb: Pulling fs layer\n",
               "aaa: Download complete\n", "aaa: Pull complete\n", "bbb: Pull complete\n",
               "Status: Downloaded newer image for ghcr.io/openclaw/openclaw:latest\n"]


class EnsureImageTests(unittest.TestCase):
    def setUp(self):
        self.lines = []

    def test_present_image_is_not_pulled(self):
        with mock.patch.object(app, "run", return_value=(0, "[]", "")), \
             mock.patch.object(app.subprocess, "Popen", side_effect=AssertionError("should not pull")):
            app.ensure_image(self.lines.append)
        self.assertEqual(self.lines, [])

    def test_missing_image_is_pulled_with_progress(self):
        with mock.patch.object(app, "run", return_value=(1, "", "No such image")), \
             mock.patch.object(app.subprocess, "Popen", return_value=FakePull(PULL_OUTPUT)) as popen:
            app.ensure_image(self.lines.append)
        self.assertEqual(popen.call_args[0][0][1:], ["pull", app.IMAGE])
        self.assertIn("  1/2 layers downloaded", self.lines)
        self.assertIn("  2/2 layers downloaded", self.lines)
        self.assertTrue(self.lines[-1].startswith("Status: Downloaded"))

    def test_failed_pull_raises(self):
        with mock.patch.object(app, "run", return_value=(1, "", "No such image")), \
             mock.patch.object(app.subprocess, "Popen", return_value=FakePull(["Error response from daemon: denied\n"], rc=1)):
            with self.assertRaises(RuntimeError):
                app.ensure_image(self.lines.append)
        self.assertIn("Error response from daemon: denied", self.lines)


if __name__ == "__main__":
    unittest.main()
