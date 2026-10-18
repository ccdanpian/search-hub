# Using a search-hub server from another computer

One always-on machine (for example a Raspberry Pi set up with `docs/DEPLOY.md`) runs search-hub with SearXNG, DeepSeek,
the judge, PDF reading and question-focused excerpts. Other computers use it as a **remote MCP server**: nothing to
install, no Python, no keys except the server's token, and every upgrade happens only on the server.

Below, `HUB_HOST` is the server's address as the other computers reach it: its Tailscale IP (`tailscale ip -4` on the
server, a `100.x.y.z` address) or MagicDNS name, or a LAN address if you listen there.

Endpoint: `http://HUB_HOST:9090/mcp` (MCP Streamable HTTP, stateless). REST: `/search`, `/fetch`, `/health` on the
same port. Every non-loopback listener requires the token from the server's `~/.config/search-hub/token.env`.

## 1. Check the connection

The computer must be on the same tailnet (or LAN) as the server.

```powershell
curl.exe http://HUB_HOST:9090/health          # -> {"ok": true, "service": "search-hub", "version": "1.1.0"}
```

## 2. Store the token as an environment variable

Get the value (`SEARCH_HUB_TOKEN=...`) from the server, e.g. `ssh <user>@HUB_HOST cat ~/.config/search-hub/token.env`,
or have it sent with `tailscale file cp`. Do not paste it into chats or commit it.

Windows (PowerShell), for the current user; open a **new** terminal afterwards so programs see it:

```powershell
[Environment]::SetEnvironmentVariable('SEARCH_HUB_TOKEN', '<value>', 'User')
```

macOS/Linux: `export SEARCH_HUB_TOKEN=<value>` in `~/.zshrc` / `~/.bashrc` (or a 600-mode file you source).

## 3. Register the MCP server

**Claude Code.** If an older local registration exists (`claude mcp get search-hub` shows a `python -m searchhub.mcp`
command), remove it first with `claude mcp remove search-hub -s user` (use the scope it shows). Then:

```powershell
claude mcp add -s user --transport http search-hub http://HUB_HOST:9090/mcp --header 'Authorization: Bearer ${SEARCH_HUB_TOKEN}'
claude mcp list                                     # -> search-hub: ... (HTTP) - ✔ Connected
```

Keep the single quotes: Claude Code expands `${SEARCH_HUB_TOKEN}` itself when it connects, so the token is not written
into `~/.claude.json`. (PowerShell and bash would expand it inside double quotes; cmd.exe never does.) If the variable is
missing, `claude mcp list` says `Missing environment variables: SEARCH_HUB_TOKEN` and shows HTTP 401.
To skip permission prompts, add `"mcp__search-hub"` to `permissions.allow` in `~/.claude/settings.json`.
Running sessions keep their old tool list: start a new session.

**Codex.**

```powershell
codex mcp add search-hub --url http://HUB_HOST:9090/mcp --bearer-token-env-var SEARCH_HUB_TOKEN
```

which writes `[mcp_servers.search-hub]` with `url` and `bearer_token_env_var` to `~/.codex/config.toml` (remove an older
`command = "python"` entry of the same name first).

**Other MCP clients**: any client that speaks Streamable HTTP with a custom header works the same way. Clients that only
speak stdio need the local install below.

## 4. Scripts and other programs (REST)

```powershell
$h = @{ Authorization = "Bearer $env:SEARCH_HUB_TOKEN" }
Invoke-RestMethod "http://HUB_HOST:9090/search?q=raspberry+pi+5+power&select=5" -Headers $h
Invoke-RestMethod http://HUB_HOST:9090/fetch -Method Post -Headers $h -ContentType 'application/json' `
  -Body '{"urls": ["https://arxiv.org/pdf/1706.03762"], "question": "What BLEU score does the big model reach?"}'
```

Request and response fields: `README.md`, HTTP API.

## What runs where, and limits

- Search, fetching, PDF reading, the DeepSeek fallback and excerpts all run **on the server**, through its proxy. The other
  computer only sends the query or URLs and receives the result.
- Pages are fetched from the server's network: pages that exist only on the other computer's LAN, or need its logins/cookies,
  cannot be read (private addresses are refused anyway).
- If the server is off or unreachable, the tools fail (`claude mcp list` shows "Failed to connect"); nothing falls back
  locally. Plan for that, or keep a local install.
- One shared token for all computers: anyone with it can use the hub, including spending its DeepSeek quota. Rotate it
  on the server (edit `token.env`, `systemctl --user restart search-hub`) and update the variable everywhere.
- The server handles `server.max_concurrent` requests at once (4 with the installer's config); more get HTTP 429.

## Optional: local install that delegates to the server

Only needed for stdio-only MCP clients, for the CLI, or to fetch pages from the computer's own network. Copy the
release (or `git clone` it), then `~/.config/search-hub/config.json`:

```json
{ "backends": [ { "name": "pi", "type": "hub", "url": "http://HUB_HOST:9090", "token_env": "SEARCH_HUB_TOKEN" } ],
  "env_files": [] }
```

Search goes to the server; `web_fetch` runs locally, so PDFs need poppler on that computer, and the fetch fallback and
`question=` need a `DEEPSEEK_API_KEY` there (otherwise pages come back raw). Register it as a stdio server
(`python -m searchhub.mcp`, see `README.md`, MCP).
