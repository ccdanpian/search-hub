import json
import os
import unittest
from unittest import mock

from tests.helpers import core, cfg
from searchhub import extract as X, mcp

PAGE_TEXT = ("Raspberry Pi 5 review.\nWe measured the board at idle: it draws 2.7 W with nothing attached, "
             "and 3.4 W with the official cooler spinning.\nUnder full load (stress-ng, 4 cores) it reached 8.8 W.\n"
             + "Unrelated filler paragraph about the case design. " * 120
             + "\n价格方面，树莓派5 的 4GB 版本\n售价 60 美元。")


def page(text=PAGE_TEXT, **kw):
    return {"ok": True, "url": "https://r.org/pi5", "title": "Pi 5 review", "text": text, "text_length": len(text), **kw}


def extractor(reply=None, exc=None, **conf):
    calls = []

    def ex(question, p):
        calls.append((question, p["text"]))
        if exc:
            raise exc
        return reply
    ex.config = {**X.DEFAULTS, **conf}
    ex.calls = calls
    return ex


class Verify(unittest.TestCase):
    def test_exact_and_whitespace_insensitive(self):
        kept, dropped = X.verify(["it draws 2.7 W with nothing attached", "IT DRAWS   2.7 w"], PAGE_TEXT)
        self.assertEqual((len(kept), dropped), (2, 0))

    def test_chinese_across_line_break_and_fullwidth(self):
        kept, _ = X.verify(["树莓派５的4GB版本售价60美元。"], PAGE_TEXT)  # full-width digit, no spaces, line break in source
        self.assertEqual(len(kept), 1)

    def test_paraphrase_and_invented_numbers_dropped(self):
        kept, dropped = X.verify(["it uses about 3 W at idle", "Under full load it reached 9.8 W."], PAGE_TEXT)
        self.assertEqual((kept, dropped), ([], 2))

    def test_ellipsis_pieces_must_each_match(self):
        self.assertEqual(len(X.verify(["We measured the board at idle ... it reached 8.8 W."], PAGE_TEXT)[0]), 1)
        self.assertEqual(len(X.verify(["We measured the board at idle … it reached 12 W."], PAGE_TEXT)[0]), 0)

    def test_smart_quotes_and_dashes(self):
        self.assertEqual(len(X.verify(["“stress–ng”"], 'ran "stress-ng" twice')[0]), 1)

    def test_garbage_input(self):
        self.assertEqual(X.verify(None, "x"), ([], 0))
        self.assertEqual(X.verify([None, 3, ""], "x"), ([], 3))


class Focus(unittest.TestCase):
    def test_returns_verified_excerpts_only(self):
        ex = extractor({"relevant": True, "note": "idle and load power",
                        "excerpts": ["it draws 2.7 W with nothing attached", "it draws 1.2 W (made up)"]})
        r = X.focus(page(), "pi 5 idle power", ex, 8000)
        self.assertEqual(r["mode"], "extract")
        self.assertEqual(r["excerpts"], ["it draws 2.7 W with nothing attached"])
        self.assertEqual(r["unverified_dropped"], 1)
        self.assertTrue(r["relevant"])
        self.assertNotIn("text", r)
        self.assertEqual(r["page_chars"], len(PAGE_TEXT))

    def test_not_relevant(self):
        r = X.focus(page(), "pi 5 idle power", extractor({"relevant": False, "excerpts": [], "note": "about cases"}), 8000)
        self.assertEqual((r["mode"], r["relevant"], r["excerpts"]), ("extract", False, []))

    def test_short_page_returned_whole_without_model_call(self):
        ex = extractor({"relevant": True, "excerpts": []})
        r = X.focus(page("short text " * 20), "q", ex, 8000)
        self.assertEqual(r["mode"], "raw (short page)")
        self.assertEqual(ex.calls, [])

    def test_model_failure_falls_back_to_raw(self):
        r = X.focus(page(), "q", extractor(exc=TimeoutError("slow")), 500)
        self.assertEqual((r["mode"], len(r["text"])), ("raw (extract failed)", 500))
        self.assertIn("slow", r["extract_error"])

    def test_all_excerpts_unverified_falls_back_to_raw(self):
        r = X.focus(page(), "q", extractor({"relevant": True, "excerpts": ["invented one", "invented two"]}), 300)
        self.assertEqual(r["mode"], "raw (excerpts failed verification)")
        self.assertEqual(r["unverified_dropped"], 2)
        self.assertEqual(len(r["text"]), 300)

    def test_output_budget(self):
        sents = [s.strip() + "." for s in PAGE_TEXT.split(".") if len(s.strip()) > 20][:6]
        r = X.focus(page(), "q", extractor({"relevant": True, "excerpts": sents}, max_output_chars=120), 8000)
        self.assertLessEqual(sum(map(len, r["excerpts"])), 120 + max(map(len, sents)))
        self.assertGreaterEqual(len(r["excerpts"]), 1)

    def test_failed_page_passes_through(self):
        bad = {"ok": False, "url": "u", "error": "x"}
        self.assertIs(X.focus(bad, "q", extractor({}), 100), bad)

    def test_long_page_reports_read_limit(self):
        r = X.focus(page(text=PAGE_TEXT, text_length=200000), "q",
                    extractor({"relevant": True, "excerpts": ["it reached 8.8 W"]}), 8000)
        self.assertEqual(r["read_chars"], len(PAGE_TEXT))


