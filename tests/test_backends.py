import json
import os
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import core, item, cfg


def good(n=3, prefix="https://x.org/"):
    return [item(f"{prefix}{i}", f"python release {i}", "python release notes", ["e"], [i + 1]) for i in range(n)]


class FakeResp:
    def __init__(self, obj): self.raw = json.dumps(obj).encode()
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self, *a): return self.raw


class DeepSeekAdapter(unittest.TestCase):
    def call(self, b=None, query="q", **kw):
        seen = {}
        def op(req, timeout=None):
            seen["req"], seen["body"] = req, json.loads(req.data)
            return FakeResp({"results": [
                {"url": "https://a.org/1", "title": "T1", "summary": "S1", "snippet": "sn1", "date": "2026-10-01T00:00:00Z"},
                {"url": "https://a.org/2", "title": "T2", "snippet": "only snippet", "date": None},
                {"title": "no url"}]})
        with mock.patch.object(core._direct, "open", side_effect=op):
            out = core._deepseek_search({"key": "k", **(b or {})}, query, core.make_opts(**kw))
        return out, seen

    def test_mapping(self):
        out, seen = self.call()
        self.assertEqual([x["url"] for x in out], ["https://a.org/1", "https://a.org/2"])
        self.assertEqual(out[0]["content"], "S1")           # summary preferred
        self.assertEqual(out[1]["content"], "only snippet")  # snippet fallback
        self.assertEqual(out[0]["publishedDate"], "2026-10-01T00:00:00Z")
        self.assertEqual(out[0]["engines"], ["deepseek"])
        self.assertEqual([x["positions"] for x in out], [[1], [2]])

    def test_request_shape(self):
        _, seen = self.call()
        self.assertTrue(seen["req"].full_url.endswith("/v1/tools/search"))
        self.assertEqual(seen["req"].get_header("Authorization"), "Bearer k")
        self.assertEqual(seen["body"], {"query": "q", "count": 10, "need_content": False})

    def test_count_clamped(self):
        self.assertEqual(self.call({"count": 999})[1]["body"]["count"], 50)
        self.assertEqual(self.call({"count": 1})[1]["body"]["count"], 5)

    def test_time_range_becomes_after_operator(self):
        for tr in ("day", "month", "year"):
            q = self.call(time_range=tr)[1]["body"]["query"]
            self.assertIn(" after:", q, tr)
        self.assertEqual(self.call(query="x after:2020")[1]["body"]["query"].count("after:"), 1)  # user operator wins
        self.assertNotIn("after:", self.call()[1]["body"]["query"])

    def test_no_key_returns_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(core._deepseek_search({}, "q", core.make_opts()))

    def test_key_from_env_var_name(self):
        with mock.patch.dict(os.environ, {"MY_KEY": "zzz"}, clear=True), \
                mock.patch.object(core._direct, "open", return_value=FakeResp({"results": []})) as o:
            self.assertEqual(core._deepseek_search({"key_env": "MY_KEY"}, "q", core.make_opts()), [])
            self.assertEqual(o.call_args[0][0].get_header("Authorization"), "Bearer zzz")


