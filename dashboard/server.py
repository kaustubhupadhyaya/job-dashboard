"""Job-bot dashboard: a stdlib HTTP server on 127.0.0.1 only.

    /                  the board (static/index.html, app.css, app.js)
    /api/status        every row, computed from the bots' files on each request (read-only)
    /view/log/<key>    read-only tail of a whitelisted log (Edge blocks file:// links from a 127.0.0.1 page)
    /view/shot/<name>  a resume-refresh verification screenshot
    /quality           match quality: saved applications graded by portal_kit/fit.py (/api/quality as JSON; /audit redirects)
    /week              week report: every application each applier saved in the last 7 days, per day and as a list (/api/week as JSON)
    /linkedin-speed    where the LinkedIn bot's time goes, per count and per step (/api/linkedin-speed as JSON; lispeed.py computes in a
                       background thread every 10 min, lispeed_view.py renders)
    /external          company-site jobs nobody applies to, duplicates merged, Done/Skip marks kept (/api/external as JSON;
                       the one write the board makes: POST /api/external/mark -> state\\external_jobs_review.json)
    /boards            the pluggable board registry: each applier's repo, install state, last run, applied today and cap
                       (/api/boards as JSON; read-only, boards.json at the repo root)

Start: job-tracker\\dashboard.cmd (the JobDashboard scheduled task starts it hidden at logon).
"""
from __future__ import annotations

import html
import json
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402
import collect  # noqa: E402
import quality  # noqa: E402
import week  # noqa: E402
import external  # noqa: E402
import lispeed  # noqa: E402
import lispeed_view  # noqa: E402
import boards  # noqa: E402

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
_lock = threading.Lock()
_cache: tuple[float, bytes] | None = None


def status_json() -> bytes:
    global _cache
    with _lock:   # one collector pass at a time; concurrent tabs share a 5 s result
        if _cache and time.time() - _cache[0] < 5:
            return _cache[1]
        body = json.dumps(collect.status(), default=str).encode("utf-8")
        _cache = (time.time(), body)
        return body


def now_states() -> dict:
    """Each applier's state in the board's own words (the shared 5 s result), for the week report's 'Right now' lines."""
    try:
        out = {}
        for p in json.loads(status_json())["pipelines"]:
            cause = "".join(c.get("t", "") for c in (p.get("cause") or []) if isinstance(c, dict)).strip()
            detail = (p.get("detail") or "").strip()
            out[p["key"]] = {"word": p.get("word", ""), "text": f"{detail.rstrip('.')}. {cause}" if detail and cause else detail or cause,
                             "sign": bool(p.get("sign"))}
        return out
    except Exception:    # the report is still right without it; only the 'Right now' lines drop out
        return {}


def log_page(key: str, path: Path) -> bytes:
    lines = collect.tail_lines(path, 300_000)[-600:]
    esc = html.escape("\n".join(lines))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(path.name)}</title>
