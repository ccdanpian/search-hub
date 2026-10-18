import time
import unittest

from tests.helpers import core, AUTH


class InferDate(unittest.TestCase):
    NOW = time.mktime((2026, 10, 18, 12, 0, 0, 0, 0, 0))

    def test_formats(self):
        self.assertTrue(core.infer_date("2021年4月28日 ... text").startswith("2021-04-28"))
        self.assertTrue(core.infer_date("2026-08-31 text").startswith("2026-08-31"))
        self.assertTrue(core.infer_date("Oct 23, 2009 - foo").startswith("2009-10-23"))
        self.assertIsNotNone(core.infer_date("6天前 - foo", self.NOW))
        self.assertIsNotNone(core.infer_date("5 days ago - foo", self.NOW))

    def test_relative_is_relative(self):
        d = core.infer_date("3 days ago", self.NOW)
        self.assertEqual(core.age_days(d, self.NOW), 3)

    def test_no_date(self):
        self.assertIsNone(core.infer_date("Raspberry Pi 5 review"))
        self.assertIsNone(core.infer_date(""))

    def test_age_days_bad_input(self):
        self.assertIsNone(core.age_days("garbage"))
        self.assertIsNotNone(core.age_days("2026-01-01T00:00:00Z"))
        self.assertIsNotNone(core.age_days("2026-01-01"))  # naive -> UTC


class Terms(unittest.TestCase):
    def test_stop_words(self):
        self.assertEqual(core.query_terms("the latest Python 3.14 release"), ["python", "3.14", "release"])
        self.assertEqual(core.query_terms("树莓派5是什么"), ["树莓派5"])

    def test_relevance(self):
        t = core.query_terms("python free-threading")
        self.assertEqual(core.relevance(t, "Python free-threading build"), 1.0)
        self.assertEqual(core.relevance(t, "cooking pasta"), 0.0)
        self.assertEqual(core.relevance([], "anything"), 1.0)

    def test_cjk_partial_credit(self):
        t = core.query_terms("苹果手机")
        full = core.relevance(t, "苹果手机发布")
        part = core.relevance(t, "苹果新品发布")
        self.assertEqual(full, 1.0)
        self.assertTrue(0 < part < 1.0)


class Urls(unittest.TestCase):
    def test_host_of(self):
        self.assertEqual(core.host_of("https://www.Example.com/x"), "example.com")
        self.assertEqual(core.host_of("https://example.com:443/x"), "example.com")
        self.assertEqual(core.host_of("http://example.com:80/x"), "example.com")
        self.assertEqual(core.host_of("https://example.com:8443/x"), "example.com:8443")
        self.assertEqual(core.host_of("https://example.com:80/x"), "example.com:80")  # non-default for https
        self.assertEqual(core.host_of("http://user:pw@example.com/x"), "example.com")

    def test_norm_url_merges_variants(self):
        a = core.norm_url("https://www.a.com:443/p/")
        self.assertEqual(a, core.norm_url("http://a.com/p"))
        self.assertNotEqual(a, core.norm_url("https://a.com/q"))

    def test_authority(self):
        self.assertEqual(core.authority_of("docs.python.org", AUTH), 3)
        self.assertEqual(core.authority_of("python.org", AUTH), 3)
        self.assertEqual(core.authority_of("notpython.org", AUTH), 1)  # suffix must be on a dot boundary
        self.assertEqual(core.authority_of("github.com", AUTH), 2)
        self.assertEqual(core.authority_of("random.example", AUTH), 1)

    def test_title_key(self):
        self.assertEqual(core.title_key("Hello, World!"), core.title_key("hello world"))
        self.assertEqual(core.title_key("!!!"), "")


class AuthorityFile(unittest.TestCase):
    def test_real_file_is_sane(self):
        a = core.load_authority(core.load_config())
        self.assertFalse(set(a["tier3"]) & set(a["tier2"]), "domain listed in both tiers")
        for k in ("tier3", "tier2", "skip_hosts", "skip_ext"):
            self.assertTrue(a[k] and all(isinstance(x, str) and x == x.lower() for x in a[k]), k)
        self.assertTrue(all(x.startswith(".") for x in a["skip_ext"]))
        self.assertTrue(all(not d.startswith("www.") and "/" not in d for d in a["tier3"] + a["tier2"] + a["skip_hosts"]))


if __name__ == "__main__":
    unittest.main()
