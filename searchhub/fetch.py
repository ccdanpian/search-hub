"""Fetch web pages and return readable text. Standard library only.

Because the HTTP server exposes this to other programs, it refuses non-http(s) schemes and hosts that resolve to
loopback / private / link-local / tailnet (100.64.0.0/10) / reserved addresses, on the first request and on every
redirect. Set allow_private=True to turn that off. Limits: the check uses this machine's DNS; if requests go through
a proxy, the proxy resolves the name itself, so a hostile DNS record can still differ."""
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ANTI_BOT = ["checking your browser", "cf-browser-verification", "access denied", "please enable javascript",
            "just a moment", "verify you are human", "attention required", "captcha", "访问验证", "安全验证", "请开启javascript"]
SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "iframe", "template"}
BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "table", "blockquote"}
FAKE_IP = ipaddress.ip_network("198.18.0.0/15")  # proxy "fake-ip" DNS answers; not a real internal host
# Teredo. Polluted DNS answers popular blocked sites (wikipedia, google) with 2001::1; Python calls it private, but it is
# not an internal address and cannot be reached without a Teredo tunnel, so it must not block the fetch.
TEREDO = ipaddress.ip_network("2001::/32")
MAX_BYTES = 3_000_000
PDF_MAX_BYTES = 25_000_000


def host_is_blocked(host):
    """True if host is an IP or resolves to an address that is not globally routable."""
    host = (host or "").strip("[]")
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # unresolvable: the request itself will fail
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip in FAKE_IP or ip in TEREDO:
            continue
        if not ip.is_global:
            return True
    return False


def check_url(url, allow_private=False):
    """Return an error string if the URL must not be fetched, else None."""
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        return f"scheme not allowed: {p.scheme or '(none)'}"
    if not allow_private and host_is_blocked(p.hostname):
        return f"blocked: {p.hostname} is not a public address"
    return None


class PolicyBlock(urllib.error.URLError):
    """A redirect target that the address policy refuses."""


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private):
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        err = check_url(newurl, self.allow_private)
        if err:
            raise PolicyBlock(f"redirect {err}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Text(HTMLParser):
    """Collects page text twice: all of it (out) and only what is inside <main>/<article> (main)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip, self.out, self.main, self.in_main = 0, [], [], 0
        self.title, self.in_title = "", False

    def _both(self, s):
        self.out.append(s)
        if self.in_main:
            self.main.append(s)

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in ("main", "article"):
            self.in_main += 1
        if tag in SKIP and tag != "head":
            self.skip += 1
        if tag in BLOCK:
            self._both("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in ("main", "article") and self.in_main:
            self.in_main -= 1
        if tag in SKIP and tag != "head" and self.skip:
            self.skip -= 1
        if tag in BLOCK:
            self._both("\n")
        elif tag in ("td", "th") and not self.skip:
            self._both(" | ")  # keep table cells apart instead of gluing their text together

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self._both(data)


def pdf_text(raw, timeout=30):
    """Text of a PDF via poppler's pdftotext (stdin -> stdout, no shell, no temp files). Returns '' if it is unavailable or fails."""
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "-enc", "UTF-8", "-nopgbrk", "-", "-"], input=raw, capture_output=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return _clean([out.decode("utf-8", "replace")])


def _clean(parts):
    text = "\n".join(" ".join(line.split()) for line in "".join(parts).splitlines())
    return re.sub(r"\n{2,}", "\n", text).strip()


def _decode(raw, content_type):
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    enc = m.group(1) if m else None
    if not enc:
        m2 = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
        enc = m2.group(1).decode() if m2 else "utf-8"
    for e in (enc, "utf-8", "gb18030"):
        try:
            return raw.decode(e)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


POLICY_ERRORS = ("blocked", "scheme not allowed", "redirect ")  # never retried through a third party
TRANSIENT = ("UNEXPECTED_EOF", "EOF occurred", "Connection reset", "RemoteDisconnected", "IncompleteRead",
             "timed out", "Connection aborted", "Temporary failure in name resolution")  # worth one more try
THIN = 1500  # anti-bot signals on a page this short: it is probably a challenge page, not the content


def make_fallback(cfg):
    """Return fallback(url, max_chars, timeout) -> fetch-style dict using the DeepSeek Fetch API, or None when disabled.

    Only used when the local fetch could not read a page (PDF, JS-rendered, bot-blocked). The URL has already passed the
    local public-address check by then; it is sent to DeepSeek, which fetches it from its own network."""
    from . import core
    fb = (cfg or {}).get("fetch_fallback") or {}
    if not fb.get("enabled", True):
        return None
    core.load_env_files(cfg)
    b = {"key_env": "DEEPSEEK_API_KEY", **fb}
    if not core.deepseek_key(b):
        return None

    def fallback(url, max_chars, timeout):
        t0 = time.time()
        d = core.deepseek_post(b, "/v1/tools/fetch", {"url": url}, timeout=max(timeout, 20))
        text = (d.get("content") or "").strip()
        return {"ok": len(text) >= 100, "url": url, "final_url": url, "title": " ".join((d.get("title") or "").split()),
                "text": text[:max_chars], "text_length": len(text), "anti_bot_signals": [], "via": "deepseek",
                "elapsed_ms": int((time.time() - t0) * 1000)}
    return fallback