class Extractor(unittest.TestCase):
    def test_request_shape(self):
        from tests.test_backends import FakeResp
        reply = FakeResp({"choices": [{"message": {"content": json.dumps({"relevant": True, "excerpts": ["a"], "note": ""})}}]})
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k"}, clear=True), mock.patch.object(core._direct, "open", return_value=reply) as o:
            ex = X.make_extractor({"env_files": []})
            out = ex("what power?", page())
        body = json.loads(o.call_args[0][0].data)
        self.assertEqual(out["excerpts"], ["a"])
        self.assertEqual((body["model"], body["temperature"]), ("deepseek-flash", 0))
        self.assertIn("VERBATIM", body["messages"][0]["content"])
        self.assertNotIn("MAX_N", body["messages"][0]["content"])
        self.assertIn("what power?", body["messages"][1]["content"])
        self.assertIn("<<<PAGE", body["messages"][1]["content"])

    def test_disabled_or_no_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(X.make_extractor({"env_files": []}))
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k"}, clear=True):
            self.assertIsNone(X.make_extractor({"env_files": [], "extract": {"enabled": False}}))
            self.assertEqual(X.make_extractor({"env_files": [], "extract": {"max_excerpts": 3}}).config["max_excerpts"], 3)


class FetchFocused(unittest.TestCase):
    def test_reads_more_than_max_chars_then_extracts(self):
        seen = []
        def fake_fetch(u, max_chars, timeout, allow_private, fallback):
            seen.append(max_chars); return page()
        ex = extractor({"relevant": True, "excerpts": ["it reached 8.8 W"]})
        with mock.patch.object(X.fetchmod, "fetch", side_effect=fake_fetch), mock.patch.object(X, "make_extractor", return_value=ex):
            pages = X.fetch_focused(["https://r.org/1", "https://r.org/2"], "load power", cfg(), max_chars=8000)
        self.assertEqual(seen, [60000, 60000])
        self.assertEqual([p["mode"] for p in pages], ["extract", "extract"])

    def test_no_question_is_plain_fetch(self):
        with mock.patch.object(X.fetchmod, "fetch_many", return_value=[page()]) as fm, mock.patch.object(X, "make_extractor") as mk:
            pages = X.fetch_focused(["u"], "", cfg())
        mk.assert_not_called()
        self.assertNotIn("mode", pages[0])
        fm.assert_called_once()

    def test_question_without_extractor_is_marked(self):
        with mock.patch.object(X.fetchmod, "fetch_many", return_value=[page()]), mock.patch.object(X, "make_extractor", return_value=None):
            self.assertEqual(X.fetch_focused(["u"], "q", cfg())[0]["mode"], "raw (extractor unavailable)")


class McpOutput(unittest.TestCase):
    def test_compact_search(self):
        from tests.test_surfaces import FAKE
        out = mcp.compact(dict(FAKE))
        self.assertEqual(set(out), {"ok", "query", "backend", "judge", "results", "more"})
        self.assertEqual(out["results"][0], {"title": "T", "url": "https://a.org", "snippet": "s", "date": "2026-10-01",
                                             "domain": "a.org", "authority": 2})
        self.assertEqual(out["more"], ["O | https://o.org"])

    def test_compact_failure_keeps_reason(self):
        self.assertEqual(mcp.compact({"ok": False, "query": "q", "error": "e", "tried": ["a: x"], "engines_bad": [1]}),
                         {"ok": False, "query": "q", "error": "e", "tried": ["a: x"]})

    def test_verbose_returns_everything(self):
        from tests.test_surfaces import FAKE
        with mock.patch.object(core, "search", return_value=dict(FAKE)):
            r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "web_search", "arguments": {"query": "q", "verbose": True}}}, cfg())
        self.assertIn("skipped", json.loads(r["result"]["content"][0]["text"]))

    def test_fetch_question_is_forwarded(self):
        with mock.patch.object(X, "fetch_focused", return_value=[{"ok": True}]) as ff:
            mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "web_fetch", "arguments": {"urls": ["https://a.org"], "question": " idle power "}}}, cfg())
        self.assertEqual(ff.call_args[0][1], "idle power")


if __name__ == "__main__":
    unittest.main()
