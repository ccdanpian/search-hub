"""Offline tests: python -m unittest discover -s tests -v   (run from the search-hub directory)"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("OPENROUTER_API_KEY", None)  # tests must never call the real judge
os.environ.pop("TAVILY_API_KEY", None)

from searchhub import core, fetch as fetchmod, server  # noqa: E402

NOW = 1_792_000_000  # fixed clock for date tests (2026-10-14)

SEARX_RESULTS = {
    "results": [
        {"title": "Pod Lifecycle | Kubernetes", "url": "https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/",
         "content": "Pods follow a defined lifecycle", "engines": ["bing", "google cse"], "positions": [1, 2]},
        {"title": "Pod lifecycle blog", "url": "https://www.example-blog.com/pod", "content": "kubernetes pod lifecycle explained",
         "engines": ["bing"], "positions": [2]},
        {"title": "Vietnamese food", "url": "https://food.example.org/pho", "content": "best noodles in Hanoi", "engines": ["bing"],
         "positions": [3]},
        {"title": "Cat video", "url": "https://www.youtube.com/watch?v=1", "content": "kubernetes pod lifecycle", "engines": ["bing"],
         "positions": [4]},
        {"title": "Spec", "url": "https://example.org/spec.pdf", "content": "kubernetes pod lifecycle", "engines": ["bing"],
         "positions": [5]},
        {"title": "Pod Lifecycle - Kubernetes", "url": "https://www.kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle",
         "content": "duplicate of the first (www + trailing slash)", "engines": ["360search"], "positions": [1]},
    ],
    "unresponsive_engines": [["quark", "CAPTCHA"]],
}


class FakeSearx(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(self.server.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start(handler_cls, payload=None):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    srv.payload = payload
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def cfg_for(searx_port, extra_backends=()):
    cfg = core.load_config(os.devnull) if False else json.loads(json.dumps(core.DEFAULTS))
    cfg["_path"] = None
    cfg["backends"] = [{"name": "t", "type": "searxng", "url": f"http://127.0.0.1:{searx_port}"}, *extra_backends]
    return cfg


class TextHelpers(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(core.infer_date("2021年4月28日 ... x"), "2021-04-28T00:00:00+00:00")
        self.assertEqual(core.infer_date("2026-08-31 x"), "2026-08-31T00:00:00+00:00")
        self.assertEqual(core.infer_date("Oct 23, 2009 ... x"), "2009-10-23T00:00:00+00:00")
        self.assertTrue(core.infer_date("6天前 - x", now=NOW).startswith("2026-10-08"))
        self.assertTrue(core.infer_date("5 days ago · x", now=NOW).startswith("2026-10-09"))
        self.assertIsNone(core.infer_date("no date here"))

    def test_relevance(self):
        t = core.query_terms("二甲双胍 2型糖尿病 最新指南")
        self.assertNotIn("最新", " ".join(t))
        self.assertGreater(core.relevance(t, "二甲双胍治疗2型糖尿病的指南"), 0.8)
        self.assertLess(core.relevance(t, "Vietnamese dishes you must try"), 0.05)

    def test_norm_url_merges_www_and_slash(self):
        self.assertEqual(core.norm_url("https://www.a.com/x/"), core.norm_url("https://a.com/x"))


class Ranking(unittest.TestCase):
    def setUp(self):
        self.auth = core.load_authority(json.loads(json.dumps(core.DEFAULTS)))
        self.cfg = json.loads(json.dumps(core.DEFAULTS))

    def run_rank(self, **kw):
        o = core.make_opts(lang="auto", judge=False, **kw)
        return core.rank("kubernetes pod lifecycle", json.loads(json.dumps(SEARX_RESULTS["results"])),
                         SEARX_RESULTS["unresponsive_engines"], o, self.cfg, self.auth, "t")

    def test_fusion_and_authority(self):
        res = self.run_rank()
        first = res["selected"][0]
        self.assertEqual(first["domain"], "kubernetes.io")
        self.assertEqual(first["authority"], 3)
        self.assertEqual(set(first["engines"]), {"bing", "google cse", "360search"})  # merged duplicates from 3 engines
        urls = [s["url"] for s in res["selected"]]
        self.assertEqual(len([u for u in urls if "kubernetes.io" in u]), 1)  # www/trailing-slash duplicate merged

    def test_filters(self):
        res = self.run_rank()
        reasons = {s["url"]: s["reason"] for s in res["skipped"]}
        self.assertIn("video/social site", reasons["https://www.youtube.com/watch?v=1"])
        self.assertNotIn("https://example.org/spec.pdf", reasons)  # PDFs are kept since 1.1: web_fetch reads them
        self.assertIn("off-topic", reasons["https://food.example.org/pho"])

    def test_engine_status_reported(self):
        res = self.run_rank()
        self.assertEqual(res["engines_bad"], [{"engine": "quark", "why": "CAPTCHA"}])
        self.assertEqual(res["judge"], "heuristic")

    def test_domain_cap(self):
        items = [{"title": f"kubernetes pod lifecycle {i}", "url": f"https://same.com/{i}", "content": "kubernetes pod lifecycle",
                  "engines": ["bing"], "positions": [i + 1]} for i in range(6)]
        o = core.make_opts(lang="auto", judge=False, select=5)
        res = core.rank("kubernetes pod lifecycle", items, [], o, self.cfg, self.auth, "t")
        self.assertLessEqual(len(res["selected"]), 2)


class Judge(unittest.TestCase):
    def test_failure_falls_back_to_heuristic(self):
        cfg = json.loads(json.dumps(core.DEFAULTS))
        auth = core.load_authority(cfg)
        os.environ["OPENROUTER_API_KEY"] = "sk-or-invalid"
        orig = core.llm_judge
        core.llm_judge = lambda q, items, c: (None, None)
        try:
            o = core.make_opts(lang="auto")
            res = core.rank("kubernetes pod lifecycle", json.loads(json.dumps(SEARX_RESULTS["results"])), [], o, cfg, auth, "t")
        finally:
            core.llm_judge = orig
            os.environ.pop("OPENROUTER_API_KEY", None)
        self.assertEqual(res["judge"], "heuristic")
        self.assertGreater(len(res["selected"]), 0)

    def test_low_probability_dropped_and_unjudged_not_selected(self):
        cfg = json.loads(json.dumps(core.DEFAULTS))
        cfg["judge"]["batch"] = 2
        cfg["judge"]["max_batches"] = 1  # only the 2 best candidates get judged
        auth = core.load_authority(cfg)
        os.environ["OPENROUTER_API_KEY"] = "x"
        orig = core.llm_judge
        core.llm_judge = lambda q, items, c: ([0.9, 0.05][:len(items)], "m")
        try:
            o = core.make_opts(lang="auto", select=5)
            res = core.rank("kubernetes pod lifecycle", json.loads(json.dumps(SEARX_RESULTS["results"])), [], o, cfg, auth, "t")
        finally:
            core.llm_judge = orig
            os.environ.pop("OPENROUTER_API_KEY", None)
        self.assertEqual(res["judged"], 2)
        self.assertTrue(any(s["reason"].startswith("judge:") for s in res["skipped"]))
        self.assertTrue(all("judge" in s for s in res["selected"]))  # nothing unjudged slips through


class FetchGuard(unittest.TestCase):
    def test_blocked(self):
        for u in ["file:///etc/passwd", "ftp://example.com/x", "http://127.0.0.1:9/", "http://localhost/", "http://10.0.0.5/",
                  "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/", "http://100.100.1.1/", "http://[::1]/"]:
            self.assertIsNotNone(fetchmod.check_url(u), u)

    def test_public_ip_allowed(self):
        self.assertIsNone(fetchmod.check_url("http://93.184.216.34/"))

    def test_allow_private_switch(self):
        self.assertIsNone(fetchmod.check_url("http://127.0.0.1:9/", allow_private=True))

    def test_redirect_to_private_refused(self):
        h = fetchmod._Redirects(False)
        req = urllib.request.Request("http://93.184.216.34/")
        with self.assertRaises(urllib.error.URLError):
            h.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:8888/")

    def test_fetch_reports_block(self):
        r = fetchmod.fetch("http://127.0.0.1:9/")
        self.assertFalse(r["ok"])
        self.assertIn("blocked", r["error"])


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.searx = start(FakeSearx, SEARX_RESULTS)
        cls.cfg = cfg_for(cls.searx.server_port)
        cls.cfg["server"]["loopback_requires_token"] = True
        cls.token = "t0k3n-for-tests"
        cls.cfg["server"]["token"] = cls.token
        cls.hub = HTTPServer(("127.0.0.1", 0), server.make_handler(cls.cfg, "127.0.0.1", cls.token))
        threading.Thread(target=cls.hub.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.hub.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.hub.shutdown()
        cls.searx.shutdown()

    def call(self, method, path, body=None, token="t0k3n-for-tests", headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=20) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_health_needs_no_token(self):
        code, body = self.call("GET", "/health", token=None)
        self.assertEqual((code, body["ok"]), (200, True))

    def test_search_requires_token(self):
        self.assertEqual(self.call("POST", "/search", {"query": "x"}, token=None)[0], 401)
        self.assertEqual(self.call("POST", "/search", {"query": "x"}, token="wrong")[0], 401)
        self.assertEqual(self.call("GET", "/search?q=x", token=None)[0], 401)

    def test_x_api_key_header(self):
        code, _ = self.call("POST", "/search", {"query": "kubernetes pod lifecycle", "judge": False}, token=None,
                            headers={"X-API-Key": self.token})
        self.assertEqual(code, 200)

    def test_search_hermes_compatible(self):
        code, body = self.call("POST", "/search", {"query": "kubernetes pod lifecycle", "top_k": 5, "judge": False})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        r0 = body["results"][0]
        for k in ("title", "url", "snippet", "published_at", "source_engine"):
            self.assertIn(k, r0)
        self.assertIn("elapsed_ms", body)
        self.assertEqual(body["selected"][0]["domain"], "kubernetes.io")

    def test_get_search(self):
        code, body = self.call("GET", "/search?q=kubernetes+pod+lifecycle&judge=0&select=2")
        self.assertEqual(code, 200)
        self.assertLessEqual(len(body["selected"]), 2)

    def test_bad_requests(self):
        self.assertEqual(self.call("POST", "/search", {})[0], 400)
        self.assertEqual(self.call("POST", "/fetch", {"urls": []})[0], 400)
        self.assertEqual(self.call("POST", "/fetch", {"urls": ["http://93.184.216.34/"] * 20})[0], 400)
        self.assertEqual(self.call("POST", "/nope", {})[0], 404)

    def test_fetch_blocks_internal_addresses(self):
        code, body = self.call("POST", "/fetch", {"urls": ["http://127.0.0.1:1/", "file:///etc/passwd", "http://100.100.1.1/"]})
        self.assertEqual(code, 200)
        self.assertTrue(all(not p["ok"] and ("blocked" in p["error"] or "scheme" in p["error"]) for p in body["pages"]))

    def test_oversized_body(self):
        code, body = self.call("POST", "/search", {"query": "x" * 70_000})
        self.assertEqual(code, 400)


class Backends(unittest.TestCase):
    def test_empty_primary_falls_through_to_second_backend(self):
        empty = start(FakeSearx, {"results": [], "unresponsive_engines": [["bing", "timeout"]]})
        full = start(FakeSearx, SEARX_RESULTS)
        cfg = cfg_for(empty.server_port, [{"name": "t2", "type": "searxng", "url": f"http://127.0.0.1:{full.server_port}"}])
        res = core.search("kubernetes pod lifecycle", cfg, judge=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["backend"], "t2")

    def test_all_backends_down(self):
        cfg = cfg_for(1)  # nothing listens on port 1
        res = core.search("kubernetes pod lifecycle", cfg, judge=False)
        self.assertFalse(res["ok"])
        self.assertTrue(res["tried"])

    def test_hub_backend_used_and_no_fallback_flag_sent(self):
        seen = {}

        class FakeHub(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["auth"] = self.headers.get("Authorization")
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = json.dumps({"ok": True, "selected": [{"title": "t", "url": "https://a.com"}], "results": [{"url": "https://a.com"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        hub = start(FakeHub)
        cfg = cfg_for(1, [{"name": "pi", "type": "hub", "url": f"http://127.0.0.1:{hub.server_port}", "token": "abc"}])
        res = core.search("kubernetes pod lifecycle", cfg, judge=False)
        self.assertEqual(res["backend"], "pi")
        self.assertEqual(seen["auth"], "Bearer abc")
        self.assertTrue(seen["body"]["no_fallback"])  # prevents hub <-> hub loops

    def test_no_fallback_skips_hub_backends(self):
        cfg = cfg_for(1, [{"name": "pi", "type": "hub", "url": "http://127.0.0.1:1"}])
        res = core.search("x y z", cfg, judge=False, allow_hub=False)
        self.assertFalse(any("pi" in t for t in res["tried"]))


class Serve(unittest.TestCase):
    def test_refuses_nonloopback_without_token(self):
        cfg = json.loads(json.dumps(core.DEFAULTS))
        os.environ.pop("SEARCH_HUB_TOKEN", None)
        with self.assertRaises(SystemExit) as cm:
            server.serve(["0.0.0.0:0"], cfg)
        self.assertIn("refusing", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
