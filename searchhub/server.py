"""HTTP API for search-hub (standard library only).

  GET  /health                       -> {ok, service, version}           (no token needed)
  GET  /search?q=...&select=5&recent=1&lang=&engines=&categories=&time_range=&judge=0&compact=1
  POST /search  {query, select|top_k, recent, lang, engines, categories, time_range, judge, backend, no_fallback}
  POST /fetch   {urls:[...] | url, question, max_chars, timeout}   (question -> verified excerpts, see searchhub.extract)
  POST /mcp     MCP over Streamable HTTP (stateless JSON responses): the same web_search / web_fetch tools as
                `python -m searchhub.mcp`, so other machines can use this instance as a remote MCP server.

POST /search is compatible with the Hermes "Multi Search" server (it also returns results/elapsed_ms/searxng_count).
A token (Authorization: Bearer <token> or X-API-Key) is required on every listener that is not loopback; on loopback it
is required only if server.loopback_requires_token is true."""
import hmac
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core, extract, fetch as fetchmod, mcp

MAX_BODY = 64 * 1024        # largest request body we will parse
MAX_DRAIN = 1024 * 1024     # larger bodies than this are not even read; the connection is closed
MAX_URLS = 8
MAX_CHARS = 50_000


def is_loopback(host):
    return host in ("127.0.0.1", "localhost", "::1")


def get_token(cfg):
    s = cfg["server"]
    return s.get("token") or os.environ.get(s.get("token_env", "SEARCH_HUB_TOKEN"), "")


def _truthy(v, default=None):
    if v is None:
        return default
    return str(v).lower() not in ("0", "false", "no", "off", "")


