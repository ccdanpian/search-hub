# search-hub

A small self-hosted search tool for AI agents. It queries your own SearXNG and, optionally, the DeepSeek Search API,
fuses their rankings (RRF), drops off-topic, social and duplicate results, can ask an LLM to judge relevance, and picks
the URLs worth reading. It also reads pages (HTML and PDF) and, given a question, returns only verified verbatim excerpts
so the calling model spends fewer tokens. Python standard library only; no pip packages.

Three ways to use it: **MCP server** (preferred for agents), **CLI**, **HTTP API**. All three run the same code and read
the same config.

```
 AI agent ──MCP / CLI / HTTP──> search-hub ──┬─> SearXNG (google cse, yandex, yahoo, naver ...)     ┐ queried in parallel,
                                             ├─> DeepSeek Search API                                 ┘ fused into one ranking
                                             └─> [optional] another search-hub (e.g. on a Raspberry Pi, over Tailscale)
            web_fetch ──> local HTML/PDF reader ──> [fallback] DeepSeek Fetch API
                     └──> [question=] DeepSeek Flash picks excerpts, each checked against the page
            relevance judge: OpenRouter Decisions, falling back to DeepSeek Flash
```

Version 1.1.0. **Deploying: `docs/DEPLOY.md`** (server, single computer, or client of a server: `docs/CLIENTS.md`).
Changes since 1.0: `CHANGELOG.md`. Measured quality and known gaps: `docs/EVALUATION.md`.

## Requirements

- Python 3.9+ (Linux, macOS or Windows).
- At least one search backend: a SearXNG with the JSON format enabled (`search.formats: [html, json]`), a DeepSeek API
  key, or another search-hub to delegate to (`type: hub`).
- Optional: `pdftotext` (poppler) to read PDFs. Debian/Raspberry Pi OS `sudo apt install poppler-utils`, macOS
  `brew install poppler`, Windows `scoop install poppler` or `choco install poppler`. Without it, PDFs are reported as
  unreadable and everything else works.
- Optional keys: `DEEPSEEK_API_KEY` (search backend, fetch fallback, question-focused reading, fallback judge),
  `OPENROUTER_API_KEY` (relevance judge), `TAVILY_API_KEY` (extra source).

## Quick start

```bash
python -m searchhub doctor                              # self-check: config, keys, authority list, each backend
python -m searchhub search "kubernetes pod lifecycle" --text
python -m searchhub search "Python 3.15 release date" --recent
python -m searchhub fetch https://arxiv.org/pdf/1706.03762 -q "What BLEU score does the big model reach?"
python -m searchhub serve --listen 127.0.0.1:9090       # HTTP API
python -m searchhub.mcp                                 # MCP server on stdio
python -m unittest discover -s tests                    # offline tests (174 run, 14 live ones skipped)
```

Run these from the `search-hub` directory, or put it on `PYTHONPATH`. Use `python3` or `py` where that is the launcher.

## Configuration

Searched in this order: `$SEARCH_HUB_CONFIG`, `~/.config/search-hub/config.json`, `./config.json`. Start from
`config.example.json`. Top-level dictionaries are merged over the defaults; lists (such as `backends`) replace them.

**Secrets never go into config.json.** They are read from the environment and from `env_files` (default
`~/.config/search-hub/judge.env` and `deepseek.env`; `KEY=VALUE` lines, `#` comments, optional `export` and quotes).
A file never overrides a variable that is already set; `SEARCH_HUB_ENV_FILE` adds one more file; `"env_files": []`
loads none. Keep these files private (`chmod 600`).

