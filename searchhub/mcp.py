"""Minimal MCP server (stdio, JSON-RPC 2.0, newline-delimited) exposing two tools: web_search and web_fetch.

Standard library only. Register it in an MCP client with:  python -m searchhub.mcp
It calls the search-hub code in-process, so it uses the same config (SEARCH_HUB_CONFIG / ~/.config/search-hub/config.json),
backends and fallbacks as the CLI. On start it also loads ~/.config/search-hub/judge.env (or SEARCH_HUB_ENV_FILE)
so the LLM judge key reaches MCP clients without copying it into each client's config."""
import json
import os
import sys

from . import core, extract, fetch as fetchmod

PROTOCOL = "2024-11-05"
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")  # echoed back when the client asks for one of these
TOOLS = [
    {
        "name": "web_search",
        "description": "Search the web through a self-hosted aggregator (SearXNG engines fused with RRF, filtered, optionally "
                       "LLM-judged). Returns URLs to read, each with authority (1-3), engines, score and snippet. Use "
                       "web_fetch next to read the pages. Use recent=true for 'latest' questions.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "search query"},
            "select": {"type": "integer", "description": "how many URLs to select (1-20, default 5)"},
            "recent": {"type": "boolean", "description": "prefer newer pages"},
            "lang": {"type": "string", "description": "e.g. zh-CN or en; default: zh-CN if the query has Chinese"},
            "categories": {"type": "string", "description": "general | news | it | science"},
            "time_range": {"type": "string", "enum": ["day", "month", "year"]},
            "verbose": {"type": "boolean", "description": "also return scores, skipped results and engine diagnostics"}},
            "required": ["query"]},
    },
    {
        "name": "web_fetch",
        "description": "Fetch up to 8 public web pages (HTML or PDF). Pass `question` -- what you need from these pages -- "
                       "to get only verbatim excerpts relevant to it, each checked against the page text: several times fewer "
                       "tokens, and up to 60k characters of each page are read instead of the first 8k. Omit `question` only "
                       "when you need the whole page (summarising it, reading code or long tables). Refuses private, loopback "
                       "and tailnet addresses; blocked or JavaScript pages are retried through DeepSeek Fetch (via=deepseek).",
        "inputSchema": {"type": "object", "properties": {
            "urls": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
            "question": {"type": "string", "description": "what you want to learn from the pages; enables verified excerpts"},
            "max_chars": {"type": "integer", "description": "raw text per page when no excerpts are returned, default 8000, max 50000"}},
            "required": ["urls"]},
    },
]


def call_tool(name, args, cfg):
    if name == "web_search":
        res = core.search(str(args.get("query", "")), cfg, select=args.get("select"), recent=bool(args.get("recent")),
                          lang=args.get("lang"), categories=args.get("categories"), time_range=args.get("time_range"))
        res.pop("_ranked", None)
        return (res if args.get("verbose") else compact(res)), not res.get("ok")
    if name == "web_fetch":
        urls = args.get("urls") or []
        if not isinstance(urls, list) or not urls or len(urls) > 8:
            return {"ok": False, "error": "urls must be a list of 1 to 8 strings"}, True
        pages = extract.fetch_focused([str(u) for u in urls], str(args.get("question") or "").strip(), cfg,
                                      max(100, min(int(args.get("max_chars") or 8000), 50000)), 20,
                                      bool(cfg["server"].get("allow_private_fetch")), fetchmod.make_fallback(cfg))
        return {"ok": True, "pages": pages}, not any(p.get("ok") for p in pages)
    return {"ok": False, "error": f"unknown tool {name}"}, True


def compact(res):
    """What an agent needs from a search: the picks, a few alternatives, and why it failed if it did. Scores, skipped
    results and engine diagnostics are dropped (about a third of the tokens); verbose=true returns everything."""
    if not res.get("ok"):
        return {k: res[k] for k in ("ok", "query", "error", "tried") if k in res}
    out = {"ok": True, "query": res["query"], "backend": res.get("backend"), "judge": res.get("judge"),
           "results": [{"title": m["title"], "url": m["url"], "snippet": m["snippet"],
                        **({"date": m["published_at"][:10]} if m.get("published_at") else {}),
                        "domain": m["domain"], "authority": m["authority"]} for m in res.get("selected", [])]}
    if res.get("others"):
        out["more"] = [f"{o['title'][:80]} | {o['url']}" for o in res["others"][:5]]
    return out


def handle(msg, cfg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": ((msg.get("params") or {}).get("protocolVersion") if
                                (msg.get("params") or {}).get("protocolVersion") in SUPPORTED else PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "search-hub", "version": core.VERSION}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            data, is_err = call_tool(p.get("name"), p.get("arguments") or {}, cfg)
        except Exception as e:
            data, is_err = {"ok": False, "error": f"{type(e).__name__}: {e}"}, True
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": is_err}}
    if mid is None:  # notification (e.g. notifications/initialized): no reply
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def load_env_file(path=None):
    """Kept for compatibility: the MCP server now loads every file from config['env_files']."""
    core.load_env_file(path)


def main():
    cfg = core.load_config()
    core.load_env_files(cfg)
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}), flush=True)
            continue
        reply = handle(msg, cfg)
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
