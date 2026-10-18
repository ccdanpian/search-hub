# search-hub evaluation, 2026-10-18

Scope: SearXNG (google cse, yandex, yahoo, naver) + DeepSeek Search API fused by RRF; `web_search` / `web_fetch` over MCP,
CLI and HTTP; DeepSeek Flash for question-focused page reading and as a fallback relevance judge.

## Verdict

Fit for everyday agent use: lookups, docs, news, error messages, how-tos, papers, Chinese and English. Not a fit yet for
image/video search or sustained high query rates (google cse rate-limits). Sites with strong anti-bot (zhihu, openai.com,
stackoverflow) cannot be read by either the local fetcher or DeepSeek Fetch.

## Tests

| Layer | What | Result |
|---|---|---|
| Offline (`tests/`, ~5 s, no network) | text helpers, RRF, rank (filters, caps, `site:`, judge on/off/failure, Flash judge), DeepSeek adapter, orchestration (parallel, deadline, failure isolation, pinning, scope), env/config, MCP (compact output, question forwarding), HTTP server (auth, 400/401/404/429/502, body limit), fetch (SSRF incl. polluted DNS, redirects, encodings, tables, PDF, DeepSeek fallback), extraction (verbatim check, fallbacks, budget) | 151 tests, 137 run + 14 live skipped; repeated runs green |
| Live (`SEARCH_HUB_LIVE=1`) | both backends contribute, zh query, result shape, `site:`, each backend alone, latency, engine health, PDF read, private URL never sent to DeepSeek, `engines=` honoured, images skip DeepSeek, question mode returns verified excerpts | 14/14 |
| Search quality (`evals/run_eval.py`) | 32 queries x {SearXNG, DeepSeek, fused}, judge off, graded blind by deepseek-flash | below |
| Extraction quality (`evals/eval_extract.py`) | 64 pages (top-2 fused results per query), key-fact recall and tokens with vs without `question` | below |

## Search quality (average grade 0..2 / P@5 = share of results graded >= 1)

| Run | Conditions | SearXNG | DeepSeek | Fused |
|---|---|---|---|---|
| 1 | google cse healthy until query 29, old ranking | 1.427 / 0.930 | 1.457 / 0.921 | 1.456 / 0.943 |
| 4 | google cse suspended, final ranking, `.pdf` results allowed | 1.431 / 0.906 | 1.439 / 0.917 | **1.444 / 0.925** |

In run 4, queries with fewer than 3 relevant results out of 5 were: SearXNG 1, DeepSeek 3, fused 0. Authority >= 2 share:
fused 0.61, DeepSeek 0.47. Fused is never meaningfully worse than the better single source and has the best floor. Results
found by both sources grade highest (1.82). Differences under ~0.05 are noise.
Latency without the judge: DeepSeek p50 0.4 s, fused p50 1.5 s, worst 7.5 s. The OpenRouter judge adds ~1.5-2 s.

## Token use of the calling model (Claude)

| Step | Before | After |
|---|---|---|
| `web_search` MCP result | 1.3k-2.0k | 0.9k-1.1k (scores, skipped list, engine diagnostics dropped; `verbose: true` restores them) |
| `web_fetch` 5 pages | 5.2k-15.3k | 1.5k-7.4k with `question` |
| search + fetch, English query | 6.5k | **2.4k (-63%)** |
| search + fetch, Chinese query | 17.3k | **8.5k (-51%)** |

## Extraction quality (`web_fetch` with `question`)

Facts: Flash lists up to 3 key facts per page from the full text (60k chars). A = what Claude got before (title + first
8000 chars). B = title + verified excerpts (or the raw fallback). Same fact lists in every run.

| Run | Change | Recall A | Recall B | Long pages (>8k chars) A / B | Tokens per page |
|---|---|---|---|---|---|
| 1 | first prompt, 3000-char budget, no title | 0.87 | 0.69 | 0.60 / 0.44 | 1620 -> 569 |
| 2 | inclusive prompt, 4500-char budget, short-page threshold 3500 | 0.87 | 0.81 | 0.63 / 0.65 | 1642 -> 904 |
| 3 | + passages must name their subject; title given to both A and B | **0.86** | **0.83** | **0.59 / 0.69** | **1683 -> 862 (-49%)** |

The 0.03 gap in run 3 is within grader noise. On long pages B is better, because Flash reads the whole page instead of the
first 8000 characters. Remaining losses are mostly missing subject names or code examples that the grader counts strictly.
Verification: 20 of the excerpts proposed for 33 pages were not found in the page and were dropped. A page falls back to
raw text only when all its excerpts fail (1 page in run 2).
Extraction latency: p50 7 s; capped at 30 s, after which the page falls back to raw text.

## Fixes made during this evaluation

- URLs with explicit default ports (`:443`) were not deduplicated and missed authority tiers.
- `doctor` reported the judge key missing when it was set.
- A hung backend could hold the request; now 15 s total, the other backend still answers.
- `site:` was ignored by ranking and penalised as a keyword; now filters, and the 2-per-domain cap is lifted for it.
- `engines=` was ignored by SearXNG (it was always sent together with `categories`).
- DeepSeek was queried for image/video searches and when specific engines were requested.
- `web_fetch` could not read PDFs (now `pdftotext`, 25 MB cap) or bot-blocked / TLS-reset pages (now DeepSeek Fetch fallback;
  never for URLs blocked by the address policy, including redirects into private ranges).
- Wikipedia (and other GFW-polluted names) was refused as "not a public address": local DNS answers `2001::1` (Teredo).
  Teredo answers are now ignored by the SSRF check; private and ULA answers still block.
- A one-off TLS reset on the proxy node failed the fetch outright, and DeepSeek Fetch cannot read Wikipedia (404), so nothing
  recovered it. Transient network errors (TLS EOF, connection reset, timeout) now get one local retry before the fallback.
  Live check afterwards: 10/10 Wikipedia pages (en, zh, ja, mobile) read.
- Table cells were glued together in extracted text (`APIUnitPrice`); cells are now separated with ` | `.
- The Flash judge failed half the time on 15-candidate batches (it skipped an id); scores are now keyed by id and a missing
  one gets a neutral 0.5.
- A test for the 429 limit was flaky (slot released after the response); it now has its own listener.

## Known gaps, in priority order

1. **google cse is a single point and rate-limits.** It uses a shared partner key; the evaluation's burst of SearXNG
   calls suspended it and it was still suspended at 15:27, hours later. Without it, yandex/yahoo/naver + DeepSeek still give usable
   results (run 4 was made in that state).
2. Anti-bot sites (zhihu, openai.com, stackoverflow, toutiao) are unreadable by both fetchers: 10 of 64 eval pages.
3. Extraction adds ~5-10 s per fetch; it may drop code examples or subject names that a raw read would keep.
4. `categories=science` can take ~8 s (arxiv/pubmed timeouts).
5. The Flash judge takes ~8 s for 15 candidates; it only runs when OpenRouter is missing or fails.
6. Eval limits: queries written by the same author; DeepSeek Flash both extracts and grades (possible self-preference);
   grades use snippets or excerpts, not ground truth; one run per configuration.
