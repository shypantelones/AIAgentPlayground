"""Tests for "lab from a diagram": vm_runner's spec -> lab file conversion and draw.io summary, and app.py's draft
flow (docker compose and agent turns mocked out).
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import base64, json, shutil, sys, tempfile, time, unittest, urllib.parse, zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import vm_runner as vr  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32

SPEC = {"title": "Two subnets", "notes": ["OSPF area 0 between r1 and r2"],
        "nodes": [{"name": "R1", "role": "router", "routes": ["10.3.0.0/24 via 10.2.0.2"]},
                  {"name": "sw1", "role": "switch"},
                  {"name": "h1", "role": "pc", "routes": ["0.0.0.0/0 via 10.1.0.1"]},
                  {"name": "r2", "role": "router"}],
        "links": [{"a": "r1", "b": "sw1", "a_ip": "10.1.0.1/24"},
                  {"a": "h1", "b": "sw1", "a_ip": "10.1.0.10/24"},
                  {"a": "r1", "b": "r2", "a_ip": "10.2.0.1/30", "b_ip": "10.2.0.2/30"}]}


def spec(**changes):
    s = json.loads(json.dumps(SPEC))
    s.update(changes)
    return s


class SpecToLabfile(unittest.TestCase):
    def test_configured_lab(self):
        lf, diagram, warnings = vr.spec_to_labfile(SPEC, source={"diagram": "net.png"})
        self.assertEqual(warnings, [])
        self.assertEqual([n["name"] for n in lf["topology"]["nodes"]], ["r1", "sw1", "h1", "r2"])
        self.assertEqual(lf["topology"]["nodes"][2]["role"], "host")            # "pc" is a host
        r1 = lf["configs"]["r1"]
        self.assertEqual(r1["addresses"], "enp0s8 UP 10.1.0.1/24\nenp0s9 UP 10.2.0.1/30\n")
        self.assertIn("10.3.0.0/24 via 10.2.0.2 dev enp0s9", r1["routes"])
        self.assertIn("net.ipv4.ip_forward = 1", r1["forwarding"])
        self.assertIn("default via 10.1.0.1 dev enp0s8", lf["configs"]["h1"]["routes"])
        self.assertNotIn("forwarding", lf["configs"]["h1"])                     # hosts don't route
        self.assertNotIn("sw1", lf["configs"])                                  # switches are set up by the lab
        self.assertEqual(lf["source"], {"diagram": "net.png", "notes": ["OSPF area 0 between r1 and r2"], "warnings": []})
        self.assertEqual(diagram["addresses"]["r1"], {"enp0s8": ["10.1.0.1/24"], "enp0s9": ["10.2.0.1/30"]})
        self.assertEqual(vr.parse_labfile(lf)[0], "Two subnets")

    def test_bare_lab_keeps_only_the_topology(self):
        lf, diagram, _ = vr.spec_to_labfile(SPEC, configured=False)
        self.assertEqual(lf["configs"], {})
        self.assertEqual(len(lf["topology"]["links"]), 3)
        self.assertEqual(diagram["addresses"], {})

    def test_apply_script_and_faithful_rebuild_check(self):
        lf, _, _ = vr.spec_to_labfile(SPEC)
        script, skipped = vr.render_apply_script(lf["configs"]["r1"])
        self.assertEqual(skipped, [])
        self.assertIn("ip addr replace 10.2.0.1/30 dev enp0s9", script)
        self.assertIn("ip route replace 10.3.0.0/24 via 10.2.0.2 dev enp0s9", script)
        self.assertNotIn("proto", script)                                       # kernel routes come by themselves
        self.assertIn("net.ipv4.ip_forward=1", script)
        live = {"addresses": "lo UNKNOWN 127.0.0.1/8 ::1/128\nenp0s3 UP 10.0.2.15/24 fe80::1/64\n"
                             "enp0s8 UP 10.1.0.1/24 fe80::2/64\nenp0s9 UP 10.2.0.1/30 fe80::3/64\n",
                "routes": "default via 10.0.2.2 dev enp0s3 proto dhcp src 10.0.2.15 metric 100\n"
                          "10.0.2.0/24 dev enp0s3 proto kernel scope link src 10.0.2.15 metric 100\n"
                          "10.1.0.0/24 dev enp0s8 proto kernel scope link src 10.1.0.1\n"
                          "10.2.0.0/30 dev enp0s9 proto kernel scope link src 10.2.0.1\n"
                          "10.3.0.0/24 via 10.2.0.2 dev enp0s9\n# ipv6\n::1 dev lo proto kernel metric 256 pref medium\n"
                          "fe80::/64 dev enp0s8 proto kernel metric 256 pref medium\n",
                "forwarding": "net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 0\n"}
        self.assertEqual(vr.config_mismatches(lf["configs"]["r1"], live), [])

    def test_switch_and_upstream_addresses_are_dropped_with_a_warning(self):
        s = spec(nodes=SPEC["nodes"] + [{"name": "isp", "role": "internet"}])
        s["links"] = SPEC["links"] + [{"a": "r2", "b": "isp", "a_ip": "198.51.100.2/30", "b_ip": "198.51.100.9/30"}]
        s["links"][0]["b_ip"] = "10.1.0.2/24"
        lf, _, warnings = vr.spec_to_labfile(s)
        self.assertEqual(lf["topology"]["nodes"][-1]["role"], "upstream")
        self.assertEqual(len(warnings), 2)
        self.assertNotIn("isp", lf["configs"])
        self.assertEqual(lf["source"]["warnings"], warnings)

    def test_errors_are_messages_for_the_agent(self):
        cases = {
            "unknown role": spec(nodes=[{"name": "r1", "role": "toaster"}] + SPEC["nodes"][1:]),
            "isn't in nodes": spec(links=SPEC["links"] + [{"a": "r1", "b": "r9"}]),
            "10.0.2.0/24": spec(links=[dict(SPEC["links"][0], a_ip="10.0.2.1/24")] + SPEC["links"][1:]),
            "IPv4 only": spec(links=[dict(SPEC["links"][0], a_ip="2001:db8::1/64")] + SPEC["links"][1:]),
            "prefix length": spec(links=[dict(SPEC["links"][0], a_ip="banana")] + SPEC["links"][1:]),
            "isn't on any": spec(nodes=[dict(SPEC["nodes"][0], routes=["10.9.0.0/24 via 10.8.0.1"])] + SPEC["nodes"][1:]),
            "should look like": spec(nodes=[dict(SPEC["nodes"][0], routes=["to 10.9.0.0/24"])] + SPEC["nodes"][1:]),
            "bad node name": spec(nodes=[{"name": "1r", "role": "router"}] + SPEC["nodes"][1:]),
            "2 to 12": spec(nodes=[{"name": f"h{i}", "role": "host"} for i in range(13)]),
            "no links": spec(links=[]),
            '"nodes" and "links"': {"nodes": "r1"},
        }
        for needle, s in cases.items():
            with self.subTest(needle):
                with self.assertRaises(ValueError) as cm:
                    vr.spec_to_labfile(s)
                self.assertIn(needle, str(cm.exception))

    def test_too_many_links_on_one_node(self):
        nodes = [{"name": "sw1", "role": "switch"}] + [{"name": f"h{i}", "role": "host"} for i in range(8)]
        with self.assertRaisesRegex(ValueError, "more than 7 links"):
            vr.spec_to_labfile({"nodes": nodes, "links": [{"a": f"h{i}", "b": "sw1"} for i in range(8)]})


DRAWIO = ('<mxfile><diagram id="a"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
          '<mxCell id="2" value="R1&lt;br&gt;10.1.0.1" style="shape=mxgraph.cisco.routers.router;html=1" vertex="1" parent="1"/>'
          '<mxCell id="3" value="PC1" style="rounded=1" vertex="1" parent="1"/>'
          '<mxCell id="4" value="Gi0/0" edge="1" source="2" target="3" parent="1"/></root></mxGraphModel></diagram></mxfile>')


class DrawioSummary(unittest.TestCase):
    WANT = "node R1 10.1.0.1 [router]\nnode PC1\nlink R1 10.1.0.1 -- PC1: Gi0/0"

    def test_plain(self):
        self.assertEqual(vr.drawio_summary(DRAWIO), self.WANT)

    def test_compressed(self):
        inner = DRAWIO[DRAWIO.index("<mxGraphModel"):DRAWIO.index("</diagram>")]
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        packed = base64.b64encode(c.compress(urllib.parse.quote(inner).encode()) + c.flush()).decode()
        self.assertEqual(vr.drawio_summary(f'<mxfile><diagram id="a">{packed}</diagram></mxfile>'), self.WANT)

    def test_not_drawio(self):
        self.assertIsNone(vr.drawio_summary("graph LR\n r1 --- sw1"))
        self.assertIsNone(vr.drawio_summary("<mxfile><diagram>not base64!</diagram></mxfile>"))


class ModelPatch(unittest.TestCase):
    def test_cloud_models_take_images_local_ones_dont(self):
        cloud = app.model_patch({"backend": "cloud", "provider": "anthropic", "cloud_model": "claude-haiku-4-5"})
        self.assertIn('input: ["text", "image"]', cloud)
        self.assertIn('input: ["text"]', app.model_patch({"backend": "local", "model": "m"}))


class LabDrafts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.calls, self.prompts = [], []
        self.workspace_spec = json.dumps(SPEC)
        self.reply = {"reply": "4 nodes, 3 links.", "ok": True}
        self.vision = True
        self.metas = {"cloudy": {"backend": "cloud", "provider": "anthropic", "cloud_model": "claude-haiku-4-5"},
                      "loco": {"backend": "local", "model": "qwen"}}
        self.patches = [
            mock.patch.object(app, "DRAFT_DIR", self.tmp),
            mock.patch.object(app, "DRAFTS", {}),
            mock.patch.object(app, "agent_running", lambda n: True),
            mock.patch.object(app, "load_meta", lambda n: self.metas[n]),
            mock.patch.object(app, "dc", self.fake_dc),
            mock.patch.object(app, "run_turn", self.fake_run_turn),
            mock.patch.object(app, "wait_gateway", lambda *a, **k: True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_dc(self, name, *args, input=None, timeout=120):
        self.calls.append((name, args, input))
        if args[-1:] == (f"{app.WORKSPACE}/{app.DRAFT_SPEC_FILE}",) and "cat" in args:
            return (0, self.workspace_spec, "") if self.workspace_spec is not None else (1, "", "No such file")
        if "get" in args:
            return 0, '[{"input": ["text"%s]}]' % (', "image"' if self.vision else ""), ""
        return 0, "", ""

    def fake_run_turn(self, name, chat_id, message, meta=None, log=lambda s: None, title=None, timeout=700):
        self.prompts.append(message)
        return dict(self.reply)

    def wait(self, did):
        end = time.time() + 5
        while app.DRAFTS[did]["state"] == "drafting" and time.time() < end:
            time.sleep(0.01)
        return app.DRAFTS[did]

    def create(self, **form):
        f = {"agent": "cloudy", "context": "R boxes are routers", "diagram": {"name": "net.drawio", "text": DRAWIO}}
        f.update(form)
        return app.create_lab_draft(f)

    def test_text_diagram_becomes_a_lab_file(self):
        d = self.wait(self.create())
        self.assertEqual(d["state"], "ready", d["error"])
        self.assertEqual(d["title"], "Two subnets")
        self.assertEqual(d["notes"], ["OSPF area 0 between r1 and r2"])
        self.assertEqual(d["labfile"]["source"]["drafted_by"], "anthropic/claude-haiku-4-5")
        prompt = self.prompts[0]
        self.assertIn("node R1 10.1.0.1 [router]", prompt)                      # the summary, not the XML
        self.assertNotIn("mxCell", prompt)
        self.assertIn("User notes: R boxes are routers", prompt)
        self.assertNotIn("Topology only", prompt)
        self.assertLess(len(prompt), 1400)                                       # token conscious
        self.assertEqual(app.draft_view(d)["state"], "ready")
        self.assertIn("labfile", app.draft_view(d, full=True))

    def test_bare_mode_asks_for_the_topology_only(self):
        d = self.wait(self.create(configured=False, diagram={"name": "n.mmd", "text": "graph LR\n r1 --- sw1"}))
        self.assertIn("Topology only", self.prompts[0])
        self.assertIn("graph LR", self.prompts[0])
        self.assertEqual(d["labfile"]["configs"], {})

    def test_spec_from_the_reply_when_no_file(self):
        self.workspace_spec = None
        self.reply = {"reply": "Here:\n```json\n" + json.dumps(SPEC) + "\n```", "ok": True}
        self.assertEqual(self.wait(self.create())["state"], "ready")

    def test_bad_spec_is_sent_back_on_revise(self):
        self.workspace_spec = json.dumps(spec(nodes=[{"name": "r1", "role": "toaster"}] + SPEC["nodes"][1:]))
        did = self.create()
        d = self.wait(did)
        self.assertEqual((d["state"], d["fixable"], d["turns"]), ("failed", True, 1))
        self.workspace_spec = json.dumps(SPEC)
        app.revise_lab_draft(did, "")
        self.assertEqual(self.wait(did)["state"], "ready")
        self.assertIn("unknown role 'toaster'", self.prompts[1])
        self.assertNotIn("Reverse-engineer", self.prompts[1])                   # the agent already has the diagram
        app.revise_lab_draft(did, "r2 is a firewall")
        self.wait(did)
        self.assertTrue(self.prompts[2].startswith("r2 is a firewall\nUpdate lab-draft.json"))
        with self.assertRaisesRegex(ValueError, "say what to change"):
            app.revise_lab_draft(did, " ")

    def test_a_draft_that_never_reached_the_agent_is_retried_from_the_start(self):
        self.reply = {"reply": "(no reply) boom", "ok": False}
        did = self.create()
        d = self.wait(did)
        self.assertEqual((d["state"], d["fixable"], d["turns"]), ("failed", False, 0))
        self.reply = {"reply": "ok", "ok": True}
        app.revise_lab_draft(did, "")
        self.assertEqual(self.wait(did)["state"], "ready")
        self.assertEqual(self.prompts[1], self.prompts[0])

    def test_image_goes_to_the_workspace_and_vision_is_turned_on(self):
        self.vision = False
        did = self.create(diagram={"name": "net.png", "data": base64.b64encode(PNG).decode()})
        d = self.wait(did)
        self.assertEqual(d["state"], "ready", d["error"])
        self.assertIn(f"./diagram-{did}.png (read it)", self.prompts[0])
        patched = [c for c in self.calls if "patch" in c[1]]
        self.assertEqual(len(patched), 1)
        self.assertIn('"image"', patched[0][2])
        copy = next(c for c in self.calls if "sh" in c[1])
        self.assertIn(f"diagram-{did}.png", copy[1][-1])
        self.assertEqual(base64.b64decode(copy[2]), PNG)
        self.assertEqual((self.tmp / did / f"diagram-{did}.png").read_bytes(), PNG)
        self.calls.clear()
        self.vision = True
        self.wait(self.create(diagram={"name": "net.png", "data": base64.b64encode(PNG).decode()}))
        self.assertFalse([c for c in self.calls if "patch" in c[1]])          # already on: no gateway restart
        app.delete_lab_draft(did)
        self.assertFalse((self.tmp / did).exists())
        self.assertTrue(any("rm" in c[1] and c[1][-1].endswith(f"diagram-{did}.png") for c in self.calls))

    def test_validation(self):
        png = base64.b64encode(PNG).decode()
        cases = {
            "choose the agent": dict(agent=""),
            "add notes": dict(context="  "),
            "needs a cloud agent": dict(agent="loco", diagram={"name": "n.png", "data": png}),
            "PNG, JPEG, GIF or WebP": dict(diagram={"name": "n.png", "data": base64.b64encode(b"<svg/>").decode()}),
            "too large": dict(diagram={"name": "n.txt", "text": "x" * (app.DRAFT_MAX_TEXT + 1)}),
            "empty": dict(diagram={"name": "n.txt", "text": " "}),
            "choose a diagram": dict(diagram=None),
        }
        for needle, form in cases.items():
            with self.subTest(needle):
                with self.assertRaises(ValueError) as cm:
                    self.create(**form)
                self.assertIn(needle, str(cm.exception))
        self.assertEqual(app.DRAFTS, {})

    def test_busy_drafts_and_restart(self):
        did = self.create()
        self.wait(did)
        app.DRAFTS[did]["state"] = "drafting"
        app.save_draft(app.DRAFTS[did])
        for fn in (lambda: app.revise_lab_draft(did, "x"), lambda: app.delete_lab_draft(did)):
            with self.assertRaisesRegex(ValueError, "still working"):
                fn()
        app.DRAFTS.clear()
        app.load_lab_drafts()
        self.assertEqual(app.DRAFTS[did]["state"], "failed")
        self.assertIn("restarted", app.DRAFTS[did]["error"])


if __name__ == "__main__":
    unittest.main()
