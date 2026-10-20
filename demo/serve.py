"""Public demo entry point: ``python -m demo.serve``.

The app itself, unchanged, wrapped so that it works as an open demo:

* no sign-in: every visitor gets a signed session as the user "demo" (the normal session cookie, so CSRF and the
  same-origin checks still apply to every change);
* nothing reaches outside: the Assistant Intake API and the ChatGPT/MCP paths answer 404, and the settings that
  manage them, file uploads and the currency switch are refused (the demo shares one database between visitors);
* a banner on every page says the records are made up and reset nightly.

It refuses to start on a database that demo.seed didn't build, so it can never open a real ledger without sign-in.
Run the app as usual (``python -m bets.serve``) for a real install.
"""
from __future__ import annotations

import logging
import re
import sys
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import quote, urlsplit

import uvicorn

from bets import services
from bets.auth import COOKIE, SessionCodec, load_secret
from bets.config import load_settings
from bets.db import Database
from bets.web import create_app

DEMO_USER = "demo"
DEMO_SETTING = "demo"                   # written by demo.seed: {"seeded_for": "YYYY-MM-DD"}
REPO = "https://github.com/sbd1017/matched-betting-dashboard"
HERE = Path(__file__).resolve().parent

BANNER = ('<div class="demo-banner" role="note"><div class="wrap"><b>Demo</b><span>Every record here is made up. '
          'Add, edit and settle whatever you like: it all goes back to how it was at 4am (UK time).</span>'
          f'<a href="{REPO}">Source on GitHub</a></div></div>')
HIDDEN_SETTINGS = ("Assistant Intake (ChatGPT)", "Assistant permissions", "Trusted Assistant mode", "Currency")
REFUSED_WRITES = {"/settings/currency": "The currency is fixed in the demo",
                  "/evidence": "File uploads are switched off in the demo"}


class DemoGate:
    def __init__(self, app, settings):
        self.app, self.base = app, settings.base_path
        self.max_age = settings.session_hours * 3600
        self.codec = SessionCodec(load_secret(settings.secret_file), self.max_age)
        self.secure = settings.secure_cookies
        self.css = (HERE / "demo.css").read_bytes()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        b, path, method = self.base, scope["path"], scope["method"]
        if path == b + "/demo.css":
            return await self._reply(send, 200, self.css, "text/css; charset=utf-8", [(b"cache-control", b"public, max-age=3600")])
        if path.startswith((b + "/api/assistant/", b + "/mcp")) or path.startswith("/.well-known/"):
            return await self._reply(send, 404, b"Not found")
        if path in (b + "/login", b + "/logout"):
            return await self._redirect(send, b + "/")
        if method not in ("GET", "HEAD"):
            refused = REFUSED_WRITES.get(path[len(b):]) or (
                "The ChatGPT connection is switched off in the demo" if path.startswith(b + "/settings/assistant/") else None)
            if refused:
                return await self._redirect(send, self._back(scope) + "?err=" + quote(refused))

        headers = [(k, v) for k, v in scope["headers"] if k != b"cookie"]
        jar = SimpleCookie()
        for k, v in scope["headers"]:
            if k == b"cookie":
                try:
                    jar.load(v.decode("latin-1"))
                except Exception:          # noqa: BLE001 (a malformed cookie is just ignored)
                    pass
        session = self.codec.loads(jar[COOKIE].value) if COOKIE in jar else None
        set_cookie = None
        if not session or session.get("u") != DEMO_USER:
            session = self.codec.new(DEMO_USER)
            set_cookie = self.codec.dumps(session)
        cookies = {k: m.value for k, m in jar.items()}
        cookies[COOKIE] = set_cookie or cookies[COOKIE]
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode("latin-1")))
        scope = {**scope, "headers": headers}

        start, chunks = {}, []

        async def wrapped(message):
            if message["type"] not in ("http.response.start", "http.response.body"):
                await send(message)                  # e.g. the test client's template capture
                return
            if message["type"] == "http.response.start":
                hdrs = list(message.get("headers", []))
                if set_cookie:
                    attrs = f"{COOKIE}={set_cookie}; Max-Age={self.max_age}; Path={b}; HttpOnly; SameSite=Strict"
                    hdrs.append((b"set-cookie", (attrs + ("; Secure" if self.secure else "")).encode("latin-1")))
                start.update(message, headers=hdrs)
                if not self._is_html(hdrs):
                    await send(start)
                return
            if not self._is_html(start["headers"]):
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = self._rewrite(b"".join(chunks).decode("utf-8"), path).encode("utf-8")
            start["headers"] = [(k, v) for k, v in start["headers"] if k != b"content-length"] + \
                [(b"content-length", str(len(body)).encode())]
            await send(start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped)

    @staticmethod
    def _is_html(headers) -> bool:
        return any(k == b"content-type" and v.startswith(b"text/html") for k, v in headers)

    def _rewrite(self, html: str, path: str) -> str:
        b = self.base
        html = html.replace("</head>", f'<link rel="stylesheet" href="{b}/demo.css">\n</head>', 1)
        html = re.sub(r"<body([^>]*)>", lambda m: f"<body{m.group(1)}>\n{BANNER}", html, count=1)
        html = re.sub(r'<form method="post" action="' + re.escape(b) + r'/logout".*?</form>', "", html, flags=re.S)
        if path == b + "/settings":
            for title in HIDDEN_SETTINGS:
                html = re.sub(r'<section class="panel"[^>]*>\s*<h2>' + re.escape(title) + r".*?</section>\s*", "", html,
                              flags=re.S)
            html = re.sub(r" · \d+ backup\(s\)[^<]*", "", html)
            html = re.sub(r"<li>Signed in as <b>.*?</li>", "<li>Demo: no sign-in, and every change is undone at 4am "
                          "(UK time) when the made-up records are rebuilt.</li>", html, flags=re.S)
        return html

    def _back(self, scope) -> str:
        """The page the refused form was on (same site only), else the dashboard."""
        for k, v in scope["headers"]:
            if k == b"referer":
                p = urlsplit(v.decode("latin-1")).path
                if p.startswith(self.base + "/"):
                    return p
        return self.base + "/"

    @staticmethod
    async def _reply(send, status: int, body: bytes, ctype: str = "text/plain; charset=utf-8", extra=()) -> None:
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode()),
            (b"x-content-type-options", b"nosniff"), *extra]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _redirect(send, location: str) -> None:
        await send({"type": "http.response.start", "status": 303, "headers": [
            (b"location", location.encode("latin-1")), (b"content-length", b"0"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b""})


def create_demo_app(settings=None):
    settings = settings or load_settings()
    db = Database(settings.db_path)
    if not settings.db_path.exists():
        raise SystemExit(f"No demo database at {settings.db_path}: build one with python -m demo.seed")
    with db.read() as conn:
        try:
            marker = services.get_setting(conn, DEMO_SETTING)
        except Exception:                  # noqa: BLE001
            marker = None
    if not marker:
        raise SystemExit(f"{settings.db_path} was not built by demo.seed: refusing to open it without sign-in")
    return DemoGate(create_app(settings), settings)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = load_settings()
    app = create_demo_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, access_log=False, proxy_headers=False,
                server_header=False, log_config=None, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    sys.exit(main())
