"""Tests for agent_attachments(): which VM and network-lab runs an agent is working in right now (shown in the agent
window's header). Only runs that hold the agent count; finished, idle and other agents' runs do not.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402


class AgentAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.vm = {}
        self.topo = {}
        self.patches = [mock.patch.object(app, "VM_RUNS", self.vm), mock.patch.object(app, "TOPO_RUNS", self.topo)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_a_working_vm_run_is_listed_with_its_task(self):
        self.vm["v1"] = {"id": "v1", "agent": "alpha", "state": "working", "task_title": "Fix the parser"}
        self.assertEqual(app.agent_attachments("alpha"),
                         [{"kind": "vm", "id": "v1", "title": "Fix the parser", "state": "working"}])

    def test_a_vm_run_with_its_own_prompt_is_titled_as_such(self):
        self.vm["v2"] = {"id": "v2", "agent": "alpha", "state": "attached", "custom_prompt": "do X"}
        self.assertEqual(app.agent_attachments("alpha")[0]["title"], "own prompt")

    def test_a_working_lab_is_listed_as_a_network_lab(self):
        self.topo["t1"] = {"id": "t1", "agent": "alpha", "agent_done": False, "state": "working",
                           "topology_title": "2 hosts, 1 router", "plan_first": False}
        self.assertEqual(app.agent_attachments("alpha"),
                         [{"kind": "network", "id": "t1", "title": "2 hosts, 1 router", "state": "working"}])

    def test_finished_idle_and_other_agents_runs_are_not_listed(self):
        self.vm["done"] = {"id": "done", "agent": "alpha", "state": "done"}
        self.vm["other"] = {"id": "other", "agent": "beta", "state": "working"}
        self.vm["none"] = {"id": "none", "agent": None, "state": "working"}
        self.topo["idle"] = {"id": "idle", "agent": "alpha", "agent_done": True, "state": "ready"}
        self.assertEqual(app.agent_attachments("alpha"), [])


if __name__ == "__main__":
    unittest.main()
