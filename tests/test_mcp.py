"""Talks to `python -m searchhub.mcp` over stdio like a real MCP client would."""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class McpStdio(unittest.TestCase):
    def rpc(self, messages):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "SEARCH_HUB_CONFIG": os.path.join(ROOT, "tests", "dead_backend.json")}
        env.pop("OPENROUTER_API_KEY", None)
        p = subprocess.run([sys.executable, "-m", "searchhub.mcp"], cwd=ROOT, env=env, capture_output=True, timeout=60,
                           input=("\n".join(json.dumps(m) for m in messages) + "\n").encode("utf-8"))
        return [json.loads(l) for l in p.stdout.decode("utf-8").splitlines() if l.strip()]

    def test_handshake_list_and_calls(self):
        out = self.rpc([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                                                          "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": "x y"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "web_fetch", "arguments": {"urls": ["http://127.0.0.1:1/"]}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 6, "method": "bogus"},
        ])
        by_id = {m["id"]: m for m in out}
        self.assertEqual(len(out), 6)  # the notification got no reply
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "search-hub")
        self.assertEqual({t["name"] for t in by_id[2]["result"]["tools"]}, {"web_search", "web_fetch"})
        self.assertTrue(by_id[3]["result"]["isError"])  # dead backend -> reported, not crashed
        self.assertIn("no results", by_id[3]["result"]["content"][0]["text"])
        self.assertIn("blocked", by_id[4]["result"]["content"][0]["text"])  # private address refused
        self.assertTrue(by_id[5]["result"]["isError"])
        self.assertEqual(by_id[6]["error"]["code"], -32601)

    def test_bad_json_line(self):
        out = self.rpc([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(out[0]["result"], {})


if __name__ == "__main__":
    unittest.main()
