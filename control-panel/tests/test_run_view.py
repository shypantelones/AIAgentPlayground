"""The lab page reads a run through app.topo_run_view. Anything the page decides from must be in that view: the
forwarding button shows only when the view says build_mode is "full"."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402


def record(**extra):
    r = {"id": "abc12345", "state": "ready", "reason": "", "keep": True, "memory_mb": 512, "cpus": 1, "created": 0,
         "started": 0, "ended": None, "agent": None, "topology_id": "r1s1h2", "topology_title": "t", "task_id": None,
         "task_title": None, "custom_prompt": None, "chat": None, "score": None, "benchmark_id": None,
         "nodes": {"r1": {"role": "router", "ssh_port": 1, "terminal": {"active": False, "port": None}}}}
    r.update(extra)
    return r


class RunViewTests(unittest.TestCase):
    def test_a_container_lab_says_so_in_its_view(self):
        self.assertEqual(app.topo_run_view(record(build_mode="full"))["build_mode"], "full")

    def test_a_record_without_a_build_mode_is_a_vm_lab_in_the_view(self):
        self.assertIsNone(app.topo_run_view(record())["build_mode"])


if __name__ == "__main__":
    unittest.main()
