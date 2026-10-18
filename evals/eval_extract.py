"""Does web_fetch(question=...) keep the information that matters, and how many tokens does it save?

Pages: the top-2 fused results per query from a run_eval.py output. For every page:
  facts  = DeepSeek Flash lists up to 3 key facts that answer the query, reading the FULL page (up to 60k chars)
  A      = what an agent gets today without a question: the first 8000 chars of raw text
  B      = what it gets with question=: verified excerpts (or the raw fallback)
  recall = share of facts present in A / in B, judged by Flash. Token counts are estimated (CJK 1/char, else 1/3.6 chars).
Usage: python3 evals/eval_extract.py <run_eval output> <out.jsonl> [earlier extract output to reuse its facts]
Limits: Flash both extracts and grades (possible self-preference); facts come from one model; one run."""
import tempfile
import json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from searchhub import core, extract as X, fetch as F

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), "search-hub-eval.jsonl")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(tempfile.gettempdir(), "search-hub-extract.jsonl")
cfg = core.load_config(); core.load_env_files(cfg)
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7890"); os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:7890")
ex, fb = X.make_extractor(cfg), F.make_fallback(cfg)
PREV = {}  # reuse facts from an earlier run (3rd argument) so A/B differences are not fact-list noise
if len(sys.argv) > 3:
    PREV = {r["url"]: r["facts"] for r in map(json.loads, open(sys.argv[3], encoding="utf-8")) if r.get("ok")}


def tok(s):
    cjk = len(re.findall(r"[　-鿿＀-￯]", s)); return int(cjk + (len(s) - cjk) / 3.6)


def flash(prompt):
    for _ in range(3):
        try:
            r = core.deepseek_post({}, "/chat/completions", {"model": "deepseek-flash", "temperature": 0, "response_format": {"type": "json_object"},
                                                             "messages": [{"role": "user", "content": prompt}]}, timeout=90)
            return json.loads(r["choices"][0]["message"]["content"])
        except Exception as e:
            err = e; time.sleep(2)
    raise err


def facts_of(q, text):
    return flash(f"Question: {q}\n\nPage:\n<<<\n{text}\n>>>\n\nList up to 3 key facts from this page that help answer the question, "
                 "each as one short self-contained statement with its specific numbers/names. If the page does not help, return none.\n"
                 'Reply JSON only: {"facts": ["...", ...]}').get("facts") or []


def present(facts, material):
    if not facts:
        return []
    lines = "\n".join(f"{i}. {f}" for i, f in enumerate(facts))
    p = flash(f"Material:\n<<<\n{material}\n>>>\n\nFor each statement below, is the information it states contained in the material "
              f"(same meaning and same specific numbers/names)?\n{lines}\n\nReply JSON only: {{\"present\": [true/false per statement, in order]}}").get("present") or []
    return [bool(x) for x in p][:len(facts)] + [False] * (len(facts) - len(p))


def material_b(p):
    """What the agent sees: the page title plus excerpts (the unverified note is left out), or the raw text."""
    if p.get("mode") == "extract":
        return f"Title: {p.get('title', '')}\n\n" + ("\n\n".join(p["excerpts"]) if p["excerpts"] else "(no relevant content)")
    return p.get("text", "")


def one(job):
    q, url = job
    full = F.fetch(url, 60000, 20, False, fb)
    if not full.get("ok"):
        return {"q": q, "url": url, "ok": False, "error": full.get("error")}
    a_text = f"Title: {full.get('title', '')}\n\n" + full["text"][:8000]
    t = time.time()
    b = X.focus(dict(full), q, ex, 8000)
    b_ms = int((time.time() - t) * 1000)
    b_json = json.dumps({k: v for k, v in b.items() if k in ("excerpts", "note", "text", "relevant", "mode")}, ensure_ascii=False)
    facts = PREV[url] if url in PREV else facts_of(q, full["text"])
    mb = material_b(b)
    return {"q": q, "url": url, "ok": True, "page_chars": full["text_length"], "mode": b.get("mode"), "dropped": b.get("unverified_dropped", 0),
            "tok_a": tok(a_text), "tok_b": tok(b_json), "b_ms": b_ms, "facts": facts,
            "rec_a": present(facts, a_text), "rec_b": present(facts, mb), "note": b.get("note"), "material_b": mb[:6000]}


jobs = []
for l in open(SRC, encoding="utf-8"):
    r = json.loads(l)
    jobs += [(r["q"], m["url"]) for m in r["runs"]["fused"]["sel"][:2]]
with open(OUT, "w", encoding="utf-8") as out, ThreadPoolExecutor(5) as pool:
    for i, res in enumerate(pool.map(one, jobs), 1):
        out.write(json.dumps(res, ensure_ascii=False) + "\n"); out.flush()
        print(f"[{i}/{len(jobs)}] {res['url'][:60]:60s} " + (f"{res['mode']:<28s} A={res['tok_a']} B={res['tok_b']} facts={len(res['facts'])} "
              f"recA={sum(res['rec_a'])} recB={sum(res['rec_b'])}" if res["ok"] else f"FETCH FAIL {res['error'][:50]}"), flush=True)