class Orchestration(unittest.TestCase):
    def search(self, a=None, b=None, c=None, **kw):
        a = a if a is not None else (lambda *x: ({"results": good(3, "https://a.org/")}))
        b = b if b is not None else (lambda *x, **k: good(3, "https://b.org/"))
        with mock.patch.object(core, "_searxng_get", side_effect=a), mock.patch.object(core, "_deepseek_search", side_effect=b), \
                mock.patch.object(core, "_tavily", return_value=[]):
            return core.search("python release", c or cfg(), judge=False, **kw)

    def test_both_merge(self):
        r = self.search(select=10)
        self.assertTrue(r["ok"])
        self.assertEqual(r["backend"], "a+b")
        self.assertEqual(r["backends"], ["a", "b"])
        self.assertEqual(r["total"], 6)

    def test_one_raises_other_answers(self):
        def boom(*x): raise OSError("down")
        r = self.search(a=boom)
        self.assertTrue(r["ok"] and r["backend"] == "b")

    def test_deepseek_raises(self):
        def boom(*x, **k): raise RuntimeError("500")
        r = self.search(b=boom)
        self.assertTrue(r["ok"] and r["backend"] == "a")

    def test_all_fail(self):
        def boom(*x, **k): raise OSError("down")
        r = self.search(a=boom, b=boom)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["tried"]), 2)

    def test_deepseek_without_key_is_skipped_silently(self):
        r = self.search(b=lambda *x, **k: None)
        self.assertTrue(r["ok"] and r["backend"] == "a")

    def test_pin_backend(self):
        self.assertEqual(self.search(backend="b")["backend"], "b")
        self.assertEqual(self.search(backend="a")["backend"], "a")
        self.assertFalse(self.search(backend="nope")["ok"])

    def test_empty_query(self):
        self.assertFalse(core.search("   ", cfg())["ok"])

    def test_non_fuse_primary_answers_alone(self):
        c = cfg(); c["backends"][0].pop("fuse")
        called = []
        r = self.search(b=lambda *x, **k: called.append(1) or good(), c=c)
        self.assertEqual(r["backend"], "a")
        self.assertEqual(called, [], "later backend must not be called when a non-fuse primary answers")

    def test_non_fuse_falls_through_when_empty(self):
        c = cfg(); c["backends"][0].pop("fuse")
        r = self.search(a=lambda *x: {"results": []}, c=c)
        self.assertEqual(r["backend"], "b")

    def test_runs_concurrently(self):
        def slow_a(*x): time.sleep(0.5); return {"results": good(2, "https://a.org/")}
        def slow_b(*x, **k): time.sleep(0.5); return good(2, "https://b.org/")
        t = time.time(); r = self.search(a=slow_a, b=slow_b)
        self.assertLess(time.time() - t, 0.9)
        self.assertEqual(r["backend"], "a+b")

    def test_deadline_drops_slow_backend(self):
        def hang(*x): time.sleep(3); return {"results": good()}
        t = time.time(); r = self.search(a=hang, c=cfg(backend_timeout=0.4))
        self.assertLess(time.time() - t, 1.5)
        self.assertEqual(r["backend"], "b")

    def test_merge_order_independent_of_finish_order(self):
        def slow_a(*x): time.sleep(0.3); return {"results": good(2, "https://a.org/")}
        r = self.search(a=slow_a)
        self.assertEqual(r["backends"], ["a", "b"])

    def test_hub_backend_answer_returned(self):
        c = cfg(); c["backends"] = [{"name": "h", "type": "hub", "url": "http://h"}]
        with mock.patch.object(core, "_hub_search", return_value={"ok": True, "selected": [{"url": "u"}], "results": [1]}):
            r = core.search("q", c)
        self.assertEqual(r["backend"], "h")
        self.assertEqual(r["_ranked"], [1])
        with mock.patch.object(core, "_hub_search", return_value={"ok": True, "selected": [{"url": "u"}], "results": [1]}) as h:
            core.search("q", c, allow_hub=False)
            h.assert_not_called()

    def test_hub_after_fused_backends_is_only_a_fallback(self):
        c = cfg(); c["backends"].append({"name": "h", "type": "hub", "url": "http://h"})
        hub = {"ok": True, "selected": [{"url": "u"}], "results": [1]}
        with mock.patch.object(core, "_hub_search", return_value=dict(hub)) as h:
            r = self.search(c=c)
            self.assertEqual(r["backend"], "a+b")   # the fused pool answers ...
            h.assert_not_called()                   # ... and the hub is not even asked
        def boom(*x, **k): raise OSError("down")
        with mock.patch.object(core, "_hub_search", return_value=dict(hub)) as h:
            r = self.search(a=boom, b=boom, c=c)
            self.assertEqual(r["backend"], "h")     # both fused backends failed: fall back to the hub
            h.assert_called_once()

    def test_unknown_backend_type(self):
        c = cfg(); c["backends"] = [{"name": "z", "type": "weird"}]
        r = core.search("q", c)
        self.assertFalse(r["ok"])
        self.assertIn("unknown backend type", r["tried"][0])

    def test_searxng_unresponsive_engines_reported(self):
        r = self.search(a=lambda *x: {"results": good(), "unresponsive_engines": [["bing", "timeout"]]})
        self.assertEqual(r["engines_bad"], [{"engine": "bing", "why": "timeout"}])


