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

`Invoke-RestMethod` and the agent CLIs use the system proxy. If this computer has a proxy configured (a local Clash or
similar) and it is not running, a tailnet request can fail at once with "connection actively refused": use
`-NoProxy` on `Invoke-RestMethod` (PowerShell 7+) and set `NO_PROXY` to the server address, `localhost` and `127.0.0.1`
for the agents (below). Adding the address to the proxy's bypass list works as well.

## 2. Store the token (and the address) as environment variables

Get the value (`SEARCH_HUB_TOKEN=...`) from the server, e.g. `ssh <user>@HUB_HOST cat ~/.config/search-hub/token.env`,
or have it sent with `tailscale file cp`. Do not paste it into chats or commit it.

Windows (PowerShell), for the current user; open a **new** terminal afterwards so programs see it:

```powershell
[Environment]::SetEnvironmentVariable('SEARCH_HUB_TOKEN', '<value>', 'User')
[Environment]::SetEnvironmentVariable('SEARCH_HUB_URL', 'http://HUB_HOST:9090', 'User')   # used by the skill's HTTP fallback
[Environment]::SetEnvironmentVariable('NO_PROXY', 'HUB_HOST,localhost,127.0.0.1', 'User') # keep tailnet traffic off the proxy
```

macOS/Linux: `export SEARCH_HUB_TOKEN=<value>` and `export SEARCH_HUB_URL=http://HUB_HOST:9090` in `~/.zshrc` /
`~/.bashrc` (or a 600-mode file you source).

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

## 4. Install the skill (how to use the tools well)

The MCP tools give agents the ability; the skill `skills/search-hub/SKILL.md` tells them how to use it: pass `question`
when reading, prefer authority 3, cross-check, cite. It loads only when a task needs web search, and it includes HTTP
recipes for agents that have no MCP tools but can run commands. The same file works for Claude Code and Codex:

```powershell
# from a clone or the release archive of search-hub
mkdir $HOME\.claude\skills\search-hub -Force; copy skills\search-hub\SKILL.md $HOME\.claude\skills\search-hub\
mkdir $HOME\.codex\skills\search-hub -Force;  copy skills\search-hub\SKILL.md $HOME\.codex\skills\search-hub\
```

(macOS/Linux: `mkdir -p ~/.claude/skills/search-hub ~/.codex/skills/search-hub` and copy the file into both.) Agents
without skill support: paste the body of `SKILL.md` (everything after the `---` header) into their instructions
(`AGENTS.md`, `CLAUDE.md`, system prompt).

## 5. Scripts and other programs (REST)

```powershell
$h = @{ Authorization = "Bearer $env:SEARCH_HUB_TOKEN" }
Invoke-RestMethod "http://HUB_HOST:9090/search?q=raspberry+pi+5+power&select=5" -Headers $h
Invoke-RestMethod http://HUB_HOST:9090/fetch -Method Post -Headers $h -ContentType 'application/json' `
  -Body '{"urls": ["https://arxiv.org/pdf/1706.03762"], "question": "What BLEU score does the big model reach?"}'
```

Add `compact=1` to `/search` for the short agent-facing result. Request and response fields: `README.md`, HTTP API.

## Let an agent do the setup

Steps 1, 3 and 4 can be done by the agent itself; step 2 (the token) is yours, so the secret never passes through the
chat. After you have set the variables and opened a new terminal, start the agent there and give it:

> Set up search-hub on this computer following docs/CLIENTS.md from https://github.com/ccdanpian/search-hub (HUB_HOST is
> the value in my SEARCH_HUB_URL variable). SEARCH_HUB_TOKEN is already set: do not print it or write it into any file.
> Check the connection, replace any old local search-hub MCP registration with the remote one for every agent CLI
> installed here (Claude Code, Codex), install the skill for both, and show me `claude mcp list` at the end.

The new tools appear in the **next** session, not in the one that registered them.

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
