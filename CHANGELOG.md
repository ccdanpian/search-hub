# Changelog

## 1.1.0 (2026-10-18)

### Search
- New backend type `deepseek` (DeepSeek Search API). New per-backend flag `fuse`: all fuse backends are queried in
  parallel and ranked as one pool; merge order follows the config, a slow backend is dropped after `backend_timeout`
  (15 s), one failing backend no longer affects the others. Entries without `fuse` are fallbacks, tried in order only
  while nothing has been found (a `hub` listed last is now a real fallback instead of overriding local results).
- DeepSeek is only queried for general-web searches (`general`, `news`, `it`, `science`), not when `engines` or another
  category is requested. `time_range` is passed to it as `after:`.
- `engines=` now works: it is sent to SearXNG instead of `categories` (SearXNG ignored it when both were sent).
- `site:` / `-site:` in the query filter the results (unless nothing matches), and the 2-per-domain cap is lifted for them.
  Search operators, quotes and `-words` no longer count as query terms in the relevance score.
- URLs with default ports (`example.com:443`, as DeepSeek returns them) are deduplicated and get their authority tier.
- DeepSeek's `rerank_score` adds a small prior (0.03) to break near-ties.
- PDFs are no longer dropped from results (`.pdf` removed from `skip_ext`), because fetch now reads them.
- Relevance judge falls back to DeepSeek Flash when OpenRouter is missing or fails (`judge.flash_fallback`). Scores are
  keyed by candidate id, so one skipped id no longer discards a batch.

### Fetch
- PDFs are read with `pdftotext` (poppler), up to 25 MB.
- Unreadable pages (403, TLS reset, JS shell, bot challenge) are retried through DeepSeek Fetch (`fetch_fallback`); never
  for URLs refused by the address policy, including redirects into private ranges.
- Transient network errors are retried once locally before the fallback.
- New `question` argument (MCP `web_fetch`, HTTP `/fetch`, CLI `fetch -q`): DeepSeek Flash returns verbatim excerpts, each
  verified against the page text; falls back to raw text when it cannot help. About half the tokens with the same
  key-fact recall in the evaluation; module `searchhub/extract.py`, config key `extract`.
- Table cells are separated with ` | ` instead of being glued together.
- Polluted DNS answers (`2001::/32`, Teredo) no longer make Wikipedia, Google etc. "not a public address".

### Interfaces
- Remote MCP: `serve` answers MCP over Streamable HTTP at `POST /mcp` (stateless JSON, same tools and token rules), so
  other computers register one URL instead of installing search-hub (`docs/CLIENTS.md`). The MCP server echoes the
  client's protocol version when it is one it knows (2025-06-18, 2025-03-26, 2024-11-05).
- MCP `web_search` returns a compact result (`results`, `more`, `backend`, `judge`); `verbose: true` gives the full
  response. HTTP and CLI output are unchanged apart from the new fields.
- `env_files` config key: key files loaded by every interface (default `judge.env` and `deepseek.env`); `[]` loads none.
- `doctor` loads the key files before checking, shows them, and skips backends whose key is missing.

### Project
- Tests: 30 -> 188 offline (14 more live ones with `SEARCH_HUB_LIVE=1`). Offline tests ignore the machine's keys and config.
- `evals/`: search-quality and extraction evaluations graded by DeepSeek Flash; results in `docs/EVALUATION.md`.
- `deploy/pi/install.sh` keeps an existing `config.json`, creates `deepseek.env`, loads it in the unit, warns when
  `pdftotext` is missing, restarts search-hub on upgrade, and works when run from the install directory itself.
- Docs: README rewritten (configuration reference, MCP/CLI/HTTP reference, data sent to third parties);
  `docs/DEPLOY.md` (deployment guide) and `docs/CLIENTS.md` (remote use); `AUTHORITY.md` and `docs/AGENTS.md` updated.

### Upgrading from 1.0
1. Replace the `searchhub/` directory and `authority.json` (and the docs if you keep them). No new dependencies; Python 3.9+.
2. Optional but recommended: put `DEEPSEEK_API_KEY=...` in `~/.config/search-hub/deepseek.env` (`chmod 600`).
3. To use DeepSeek as a search source, add to `backends` in `config.json`:
   `{ "name": "deepseek", "type": "deepseek", "fuse": true }`, and add `"fuse": true` to your SearXNG entry. Without
   this, search behaves as in 1.0; fetch fallback, `question=` and the Flash judge only need the key.
4. If you list a `hub` after local backends, it is now only used when they return nothing.
5. Optional: install poppler for PDFs (see README, Requirements).
6. Run `python -m searchhub doctor` and `python -m unittest discover -s tests`, then restart the HTTP service if you run one
   and open new sessions in MCP clients.
7. If you run SearXNG with `deploy/pi/settings.yml`, the installer does not overwrite your copy: compare the `engines`
   section by hand.
