import os
import tempfile
import json, sys, collections, statistics as st
rows = [json.loads(l) for l in open(sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), "search-hub-extract.jsonl"), encoding="utf-8")]
ok = [r for r in rows if r["ok"]]
print(f"pages {len(rows)}, fetched {len(ok)}, fetch failures {len(rows) - len(ok)}")
print("modes:", dict(collections.Counter(r["mode"] for r in ok)))
ta, tb = sum(r["tok_a"] for r in ok), sum(r["tok_b"] for r in ok)
print(f"tokens  A(raw 8k)={ta}  B(question)={tb}  saved {100 * (1 - tb / ta):.0f}%   per page {ta / len(ok):.0f} -> {tb / len(ok):.0f}")
ex = [r for r in ok if r["mode"] == "extract"]
print(f"extract pages: {len(ex)}, latency p50 {st.median(r['b_ms'] for r in ex)/1000:.1f}s max {max(r['b_ms'] for r in ex)/1000:.1f}s, "
      f"unverified excerpts dropped total {sum(r['dropped'] for r in ok)}")
f = [r for r in ok if r["facts"]]
na = sum(sum(r["rec_a"]) for r in f); nb = sum(sum(r["rec_b"]) for r in f); n = sum(len(r["facts"]) for r in f)
print(f"facts: {n} on {len(f)} pages   recall A={na / n:.2f}  B={nb / n:.2f}")
both = sum(1 for r in f for a, b in zip(r["rec_a"], r["rec_b"]) if a and b)
print(f"  in A but not B: {sum(1 for r in f for a, b in zip(r['rec_a'], r['rec_b']) if a and not b)}   in B but not A: "
      f"{sum(1 for r in f for a, b in zip(r['rec_a'], r['rec_b']) if b and not a)}   in both: {both}")
long = [r for r in f if r["page_chars"] > 8000]
if long:
    print(f"  pages longer than 8000 chars ({len(long)}): recall A={sum(sum(r['rec_a']) for r in long) / sum(len(r['facts']) for r in long):.2f} "
          f"B={sum(sum(r['rec_b']) for r in long) / sum(len(r['facts']) for r in long):.2f}")
print("  worst B-vs-A losses:")
for r in sorted(f, key=lambda r: sum(r["rec_b"]) - sum(r["rec_a"]))[:6]:
    print(f"   A={sum(r['rec_a'])} B={sum(r['rec_b'])}/{len(r['facts'])} {r['mode'][:8]} {r['url'][:70]}")
