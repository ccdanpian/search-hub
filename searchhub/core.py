"""search-hub core: SearXNG (and optional hub) backends -> RRF fusion -> filtering -> optional LLM judge -> URL selection.

Standard library only. Used by the CLI (searchhub.cli), the HTTP server (searchhub.server) and the tests."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

VERSION = "1.1.0"
DEEPSEEK_CATEGORIES = ["general", "news", "it", "science"]
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CJK = re.compile(r"[一-鿿]")

DEFAULTS = {
    "backends": [{"name": "local", "type": "searxng", "url": "http://127.0.0.1:8888"}],
    "authority_file": os.path.join(ROOT, "authority.json"),
    "rrf_k": 60,
    "judge": {"enabled": True, "model": "openai/gpt-6-luna-decisions", "batch": 15, "max_batches": 3,
              "drop_below": 0.2, "timeout": 40},
    "server": {"token": "", "token_env": "SEARCH_HUB_TOKEN", "max_concurrent": 6, "allow_private_fetch": False},
    "env_files": [os.path.expanduser("~/.config/search-hub/judge.env"),
                  os.path.expanduser("~/.config/search-hub/deepseek.env")],
}

# Direct connections for backends (loopback / tailnet addresses must not go through the system proxy).
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _config_paths():
    if os.environ.get("SEARCH_HUB_CONFIG"):
        yield os.environ["SEARCH_HUB_CONFIG"]
    yield os.path.join(os.path.expanduser("~"), ".config", "search-hub", "config.json")
    yield os.path.join(ROOT, "config.json")


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULTS))
    p = path or next((x for x in _config_paths() if os.path.isfile(x)), None)
    if p:
        with open(p, encoding="utf-8-sig") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    cfg["_path"] = p
    return cfg


def load_authority(cfg):
    with open(cfg["authority_file"], encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- backends
def _searxng_get(b, qs, timeout=60):
    url = b["url"].rstrip("/") + "/search?" + qs

    def get():
        with _direct.open(url, timeout=timeout) as r:
            return json.load(r)

    try:
        return get()
    except urllib.error.HTTPError:
        raise
    except OSError:  # not reachable: try the optional start command, then wait up to ~30 s
        cmd = b.get("start_cmd")
        if not cmd:
            raise
        subprocess.run(cmd, capture_output=True, timeout=60, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
        for _ in range(15):
            time.sleep(2)
            try:
                return get()
            except OSError:
                pass
        raise


def _hub_search(b, payload, timeout=90):
    tok = b.get("token") or os.environ.get(b.get("token_env", "SEARCH_HUB_TOKEN"), "")
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(b["url"].rstrip("/") + "/search", data=json.dumps(payload).encode(), headers=headers)
    with _direct.open(req, timeout=timeout) as r:
        return json.load(r)


def _read_env_file(path):
    path = os.path.expanduser(path)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v


def load_env_file(path=None):
    """Load one KEY=VALUE file (systemd EnvironmentFile style) without overriding variables already set."""
    _read_env_file(path or os.environ.get("SEARCH_HUB_ENV_FILE")
                   or os.path.expanduser("~/.config/search-hub/judge.env"))


def load_env_files(cfg=None):
    """Load every file in cfg['env_files'] (SEARCH_HUB_ENV_FILE wins if set). Idempotent and silent on missing files."""
    if os.environ.get("SEARCH_HUB_ENV_FILE"):
        _read_env_file(os.environ["SEARCH_HUB_ENV_FILE"])
    cfg = cfg or {}
    files = cfg["env_files"] if "env_files" in cfg else [os.path.expanduser("~/.config/search-hub/judge.env")]  # [] = none
    for p in files:
        _read_env_file(p)


def deepseek_key(b):
    return b.get("key") or os.environ.get(b.get("key_env", "DEEPSEEK_API_KEY"))


def deepseek_post(b, path, body, timeout=40):
    """POST JSON to a DeepSeek tools endpoint (/v1/tools/search, /v1/tools/fetch) and return the decoded reply."""
    req = urllib.request.Request(
        b.get("url", "https://api.deepseek.com").rstrip("/") + path, method="POST", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {deepseek_key(b)}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener() if b.get("use_proxy") else _direct
    with opener.open(req, timeout=timeout) as r:
        return json.load(r)


def _deepseek_search(b, query, o, timeout=40):
    """DeepSeek Search API (POST /v1/tools/search) -> SearXNG-style items for fuse().

    Returns None when no key is configured (backend skipped silently), [] when the request succeeded with no hits."""
    if not deepseek_key(b):
        return None
    q = query
    if o.time_range and "after:" not in q and "before:" not in q:  # DeepSeek has date operators, SearXNG has ranges
        q += {"day": " after:" + datetime.now(timezone.utc).strftime("%Y-%m-%d"),
              "month": " after:" + (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),
              "year": " after:" + (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")}[o.time_range]
    body = {"query": q, "count": max(5, min(int(b.get("count") or 10), 50)), "need_content": False}
    results = deepseek_post(b, "/v1/tools/search", body, timeout).get("results", [])
    return [{"title": x.get("title", ""), "url": x["url"], "content": x.get("summary") or x.get("snippet") or "",
             "publishedDate": x.get("date"), "engines": ["deepseek"], "positions": [i + 1],
             "rerank": x.get("rerank_score")}
            for i, x in enumerate(results) if x.get("url")]


def _tavily(query):
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []
    try:
        req = urllib.request.Request(
            "https://api.tavily.com/search", method="POST",
            data=json.dumps({"query": query, "max_results": 10, "search_depth": "basic"}).encode(),
            headers={"content-type": "application/json", "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        return [{"title": x.get("title", ""), "url": x["url"], "content": x.get("content", ""),
                 "publishedDate": x.get("published_date"), "engines": ["tavily"], "positions": [i + 1]}
                for i, x in enumerate(d.get("results", []))]
    except Exception as e:
        print(f"tavily failed: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------- text helpers
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
STOP_ZH = ["最新", "是什么", "什么", "怎么", "如何", "的", "了", "吗"]
STOP_EN = {"the", "a", "an", "of", "to", "and", "is", "in", "for", "latest", "how", "what"}


def infer_date(snippet, now=None):
    """Search engines often start the snippet with a date. Return an ISO string or None."""
    now = now or time.time()
    s = snippet[:40]
    m = re.match(r"\s*(\d{4})\s*[年-]\s*(\d{1,2})\s*[月-]\s*(\d{1,2})", s)  # 2021年4月28日 / 2026-08-31
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}T00:00:00+00:00"
    m = re.match(r"\s*(\d+)\s*(小时|天|周|个月|年)前", s)  # 6天前
    if m:
        days = int(m.group(1)) * {"小时": 0, "天": 1, "周": 7, "个月": 30, "年": 365}[m.group(2)]
        return datetime.fromtimestamp(now - days * 86400, timezone.utc).isoformat()
    m = re.match(r"\s*([A-Za-z]{3})[a-z]* (\d{1,2}), (\d{4})", s)  # Oct 23, 2009
    if m and m.group(1).lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}T00:00:00+00:00"
    m = re.match(r"\s*(\d+) (hour|day|week|month|year)s? ago", s)  # 5 days ago
    if m:
        days = int(m.group(1)) * {"hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]
        return datetime.fromtimestamp(now - days * 86400, timezone.utc).isoformat()
    return None


def age_days(s, now=None):
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ref = datetime.fromtimestamp(now, timezone.utc) if now else datetime.now(timezone.utc)
        return (ref - dt).days
    except Exception:
        return None


OPERATOR = re.compile(r"(?:^|\s)(-?)(site|after|before|lang):(\S+)", re.I)


def site_filters(q):
    """(include, exclude) host lists from site:/-site: operators in the query."""
    inc, exc = [], []
    for neg, key, val in OPERATOR.findall(q):
        if key.lower() == "site":
            (exc if neg else inc).append(val.lower().removeprefix("www.").rstrip("/"))
    return inc, exc


def query_terms(q):
    q = OPERATOR.sub(" ", q.lower().replace('"', " "))
    q = re.sub(r"(?:^|\s)-\S+", " ", q)  # -excluded terms are not things the page should contain
    for w in STOP_ZH:
        q = q.replace(w, " ")
    return [t for t in re.split(r"[\s,，。；;:：、?？]+", q) if t and t not in STOP_EN]


def relevance(terms, text):
    """Share of query terms found in text; Chinese terms not found whole get partial credit from 2-char pieces."""
    if not terms:
        return 1.0
    text, score = text.lower(), 0.0
    for t in terms:
        if t in text:
            score += 1
        elif CJK.search(t):
            pieces = {t[i:i + 2] for i in range(len(t) - 1)} or {t}
            score += 0.8 * sum(1 for x in pieces if x in text) / len(pieces)
    return score / len(terms)


def host_of(u):
    """Lower-case host without 'www.' and without a default port (DeepSeek returns things like 'example.com:443')."""
    x = urllib.parse.urlsplit(u)
    h = x.netloc.lower().rpartition("@")[2]
    for port in (":443", ":80"):
        if h.endswith(port) and x.scheme in ("http", "https") and port == (":443" if x.scheme == "https" else ":80"):
            h = h[:-len(port)]
    return h.removeprefix("www.")


def norm_url(u):
    return (host_of(u) + urllib.parse.urlsplit(u).path.rstrip("/")).lower()


def _matches(h, doms):
    return any(h == d or h.endswith("." + d) for d in doms)


def authority_of(h, auth):
    return 3 if _matches(h, auth["tier3"]) else 2 if _matches(h, auth["tier2"]) else 1


def title_key(t):
    return re.sub(r"[\W_]+", "", t.lower())[:24]


# ---------------------------------------------------------------- fusion / judge / selection
def fuse(items, rrf_k=60):
    """Merge duplicate URLs; each engine that returned a URL adds 1/(k+rank) to its score."""
    merged = {}
    for r in items:
        engines = r.get("engines") or [r.get("engine", "?")]
        positions = r.get("positions") or [i + 1 for i in range(len(engines))]
        m = merged.setdefault(norm_url(r["url"]), {"title": r.get("title", "").strip(), "url": r["url"], "snippet": "",
                                                   "published_at": None, "engines": set(), "rrf": 0.0})
        for e, pos in zip(engines, positions):
            if e not in m["engines"]:
                m["rrf"] += 1.0 / (rrf_k + pos)
                m["engines"].add(e)
        snip = (r.get("content") or "").strip()
        if len(snip) > len(m["snippet"]):
            m["snippet"] = snip
        m["published_at"] = m["published_at"] or r.get("publishedDate") or r.get("published_date")
        if isinstance(r.get("rerank"), (int, float)):
            m["rerank"] = max(m.get("rerank", 0), r["rerank"])
    return merged


def judge_available(cfg):
    return bool(os.environ.get("OPENROUTER_API_KEY")) or (cfg["judge"].get("flash_fallback", True) and bool(deepseek_key({})))


def llm_judge(query, items, cfg):
    """Relevance probability per candidate: OpenRouter Decisions first, DeepSeek Flash if that is missing or fails.
    Returns (probabilities, model) or (None, None)."""
    if not items:
        return None, None
    probs, model = _openrouter_judge(query, items, cfg)
    if probs is None and cfg["judge"].get("flash_fallback", True):
        probs, model = _flash_judge(query, items, cfg)
    return probs, model


def _flash_judge(query, items, cfg):
    if not deepseek_key({}):
        return None, None
    model = cfg["judge"].get("flash_model", "deepseek-flash")
    lines = "\n".join(f"id {i}: [{m['domain']}] {m['title']} -- {m['snippet'][:300]}" for i, m in enumerate(items))
    prompt = (f"Search query: {query}\n\nFor each candidate, give the probability (0 to 1) that it is on the same topic as "
              f"the query and useful for answering it. Off-topic, spam or wrong-entity results get a low number.\n\n{lines}"
              f"\n\nReply with JSON only, one entry per candidate id: {{\"scores\": {{\"0\": 0.9, \"1\": 0.1, ...}}}}")
    try:
        reply = deepseek_post({}, "/chat/completions", {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                                                        "messages": [{"role": "user", "content": prompt}]},
                              timeout=cfg["judge"]["timeout"])
        got = json.loads(reply["choices"][0]["message"]["content"])["scores"]
        if isinstance(got, list):  # tolerated: a plain list in candidate order
            got = {str(i): x for i, x in enumerate(got)}
        missing = [i for i in range(len(items)) if str(i) not in got]
        if len(missing) > len(items) // 2:
            raise ValueError(f"scores for only {len(items) - len(missing)} of {len(items)} candidates")
        # Flash sometimes skips an id in a long list: that candidate gets a neutral 0.5 instead of failing the batch
        return [max(0.0, min(1.0, float(got.get(str(i), 0.5)))) for i in range(len(items))], f"flash:{model}"
    except Exception as e:
        print(f"flash judge failed, using heuristic only: {e}", file=sys.stderr)
        return None, None


def _openrouter_judge(query, items, cfg):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None, None
    model = os.environ.get("SEARCH_JUDGE_MODEL", cfg["judge"]["model"])
    state = json.dumps({"query": query, "candidates": [
        {"id": i, "title": m["title"], "domain": m["domain"], "snippet": m["snippet"][:300]} for i, m in enumerate(items)]},
        ensure_ascii=False)
    questions = {f"r{i}": {"type": "noul",
                           "instructions": f"Is candidate id {i} about the same topic as the query and useful for answering it?",
                           "criteria": {"true": "On topic and helps answer the query",
                                        "false": "Off topic, spam, or does not help answer the query"}}
                 for i in range(len(items))}
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/alpha/decisions", method="POST",
            data=json.dumps({"model": model, "state": state, "questions": questions}).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=cfg["judge"]["timeout"]) as r:
            answers = json.load(r)["answers"]
        return [float(answers[f"r{i}"]["noul"]) for i in range(len(items))], model
    except Exception as e:
        print(f"judge failed, using heuristic only: {e}", file=sys.stderr)
        return None, None


def rank(query, items, bad, o, cfg, auth, backend):
    """items: SearXNG-style result dicts. Returns the response dict (with a private '_ranked' list)."""
    merged = fuse(items, cfg["rrf_k"])
    qterms = query_terms(query)
    top = max((m["rrf"] for m in merged.values()), default=1) or 1
    cands, skipped = [], []
    inc, exc = site_filters(query)
    if inc and not any(_matches(host_of(m["url"]), inc) for m in merged.values()):
        inc = []  # nothing on that site came back: keep the pool rather than return nothing
    for m in merged.values():
        h = host_of(m["url"])
        if (inc and not _matches(h, inc)) or (exc and _matches(h, exc)):
            skipped.append({"url": m["url"], "reason": "site: operator"})
            continue
        path = urllib.parse.urlsplit(m["url"]).path.lower()
        if _matches(h, auth["skip_hosts"]):
            skipped.append({"url": m["url"], "reason": "video/social site"})
            continue
        if any(path.endswith(x) for x in auth["skip_ext"]):
            skipped.append({"url": m["url"], "reason": "non-html file"})
            continue
        m["published_at"] = m["published_at"] or infer_date(m["snippet"])
        days = age_days(m["published_at"]) if m["published_at"] else None
        recency = 0.0 if days is None else max(0.0, 1 - days / 365) * (0.35 if o.recent else 0.1)
        m["domain"], m["authority"] = h, authority_of(h, auth)
        m["relevance"] = round(relevance(qterms, m["title"] + " " + m["snippet"]), 2)
        if m["relevance"] < 0.05 and len(m["engines"]) < 2:  # nothing from the query, single engine: off-topic
            skipped.append({"url": m["url"], "reason": "off-topic (no query terms in title/snippet)"})
            continue
        prior = 0.03 * min(1.0, m.get("rerank", 0) / 3)  # DeepSeek's own 0-3 relevance score, when it supplied one
        m["score"] = (m["rrf"] / top + 0.15 * (m["authority"] - 1) + recency + prior) * (0.4 + 0.6 * min(1.0, m["relevance"] / 0.6))
        cands.append(m)
    cands.sort(key=lambda m: -m["score"])

    # Optional LLM judge: batches of 15, best first, until select+3 survive or max_batches used. Unjudged candidates
    # are not eligible unless the judge fails midway (then they keep a neutral p=0.5).
    jc = cfg["judge"]
    judge_model, judged = None, 0
    if jc["enabled"] and o.judge and judge_available(cfg) and cands:
        keep, pool, batches, failed = [], list(cands), 0, False
        while pool and batches < jc["max_batches"] and len(keep) < o.select + 3:
            chunk, pool = pool[:jc["batch"]], pool[jc["batch"]:]
            batches += 1
            probs, model = llm_judge(query, chunk, cfg)
            if probs is None:
                failed, pool = True, chunk + pool
                break
            judge_model = model
            judged += len(chunk)
            for m, pr in zip(chunk, probs):
                m["judge"] = round(pr, 2)
                if pr < jc["drop_below"]:
                    skipped.append({"url": m["url"], "reason": f"judge: off-topic (p={pr:.2f})"})
                    continue
                m["score"] *= 0.3 + 0.7 * pr
                keep.append(m)
        if failed and not keep:
            pass  # the judge never worked: plain heuristic ranking
        else:
            if failed:
                for m in pool:
                    m["score"] *= 0.65
            cands = keep + (pool if failed else [])
    for m in cands:
        m["score"] = round(m["score"], 4)
    cands.sort(key=lambda m: -m["score"])

    selected, per_dom, seen = [], {}, set()
    dom_cap = o.select if inc else 2  # asking for one site means wanting several pages from it
    for m in cands:
        tk = title_key(m["title"])
        if tk and tk in seen:
            skipped.append({"url": m["url"], "reason": "duplicate title"})
            continue
        if per_dom.get(m["domain"], 0) >= dom_cap:
            skipped.append({"url": m["url"], "reason": "domain cap"})
            continue
        selected.append(m)
        per_dom[m["domain"]] = per_dom.get(m["domain"], 0) + 1
        seen.add(tk)
        if len(selected) >= o.select:
            break

    def out(m):
        return {"title": m["title"], "url": m["url"], "snippet": m["snippet"][:300], "published_at": m["published_at"],
                "domain": m["domain"], "authority": m["authority"], "engines": sorted(m["engines"]), "score": m["score"],
                "relevance": m["relevance"], **({"judge": m["judge"]} if "judge" in m else {})}

    return {"ok": True, "query": query, "backend": backend, "language": o.lang, "total": len(merged),
            "judge": f"llm:{judge_model}" if judge_model else "heuristic", "judged": judged,
            "engines_ok": sorted({e for m in merged.values() for e in m["engines"]}),
            "engines_bad": [{"engine": e, "why": w} for e, w in bad],
            "selected": [out(m) for m in selected], "skipped": skipped,
            "others": [{"title": m["title"], "url": m["url"]} for m in cands if m not in selected][:10],
            "_ranked": [{"title": m["title"], "url": m["url"], "snippet": m["snippet"][:300],
                         "published_at": m["published_at"], "source_engine": "+".join(sorted(m["engines"]))}
                        for m in cands[:30]]}


# ---------------------------------------------------------------- entry point
def make_opts(**kw):
    o = SimpleNamespace(select=5, engines=None, categories="general", lang=None, time_range=None, recent=False,
                        judge=True, backend=None, allow_hub=True)
    for k, v in kw.items():
        if v is not None and hasattr(o, k):
            setattr(o, k, v)
    o.select = max(1, min(int(o.select), 20))
    return o


def search(query, cfg=None, **kw):
    """Run a search. kw: select, engines, categories, lang, time_range, recent, judge, backend, allow_hub."""
    cfg = cfg or load_config()
    load_env_files(cfg)  # judge / deepseek / tavily keys from ~/.config/search-hub/*.env
    o = make_opts(**kw)
    query = (query or "").strip()
    if not query:
        return {"ok": False, "query": query, "error": "empty query", "_ranked": []}
    o.lang = o.lang or ("zh-CN" if CJK.search(query) else "auto")
    params = {"q": query, "format": "json", "language": o.lang}
    if o.engines:  # SearXNG ignores `engines` when `categories` is sent too, so send only one of them
        params["engines"] = o.engines
    else:
        params["categories"] = o.categories
    if o.time_range:
        params["time_range"] = o.time_range
    qs = urllib.parse.urlencode(params)
    auth = load_authority(cfg)

    def pooled(b):
        """One searxng/deepseek call -> (items, unresponsive engines); items is None when the backend is skipped."""
        if b["type"] == "searxng":
            raw = _searxng_get(b, qs)
            return raw.get("results", []) + _tavily(query), raw.get("unresponsive_engines", [])
        return _deepseek_search(b, query, o), []

    def in_scope(b):
        """DeepSeek is a general-web source: skip it when the caller picked specific SearXNG engines or a non-web category
        (images, videos, music, ...), unless it was pinned with backend=."""
        if o.backend:
            return b["name"] == o.backend
        if b["type"] == "deepseek":
            return not o.engines and o.categories in b.get("categories", DEEPSEEK_CATEGORIES)
        return b["type"] != "hub" or o.allow_hub

    chosen = [b for b in cfg["backends"] if in_scope(b) and (b["type"] != "hub" or o.allow_hub)]
    # fuse-scoped backends are independent: query them concurrently, merge in config order below
    fused = [b for b in chosen if b.get("fuse") and b["type"] in ("searxng", "deepseek")]
    pool = ThreadPoolExecutor(max_workers=len(fused)) if len(fused) > 1 else None
    futures = {b["name"]: pool.submit(pooled, b) for b in fused} if pool else {}

    tried = []
    items, bad, merged_from = [], [], []
    deadline = float(cfg.get("backend_timeout", 15))  # a slow backend must not hold back the others
    t_start = time.time()
    try:
        for b in chosen:
            if items and not b.get("fuse"):  # entries without fuse are fallbacks: only tried while nothing was found
                continue
            try:
                if b["type"] in ("searxng", "deepseek"):
                    fut = futures.get(b["name"])
                    got, b_bad = fut.result(timeout=max(0.1, deadline - (time.time() - t_start))) if fut else pooled(b)
                    if got is None:
                        tried.append(f"{b['name']}: no API key, skipped")
                        continue
                    bad += b_bad
                    items += got
                    if not got:
                        tried.append(f"{b['name']}: no results")
                        continue
                    merged_from.append(b["name"])
                    if not b.get("fuse"):  # a plain primary backend answers alone, as before
                        return rank(query, items, bad, o, cfg, auth, "+".join(merged_from))
                elif b["type"] == "hub":
                    res = _hub_search(b, {"query": query, "select": o.select, "engines": o.engines,
                                          "categories": o.categories, "lang": o.lang, "time_range": o.time_range,
                                          "recent": o.recent, "judge": o.judge, "no_fallback": True})
                    if res.get("ok") and res.get("selected"):
                        res["backend"] = b["name"]
                        res["_ranked"] = res.pop("results", [])
                        return res
                    tried.append(f"{b['name']}: no results")
                else:
                    tried.append(f"{b['name']}: unknown backend type {b['type']!r}")
            except FutureTimeout:
                tried.append(f"{b['name']}: no answer within {deadline:g}s")
            except Exception as e:
                tried.append(f"{b['name']}: {type(e).__name__}: {e}")
    finally:
        if pool:
            pool.shutdown(wait=False)
    if items:  # every fuse-scoped backend contributed: rank the pool together
        res = rank(query, items, bad, o, cfg, auth, "+".join(merged_from))
        res["backends"] = merged_from
        return res
    return {"ok": False, "query": query, "error": "no results from any backend", "tried": tried,
            "engines_bad": [{"engine": e, "why": w} for e, w in bad], "_ranked": []}
