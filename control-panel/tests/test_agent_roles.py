"""Tests for team roles: parsing a role from a team line, which commands each role's guard refuses, the role in the
member's prompt, and the guard the wrapper gets. Real wrapper runs use sh and a fake ssh (not on Windows).
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import lab_roles as lr  # noqa: E402
import vm_runner as vr  # noqa: E402

NODES = ["h1", "r1", "h2"]


class RoleParsingTests(unittest.TestCase):
    def test_a_bracketed_role_is_taken_off_the_brief(self):
        self.assertEqual(lr.role_of("[web-admin] serves the feed"), ("web-admin", "serves the feed"))

    def test_no_role_is_fine(self):
        self.assertEqual(lr.role_of("just a brief"), (None, "just a brief"))

    def test_unknown_or_broken_roles_say_what_is_wrong(self):
        with self.assertRaisesRegex(ValueError, "no role called 'wizard'"):
            lr.role_of("[wizard] casts spells")
        with self.assertRaisesRegex(ValueError, "closing"):
            lr.role_of("[web-admin serves")

    def test_validate_team_carries_the_role_and_strips_it_from_the_brief(self):
        (m,) = app.validate_team("alpha | h2 | 1 | [web-admin] serves the feed", NODES)
        self.assertEqual((m["role"], m["brief"]), ("web-admin", "serves the feed"))

    def test_a_role_with_no_task_after_it_is_refused(self):
        with self.assertRaisesRegex(ValueError, "after its role"):
            app.validate_team("alpha | h2 | 1 | [web-admin]", NODES)


class GuardTests(unittest.TestCase):
    def test_the_network_admin_may_change_firewall_and_routes(self):
        deny = lr.denied_patterns("network-admin")
        self.assertFalse(any("nft" in d or "route" in d for d in deny))

    def test_a_web_admin_may_not_touch_the_firewall_or_routes(self):
        deny = "|".join(lr.denied_patterns("web-admin"))
        self.assertIn(r"\bnft\b", deny)
        self.assertIn(r"\bufw\b", deny)
        self.assertIn(r"\bip\s+route\b", deny)

    def test_every_role_refuses_power_operations(self):
        for role in list(lr.ROLES) + [None]:
            with self.subTest(role=role):
                self.assertIn(r"\breboot\b", lr.denied_patterns(role))

    def test_a_role_line_is_in_the_members_prompt(self):
        r = {"nodes": {"h2": {"role": "host"}}, "custom_prompt": "go", "egress": {}, "intents": []}
        m = {"agent": "beta", "nodes": ["h2"], "stage": 1, "brief": "serves the feed", "role": "web-admin"}
        text = app.member_prompt(r, None, m, [m])
        self.assertIn("Your role is web-admin", text)
        self.assertIn("Your task: serves the feed", text)

    def test_a_member_turn_gives_its_wrapper_its_roles_guard(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            priv = tmp / "id_ed25519"; priv.write_text("key")
            (tmp / "topo-runs").mkdir()
            (tmp / "topo-runs").mkdir()
            topo = vr.get_topology("s1h2")
            m = {"agent": "beta", "nodes": ["h2"], "stage": 1, "brief": "x", "role": "web-admin", "chat": "c"}
            r = {"id": "rl", "nodes": {"h2": {"role": "host"}, "h1": {"role": "host"}, "sw1": {"role": "switch"}},
                 "custom_prompt": "go", "egress": {}, "intents": []}
            wrappers = []
            with mock.patch.object(app, "TOPOR_DIR", tmp / "topo-runs"), \
                    mock.patch.object(app.vr, "ssh_script", return_value=(0, "", "")), \
                    mock.patch.object(app, "dc", side_effect=lambda *a, **k: wrappers.append(k.get("input")) or (0, "", "")), \
                    mock.patch.object(app, "run", return_value=(0, "", "")), \
                    mock.patch.object(app, "topo_agent_turn", return_value={"ok": True}), \
                    mock.patch.object(app, "detach_topo_agent"), \
                    mock.patch.object(app, "env_file", return_value=tmp / "e"), mock.patch.object(app, "proj", return_value="p"):
                app.topo_member_turn(r, topo, {"h1": 1, "sw1": 2, "h2": 3}, priv, m, [m])
            text = next(w for w in wrappers if w and "grep -Eq" in w)
            self.assertIn("ufw", text)                          # the web admin's wrapper refuses ufw
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(sys.platform.startswith("win"), "runs the wrapper with sh and a fake ssh")
class RealWrapperTests(unittest.TestCase):
    def test_a_web_admin_wrapper_refuses_firewall_commands_and_runs_the_rest(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            b = tmp / "bin"; b.mkdir()
            (b / "ssh").write_text("#!/bin/sh\necho ran\ncat >/dev/null 2>&1\n"); (b / "ssh").chmod(0o755)
            w = tmp / "vmrun-h2"
            w.write_text(vr.vmrun_script("/k", 1, "h", str(tmp / "log"), node="h2", deny=lr.denied_patterns("web-admin")))
            env = dict(os.environ, PATH=f"{b}{os.pathsep}{os.environ['PATH']}")
            ok = subprocess.run(["sh", str(w), "systemctl status nginx"], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            bad = subprocess.run(["sh", str(w), "sudo nft list ruleset"], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            bad_script = subprocess.run(["sh", str(w)], input="sudo ufw disable\n", env=env, capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0)
            self.assertEqual((bad.returncode, bad_script.returncode), (4, 4))
            self.assertIn("REFUSED by this agent's role", (tmp / "log").read_text())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
