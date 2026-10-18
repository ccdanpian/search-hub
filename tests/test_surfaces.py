"""MCP, HTTP server and fetch: exercised against local fakes, no internet."""
import http.server
import json
import os
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from tests.helpers import core, cfg
from searchhub import fetch as F, mcp, server

SEL = {"title": "T", "url": "https://a.org", "snippet": "s", "published_at": "2026-10-01T00:00:00+00:00", "domain": "a.org",
       "authority": 2, "engines": ["e"], "score": 0.9, "relevance": 1.0}
FAKE = {"ok": True, "query": "q", "backend": "t", "judge": "heuristic", "selected": [SEL], "skipped": [{"url": "x", "reason": "r"}],
        "others": [{"title": "O", "url": "https://o.org"}], "engines_bad": [], "_ranked": [{"url": "https://a.org"}]}


class Mcp(unittest.TestCase):
    def rpc(self, method, params=None, mid=1):
        return mcp.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, cfg())

    def test_handshake_and_tools(self):
        self.assertEqual(self.rpc("initialize")["result"]["serverInfo"]["name"], "search-hub")
        self.assertEqual({t["name"] for t in self.rpc("tools/list")["result"]["tools"]}, {"web_search", "web_fetch"})
        self.assertEqual(self.rpc("ping")["result"], {})

    def test_notification_gets_no_reply(self):
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, cfg()))

    def test_unknown_method(self):
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)

    def test_web_search_call(self):
        with mock.patch.object(core, "search", return_value=dict(FAKE)) as s:
            r = self.rpc("tools/call", {"name": "web_search", "arguments": {"query": "q", "select": 3, "recent": True}})
        self.assertFalse(r["result"]["isError"])
        body = json.loads(r["result"]["content"][0]["text"])
        self.assertNotIn("_ranked", body)
        self.assertEqual(s.call_args.kwargs["select"], 3)

    def test_search_failure_is_flagged(self):
        with mock.patch.object(core, "search", return_value={"ok": False, "error": "x", "_ranked": []}):
            r = self.rpc("tools/call", {"name": "web_search", "arguments": {"query": "q"}})
        self.assertTrue(r["result"]["isError"])

    def test_tool_exception_becomes_error_result(self):
        with mock.patch.object(core, "search", side_effect=RuntimeError("kaboom")):
            r = self.rpc("tools/call", {"name": "web_search", "arguments": {"query": "q"}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("kaboom", r["result"]["content"][0]["text"])

    def test_fetch_validation(self):
        for bad in ({}, {"urls": []}, {"urls": ["u"] * 9}, {"urls": "u"}):
            r = self.rpc("tools/call", {"name": "web_fetch", "arguments": bad})
            self.assertTrue(r["result"]["isError"], bad)
        self.assertTrue(self.rpc("tools/call", {"name": "zzz", "arguments": {}})["result"]["isError"])


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = cfg(); cls.cfg["server"].update(max_concurrent=8)
        cls.open_srv = cls._start("127.0.0.1", "")
        # the 429 test needs its own listener: the slot is released just after the response is sent, so sharing a
        # one-slot server with other tests made them flaky
        one = cfg(); one["server"].update(max_concurrent=1)
        cls.one_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(one, "127.0.0.1", ""))
        cls.one_srv.daemon_threads = True
        threading.Thread(target=cls.one_srv.serve_forever, daemon=True).start()
        cls.tok_srv = cls._start("0.0.0.0", "s3cret")

    @classmethod
    def _start(cls, host, token):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.cfg, host, token))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    @classmethod
    def tearDownClass(cls):
        for s in (cls.open_srv, cls.tok_srv, cls.one_srv):
            s.shutdown(); s.server_close()

    def req(self, srv, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                body = resp.read()
                return resp.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_health_needs_no_token(self):
        self.assertEqual(self.req(self.tok_srv, "GET", "/health")[0], 200)

    def test_token_required_off_loopback(self):
        self.assertEqual(self.req(self.tok_srv, "GET", "/search?q=x")[0], 401)
        self.assertEqual(self.req(self.tok_srv, "GET", "/search?q=x", headers={"Authorization": "Bearer wrong"})[0], 401)
        with mock.patch.object(core, "search", return_value=dict(FAKE)):
            self.assertEqual(self.req(self.tok_srv, "GET", "/search?q=x", headers={"Authorization": "Bearer s3cret"})[0], 200)
            self.assertEqual(self.req(self.tok_srv, "GET", "/search?q=x", headers={"X-API-Key": "s3cret"})[0], 200)
        self.assertEqual(self.req(self.tok_srv, "POST", "/search", {"query": "x"})[0], 401)

    def test_search_get_and_post(self):
        with mock.patch.object(core, "search", return_value=dict(FAKE)) as s:
            st, b = self.req(self.open_srv, "GET", "/search?q=hello&select=2&judge=0&recent=1")
            self.assertEqual(st, 200)
            self.assertEqual(b["results"], FAKE["_ranked"])
            self.assertIn("elapsed_ms", b)
            kw = s.call_args.kwargs
            self.assertEqual((kw["select"], kw["judge"], kw["recent"]), ("2", False, True))
            self.assertEqual(self.req(self.open_srv, "POST", "/search", {"query": "x", "top_k": 4})[0], 200)

    def test_validation_errors(self):
        self.assertEqual(self.req(self.open_srv, "GET", "/search")[0], 400)
        self.assertEqual(self.req(self.open_srv, "POST", "/search", raw=b"{not json")[0], 400)
        self.assertEqual(self.req(self.open_srv, "POST", "/search", [1, 2])[0], 400)
        self.assertEqual(self.req(self.open_srv, "POST", "/fetch", {"urls": []})[0], 400)
        self.assertEqual(self.req(self.open_srv, "GET", "/nope")[0], 404)
        self.assertEqual(self.req(self.open_srv, "POST", "/nope", {})[0], 404)

    def test_oversized_body_rejected(self):
        st, _ = self.req(self.open_srv, "POST", "/search", raw=b"x" * (server.MAX_BODY + 10))
        self.assertEqual(st, 400)

    def test_all_backends_failed_is_502_but_no_results_is_200(self):
        with mock.patch.object(core, "search", return_value={"ok": False, "tried": ["a: OSError: down"], "_ranked": []}):
            self.assertEqual(self.req(self.open_srv, "GET", "/search?q=x")[0], 502)
        with mock.patch.object(core, "search", return_value={"ok": False, "tried": ["a: no results"], "_ranked": []}):
            self.assertEqual(self.req(self.open_srv, "GET", "/search?q=x")[0], 200)

    def test_concurrency_limit_429(self):
        gate, started = threading.Event(), threading.Event()
        def slow(*a, **k):
            started.set(); gate.wait(5); return dict(FAKE)
        out = []
        with mock.patch.object(core, "search", side_effect=slow):
            t = threading.Thread(target=lambda: out.append(self.req(self.one_srv, "GET", "/search?q=a")))
            t.start(); self.assertTrue(started.wait(5))
            self.assertEqual(self.req(self.one_srv, "GET", "/search?q=b")[0], 429)
            gate.set(); t.join()
        self.assertEqual(out[0][0], 200)

    def test_mcp_over_http(self):
        rpc = lambda m, srv=None, h=None: self.req(srv or self.open_srv, "POST", "/mcp", m, headers=h)
        st, b = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        self.assertEqual((st, b["result"]["protocolVersion"], b["result"]["serverInfo"]["name"]), (200, "2025-06-18", "search-hub"))
        self.assertEqual(rpc({"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})[1]
                         ["result"]["protocolVersion"], mcp.PROTOCOL)  # unknown version: offer ours
        self.assertEqual(rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}), (202, None))
        st, b = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual({t["name"] for t in b["result"]["tools"]}, {"web_search", "web_fetch"})
        with mock.patch.object(core, "search", return_value=dict(FAKE)):
            st, b = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": "q"}}})
        self.assertEqual(json.loads(b["result"]["content"][0]["text"])["results"][0]["url"], "https://a.org")  # compact output
        st, b = rpc([{"jsonrpc": "2.0", "id": 4, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/x"}])
        self.assertEqual(b, [{"jsonrpc": "2.0", "id": 4, "result": {}}])  # batch: notification gets no entry
        self.assertEqual(self.req(self.open_srv, "GET", "/mcp")[0], 405)
        self.assertEqual(self.req(self.open_srv, "POST", "/mcp", raw=b"{oops")[0], 400)
        self.assertEqual(rpc("not an object")[0], 400)

    def test_mcp_needs_token_off_loopback(self):
        m = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        self.assertEqual(self.req(self.tok_srv, "POST", "/mcp", m)[0], 401)
        self.assertEqual(self.req(self.tok_srv, "GET", "/mcp")[0], 401)
        self.assertEqual(self.req(self.tok_srv, "DELETE", "/mcp")[0], 401)
        self.assertEqual(self.req(self.tok_srv, "POST", "/mcp", m, headers={"Authorization": "Bearer s3cret"})[0], 200)
        self.assertEqual(self.req(self.tok_srv, "DELETE", "/mcp", headers={"Authorization": "Bearer s3cret"})[0], 405)

    def test_refuses_public_bind_without_token(self):
        with self.assertRaises(SystemExit):
            server.serve(["0.0.0.0:0"], cfg())

    def test_fetch_endpoint_blocks_private(self):
        st, b = self.req(self.open_srv, "POST", "/fetch", {"url": "http://127.0.0.1:1/"})
        self.assertEqual(st, 200)
        self.assertFalse(b["pages"][0]["ok"])
        self.assertIn("blocked", b["pages"][0]["error"])


def _make_pdf(text):
    """A one-page PDF with a single text line (poppler repairs the missing xref table)."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    return b"".join([
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 700 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n",
        f"4 0 obj<</Length {len(stream)}>>stream\n".encode(), stream, b"\nendstream endobj\n",
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R/Size 6>>\n%%EOF\n"])


PDF = _make_pdf("Searchhub PDF extraction test sentence number one. It must be long enough to count as a readable page of text.")


class Page(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/redir":
            self.send_response(302); self.send_header("Location", "http://169.254.169.254/latest"); self.end_headers(); return
        if self.path == "/bin":
            self.send_response(200); self.send_header("Content-Type", "image/png"); self.end_headers(); self.wfile.write(b"\x89PNG"); return
        if self.path in ("/pdf", "/pdf-octet", "/pdf-junk"):
            body = PDF if self.path != "/pdf-junk" else b"%PDF-1.4 this is not really a pdf"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream" if self.path == "/pdf-octet" else "application/pdf")
            self.end_headers(); self.wfile.write(body); return
        if self.path == "/js":
            body = b"<html><head><title>App</title></head><body><div id=root></div><script>render()</script></body></html>"
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body); return
        if self.path == "/challenge":
            body = b"<html><head><title>Just a moment...</title></head><body>" + b"Checking your browser before accessing. " * 4 + b"</body></html>"
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body); return
        if self.path == "/gbk":
            body = ("<html><head><meta charset='gb2312'><title>标题</title></head><body><article>" + "苹果手机发布会。" * 40 + "</article></body></html>").encode("gb18030")
            ctype = "text/html"
        else:
            body = ("<html><head><title>My Title</title><style>.x{}</style></head><body><nav>MENU</nav><main><h1>Head</h1><p>"
                    + "Real content sentence. " * 30 + "</p></main><footer>FOOT</footer><script>evil()</script></body></html>").encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200); self.send_header("Content-Type", ctype); self.end_headers(); self.wfile.write(body)


class Fetch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def test_scheme_and_private_blocked(self):
        for u in ("file:///etc/passwd", "ftp://x/y", "gopher://x", "javascript:1", "http://127.0.0.1/", "http://[::1]/",
                  "http://169.254.169.254/", "http://10.0.0.1/", "http://192.168.1.1/", "http://100.64.0.1/", "http://localhost/"):
            self.assertIsNotNone(F.check_url(u), u)
        self.assertIsNone(F.check_url("http://127.0.0.1/", allow_private=True))

    def test_polluted_dns_answers_do_not_block(self):
        fake = lambda answers: (lambda host, port: [(None, None, None, "", (a, 0)) for a in answers])
        with mock.patch.object(F.socket, "getaddrinfo", fake(["31.13.73.169", "2001::1"])):
            self.assertFalse(F.host_is_blocked("en.wikipedia.org"))
        with mock.patch.object(F.socket, "getaddrinfo", fake(["31.13.73.169", "10.0.0.5"])):
            self.assertTrue(F.host_is_blocked("evil.example"))   # a private answer still blocks
        with mock.patch.object(F.socket, "getaddrinfo", fake(["fd00::1"])):
            self.assertTrue(F.host_is_blocked("evil6.example"))  # IPv6 ULA still blocks

    def test_fetch_blocked_by_default(self):
        r = F.fetch(self.base + "/")
        self.assertFalse(r["ok"]); self.assertIn("blocked", r["error"])

    def test_extracts_main_text_and_strips_chrome(self):
        r = F.fetch(self.base + "/", allow_private=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["title"], "My Title")
        self.assertIn("Real content", r["text"])
        for junk in ("MENU", "FOOT", "evil()"):
            self.assertNotIn(junk, r["text"])

    def test_table_cells_are_separated(self):
        p = F._Text()
        p.feed("<table><tr><th>API</th><th>Price</th></tr><tr><td>Search</td><td>$0.75</td></tr></table>")
        text = F._clean(p.out)
        self.assertIn("API | Price", text)
        self.assertIn("Search | $0.75", text)

    def test_max_chars(self):
        r = F.fetch(self.base + "/", max_chars=100, allow_private=True)
        self.assertEqual(len(r["text"]), 100)
        self.assertGreater(r["text_length"], 100)

    def test_gb_encoding(self):
        r = F.fetch(self.base + "/gbk", allow_private=True)
        self.assertTrue(r["ok"]); self.assertIn("苹果手机", r["text"])

    def test_non_text_rejected(self):
        r = F.fetch(self.base + "/bin", allow_private=True)
        self.assertFalse(r["ok"]); self.assertIn("not a text page", r["error"])

    def test_pdf_text_extracted(self):
        if not F.shutil.which("pdftotext"):
            self.skipTest("pdftotext not installed")
        for path in ("/pdf", "/pdf-octet"):  # by content type and by %PDF- magic
            r = F.fetch(self.base + path, allow_private=True)
            self.assertTrue(r["ok"], (path, r))
            self.assertEqual(r["kind"], "pdf")
            self.assertIn("extraction test sentence", r["text"])

    def test_bad_pdf_reports_error(self):
        r = F.fetch(self.base + "/pdf-junk", allow_private=True)
        self.assertFalse(r["ok"]); self.assertIn("PDF", r["error"])

    def test_pdf_size_cap(self):
        with mock.patch.object(F, "PDF_MAX_BYTES", 100):
            r = F.fetch(self.base + "/pdf", allow_private=True)
        self.assertFalse(r["ok"]); self.assertIn("larger", r["error"])

    def test_redirect_to_private_blocked(self):
        # first hop is allowed (allow_private for the local server only), redirect target must still be checked
        with mock.patch.object(F, "host_is_blocked", side_effect=lambda h: h == "169.254.169.254"):
            r = F.fetch(self.base + "/redir")
        self.assertFalse(r["ok"]); self.assertIn("redirect", r["error"])

    def test_fetch_many_preserves_order(self):
        rs = F.fetch_many([self.base + "/", self.base + "/gbk", self.base + "/bin"], allow_private=True)
        self.assertEqual([r["ok"] for r in rs], [True, True, False])


class FetchFallback(unittest.TestCase):
    base = None

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    @staticmethod
    def fb(text="Fallback body. " * 20, calls=None):
        def f(url, max_chars, timeout):
            if calls is not None: calls.append(url)
            return {"ok": True, "url": url, "text": text[:max_chars], "text_length": len(text), "via": "deepseek", "anti_bot_signals": []}
        return f

    def test_not_called_when_local_ok(self):
        calls = []
        r = F.fetch(self.base + "/", allow_private=True, fallback=self.fb(calls=calls))
        self.assertTrue(r["ok"]); self.assertEqual(calls, []); self.assertNotIn("via", r)

    def test_js_page_uses_fallback(self):
        calls = []
        r = F.fetch(self.base + "/js", allow_private=True, fallback=self.fb(calls=calls))
        self.assertEqual((r["ok"], r["via"], len(calls)), (True, "deepseek", 1))
        self.assertIn("under 100 characters", r["fallback_from"])

    def test_bot_challenge_uses_fallback_when_it_has_more(self):
        r = F.fetch(self.base + "/challenge", allow_private=True, fallback=self.fb("Real article text. " * 200))
        self.assertEqual(r.get("via"), "deepseek")
        self.assertEqual(r["fallback_from"], "bot challenge page")

    def test_bot_challenge_kept_when_fallback_is_not_better(self):
        r = F.fetch(self.base + "/challenge", allow_private=True, fallback=self.fb("x"))
        self.assertNotIn("via", r); self.assertIn("fallback_error", r)

    def test_policy_blocks_never_reach_the_fallback(self):
        calls = []
        for u in ("http://127.0.0.1:9/", "file:///etc/passwd"):
            r = F.fetch(u, fallback=self.fb(calls=calls))
            self.assertFalse(r["ok"])
        self.assertEqual(calls, [])
        with mock.patch.object(F, "host_is_blocked", side_effect=lambda h: h == "169.254.169.254"):
            r = F.fetch(self.base + "/redir", fallback=self.fb(calls=calls))
        self.assertEqual(calls, [])

    def test_fallback_exception_keeps_local_error(self):
        def boom(*a): raise OSError("deepseek down")
        r = F.fetch(self.base + "/js", allow_private=True, fallback=boom)
        self.assertFalse(r["ok"]); self.assertIn("deepseek down", r["fallback_error"]); self.assertIn("error", r)

    def test_fetch_many_passes_fallback(self):
        rs = F.fetch_many([self.base + "/js", self.base + "/"], allow_private=True, fallback=self.fb())
        self.assertEqual([r["ok"] for r in rs], [True, True]); self.assertEqual(rs[0]["via"], "deepseek")

    def test_make_fallback_switches(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(F.make_fallback({"fetch_fallback": {"enabled": False}, "env_files": []}))
            self.assertIsNone(F.make_fallback({"env_files": []}))                      # no key
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k"}, clear=True):
            self.assertTrue(callable(F.make_fallback({"env_files": []})))

    def test_make_fallback_request_and_mapping(self):
        from tests.test_backends import FakeResp
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(core._direct, "open", return_value=FakeResp({"title": " T  x ", "content": "word " * 60})) as o:
            f = F.make_fallback({"env_files": []})
            r = f("https://a.org/p", 50, 20)
        req = o.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/v1/tools/fetch"))
        self.assertEqual(json.loads(req.data), {"url": "https://a.org/p"})
        self.assertEqual((r["ok"], r["title"], len(r["text"]), r["text_length"], r["via"]), (True, "T x", 50, 299, "deepseek"))


class Retry(unittest.TestCase):
    def test_transient_error_is_retried_once(self):
        seq = [{"ok": False, "url": "u", "error": "URLError: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred>", "elapsed_ms": 5},
               {"ok": True, "url": "u", "text": "x" * 200, "text_length": 200, "elapsed_ms": 7}]
        with mock.patch.object(F, "_fetch_local", side_effect=seq) as fl, mock.patch.object(F.time, "sleep"):
            r = F.fetch("https://a.org/")
        self.assertEqual(fl.call_count, 2)
        self.assertTrue(r["ok"])
        self.assertIn("UNEXPECTED_EOF", r["retried"])

    def test_permanent_errors_are_not_retried(self):
        for err in ("HTTPError: HTTP Error 403: Forbidden", "blocked: 10.0.0.1 is not a public address", "not a text page (image/png)"):
            with mock.patch.object(F, "_fetch_local", return_value={"ok": False, "url": "u", "error": err}) as fl:
                F.fetch("https://a.org/")
            self.assertEqual(fl.call_count, 1, err)

    def test_retry_failure_then_fallback(self):
        bad = {"ok": False, "url": "u", "error": "ConnectionResetError: Connection reset by peer"}
        fb = mock.Mock(return_value={"ok": True, "url": "u", "text": "y" * 300, "text_length": 300, "via": "deepseek"})
        with mock.patch.object(F, "_fetch_local", side_effect=[dict(bad), dict(bad)]), mock.patch.object(F.time, "sleep"):
            r = F.fetch("https://a.org/", fallback=fb)
        self.assertEqual(r["via"], "deepseek")
        fb.assert_called_once()


class Scope(unittest.TestCase):
    """DeepSeek is only queried for general-web searches; engines= and categories= reach SearXNG correctly."""

    def run_search(self, **kw):
        seen = {}
        def sx(b, qs):
            seen["qs"] = urllib.parse.parse_qs(qs); return {"results": []}
        ds = mock.Mock(return_value=[])
        with mock.patch.object(core, "_searxng_get", side_effect=sx), mock.patch.object(core, "_deepseek_search", ds), \
                mock.patch.object(core, "_tavily", return_value=[]):
            core.search("q", cfg(), judge=False, **kw)
        return seen["qs"], ds.called

    def test_default_uses_general_and_deepseek(self):
        qs, ds = self.run_search()
        self.assertEqual((qs["categories"], "engines" in qs, ds), (["general"], False, True))

    def test_engines_sent_without_categories_and_skip_deepseek(self):
        qs, ds = self.run_search(engines="yandex,naver")
        self.assertEqual((qs["engines"], "categories" in qs, ds), (["yandex,naver"], False, False))

    def test_non_web_categories_skip_deepseek(self):
        for cat, expect in (("images", False), ("videos", False), ("science", True), ("news", True), ("it", True)):
            self.assertEqual(self.run_search(categories=cat)[1], expect, cat)

    def test_pinned_deepseek_ignores_scope(self):
        with mock.patch.object(core, "_deepseek_search", return_value=[]) as ds:
            core.search("q", cfg(), judge=False, backend="b", engines="yandex")
        self.assertTrue(ds.called)

    def test_backend_categories_override(self):
        c = cfg(); c["backends"][1]["categories"] = ["general"]
        with mock.patch.object(core, "_searxng_get", return_value={"results": []}), mock.patch.object(core, "_tavily", return_value=[]), \
                mock.patch.object(core, "_deepseek_search", return_value=[]) as ds:
            core.search("q", c, judge=False, categories="science")
        self.assertFalse(ds.called)


if __name__ == "__main__":
    unittest.main()
