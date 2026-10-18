import json
import unittest
from unittest import mock

from tests.helpers import core, AUTH, item, cfg, opts


def run_rank(items, query="python release", judge=False, **kw):
    return core.rank(query, items, [], opts(judge=judge, **kw), cfg(), AUTH, "t")


class Fuse(unittest.TestCase):
    def test_same_url_from_two_engines_merges_and_sums_rrf(self):
        m = core.fuse([item("https://a.com/x", "T", "short", ["e1"], [1]),
                       item("https://www.a.com/x/", "T", "a longer snippet", ["e2"], [3])])
        self.assertEqual(len(m), 1)
        v = next(iter(m.values()))
        self.assertEqual(v["engines"], {"e1", "e2"})
        self.assertAlmostEqual(v["rrf"], 1 / 61 + 1 / 63)
        self.assertEqual(v["snippet"], "a longer snippet")

    def test_same_engine_twice_counts_once(self):
        m = core.fuse([item("https://a.com/x", engines=["e1"], positions=[1]), item("https://a.com/x", engines=["e1"], positions=[2])])
        self.assertAlmostEqual(next(iter(m.values()))["rrf"], 1 / 61)

    def test_missing_engine_field(self):
        m = core.fuse([{"url": "https://a.com/x", "engine": "solo", "title": "t"}])
        self.assertEqual(next(iter(m.values()))["engines"], {"solo"})

    def test_date_kept(self):
        m = core.fuse([item("https://a.com/x", engines=["e1"]), item("https://a.com/x", engines=["e2"], date="2026-01-02")])
        self.assertEqual(next(iter(m.values()))["published_at"], "2026-01-02")


class Rank(unittest.TestCase):
    def test_skips_hosts_and_extensions(self):
        r = run_rank([item("https://youtube.com/watch?v=1", "python release", "python release"),
                      item("https://x.org/paper.pdf", "python release", "python release"),
                      item("https://x.org/a", "python release notes", "python release")])
        self.assertEqual([s["url"] for s in r["selected"]], ["https://x.org/a"])
        self.assertEqual({s["reason"] for s in r["skipped"]}, {"video/social site", "non-html file"})

    def test_off_topic_single_engine_dropped_but_two_engines_kept(self):
        r = run_rank([item("https://a.org/1", "cooking pasta", "boil water", ["e1"]),
                      item("https://b.org/1", "cooking pasta", "boil water", ["e1", "e2"])])
        self.assertEqual([s["url"] for s in r["selected"]], ["https://b.org/1"])

    def test_domain_cap_two(self):
        items = [item(f"https://same.org/{i}", f"python release {i}", "python release", ["e1"], [i + 1]) for i in range(6)]
        r = run_rank(items, select=5)
        self.assertEqual(len(r["selected"]), 2)
        self.assertIn("domain cap", {s["reason"] for s in r["skipped"]})

    def test_duplicate_title_dropped(self):
        r = run_rank([item("https://a.org/1", "Python release notes", "python release"),
                      item("https://b.org/2", "python release  notes!", "python release")])
        self.assertEqual(len(r["selected"]), 1)

    def test_authority_outranks_equal_rrf(self):
        r = run_rank([item("https://blog.example/1", "python release", "python release", ["e1"], [1]),
                      item("https://python.org/1", "python release official", "python release", ["e2"], [1])])
        self.assertEqual(r["selected"][0]["domain"], "python.org")
        self.assertEqual(r["selected"][0]["authority"], 3)

    def test_select_bounds(self):
        items = [item(f"https://h{i}.org/", f"python release {i}", "python release", ["e1"], [i + 1]) for i in range(30)]
        self.assertEqual(len(run_rank(items, select=3)["selected"]), 3)
        self.assertEqual(len(run_rank(items, select=999)["selected"]), 20)
        self.assertEqual(len(run_rank(items, select=0)["selected"]), 1)

    def test_recent_prefers_newer(self):
        new = item("https://n.org/1", "python release", "python release", ["e1"], [2], date="2026-10-01T00:00:00+00:00")
        old = item("https://o.org/1", "python release", "python release", ["e1"], [1], date="2019-01-01T00:00:00+00:00")
        self.assertEqual(run_rank([old, new], recent=True)["selected"][0]["domain"], "n.org")

    def test_empty_and_response_shape(self):
        r = run_rank([])
        self.assertTrue(r["ok"] and r["selected"] == [] and r["total"] == 0)
        r = run_rank([item("https://a.org/1", "python release", "python release")])
        for k in ("backend", "judge", "engines_ok", "engines_bad", "selected", "skipped", "others", "_ranked"):
            self.assertIn(k, r)
        self.assertEqual(r["judge"], "heuristic")


