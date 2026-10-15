"""https://example.com/bets/: the tracked nginx config (deploy/nginx) in front of the real app.

A throwaway, unprivileged nginx runs deploy/nginx/*.conf as installed (test ports/paths only).
Each test is a different visitor (CF-Connecting-IP) so rate-limit buckets do not interfere.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from bets import assistant
from bets.web import create_app
from tests.conftest import PASSWORD, USER, make_settings
from tests.forms import find_form

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "nginx"
NGINX = "/usr/sbin/nginx"
INSTALLED = {"bets-locations.conf": "/etc/nginx/snippets/matched-betting.conf",
             "bets-proxy.conf": "/etc/nginx/snippets/matched-betting-proxy.conf",
             "bets-headers.conf": "/etc/nginx/snippets/matched-betting-headers.conf",
             "bets-mcp-proxy.conf": "/etc/nginx/snippets/matched-betting-mcp-proxy.conf",
             "bets-http.conf": "/etc/nginx/conf.d/20-matched-betting.conf"}
SITE = "https://example.com"
ESCAPES = re.compile(r'(?:href|src|action)="(?!/bets/|#|\?)([^"]*)"')

pytestmark = pytest.mark.skipif(not os.access(NGINX, os.X_OK), reason="nginx not installed")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"port {port} did not open")


class Stack:
    def __init__(self, tmp: Path):
        self.app_port, self.nginx_port = free_port(), free_port()
        self.settings = make_settings(tmp / "app", secure_cookies=True, port=self.app_port)
        self.app = create_app(self.settings)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.app_port, log_level="warning",
                                                    access_log=False, proxy_headers=False, server_header=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        wait_port(self.app_port)
        n = tmp / "nginx"
        (n / "logs").mkdir(parents=True)
        for name in INSTALLED:
            text = ((DEPLOY / name).read_text()
                    .replace("snippets/matched-betting-proxy.conf", str(n / "bets-proxy.conf"))
                    .replace("snippets/matched-betting-headers.conf", str(n / "bets-headers.conf"))
                    .replace("snippets/matched-betting-mcp-proxy.conf", str(n / "bets-mcp-proxy.conf"))
                    .replace("127.0.0.1:8760", f"127.0.0.1:{self.app_port}")
                    .replace("/var/log/nginx/", f"{n}/logs/"))
            (n / name).write_text(text)
        temp = " ".join(f"{k}_temp_path {n / k};" for k in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
        (n / "nginx.conf").write_text(f"""
