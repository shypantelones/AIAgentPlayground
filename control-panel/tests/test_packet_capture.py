"""Tests for packet captures on lab nodes (app.topo_capture_start / topo_capture_file, vm_runner.capture_command).
SSH and scp are mocked; the capture job runs for real in its thread, so these wait for it to finish.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import shutil, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import vm_runner as vr  # noqa: E402


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class CaptureBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs")
        self.patch.start()
        self.rid = "cap12345"
        self.run = {"id": self.rid, "state": "ready", "nodes": {"h1": {"ssh_port": 2201}, "r1": {"ssh_port": 2202}},
                    "captures": [], "created": time.time()}
        app.TOPO_RUNS[self.rid] = self.run
        d = app.topo_run_dir(self.rid)
        d.mkdir(parents=True)
        (d / "id_ed25519").write_text("key")

    def tearDown(self):
        app.TOPO_RUNS.pop(self.rid, None)
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def finished(self, cap_id):
        return next(c for c in self.run["captures"] if c["id"] == cap_id)["state"] != "capturing"


class CommandTests(unittest.TestCase):
    def test_capture_is_time_limited_packet_capped_and_readable_after(self):
        cmd = vr.capture_command("enp0s8", 10, "/tmp/capture-abc.pcap")
        self.assertIn("timeout -s INT 10 tcpdump -i enp0s8", cmd)
        self.assertIn(f"-c {vr.CAPTURE_MAX_PACKETS}", cmd)
        self.assertIn("-w /tmp/capture-abc.pcap", cmd)
        self.assertIn("chmod 644 /tmp/capture-abc.pcap", cmd)
        self.assertTrue(cmd.rstrip().endswith("test -s /tmp/capture-abc.pcap"))   # an empty capture is a failure


class StartTests(CaptureBase):
    def test_bad_interface_names_are_refused(self):
        for iface in ["", "enp0s8; reboot", "../etc", "x" * 16, None]:
            with self.subTest(iface=iface), self.assertRaisesRegex(ValueError, "interface name"):
                app.topo_capture_start(self.rid, "h1", iface, 5)

    def test_seconds_must_be_a_number_in_range(self):
        with self.assertRaisesRegex(ValueError, "whole number"):
            app.topo_capture_start(self.rid, "h1", "enp0s8", "ten")
        for s in (0, app.CAPTURE_MAX_S + 1):
            with self.subTest(seconds=s), self.assertRaisesRegex(ValueError, "capture for 1 to"):
                app.topo_capture_start(self.rid, "h1", "enp0s8", s)

    def test_unknown_node_is_refused(self):
        with self.assertRaisesRegex(KeyError, "no node called 'h9'"):
            app.topo_capture_start(self.rid, "h9", "enp0s8", 5)

    def test_lab_must_have_its_vms_up(self):
        self.run["state"] = "queued"
        with self.assertRaisesRegex(ValueError, "aren't running"):
            app.topo_capture_start(self.rid, "h1", "enp0s8", 5)

    def test_one_capture_per_node_at_a_time(self):
        self.run["captures"].append({"id": "aaaaaaaa", "node": "h1", "state": "capturing"})
        with self.assertRaisesRegex(ValueError, "already capturing"):
            app.topo_capture_start(self.rid, "h1", "enp0s8", 5)

    def test_successful_capture_is_copied_off_and_downloadable(self):
        def fake_ssh(port, key, command, timeout=0):
            return 0, "", ""

        def fake_scp(port, key, remote, local, timeout=0):
            Path(local).write_bytes(b"\xd4\xc3\xb2\xa1pcap-bytes")
            return 0, "", ""

        with mock.patch.object(app.vr, "ssh_run", side_effect=fake_ssh), \
                mock.patch.object(app.vr, "scp_from", side_effect=fake_scp):
            cap = app.topo_capture_start(self.rid, "h1", "enp0s8", 5)
            self.assertEqual(cap["state"], "capturing")
            self.assertTrue(wait_for(lambda: self.finished(cap["id"])))
        done = self.run["captures"][0]
        self.assertEqual((done["state"], done["size"]), ("done", len(b"\xd4\xc3\xb2\xa1pcap-bytes")))
        name, data = app.topo_capture_file(self.rid, cap["id"])
        self.assertEqual(data, b"\xd4\xc3\xb2\xa1pcap-bytes")
        self.assertEqual(name, f"lab-{self.rid}-h1-{cap['id']}.pcap")

    def test_tcpdump_that_captured_nothing_is_a_failed_capture_with_the_reason(self):
        def fake_ssh(port, key, command, timeout=0):
            if "tcpdump" in command:
                return 1, "", "tcpdump: enp0s8: No such device"
            return 0, "", ""

        with mock.patch.object(app.vr, "ssh_run", side_effect=fake_ssh):
            cap = app.topo_capture_start(self.rid, "h1", "enp0s8", 5)
            self.assertTrue(wait_for(lambda: self.finished(cap["id"])))
        self.assertEqual(self.run["captures"][0]["state"], "failed")
        self.assertIn("no packets were captured", self.run["captures"][0]["reason"])

    def test_node_that_cannot_be_reached_says_so(self):
        with mock.patch.object(app.vr, "ssh_run", return_value=(255, "", "Connection refused")):
            cap = app.topo_capture_start(self.rid, "h1", "enp0s8", 5)
            self.assertTrue(wait_for(lambda: self.finished(cap["id"])))
        self.assertIn("could not reach h1", self.run["captures"][0]["reason"])

    def test_failed_copy_is_reported_not_downloaded(self):
        with mock.patch.object(app.vr, "ssh_run", return_value=(0, "", "")), \
                mock.patch.object(app.vr, "scp_from", return_value=(1, "", "scp: lost connection")):
            cap = app.topo_capture_start(self.rid, "h1", "enp0s8", 5)
            self.assertTrue(wait_for(lambda: self.finished(cap["id"])))
        self.assertIn("could not copy", self.run["captures"][0]["reason"])
        with self.assertRaises(KeyError):
            app.topo_capture_file(self.rid, cap["id"])


class DownloadTests(CaptureBase):
    def test_only_finished_captures_of_this_lab_download(self):
        self.run["captures"].append({"id": "bbbbbbbb", "node": "h1", "state": "failed"})
        with self.assertRaises(KeyError):
            app.topo_capture_file(self.rid, "bbbbbbbb")
        with self.assertRaises(KeyError):
            app.topo_capture_file(self.rid, "../../etc")
        with self.assertRaises(KeyError):
            app.topo_capture_file("nosuchlab", "bbbbbbbb")


if __name__ == "__main__":
    unittest.main()
