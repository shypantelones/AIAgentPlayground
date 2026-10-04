"""Tests for VM-side role logins: provisioning creates them with limited sudo, a member's key goes to its role's
login only, and a member's wrapper connects as that login. Nothing here touches a VM.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import shutil, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import lab_roles as lr  # noqa: E402
import vm_runner as vr  # noqa: E402


class ProvisionTests(unittest.TestCase):
    def test_every_role_login_is_created_on_every_lab_vm(self):
        script = vr._topo_provision_script("host", "ssh-ed25519 K")
        for user in list(lr.ROLE_USERS.values()) + [lr.NO_ROLE_USER]:
            self.assertIn(f"useradd -m -s /bin/bash {user}", script)

    def test_a_sudoers_file_is_checked_and_removed_if_it_is_bad(self):
        script = lr.provision_users_script()
        self.assertIn("visudo -cf /etc/sudoers.d/92-fwadmin", script)
        self.assertIn("|| rm -f /etc/sudoers.d/92-fwadmin", script)

    def test_the_firewall_login_may_use_nft_and_nothing_outside_its_list(self):
        allow = lr.SUDO_ALLOW["firewall-admin"]
        self.assertIn("/usr/sbin/nft", allow)
        self.assertNotIn("/bin/bash", allow)
        for role, paths in lr.SUDO_ALLOW.items():
            for p in paths:
                with self.subTest(role=role, path=p):
                    self.assertTrue(p.startswith("/"), p)
                    self.assertNotEqual(p, "ALL")

    def test_the_client_and_the_no_role_login_get_no_sudo_at_all(self):
        script = lr.provision_users_script()
        self.assertNotIn("92-clientdev", script)
        self.assertNotIn("92-member", script)

    def test_a_member_without_a_role_gets_the_member_login(self):
        self.assertEqual(lr.user_for(None), "member")
        self.assertEqual(lr.user_for("firewall-admin"), "fwadmin")


class KeyTests(unittest.TestCase):
    def test_install_puts_the_key_in_the_role_login_only(self):
        s = app.member_key_script("fwadmin", "ssh-ed25519 AAAA agent")
        self.assertIn("/home/fwadmin/.ssh/authorized_keys", s)
        self.assertNotIn("/home/bench", s)
        self.assertNotIn("ssh-ed25519", s)                   # the key travels base64-encoded

    def test_remove_takes_only_that_key_out(self):
        s = app.member_key_script("fwadmin", "ssh-ed25519 AAAA agent", remove=True)
        self.assertIn('grep -v -F -x "$K"', s)
        self.assertIn("rm -f /tmp/ak.$$", s)


class MemberLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "topo-runs").mkdir()
        self.patches = [mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs"),
                        mock.patch.object(app, "env_file", return_value=self.tmp / "e"),
                        mock.patch.object(app, "proj", return_value="p")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_firewall_admin_member_connects_as_its_login_and_gets_its_key_installed(self):
        topo = vr.get_topology("s1h2")
        priv = self.tmp / "id_ed25519"; priv.write_text("bench")
        m = {"agent": "alpha", "nodes": ["h1"], "stage": 1, "brief": "x", "role": "firewall-admin", "chat": "c"}
        r = {"id": "ru", "nodes": {"h1": {"role": "host"}, "sw1": {}, "h2": {}}, "custom_prompt": "go", "egress": {}, "intents": []}
        ssh_calls, wrappers = [], []
        with mock.patch.object(app, "dc", side_effect=lambda *a, **k: wrappers.append(k.get("input")) or (0, "", "")), \
                mock.patch.object(app, "run", return_value=(0, "", "")), \
                mock.patch.object(app.vr, "ssh_script", side_effect=lambda *a, **k: ssh_calls.append(a[2]) or (0, "", "")), \
                mock.patch.object(app, "topo_agent_turn", return_value={"ok": True}), \
                mock.patch.object(app, "detach_topo_agent"):
            err = app.topo_member_turn(r, topo, {"h1": 1, "sw1": 2, "h2": 3}, priv, m, [m])
        self.assertIsNone(err)
        self.assertTrue(any("/home/fwadmin/.ssh/authorized_keys" in s for s in ssh_calls))     # installed on its node
        wrapper = next(w for w in wrappers if w and "grep -Eq" in w)
        self.assertIn("fwadmin@vm-relay-topo", wrapper)                                          # connects as fwadmin
        self.assertNotIn("bench@vm-relay-topo", wrapper)


if __name__ == "__main__":
    unittest.main()