<style>
:root {{ color-scheme: light dark; --paper:#F3F5F2; --ink:#1B2230; --muted:#5E6B66; }}
@media (prefers-color-scheme: dark) {{ :root {{ --paper:#13233B; --ink:#E6ECF2; --muted:#93A4B8; }} }}
body {{ margin:0; background:var(--paper); color:var(--ink); font:14px/1.35 Bahnschrift, 'Segoe UI', sans-serif; }}
header {{ padding:16px 40px; color:var(--muted); }}
header a {{ color:var(--ink); }}
pre {{ margin:0; padding:0 40px 40px; font:13px/1.45 Consolas, ui-monospace, monospace; white-space:pre-wrap; word-break:break-word; }}
</style></head><body>
<header><a href="/">Back to the board</a> &nbsp; {html.escape(str(path))}, last {len(lines)} lines, newest at the bottom</header>
<pre>{esc}</pre><script>window.scrollTo(0, document.body.scrollHeight);</script></body></html>""".encode("utf-8")


def shortlist_page(key: str) -> bytes | None:
    """Read-only list of jobs a portal found but cannot or must not apply to (employer-site apply, a form it
    will not guess, a portal that is not armed), for the user to apply by hand. Every field is third-party text:
    escaped, and a link is only ever an http(s) URL."""
    path = collect.gulf_shortlist_path(key)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("items", []) if isinstance(data, dict) else list(data)
        updated = str(data.get("updated", "")) if isinstance(data, dict) else ""
    except (OSError, ValueError):
        items, updated = [], ""
    e = lambda v: html.escape(str(v if v is not None else ""))
    rows = []
    for it in items[:300]:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "")
        title = e(it.get("title") or "(no title)")
        link = (f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{title}</a>'
                if re.match(r"https?://", url) else title)
        rows.append(f"<tr><td>{link}</td><td>{e(it.get('company'))}</td><td>{e(it.get('location'))}</td>"
                    f"<td>{e(it.get('reason'))}</td><td>{e(it.get('posted'))}</td></tr>")
    table = ("<table><thead><tr><th>Job</th><th>Company</th><th>Where</th><th>Why it is here</th><th>Posted</th></tr></thead>"
             f"<tbody>{''.join(rows)}</tbody></table>") if rows else "<p>No jobs on this list yet.</p>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Shortlist: {e(key)}</title>
<style>
:root {{ color-scheme: light dark; --paper:#F3F5F2; --ink:#1B2230; --muted:#5E6B66; --grid:#D3DCD6; }}
@media (prefers-color-scheme: dark) {{ :root {{ --paper:#13233B; --ink:#E6ECF2; --muted:#93A4B8; --grid:#2A3D5A; }} }}
body {{ margin:0; background:var(--paper); color:var(--ink); font:14px/1.4 Bahnschrift, 'Segoe UI', sans-serif; }}
header {{ padding:16px 40px 8px; color:var(--muted); }} header a {{ color:var(--ink); }}
h1 {{ margin:0; padding:0 40px; font-size:21px; }} p {{ padding:0 40px; margin:8px 0; max-width:80ch; }}
table {{ margin:8px 40px 40px; border-collapse:collapse; width:calc(100% - 80px); }}
th, td {{ text-align:left; padding:6px 12px 6px 0; border-bottom:1px solid var(--grid); vertical-align:top; }}
th {{ color:var(--muted); font-weight:400; }} a {{ color:var(--ink); }}
</style></head><body>
<header><a href="/">Back to the board</a></header>
<h1>Shortlist: {e(key)}</h1>
<p>Jobs the bot found but cannot or must not apply to by itself: employer-site apply, a form it will not guess, or a
portal that is not armed. Apply by hand if one looks good.{f' Updated {e(updated)}.' if updated else ''}</p>
{table}</body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "JobBoard/1"

    def log_message(self, fmt, *args):   # keep the hidden server quiet
        pass

    def send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/audit", "/audit.html"):
                # the static audit page of 2026-10-05 is retired (one-off snapshot, partly invented numbers)
                self.send_response(302)
                self.send_header("Location", "/quality")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/quality":
                return self.send(200, quality.page(), TYPES[".html"])
            if path == "/api/quality":
                return self.send(200, json.dumps(quality.report(), default=str).encode("utf-8"),
                                 "application/json; charset=utf-8")
            if path == "/week":
                return self.send(200, week.page(now=now_states()), TYPES[".html"])
            if path == "/api/week":
                return self.send(200, json.dumps(week.report(now=now_states()), default=str).encode("utf-8"),
                                 "application/json; charset=utf-8")
            if path == "/linkedin-speed":
                return self.send(200, lispeed_view.page(), TYPES[".html"])
            if path == "/api/linkedin-speed":
                return self.send(200, json.dumps(lispeed_view.report_json(), default=str).encode("utf-8"), "application/json; charset=utf-8")
            if path in ("/external", "/api/external"):
                m = re.search(r"(?:^|[?&])days=(\d{1,2})(?:&|$)", self.path.split("?", 1)[1] if "?" in self.path else "")
                days = int(m.group(1)) if m else 7
                if path == "/external":
                    return self.send(200, external.page(days), TYPES[".html"])
                return self.send(200, json.dumps(external.report(days), default=str).encode("utf-8"),
                                 "application/json; charset=utf-8")
            if path in ("/", "/index.html"):
                return self.send(200, (STATIC / "index.html").read_bytes(), TYPES[".html"])
            if re.fullmatch(r"/(app\.css|app\.js)", path):
                f = STATIC / path.lstrip("/")
                return self.send(200, f.read_bytes(), TYPES[f.suffix])
            if path == "/boards":
                return self.send(200, (STATIC / "boards.html").read_bytes(), TYPES[".html"])
            if path == "/api/boards":
                return self.send(200, json.dumps(boards.summary(), default=str).encode("utf-8"),
                                 "application/json; charset=utf-8")
            if path == "/api/status":
                return self.send(200, status_json(), "application/json; charset=utf-8")
            m = re.fullmatch(r"/view/log/([a-z0-9-]+)", path)
            if m:
                collect.gulf_registry()          # registers the Gulf portals' logs even before the first /api/status
            if m and m.group(1) in collect.LOGS:
                return self.send(200, log_page(m.group(1), collect.LOGS[m.group(1)]), TYPES[".html"])
            m = re.fullmatch(r"/view/shortlist/([a-z0-9-]+)", path)
            if m:
                page = shortlist_page(m.group(1))
                if page is not None:
                    return self.send(200, page, TYPES[".html"])
            m = re.fullmatch(r"/view/shot/([A-Za-z0-9_.-]+\.png)", path)
            if m and (collect.SHOTS / m.group(1)).is_file():
                return self.send(200, (collect.SHOTS / m.group(1)).read_bytes(), "image/png", "max-age=3600")
            return self.send(404, b"Not found", "text/plain; charset=utf-8")
        except Exception:
            return self.send(500, traceback.format_exc().encode("utf-8"), "text/plain; charset=utf-8")


    def do_POST(self):
        """The board's only write: Done / Skip / Reopen on a company-site job. A cross-site page cannot send the X-Board header without a
        preflight this server never answers, and a request from another origin is refused outright."""
        path = self.path.split("?", 1)[0]
        try:
            if path != "/api/external/mark":
                return self.send(404, b"Not found", "text/plain; charset=utf-8")
            port = self.server.server_address[1]
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return self.send(403, b"Forbidden", "text/plain; charset=utf-8")
            n = int(self.headers.get("Content-Length") or 0)
            if self.headers.get("X-Board") != "1" or not 0 < n <= 4096:
                return self.send(400, b"Bad request", "text/plain; charset=utf-8")
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            ok = external.mark(str(data.get("key", "")), str(data.get("state", "")))
            return self.send(200 if ok else 400, json.dumps({"ok": bool(ok)}).encode("utf-8"), "application/json; charset=utf-8")
        except Exception:
            return self.send(500, traceback.format_exc().encode("utf-8"), "text/plain; charset=utf-8")


class ExclusiveServer(ThreadingHTTPServer):
    """HTTPServer sets SO_REUSEADDR, which on Windows lets a second process bind the same port and
    silently split requests between an old and a new server (seen 2026-09-30). Take the port
    exclusively instead, so a second copy fails to bind and exits."""
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    port = int(paths.SETTINGS.get("port", 8787))
    try:
        srv = ExclusiveServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"Port {port} is taken ({e}); another board is already running.", flush=True)
        return
    srv.daemon_threads = True
    lispeed.start()          # the LinkedIn speed page reads a result computed in the background every 10 minutes
    print(f"Job board on http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
