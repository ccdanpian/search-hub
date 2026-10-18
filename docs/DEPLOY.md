# Deploying search-hub

Pick one layout. They can be combined: one server for the house, clients everywhere else.

| Layout | Use it for | Needs |
|---|---|---|
| **A. Server** with its own SearXNG (Linux, e.g. Raspberry Pi) | an always-on machine that searches for itself and for other computers | Linux with systemd, Python 3.9+, git, ~1 GB disk; a proxy if GitHub/pip/search engines are blocked where you are |
| **B. Single computer, DeepSeek only** | the quickest start, any OS | Python 3.9+ and a DeepSeek API key |
| **C. Client** of a server | laptops and desktops | network access to the server and its token; nothing installed (`docs/CLIENTS.md`) |

Key files live in `~/.config/search-hub/` on every layout: `deepseek.env` (`DEEPSEEK_API_KEY=...`), `judge.env`
(`OPENROUTER_API_KEY=...`, optional), `token.env` (`SEARCH_HUB_TOKEN=...`, servers only). Keep them `chmod 600` and out
of git. What each key enables: `README.md`, Requirements.

## A. Server with `deploy/pi/install.sh`

The installer set up the reference server on Raspberry Pi OS (Debian 13 trixie, arm64); the 1.1 changes to it (kept
config, `deepseek.env`, optional proxy, upgrade restart) were reviewed and syntax-checked but not re-run there. Any
systemd Linux should work. Run everything as your normal user.

**1. Packages**

```bash
sudo apt install -y python3 python3-venv git poppler-utils curl
```