class Judge(unittest.TestCase):
    ITEMS = [item(f"https://h{i}.org/", f"python release {i}", "python release", ["e1"], [i + 1]) for i in range(4)]

    def test_judge_drops_low_probability(self):
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "x"}), \
                mock.patch.object(core, "llm_judge", side_effect=lambda q, ch, c: ([0.9, 0.05, 0.9, 0.9][:len(ch)], "m")):
            c = cfg(); c["judge"]["enabled"] = True
            r = core.rank("python release", self.ITEMS, [], opts(judge=True, select=5), c, AUTH, "t")
        self.assertEqual(r["judge"], "llm:m")
        self.assertEqual(len(r["selected"]), 3)
        self.assertTrue(any("judge: off-topic" in s["reason"] for s in r["skipped"]))

    def test_judge_failure_falls_back_to_heuristic(self):
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "x"}), \
                mock.patch.object(core, "llm_judge", return_value=(None, None)):
            c = cfg(); c["judge"]["enabled"] = True
            r = core.rank("python release", self.ITEMS, [], opts(judge=True, select=5), c, AUTH, "t")
        self.assertEqual(r["judge"], "heuristic")
        self.assertEqual(len(r["selected"]), 4)

    def test_judge_off_when_no_key_or_disabled(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(core, "llm_judge") as j:
            c = cfg(); c["judge"]["enabled"] = True
            core.rank("python release", self.ITEMS, [], opts(judge=True), c, AUTH, "t")
            j.assert_not_called()

    def test_llm_judge_request_and_error(self):
        class R:
            def __enter__(s): return s
            def __exit__(s, *a): pass
            def read(s): return b'{"answers": {"r0": {"noul": 0.7}, "r1": {"noul": 0.1}}}'
        ms = [{"title": "a", "domain": "d", "snippet": "s"}, {"title": "b", "domain": "d", "snippet": "s"}]
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "x"}, clear=True), mock.patch("urllib.request.urlopen", return_value=R()) as u:
            probs, model = core.llm_judge("q", ms, cfg())
        self.assertEqual(probs, [0.7, 0.1])
        self.assertEqual(u.call_args[0][0].get_header("Authorization"), "Bearer x")
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "x"}, clear=True), mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertEqual(core.llm_judge("q", ms, cfg()), (None, None))


if __name__ == "__main__":
    unittest.main()


class Operators(unittest.TestCase):
    def test_site_filters_parse(self):
        self.assertEqual(core.site_filters("a site:www.GitHub.com -site:x.org b"), (["github.com"], ["x.org"]))
        self.assertEqual(core.site_filters("plain query"), ([], []))

    def test_terms_ignore_operators(self):
        self.assertEqual(core.query_terms('site:github.com searxng -bing "exact phrase" after:2026'),
                         ["searxng", "exact", "phrase"])

    def test_site_include_filters_pool(self):
        r = run_rank([item("https://github.com/a/b", "searxng engine", "searxng engine", ["e1"], [2]),
                      item("https://other.org/x", "searxng engine", "searxng engine", ["e1"], [1])], query="site:github.com searxng engine")
        self.assertEqual([s["domain"] for s in r["selected"]], ["github.com"])
        self.assertIn("site: operator", {s["reason"] for s in r["skipped"]})

    def test_site_filter_ignored_when_nothing_matches(self):
        r = run_rank([item("https://other.org/x", "searxng engine", "searxng engine")], query="site:github.com searxng engine")
        self.assertEqual(len(r["selected"]), 1)

    def test_site_exclude(self):
        r = run_rank([item("https://bad.org/x", "python release", "python release", ["e1"], [1]),
                      item("https://ok.org/x", "python release", "python release", ["e1"], [2])], query="python release -site:bad.org")
        self.assertEqual([s["domain"] for s in r["selected"]], ["ok.org"])

    def test_deepseek_rerank_prior_breaks_ties(self):
        lo = item("https://lo.org/1", "python release", "python release", ["deepseek"], [1]); lo["rerank"] = 0.2
        hi = item("https://hi.org/1", "python release", "python release", ["deepseek"], [1]); hi["rerank"] = 2.9
        self.assertEqual(run_rank([lo, hi])["selected"][0]["domain"], "hi.org")

    def test_adapter_passes_rerank_through(self):
        from tests.test_backends import FakeResp
        with mock.patch.object(core._direct, "open", return_value=FakeResp({"results": [{"url": "https://a.org", "rerank_score": 2.5}]})):
            out = core._deepseek_search({"key": "k"}, "q", core.make_opts())
        self.assertEqual(out[0]["rerank"], 2.5)


