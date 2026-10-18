#!/usr/bin/env bash
# Install search-hub + its own SearXNG on a Raspberry Pi / Linux box, as user services. Idempotent.
# It replaces the old Hermes "Multi Search" server on port 9090: search-hub answers POST /search {query, top_k} the same way,
# so Hermes needs no change. The old services must already be stopped (this script checks that port 9090 is free).
#   ports: SearXNG 127.0.0.1:8889 (loopback only), search-hub 127.0.0.1:9090 (no token) and <tailscale-ip>:9090 (token required)
#   override the port with: HUB_PORT=9091 bash deploy/pi/install.sh
#   proxy for GitHub, pip, search engines and page fetches: PROXY=http://host:port (default http://127.0.0.1:7890);
#   no proxy at all: PROXY= bash deploy/pi/install.sh
# Run as the normal user (not root):  bash deploy/pi/install.sh
# Re-running it upgrades the code and units and keeps config.json, settings.yml and all key files.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
HUB="$HOME/search-hub"
SX="$HOME/app/searxng-hub"
CFG="$HOME/.config/search-hub"
UNITS="$HOME/.config/systemd/user"
PROXY="${PROXY-http://127.0.0.1:7890}"  # an explicitly empty PROXY means: no proxy
TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
PORT="${HUB_PORT:-9090}"

