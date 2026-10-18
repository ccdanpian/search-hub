import io
import json
import os
import sys
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("OPENROUTER_API_KEY", None)
from searchhub import core, server  # noqa: E402


class NoQueryInLogs(unittest.TestCase):
    def test_query_string_not_logged(self):
        cfg = json.loads(json.dumps(core.DEFAULTS))
        cfg["backends"] = [{"name": "dead", "type": "searxng", "url": "http://127.0.0.1:1"}]
        hub = HTTPServer(("127.0.0.1", 0), server.make_handler(cfg, "127.0.0.1", ""))
        threading.Thread(target=hub.serve_forever, daemon=True).start()
        buf = io.StringIO()
        with redirect_stderr(buf):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                opener.open(f"http://127.0.0.1:{hub.server_port}/search?q=my-secret-topic&judge=0", timeout=20)
            except Exception:
                pass
        hub.shutdown()
        log = buf.getvalue()
        self.assertIn("GET /search", log)
        self.assertNotIn("my-secret-topic", log)


if __name__ == "__main__":
    unittest.main()