class SearxngGet(unittest.TestCase):
    def test_http_error_propagates_without_restart(self):
        import urllib.error
        with mock.patch.object(core._direct, "open", side_effect=urllib.error.HTTPError("u", 500, "x", {}, None)), \
                mock.patch.object(core.subprocess, "run") as run:
            with self.assertRaises(urllib.error.HTTPError):
                core._searxng_get({"url": "http://x", "start_cmd": ["echo"]}, "q=1")
            run.assert_not_called()

    def test_unreachable_without_start_cmd_raises(self):
        with mock.patch.object(core._direct, "open", side_effect=ConnectionRefusedError()):
            with self.assertRaises(OSError):
                core._searxng_get({"url": "http://x"}, "q=1")


class EnvAndConfig(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
        f.write(text); f.close(); self.addCleanup(os.unlink, f.name)
        return f.name

    def test_env_file_parsing(self):
        p = self.write("# c\nexport A_T=1\nB_T = 'two'\nC_T=\"three\"\n; x\nBAD LINE\nD_T=a=b\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            core._read_env_file(p)
            self.assertEqual([os.environ.get(k) for k in ("A_T", "B_T", "C_T", "D_T")], ["1", "two", "three", "a=b"])

    def test_never_overrides(self):
        p = self.write("K_T=file\n")
        with mock.patch.dict(os.environ, {"K_T": "env"}, clear=True):
            core._read_env_file(p)
            self.assertEqual(os.environ["K_T"], "env")

    def test_missing_file_is_silent(self):
        core._read_env_file("/nonexistent/x.env")

    def test_load_env_files_list_and_override_var(self):
        p1, p2, p3 = self.write("V1=1\n"), self.write("V2=2\n"), self.write("V3=3\n")
        with mock.patch.dict(os.environ, {"SEARCH_HUB_ENV_FILE": p3}, clear=True):
            core.load_env_files({"env_files": [p1, p2]})
            self.assertEqual([os.environ.get(k) for k in ("V1", "V2", "V3")], ["1", "2", "3"])

    def test_empty_env_files_means_none(self):
        p = self.write("ONLY_DEFAULT=1\n")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(core.os.path, "expanduser", return_value=p):
            core.load_env_files({"env_files": []})
            self.assertNotIn("ONLY_DEFAULT", os.environ)
            core.load_env_files({})                      # key absent: the default judge.env is read
            self.assertEqual(os.environ.get("ONLY_DEFAULT"), "1")

    def test_config_merge_and_bom(self):
        p = self.write("")
        with open(p, "w", encoding="utf-8-sig") as f:
            json.dump({"judge": {"enabled": False}, "backends": [{"name": "n", "type": "searxng", "url": "u"}]}, f)
        c = core.load_config(p)
        self.assertFalse(c["judge"]["enabled"])
        self.assertEqual(c["judge"]["batch"], 15)          # untouched defaults survive a partial dict
        self.assertEqual(c["backends"][0]["name"], "n")    # lists replace
        self.assertEqual(c["_path"], p)

    def test_make_opts_clamps_and_ignores_none(self):
        self.assertEqual(core.make_opts(select=None, bogus=1).select, 5)
        self.assertEqual(core.make_opts(select="7").select, 7)


if __name__ == "__main__":
    unittest.main()