def _needs_fallback(res):
    if res.get("ok"):
        return bool(res.get("anti_bot_signals")) and res.get("text_length", 0) < THIN
    return not res.get("policy_block") and not str(res.get("error", "")).startswith(POLICY_ERRORS)


def fetch(url, max_chars=8000, timeout=20, allow_private=False, fallback=None):
    """Fetch one page locally; if that fails (or returns a bot challenge) and `fallback` is given, try it once."""
    res = _fetch_local(url, max_chars, timeout, allow_private)
    if not res.get("ok") and any(t in str(res.get("error", "")) for t in TRANSIENT):
        time.sleep(0.5)  # proxy nodes drop TLS sessions now and then; a second attempt usually succeeds
        first = res
        res = _fetch_local(url, max_chars, timeout, allow_private)
        res["retried"] = first.get("error")
        res["elapsed_ms"] = res.get("elapsed_ms", 0) + first.get("elapsed_ms", 0) + 500
    if fallback and _needs_fallback(res):
        t0 = time.time()
        try:
            alt = fallback(url, max_chars, timeout)
        except Exception as e:
            res["fallback_error"] = f"{type(e).__name__}: {e}"
            return res
        if alt.get("ok") and alt.get("text_length", 0) > res.get("text_length", 0):
            alt["fallback_from"] = res.get("error") or "bot challenge page"
            alt["elapsed_ms"] = res.get("elapsed_ms", 0) + int((time.time() - t0) * 1000)
            return alt
        res["fallback_error"] = "fallback returned no usable text"
    return res


def _fetch_local(url, max_chars, timeout, allow_private):
    t0 = time.time()
    res = {"ok": False, "url": url}
    try:
        err = check_url(url, allow_private)
        if err:
            res["error"] = err
            return res
        opener = urllib.request.build_opener(_Redirects(allow_private))
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                                                   "Accept-Encoding": "gzip, deflate"})
        with opener.open(req, timeout=timeout) as r:
            ctype, final, enc = r.headers.get("Content-Type", ""), r.geturl(), (r.headers.get("Content-Encoding") or "").lower()
            is_pdf_type = "pdf" in ctype.lower()
            raw = r.read((PDF_MAX_BYTES if is_pdf_type else MAX_BYTES) + 1)
        if is_pdf_type and len(raw) > PDF_MAX_BYTES:
            res.update(error=f"PDF larger than {PDF_MAX_BYTES // 1_000_000} MB", final_url=final)
            res["elapsed_ms"] = int((time.time() - t0) * 1000)
            return res
        if "gzip" in enc:
            raw = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        elif "deflate" in enc:
            raw = zlib.decompress(raw)
        if is_pdf_type or raw[:5] == b"%PDF-":
            text = pdf_text(raw, timeout)
            res.update(ok=len(text) >= 100, final_url=final, title=urllib.parse.unquote(final.rsplit("/", 1)[-1].split("?")[0]),
                       text=text[:max_chars], text_length=len(text), anti_bot_signals=[], kind="pdf")
            if len(text) < 100:
                res["error"] = "PDF has no extractable text (scanned images?) or pdftotext is missing"
        elif ctype and not re.search(r"html|text|xml", ctype, re.I):
            res.update(error=f"not a text page ({ctype})", final_url=final)
        else:
            p = _Text()
            p.feed(_decode(raw, ctype))
            text = _clean(p.main)
            if len(text) < 500:  # no usable <main>/<article>: use the whole page
                text = _clean(p.out)
            low = (text[:3000] + p.title).lower()
            res.update(ok=len(text) >= 100, final_url=final, title=" ".join(p.title.split()), text=text[:max_chars],
                       text_length=len(text), anti_bot_signals=[s for s in ANTI_BOT if s in low])
            if len(text) < 100:
                res["error"] = "page text under 100 characters (likely JS-rendered or blocked)"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        res["policy_block"] = isinstance(e, PolicyBlock)
    res["elapsed_ms"] = int((time.time() - t0) * 1000)
    return res


def fetch_many(urls, max_chars=8000, timeout=20, allow_private=False, fallback=None):
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(urls)))) as ex:
        return list(ex.map(lambda u: fetch(u, max_chars, timeout, allow_private, fallback), urls))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(fetch_many(sys.argv[1:]), ensure_ascii=False, indent=2))
