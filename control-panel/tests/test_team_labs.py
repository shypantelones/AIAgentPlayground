"""Tests for multi-agent (team) labs: validation, who holds a lab, each member's prompt and relay scope, the order
stages run in, and creation. Docker, SSH, VirtualBox and the agents are mocked.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import shutil, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import vm_runner as vr  # noqa: E402

NODES = ["h1", "sw1", "h2", "r1"]


class ValidateTeamTests(unittest.TestCase):
    def test_lines_become_members(self):
        team = app.validate_team("alpha | h1,r1 | 1 | network admin\nbeta | h2 | 2 | server", NODES)
        self.assertEqual([(m["agent"], m["nodes"], m["stage"]) for m in team],
                         [("alpha", ["h1", "r1"], 1), ("beta", ["h2"], 2)])
        self.assertEqual(team[0]["brief"], "network admin")

    def test_a_list_of_dicts_is_accepted_too(self):
        team = app.validate_team([{"agent": "Alpha", "nodes": ["h1"], "stage": 1, "brief": "client"}], NODES)
        self.assertEqual(team[0]["agent"], "alpha")

    def test_brief_may_contain_a_bar(self):
        team = app.validate_team("alpha | h1 | 1 | route a | b", NODES)
        self.assertEqual(team[0]["brief"], "route a | b")

    def test_each_problem_is_named(self):
        cases = [
            ("alpha | h1 | 1", "write it as"),
            ("alpha | h9 | 1 | x", "no node called 'h9'"),
            ("alpha | h1 | two | x", "has to be a number"),
            ("alpha | h1 | 12 | x", "1 to 9"),
            ("alpha | h1 | 1 |  ", "needs a brief"),
            ("alpha |  | 1 | x", "at least one node"),
            ("alpha | h1 | 1 | x\nalpha | h2 | 1 | y", "once"),
            ("", "at least one member"),
        ]
        for raw, msg in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, msg):
                app.validate_team(raw, NODES)

    def test_a_team_is_at_most_six(self):
        raw = "\n".join(f"a{i} | h1 | 1 | x" for i in range(app.TEAM_MAX + 1))
        with self.assertRaisesRegex(ValueError, "at most"):
            app.validate_team(raw, NODES)


class HoldersTests(unittest.TestCase):
    def test_every_team_member_holds_the_lab_and_a_single_agent_lab_still_does(self):
        team_lab = {"id": "t1", "agent": None, "state": "working", "team": [{"agent": "alpha"}, {"agent": "beta"}]}
        single = {"id": "s1", "agent": "gamma", "state": "working", "team": None}
        self.assertEqual(app.lab_agents(team_lab), ["alpha", "beta"])
        self.assertTrue(app.agent_holds_lab(team_lab, "beta"))
        self.assertFalse(app.agent_holds_lab(team_lab, "delta"))
        self.assertTrue(app.agent_holds_lab(single, "gamma"))
        self.assertEqual(app.lab_agents(single), ["gamma"])


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.r = {"nodes": {"h1": {"role": "host"}, "r1": {"role": "router"}, "h2": {"role": "host"}},
                  "custom_prompt": "make h1 reach h2", "egress": {"port": 62500}, "intents": []}
        self.team = [{"agent": "alpha", "nodes": ["h1", "r1"], "stage": 1, "brief": "network admin"},
                     {"agent": "beta", "nodes": ["h2"], "stage": 2, "brief": "web server"}]

    def test_a_member_sees_its_own_nodes_and_not_the_others(self):
        text = app.member_prompt(self.r, None, self.team[0], self.team)
        self.assertIn("./vmrun-h1", text)
        self.assertIn("./vmrun-r1", text)
        self.assertNotIn("./vmrun-h2", text)
        self.assertIn("beta (h2): web server", text)        # knows the other member and what it does

    def test_the_shared_goal_and_the_proxy_are_in_every_brief(self):
        text = app.member_prompt(self.r, None, self.team[1], self.team)
        self.assertIn("make h1 reach h2", text)
        self.assertIn("http://192.168.56.1:62500", text)
        self.assertIn("Your role: web server", text)


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs")
        self.patch.start()
        (self.tmp / "topo-runs").mkdir()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stage_two_starts_only_after_stage_one_has_finished(self):
        team = [{"agent": "alpha", "nodes": ["h1"], "stage": 1, "brief": "a"},
                {"agent": "gamma", "nodes": ["h1"], "stage": 1, "brief": "a2"},
                {"agent": "beta", "nodes": ["h2"], "stage": 2, "brief": "b"}]
        r = {"id": "st1", "team": team, "nodes": {}, "intents": [], "state": "queued"}
        log, lock = [], threading.Lock()

        def member(r_, topo, ports, priv, m, team_):
            with lock:
                log.append(("start", m["agent"]))
            time.sleep(0.05)
            with lock:
                log.append(("end", m["agent"]))
            return None

        with mock.patch.object(app, "topo_member_turn", side_effect=member):
            err = app.topo_team_phase(r, None, {}, Path("unused"), None, lambda: False)
        self.assertIsNone(err)
        beta_start = log.index(("start", "beta"))
        self.assertTrue(all(log.index(("end", a)) < beta_start for a in ("alpha", "gamma")), log)
        self.assertLess(log.index(("start", "alpha")), log.index(("end", "gamma")))   # stage 1 really overlapped

    def test_one_failing_member_is_reported_and_the_others_still_run(self):
        team = [{"agent": "alpha", "nodes": ["h1"], "stage": 1, "brief": "a"},
                {"agent": "beta", "nodes": ["h2"], "stage": 1, "brief": "b"}]
        r = {"id": "st2", "team": team, "nodes": {}, "intents": [], "state": "queued"}
        ran = []

        def member(r_, topo, ports, priv, m, team_):
            ran.append(m["agent"])
            return "alpha: its turn failed (see transcript)" if m["agent"] == "alpha" else None

        with mock.patch.object(app, "topo_member_turn", side_effect=member):
            err = app.topo_team_phase(r, None, {}, Path("unused"), None, lambda: False)
        self.assertEqual(sorted(ran), ["alpha", "beta"])
        self.assertEqual(err, "alpha: its turn failed (see transcript)")


class MemberRelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.priv = self.tmp / "id_ed25519"
        self.priv.write_text("key")
        # Records are saved while these run: keep them out of the real data folder.
        (self.tmp / "topo-runs").mkdir()
        self.patches = [mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs"),
                        # no real agent is needed: the compose project and env file are only names here
                        mock.patch.object(app, "env_file", return_value=self.tmp / "agent.env"),
                        mock.patch.object(app, "proj", return_value="proj"),
                        # the member's key goes onto its nodes over ssh: not in these tests
                        mock.patch.object(app.vr, "ssh_script", return_value=(0, "", ""))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_member_gets_a_relay_and_wrappers_for_its_nodes_only(self):
        topo = vr.get_topology("s1h2")                      # nodes h1, sw1, h2
        m = {"agent": "alpha", "nodes": ["h1"], "stage": 1, "brief": "client", "chat": "vmtopo-x-alpha"}
        r = {"id": "x", "nodes": {"h1": {"role": "host"}, "sw1": {"role": "switch"}, "h2": {"role": "host"}},
             "custom_prompt": "go", "egress": {}, "intents": []}
        ports = {"h1": 62200, "sw1": 62201, "h2": 62202}
        with mock.patch.object(app, "dc", return_value=(0, "", "")) as dc, \
                mock.patch.object(app, "run", return_value=(0, "", "")) as docker, \
                mock.patch.object(app, "topo_agent_turn", return_value={"ok": True}) as turn, \
                mock.patch.object(app, "detach_topo_agent"):
            err = app.topo_member_turn(r, topo, ports, self.priv, m, [m])
        self.assertIsNone(err)
        written = [c for c in dc.call_args_list if "cat > /home/node/.openclaw/workspace/vmrun-" in " ".join(c.args)]
        self.assertEqual(len(written), 1)                   # one wrapper: only the member's own node
        relay_env = docker.call_args.kwargs["env"]["VM_RELAY_TOPO_CMD"]
        self.assertIn(f"TCP:host.docker.internal:62200", relay_env)
        self.assertNotIn("62201", relay_env)
        self.assertNotIn("62202", relay_env)
        self.assertEqual(turn.call_args.kwargs["member"], m)

    def test_a_member_whose_relay_fails_is_detached_and_reported(self):
        topo = vr.get_topology("s1h2")
        m = {"agent": "alpha", "nodes": ["h1"], "stage": 1, "brief": "client", "chat": "c"}
        r = {"id": "y", "nodes": {"h1": {"role": "host"}}, "custom_prompt": "go", "egress": {}, "intents": []}
        with mock.patch.object(app, "dc", return_value=(0, "", "")), \
                mock.patch.object(app, "run", return_value=(1, "", "boom")), \
                mock.patch.object(app, "detach_topo_agent") as detach:
            err = app.topo_member_turn(r, topo, {"h1": 62200}, self.priv, m, [m])
        self.assertIn("could not attach its relay", err)
        detach.assert_called_once_with("alpha")


class CreateTeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "topo-runs").mkdir()
        self.patches = [mock.patch.object(app, "TOPOR_DIR", self.tmp / "topo-runs"),
                        mock.patch.object(app, "load_meta", return_value={}),
                        mock.patch.object(app, "agent_running", return_value=True),
                        mock.patch.object(app, "agent_model_id", return_value="anthropic/claude-haiku-4-5")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        app.TOPO_RUNS.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_team_lab_records_each_member_with_its_own_chat(self):
        with mock.patch.object(app.threading, "Thread") as thread:
            rid = app.create_topo_run({"topology_id": "r1s2h2", "custom_prompt": "make it work", "team":
                                       "alpha | h1,sw1 | 1 | network admin\nbeta | h2 | 2 | server"})
        r = app.TOPO_RUNS[rid]
        self.assertIsNone(r["agent"])
        self.assertEqual([m["chat"] for m in r["team"]], [f"vmtopo-{rid}-alpha", f"vmtopo-{rid}-beta"])
        thread.assert_called_once()

    def test_a_team_and_a_single_agent_are_refused_together(self):
        with self.assertRaisesRegex(ValueError, "takes its members"):
            app.create_topo_run({"topology_id": "r1s2h2", "custom_prompt": "x", "agent": "alpha",
                                 "team": "beta | h2 | 1 | server"})

    def test_a_team_needs_a_goal(self):
        with self.assertRaisesRegex(ValueError, "shared goal"):
            app.create_topo_run({"topology_id": "r1s2h2", "team": "beta | h2 | 1 | server"})


if __name__ == "__main__":
    unittest.main()
