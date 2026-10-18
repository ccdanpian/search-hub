"""Quality evaluation of search-hub (opt-in, uses the network).

For every query it runs three variants -- SearXNG only, DeepSeek only, fused -- with the OpenRouter judge OFF, then grades each
distinct (query, url) once with a cheap LLM (DeepSeek `deepseek-flash`) from title/domain/snippet only, blind to the source.
Usage: python3 evals/run_eval.py [queries.json] [out.jsonl]
Limits: grading sees snippets, not pages; the grader is one model; this measures relevance, not truth or freshness."""
import tempfile
import json, os, sys, time, urllib.request, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from searchhub import core

QS = json.load(open(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "queries.json"), encoding="utf-8"))
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(tempfile.gettempdir(), "search-hub-eval.jsonl")
cfg = core.load_config(); core.load_env_files(cfg)
KEY = os.environ["DEEPSEEK_API_KEY"]
MODEL = os.environ.get("EVAL_GRADER_MODEL", "deepseek-flash")
VARIANTS = {"searxng": {"backend": "pi-searxng"}, "deepseek": {"backend": "deepseek"}, "fused": {}}
_direct = core._direct


def grade(query, items):
    """items: [(title, domain, snippet)] -> list of 0/1/2."""
    lines = "\n".join(f"{i}. [{d}] {t} -- {s[:220]}" for i, (t, d, s) in enumerate(items))
    prompt = (f"Search query: {query}\n\nGrade each result for how well it helps answer the query, using only the title, domain "
              f"and snippet.\n2 = directly answers / the right page, 1 = related and somewhat useful, 0 = off-topic, spam, or wrong "
              f"entity.\n\n{lines}\n\nReply with JSON only: {{\"scores\": [one integer per result, in order]}}")
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0,
            "response_format": {"type": "json_object"}}
    req = urllib.request.Request("https://api.deepseek.com/chat/completions", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with _direct.open(req, timeout=90) as r:
                txt = json.load(r)["choices"][0]["message"]["content"]
            sc = json.loads(txt)["scores"]
            if len(sc) == len(items):
                return [max(0, min(2, int(x))) for x in sc]
        except Exception as e:
            err = e
            time.sleep(1.5)
    raise RuntimeError(f"grader failed: {err}")


cache = {}
with open(OUT, "w", encoding="utf-8") as out:
    for n, spec in enumerate(QS, 1):
        q, runs = spec["q"], {}
        for name, kw in VARIANTS.items():
            t = time.time()
            try:
                r = core.search(q, cfg, judge=False, select=5, recent=bool(spec.get("recent")), **kw)
            except Exception as e:
                r = {"ok": False, "error": repr(e), "selected": []}
            runs[name] = {"ms": int((time.time() - t) * 1000), "ok": r.get("ok"), "err": r.get("error"),
                          "sel": [{"url": m["url"], "title": m["title"], "domain": m["domain"], "snippet": m["snippet"],
                                   "authority": m["authority"], "engines": m["engines"]} for m in r.get("selected", [])]}
        need = {m["url"]: (m["title"], m["domain"], m["snippet"]) for v in runs.values() for m in v["sel"] if (q, m["url"]) not in cache}
        if need:
            urls = list(need)
            for u, s in zip(urls, grade(q, [need[u] for u in urls])):
                cache[(q, u)] = s
        for v in runs.values():
            for m in v["sel"]:
                m["grade"] = cache[(q, m["url"])]
        out.write(json.dumps({"q": q, "tag": spec["tag"], "runs": runs}, ensure_ascii=False) + "\n"); out.flush()
        print(f"[{n}/{len(QS)}] {q[:40]:40s} " + " ".join(f"{k}:{len(v['sel'])}r/{v['ms']}ms" for k, v in runs.items()), flush=True)
