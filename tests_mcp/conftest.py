"""A real stack for the plugin tests, all throwaway:

  betting app (its own venv, subprocess, temp data, test login + assistant token, sample accounts)
  <- MCP server (this venv, uvicorn thread, temp mcp-var)
  <- unprivileged nginx running the tracked deploy/nginx files
and the official MCP Python client talking to it. Run with .venv-mcp/bin/python -m pytest tests_mcp.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import anyio
import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
APP_PY = ROOT / ".venv" / "bin" / "python"
NGINX = "/usr/sbin/nginx"
SITE = "https://example.com"
# ChatGPT's CIMD documents. The stable one is exactly as served by https://chatgpt.com/oauth/client.json
# (fetched 2026-10-09); the callback-specific shape is used when issuer identification is not advertised.
CALLBACK = "https://chatgpt.com/connector/oauth/cb_7f3KxQ2"
STABLE_CIMD_URL = "https://chatgpt.com/oauth/client.json"
STABLE_CIMD = {"client_id": STABLE_CIMD_URL, "client_uri": "https://chatgpt.com/",
               "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
               "token_endpoint_auth_method": "private_key_jwt", "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
               "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "client_name": "ChatGPT",
               "logo_uri": "https://persistent.oaistatic.com/sonic/misc/openai-logo.png",
               "token_endpoint_auth_signing_alg": "RS256", "jwks_uri": "https://chatgpt.com/oauth/jwks.json"}
CB_CIMD_URL = "https://chatgpt.com/oauth/cb_7f3KxQ2/client.json"
CB_CIMD = {**STABLE_CIMD, "client_id": CB_CIMD_URL, "redirect_uris": [CALLBACK]}


class FakeCimd:
    """Stands in for https://chatgpt.com (the tests make no outbound requests); records what was fetched."""

    def __init__(self):
        self.docs = {STABLE_CIMD_URL: STABLE_CIMD, CB_CIMD_URL: CB_CIMD}
        self.fetched = []

    async def fetch(self, url):
        self.fetched.append(url)
        if url not in self.docs:
            raise ValueError("HTTP 404")
        return self.docs[url], 300


FAKE_CIMD = FakeCimd()
TEST_REDIRECT = "https://client.test/callback"
LOGIN_USER, LOGIN_PASSWORD = "tester", "correct horse battery staple"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"port {port} did not open")


SEED = """
import sys
from bets.config import load_settings
from bets.auth import write_credentials
from bets import assistant, services
from bets.db import Database
s = load_settings(); s.data_dir.mkdir(parents=True, exist_ok=True)
write_credentials(s.auth_file, sys.argv[1], sys.argv[2])
db = Database(s.db_path); db.migrate()
with db.tx() as c:
    services.create_account(c, name="Lloyds", kind="bank")
    services.create_account(c, name="Smarkets", kind="exchange", commission_bp=200, opening_balance=8000)
    services.create_account(c, name="Betfred", kind="bookmaker", opening_balance=5000)
print(assistant.issue(s.assistant_file))
"""