| Key | Default | Meaning |
|---|---|---|
| `backends[]` | one SearXNG at `127.0.0.1:8888` | Search sources, see below. |
| `backend_timeout` | `15` | Seconds to wait for the parallel (`fuse`) backends; a slower one is dropped and reported in `tried`. |
| `rrf_k` | `60` | RRF constant. |
| `authority_file` | `authority.json` next to the code | Domain tiers, see `AUTHORITY.md`. |
| `judge.enabled` | `true` | Run the LLM relevance judge when a judge key is available. |
| `judge.model` | `openai/gpt-6-luna-decisions` | OpenRouter Decisions model (env `SEARCH_JUDGE_MODEL` overrides). |
| `judge.batch` / `max_batches` / `drop_below` / `timeout` | `15` / `3` / `0.2` / `40` | Candidates per request, requests per search, drop threshold, seconds. |
| `judge.flash_fallback` / `flash_model` | `true` / `deepseek-flash` | Judge with DeepSeek Flash when OpenRouter is missing or fails. |
| `server.token_env` / `token` | `SEARCH_HUB_TOKEN` / empty | API token for non-loopback listeners (prefer the env variable). |
| `server.max_concurrent` | `6` | Parallel requests before `429`. |
| `server.allow_private_fetch` | `false` | Allow fetching private, loopback and tailnet addresses. |
| `server.loopback_requires_token` | `false` | Require the token on loopback too. |
| `fetch_fallback.enabled` | `true` | Retry unreadable pages through DeepSeek Fetch (needs `DEEPSEEK_API_KEY`). |
| `extract.enabled` | `true` | Allow `question=` on fetch (needs `DEEPSEEK_API_KEY`). |
| `extract.model` / `max_input_chars` / `max_output_chars` / `max_excerpts` / `raw_below` / `timeout` | `deepseek-flash` / `60000` / `4500` / `12` / `3500` / `30` | Model, page characters read, excerpt budget per page, excerpt count, pages shorter than this are returned whole, seconds before falling back to raw text. |

### Backends

`backends` is an ordered list. Each entry has a `name` and a `type`:

