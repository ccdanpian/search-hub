"""Shared fixtures: a fake authority table, SearXNG-style items and a default config."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from searchhub import core  # noqa: E402

# Offline tests must behave the same on every machine: no real keys, no key files, no user config.
for _k in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "TAVILY_API_KEY", "SEARCH_HUB_ENV_FILE", "SEARCH_JUDGE_MODEL"):
    os.environ.pop(_k, None)
core.DEFAULTS["env_files"] = []

AUTH = {"tier3": ["python.org", "arxiv.org"], "tier2": ["github.com", "zhihu.com"],
        "skip_hosts": ["youtube.com", "bilibili.com"], "skip_ext": [".pdf", ".jpg"]}


def item(url, title="", content="", engines=("e1",), positions=None, date=None):
    d = {"url": url, "title": title, "content": content, "engines": list(engines),
         "positions": list(positions or range(1, len(engines) + 1))}
    if date:
        d["publishedDate"] = date
    return d


def cfg(**over):
    c = core.json.loads(core.json.dumps(core.DEFAULTS))
    c["_path"] = None
    c["backends"] = [{"name": "a", "type": "searxng", "url": "http://x", "fuse": True},
                     {"name": "b", "type": "deepseek", "key": "k", "fuse": True}]
    c["judge"]["enabled"] = False
    c.update(over)
    return c


def opts(**kw):
    return core.make_opts(**kw)
