import os
import tempfile
import json, sys, collections, statistics as st
rows = [json.loads(l) for l in open(sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), "search-hub-eval.jsonl"), encoding="utf-8")]
V = ["searxng", "deepseek", "fused"]
def stats(rs, v):
    sel = [r["runs"][v]["sel"] for r in rs]
    g = [m["grade"] for s in sel for m in s]
    per_q = [sum(1 for m in s if m["grade"] >= 1) for s in sel]
    ms = sorted(r["runs"][v]["ms"] for r in rs)
    return {"n": len(rs), "returned": round(sum(len(s) for s in sel) / len(rs), 2),
            "P@5(rel>=1)": round(sum(1 for x in g if x >= 1) / max(1, len(g)), 3),
            "strong(=2)": round(sum(1 for x in g if x == 2) / max(1, len(g)), 3),
            "avg_grade": round(st.mean(g), 3) if g else 0,
            "q_with_<3_rel": sum(1 for x in per_q if x < 3), "q_zero_rel": sum(1 for x in per_q if x == 0),
            "auth>=2": round(sum(1 for s in sel for m in s if m["authority"] >= 2) / max(1, len(g)), 3),
            "p50_ms": ms[len(ms) // 2], "max_ms": ms[-1]}
def show(title, rs):
    print(f"\n== {title} (n={len(rs)})")
    keys = list(stats(rs, "fused"))
    print(f"{'':14s}" + "".join(f"{v:>11s}" for v in V))
    S = {v: stats(rs, v) for v in V}
    for k in keys: print(f"{k:14s}" + "".join(f"{S[v][k]:>11}" for v in V))
show("ALL", rows)
by = collections.defaultdict(list)
for r in rows: by[r["tag"]].append(r)
print("\n== avg_grade / P@5 by tag   (searxng | deepseek | fused)")
for t, rs in sorted(by.items()):
    S = {v: stats(rs, v) for v in V}
    print(f"{t:12s} n={len(rs)}  " + " | ".join(f"{S[v]['avg_grade']:.2f}/{S[v]['P@5(rel>=1)']:.2f}" for v in V))
# where fused loses to the better single source
print("\n== queries where fused avg grade < best single by >=0.4, or fused returned nothing")
for r in rows:
    avg = {v: (st.mean([m["grade"] for m in r["runs"][v]["sel"]]) if r["runs"][v]["sel"] else 0) for v in V}
    if avg["fused"] < max(avg["searxng"], avg["deepseek"]) - 0.4 or not r["runs"]["fused"]["sel"]:
        print(f"  {r['q'][:50]:50s} " + " ".join(f"{v}={avg[v]:.1f}" for v in V))
print("\n== source contribution inside fused selections")
c = collections.Counter(); g = collections.defaultdict(list)
for r in rows:
    for m in r["runs"]["fused"]["sel"]:
        key = "both" if "deepseek" in m["engines"] and len(m["engines"]) > 1 else "deepseek-only" if m["engines"] == ["deepseek"] else "searxng-only"
        c[key] += 1; g[key].append(m["grade"])
for k in c: print(f"  {k:14s} {c[k]:3d} results, avg grade {st.mean(g[k]):.2f}")