class Bets:
    """The betting app as its own process (its own venv: it does not share packages with the plugin)."""

    def __init__(self, tmp: Path):
        self.port = free_port()
        self.data = tmp / "bets-var"
        self.env = {**os.environ, "BETS_DATA_DIR": str(self.data), "BETS_PORT": str(self.port),
                    "BETS_SECURE_COOKIES": "0", "BETS_PUBLIC_ORIGINS": f"http://127.0.0.1:{self.port}"}
        out = subprocess.run([str(APP_PY), "-c", SEED, LOGIN_USER, LOGIN_PASSWORD], cwd=ROOT, env=self.env,
                             capture_output=True, text=True, check=True)
        self.assistant_token = out.stdout.strip().splitlines()[-1]
        self.log = open(tmp / "bets.log", "w")
        self.proc = subprocess.Popen([str(APP_PY), "-m", "bets.serve"], cwd=ROOT, env=self.env, stdout=self.log,
                                     stderr=subprocess.STDOUT)
        wait_port(self.port)

    def q(self, sql: str, *args):
        conn = sqlite3.connect(f"file:{self.data / 'bets.sqlite3'}?mode=ro", uri=True)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def one(self, sql: str, *args):
        rows = self.q(sql, *args)
        return rows[0][0] if rows else None

    def seed(self, code: str) -> dict:
        """Run Python against the running app's data (services inside one transaction) and return what it prints as
        JSON -- for setting up a scenario the way earlier entries would have left it."""
        script = ("import json, sys\nfrom datetime import timedelta\nfrom bets.config import load_settings\n"
                  "from bets.db import Database\nfrom bets import services, times\n"
                  "db = Database(load_settings().db_path)\nout = {}\nwith db.tx() as conn:\n"
                  + "".join("    " + line + "\n" for line in code.strip("\n").splitlines())
                  + "print(json.dumps(out))\n")
        r = subprocess.run([str(APP_PY), "-c", script], cwd=ROOT, env=self.env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]
        return json.loads(r.stdout.strip().splitlines()[-1])

    def browser(self) -> httpx2.Client:
        """A signed-in /bets/ browser session (cookie + CSRF), for the human side of the flow."""
        c = httpx2.Client(base_url=f"http://127.0.0.1:{self.port}", follow_redirects=False, timeout=20)
        import re
        page = c.get("/bets/login").text
        token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        r = c.post("/bets/login", data={"csrf_token": token, "username": LOGIN_USER, "password": LOGIN_PASSWORD,
                                         "next": "/bets/"})
        assert r.status_code == 303, r.text[:200]
        c.csrf = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/bets/").text).group(1)
        return c

    def close(self):
        self.proc.terminate()
        self.proc.wait(10)
        self.log.close()


class Mcp:
    def __init__(self, tmp: Path, bets: Bets):
        from bets_mcp.config import load_settings
        from bets_mcp.server import build
        self.port = free_port()
        env = {"BETS_MCP_DATA_DIR": str(tmp / "mcp-var"), "BETS_MCP_PORT": str(self.port),
               "BETS_MCP_UPSTREAM": f"http://127.0.0.1:{bets.port}/bets/api/assistant",
               "BETS_MCP_TEST_REDIRECT": TEST_REDIRECT}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            self.settings = load_settings()
        finally:
            for k, v in old.items():
                os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
        self.settings.data_dir.mkdir(mode=0o700, parents=True)
        fd = os.open(self.settings.assistant_token_file, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, bets.assistant_token.encode())
        os.close(fd)
        self.app, self.server, self.store = build(self.settings, cimd_fetch=FAKE_CIMD.fetch)
        self.uv = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning",
                                                access_log=False, proxy_headers=False, server_header=False))
        self.thread = threading.Thread(target=self.uv.run, daemon=True)
        self.thread.start()
        wait_port(self.port)
        self.url = f"http://127.0.0.1:{self.port}/bets/mcp"

    def token(self) -> str:
        """An access token as the OAuth flow would issue it (the flow itself is tested separately)."""
        code = self.store.new_grant_and_code("test-client", "test", {"scopes": ["bets.intake"]}, 60)
        row = self.store.take_code(code)
        access, _ = self.store.issue_tokens(row["grant_id"], "test-client", ["bets.intake"], self.settings.resource,
                                            3600, 86400)
        return access

    def close(self):
        self.uv.should_exit = True
        self.thread.join(10)