[ "$(id -u)" -ne 0 ] || { echo "run as a normal user, not root"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v git >/dev/null || { echo "git is required"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "python >= 3.9 is required"; exit 1; }
command -v pdftotext >/dev/null || echo "note: pdftotext not found; web_fetch cannot read PDFs until you run: sudo apt install poppler-utils"
# Refuse to start if something else already uses the port (for example the old multi-search service). Re-running this
# script is fine: our own search-hub.service is allowed to hold it.
if ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$PORT\$" && ! systemctl --user is-active --quiet search-hub.service; then
  echo "port $PORT is already in use by something else; stop it first (see: ss -tlnp | grep :$PORT)"; exit 1
fi

echo "== 1/6 copy search-hub to $HUB"
mkdir -p "$HUB"
if [ "$(cd "$HUB" && pwd -P)" != "$(cd "$SRC" && pwd -P)" ]; then  # running from inside $HUB: nothing to copy
  rm -rf "$HUB/searchhub"
  cp -r "$SRC/searchhub" "$SRC/authority.json" "$SRC/AUTHORITY.md" "$SRC/README.md" "$SRC/config.example.json" "$HUB/"
  mkdir -p "$HUB/docs" && cp -r "$SRC/docs/." "$HUB/docs/"
fi
[ -f "$HUB/authority.json" ] || { echo "authority.json missing"; exit 1; }

echo "== 2/6 SearXNG in $SX (own copy, port 8889)"
mkdir -p "$SX"
# A proxied TLS connection to GitHub often drops mid-transfer ("early EOF", "GnuTLS recv error"): retry, and use HTTP/1.1.
if [ ! -f "$SX/src/.git/HEAD" ]; then
  ok=0
  for attempt in 1 2 3 4 5; do
    rm -rf "$SX/src"
    if git ${PROXY:+-c http.proxy="$PROXY"} -c http.version=HTTP/1.1 clone --depth 1 https://github.com/searxng/searxng "$SX/src"; then
      ok=1; break
    fi
    echo "git clone failed (attempt $attempt/5), retrying in 5 s ..."; sleep 5
  done
  [ "$ok" -eq 1 ] || { echo "could not clone SearXNG after 5 attempts; check the network${PROXY:+ / proxy: curl -x $PROXY -I https://github.com}"; exit 1; }
fi
if [ ! -x "$SX/.venv/bin/python" ]; then
  python3 -m venv "$SX/.venv"
fi
export PIP_PROXY="$PROXY" PIP_RETRIES=8 PIP_DEFAULT_TIMEOUT=60
"$SX/.venv/bin/pip" install -q -U pip setuptools wheel pyyaml msgspec typing_extensions
(cd "$SX/src" && "$SX/.venv/bin/pip" install -q --use-pep517 --no-build-isolation -e .)
"$SX/.venv/bin/pip" install -q granian
if [ ! -f "$SX/settings.yml" ]; then
  SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  if [ -n "$PROXY" ]; then
    sed -e "s/__SECRET__/$SECRET/" -e "s|__PROXY__|$PROXY|" "$HERE/settings.yml" > "$SX/settings.yml"
  else
    sed -e "s/__SECRET__/$SECRET/" -e '/PROXY-BEGIN/,/PROXY-END/d' "$HERE/settings.yml" > "$SX/settings.yml"
  fi
  chmod 600 "$SX/settings.yml"
fi

echo "== 3/6 config + token"
mkdir -p "$CFG"
if [ ! -f "$CFG/token.env" ]; then
  umask 077
  printf 'SEARCH_HUB_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" > "$CFG/token.env"
fi
if [ ! -f "$CFG/config.json" ]; then  # never overwrite an existing config
cat > "$CFG/config.json" <<JSON
{
  "backends": [
    { "name": "pi-searxng", "type": "searxng", "url": "http://127.0.0.1:8889", "fuse": true },
    { "name": "deepseek", "type": "deepseek", "key_env": "DEEPSEEK_API_KEY", "count": 10, "fuse": true }
  ],
  "judge": { "enabled": true },
  "server": { "token_env": "SEARCH_HUB_TOKEN", "max_concurrent": 4, "allow_private_fetch": false },
  "env_files": ["$CFG/judge.env", "$CFG/deepseek.env"]
}
JSON
else
  echo "keeping existing $CFG/config.json (compare with config.example.json for new keys)"
fi
[ -f "$CFG/judge.env" ] || { umask 077; printf '# optional: OPENROUTER_API_KEY=sk-or-...\n# optional: TAVILY_API_KEY=...\n' > "$CFG/judge.env"; }
[ -f "$CFG/deepseek.env" ] || { umask 077; printf '# optional but recommended: DEEPSEEK_API_KEY=sk-...\n# enables the DeepSeek search backend, fetch fallback, question= excerpts and the fallback judge\n' > "$CFG/deepseek.env"; }

echo "== 4/6 systemd user units"
mkdir -p "$UNITS"
# Do not set GRANIAN_PROCESS_NAME here: it needs the granian[pname] extra, which this venv does not install, and the unit then fails to start.
cat > "$UNITS/searxng-hub.service" <<UNIT
[Unit]
Description=SearXNG for search-hub (loopback 8889)
After=network-online.target mihomo.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SX/src
Environment=SEARXNG_SETTINGS_PATH=$SX/settings.yml
Environment=GRANIAN_INTERFACE=wsgi
Environment=GRANIAN_HOST=127.0.0.1
Environment=GRANIAN_PORT=8889
Environment=GRANIAN_WEBSOCKETS=false
Environment=GRANIAN_BLOCKING_THREADS=4
ExecStart=$SX/.venv/bin/granian searx.webapp:app
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT

LISTEN="--listen 127.0.0.1:$PORT"
[ -n "$TS_IP" ] && LISTEN="$LISTEN --listen $TS_IP:$PORT"
cat > "$UNITS/search-hub.service" <<UNIT
[Unit]
Description=search-hub (aggregate search + fetch API)
After=network-online.target searxng-hub.service tailscaled.service
Wants=network-online.target searxng-hub.service

[Service]
Type=simple
WorkingDirectory=$HUB
Environment=PYTHONUNBUFFERED=1
Environment=SEARCH_HUB_CONFIG=$CFG/config.json
Environment=HTTP_PROXY=$PROXY
Environment=HTTPS_PROXY=$PROXY
Environment=NO_PROXY=127.0.0.1,localhost,100.64.0.0/10
EnvironmentFile=$CFG/token.env
EnvironmentFile=-$CFG/judge.env
EnvironmentFile=-$CFG/deepseek.env
ExecStart=/usr/bin/env python3 -m searchhub serve $LISTEN
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT

echo "== 5/6 start"
systemctl --user daemon-reload
systemctl --user enable --now searxng-hub.service
sleep 8
systemctl --user enable search-hub.service
systemctl --user restart search-hub.service  # restart, not just start: an upgrade must load the new code
loginctl enable-linger "$USER" 2>/dev/null || echo "note: could not enable linger; services start at login only (sudo loginctl enable-linger $USER)"

echo "== 6/6 check"
sleep 4
curl -s -m 5 "http://127.0.0.1:$PORT/health" && echo
( set -a; . "$CFG/token.env"; set +a
  curl -s -m 90 -H "Authorization: Bearer $SEARCH_HUB_TOKEN" "http://127.0.0.1:$PORT/search?q=kubernetes+pod+lifecycle&judge=0&select=2" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print("search ok=%s backend=%s engines_ok=%s selected=%d" % (d.get("ok"), d.get("backend"), d.get("engines_ok"), len(d.get("selected", []))))' )
echo
echo "Token file: $CFG/token.env  (copy SEARCH_HUB_TOKEN to the machines that call this service; do not paste it into chats)"
echo "DeepSeek (recommended): put DEEPSEEK_API_KEY=... into $CFG/deepseek.env; OpenRouter judge: OPENROUTER_API_KEY=... into $CFG/judge.env"
echo "then: systemctl --user restart search-hub"
echo "Logs: journalctl --user -u search-hub -u searxng-hub -n 50"
