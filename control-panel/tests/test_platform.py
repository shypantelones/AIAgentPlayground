"""Tests for platform_support. Run from control-panel/:  python3 -m unittest discover -s tests -v

The macOS Keychain and Linux Secret Service backends are exercised against fake `security` / `secret-tool`
programs (shell scripts) so the command construction, stdin handling and cleanup can be checked on any OS with bash.
They do NOT prove behaviour against the real tools; test on a real Mac / Linux desktop before relying on them.
"""
import json, os, shutil, stat, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import platform_support as ps  # noqa: E402

TOKEN = "sk-ant-TESTKEY-1234567890abcdef"
HAVE_BASH = shutil.which("bash") is not None and not ps.IS_WIN

FAKE_SECURITY = r"""#!/usr/bin/env bash
# fake macOS `security`: records every argv, supports -i (stdin commands), find, delete
STORE="$FAKE_DIR/kc"; mkdir -p "$STORE"; echo "ARGV: $*" >> "$FAKE_DIR/argv.log"
run() {  # run "<command line>"
  set -- $1
  case "$1" in
    add-generic-password) while [ $# -gt 0 ]; do case "$1" in -a) A="$2"; shift;; -w) W="$2"; shift;; esac; shift; done; printf %s "$W" > "$STORE/$A";;
    find-generic-password) while [ $# -gt 0 ]; do case "$1" in -a) A="$2"; shift;; esac; shift; done; [ -f "$STORE/$A" ] && cat "$STORE/$A" && echo || exit 44;;
    delete-generic-password) while [ $# -gt 0 ]; do case "$1" in -a) A="$2"; shift;; esac; shift; done; rm -f "$STORE/$A";;
  esac
}
if [ "$1" = "-i" ]; then while IFS= read -r line; do echo "STDIN: ${line%% -w *} -w [redacted-in-log]" >> "$FAKE_DIR/argv.log"; run "$line"; done; else run "$*"; fi
"""

FAKE_SECRET_TOOL = r"""#!/usr/bin/env bash
STORE="$FAKE_DIR/ss"; mkdir -p "$STORE"; echo "ARGV: $*" >> "$FAKE_DIR/argv.log"
cmd="$1"; shift
acct=""; while [ $# -gt 0 ]; do [ "$1" = "account" ] && acct="$2"; shift; done
case "$cmd" in
  store) cat > "$STORE/$acct";;
  lookup) [ -f "$STORE/$acct" ] && cat "$STORE/$acct" || exit 1;;
  clear) rm -f "$STORE/$acct";;
  --label=*) cat > "$STORE/$acct";;
esac
"""


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ps.SecretStore(Path(self.tmp) / "secrets", force="file")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_and_delete(self):
        self.assertFalse(self.store.exists("a"))
        self.store.put("a", TOKEN)
        self.assertTrue(self.store.exists("a"))
        self.assertEqual(self.store.get("a"), TOKEN)
        self.store.delete("a")
        self.assertFalse(self.store.exists("a"))
        self.assertIsNone(self.store.get("a"))

    @unittest.skipIf(ps.IS_WIN, "POSIX permissions")
    def test_file_is_private(self):
        self.store.put("a", TOKEN)
        mode = stat.S_IMODE(os.stat(self.store._marker("a")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_label_is_honest(self):
        self.assertIn("NOT encrypted", self.store.label)


@unittest.skipUnless(HAVE_BASH, "needs bash and a POSIX OS")
class FakeKeychainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = Path(self.tmp) / "bin"; self.bin.mkdir()
        for name, body in (("security", FAKE_SECURITY), ("secret-tool", FAKE_SECRET_TOOL)):
            f = self.bin / name; f.write_text(body); f.chmod(0o755)
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": self.tmp}
        self.patch = mock.patch.dict(os.environ, env); self.patch.start()

    def tearDown(self):
        self.patch.stop(); shutil.rmtree(self.tmp, ignore_errors=True)

    def argv_log(self):
        return (Path(self.tmp) / "argv.log").read_text()

    def check_store(self, kind):
        s = ps.SecretStore(Path(self.tmp) / "secrets", force=kind)
        s.put("agent", TOKEN)
        self.assertTrue(s.exists("agent"))
        self.assertEqual(s.get("agent"), TOKEN)
        marker = s._marker("agent").read_text()
        self.assertNotIn(TOKEN, marker, "the marker file must not contain the key")
        self.assertEqual(json.loads(marker)["store"], kind)
        self.assertNotIn(TOKEN, self.argv_log().replace("redacted-in-log", ""), "key must never appear in command-line arguments")
        s.delete("agent")
        self.assertFalse(s.exists("agent")); self.assertIsNone(s.get("agent"))

    def test_keychain(self):
        self.check_store("keychain")
        self.assertIn("STDIN: add-generic-password", self.argv_log())   # delivered via `security -i` stdin

    def test_libsecret(self):
        self.check_store("libsecret")

    def test_pick_prefers_libsecret_on_linux(self):
        with mock.patch.object(ps, "IS_WIN", False), mock.patch.object(ps, "IS_MAC", False), mock.patch.object(ps, "IS_LINUX", True):
            self.assertEqual(ps.SecretStore(Path(self.tmp) / "s2").kind, "libsecret")

    def test_pick_prefers_keychain_on_mac(self):
        with mock.patch.object(ps, "IS_WIN", False), mock.patch.object(ps, "IS_MAC", True):
            self.assertEqual(ps.SecretStore(Path(self.tmp) / "s3").kind, "keychain")

    def test_pick_falls_back_to_file_without_tools(self):
        for t in ("security", "secret-tool"):
            (self.bin / t).unlink()
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}), mock.patch.object(ps, "IS_WIN", False):
            self.assertEqual(ps.SecretStore(Path(self.tmp) / "s4").kind, "file")


