"""Tests for agent creation with the backend chosen up front (local Ollama or a cloud model). No Docker needed.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import json, shutil, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402

KEY = "sk-ant-TESTKEY-1234567890abcdef"


class FakeStore:
    def __init__(self):
        self.d = {}

    def put(self, name, token):
        self.d[name] = token

    def get(self, name):
        return self.d.get(name)

    def exists(self, name):
        return name in self.d

    def delete(self, name):
        self.d.pop(name, None)


class CreateAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = FakeStore()
        self.calls = []                                     # ("dc", args, input) / ("ensure_shared",) / ...
        self.patches = [
            mock.patch.object(app, "DATA", self.tmp),
            mock.patch.object(app, "JOBS", {}),
            mock.patch.object(app, "STORE", self.store),
            mock.patch.object(app, "free_port", lambda: 18801),
            mock.patch.object(app, "dc", lambda name, *a, input=None, timeout=0: self.calls.append(("dc", a, input)) or (0, "", "")),
            mock.patch.object(app, "ensure_shared", lambda log=None: self.calls.append(("ensure_shared",))),
            mock.patch.object(app, "ensure_llm_network", lambda log=None: self.calls.append(("ensure_llm_network",))),
            mock.patch.object(app, "ensure_image", lambda log=None, image=None: None),
            mock.patch.object(app, "wait_gateway", lambda *a, **k: True),
            mock.patch.object(app, "default_local_model", side_effect=AssertionError("the user picks the model, not the panel")),
            mock.patch.object(app, "ollama_list", lambda: self.installed),
            mock.patch.object(app, "pull_model", lambda model, log: self.calls.append(("pull", model))),
        ]
        self.installed = [("llama3.1:8b", 4.9)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_create(self, name, form):
        jid = app.create_agent(name, form)
        end = time.time() + 5
        while not app.JOBS[jid]["done"] and time.time() < end:
            time.sleep(0.02)
        return app.JOBS[jid]

    def meta(self, name):
        return json.loads((self.tmp / name / "meta.json").read_text())

    def kinds(self):
        return [c[0] for c in self.calls]

    def patch_payload(self):
        return next(i for k, a, i in (c for c in self.calls if c[0] == "dc") if "patch" in a)

    # ---- cloud
    def test_cloud_agent_is_configured_before_anything_is_built_and_needs_no_ollama(self):
        j = self.run_create("cloudy", {"backend": "cloud", "provider": "anthropic", "model": "claude-opus-5-5", "rate": 20, "token": KEY})
        self.assertTrue(j["ok"], j["lines"])
        m = self.meta("cloudy")
        self.assertEqual((m["backend"], m["provider"], m["cloud_model"], m["rate"]), ("cloud", "anthropic", "claude-opus-5-5", 20))
        self.assertNotIn("model", m)                                  # no local model chosen or downloaded
        self.assertEqual(self.store.get("cloudy"), KEY)
        self.assertTrue((self.tmp / "cloudy" / "relay" / "default.conf.template").exists())
        self.assertIn("ensure_llm_network", self.kinds())
        self.assertNotIn("ensure_shared", self.kinds())               # the local model server is never started
        payload = self.patch_payload()
        self.assertIn('primary: "anthropic/claude-opus-5-5"', payload)
        self.assertIn("http://cloud-relay:8080", payload)
        self.assertNotIn(KEY, payload)                                # the agent only ever gets a placeholder

    def test_cloud_without_a_key_is_refused_up_front(self):
        with self.assertRaises(ValueError):
            app.create_agent("cloudy", {"backend": "cloud", "provider": "anthropic", "model": "claude-opus-5-5"})
        self.assertFalse((self.tmp / "cloudy").exists())

    def test_bad_cloud_settings_are_refused_before_the_key_is_stored(self):
        for form in ({"backend": "cloud", "provider": "nope", "model": "m", "token": KEY},
                     {"backend": "cloud", "provider": "anthropic", "model": "", "token": KEY},
                     {"backend": "cloud", "provider": "anthropic", "model": "claude-opus-5-5", "rate": 0, "token": KEY},
                     {"backend": "cloud", "provider": "openai-compatible", "model": "m", "upstream": "http://10.0.0.1", "token": KEY}):
            with self.subTest(form=form), self.assertRaises(ValueError):
                app.create_agent("cloudy", form)
        self.assertEqual(self.store.d, {})
        self.assertFalse((self.tmp / "cloudy").exists())

    def test_malformed_key_is_refused(self):
        with self.assertRaises(ValueError):
            app.create_agent("cloudy", {"backend": "cloud", "provider": "anthropic", "model": "claude-opus-5-5", "token": "short"})
        self.assertFalse((self.tmp / "cloudy").exists())

    # ---- local: the user must choose the model
    def test_local_is_still_the_default_backend(self):
        j = self.run_create("loco", {"model": "llama3.1:8b"})
        self.assertTrue(j["ok"], j["lines"])
        m = self.meta("loco")
        self.assertEqual((m["backend"], m["model"]), ("local", "llama3.1:8b"))
        self.assertIn("ensure_shared", self.kinds())
        self.assertNotIn("ensure_llm_network", self.kinds())
        self.assertIn('primary: "ollama/llama3.1:8b"', self.patch_payload())
        self.assertEqual(self.store.d, {})

    def test_local_without_a_model_is_refused_up_front(self):
        # regression: the panel used to pick a model silently (qwen3:32b, never downloaded -> 404 on first chat)
        for form in (None, {}, {"backend": "local"}, {"backend": "local", "model": "  "}):
            with self.subTest(form=form), self.assertRaises(ValueError):
                app.create_agent("loco", form)
        self.assertFalse((self.tmp / "loco").exists())

    def test_bad_model_name_is_refused(self):
        with self.assertRaises(ValueError):
            app.create_agent("loco", {"backend": "local", "model": "x; rm -rf /"})
        self.assertFalse((self.tmp / "loco").exists())

    def test_downloaded_model_is_not_pulled_again(self):
        self.assertTrue(self.run_create("loco", {"model": "llama3.1:8b"})["ok"])
        self.assertNotIn("pull", self.kinds())

    def test_chosen_model_is_downloaded_before_the_agent_uses_it(self):
        self.installed = [("qwen3:14b", 9.3)]
        j = self.run_create("loco", {"model": "qwen2.5:14b"})
        self.assertTrue(j["ok"], j["lines"])
        self.assertLess(self.kinds().index("ensure_shared"), self.calls.index(("pull", "qwen2.5:14b")))
        self.assertLess(self.calls.index(("pull", "qwen2.5:14b")),
                        next(i for i, c in enumerate(self.calls) if c[0] == "dc" and "patch" in c[1]))

    def test_unknown_backend_is_refused(self):
        with self.assertRaises(ValueError):
            app.create_agent("x", {"backend": "gpu-farm"})


class CloudModelListTests(unittest.TestCase):
    def test_claude_dropdown_ids_are_valid_and_default_to_opus(self):
        ids = [m["id"] for m in app.CLOUD_MODELS["anthropic"]]
        self.assertEqual(ids[0], "claude-opus-5-5")
        self.assertEqual(len(ids), len(set(ids)))
        for i in ids:
            self.assertRegex(i, app.MODEL_RE)


class LlmNetworkTests(unittest.TestCase):
    def test_existing_network_is_left_alone(self):
        with mock.patch.object(app, "run", return_value=(0, "[]", "")), \
             mock.patch.object(app, "shared", side_effect=AssertionError("must not touch the model server")):
            app.ensure_llm_network(lambda s: None)

    def test_missing_network_is_created_without_starting_the_model_server(self):
        with mock.patch.object(app, "run", return_value=(1, "", "No such network")), \
             mock.patch.object(app, "shared", return_value=(0, "", "")) as shared:
            app.ensure_llm_network(lambda s: None)
        shared.assert_called_once_with("up", "--no-start")


if __name__ == "__main__":
    unittest.main()
