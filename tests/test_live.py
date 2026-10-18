"""Live smoke tests against the real backends. Opt in with SEARCH_HUB_LIVE=1 (uses the network, a little DeepSeek quota)."""
import os
import time
import unittest

from tests.helpers import core

LIVE = os.environ.get("SEARCH_HUB_LIVE") == "1"


@unittest.skipUnless(LIVE, "set SEARCH_HUB_LIVE=1 to run")
class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = core.load_config()
        core.load_env_files(cls.cfg)

    def search(self, q, **kw):
        t = time.time()
        r = core.search(q, self.cfg, judge=False, **kw)
        r["_wall"] = time.time() - t
        return r

    def test_both_backends_contribute(self):
        r = self.search("Raspberry Pi 5 idle power consumption")
        self.assertTrue(r["ok"])
        self.assertEqual(r["backends"], ["pi-searxng", "deepseek"])
        self.assertIn("deepseek", r["engines_ok"])
        self.assertGreaterEqual(len(set(r["engines_ok"]) - {"deepseek"}), 2, "SearXNG should have >=2 working engines")
        self.assertEqual(len(r["selected"]), 5)

    def test_chinese_query(self):
        r = self.search("树莓派5 功耗 待机 实测")
        self.assertTrue(r["ok"] and len(r["selected"]) >= 3)

    def test_selected_shape(self):
        for m in self.search("python asyncio TaskGroup")["selected"]:
            self.assertTrue(m["url"].startswith("http"))
            self.assertTrue(m["title"] and m["domain"])
            self.assertIn(m["authority"], (1, 2, 3))
            self.assertNotIn(":443", m["domain"])

    def test_site_operator_respected(self):
        r = self.search("site:github.com searxng engine yahoo", backend="deepseek")
        self.assertTrue(r["ok"])
        self.assertTrue(all(m["domain"].endswith("github.com") for m in r["selected"]))

    def test_each_backend_alone(self):
        for b in ("pi-searxng", "deepseek"):
            r = self.search("linux kernel 6.12 features", backend=b)
            self.assertTrue(r["ok"], b)
            self.assertEqual(r["backend"], b)

    def test_latency_budget(self):
        r = self.search("systemd user service linger")
        self.assertLess(r["_wall"], 8.0, "fused search without judge should finish well under 8 s")

    def test_no_engine_silently_dead(self):
        r = self.search("test query for engines")
        # a rate-limit suspension is an environment condition, not a defect: report it, fail on anything else
        broken = [b for b in r["engines_bad"] if "too many requests" not in b["why"].lower()]
        self.assertEqual(broken, [], f"engines failing: {broken}")
        if r["engines_bad"]:
            print(f"\n[live] note: engines unavailable right now: {r['engines_bad']}")

    def test_unanswerable_query_does_not_crash(self):
        r = self.search("qzxvbnm wkjhgfd 8472619 plmoknijb")
        self.assertIn("ok", r)

    def test_judge_runs_when_key_present(self):
        if not os.environ.get("OPENROUTER_API_KEY"):
            self.skipTest("no OPENROUTER_API_KEY")
        r = core.search("python 3.14 release", self.cfg, select=3)
        self.assertTrue(r["judge"].startswith("llm:"))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(LIVE, "set SEARCH_HUB_LIVE=1 to run")
class LiveFetchAndScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from searchhub import fetch as F
        cls.F = F
        cls.cfg = core.load_config()
        core.load_env_files(cls.cfg)
        os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7890")

    def test_pdf_is_read(self):
        r = self.F.fetch("https://arxiv.org/pdf/1706.03762", max_chars=500, fallback=self.F.make_fallback(self.cfg))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["kind"], "pdf")
        self.assertIn("Attention", r["text"] + self.F.fetch("https://arxiv.org/pdf/1706.03762", max_chars=3000)["text"])

    def test_private_address_never_goes_to_fallback(self):
        r = self.F.fetch("http://127.0.0.1:8889/", fallback=self.F.make_fallback(self.cfg))
        self.assertFalse(r["ok"])
        self.assertIn("blocked", r["error"])
        self.assertNotIn("via", r)

    def test_engines_param_reaches_searxng_and_skips_deepseek(self):
        r = core.search("raspberry pi 5", self.cfg, judge=False, engines="yandex", select=5)
        self.assertTrue(r["ok"])
        self.assertEqual(r["engines_ok"], ["yandex"])
        self.assertEqual(r["backend"], "pi-searxng")

    def test_images_category_does_not_query_deepseek(self):
        r = core.search("raspberry pi", self.cfg, judge=False, categories="images")
        self.assertNotIn("deepseek", r.get("engines_ok", []))


@unittest.skipUnless(LIVE, "set SEARCH_HUB_LIVE=1 to run")
class LiveExtract(unittest.TestCase):
    def test_question_mode_returns_verified_excerpts(self):
        from searchhub import extract as X, fetch as F
        cfg = core.load_config()
        core.load_env_files(cfg)
        os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7890")
        url = "https://api-docs.deepseek.com/guides/search"
        p = X.fetch_focused([url], "What is the QPS limit and price of the Search API?", cfg, fallback=F.make_fallback(cfg))[0]
        self.assertTrue(p["ok"], p)
        self.assertEqual(p["mode"], "extract", p)
        self.assertTrue(p["relevant"] and p["excerpts"])
        raw = F.fetch(url, 60000, 20, False, F.make_fallback(cfg))["text"]
        for e in p["excerpts"]:  # independent re-check of the verbatim guarantee
            self.assertTrue(all(X.norm(part) in X.norm(raw) for part in e.split(" … ")), e)
        self.assertLess(sum(map(len, p["excerpts"])), len(raw) / 2)
