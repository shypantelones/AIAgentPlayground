"""Tests for the agent's ./forward command and the panel's answers to it (app.forward_script, serve_forward_requests_once).
The gateway calls are mocked; the ./forward script itself runs with sh, with its files moved to a temp folder.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402

NAMES = ["h1", "r1", "h2"]


class ForwardScriptTests(unittest.TestCase):
    @unittest.skipIf(sys.platform.startswith("win"), "runs the script with sh")
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.outbox = self.tmp / "outbox"
        self.result = self.tmp / "result"
        script = app.forward_script(NAMES).replace(app.FORWARD_OUTBOX, str(self.outbox)) \
                                          .replace(app.FORWARD_RESULT, str(self.result))
        self.script = self.tmp / "forward"
        self.script.write_text(script)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_forward(self, *args):
        return subprocess.run(["sh", str(self.script), *args], capture_output=True, text=True, timeout=10)

    def test_a_bad_value_or_unknown_node_is_refused_without_queuing(self):
        self.assertEqual(self.run_forward("r1", "yes").returncode, 2)
        bad = self.run_forward("nope", "on")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("this lab's nodes are: h1, r1, h2", bad.stderr)
        self.assertFalse(self.outbox.exists())

    @unittest.skipIf(sys.platform.startswith("win"), "runs the script with sh")
    def test_a_valid_request_is_queued_and_the_answer_is_printed(self):
        self.result.write_text("")
        # the panel answers while the script waits: write the answer after the request appears
        def answer_later():
            import time
            for _ in range(50):
                if self.outbox.exists() and self.outbox.read_text().strip():
                    rid_ = self.outbox.read_text().split()[0]
                    self.result.write_text(f"{rid_} ok r1 on\n")
                    return
                time.sleep(0.1)
        import threading
        threading.Thread(target=answer_later, daemon=True).start()
        p = self.run_forward("r1", "on")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ok r1 on", p.stdout)
        self.assertIn(" r1 on", self.outbox.read_text())

    @unittest.skipIf(sys.platform.startswith("win"), "runs the script with sh")
    def test_an_error_answer_is_a_failure_for_the_agent(self):
        self.result.write_text("")
        import threading, time
        def answer_later():
            for _ in range(50):
                if self.outbox.exists() and self.outbox.read_text().strip():
                    self.result.write_text(self.outbox.read_text().split()[0] + " error lab is not up\n")
                    return
                time.sleep(0.1)
        threading.Thread(target=answer_later, daemon=True).start()
        p = self.run_forward("h1", "off")
        self.assertEqual(p.returncode, 1)
        self.assertIn("error lab is not up", p.stdout)


class PanelAnswerTests(unittest.TestCase):
    def setUp(self):
        self.answers = []
        self.run = {"id": "abc12345", "build_mode": "full", "agent": "alpha", "state": "working", "nodes": {}}
        self.patches = [
            mock.patch.object(app, "TOPO_RUNS", {"abc12345": self.run}),
            mock.patch.object(app, "answer_forward", side_effect=lambda agent, text: self.answers.append(text)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def queue(self, *requests):
        lines = "".join(f"{i} {node} {'on' if on else 'off'}\n" for i, node, on in requests)
        return mock.patch.object(app, "dc", return_value=(0, lines, ""))

    def test_a_request_is_applied_and_answered_with_ok(self):
        with self.queue(("1700000001", "r1", True)), \
                mock.patch.object(app, "set_topo_forwarding", return_value={}) as set_fwd:
            app.serve_forward_requests_once()
        set_fwd.assert_called_once_with("abc12345", "r1", True)
        self.assertEqual(self.answers, ["1700000001 ok r1 on"])
        self.assertEqual(self.run["forward_changes"], 1)

    def test_a_refusal_from_the_lab_is_answered_with_its_reason(self):
        with self.queue(("1700000002", "r1", False)), \
                mock.patch.object(app, "set_topo_forwarding", side_effect=ValueError("the lab must be up")):
            app.serve_forward_requests_once()
        self.assertEqual(self.answers, ["1700000002 error the lab must be up"])
        self.assertNotIn("forward_changes", self.run)

    def test_a_lab_with_no_agent_or_a_vm_lab_is_not_served(self):
        self.run["agent"] = None
        with mock.patch.object(app, "dc") as dc:
            app.serve_forward_requests_once()
        dc.assert_not_called()
        self.run["agent"] = "alpha"
        self.run["build_mode"] = "vm"
        with mock.patch.object(app, "dc") as dc:
            app.serve_forward_requests_once()
        dc.assert_not_called()

    def test_changes_stop_at_the_limit_for_the_lab(self):
        self.run["forward_changes"] = app.FORWARD_MAX_CHANGES
        with self.queue(("1700000003", "r1", True)), \
                mock.patch.object(app, "set_topo_forwarding") as set_fwd:
            app.serve_forward_requests_once()
        set_fwd.assert_not_called()
        self.assertIn("already changed forwarding", self.answers[0])

    def test_a_garbled_line_in_the_outbox_is_ignored(self):
        with mock.patch.object(app, "dc", return_value=(0, "rm -rf /\nnot a request line\n", "")), \
                mock.patch.object(app, "set_topo_forwarding") as set_fwd:
            app.serve_forward_requests_once()
        set_fwd.assert_not_called()
        self.assertEqual(self.answers, [])


if __name__ == "__main__":
    unittest.main()
