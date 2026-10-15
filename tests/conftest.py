"""Fixtures: a fresh database per test, a test login, and a signed-in browser."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bets import services
from bets.auth import write_credentials
from bets.config import Settings
from bets.db import Database
from bets.web import create_app

USER, PASSWORD = "tester", "correct horse battery staple"


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "bets.sqlite3")
    d.migrate()
    return d


@pytest.fixture
def conn(db):
    with db.tx() as c:
        yield c


def make_settings(tmp_path: Path, **kw) -> Settings:
    s = Settings(data_dir=tmp_path / "var", secure_cookies=False)
    for k, v in kw.items():
        setattr(s, k, v)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    write_credentials(s.auth_file, USER, PASSWORD)
    return s


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def anon(app):
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


class Browser:
    def __init__(self, client: TestClient):
        self.c = client
        self.csrf = ""

    def login(self, user=USER, password=PASSWORD):
        page = self.c.get("/bets/login")
        r = self.c.post("/bets/login", data={"csrf_token": csrf_of(page.text), "username": user, "password": password,
                                             "next": "/bets/"}, follow_redirects=False)
        if r.status_code == 303:
            self.csrf = csrf_of(self.c.get("/bets/").text)
        return r

    def get(self, path, **kw):
        return self.c.get(path, **kw)

    def post(self, path, data=None, **kw):
        kw.setdefault("follow_redirects", False)
        return self.c.post(path, data={"csrf_token": self.csrf, **(data or {})}, **kw)


@pytest.fixture
def browser(app):
    with TestClient(app, base_url="http://testserver") as c:
        b = Browser(c)
        assert b.login().status_code == 303
        yield b


@pytest.fixture
def world(conn):
    """Typical accounts: two bookmakers, one exchange (2%), one bank."""
    ids = {
        "betfred": services.create_account(conn, name="Betfred", kind="bookmaker", opening_balance=0),
        "paddy": services.create_account(conn, name="Paddy Power", kind="bookmaker"),
        "smarkets": services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=200),
        "lloyds": services.create_account(conn, name="Lloyds", kind="bank"),
    }
    return ids
