"""Tests for model_info (memory estimates, fit logic, metadata parsing) and the apply_model validation.
Run from control-panel/:  python3 -m unittest discover -s tests -v
"""
import json, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import model_info as mi  # noqa: E402

# trimmed real output of `ollama show --verbose qwen3:14b`
SHOW_TEXT = """
  Model
    architecture        qwen3
    parameters          14.8B
    context length      40960
    embedding length    5120
    quantization        Q4_K_M

  Capabilities
    completion
    tools
    thinking

  Parameters
    stop    "<|im_start|>"

  Metadata
    qwen3.attention.head_count                40
    qwen3.attention.head_count_kv             8
    qwen3.attention.key_length                128
    qwen3.attention.value_length              128
    qwen3.block_count                         40
    qwen3.context_length                      40960
    qwen3.embedding_length                    5120
"""
SHOW_JSON = {"details": {"parameter_size": "14.8B", "quantization_level": "Q4_K_M"}, "capabilities": ["completion", "tools", "thinking"],
             "model_info": {"qwen3.block_count": 40, "qwen3.attention.head_count": 40, "qwen3.attention.head_count_kv": 8,
                            "qwen3.attention.key_length": 128, "qwen3.attention.value_length": 128, "qwen3.embedding_length": 5120,
                            "general.parameter_count": 14768307200, "tokenizer.ggml.tokens": ["a", "b"]}}

GPU16 = {"ram_gb": 67.7, "vram_gb": 17.2, "gpu_name": "RTX", "unified_memory": False, "free_disk_gb": 60}
MAC32 = {"ram_gb": 34.4, "vram_gb": None, "gpu_name": None, "unified_memory": True, "free_disk_gb": 200}
CPU16 = {"ram_gb": 17.2, "vram_gb": None, "gpu_name": None, "unified_memory": False, "free_disk_gb": 100}


class ParseTests(unittest.TestCase):
    def test_text_and_json_agree(self):
        t, j = mi.parse_show_text(SHOW_TEXT), mi.parse_show_json(SHOW_JSON)
        for info in (t, j):
            self.assertEqual(info["params"], "14.8B")
            self.assertEqual(info["quant"], "Q4_K_M")
            self.assertIn("tools", info["caps"])
            self.assertEqual(mi.kv_bytes_per_token(info), 40 * 8 * (128 + 128) * 2)   # 163,840 bytes per token

    def test_kv_falls_back_to_embedding_over_heads(self):
        info = {"raw": {"block_count": 32, "attention.head_count": 32, "attention.head_count_kv": 8, "embedding_length": 4096}}
        self.assertEqual(mi.kv_bytes_per_token(info), 32 * 8 * (128 + 128) * 2)

    def test_incomplete_metadata_is_unknown_not_guessed(self):
        self.assertIsNone(mi.kv_bytes_per_token({"raw": {"block_count": 32}}))
        self.assertIsNone(mi.kv_bytes_per_token({}))

    def test_per_layer_arrays_use_largest(self):
        self.assertEqual(mi._num("[2, 8, 4]"), 8)


class EstimateTests(unittest.TestCase):
    def test_exact_estimate_matches_what_ollama_reported(self):
        # measured on the real machine: ollama ps showed ~14 GB for qwen3:14b at 32K context; estimate should be close and not low
        need = mi.need_gb_exact(9.3, mi.parse_show_text(SHOW_TEXT), 32768)
        self.assertAlmostEqual(need, 15.0, delta=0.3)
        self.assertGreaterEqual(need, 14.0)

    def test_context_scales_kv_only(self):
        info = mi.parse_show_text(SHOW_TEXT)
        self.assertLess(mi.need_gb_exact(9.3, info, 8192), mi.need_gb_exact(9.3, info, 32768))
        self.assertGreater(mi.need_gb_exact(9.3, info, 8192), 9.3)

    def test_catalog_is_sane(self):
        ctx, models = mi.load_catalog()
        self.assertEqual(ctx, 32768)
        names = [m["name"] for m in models]
        self.assertEqual(len(names), len(set(names)))
        for m in models:
            self.assertRegex(m["name"], r"^[a-z0-9._-]+:[a-z0-9._-]+$")
            self.assertGreater(m["size_gb"], 0)
            self.assertGreater(m["kv_gb_32k"], 0)
            self.assertTrue(m["tools"])                    # the catalog only lists tool-capable models
            self.assertTrue(m["note"])
        self.assertIn("qwen3:14b", names)                  # the default model is offered