| type | Fields | What it does |
|---|---|---|
| `searxng` | `url`, optional `start_cmd` (command list run when unreachable) | SearXNG JSON API. Tavily results are merged in when `TAVILY_API_KEY` is set. |
| `deepseek` | optional `key_env` (`DEEPSEEK_API_KEY`), `count` (10, max 50), `categories` (`general`, `news`, `it`, `science`), `use_proxy`, `url` | [DeepSeek Search API](https://api-docs.deepseek.com/guides/search). Skipped silently without a key. |
| `hub` | `url`, `token_env` or `token` | Another search-hub (its `/search`). |

Any entry can set `"fuse": true`. All fuse backends are queried **in parallel** and their results go into one pool that is
ranked once (`backend` then reads e.g. `pi-searxng+deepseek`); merge order follows the config, so ranking does not depend
on who answers first, and one backend failing or timing out does not block the others. Entries without `fuse` keep the
1.0 behaviour (tried in order, the first with results answers alone) and are skipped once the fuse backends have found
something, so a `hub` listed last is a fallback for when the local sources fail.

DeepSeek is a general-web source: it is skipped when the caller names SearXNG `engines` or asks for another category
(images, videos ...), unless pinned with `backend`. A `time_range` becomes DeepSeek's `after:` operator. When `engines`
is given it is sent to SearXNG instead of `categories`, because SearXNG ignores `engines` when both are present.

### Environment variables

`SEARCH_HUB_CONFIG`, `SEARCH_HUB_ENV_FILE`, `SEARCH_HUB_TOKEN` (or whatever `server.token_env` names),
`DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `SEARCH_JUDGE_MODEL`, `TAVILY_API_KEY`, `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`
(used for page fetches, OpenRouter and Tavily; SearXNG and hub connections are always direct, DeepSeek ones are direct
unless the backend or `fetch_fallback` sets `use_proxy: true`), and
`SEARCH_HUB_LIVE=1` for the live tests.

## MCP

Server command: `python -m searchhub.mcp` (stdio, JSON-RPC 2.0). It loads `env_files` itself, so clients need no keys
in their own config. Example entries (adjust the path and Python command):

```json
{ "mcpServers": { "search-hub": { "command": "python", "args": ["-m", "searchhub.mcp"], "cwd": "D:/app/search-hub",
                                  "env": { "PYTHONPATH": "D:/app/search-hub" } } } }
```

```bash
claude mcp add -s user search-hub -e PYTHONPATH=$HOME/search-hub -- python3 -m searchhub.mcp   # Claude Code
```

**Remote MCP.** `serve` also answers MCP over Streamable HTTP at `/mcp` (stateless, same tools, same token rules), so
other computers can use one instance without installing anything:
`claude mcp add -s user --transport http search-hub http://HOST:9090/mcp --header 'Authorization: Bearer ${SEARCH_HUB_TOKEN}'`.
Step-by-step: `docs/CLIENTS.md`.

Codex (`~/.codex/config.toml`): `[mcp_servers.search-hub]` with `command = "python3"`, `args = ["-m", "searchhub.mcp"]`
and `env = { PYTHONPATH = "..." }`. If the machine needs a proxy for the web, add `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`
to `env`. Clients read the tool list at session start: open a new session after updating search-hub.

| Tool | Arguments | Returns |
|---|---|---|
| `web_search` | `query`, `select` (1-20, 5), `recent`, `lang`, `categories`, `time_range` (`day`/`month`/`year`), `verbose` | Compact: `results[]` (`title`, `url`, `snippet`, `date`, `domain`, `authority`), `more[]` (alternatives), `backend`, `judge`. `verbose: true` returns the full response below. |
| `web_fetch` | `urls` (1-8), `question`, `max_chars` (8000) | `pages[]`, see Fetch. |

## CLI

```
python -m searchhub search QUERY [-n 5] [-e ENGINES] [-c CATEGORIES] [-l LANG] [-t day|month|year] [--recent]
                                 [--no-judge] [--backend NAME] [--text]
python -m searchhub fetch URL... [-q QUESTION] [--max-chars 8000] [--timeout 20] [--no-fallback]
python -m searchhub serve [--listen HOST:PORT ...]
python -m searchhub doctor
```

## HTTP API

`python -m searchhub serve --listen HOST:PORT` (repeat `--listen` for several addresses). On loopback no token is
needed. On any other address a token is required, and the server **refuses to start** without one. Send it as
`Authorization: Bearer <token>` or `X-API-Key: <token>`.

| Request | Purpose |
|---|---|
| `GET /health` | Liveness. No token. |
| `GET /search?q=...&select=5&recent=1&lang=&engines=&categories=&time_range=&judge=0&backend=` | Search. |
| `POST /search` `{"query", "select" or "top_k", "recent", "lang", "engines", "categories", "time_range", "judge", "backend", "no_fallback"}` | Search. Accepts the Hermes "Multi Search" body `{query, top_k}`. |
| `POST /fetch` `{"urls": [...up to 8] or "url", "question", "max_chars": 8000, "timeout": 20}` | Page text or excerpts. |
| `POST /mcp` | MCP (Streamable HTTP, JSON responses, no sessions; `GET`/`DELETE` answer 405). |

Search response: `ok`, `query`, `backend`, `backends`, `language`, `total` (merged candidates), `judge`
(`llm:<model>`, `llm:flash:deepseek-flash` or `heuristic`), `judged`, `selected[]` (`title`, `url`, `snippet`,
`published_at`, `domain`, `authority` 1-3, `engines`, `score`, `relevance`, `judge`), `skipped[]` (with `reason`),
`others[]`, `engines_ok`, `engines_bad[]`, `elapsed_ms`, `tried` (when a backend failed), plus `results[]` (up to 30
ranked items with `source_engine`) for Hermes. Errors: `400` bad request, `401` bad token, `429` too many concurrent
requests, `502` every backend failed, `500` unexpected. The HTTP and CLI output is never compacted.

## Fetch

Each page in `pages[]` has `ok`, `url`, `final_url`, `title`, `elapsed_ms`, and either `text` + `text_length` or `error`.
Optional fields: `kind: "pdf"`, `via: "deepseek"` + `fallback_from` (read by the fallback), `retried` (a transient
network error was retried), `anti_bot_signals`.

- HTML is reduced to the main text (`<main>`/`<article>` when present; table cells separated by ` | `), with charset
  detection including GBK/GB18030. PDFs go through `pdftotext` (25 MB cap).
- Transient network errors (TLS reset, connection reset, timeout) are retried once. If the page still cannot be read, or
  only a bot challenge came back, it is fetched once more through **DeepSeek Fetch**. DeepSeek Fetch cannot read PDFs or
  Wikipedia (it answers 404), so those depend on the local reader.
- Addresses are checked before every request and every redirect: non-http(s) schemes and hosts resolving to loopback,
  private, link-local, tailnet (100.64.0.0/10) or reserved addresses are refused, and such URLs are never passed to the
  fallback. Teredo answers (`2001::/32`, what polluted DNS returns for blocked sites) are ignored by this check.

**`question=`: verified excerpts.** The page (up to 60k characters, against 8000 without a question) goes to DeepSeek
Flash, which must copy the passages that answer the question verbatim. Every passage is then checked against the page
text (NFKC, quotes and dashes unified, case and whitespace ignored) and dropped if it does not occur, so model-written
text can only appear in the short `note`. The page comes back as `mode: "extract"`, `relevant`, `excerpts[]`, `note`,
`page_chars`. It falls back to raw text, with `mode` saying why, for pages under 3500 characters, when Flash fails or
takes over 30 s, when every excerpt fails the check, or when no DeepSeek key is set. Measured: about half the tokens of a
raw read with the same key-fact recall, and better recall on long pages (`docs/EVALUATION.md`). Omit `question` when you
need the whole page (summaries, code, long tables).

## How a result is chosen

1. Each engine's ranking is fused with RRF, so a URL found by several engines (or by both SearXNG and DeepSeek) ranks higher.
   URLs are deduplicated by host and path (`www.`, default ports and trailing slashes ignored).
2. Dropped: social/video hosts and image/archive files (`authority.json`), results outside a `site:` / `-site:` in the
   query, and results with almost none of the query words that only one engine returned.
3. Score = fusion + authority bonus + recency (stronger with `recent`) + a small prior from DeepSeek's own relevance
   score, scaled by query-word overlap.
4. The best 15 are judged in batches when a judge is available (yes/no "is this on topic?"); p < 0.2 is dropped and the
   rest are scaled by p. Never-judged candidates are not selected.
5. At most 2 per domain (lifted for `site:` queries), duplicate titles merged. Default 5 selected.

Formula and tier rules: `AUTHORITY.md`.

## Limits (read before relying on it)

- Free scraped engines are often CAPTCHA-blocked or rate-limited; google cse in particular suspends for an hour or more
  after bursts. The same query can return different results later. Results are not verified facts: read the pages.
- Scoring constants are hand-picked; the evaluation covers 32 queries and 64 pages (`docs/EVALUATION.md`).
- The judge only decides topical relevance. It cannot resolve an ambiguous query or judge source quality.
- Excerpts can omit context a raw read would keep (code examples, the name of the subject); use a raw read when in doubt.
- JavaScript-only pages and strong anti-bot sites (zhihu, openai.com, stackoverflow) often stay unreadable even with the
  DeepSeek fallback.
- The address check uses this machine's DNS. When a proxy resolves names itself, a hostile DNS answer that differs between
  the check and the request is not fully excluded.
- The OpenRouter Decisions endpoint is documented as alpha and may change; on failure the Flash judge or the heuristic is used.

## Data sent to third parties

- **DeepSeek**: search queries (`deepseek` backend), URLs the local reader could not read (fetch fallback), page text and
  the question (`question=`), and titles/snippets of candidates (Flash judge, only when OpenRouter is unavailable).
- **OpenRouter**: the query plus titles and snippets of candidates (judge).
- **Tavily**: search queries, only when its key is set.

Nothing is sent to a service whose key is not configured. Private and loopback URLs are never sent anywhere.

## Security notes

- Loopback needs no token, so any program on the same machine can use it (and the fetch feature). That is the intended
  trade-off for local agents; set `server.loopback_requires_token` to `true` to change it.
- Exposed listeners require a token, compared in constant time; request bodies are capped at 64 KB, concurrency at
  `max_concurrent`. Logs contain only `METHOD /path STATUS`, never the query.
- The token is shared by all callers: there is no per-caller identity, quota or rate limit. Rotate it by editing the
  token file and restarting.
- Restrict port 9090 with Tailscale ACLs (or a firewall) if other devices share the network.
- Page text is data to the extraction model; the verbatim check means injected instructions cannot add text to the
  excerpts, only to the 200-character `note`.

## Tests and evaluation

```bash
python -m unittest discover -s tests                    # offline: no network, no keys, ~7 s
SEARCH_HUB_LIVE=1 python -m unittest tests.test_live    # real backends and DeepSeek, ~1-2 min, uses a little quota
python evals/run_eval.py                                # 32-query search quality, graded by deepseek-flash
python evals/summarize.py <out.jsonl>
python evals/eval_extract.py <run_eval out> <out.jsonl> # question= recall and token use on the top pages
python evals/summarize_extract.py <out.jsonl>
```

Eval outputs default to the system temp directory. Treat average-grade differences under ~0.05 as noise.

## Layout

```
searchhub/   core.py (backends, fusion, ranking, judge), fetch.py (page reader, address policy, DeepSeek fallback),
             extract.py (question= excerpts), server.py (HTTP), mcp.py (MCP), cli.py
authority.json, AUTHORITY.md   domain tiers and the scoring formula
config.example.json            sample config
deploy/pi/                     install.sh, uninstall.sh, settings.yml (Raspberry Pi / Linux, user systemd)
docs/AGENTS.md                 short instructions to give an AI agent
docs/DEPLOY.md                 how to deploy: server with install.sh, DeepSeek-only, client; engines; troubleshooting
docs/CLIENTS.md                using a server from other computers (remote MCP, REST)
docs/EVALUATION.md             measured quality, token savings, fixes and known gaps
evals/                         evaluation scripts and the query set
tests/                         offline tests (test_live.py runs only with SEARCH_HUB_LIVE=1)
```
