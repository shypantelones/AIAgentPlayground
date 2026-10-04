"""Tests for lab intents (lab_intents.py): parsing, the checks each intent runs from its source node, and the SSH wiring
in app.py. No VM is needed: the node runner is a fake that answers from a small table.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import lab_intents as li  # noqa: E402

NODES = ["h1", "h2", "r1", "r2", "web1", "sw1"]

# `ip -4 -o addr show` output as each node would print it: the setup NIC (10.0.2.x) must be ignored.
ADDR = {
    "h1": "2: enp0s3    inet 10.0.2.15/24 brd 10.0.2.255 scope global\n3: enp0s8    inet 10.1.0.2/24 scope global",
    "h2": "3: enp0s8    inet 10.2.0.2/24 scope global",
    "r1": "3: enp0s8    inet 10.1.0.1/24\n4: enp0s9    inet 10.9.0.1/30\n1: lo    inet 127.0.0.1/8",
    "r2": "3: enp0s8    inet 10.9.0.2/30\n4: enp0s9    inet 10.2.0.1/24",
    "web1": "3: enp0s8    inet 10.2.0.10/24",
}


def fake_runner(results=None, unreachable=(), ):
    """A run_on(node, command, timeout) for tests. `results` maps (node, command substring) -> (rc, out)."""
    results = results or {}
    calls = []

    def run_on(node, command, timeout):
        calls.append((node, command))
        if node in unreachable:
            raise OSError(f"{node} is not answering")
        if command == "ip -4 -o addr show":
            return 0, ADDR.get(node, "")
        for (n, needle), answer in results.items():
            if n == node and needle in command:
                return answer
        return 1, ""
    run_on.calls = calls
    return run_on


class ParseTests(unittest.TestCase):
    def test_reach_and_block_lines(self):
        ints = li.parse_intents(["h1 -> h2 icmp reach", "h2 -> web1 tcp/22 block"], NODES)
        self.assertEqual(ints[0]["kind"], "reach")
        self.assertEqual((ints[0]["src"], ints[0]["dst"], ints[0]["proto"], ints[0]["port"]), ("h1", "h2", "icmp", None))
        self.assertEqual((ints[1]["kind"], ints[1]["proto"], ints[1]["port"], ints[1]["verdict"]), ("block", "tcp", 22, "block"))
        self.assertEqual(ints[1]["text"], "h2 -> web1 tcp/22 block")

    def test_path_line_keeps_order_of_via_nodes(self):
        (i,) = li.parse_intents(["h1 -> h2 path via r1, r2"], NODES)
        self.assertEqual(i["kind"], "path")
        self.assertEqual(i["via"], ["r1", "r2"])
        self.assertEqual(i["text"], "h1 -> h2 path via r1, r2")

    def test_blank_lines_and_comments_are_skipped_and_spacing_is_normalised(self):
        ints = li.parse_intents("# the core\n\n  h1   ->   h2  icmp   reach  \n", NODES)
        self.assertEqual([i["text"] for i in ints], ["h1 -> h2 icmp reach"])

    def test_ip_address_is_a_valid_destination(self):
        (i,) = li.parse_intents(["h1 -> 10.2.0.10 icmp reach"], NODES)
        self.assertEqual(i["dst"], "10.2.0.10")

    def test_unknown_node_is_named_in_the_error(self):
        with self.assertRaisesRegex(ValueError, "no node called 'lb9'"):
            li.parse_intents(["h1 -> lb9 icmp reach"], NODES)

    def test_bad_grammar_explains_what_is_accepted(self):
        with self.assertRaisesRegex(ValueError, "write it as"):
            li.parse_intent("h1 h2 icmp reach", NODES)
        with self.assertRaisesRegex(ValueError, "udp isn't supported"):
            li.parse_intent("h1 -> h2 udp/53 reach", NODES)
        with self.assertRaisesRegex(ValueError, "out of range"):
            li.parse_intent("h1 -> h2 tcp/70000 reach", NODES)
        with self.assertRaisesRegex(ValueError, "at least one node"):
            li.parse_intent("h1 -> h2 path via", NODES)

    def test_subnet_destination_is_refused_for_now(self):
        with self.assertRaisesRegex(ValueError, "not a subnet"):
            li.parse_intent("h1 -> 10.2.0.0/24 path via r1", NODES)

    def test_source_must_be_a_node(self):
        with self.assertRaisesRegex(ValueError, "source has to be a node"):
            li.parse_intent("10.1.0.2 -> h2 icmp reach", NODES)

    def test_too_many_intents_is_refused(self):
        with self.assertRaisesRegex(ValueError, "at most"):
            li.parse_intents(["h1 -> h2 icmp reach"] * (li.MAX_INTENTS + 1), NODES)

    def test_non_text_line_is_refused_not_dropped(self):
        with self.assertRaisesRegex(ValueError, "line of text"):
            li.parse_intents([{"src": "h1"}], NODES)

    def test_no_node_list_skips_name_check(self):
        (i,) = li.parse_intents(["anything -> else icmp reach"])
        self.assertEqual(i["dst"], "else")


class AddressTests(unittest.TestCase):
    def test_setup_network_loopback_and_link_local_are_left_out(self):
        self.assertEqual(li.parse_addresses(ADDR["h1"]), ["10.1.0.2"])
        self.assertEqual(li.parse_addresses(ADDR["r1"]), ["10.1.0.1", "10.9.0.1"])
        self.assertEqual(li.parse_addresses("inet 169.254.1.1/16 scope link"), [])

    def test_traceroute_hops_in_order_skip_silent_hops(self):
        out = ("traceroute to 10.2.0.10 (10.2.0.10), 12 hops max\n"
               " 1  10.1.0.1  0.5 ms\n 2  *\n 3  10.9.0.2  1.2 ms\n 4  10.2.0.10  2.0 ms\n")
        self.assertEqual(li.parse_traceroute(out), ["10.1.0.1", "10.9.0.2", "10.2.0.10"])


class CheckTests(unittest.TestCase):
    def test_reach_passes_when_the_probe_gets_through(self):
        (i,) = li.parse_intents(["h1 -> h2 icmp reach"], NODES)
        run_on = fake_runner({("h1", "ping -c 2 -W 2 -q 10.2.0.2"): (0, "")})
        results = li.run_intent_checks([i], NODES, run_on)
        self.assertTrue(results[0]["passed"])
        self.assertIn("got through", results[0]["detail"])

    def test_reach_fails_when_the_probe_fails(self):
        (i,) = li.parse_intents(["h1 -> h2 icmp reach"], NODES)
        results = li.run_intent_checks([i], NODES, fake_runner())
        self.assertFalse(results[0]["passed"])

    def test_block_passes_only_when_the_probe_fails(self):
        (blocked,) = li.parse_intents(["h2 -> web1 tcp/22 block"], NODES)
        self.assertTrue(li.run_intent_checks([blocked], NODES, fake_runner())[0]["passed"])
        open_port = fake_runner({("h2", "dev/tcp/10.2.0.10/22"): (0, "")})
        self.assertFalse(li.run_intent_checks([blocked], NODES, open_port)[0]["passed"])

    def test_tcp_probe_runs_from_the_source_node_to_the_port(self):
        (i,) = li.parse_intents(["h2 -> web1 tcp/80 reach"], NODES)
        run_on = fake_runner({("h2", "dev/tcp/10.2.0.10/80"): (0, "")})
        self.assertTrue(li.run_intent_checks([i], NODES, run_on)[0]["passed"])
        self.assertEqual(run_on.calls[-1][0], "h2")

    def test_node_destination_uses_any_of_its_addresses(self):
        (i,) = li.parse_intents(["h1 -> r2 icmp reach"], NODES)
        run_on = fake_runner({("h1", "ping -c 2 -W 2 -q 10.9.0.2"): (0, "")})
        self.assertTrue(li.run_intent_checks([i], NODES, run_on)[0]["passed"])
        probe = [c for n, c in run_on.calls if n == "h1" and c.startswith("ping")][0]
        self.assertIn("10.9.0.2", probe)

    def test_destination_without_an_address_fails_with_a_reason(self):
        (i,) = li.parse_intents(["h1 -> sw1 icmp reach"], NODES)
        results = li.run_intent_checks([i], NODES, fake_runner())
        self.assertFalse(results[0]["passed"])
        self.assertIn("no lab address", results[0]["detail"])

    def test_unreachable_source_fails_that_intent_and_the_rest_still_run(self):
        a, b = li.parse_intents(["h1 -> h2 icmp reach", "h2 -> web1 tcp/22 block"], NODES)
        run_on = fake_runner(unreachable=("h1",))
        results = li.run_intent_checks([a, b], NODES, run_on)
        self.assertFalse(results[0]["passed"])
        self.assertIn("is not answering", results[0]["detail"])
        self.assertTrue(results[1]["passed"])

    def test_path_passes_when_via_nodes_appear_in_order(self):
        (i,) = li.parse_intents(["h1 -> 10.2.0.10 path via r1, r2"], NODES)
        trace = "traceroute to 10.2.0.10\n 1  10.1.0.1 0.4 ms\n 2  10.9.0.2 1.0 ms\n 3  10.2.0.10 2.0 ms\n"
        run_on = fake_runner({("h1", "traceroute"): (0, trace)})
        results = li.run_intent_checks([i], NODES, run_on)
        self.assertTrue(results[0]["passed"], results)
        self.assertIn("10.1.0.1 > 10.9.0.2 > 10.2.0.10", results[0]["detail"])

    def test_path_fails_when_a_via_node_is_skipped(self):
        (i,) = li.parse_intents(["h1 -> 10.2.0.10 path via r1, r2"], NODES)
        trace = " 1  10.1.0.1 0.4 ms\n 2  10.2.0.10 2.0 ms\n"       # r1 routes straight to the host, r2 never seen
        results = li.run_intent_checks([i], NODES, fake_runner({("h1", "traceroute"): (0, trace)}))
        self.assertFalse(results[0]["passed"])
        self.assertIn("does not go through r2", results[0]["detail"])

    def test_path_fails_when_the_destination_is_not_reached(self):
        (i,) = li.parse_intents(["h1 -> 10.2.0.10 path via r1"], NODES)
        trace = " 1  10.1.0.1 0.4 ms\n 2  *\n"
        results = li.run_intent_checks([i], NODES, fake_runner({("h1", "traceroute"): (0, trace)}))
        self.assertFalse(results[0]["passed"])
        self.assertIn("did not reach 10.2.0.10", results[0]["detail"])

    def test_path_order_is_checked_not_just_membership(self):
        (i,) = li.parse_intents(["h1 -> 10.2.0.10 path via r2, r1"], NODES)
        trace = " 1  10.1.0.1 0.4 ms\n 2  10.9.0.2 1.0 ms\n 3  10.2.0.10 2.0 ms\n"
        results = li.run_intent_checks([i], NODES, fake_runner({("h1", "traceroute"): (0, trace)}))
        self.assertFalse(results[0]["passed"])

    def test_summary_for_prompt_is_one_line_per_intent(self):
        ints = li.parse_intents(["h1 -> h2 icmp reach", "h2 -> web1 tcp/22 block"], NODES)
        self.assertEqual(li.summary_for_prompt(ints), "- h1 -> h2 icmp reach\n- h2 -> web1 tcp/22 block")


class CreateLabTests(unittest.TestCase):
    def test_a_bad_intent_stops_the_lab_before_any_vm_is_built(self):
        # s1h2 has nodes h1, sw1, h2: a typo'd node must fail at creation, not as a red X after the build.
        with mock.patch.object(app.threading, "Thread") as thread:
            with self.assertRaisesRegex(ValueError, "no node called 'h9'"):
                app.create_topo_run({"topology_id": "s1h2", "intents": "h1 -> h9 icmp reach"})
        thread.assert_not_called()


class SshWiringTests(unittest.TestCase):
    def test_ssh_failure_is_an_unreachable_node_not_a_failed_probe(self):
        r = {"nodes": {"h1": {"ssh_port": 2201}}}
        with mock.patch.object(app.vr, "ssh_run", return_value=(255, "", "Connection refused")):
            run_on = app.intent_run_on(r, Path("key"))
            with self.assertRaisesRegex(OSError, "Connection refused"):
                run_on("h1", "ping -c 2 -W 2 -q 10.2.0.2", 15)

    def test_probe_exit_status_comes_back_as_is(self):
        r = {"nodes": {"h1": {"ssh_port": 2201}}}
        with mock.patch.object(app.vr, "ssh_run", return_value=(1, "", "")) as ssh:
            self.assertEqual(app.intent_run_on(r, Path("key"))("h1", "ping -c 2 -W 2 -q 10.2.0.2", 15), (1, ""))
        ssh.assert_called_once_with(2201, Path("key"), "ping -c 2 -W 2 -q 10.2.0.2", timeout=15)

    def test_intents_score_passes_only_when_every_intent_passes(self):
        ok = [{"text": "a", "passed": True, "detail": "x"}, {"text": "b", "passed": True, "detail": "y"}]
        bad = ok + [{"text": "c", "passed": False, "detail": "z"}]
        self.assertTrue(app.intents_score(ok)["passed"])
        score = app.intents_score(bad)
        self.assertFalse(score["passed"])
        self.assertIn("FAIL  c  (z)", score["output"])

    def test_feedback_names_each_failing_intent(self):
        text = app.intent_feedback([{"text": "h2 -> web1 tcp/22 block", "passed": False, "detail": "probe got through"}])
        self.assertIn("- h2 -> web1 tcp/22 block: probe got through", text)


if __name__ == "__main__":
    unittest.main()