worker_processes 1;
pid {n}/nginx.pid;
error_log {n}/logs/error.log warn;
events {{ worker_connections 128; }}
http {{
    types {{ text/html html; }}
    default_type application/octet-stream;
    access_log off;
    server_tokens off;
    {temp}
    set_real_ip_from 127.0.0.1;
    real_ip_header CF-Connecting-IP;
    map "$realip_remote_addr|$remote_addr|$http_cf_ray|$http_cf_connecting_ip" $private_app_ok {{
        default 0;
        "127.0.0.1|127.0.0.1||" 1;
    }}
    log_format apps_log '$remote_addr $request_method $uri $status';
    include {n}/bets-http.conf;
    server {{
        listen 127.0.0.1:{self.nginx_port};
        server_name example.com;
        absolute_redirect off;
        include {n}/bets-locations.conf;
    }}
}}""")
        self.nginx = subprocess.Popen([NGINX, "-p", str(n), "-c", str(n / "nginx.conf"), "-e", str(n / "logs/error.log"),
                                       "-g", "daemon off;"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            wait_port(self.nginx_port)
        except RuntimeError:
            self.nginx.kill()
            raise RuntimeError(self.nginx.stderr.read().decode())

    def close(self):
        self.nginx.terminate()
        self.nginx.wait(5)
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("public-bets"))
    yield s
    s.close()


_visitor = iter(range(1, 250))


class Visitor:
    """A browser arriving through Cloudflare; the Secure cookie is carried by hand (test uses http)."""

    def __init__(self, stack):
        self.ip = f"198.51.100.{next(_visitor)}"
        self.c = httpx.Client(base_url=f"http://127.0.0.1:{stack.nginx_port}", follow_redirects=False, timeout=20)
        self.cookie = ""
        self.cookies_seen: list[str] = []
        self.csrf = ""

    def headers(self, extra=None):
        h = {"Host": "example.com", "CF-Connecting-IP": self.ip, "CF-Ray": "test"}
        if self.cookie:
            h["Cookie"] = f"bets_session={self.cookie}"
        h.update(extra or {})
        return h

    def keep(self, r):
        for v in r.headers.get_list("set-cookie"):
            self.cookies_seen.append(v)
            m = re.match(r"bets_session=([^;]*)", v)
            if m:
                self.cookie = m.group(1)
        m = re.search(r'name="csrf-token" content="([^"]+)"', r.text)
        if m:
            self.csrf = m.group(1)
        return r

    def get(self, path, **h):
        return self.keep(self.c.get(path, headers=self.headers(h)))

    def post(self, path, data=None, origin=SITE, **h):
        extra = {"Origin": origin} if origin else {}
        extra.update(h)
        return self.keep(self.c.post(path, data={"csrf_token": self.csrf, **(data or {})}, headers=self.headers(extra)))

    def login(self, password=PASSWORD):
        self.get("/bets/login")
        return self.post("/bets/login", {"username": USER, "password": password, "next": "/bets/"})


def test_redirects_and_login_challenge(stack):
    v = Visitor(stack)
    r = v.get("/bets")
    assert (r.status_code, r.headers["location"]) == (308, "/bets/")
    assert v.get("/bets?x=1").headers["location"] == "/bets/?x=1"
    for path in ("/bets/", "/bets/ledger", "/bets/accounts", "/bets/export/full.json", "/bets/static/app.js"):
        r = v.get(path)
        assert r.status_code in (303, 401), path
        if r.status_code == 303:
            assert r.headers["location"].startswith("/bets/login?next=")
        assert "£" not in r.text
    login = v.get("/bets/login")
    assert login.status_code == 200 and "Sign in" in login.text and "£" not in login.text
    assert v.get("/bets/healthz").status_code == 404


def test_login_through_the_proxy_sets_a_secure_cookie(stack):
    v = Visitor(stack)
    r = v.login()
    assert (r.status_code, r.headers["location"]) == (303, "/bets/")
    cookie = [c for c in v.cookies_seen if c.startswith("bets_session=")][-1].lower()
    for part in ("httponly", "secure", "path=/bets", "samesite=strict"):
        assert part in cookie
    page = v.get("/bets/")
    assert page.status_code == 200 and "Realised profit" in page.text
    assert ESCAPES.findall(page.text) == []
    for asset in ("/bets/static/design.css", "/bets/static/app.js", "/bets/manifest.webmanifest"):
        assert v.get(asset).status_code == 200, asset


def test_writes_need_same_origin_and_token(stack):
    v = Visitor(stack)
    v.login()
    assert v.post("/bets/accounts", {"name": "X1", "kind": "bookmaker"}, origin="https://evil.example").status_code == 403
    assert v.post("/bets/accounts", {"name": "X1", "kind": "bookmaker", "csrf_token": "forged"}).status_code == 403
    r = v.post("/bets/accounts", {"name": "Bet365", "kind": "bookmaker"})
    assert r.status_code == 303 and r.headers["location"].startswith("/bets/")
    for method in ("PUT", "DELETE", "OPTIONS", "PATCH"):
        r = v.c.request(method, "/bets/api/import", headers=v.headers({"Origin": "https://evil.example"}))
        assert r.status_code == 405 and "access-control-allow-origin" not in r.headers


def test_add_account_form_as_rendered(stack):
    """Regression: the Accounts form's own hidden token (not one the test supplies) must be accepted."""
    v = Visitor(stack)
    v.login()
    form = find_form(v.get("/bets/accounts").text, "/bets/accounts")
    r = v.keep(v.c.post("/bets/accounts", data={**form.fields, "name": "Betfred", "kind": "bookmaker"},
                        headers=v.headers({"Origin": SITE, "Sec-Fetch-Site": "same-origin"})))
    assert r.status_code == 303 and r.headers["location"].startswith("/bets/accounts?ok="), (r.status_code, r.text[:200])
    assert "Betfred" in v.get("/bets/accounts").text