class SiteCap(unittest.TestCase):
    def test_domain_cap_lifted_for_site_queries(self):
        items = [item(f"https://github.com/o/r{i}", f"searxng engine {i}", "searxng engine", ["e1"], [i + 1]) for i in range(6)]
        self.assertEqual(len(run_rank(items, query="site:github.com searxng engine", select=5)["selected"]), 5)
        self.assertEqual(len(run_rank(items, query="searxng engine", select=5)["selected"]), 2)


class FlashJudge(unittest.TestCase):
    MS = [{"title": "a", "domain": "d", "snippet": "s"}, {"title": "b", "domain": "d", "snippet": "s"}]

    @staticmethod
    def reply(obj):
        from tests.test_backends import FakeResp
        return FakeResp({"choices": [{"message": {"content": json.dumps(obj)}}]})

    def test_used_when_openrouter_missing(self):
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(core._direct, "open", return_value=self.reply({"scores": {"0": 0.9, "1": 1.7}})) as o:
            probs, model = core.llm_judge("q", self.MS, cfg())
        self.assertEqual((probs, model), ([0.9, 1.0], "flash:deepseek-flash"))
        self.assertTrue(o.call_args[0][0].full_url.endswith("/chat/completions"))

    def test_used_when_openrouter_fails(self):
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k", "OPENROUTER_API_KEY": "x"}, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("down")), \
                mock.patch.object(core._direct, "open", return_value=self.reply({"scores": {"0": 0.2, "1": 0.8}})):
            self.assertEqual(core.llm_judge("q", self.MS, cfg())[1], "flash:deepseek-flash")

    def test_wrong_length_or_disabled(self):
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(core._direct, "open", return_value=self.reply({"scores": {}})):
            self.assertEqual(core.llm_judge("q", self.MS, cfg()), (None, None))
        c = cfg(); c["judge"]["flash_fallback"] = False
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), mock.patch.object(core._direct, "open") as o:
            self.assertEqual(core.llm_judge("q", self.MS, c), (None, None))
            self.assertFalse(core.judge_available(c))
            o.assert_not_called()

    def test_rank_runs_judge_with_only_deepseek_key(self):
        items = [item(f"https://h{i}.org/", f"python release {i}", "python release", ["e1"], [i + 1]) for i in range(3)]
        c = cfg(); c["judge"]["enabled"] = True
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(core._direct, "open", return_value=self.reply({"scores": {"0": 0.9, "1": 0.05, "2": 0.9}})):
            r = core.rank("python release", items, [], opts(judge=True), c, AUTH, "t")
        self.assertEqual(r["judge"], "llm:flash:deepseek-flash")
        self.assertEqual(len(r["selected"]), 2)


class FlashJudgeTolerance(unittest.TestCase):
    def test_one_missing_id_gets_neutral_score(self):
        ms = [{"title": str(i), "domain": "d", "snippet": "s"} for i in range(15)]
        scores = {str(i): 0.9 for i in range(15) if i != 7}
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(core._direct, "open", return_value=FlashJudge.reply({"scores": scores})):
            probs, model = core.llm_judge("q", ms, cfg())
        self.assertEqual(len(probs), 15)
        self.assertEqual(probs[7], 0.5)
        self.assertEqual(model, "flash:deepseek-flash")

    def test_list_form_accepted(self):
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(core._direct, "open", return_value=FlashJudge.reply({"scores": [0.3, 0.6]})):
            self.assertEqual(core.llm_judge("q", FlashJudge.MS, cfg())[0], [0.3, 0.6])
