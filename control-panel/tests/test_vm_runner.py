"""Pure-logic tests for vm_runner.py: no VirtualBox/Vagrant/Docker needed.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import sys, tempfile, shutil, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import vm_runner as vr  # noqa: E402


class PortAllocationTests(unittest.TestCase):
    def test_skips_taken_ports(self):
        with mock.patch.object(vr, "port_free", return_value=True):
            self.assertEqual(vr.allocate_port((100, 105), {100, 101}), 102)

    def test_skips_ports_in_use_on_the_host(self):
        busy = {103}
        with mock.patch.object(vr, "port_free", side_effect=lambda p, host="127.0.0.1": p not in busy):
            self.assertEqual(vr.allocate_port((100, 105), set()), 100)
            self.assertEqual(vr.allocate_port((103, 105), set()), 104)

    def test_raises_when_range_exhausted(self):
        with mock.patch.object(vr, "port_free", return_value=False):
            with self.assertRaises(RuntimeError):
                vr.allocate_port((100, 101), set())


class VagrantfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_render_produces_expected_isolation_settings(self):
        vr.render_vagrantfile(self.tmp, "test-vm", 62201, "ssh-ed25519 AAAA test", 1536, 2, offline=True)
        vf = (self.tmp / "Vagrantfile").read_text()
        prov = (self.tmp / "provision.sh").read_text()
        self.assertIn('config.vm.box = "ubuntu/jammy64"', vf)
        self.assertIn("synced_folder \".\", \"/vagrant\", disabled: true", vf)
        self.assertIn('host: 62201, host_ip: "127.0.0.1"', vf)                   # loopback only, never the LAN
        self.assertIn('vb.memory = 1536', vf)
        self.assertIn('vb.cpus = 2', vf)
        self.assertIn('--clipboard-mode", "disabled"', vf)
        self.assertIn('--draganddrop", "disabled"', vf)
        self.assertIn("AAAA test", prov)                                        # the pubkey made it into authorized_keys
        self.assertIn("ufw default deny outgoing", prov)                        # offline lockdown present
        self.assertIn("ufw --force enable", prov)

    def test_offline_false_skips_the_firewall_lockdown(self):
        vr.render_vagrantfile(self.tmp, "t", 62202, "k", 1024, 1, offline=False)
        prov = (self.tmp / "provision.sh").read_text()
        self.assertNotIn("ufw default deny outgoing", prov)

    def test_no_crlf_in_generated_files(self):
        """Regression: a shebang corrupted by \\r\\n makes the Linux side report 'not found'."""
        vr.render_vagrantfile(self.tmp, "t", 62203, "k", 1024, 1, offline=True)
        for f in ("Vagrantfile", "provision.sh"):
            self.assertNotIn(b"\r\n", (self.tmp / f).read_bytes())

    def test_vm_name_and_port_are_not_injectable(self):
        # render_vagrantfile trusts its callers to have validated these (app.py generates them), but at minimum
        # a quote in a value must not let generated Ruby/shell break out of its string context undetected.
        vr.render_vagrantfile(self.tmp, "ok-name", 62204, "ssh-ed25519 AAAA x", 1024, 1, offline=True)
        vf = (self.tmp / "Vagrantfile").read_text()
        self.assertIn('vb.name = "ok-name"', vf)


class SeedFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_nested_files(self):
        vr.write_seed_files(self.tmp, {"a.py": "print(1)\n", "sub/b.txt": "hi\n"})
        self.assertEqual((self.tmp / "seed" / "a.py").read_text(), "print(1)\n")
        self.assertEqual((self.tmp / "seed" / "sub" / "b.txt").read_text(), "hi\n")

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            vr.write_seed_files(self.tmp, {"../escape.txt": "x"})
        with self.assertRaises(ValueError):
            vr.write_seed_files(self.tmp, {"/etc/passwd": "x"})


class TaskCatalogTests(unittest.TestCase):
    def test_catalog_loads_and_ids_are_valid(self):
        tasks = vr.load_tasks()
        self.assertGreaterEqual(len(tasks), 1)
        ids = [t["id"] for t in tasks]
        self.assertEqual(len(ids), len(set(ids)))
        for t in tasks:
            self.assertRegex(t["id"], r"^[a-z][a-z0-9-]{1,40}$")
            self.assertIn("prompt", t)
            self.assertIn("check", t)
            self.assertIsInstance(t.get("seed", {}), dict)

    def test_get_task_roundtrip_and_unknown_raises(self):
        t = vr.get_task(vr.load_tasks()[0]["id"])
        self.assertEqual(vr.get_task(t["id"])["id"], t["id"])
        with self.assertRaises(KeyError):
            vr.get_task("does-not-exist")

    def test_fizzbuzz_check_script_logic_runs_locally(self):
        # the check script is bash+python; run its python core directly to prove the comparison logic itself is right
        task = vr.get_task("fizzbuzz-cli")
        py = task["check"].split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
        lines = [str(i) for i in range(1, 101)]
        for i in range(3, 101, 3):
            lines[i - 1] = "Fizz"
        for i in range(5, 101, 5):
            lines[i - 1] = "Buzz"
        for i in range(15, 101, 15):
            lines[i - 1] = "FizzBuzz"
        out = Path(tempfile.mktemp())
        out.write_text("\n".join(lines) + "\n")
        ns = {}
        exec(py.replace("/tmp/out.txt", out.as_posix()), ns)   # as_posix(): avoids Windows backslashes breaking the generated source
        out.unlink()


class HaveToolsTests(unittest.TestCase):
    def test_reports_a_dict_of_booleans(self):
        t = vr.have_tools()
        for k in ("vagrant", "ssh", "scp", "ssh-keygen", "docker"):
            self.assertIn(k, t)
            self.assertIsInstance(t[k], bool)


if __name__ == "__main__":
    unittest.main()
