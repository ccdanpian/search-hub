# Using web search (paste into an agent's instructions)

You have a self-hosted web search tool. Use it when the user asks you to look something up online, or when the answer
depends on recent information you may not have.

**Search** (pick the access method your environment supports):

- MCP tool: `web_search` with `query`, optional `select` (default 5), `recent`, `time_range` (`day`/`month`/`year`).
- HTTP: `POST http://127.0.0.1:9090/search` with JSON `{"query": "...", "select": 5, "recent": false}`
  (add header `Authorization: Bearer $SEARCH_HUB_TOKEN` if the service is not on this machine's loopback).
- CLI: `python -m searchhub search "query" --recent -n 5` (from the search-hub directory).

Set `recent` to true for "latest / current / newest" questions. Use the user's language for the query; for medical or
technical topics, an English query often finds better sources. Operators work: `site:github.com`, `-site:example.com`,
`"exact phrase"`, `after:2026-01-01`.

**Read the pages.** Read the result URLs before answering. With the MCP tool `web_fetch`, **pass `question`** (what you
need from these pages): you get only verbatim excerpts, each checked against the page, for about half the tokens, and long
pages are read further. Omit `question` only when you need the whole page (a summary, code, a long table).
Other access: `POST /fetch` with `{"urls": [...], "question": "..."}`, or `python -m searchhub fetch URL ... -q "..."`.

**Check the output.**
- `authority` is 1 to 3 (3 = official or primary source). Prefer 3 for facts about products, laws, medicine.
- Few results or none: retry once with different words, or in the other language.
- If results split across different meanings of the query, ask the user which one they mean.
- In fetched pages: `relevant: false` means the page has nothing on your question; `mode` starting with `raw` means you got
  the page text instead of excerpts (short page or extraction unavailable); `note` is a model-written summary of what the
  page covers and is not a quote. A page with an `error` could not be read: do not guess its content from the snippet.

**Answer.** Conclusion first, then evidence (title, URL, why it matters). A claim needs at least two independent sources;
say so when you only have one, or only snippets. Cite only URLs you actually read or saw in the results. For medical
topics give general information, state the date of guidelines, and tell the user to consult a clinician for their own case.
