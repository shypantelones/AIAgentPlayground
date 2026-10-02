"""Tests for the per-agent compose .env (rebuilt from meta.json when missing or from another machine). No Docker needed.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import json, shutil, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402

META = {"name": "alpha", "port": 18801, "token": "t0ken", "model": "qwen2.5:7b", "backend": "local"}


class AgentEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(app, "DATA", self.tmp / "instances")
        self.patch.start()
        self.d = app.DATA / "alpha"
        self.d.mkdir(parents=True)
        (self.d / "meta.json").write_text(json.dumps(META))

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env_lines(self):
        return (self.d / ".env").read_text().splitlines()

    def test_missing_env_is_rebuilt_from_meta(self):
        # e.g. data/ restored from a backup or checked out of git: meta.json is there, .env is not
        p = app.env_file("alpha")
        self.assertEqual(p, self.d / ".env")
        self.assertEqual(self.env_lines(), [
            "HOST_PORT=18801", "OPENCLAW_GATEWAY_TOKEN=t0ken",
            f"OPENCLAW_IMAGE={app.IMAGE}", f"INSTANCE_DIR={self.d.as_posix()}"])

    def test_env_from_another_machine_is_rebuilt(self):
        (self.d / ".env").write_text("HOST_PORT=18801\nOPENCLAW_GATEWAY_TOKEN=old\nOPENCLAW_IMAGE=x\n"
                                     "INSTANCE_DIR=C:/Users/someone/openclaw-playground/control-panel/data/instances/alpha\n")
        app.env_file("alpha")
        self.assertIn(f"INSTANCE_DIR={self.d.as_posix()}", self.env_lines())
        self.assertIn("OPENCLAW_GATEWAY_TOKEN=t0ken", self.env_lines())

    def test_current_env_is_left_alone(self):
        text = f"HOST_PORT=18801\nOPENCLAW_GATEWAY_TOKEN=t0ken\nOPENCLAW_IMAGE=custom:tag\nINSTANCE_DIR={self.d.as_posix()}\n"
        (self.d / ".env").write_text(text)
        app.env_file("alpha")
        self.assertEqual((self.d / ".env").read_text(), text)

    def test_unknown_agent_is_rejected(self):
        with self.assertRaises(KeyError):
            app.env_file("nobody")

    def test_dc_works_without_an_env_file(self):
        """Regression: switching an imported agent's model failed with "couldn't find env file"."""
        calls = []
        with mock.patch.object(app, "run", lambda args, **k: calls.append(args) or (0, "", "")):
            app.dc("alpha", "up", "-d")
        args = calls[0]
        self.assertEqual(args[args.index("--env-file") + 1], str(self.d / ".env"))
        self.assertTrue((self.d / ".env").exists())


class OllamaHostnameTests(unittest.TestCase):
    """Regression: a host-mode Ollama (macOS) rejects Host: ollama:11434 with 403 (its DNS-rebinding guard only
    trusts localhost, IPs and *.local/*.localhost/*.internal), so agents failed with "authentication failed, HTTP 403"."""
    def test_agents_use_a_hostname_ollama_accepts(self):
        self.assertIn('baseUrl: "http://ollama.internal:11434"', app.model_patch({"backend": "local", "model": "m"}))

    def test_relay_answers_to_that_name_and_it_bypasses_the_egress_proxy(self):
        compose = (app.TPL / "instance.compose.yml").read_text()
        self.assertIn("aliases: [ollama, ollama.internal]", compose)
        no_proxy = next(l for l in compose.splitlines() if "NO_PROXY:" in l)
        self.assertIn("ollama.internal", no_proxy.split(":", 1)[1].strip().split(","))


if __name__ == "__main__":
    unittest.main()