class OllamaModeTests(unittest.TestCase):
    def test_env_override(self):
        for m in ("nvidia", "cpu", "host"):
            with mock.patch.dict(os.environ, {"OLLAMA_MODE": m}):
                self.assertEqual(ps.detect_ollama_mode(), m)

    def test_mac_defaults_to_host(self):
        with mock.patch.dict(os.environ, {"OLLAMA_MODE": ""}), mock.patch.object(ps, "IS_MAC", True):
            self.assertEqual(ps.detect_ollama_mode(), "host")

    def test_nvidia_runtime_detected(self):
        with mock.patch.dict(os.environ, {"OLLAMA_MODE": ""}), mock.patch.object(ps, "IS_MAC", False), \
             mock.patch.object(ps, "_run", return_value=(0, '{"nvidia":{},"runc":{}}', "")):
            self.assertEqual(ps.detect_ollama_mode(), "nvidia")

    def test_cpu_when_no_nvidia(self):
        with mock.patch.dict(os.environ, {"OLLAMA_MODE": ""}), mock.patch.object(ps, "IS_MAC", False), \
             mock.patch.object(ps, "_run", return_value=(0, '{"runc":{}}', "")):
            self.assertEqual(ps.detect_ollama_mode(), "cpu")

    def test_cpu_when_docker_missing(self):
        with mock.patch.dict(os.environ, {"OLLAMA_MODE": ""}), mock.patch.object(ps, "IS_MAC", False), \
             mock.patch.object(ps, "_run", return_value=(127, "", "no docker")):
            self.assertEqual(ps.detect_ollama_mode(), "cpu")


class UnifiedMemoryTests(unittest.TestCase):
    def test_apple_silicon_mac(self):
        with mock.patch.object(ps, "IS_MAC", True), mock.patch.object(ps, "_run", return_value=(0, "1\n", "")):
            self.assertTrue(ps.apple_silicon())

    def test_intel_mac(self):
        # Intel Macs have no hw.optional.arm64 key: sysctl exits non-zero
        with mock.patch.object(ps, "IS_MAC", True), mock.patch.object(ps, "_run", return_value=(1, "", "unknown oid")):
            self.assertFalse(ps.apple_silicon())

    def test_not_a_mac(self):
        with mock.patch.object(ps, "IS_MAC", False):
            self.assertFalse(ps.apple_silicon())


if __name__ == "__main__":
    unittest.main()
