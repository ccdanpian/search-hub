---
name: search-hub
description: Search the web and read web pages or PDFs through the self-hosted search-hub (SearXNG + DeepSeek, LLM-judged). Use when the user asks to look something up online, when the answer depends on current or recent information, or when a URL, article or paper has to be read. Explains how to use the web_search / web_fetch tools well, and how to call the search-hub HTTP API when those tools are not available.
---

# search-hub: web search and page reading

## 1. Pick the access method

1. **MCP tools `web_search` and `web_fetch` are available** (they may be listed as `mcp__search-hub__web_search` etc.):
   use them. Everything below about arguments applies to them directly.
2. **No such tools, but you can run shell commands**: use the HTTP API with the recipes in section 6. It needs the
   environment variables `SEARCH_HUB_URL` (e.g. `http://100.x.y.z:9090`, or `http://127.0.0.1:9090` on the server
   itself) and, except on the server's loopback, `SEARCH_HUB_TOKEN`. Check that they are set; never print the token.
3. **Neither**: tell the user search-hub is not set up on this computer and point them to `docs/CLIENTS.md` in the
   search-hub repository. Do not invent search results.

## 2. Search

- `query` in the user's language; for technical or medical topics an English query often finds better sources. Try
  the other language once if results are thin.
- `recent: true` for "latest / current / newest / this year" questions; `time_range` (`day`/`month`/`year`) for hard limits.
- Operators work: `site:github.com`, `-site:example.com`, `"exact phrase"`, `after:2026-01-01`.
- `select` (default 5, max 20) is how many URLs come back. Each has `title`, `url`, `snippet`, `date`, `domain`,
  `authority` (1-3, 3 = official or primary source); `more` lists alternatives.

## 3. Read before answering

Read the most relevant URLs with `web_fetch` (up to 8 per call). **Pass `question`** (what you need from these pages):
you get only verbatim excerpts, each checked against the page text, for about half the tokens, and long pages are read
much further than a raw read. Omit `question` only when you need the whole page (a summary of it, code, a long table).
PDFs are read too.

Per page: `relevant: false` means the page has nothing on your question. A `mode` starting with `raw` means you got the
page text instead of excerpts (short page, or extraction unavailable). `note` is a model-written summary of what the
page covers: useful for deciding, but not a quote. A page with `ok: false` / `error` could not be read; do not guess its
content from the snippet, read another result.

## 4. Check the evidence

- Prefer `authority` 3 for facts about products, laws, standards, medicine; treat forums and reposts as leads.
- A claim needs two independent sources; say so when you only have one, or only snippets.
- If results split across different meanings of the query, ask the user which one they mean.
- Few or no results: rephrase once (other words, other language, drop operators) before giving up.

## 5. Answer

Conclusion first, then the evidence: title, URL, and why it matters. Cite only URLs you read or saw in the results.
Give dates for time-sensitive facts. For medical topics give general information, state the guideline date, and
recommend a clinician for the user's own case.

## 6. HTTP API recipes (only when the MCP tools are missing)

bash / zsh:

```bash
H="Authorization: Bearer ${SEARCH_HUB_TOKEN:-}"
curl -sG "$SEARCH_HUB_URL/search" -H "$H" --data-urlencode "q=YOUR QUERY" -d select=5 -d compact=1
curl -s "$SEARCH_HUB_URL/fetch" -H "$H" -H 'Content-Type: application/json' --data-binary @- <<'JSON'
{"urls": ["https://example.org/page"], "question": "what you need from it"}
JSON
```

Add `-d recent=1` or `-d time_range=month` to the search as needed.

PowerShell (Windows):

```powershell
$h = @{ Authorization = "Bearer $env:SEARCH_HUB_TOKEN" }
$q = [uri]::EscapeDataString('YOUR QUERY')
Invoke-RestMethod "$env:SEARCH_HUB_URL/search?q=$q&select=5&compact=1" -Headers $h | ConvertTo-Json -Depth 8
$body = @{ urls = @('https://example.org/page'); question = 'what you need from it' } | ConvertTo-Json
Invoke-RestMethod "$env:SEARCH_HUB_URL/fetch" -Method Post -Headers $h -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($body)) | ConvertTo-Json -Depth 8
```

The responses have the same fields as the tools (search: `results`, `more`; fetch: `pages`). HTTP 401 means the token
is missing or wrong, 429 means the server is busy (retry shortly), 502 means every search backend failed.