def test_login_guessing_is_rate_limited(stack):
    v = Visitor(stack)
    v.get("/bets/login")
    codes = [v.post("/bets/login", {"username": USER, "password": f"wrong-{i}-xx"}).status_code for i in range(6)]
    assert codes[:4] == [401] * 4 and 429 in codes[4:]


def test_reads_and_writes_are_rate_limited(stack):
    v = Visitor(stack)
    v.login()
    flood = [v.get("/bets/api/calc?stake=10&back_odds=2&lay_odds=2.1").status_code for _ in range(100)]
    assert 429 in flood and flood.count(200) >= 50
    w = Visitor(stack)
    w.login()
    writes = [w.post("/bets/positions/999/settle", {"outcome": "void"}).status_code for _ in range(20)]
    assert 429 in writes


def test_request_size_limit(stack):
    v = Visitor(stack)
    v.login()
    r = v.c.post("/bets/api/import", content=b"x" * (70 * 1024),
                 headers=v.headers({"Origin": SITE, "Content-Type": "application/json", "X-CSRF-Token": v.csrf}))
    assert r.status_code == 413


def test_security_headers_on_proxied_and_nginx_responses(stack):
    v = Visitor(stack)
    responses = [v.get("/bets/login"), v.get("/bets"), v.c.request("PUT", "/bets/", headers=v.headers())]
    for r in responses:
        for name in ("content-security-policy", "x-frame-options", "x-content-type-options", "referrer-policy",
                     "permissions-policy", "strict-transport-security"):
            assert len(r.headers.get_list(name)) == 1, (r.status_code, name)
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert "access-control-allow-origin" not in r.headers
        assert not re.search(r"\d", r.headers.get("server", ""))


def test_installed_nginx_files_match_the_tracked_copies():
    checked = 0
    for name, target in INSTALLED.items():
        if Path(target).exists():
            assert Path(target).read_text() == (DEPLOY / name).read_text(), target
            checked += 1
    if not checked:
        pytest.skip("nginx config for /bets not installed here")


def test_assistant_api_through_nginx(stack):
    token = assistant.issue(stack.settings.assistant_file)
    v = Visitor(stack)
    auth = {"Authorization": f"Bearer {token}"}
    assert v.get("/bets/api/assistant/schema").status_code == 401
    r = v.get("/bets/api/assistant/schema", **auth)
    assert r.status_code == 200 and r.json()["types"]                   # nginx passes the bearer token here
    r = v.c.post("/bets/api/assistant/candidates/account", json={"name": "Coral", "account_type": "bookmaker",
                                                                  "dry_run": True}, headers=v.headers(auth))
    assert r.status_code == 200 and r.json()["status"] == "valid"
    assert "set-cookie" not in r.headers and r.headers["cache-control"] == "no-store"
    assert v.c.request("PUT", "/bets/api/assistant/schema", headers=v.headers(auth)).status_code == 405
    big = v.c.post("/bets/api/assistant/candidates/account", content=b"{" + b" " * (20 * 1024) + b"}",
                   headers=v.headers({**auth, "Content-Type": "application/json"}))
    assert big.status_code == 413
    for path in ("/bets/", "/bets/ledger", "/bets/export/full.json"):   # ...and nowhere else
        assert v.get(path, **auth).status_code in (303, 401), path