class FitTests(unittest.TestCase):
    def fit(self, need, size, machine, mode, installed=False):
        return mi.assess(need, size, machine, mode, installed)

    def test_nvidia(self):
        self.assertEqual(self.fit(15.0, 9.3, GPU16, "nvidia")["fit"], "gpu")
        self.assertEqual(self.fit(20.0, 14, GPU16, "nvidia")["fit"], "partial")
        self.assertEqual(self.fit(90.0, 60, GPU16, "nvidia")["fit"], "toobig")
        self.assertEqual(self.fit(15.0, 9.3, GPU16, "nvidia")["memory_kind"], "VRAM")

    def test_cpu_mode_ignores_gpu(self):
        self.assertEqual(self.fit(8.0, 4.7, GPU16, "cpu")["fit"], "cpu")
        self.assertEqual(self.fit(8.0, 4.7, CPU16, "cpu")["fit"], "cpu")
        self.assertEqual(self.fit(15.0, 9.3, CPU16, "cpu")["fit"], "toobig")       # 15 > 60% of 17 GB

    def test_mac_unified_memory(self):
        self.assertEqual(self.fit(15.0, 9.3, MAC32, "host")["fit"], "gpu")          # 15 <= 65% of 34 GB
        self.assertEqual(self.fit(26.0, 19, MAC32, "host")["fit"], "tight")
        self.assertEqual(self.fit(40.0, 30, MAC32, "host")["fit"], "toobig")
        self.assertEqual(self.fit(15.0, 9.3, MAC32, "host")["memory_kind"], "unified memory")

    def test_disk(self):
        low = dict(GPU16, free_disk_gb=6)
        self.assertFalse(self.fit(15.0, 9.3, low, "nvidia")["disk_ok"])             # needs size + 2 GB spare
        self.assertTrue(self.fit(15.0, 9.3, low, "nvidia", installed=True)["disk_ok"])   # already on disk
        self.assertTrue(self.fit(15.0, 9.3, dict(GPU16, free_disk_gb=None), "nvidia")["disk_ok"])

    def test_unknown_when_metadata_missing(self):
        self.assertEqual(self.fit(None, 9.3, GPU16, "nvidia")["fit"], "unknown")


class DescribeTests(unittest.TestCase):
    def test_installed_uses_exact_numbers_and_flags_capabilities(self):
        d = mi.describe_installed("qwen3:14b", 9.3, mi.parse_show_text(SHOW_TEXT), GPU16, "nvidia", 32768)
        self.assertTrue(d["exact"] and d["installed"] and d["tools"] and d["thinking"])
        self.assertEqual(d["fit"], "gpu")

    def test_embedding_only_model_is_flagged(self):
        info = {"raw": {}, "caps": ["embedding"], "params": "137M", "quant": "F16"}
        self.assertTrue(mi.describe_installed("nomic-embed-text:latest", 0.3, info, GPU16, "nvidia", 32768)["embedding_only"])

    def test_catalog_entries_are_marked_estimates(self):
        _, models = mi.load_catalog()
        d = mi.describe_catalog(models[0], GPU16, "nvidia", 32768)
        self.assertFalse(d["exact"] or d["installed"])


class ApplyLocalModelTests(unittest.TestCase):
    """Switching an agent's local model: regression for OpenClaw refusing to drop entries from the model list."""

    def setUp(self):
        import tempfile, shutil, time
        from unittest import mock
        import app
        self.app, self.tmp = app, Path(tempfile.mkdtemp())
        (self.tmp / "alpha").mkdir()
        (self.tmp / "alpha" / "meta.json").write_text(json.dumps({"name": "alpha", "backend": "local", "model": "qwen3:14b", "port": 18801}))
        self.calls = []
        self.fake_dc = lambda name, *a, input=None, timeout=0: (self.calls.append((a, input)) or (0, "", ""))
        self.patches = [mock.patch.object(app, "DATA", self.tmp), mock.patch.object(app, "dc", self.fake_dc),
                        mock.patch.object(app, "ensure_shared", lambda log=None: None), mock.patch.object(app, "ensure_image", lambda log=None, image=None: None),
                        mock.patch.object(app, "wait_gateway", lambda *a, **k: True),
                        mock.patch.object(app, "ollama_list", lambda: [("qwen3:14b", 9.3), ("qwen3:1.7b", 1.4)])]
        for p in self.patches:
            p.start()
        self.shutil, self.time = shutil, time

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def run_job(self, form):
        jid = self.app.apply_model("alpha", form)
        end = self.time.time() + 5
        while not self.app.JOBS[jid]["done"] and self.time.time() < end:
            self.time.sleep(0.02)
        return self.app.JOBS[jid]

    def meta_model(self):
        return json.loads((self.tmp / "alpha" / "meta.json").read_text())["model"]

    def test_switch_replaces_the_provider_model_list_and_updates_meta(self):
        j = self.run_job({"backend": "local", "model": "qwen3:1.7b"})
        self.assertTrue(j["ok"], j["lines"])
        self.assertEqual(self.meta_model(), "qwen3:1.7b")
        patch_calls = [(a, i) for a, i in self.calls if "patch" in a]
        self.assertEqual(len(patch_calls), 1)
        args, payload = patch_calls[0]
        self.assertIn("--replace-path", args)
        self.assertEqual(args[args.index("--replace-path") + 1], "models.providers.ollama.models")
        self.assertIn('primary: "ollama/qwen3:1.7b"', payload)
        self.assertIn('id: "qwen3:1.7b"', payload)

    def test_not_downloaded_is_refused_and_nothing_changes(self):
        j = self.run_job({"backend": "local", "model": "qwen3:32b"})
        self.assertFalse(j["ok"])
        self.assertIn("not downloaded", " ".join(j["lines"]))
        self.assertEqual(self.meta_model(), "qwen3:14b")
        self.assertEqual(self.calls, [])                       # no containers touched

    def test_bad_name_rejected_up_front(self):
        with self.assertRaises(ValueError):
            self.app.apply_model("alpha", {"backend": "local", "model": "bad name; echo hi"})


if __name__ == "__main__":
    unittest.main()
