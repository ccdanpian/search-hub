"""Command line: search, fetch, serve, doctor."""
import argparse
import json
import os
import sys

from . import core, extract, fetch as fetchmod


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def cmd_search(a, cfg):
    res = core.search(a.query, cfg, select=a.select, engines=a.engines, categories=a.categories, lang=a.lang,
                      time_range=a.time_range, recent=a.recent, judge=not a.no_judge, backend=a.backend)
    res.pop("_ranked", None)
    if a.text and res.get("ok"):
        for i, m in enumerate(res["selected"], 1):
            print(f"{i}. {m['title']}\n   {m['url']}\n   [{'+'.join(m['engines'])}] auth={m['authority']} "
                  f"score={m['score']}  {m['snippet'][:160]}\n")
        print(f"engines ok: {res['engines_ok']} | bad: {[(b['engine'], b['why']) for b in res['engines_bad']]} "
              f"| backend: {res['backend']} | judge: {res['judge']}", file=sys.stderr)
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def cmd_fetch(a, cfg):
    pages = extract.fetch_focused(a.urls, a.question, cfg, a.max_chars, a.timeout, bool(cfg["server"].get("allow_private_fetch")),
                                  None if a.no_fallback else fetchmod.make_fallback(cfg))
    print(json.dumps(pages, ensure_ascii=False, indent=2))
    return 0 if any(p.get("ok") for p in pages) else 1


def cmd_serve(a, cfg):
    from . import server
    server.serve(a.listen or ["127.0.0.1:9090"], cfg)
    return 0


def cmd_doctor(a, cfg):
    ok = True

    def line(good, msg):
        nonlocal ok
        ok = ok and good
        print(("[ok]   " if good else "[FAIL] ") + msg)
    print(f"search-hub {core.VERSION}, python {sys.version.split()[0]}")
    line(True, f"config: {cfg['_path'] or '(none, using defaults)'}")
    try:
        auth = core.load_authority(cfg)
        both = set(auth["tier3"]) & set(auth["tier2"])
        line(not both, f"authority file: {len(auth['tier3'])} tier3, {len(auth['tier2'])} tier2" + (f", in both tiers: {both}" if both else ""))
    except Exception as e:
        line(False, f"authority file unreadable: {e}")
    core.load_env_files(cfg)  # so the key checks below see the same environment as a real search
    key = bool(os.environ.get("OPENROUTER_API_KEY"))
    print(f"[info] LLM judge: {'key set' if key else 'OPENROUTER_API_KEY not set -> heuristic only'}"
          f"{'' if cfg['judge']['enabled'] else ' (disabled in config)'}")
    print(f"[info] Tavily: {'key set' if os.environ.get('TAVILY_API_KEY') else 'not set (optional)'}")
    print(f"[info] env files: {cfg.get('env_files') or ['~/.config/search-hub/judge.env (default)']}")
    for b in cfg["backends"]:
        needed = b.get("key_env", "DEEPSEEK_API_KEY") if b["type"] == "deepseek" else None
        if needed and not os.environ.get(needed):
            print(f"[skip] backend {b['name']} ({b['type']}): {needed} not set")
            continue
        r = core.search("test", cfg, backend=b["name"], judge=False, select=1)
        line(bool(r.get("ok")), f"backend {b['name']} ({b['type']}): " +
             (f"engines ok {r['engines_ok']}" if r.get("ok") else f"{r.get('tried') or r.get('error')}"))
    return 0 if ok else 1


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(prog="searchhub")
    ap.add_argument("--config", help="path to config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("-n", "--select", type=int, default=5)
    s.add_argument("-e", "--engines")
    s.add_argument("-c", "--categories", default="general")
    s.add_argument("-l", "--lang")
    s.add_argument("-t", "--time-range", choices=["day", "month", "year"])
    s.add_argument("--recent", action="store_true")
    s.add_argument("--no-judge", action="store_true")
    s.add_argument("--backend", help="use only this configured backend")
    s.add_argument("--text", action="store_true")
    s.set_defaults(fn=cmd_search)

    f = sub.add_parser("fetch")
    f.add_argument("urls", nargs="+")
    f.add_argument("--max-chars", type=int, default=8000)
    f.add_argument("--timeout", type=int, default=20)
    f.add_argument("--no-fallback", action="store_true", help="do not retry unreadable pages through DeepSeek Fetch")
    f.add_argument("-q", "--question", help="return only verified excerpts relevant to this question")
    f.set_defaults(fn=cmd_fetch)

    v = sub.add_parser("serve")
    v.add_argument("--listen", action="append", metavar="HOST:PORT", help="repeatable; default 127.0.0.1:9090")
    v.set_defaults(fn=cmd_serve)

    d = sub.add_parser("doctor")
    d.set_defaults(fn=cmd_doctor)

    a = ap.parse_args(argv)
    return a.fn(a, core.load_config(a.config))


if __name__ == "__main__":
    sys.exit(main())
