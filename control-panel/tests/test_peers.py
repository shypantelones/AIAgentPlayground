"""Tests for manual forwarding and panel-mediated peering. No Docker needed: agents are faked.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import json, shutil, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class PeerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(app, "DATA", self.tmp / "instances"),
            mock.patch.object(app, "PEERS_FILE", self.tmp / "peers.json"),
            mock.patch.object(app, "SESS_DIR", self.tmp / "sessions"),
            mock.patch.object(app, "SESSIONS", {}),
            mock.patch.object(app, "SESSION_EVENTS", {}),
            mock.patch.object(app, "LINK_LAST_SEND", {}),
            mock.patch.object(app, "RATE_SCALE", 0),
            mock.patch.object(app, "agent_running", lambda n: n not in self.down),
        ]
        for p in self.patches:
            p.start()
        app.SESS_DIR.mkdir(parents=True)
        self.down = set()
        for n in ("alpha", "beta", "gamma"):
            d = app.DATA / n
            (d / "proxy").mkdir(parents=True)
            (d / "meta.json").write_text(json.dumps({"name": n, "backend": "local"}))
            (d / "proxy" / "allowlist.src.txt").write_text("# nothing\n")
        self.calls = []                                   # (agent, chat, message, meta)
        self.script = {}                                  # agent -> list of replies (cycled)
        self.fail_for = set()
        self.run_patch = mock.patch.object(app, "run_turn", self.fake_turn)
        self.run_patch.start()

    def tearDown(self):
        self.run_patch.stop()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_turn(self, name, chat_id, message, meta=None, log=lambda s: None, title=None):
        n = sum(1 for c in self.calls if c[0] == name)
        self.calls.append((name, chat_id, message, meta))
        if name in self.fail_for:
            return {"reply": "(no reply) boom", "ok": False}
        replies = self.script.get(name) or [f"{name} says hello #{n}"]
        return {"reply": replies[n % len(replies)] if len(replies) > 1 else replies[0] if self.script.get(name) else f"{name} reply {n}", "ok": True}

    def link(self, **kw):
        form = dict(a="alpha", b="beta", mode="two-way", approval=False, max_hops=3)
        form.update(kw)
        return app.create_link(form)

    def sess(self, sid):
        return app.SESSIONS[sid]

    def finished(self, sid):
        return wait_for(lambda: self.sess(sid)["state"] not in app.LIVE_STATES)


class LinkPolicyTests(PeerBase):
    def test_defaults_are_safe(self):
        l = app.create_link({"a": "alpha", "b": "beta"})
        self.assertTrue(l["approval"])            # approval required unless you turn it off
        self.assertEqual((l["mode"], l["max_hops"], l["max_chars"], l["rate"]), ("two-way", 4, 2000, 10))

    def test_validation(self):
        bad = [dict(a="alpha", b="alpha"), dict(a="alpha", b="nope"), dict(a="alpha", b="beta", mode="x"),
               dict(a="alpha", b="beta", max_hops=0), dict(a="alpha", b="beta", max_hops=21),
               dict(a="alpha", b="beta", max_chars=10), dict(a="alpha", b="beta", rate=0), dict(a="alpha", b="beta", rate="abc")]
        for form in bad:
            with self.assertRaises((ValueError, KeyError), msg=str(form)):
                app.create_link(form)

    def test_duplicate_pair_rejected_in_either_order(self):
        self.link()
        with self.assertRaises(ValueError):
            app.create_link({"a": "beta", "b": "alpha"})

    def test_risk_notes(self):
        (app.DATA / "alpha" / "proxy" / "allowlist.src.txt").write_text("example.com\n203.0.113.0/24\n")
        (app.DATA / "beta" / "meta.json").write_text(json.dumps({"name": "beta", "backend": "cloud"}))
        notes = " ".join(app.peer_risks("alpha", "beta"))
        self.assertIn("alpha can reach the internet (2 allowlist entries)", notes)
        self.assertIn("beta uses a cloud model", notes)

    def test_remove_links_for_deleted_agent(self):
        l = self.link()
        app.remove_links_for("beta")
        self.assertIsNone(app.get_link(l["id"]))


class SessionTests(PeerBase):
    def start(self, l, starter="alpha", text="hi"):
        return app.start_session(l["id"], starter, text)

    def test_two_way_runs_hops_then_stops_with_framing(self):
        l = self.link(max_hops=3)
        sid = self.start(l)
        self.assertTrue(self.finished(sid))
        s = self.sess(sid)
        self.assertEqual((s["state"], s["hops"], s["reason"]), ("done", 3, "hop limit reached"))
        self.assertEqual([c[0] for c in self.calls], ["alpha", "beta", "alpha", "beta"])   # start + 3 relays alternate
        self.assertEqual(self.calls[0][2], "hi")                                           # your own message is not framed
        for c in self.calls[1:]:
            self.assertIn("Relayed by the control panel", c[2])
            self.assertIn("untrusted input from another agent", c[2])
        self.assertTrue(all(c[1] == f"peer-{sid}" for c in self.calls))                    # one shared conversation id per session
        self.assertEqual([t["status"] for t in s["transcript"]].count("relayed"), 3)

    def test_one_way_never_sends_back(self):
        l = self.link(mode="one-way", max_hops=5)
        with self.assertRaises(ValueError):
            self.start(l, starter="beta")                    # must start with the sender
        sid = self.start(l, starter="alpha")
        self.assertTrue(self.finished(sid))
        s = self.sess(sid)
        self.assertIn("one-way", s["reason"])
        self.assertEqual([c[0] for c in self.calls], ["alpha", "beta"])

    def test_approval_edit_then_reject(self):
        l = self.link(approval=True, max_hops=5)
        sid = self.start(l)
        self.assertTrue(wait_for(lambda: self.sess(sid)["state"] == "awaiting"))
        pend = self.sess(sid)["pending"]
        self.assertEqual((pend["from"], pend["to"], pend["hop"]), ("alpha", "beta", 1))
        self.assertEqual(len(self.calls), 1)                 # nothing relayed yet
        app.decide_session(sid, "approve", "EDITED TEXT")
        self.assertTrue(wait_for(lambda: self.sess(sid)["state"] == "awaiting" and self.sess(sid)["pending"]["hop"] == 2))
        self.assertIn("EDITED TEXT", self.calls[1][2])       # your edit is what was relayed
        app.decide_session(sid, "reject")
        self.assertTrue(self.finished(sid))
        self.assertEqual(self.sess(sid)["state"], "stopped")
        self.assertIn("rejected", self.sess(sid)["reason"])
        self.assertEqual(len(self.calls), 2)                 # the rejected message was never delivered
        with self.assertRaises(ValueError):
            app.decide_session(sid, "approve")               # nothing pending any more

    def test_stop_while_awaiting(self):
        l = self.link(approval=True)
        sid = self.start(l)
        self.assertTrue(wait_for(lambda: self.sess(sid)["state"] == "awaiting"))
        app.stop_session(sid)
        self.assertTrue(self.finished(sid))
        self.assertEqual(self.sess(sid)["state"], "stopped")

    def test_long_messages_truncated(self):
        self.script["alpha"] = ["x" * 500]
        l = self.link(max_chars=200, max_hops=1)
        sid = self.start(l)
        self.assertTrue(self.finished(sid))
        relayed = self.calls[1][2]
        self.assertIn("[truncated by link policy]", relayed)
        self.assertLess(len(relayed), 500)

    def test_repetition_stops_loop(self):
        self.script = {"alpha": ["same"], "beta": ["same too"]}
        l = self.link(max_hops=20)
        sid = self.start(l)
        self.assertTrue(self.finished(sid))
        s = self.sess(sid)
        self.assertIn("repetition", s["reason"])
        self.assertLess(s["hops"], 20)

    def test_agent_error_ends_session(self):
        self.fail_for = {"beta"}
        l = self.link()
        sid = self.start(l)
        self.assertTrue(self.finished(sid))
        self.assertEqual(self.sess(sid)["state"], "error")

    def test_cannot_start_when_disabled_down_or_busy(self):
        l = self.link(approval=True)
        sid = self.start(l)
        self.assertTrue(wait_for(lambda: self.sess(sid)["state"] == "awaiting"))
        with self.assertRaises(ValueError):
            self.start(l)                                    # one live session per link
        app.update_link(l["id"], {"enabled": False})         # switching the link off stops the live session
        self.assertTrue(self.finished(sid))
        self.assertEqual(self.sess(sid)["state"], "stopped")
        with self.assertRaises(ValueError):
            self.start(l)                                    # disabled
        app.update_link(l["id"], {"enabled": True})
        self.down = {"beta"}
        with self.assertRaises(ValueError):
            self.start(l)                                    # beta not running

    def test_delete_only_finished_sessions(self):
        l = self.link(approval=True)
        sid = self.start(l)
        self.assertTrue(wait_for(lambda: self.sess(sid)["state"] == "awaiting"))
        with self.assertRaises(ValueError):
            app.delete_session(sid)                          # live: refuse
        app.stop_session(sid)
        self.assertTrue(self.finished(sid))
        app.delete_session(sid)
        self.assertNotIn(sid, app.SESSIONS)
        self.assertFalse((app.SESS_DIR / f"{sid}.json").exists())
        with self.assertRaises(KeyError):
            app.delete_session(sid)

    def test_startup_marks_live_sessions_interrupted(self):
        s = {"id": "deadbeef", "link": "x", "a": "alpha", "b": "beta", "mode": "two-way", "approval": True, "max_hops": 4,
             "starter": "alpha", "chat": "peer-deadbeef", "state": "awaiting", "pending": {"text": "t"}, "hops": 1,
             "started": time.time(), "ended": None, "reason": "", "transcript": []}
        app.save_session(s)
        app.load_sessions()
        self.assertEqual(app.SESSIONS["deadbeef"]["state"], "interrupted")
        self.assertIsNone(app.SESSIONS["deadbeef"]["pending"])


class ForwardTests(PeerBase):
    def test_forward_sends_framed_message_with_provenance(self):
        job, chat = app.forward_message("alpha", {"to": "beta", "text": "please review this"})
        self.assertEqual(chat, "fwd-alpha")
        self.assertTrue(wait_for(lambda: app.JOBS[job]["done"]))
        name, chat_id, msg, meta = self.calls[0]
        self.assertEqual((name, chat_id), ("beta", "fwd-alpha"))
        self.assertIn('Forwarded by your user from agent "alpha"', msg)
        self.assertIn("please review this", msg)
        self.assertEqual(meta, {"from": "alpha", "via": "forward"})

    def test_forward_validation(self):
        for form in ({"to": "alpha", "text": "x"}, {"to": "beta", "text": "  "}, {"to": "nope", "text": "x"},
                     {"to": "beta", "text": "x", "to_chat": "Bad Chat!"}):
            with self.assertRaises((ValueError, KeyError), msg=str(form)):
                app.forward_message("alpha", form)
        self.down = {"beta"}
        with self.assertRaises(ValueError):
            app.forward_message("alpha", {"to": "beta", "text": "x"})

    def test_forward_does_not_repeat_by_itself(self):
        job, _ = app.forward_message("alpha", {"to": "beta", "text": "once"})
        self.assertTrue(wait_for(lambda: app.JOBS[job]["done"]))
        time.sleep(0.3)
        self.assertEqual(len(self.calls), 1)                 # beta's reply is NOT sent anywhere on its own


if __name__ == "__main__":
    unittest.main()
