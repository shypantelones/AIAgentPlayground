"""Tests for messages between team members: the ./peer-msg command, reading a member's outbox, the per-pair limit,
delivery in rounds, and the prompt that tells a member how to message a teammate. Agents and VMs are mocked; the
peer-msg script itself runs with sh.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402

TEAM = [{"agent": "alpha", "nodes": ["h1"], "stage": 1, "brief": "net", "role": "network-admin"},
        {"agent": "beta", "nodes": ["h2"], "stage": 1, "brief": "fw", "role": "firewall-admin"}]


class PeerMsgScriptTests(unittest.TestCase):
    @unittest.skipIf(sys.platform.startswith("win"), "runs the script with sh")
    def test_a_message_is_queued_base64_encoded_and_unknown_names_are_refused(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            outbox = tmp / "outbox"
            script = app.peer_msg_script("alpha", ["alpha", "beta"]).replace(app.TEAM_MAIL_OUTBOX, str(outbox))
            s = tmp / "peer-msg"; s.write_text(script)
            ok = subprocess.run(["sh", str(s), "beta", "route h2 via fw1, please"], capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertIn("queued for beta", ok.stdout)
            bad = subprocess.run(["sh", str(s), "gamma", "hi"], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 2)
            self_msg = subprocess.run(["sh", str(s), "alpha", "hi"], capture_output=True, text=True)
            self.assertEqual(self_msg.returncode, 2)
            self.assertEqual(outbox.read_text().count("\n"), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class OutboxTests(unittest.TestCase):
    def test_the_outbox_is_read_back_as_recipient_and_text(self):
        import base64
        line = "beta\t" + base64.b64encode(b"which route?").decode() + "\n"
        with mock.patch.object(app, "dc", return_value=(0, line, "")):
            self.assertEqual(app.take_member_outbox("alpha"), [("beta", "which route?")])


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "topo-runs").mkdir()
        self.patch = mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_pair_gets_its_messages_and_the_next_one_is_refused(self):
        r = {"id": "q1"}
        app.queue_team_mail(r, "alpha", [("beta", f"m{i}") for i in range(app.TEAM_MAIL_PER_PAIR + 1)])
        self.assertEqual(len(r["mail_queue"]), app.TEAM_MAIL_PER_PAIR)
        refused = [e for e in r["mail_log"] if e["status"].startswith("refused")]
        self.assertEqual(len(refused), 1)

    def test_a_long_message_is_cut_to_the_limit(self):
        r = {"id": "q2"}
        app.queue_team_mail(r, "alpha", [("beta", "x" * (app.TEAM_MAIL_MAX_CHARS + 50))])
        self.assertEqual(len(r["mail_queue"][0]["text"]), app.TEAM_MAIL_MAX_CHARS)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "topo-runs").mkdir()
        self.patch = mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_queued_message_is_delivered_in_a_turn_with_its_text(self):
        r = {"id": "d1", "mail_queue": [{"from": "alpha", "to": "beta", "text": "use fw1", "round": 1}], "mail_log": []}
        prompts = []

        def turn(r_, topo, ports, priv, m, team, prompt=None):
            prompts.append((m["agent"], prompt))
            return None

        with mock.patch.object(app, "topo_member_turn", side_effect=turn):
            app.deliver_team_mail(r, None, {}, Path("unused"), TEAM, lambda: False)
        self.assertEqual(prompts[0][0], "beta")
        self.assertIn("from alpha: use fw1", prompts[0][1])

    def test_a_chain_of_replies_stops_at_the_round_limit(self):
        r = {"id": "d2", "mail_queue": [{"from": "alpha", "to": "beta", "text": "ping", "round": 1}], "mail_log": []}
        turns = []

        def turn(r_, topo, ports, priv, m, team, prompt=None):
            turns.append(m["agent"])
            other = "alpha" if m["agent"] == "beta" else "beta"
            app.queue_team_mail(r_, m["agent"], [(other, "again")])   # every turn answers: a ping-pong
            return None

        with mock.patch.object(app, "topo_member_turn", side_effect=turn):
            app.deliver_team_mail(r, None, {}, Path("unused"), TEAM, lambda: False)
        self.assertEqual(len(turns), app.TEAM_MAIL_ROUNDS)
        self.assertEqual(r["mail_queue"], [])                  # the messages left over are dropped, not kept forever


class PromptTests(unittest.TestCase):
    def test_a_teammate_message_line_is_in_the_prompt_only_for_a_team(self):
        r = {"nodes": {"h1": {"role": "host"}, "h2": {"role": "host"}}, "custom_prompt": "go", "egress": {}, "intents": []}
        with_team = app.member_prompt(r, None, TEAM[0], TEAM)
        self.assertIn("./peer-msg <name>", with_team)
        self.assertIn("beta", with_team)
        alone = app.member_prompt(r, None, TEAM[0], [TEAM[0]])
        self.assertNotIn("./peer-msg", alone)


if __name__ == "__main__":
    unittest.main()