def make_handler(cfg, host, token):
    need_token = (not is_loopback(host)) or bool(cfg["server"].get("loopback_requires_token"))
    sem = threading.BoundedSemaphore(int(cfg["server"].get("max_concurrent", 6)))

    class Handler(BaseHTTPRequestHandler):
        server_version = "search-hub/" + core.VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            # Log "METHOD /path STATUS" only: the query string (GET /search?q=...) would leak what people search for.
            parts = getattr(self, "requestline", "").split(" ")  # METHOD TARGET HTTP/1.1
            line = f"{parts[0]} {parts[1].split('?', 1)[0]}" if len(parts) >= 2 else ""
            status = args[1] if len(args) > 1 else ""
            sys.stderr.write("[search-hub] %s %s %s\n" % (self.address_string(), line, status))

        def _drain(self):
            """Read and discard an unread request body so a keep-alive connection stays valid.
            A body larger than MAX_BODY is not read at all; the connection is closed instead."""
            if getattr(self, "_body_read", False):
                return
            self._body_read = True
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= MAX_DRAIN:  # read and discard so the client gets its response instead of a reset
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
            elif n > MAX_DRAIN:  # absurdly large: do not read it, close the connection
                self.close_connection = True

        def _send(self, status, obj):
            self._drain()
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _send_empty(self, status, headers=()):
            self._drain()
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _mcp(self):
            """Streamable HTTP, stateless: each POST carries one JSON-RPC message (or a batch) and gets a plain JSON
            answer; notifications get 202. No sessions and no server-initiated stream (GET returns 405)."""
            msg = self._body()
            msgs = msg if isinstance(msg, list) else [msg]
            if not msgs or not all(isinstance(m, dict) for m in msgs):
                raise ValueError("body must be a JSON-RPC object or a batch of them")
            replies = [r for r in (mcp.handle(m, cfg) for m in msgs) if r is not None]
            if not replies:
                return self._send_empty(202)
            self._send(200, replies if isinstance(msg, list) else replies[0])

        def _authorized(self):
            if not need_token:
                return True
            given = ""
            h = self.headers.get("Authorization", "")
            if h.lower().startswith("bearer "):
                given = h[7:].strip()
            given = given or self.headers.get("X-API-Key", "")
            return bool(token) and hmac.compare_digest(given.encode(), token.encode())

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request body too large")
            self._body_read = True
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        def _run(self, fn):
            if not sem.acquire(blocking=False):
                return self._send(429, {"ok": False, "error": "too many concurrent requests"})
            try:
                fn()
            except ValueError as e:
                self._send(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            finally:
                sem.release()

        def _search(self, p):
            t0 = time.time()
            query = str(p.get("query") or p.get("q") or "").strip()
            if not query:
                raise ValueError("missing query")
            res = core.search(
                query, cfg, select=p.get("select") or (min(int(p["top_k"]), 20) if p.get("top_k") else None),
                engines=p.get("engines"), categories=p.get("categories"), lang=p.get("lang"),
                time_range=p.get("time_range"), recent=_truthy(p.get("recent"), False), judge=_truthy(p.get("judge"), True),
                backend=p.get("backend"), allow_hub=not _truthy(p.get("no_fallback"), False))
            tried = res.get("tried") or []
            status = 502 if (not res.get("ok")) and tried and all("no results" not in t for t in tried) else 200
            if _truthy(p.get("compact"), False):  # what an agent needs; same shape as the MCP web_search result
                res.pop("_ranked", None)
                out = mcp.compact(res)
                out["elapsed_ms"] = int((time.time() - t0) * 1000)
                return self._send(status, out)
            ranked = res.pop("_ranked", [])
            res["results"] = ranked  # Hermes-compatible list
            res["elapsed_ms"] = int((time.time() - t0) * 1000)
            res["tavily_count"] = sum(1 for r in ranked if "tavily" in r.get("source_engine", ""))
            res["searxng_count"] = len(ranked) - res["tavily_count"]
            self._send(status, res)

        def _fetch(self, p):
            urls = p.get("urls") or ([p["url"]] if p.get("url") else [])
            if not isinstance(urls, list) or not urls or len(urls) > MAX_URLS or not all(isinstance(u, str) for u in urls):
                raise ValueError(f"urls must be a list of 1 to {MAX_URLS} strings")
            max_chars = max(100, min(int(p.get("max_chars") or 8000), MAX_CHARS))
            timeout = max(3, min(int(p.get("timeout") or 20), 60))
            allow_private = bool(cfg["server"].get("allow_private_fetch"))
            pages = extract.fetch_focused(urls, str(p.get("question") or "").strip(), cfg, max_chars, timeout, allow_private,
                                          fetchmod.make_fallback(cfg))
            self._send(200, {"ok": True, "pages": pages})

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path == "/health":
                return self._send(200, {"ok": True, "service": "search-hub", "version": core.VERSION})
            if not self._authorized():
                return self._send(401, {"ok": False, "error": "unauthorized"})
            if u.path == "/search":
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                return self._run(lambda: self._search(q))
            if u.path == "/mcp":  # no server-initiated event stream in this stateless transport
                return self._send_empty(405, [("Allow", "POST")])
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._send(401, {"ok": False, "error": "unauthorized"})
            path = urllib.parse.urlsplit(self.path).path
            if path == "/mcp":
                return self._run(self._mcp)
            if path not in ("/search", "/fetch"):
                return self._send(404, {"ok": False, "error": "not found"})

            def go():
                p = self._body()
                if not isinstance(p, dict):
                    raise ValueError("body must be a JSON object")
                (self._search if path == "/search" else self._fetch)(p)
            self._run(go)

        def do_DELETE(self):  # MCP clients may try to end a session; there are none to end
            if not self._authorized():
                return self._send(401, {"ok": False, "error": "unauthorized"})
            self._send_empty(405, [("Allow", "POST")])

    return Handler


def serve(listens, cfg=None):
    """listens: list of 'host:port'. Blocks until interrupted."""
    cfg = cfg or core.load_config()
    token = get_token(cfg)
    servers = []
    for spec in listens:
        host, _, port = spec.rpartition(":")
        host = host or "127.0.0.1"
        if not is_loopback(host) and not token:
            raise SystemExit(f"refusing to listen on {spec}: set {cfg['server'].get('token_env', 'SEARCH_HUB_TOKEN')} "
                             "(or server.token) before exposing the service beyond loopback")
        srv = ThreadingHTTPServer((host, int(port)), make_handler(cfg, host, token))
        srv.daemon_threads = True
        servers.append(srv)
        print(f"[search-hub] listening on http://{host}:{port} token={'required' if (not is_loopback(host) or cfg['server'].get('loopback_requires_token')) else 'not required'}",
              file=sys.stderr, flush=True)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.server_close()