Optional: [Tailscale](https://tailscale.com/download), logged in, if other computers should reach the server. The
installer then also listens on the Tailscale IP (token required) besides loopback (no token).

**2. Get the code and install**

```bash
git clone https://github.com/ccdanpian/search-hub.git ~/src/search-hub
cd ~/src/search-hub
bash deploy/pi/install.sh                        # proxy at http://127.0.0.1:7890 (the default)
PROXY=http://192.168.1.10:7890 bash deploy/pi/install.sh   # another proxy
PROXY= bash deploy/pi/install.sh                 # no proxy at all
HUB_PORT=9091 bash deploy/pi/install.sh          # another port (default 9090)
```

The proxy is used for cloning SearXNG, pip, the search engines and page fetches. In mainland China most engines and many
pages need it. The script refuses to run as root or when the port is taken by something else. It takes a few minutes on a
Pi (SearXNG and its Python dependencies are installed into a venv).

What it creates (re-running it upgrades code and units and keeps everything marked *kept*):

| Path | What |
|---|---|
| `~/search-hub/` | the code that runs |
| `~/app/searxng-hub/src`, `.venv`, `settings.yml` | SearXNG from GitHub; settings from `deploy/pi/settings.yml` with a fresh `secret_key` (*kept*) |
| `~/.config/search-hub/config.json` | SearXNG + DeepSeek backends, both `fuse: true` (*kept*) |
| `~/.config/search-hub/token.env` | random `SEARCH_HUB_TOKEN` (*kept*) |
| `~/.config/search-hub/judge.env`, `deepseek.env` | commented templates (*kept*) |
| `~/.config/systemd/user/searxng-hub.service` | SearXNG on `127.0.0.1:8889` (granian) |
| `~/.config/systemd/user/search-hub.service` | HTTP API and remote MCP on `127.0.0.1:9090` and `<tailscale-ip>:9090` |

It enables linger so both services start at boot, then prints a health check and a test search.

**3. Add keys and restart**

```bash
nano ~/.config/search-hub/deepseek.env           # DEEPSEEK_API_KEY=sk-...   (recommended)
nano ~/.config/search-hub/judge.env              # OPENROUTER_API_KEY=sk-or-... (optional)
systemctl --user restart search-hub
```

Without a DeepSeek key, search uses SearXNG alone and there is no fetch fallback, `question=` excerpts or Flash judge.

**4. Check**

```bash
cd ~/search-hub && python3 -m searchhub doctor   # every backend [ok], keys found
curl -s http://127.0.0.1:9090/health
python3 -m searchhub search "raspberry pi 5 power" --text
python3 -m unittest discover -s tests            # offline tests
```

**5. Register the agents on the server itself** (stdio; loopback needs no token):

```bash
claude mcp add -s user search-hub -e PYTHONPATH=$HOME/search-hub \
  -e HTTP_PROXY=http://127.0.0.1:7890 -e HTTPS_PROXY=http://127.0.0.1:7890 -e NO_PROXY=127.0.0.1,localhost,100.64.0.0/10 \
  -- python3 -m searchhub.mcp
codex mcp add search-hub --env PYTHONPATH=$HOME/search-hub -- python3 -m searchhub.mcp
```

Drop the proxy variables if you installed with `PROXY=`. The MCP server reads the key files itself.

**6. Serve other computers.** Give them the address (`tailscale ip -4`) and the token from `token.env`, then follow
`docs/CLIENTS.md`. Restrict port 9090 with Tailscale ACLs (or a firewall) if other people's devices share the network.

**Upgrade**

```bash
cd ~/src/search-hub && git pull && bash deploy/pi/install.sh
```

Config, keys, token and `settings.yml` are kept. Read `CHANGELOG.md` for new config keys (compare with
`config.example.json`) and for engine changes (compare `deploy/pi/settings.yml` with `~/app/searxng-hub/settings.yml`).
MCP clients pick up the new code in their next session.

**Uninstall:** `bash deploy/pi/uninstall.sh` removes the services; it lists the directories to delete by hand.

## B. Single computer, DeepSeek only (Linux, macOS, Windows)

No SearXNG, no service: the MCP server runs inside each agent.

1. Get the code (`git clone` as above, or the release archive) to e.g. `~/search-hub` or `D:\app\search-hub`.
2. Create `~/.config/search-hub/config.json` (Windows: `%USERPROFILE%\.config\search-hub\config.json`):
   ```json
   { "backends": [ { "name": "deepseek", "type": "deepseek", "fuse": true } ] }
   ```
3. Put `DEEPSEEK_API_KEY=sk-...` in `~/.config/search-hub/deepseek.env` next to it.
4. Optional: poppler for PDFs (`brew install poppler`, `scoop install poppler`).
5. Check and register (use `python`, `python3` or `py`, whichever runs Python 3.9+):
   ```bash
   cd ~/search-hub && python -m searchhub doctor && python -m unittest discover -s tests
   claude mcp add -s user search-hub -e PYTHONPATH=$HOME/search-hub -- python -m searchhub.mcp
   ```
   On Windows use the full path, e.g. `-e PYTHONPATH=D:\app\search-hub`. Add `HTTP_PROXY`/`HTTPS_PROXY` with `-e` if this
   computer needs a proxy for the web.

Quality is close to the fused setup on average (`docs/EVALUATION.md`), with fewer Chinese long-tail and authority sources.
To serve other computers from here instead, run `python -m searchhub serve --listen 127.0.0.1:9090 --listen <ip>:9090`
with `SEARCH_HUB_TOKEN` set, under whatever keeps processes running on that OS.

## C. Client of a server

See `docs/CLIENTS.md`: one `claude mcp add --transport http ...` line and the token in an environment variable.

## SearXNG engines

`deploy/pi/settings.yml` enables `google cse`, `yandex`, `yahoo` and `naver` for general search, chosen by testing from
mainland China through a proxy on 2026-10-18. Elsewhere other engines may work better. What failed there:

| Engine | Why it is disabled |
|---|---|
| bing | timed out; a SearXNG-only search took ~15 s instead of ~1.5 s |
| sogou, quark | `unexpected crash` on every request |
| 360search | so.com redirects to a CAPTCHA page; the engine returns 0 hits without reporting an error |
| baidu, duckduckgo | CAPTCHA |
| brave, qwant, fireball, seznam | rate-limited, access denied or timeout |
| startpage, mojeek | `inactive: true` in SearXNG's own settings (proof-of-work CAPTCHA) |

To try an engine, enable it in `~/app/searxng-hub/settings.yml`, restart `searxng-hub`, and query it **alone**:
`curl "http://127.0.0.1:8889/search?q=test&format=json&engines=NAME"`, without `categories`. An unknown or inactive
engine name is dropped silently and the request falls back to the default engines, so a test that also sends
`categories` (or a misspelt name) looks like success.

`google cse` uses a shared key and gets suspended (`Suspended: too many requests`, an hour or longer) after bursts such
as an evaluation run. Search keeps working on the other engines and DeepSeek.

## Troubleshooting

- **`searxng-hub` restarts with status=1 and port 8889 never listens.** Reproduce outside systemd with the unit's
  `Environment=` lines under `env -i`. A known cause is `GRANIAN_PROCESS_NAME`, which needs the `granian[pname]` extra;
  the installer does not set it.
- **search-hub answers 502.** Every backend failed: usually SearXNG is down and DeepSeek has no key or quota.
  `python3 -m searchhub doctor` shows which.
- **An engine shows up in `engines_bad`.** Engine-side (CAPTCHA, rate limit, crash); the others still answer. See above.
- **Pages of Wikipedia, Google etc. are "blocked: not a public address".** Fixed in 1.1 (polluted DNS answers `2001::1`).
  Upgrade.
- **`claude mcp list` shows HTTP 401 for a remote server.** The token variable is missing in the client's environment
  (open a new terminal after setting it) or does not match the server's `token.env`.
- **Long agent sessions over SSH die when the connection drops.** Run them inside `tmux new -A -s cc`.
- Logs: `journalctl --user -u search-hub -u searxng-hub -n 50`. The HTTP log has `METHOD /path STATUS` only, never queries.