class Nginx:
    """The tracked nginx config in front of both services (as test_public_bets does for /bets)."""

    FILES = {"bets-locations.conf": "snippets/matched-betting.conf", "bets-proxy.conf": "snippets/matched-betting-proxy.conf",
             "bets-headers.conf": "snippets/matched-betting-headers.conf",
             "bets-mcp-proxy.conf": "snippets/matched-betting-mcp-proxy.conf", "bets-http.conf": None}

    def __init__(self, tmp: Path, bets: Bets, mcp: Mcp):
        self.port = free_port()
        n = tmp / "nginx"
        (n / "logs").mkdir(parents=True)
        for name in self.FILES:
            text = (ROOT / "deploy" / "nginx" / name).read_text()
            for other, installed in self.FILES.items():
                if installed:
                    text = text.replace(installed, str(n / other))
            text = (text.replace("127.0.0.1:8760", f"127.0.0.1:{bets.port}")
                    .replace("127.0.0.1:8762", f"127.0.0.1:{mcp.port}")
                    .replace("/var/log/nginx/", f"{n}/logs/"))
            (n / name).write_text(text)
        temp = " ".join(f"{k}_temp_path {n / k};" for k in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
        (n / "nginx.conf").write_text(f"""
worker_processes 1;
pid {n}/nginx.pid;
error_log {n}/logs/error.log warn;
events {{ worker_connections 128; }}
http {{
    types {{ text/html html; application/json json; }}
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
        listen 127.0.0.1:{self.port};
        server_name example.com;
        absolute_redirect off;
        include {n}/bets-locations.conf;
    }}
}}""")
        self.logs = n / "logs"
        self.proc = subprocess.Popen([NGINX, "-p", str(n), "-c", str(n / "nginx.conf"), "-e", str(n / "logs/error.log"),
                                      "-g", "daemon off;"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            wait_port(self.port)
        except RuntimeError:
            self.proc.kill()
            raise RuntimeError(self.proc.stderr.read().decode())
        self.base = f"http://127.0.0.1:{self.port}"

    def client(self, ip: str | None = None) -> httpx2.Client:
        """A visitor arriving through Cloudflare (Host example.com, its own CF-Connecting-IP)."""
        ip = ip or f"198.51.100.{secrets.randbelow(250) + 1}"
        return httpx2.Client(base_url=self.base, follow_redirects=False, timeout=20,
                             headers={"Host": "example.com", "CF-Connecting-IP": ip, "CF-Ray": "test"})

    def local(self, url: str) -> str:
        return url.replace(SITE, self.base)

    def close(self):
        self.proc.terminate()
        self.proc.wait(10)


@pytest.fixture(scope="session")
def bets(tmp_path_factory):
    b = Bets(tmp_path_factory.mktemp("bets"))
    yield b
    b.close()


@pytest.fixture(scope="session")
def mcp(tmp_path_factory, bets):
    m = Mcp(tmp_path_factory.mktemp("mcp"), bets)
    yield m
    m.close()


@pytest.fixture(scope="session")
def nginx(tmp_path_factory, bets, mcp):
    if not os.access(NGINX, os.X_OK):
        pytest.skip("nginx not installed")
    n = Nginx(tmp_path_factory.mktemp("nginx"), bets, mcp)
    yield n
    n.close()


@pytest.fixture
def token(mcp):
    return mcp.token()


# ---------------------------------------------------------------- the official MCP client

async def _session_call(url: str, token: str | None, fn, headers: dict | None = None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    h.update(headers or {})
    async with httpx2.AsyncClient(headers=h, timeout=30) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)


def call_tool(mcp: Mcp, token: str, name: str, args: dict | None = None, url: str | None = None, headers=None):
    async def fn(session):
        return await session.call_tool(name, args or {})
    return anyio.run(_session_call, url or mcp.url, token, fn, headers)


def list_tools(mcp: Mcp, token: str, url: str | None = None, headers=None):
    async def fn(session):
        return (await session.list_tools()).tools
    return anyio.run(_session_call, url or mcp.url, token, fn, headers)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def ok(result) -> dict:
    assert not result.is_error, result.structured_content or result.content
    return result.structured_content


def err(result) -> dict:
    assert result.is_error, result.structured_content
    return result.structured_content


def jdump(x) -> str:
    return json.dumps(x, default=str)
