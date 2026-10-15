"""Web app. Everything lives under /bets and needs the login (except the login page itself
and the stylesheet/icon it uses). Writes need the session's CSRF token and a same-origin
request. Errors are generic; nothing internal is echoed.

The one exception is /bets/api/assistant/: no session there, only the assistant's bearer token, which can submit
candidates, read records (unless the user switched that off) and request named maintenance changes that are applied
or held according to the user's permission levels (see assistant.py and ops.py)."""
from __future__ import annotations

import hmac
import json
import re
import logging
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import rewards, offer_state, exposure, assistant, calc, exports, imports, issues, metrics, outlook, profit, services, times, undo
from . import commission as comm
from . import brief, evidence, ops, plans, readapi
from .auth import COOKIE, LoginLimiter, SessionCodec, load_credentials, load_secret, verify_password
from .config import Settings, load_settings
from .db import Database
from .money import (commission_text, fmt, odds_text, parse_commission, parse_odds, plain, to_pence)
from .services import UserError

log = logging.getLogger("bets")
HERE = Path(__file__).parent
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; manifest-src 'self'")
CATEGORY_LABELS = {
    "opening_balance": "Opening balance", "transfer": "Transfer", "stake": "Stake", "winnings": "Winnings",
    "lay_win": "Lay won", "lay_loss": "Lay lost (liability)", "commission": "Commission", "refund": "Refund",
    "free_bet_winnings": "Free bet winnings", "promotion_reward": "Promotion reward", "bonus_cash": "Bonus cash",
    "adjustment": "Adjustment", "reconciliation": "Reconciliation", "other": "Other"}
CAPITAL_LABELS = {"temporary_funding": "Temporary funding", "temporary_return": "Return of temporary funding",
                  "bankroll_contribution": "Bankroll contribution", "bankroll_withdrawal": "Bankroll withdrawal",
                  "profit_withdrawal": "Profit withdrawal (winnings taken out)",
                  "internal": "Between betting accounts"}
OFFER_STATUS_LABELS = {"not_started": "Not started", "qualifying": "Qualifying", "awaiting_reward": "Awaiting reward",
                       "reward_received": "Reward received", "converting": "Reward being converted",
                       "complete": "Complete", "expired": "Expired", "abandoned": "Abandoned"}
OFFER_TYPE_LABELS = {"sign_up": "Sign-up", "reload": "Reload", "free_bet": "Free bet", "bet_builder": "Bet builder",
                     "enhanced_odds": "Enhanced odds", "qualifying": "Qualifying bet", "recurring": "Recurring",
                     "other": "Other"}
KIND_LABELS = {"qualifying": "Qualifying bet", "free_bet_snr": "Free bet (SNR)", "free_bet_sr": "Free bet (SR)",
               "other": "Other"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    db.migrate()
    codec = SessionCodec(load_secret(settings.secret_file), settings.session_hours * 3600)
    limiter = LoginLimiter(settings.login_max_failures, settings.login_window_seconds)
    BASE = settings.base_path
    tokens = assistant.TokenCheck(settings.assistant_file)
    token_failures = LoginLimiter(per_ip=10, window=900, overall=10 ** 9)   # 256-bit tokens: per IP is enough
    quota = assistant.Quota()
    ASSISTANT = BASE + "/api/assistant/"
    ORIGIN = settings.public_origins[0] if settings.public_origins else ""
    public = {BASE + "/login", BASE + "/static/design.css", BASE + "/static/icon.svg", BASE + "/manifest.webmanifest",
              BASE + "/healthz"} | {BASE + "/static/fonts/" + f for f in (
                  "Archivo-Variable.woff2", "IBMPlexMono-Regular.woff2", "IBMPlexMono-Medium.woff2",
                  "IBMPlexMono-SemiBold.woff2")}   # the login page is styled with these

    def pct(bp: int) -> str:
        """Commission basis points as a percentage for an input: 0 -> '0', 200 -> '2', 150 -> '1.5'."""
        return format(Decimal(bp or 0) / 100, "f")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.settings, app.state.db = settings, db
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    env = templates.env
    env.globals.update(fmt=fmt, plain=plain, local=times.local, relative=times.relative, to_input=times.to_local_input,
                       commission_text=commission_text, commission_promo=comm.promo, pct=pct,
                       promo_date_input=comm.date_input, CATEGORY_LABELS=CATEGORY_LABELS, CAPITAL_LABELS=CAPITAL_LABELS,
                       OFFER_STATUS_LABELS=OFFER_STATUS_LABELS, OFFER_TYPE_LABELS=OFFER_TYPE_LABELS,
                       KIND_LABELS=KIND_LABELS, BASE=BASE, u=lambda p="/": BASE + p)
    def diffrows(before, after) -> list:
        """Fields that differ between two snapshots, flattened ('legs #12 result')."""
        def flat(d, pre=""):
            out = {}
            for k, v in (d or {}).items():
                if isinstance(v, dict):
                    out.update(flat(v, f"{pre}{k} "))
                elif isinstance(v, list) and v and all(isinstance(x, dict) and "id" in x for x in v):
                    for x in v:
                        out.update(flat({kk: vv for kk, vv in x.items() if kk != "id"}, f"{pre}{k} #{x['id']} "))
                elif isinstance(v, list):
                    out[pre + k] = ", ".join(map(str, v)) or "—"
                else:
                    out[pre + k] = "—" if v is None else str(v)
            return out
        b, a = flat(before), flat(after)
        return [(k.replace("_", " "), b.get(k, "—"), a.get(k, "—")) for k in dict.fromkeys([*b, *a]) if b.get(k) != a.get(k)]

    def history_of(entity: str, entity_id: int, limit: int = 12) -> list:
        with db.read() as conn:
            return readapi.history(conn, entity, entity_id, limit)

    evidence.set_dir(settings.data_dir / "evidence")
    env.globals.update(EVIDENCE_KINDS=evidence.KINDS)

    def nice_time(value: str | None, fmt_: str = "%a %-d %b %H:%M") -> str:
        """A local 'YYYY-MM-DD HH:MM' string (as the assistant views give it) for people: 'Thu 15 Oct 20:52'."""
        from datetime import datetime
        try:
            return datetime.strptime(str(value)[:16], "%Y-%m-%d %H:%M").strftime(fmt_)
        except (TypeError, ValueError):
            return value or ""
    env.globals.update(nice_time=nice_time)
    env.globals.update(diffrows=diffrows, history_of=history_of, LEVEL_TITLES=ops.LEVEL_TITLES,
                       CONFIRM_LABELS={"delete_free_bet": "Delete free bet", "delete_position": "Delete position",
                                       "delete_offer": "Delete offer", "reverse_transfer": "Reverse transfer",
                                       "reverse_reconciliation": "Reverse balance check",
                                       "reopen_position": "Reopen position", "void_position": "Void position",
                                       "void_free_bet": "Void free bet", "unlink_free_bet_from_position": "Unlink",
                                       "merge_duplicate_free_bets": "Merge"})
    app.mount(BASE + "/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ------------------------------------------------------------ boundary

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else ""
        if peer == settings.trusted_proxy:
            return request.headers.get("x-real-ip", peer)
        return peer

    def origin_problem(request: Request) -> str | None:
        """Browsers send Origin (or at least Referer) on writes; it must be this site.
        A client sending neither (a script) still needs the session's CSRF token."""
        if request.headers.get("sec-fetch-site") == "cross-site":
            return "Cross-site request blocked"
        origin = request.headers.get("origin")
        if origin == "null":
            return "Cross-site request blocked"
        origin = origin or request.headers.get("referer")
        if not origin:
            return None
        parts = urlsplit(origin)
        here = f"{parts.scheme}://{parts.netloc}"
        allowed = set(settings.public_origins) | {f"http://{request.headers.get('host', '')}"}
        return None if here in allowed else "Cross-site request blocked"

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        path = request.url.path
        if path == BASE:
            q = request.url.query
            return _headers(RedirectResponse(BASE + "/" + (f"?{q}" if q else ""), status_code=308), static=False)
        if not path.startswith(BASE + "/"):
            return _headers(PlainTextResponse("Not found", status_code=404), static=False)
        if path.startswith(ASSISTANT):
            return await assistant_gate(request, call_next)
        session = codec.loads(request.cookies.get(COOKIE))
        fresh = session is None
        if fresh:
            session = codec.new(None)
        request.state.session, request.state.session_dirty = session, fresh
        authed = bool(session.get("u"))
        if not authed and path not in public:
            wants_html = request.method == "GET" and not path.startswith(BASE + "/api/") \
                and not path.endswith((".csv", ".json", ".ics"))
            if wants_html:
                target = path + (f"?{request.url.query}" if request.url.query else "")
                resp = RedirectResponse(f"{BASE}/login?next={quote(target)}", status_code=303)
            else:
                resp = JSONResponse({"error": "Login required"}, status_code=401)
            return _finish(request, resp)
        if request.method not in ("GET", "HEAD"):
            problem = origin_problem(request)
            token = request.headers.get("x-csrf-token", "")
            body = await request.body()
            upload = path == BASE + "/evidence"
            if len(body) > (6 * 1024 * 1024 if upload else 64 * 1024):
                return _finish(request, JSONResponse({"error": "Request too large"}, status_code=413))
            ctype = request.headers.get("content-type", "")
            if not token and ctype.startswith("application/x-www-form-urlencoded"):
                token = (parse_qs(body.decode("utf-8", "replace")).get("csrf_token") or [""])[0]
            if not token and upload and ctype.startswith("multipart/form-data"):
                found = re.search(rb'name="csrf_token"\r\n\r\n([A-Za-z0-9_\-]{10,200})\r\n', body)
                token = found.group(1).decode() if found else ""
            if not problem and not (token and hmac.compare_digest(token, session["csrf"])):
                problem = "Security token missing or expired — reload the page and try again"
            if problem:
                return _finish(request, _error(request, 403, problem))
        try:
            with services.acting("user" if authed else "anonymous"):  # audit rows name the signed-in user
                response = await call_next(request)
        except Exception as exc:  # never leak details
            log.error("request failed: %s", type(exc).__name__)
            response = _error(request, 500, "Something went wrong. Nothing was saved.")
        return _finish(request, response)

    def _api_error(status: int, message: str) -> Response:
        return _headers(JSONResponse({"error": message}, status_code=status), static=False)

    async def assistant_gate(request: Request, call_next):
        """Bearer token only. No session cookie is read or set; the token is valid on no other path."""
        ip = client_ip(request)
        if request.method not in ("GET", "POST"):
            return _api_error(405, "Not allowed")
        token_id = tokens.verify(request.headers.get("authorization"))
        if not token_id:                     # a valid token is never locked out by others' failures
            if token_failures.blocked(ip):
                return _api_error(429, "Too many failed attempts; try again later")
            token_failures.fail(ip)
            resp = _api_error(401, "Assistant token missing, wrong or disabled")
            resp.headers["WWW-Authenticate"] = 'Bearer realm="bets-assistant"'
            return resp
        if request.method == "POST":
            if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return _api_error(415, "Send JSON (Content-Type: application/json)")
            if len(await request.body()) > assistant.MAX_BODY:
                return _api_error(413, "Request too large")
        request.state.assistant = token_id
        try:
            with services.acting(f"assistant:{token_id}"):          # audit rows name the assistant as the actor
                response = await call_next(request)
        except Exception as exc:  # never leak details
            log.error("assistant request failed: %s", type(exc).__name__)
            response = JSONResponse({"error": "Something went wrong. Nothing was saved."}, status_code=500)
        return _headers(response, static=False)

    def _finish(request: Request, response: Response) -> Response:
        if getattr(request.state, "session_dirty", False):
            response.set_cookie(COOKIE, codec.dumps(request.state.session), max_age=settings.session_hours * 3600,
                                path=BASE, httponly=True, secure=settings.secure_cookies, samesite="strict")
        # Only a successful asset may be cached: a cached login redirect would loop after signing in.
        return _headers(response, static=request.url.path.startswith(BASE + "/static/") and response.status_code == 200)

    def _headers(response: Response, static: bool) -> Response:
        h = response.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        h["Cache-Control"] = "private, max-age=3600" if static else "no-store"
        h["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        return response

    def _error(request: Request, status: int, message: str) -> Response:
        if request.url.path.startswith(BASE + "/api/") or "application/json" in request.headers.get("accept", "") \
                or request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"error": message}, status_code=status)
        session = getattr(request.state, "session", None) or {}
        return templates.TemplateResponse(request, "error.html", {
            "status": status, "message": message, "session": session, "csrf": session.get("csrf", ""),
            "path": request.url.path, "flash_ok": "", "flash_err": "",
            "calc_ex": calc_exchanges() if session.get("u") else {"items": [], "default": None}}, status_code=status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        message = {404: "Not found", 405: "Not allowed"}.get(exc.status_code, "Request refused")
        return _error(request, exc.status_code, message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return _error(request, 400, "Some of the values were not valid")

    # ------------------------------------------------------------ helpers

    def nav_counts() -> dict:
        """Small counts beside the top-bar links. Never lets a page fail."""
        try:
            with db.read() as conn:
                one = lambda sql: conn.execute(sql).fetchone()[0]
                return {
                    "positions": one("SELECT count(*) FROM positions WHERE deleted_at IS NULL AND status = 'open'"),
                    "offers": one("SELECT count(*) FROM offers WHERE deleted_at IS NULL AND status IN "
                                  "('qualifying', 'awaiting_reward', 'reward_received', 'converting')"),
                    "free_bets": one("SELECT count(*) FROM free_bets WHERE deleted_at IS NULL AND status = 'available'"),
                    "imports": one("SELECT count(*) FROM import_candidates WHERE status = 'needs_confirmation'")
                               + one("SELECT count(*) FROM assistant_ops WHERE status = 'held'"),
                    "issues": len(issues.collect(conn)),
                    "operations": one("SELECT count(*) FROM bet_plans WHERE status IN ('ready_to_place', 'bookmaker_placed', "
                                      "'exchange_partially_matched')"),
                }
        except Exception:   # counts are decoration
            return {}

    def calc_exchanges() -> dict:
        """Exchanges for the calculators: each one's stored commission (and a warning once a promotional rate has
        ended), with the exchange last used for a lay bet selected by default."""
        try:
            with db.read() as conn:
                rows = accounts(conn, ("exchange",))
                last = conn.execute("SELECT l.account_id FROM legs l JOIN accounts a ON a.id = l.account_id "
                                    "WHERE l.side = 'lay' AND l.deleted_at IS NULL AND a.kind = 'exchange' "
                                    "AND a.active = 1 ORDER BY l.placed_at DESC, l.id DESC LIMIT 1").fetchone()
        except Exception:                       # noqa: BLE001 (never let a page fail over the calculator)
            return {"items": [], "default": None}
        items = [{"id": a["id"], "name": a["name"], "commission": pct(a["commission_bp"]),
                  "stale": comm.stale_warning(a, None)} for a in rows]
        default = last[0] if last else (items[0]["id"] if items else None)
        return {"items": items, "default": default}

    def render(request: Request, name: str, ctx: dict | None = None, status: int = 200) -> Response:
        q = request.query_params
        base_ctx = {"session": request.state.session, "csrf": request.state.session["csrf"],
                    "flash_ok": q.get("ok", "")[:200], "flash_err": q.get("err", "")[:300], "path": request.url.path}
        if base_ctx["flash_ok"] and undo.TOKEN.match(q.get("undo", "")):
            base_ctx["flash_undo"] = (q["undo"], undo.label(q["undo"]))
        base_ctx["nav_counts"] = nav_counts() if request.state.session.get("u") else {}
        base_ctx["calc_ex"] = calc_exchanges() if request.state.session.get("u") else {"items": [], "default": None}
        return templates.TemplateResponse(request, name, {**base_ctx, **(ctx or {})}, status_code=status)

    def back(path: str, ok: str = "", err: str = "", undo_token: str = "") -> RedirectResponse:
        params = {k: v for k, v in (("ok", ok), ("err", err), ("undo", undo_token)) if v}
        sep = "&" if "?" in path else "?"
        return RedirectResponse(BASE + path + (sep + urlencode(params) if params else ""), status_code=303)

    async def form_of(request: Request) -> dict:
        data = await request.form()
        return {k: (v if isinstance(v, str) else "") for k, v in data.items()}

    def safe_next(value: str | None) -> str:
        if value and value.startswith(BASE + "/") and not value.startswith("//") and "\\" not in value:
            return value
        return BASE + "/"

    def opt_int(value) -> int | None:
        value = (value or "").strip() if isinstance(value, str) else value
        return int(value) if value not in (None, "") and str(value).isdigit() else None

    def money_or_error(value: str, label: str, **kw) -> int:
        try:
            return to_pence(value, **kw)
        except ValueError as exc:
            raise UserError(f"{label}: {exc}") from None

    def accounts(conn, kinds=("bookmaker", "exchange", "bank"), active_only=True):
        q = "SELECT * FROM accounts WHERE kind IN (%s)" % ",".join("?" * len(kinds))
        if active_only:
            q += " AND active = 1"
        return [dict(r) for r in conn.execute(q + " ORDER BY name COLLATE NOCASE", kinds)]

    def offers_open(conn):
        return [dict(r) for r in conn.execute(
            "SELECT o.*, a.name AS account FROM offers o LEFT JOIN accounts a ON a.id = o.account_id "
            "WHERE o.deleted_at IS NULL AND o.status NOT IN ('complete', 'expired', 'abandoned') ORDER BY o.title")]

    def free_bets_available(conn):
        return [dict(r) for r in conn.execute(
            "SELECT f.*, a.name AS account FROM free_bets f JOIN accounts a ON a.id = f.account_id "
            "WHERE f.status = 'available' AND f.deleted_at IS NULL ORDER BY f.expires_at IS NULL, f.expires_at")]

    def actor_label(actor: str | None) -> str:
        a = actor or ""
        return {"user": "you", "system": "automatic"}.get(a) or ("assistant, confirmed by you" if a.startswith("assistant+")
                                                                 else ("assistant" if a.startswith("assistant") else a))

    def done_line(conn, r: dict) -> dict:
        """One 'Completed today' entry in words (audit rows for statuses carry no note of their own)."""
        after = json.loads(r["after_json"] or "{}") if r.get("after_json") else {}
        text = r["note"]
        if r["entity"] == "position":
            p = services.row(conn, "positions", r["entity_id"]) if r["entity_id"] else None
            text = f"Position #{r['entity_id']}{(' ' + p['event']) if p else ''}: {after.get('status', r['action'])}"
        elif r["entity"] == "plan":
            p = services.row(conn, "bet_plans", r["entity_id"]) if r["entity_id"] else None
            what = "created" if r["action"] == "created" else str(after.get("status", r["action"])).replace("_", " ")
            text = f"Bet plan #{r['entity_id']}{(' ' + p['event']) if p else ''}: {what}"
        return {"at": r["at"], "text": text or r["action"].replace("_", " "), "who": actor_label(r["actor"])}

    def position_rows(conn, where: str = "", params: tuple = (), limit: int = 200):
        rows = conn.execute(
            "SELECT p.*, o.title AS offer_title, "
            "(SELECT group_concat(a.name || ' ' || l.side, ', ') FROM legs l JOIN accounts a ON a.id = l.account_id "
            " WHERE l.position_id = p.id AND l.deleted_at IS NULL) AS legs_text, "
            "(SELECT count(*) FROM legs l WHERE l.position_id = p.id AND l.deleted_at IS NULL AND l.side = 'back') AS backs, "
            "(SELECT count(*) FROM legs l WHERE l.position_id = p.id AND l.deleted_at IS NULL AND l.side = 'lay') AS lays "
            "FROM positions p LEFT JOIN offers o ON o.id = p.offer_id WHERE p.deleted_at IS NULL " + where +
            f" ORDER BY coalesce(p.event_at, p.created_at) DESC, p.id DESC LIMIT {int(limit)}", params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["pnl"] = services.position_pnl(conn, d["id"]) if d["status"] != "open" else None
            d["hedging"] = services.hedge_state(d, d["lays"])
            d["incomplete"] = d["status"] == "open" and d["kind"] != "other" and (not d["backs"] or not d["lays"]) \
                and not (d["hedging"] == "intentionally_unhedged" and d["backs"])
            out.append(d)
        return out

    router = APIRouter(prefix=BASE)

    # ------------------------------------------------------------ login

    @router.get("/healthz")
    def healthz():
        return PlainTextResponse("ok")      # nothing else; nginx does not expose it externally

    @router.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse({"name": "Matched betting", "short_name": "Bets", "start_url": BASE + "/",
                             "scope": BASE + "/", "display": "standalone", "background_color": "#ffffff",
                             "theme_color": "#ffffff",
                             "icons": [{"src": BASE + "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]},
                            media_type="application/manifest+json")

    @router.get("/login")
    def login_page(request: Request, next: str = ""):
        if request.state.session.get("u"):
            return RedirectResponse(safe_next(next), status_code=303)
        configured = load_credentials(settings.auth_file) is not None
        return render(request, "login.html", {"next": safe_next(next), "configured": configured})

    @router.post("/login")
    async def login(request: Request):
        f = await form_of(request)
        ip = client_ip(request)
        nxt = safe_next(f.get("next"))
        if limiter.blocked(ip):
            return render(request, "login.html", {"next": nxt, "configured": True,
                                                  "error": "Too many attempts. Try again in 15 minutes."}, 429)
        creds = load_credentials(settings.auth_file)
        ok = bool(creds) and hmac.compare_digest(f.get("username", "").strip().lower().encode(),
                                                 creds["username"].lower().encode()) \
            and verify_password(f.get("password", ""), creds["password_hash"])
        if not ok:
            limiter.fail(ip)
            log.warning("login failed")
            return render(request, "login.html", {"next": nxt, "configured": bool(creds),
                                                  "error": "Wrong username or password."}, 401)
        limiter.reset(ip)
        request.state.session = codec.new(creds["username"])          # new session id + new CSRF token
        request.state.session_dirty = True
        with db.tx() as conn:
            services.audit(conn, "session", None, "login", None, None, "")
        return RedirectResponse(nxt, status_code=303)

    @router.post("/logout")
    def logout(request: Request):
        request.state.session = codec.new(None)
        request.state.session_dirty = True
        return RedirectResponse(BASE + "/login", status_code=303)

    # ------------------------------------------------------------ dashboard

    def with_bets(conn, positions: list[dict]) -> list[dict]:
        """Each position with its bets, lay liability and the result either way (for inline odds cells)."""
        for p in positions:
            legs = [dict(r) for r in conn.execute(
                "SELECT l.*, a.name AS account FROM legs l JOIN accounts a ON a.id = l.account_id "
                "WHERE l.position_id = ? AND l.deleted_at IS NULL ORDER BY l.side, l.id", (p["id"],))]
            for leg in legs:
                leg["liability"] = calc.lay_liability(leg["stake_pence"], Decimal(leg["odds"])) if leg["side"] == "lay" else 0
                leg["free"] = leg["side"] == "back" and bool(leg["free_bet_id"] or p["kind"] in services.FREE_BET_KINDS)
            p["legs"] = legs
            p["liability"] = (metrics.open_lay_liability(conn, position_id=p["id"]) if p["status"] == "open"
                              else sum(leg["liability"] for leg in legs))
            p["if_back"], p["if_lay"] = exposure.outcomes(conn, p["id"], lambda: imports.outcomes({"kind": p["kind"], "legs": legs})) if legs else (None, None)
        return positions

    def up_next(conn, s, now: str, soon: str, free_bets: list[dict]) -> list[dict]:
        """One chronological list: what needs the user now, then open bets and deadlines by day (UK time).
        Free bets of one bookmaker expiring at the same moment are one item, not one per token."""
        today = times.now().astimezone(times.UK).date()
        needs, dated = [], []

        def group_of(at: str | None) -> tuple:
            if not at:
                return (9, "No start time", "")
            d = times.parse(at).astimezone(times.UK).date()
            diff = (d - today).days
            if diff <= 0:
                return (0, "Today", d.strftime("%a %-d %b"))
            if diff == 1:
                return (1, "Tomorrow", d.strftime("%a %-d %b"))
            week = (d.isocalendar()[1] - today.isocalendar()[1]) % 53
            return (2, "This week", "") if week == 0 else ((3, "Next week", "") if week == 1 else (4, "Later", ""))

        expiring: dict = {}
        for f in free_bets:
            if f["expires_at"]:
                expiring.setdefault((f["account_id"], f["expires_at"], f["kind"]), []).append(f)
        for (account_id, at, kind), fbs in expiring.items():
            n, total = len(fbs), sum(f["face_value_pence"] for f in fbs)
            item = {"at": at, "kind": "Expires", "cls": "k-red" if at <= soon else "k-ink",
                    "when": times.local(at, "%a %H:%M"),
                    "title": f"{n} {fbs[0]['account']} free bet{'s' if n > 1 else ''}",
                    "text": f"{kind.upper()} · {times.local(at)} · {times.relative(at)}",
                    "worth": (fmt(total).replace(".00", ""), "to use", ""),
                    "action": ("Find a match", f"/quick-add?kind=free_bet_{kind}&free_bet_id={fbs[0]['id']}"
                                               f"&back_account={account_id}", at <= soon)}
            (needs if at <= soon else dated).append(item)
        for e in exports.deadline_events(conn, "offer"):
            item = {"at": e["at"], "kind": "Deadline", "cls": "k-red" if e["at"] <= soon else "k-ink",
                    "when": times.local(e["at"], "%a %H:%M"), "title": e["title"].replace("Offer deadline: ", ""),
                    "text": f"Offer ends {times.local(e['at'])} · {times.relative(e['at'])}",
                    "action": ("Open offer", "/offers/" + e["uid"][6:], False)}
            (needs if e["at"] <= soon else dated).append(item)
        pending = conn.execute("SELECT count(*) FROM import_candidates WHERE status = 'needs_confirmation'").fetchone()[0]
        if pending:
            needs.append({"at": "", "kind": "Inbox", "cls": "k-red", "when": str(pending),
                          "title": "Confirm imported entries", "text": "Nothing reaches the ledger until you confirm it.",
                          "action": ("Review", "/imports", True)})
        for p in with_bets(conn, position_rows(conn, "AND p.status = 'open'", limit=100)):
            item = {"at": p["event_at"] or "", "pos": p, "title": p["event"],
                    "text": " · ".join(x for x in (p["selection"], p["offer_title"] or KIND_LABELS[p["kind"]]) if x)}
            if p["incomplete"]:
                needs.append(item | {"kind": "Incomplete", "cls": "k-amb", "when": times.local(p["event_at"], "%a %H:%M"),
                                     "text": item["text"] + " · missing a back or lay bet",
                                     "action": ("Add the bet", f"/positions/{p['id']}", True)})
            elif p["event_at"] and p["event_at"] <= now:
                needs.append(item | {"kind": "Started", "cls": "k-amb", "when": times.local(p["event_at"], "%a %H:%M"),
                                     "text": item["text"] + " · enter the result when it settles",
                                     "action": ("Settle", f"/positions/{p['id']}#settle", True)})
            else:
                dated.append(item | {"kind": "Kick-off" if p["event_at"] else "Open",
                                     "cls": "k-ink", "when": times.local(p["event_at"], "%H:%M") if p["event_at"] else "—",
                                     "action": ("Open", f"/positions/{p['id']}", False)})
        for d in outlook.temporary_dues(conn):
            if not d["due_at"]:
                continue
            short = max(0, d["remaining"] - max(s.available, 0))
            item = {"at": d["due_at"], "kind": "Due", "cls": "k-amb" if d["due_at"] <= soon else "k-ink",
                    "when": times.local(d["due_at"], "%a %H:%M"),
                    "title": f"Return {fmt(d['remaining'])} to {d['bank']}",
                    "text": f"Temporary funding sent to {d['account']} · {times.relative(d['due_at'])}"
                            + (f" · {fmt(short)} short until open bets settle" if short else ""),
                    "action": ("Record return", f"/accounts?from={d['account_id']}&to={d['bank_id']}"
                               f"&amount={plain(d['remaining'])}&type=temporary_return#move", d["due_at"] <= soon)}
            (needs if d["due_at"] <= soon else dated).append(item)
        for t in outlook.in_transit(conn):
            needs.append({"at": "", "kind": "On its way", "cls": "k-ink", "when": times.local(t["at"], "%a"),
                          "title": f"{fmt(t['amount_pence'])} from {t['from_name']} to {t['to_name']}",
                          "text": f"Withdrawal sent {times.relative(t['at'])} · tap when it reaches your bank",
                          "arrived": t["id"]})
        cash = outlook.withdrawable(conn)
        if cash:
            total = sum(c["balance"] for c in cash)
            needs.append({"at": "", "kind": "Cash", "cls": "k-ink", "when": str(len(cash)),
                          "title": f"Withdraw {fmt(total)} from {len(cash)} bookmaker{'s' if len(cash) > 1 else ''}",
                          "text": " · ".join(f"{c['account']} {fmt(c['balance'])}" for c in cash[:4])
                                  + " · nothing left to do there",
                          "snooze": [c["account_id"] for c in cash],
                          "action": ("Record withdrawal", f"/accounts?from={cash[0]['account_id']}"
                                     f"&amount={plain(cash[0]['balance'])}#move", False)})
        for o in conn.execute("SELECT o.*, a.name AS account FROM offers o LEFT JOIN accounts a ON a.id = o.account_id "
                              "WHERE o.deleted_at IS NULL AND o.status = 'awaiting_reward' AND o.reward_credited = 0"):
            needs.append({"at": "", "kind": "Reward", "cls": "k-blue", "when": "due", "title": o["title"],
                          "text": f"{o['account'] or 'Bookmaker'} · {o['reward'] or 'reward'} · qualifier settled, has it arrived?",
                          "reward_offer": o["id"] if o["reward_each_pence"] and o["reward_kind"] and o["account_id"] else None,
                          "action": ("Open offer", f"/offers/{o['id']}", False)})
        for p in comm.promos(conn):                 # promotional commission rates ending (30 days, 7 days, ended)
            if p["level"] == "later":
                continue
            verify = f"/accounts/{p['account_id']}?edit=commission#edit"
            item = {"at": p["expires_at"], "kind": "Commission", "when": times.local(p["expires_at"], "%-d %b"),
                    "title": p["title"], "text": p["text"]}
            if p["level"] == "soon":
                dated.append(item | {"cls": "k-ink", "action": ("Open account", f"/accounts/{p['account_id']}", False)})
            elif p["level"] == "urgent":
                needs.append(item | {"cls": "k-amb", "action": ("Check rate", verify, True)})
            else:
                needs.append(item | {"cls": "k-red", "when": "ended", "action": ("Verify rate", verify, True)})
        rest = [i for i in issues.collect(conn, s)
                if not i["key"].startswith(("started:", "incomplete:", "reward:", "commission:"))]
        if rest:
            fix = any(i["severity"] == "fix" for i in rest)
            needs.append({"at": "", "kind": "Issues", "cls": "k-red" if fix else "k-amb", "when": str(len(rest)),
                          "title": f"{len(rest)} thing{'s' if len(rest) > 1 else ''} to put right",
                          "text": " · ".join(i["title"] for i in rest[:2]) + (" …" if len(rest) > 2 else ""),
                          "action": ("See issues", "/issues", fix)})
        needs.sort(key=lambda i: (i["at"] == "", i["at"]))
        groups = [{"label": "Needs you", "date": "", "items": needs}]
        by_group: dict = {}
        for item in sorted(dated, key=lambda i: (i["at"] == "", i["at"])):
            by_group.setdefault(group_of(item["at"]), []).append(item)
        for (order, label, date), items in sorted(by_group.items()):
            groups.append({"label": label, "date": date, "items": items})
        return groups

    @router.get("/")
    def dashboard(request: Request):
        with db.tx() as conn:
            services.expire_overdue(conn)
            offer_state.sync_all(conn)
            issues.sync_dismissals(conn)
        with db.read() as conn:
            s = metrics.summary(conn)
            now = times.now_iso()
            soon = times.iso(times.now() + timedelta(days=7))
            free_bets = free_bets_available(conn)
            groups = up_next(conn, s, now, soon, free_bets)
            ctx = {
                "s": s, "now": now, "soon": soon,
                "up_next": groups,
                "come": outlook.still_to_come(conn),
                "expiry": next((i for i in groups[0]["items"] if i["kind"] == "Expires"), None),
                "free_bets": free_bets,
                "offers_waiting": [dict(r) for r in conn.execute(
                    "SELECT o.*, a.name AS account FROM offers o LEFT JOIN accounts a ON a.id = o.account_id "
                    "WHERE o.deleted_at IS NULL AND o.status IN ('qualifying', 'awaiting_reward', 'reward_received', "
                    "'converting') ORDER BY o.expires_at IS NULL, o.expires_at")],
                "recent_settled": with_bets(conn, sorted(position_rows(conn, "AND p.status <> 'open'", limit=50),
                                                         key=lambda p: (p["settled_at"] or "", p["id"]), reverse=True)[:6]),
                "recent_ledger": [dict(r) for r in conn.execute(
                    "SELECT e.*, a.name AS account FROM ledger_entries e JOIN accounts a ON a.id = e.account_id "
                    "WHERE e.superseded_at IS NULL ORDER BY e.at DESC, e.id DESC LIMIT 3")],
                "has_accounts": conn.execute("SELECT count(*) FROM accounts").fetchone()[0] > 0,
            }
        return render(request, "dashboard.html", ctx)

    # ------------------------------------------------------------ profit / winnings (read-only reporting)

    @router.get("/profit")
    def profit_page(request: Request):
        q = request.query_params
        with db.read() as conn:
            s = metrics.summary(conn)
            items = profit.items(conn)
            view = q.get("view") if q.get("view") in profit.VIEWS else profit.default_view(items)
            now = times.now().astimezone(times.UK)
            years = sorted({i["local"].year for i in items} | {now.year}, reverse=True)
            year = int(q["year"]) if (q.get("year") or "").isdigit() and int(q["year"]) in years else now.year
            rows = profit.periods(items, view, year if view == "year" else None)
            keys = {r["key"]: r for r in rows}
            months_by_year: dict = {}
            if view == "all":                 # Year -> Month: only months with settled activity, running total all time
                for m in profit.periods(items, "month"):
                    if m["count"]:
                        months_by_year.setdefault(m["key"][:4], []).append(m)
                month_keys = {m["key"]: m for ms in months_by_year.values() for m in ms}
            else:
                month_keys = {}
            selected_key = q.get("p") if q.get("p") in keys or q.get("p") in month_keys else None
            if selected_key in month_keys:
                selected = [i for i in items if profit.in_period(i, "month", selected_key)]
            else:
                selected = [i for i in items if profit.in_period(i, view, selected_key)] if selected_key else items
            ctx = {
                "s": s, "view": view, "year": year, "years": years, "rows": rows, "table": list(reversed(rows)),
                "has_other": any(r["other"] for r in rows),
                "chart": profit.chart(rows) if view != "all" else None,
                "history": profit.history(items) if view == "all" else None,
                "selected": keys.get(selected_key) or month_keys.get(selected_key),
                "lines": profit.contributions(selected) if selected_key else [],
                "scope": (keys.get(selected_key) or month_keys.get(selected_key))["label"] if selected_key else "All time",
                "months_by_year": months_by_year,
                "open_year": (selected_key or "")[:4] if selected_key in month_keys else None,
                "by_bookmaker": profit.breakdown(selected, "bookmaker"),
                "by_offer": profit.breakdown(selected, "offer"),
                "by_type": profit.breakdown(selected, "type"),
                "offers_done": profit.completed_offers(conn),
                "w": profit.winnings(conn, s),
                "eff": outlook.efficiency(conn),
                "this_month": profit.period_key(now, "month"), "this_week": profit.period_key(now, "week"),
                "now_month_label": now.strftime("%B"),
            }
        return render(request, "profit.html", ctx)

    @router.post("/profit/plan")
    async def profit_plan(request: Request):
        def fn(conn, f):
            try:
                minimum = to_pence(f.get("min_bankroll") or "0")
            except ValueError as exc:
                raise UserError(f"Minimum bankroll: {exc}") from None
            pct = (f.get("put_away_pct") or "0").strip().rstrip("%")
            if not pct.isdigit():
                raise UserError("Enter the percentage as a whole number from 0 to 100")
            profit.save_plan(conn, minimum, int(pct))
        return await write(request, fn, "Winnings plan saved. It only calculates figures; nothing is moved.",
                           "/profit#plan")

    # ------------------------------------------------------------ quick add (manual entry -> same pipeline)

    def quick_raw(conn, f: dict) -> dict:
        kind = f.get("kind") or "qualifying"
        legs = []
        back_stake = f.get("back_stake", "").strip()
        free_bet_id = opt_int(f.get("free_bet_id"))
        if free_bet_id and not back_stake:
            fb = services.row(conn, "free_bets", free_bet_id)
            back_stake = plain(fb["face_value_pence"]) if fb else ""
        if f.get("back_account") or back_stake or f.get("back_odds"):
            legs.append({"account_id": opt_int(f.get("back_account")), "side": "back", "stake": back_stake,
                         "odds": f.get("back_odds", "").strip(), "free_bet_id": free_bet_id,
                         "placed_at": f.get("placed_at") or None})
        if f.get("lay_account") or f.get("lay_odds"):
            lay_stake = f.get("lay_stake", "").strip()
            commission = f.get("commission", "").strip()
            if not lay_stake:
                try:
                    m = calc.match(kind if kind in calc.KINDS else "qualifying", to_pence(back_stake, allow_zero=False),
                                   parse_odds(f.get("back_odds")), parse_odds(f.get("lay_odds")),
                                   parse_commission(commission) if commission else
                                   (services.row(conn, "accounts", opt_int(f.get("lay_account")) or 0) or {}).get("commission_bp", 0))
                    lay_stake = plain(m.lay_stake)
                except (ValueError, TypeError):
                    lay_stake = ""
            legs.append({"account_id": opt_int(f.get("lay_account")), "side": "lay", "stake": lay_stake,
                         "odds": f.get("lay_odds", "").strip(), "commission": commission,
                         "placed_at": f.get("placed_at") or None})
        return {"event": f.get("event", ""), "selection": f.get("selection", ""), "market": f.get("market", ""),
                "event_at": f.get("event_at") or None, "kind": kind, "offer_id": opt_int(f.get("offer_id")),
                "notes": f.get("notes", ""), "position_id": opt_int(f.get("position_id")), "legs": legs}

    def suggestions(conn) -> dict:
        """Recently used events, selections and markets, so they are typed the same way every time."""
        def recent(col: str) -> list[str]:
            return [r[0] for r in conn.execute(
                f"SELECT {col} FROM positions WHERE deleted_at IS NULL AND {col} <> '' GROUP BY {col} COLLATE NOCASE "
                f"ORDER BY max(created_at) DESC LIMIT 200")]
        return {"events": recent("event"), "selections": recent("selection"), "markets": recent("market")}

    def quick_form_ctx(conn, values: dict) -> dict:
        return {"bookmakers": accounts(conn, ("bookmaker", "exchange")), "exchanges": accounts(conn, ("exchange",)),
                "offers": offers_open(conn), "free_bets": free_bets_available(conn), "v": values,
                "suggest": suggestions(conn)}

    @router.get("/quick-add")
    def quick_add(request: Request):
        with db.read() as conn:
            values = dict(request.query_params)
            offer = services.row(conn, "offers", opt_int(values.get("offer_id")) or 0)
            if offer and not offer["deleted_at"]:                 # pre-fill the qualifying bet from the offer's terms
                if offer["account_id"]:
                    values.setdefault("back_account", str(offer["account_id"]))
                if offer["qualifying_stake_pence"]:
                    values.setdefault("back_stake", plain(offer["qualifying_stake_pence"]))
                values.setdefault("kind", "qualifying")
                values["offer_hint"] = " · ".join(x for x in (
                    f"stake {fmt(offer['qualifying_stake_pence'])}" if offer["qualifying_stake_pence"] else "",
                    f"min odds {offer['min_odds']}" if offer["min_odds"] else "", offer["requirement"]) if x)
            return render(request, "quick_add.html", quick_form_ctx(conn, values))

    @router.post("/quick-add/preview")
    async def quick_preview(request: Request):
        f = await form_of(request)
        with db.read() as conn:
            raw = quick_raw(conn, f)
            try:
                candidate, warnings = imports.normalise(conn, raw, "quick_add")
            except imports.CandidateError as exc:
                return render(request, "quick_add.html", {**quick_form_ctx(conn, f), "errors": exc.errors}, 400)
            pv = imports.preview(conn, candidate)
            return render(request, "quick_preview.html", {"c": candidate, "pv": pv, "warnings": warnings, "f": f})

    @router.post("/quick-add/confirm")
    async def quick_confirm(request: Request):
        f = await form_of(request)
        try:
            with db.tx() as conn:
                candidate, _ = imports.normalise(conn, quick_raw(conn, f), "quick_add")
                pid, _ = imports.apply(conn, candidate)
        except (imports.CandidateError, UserError) as exc:
            with db.read() as conn:
                errs = exc.errors if isinstance(exc, imports.CandidateError) else [str(exc)]
                return render(request, "quick_add.html", {**quick_form_ctx(conn, f), "errors": errs}, 400)
        return back(f"/positions/{pid}", ok="Saved")

    # ------------------------------------------------------------ calculator

    def calc_from(params) -> tuple[calc.Match | None, str]:
        """A typed commission wins; otherwise the chosen exchange's stored rate (never written back)."""
        try:
            kind = params.get("kind") or "qualifying"
            typed = (params.get("commission") or "").strip()
            if typed or not opt_int(params.get("exchange")):
                bp = parse_commission(typed or "0")
            else:
                with db.read() as conn:
                    acc = services.row(conn, "accounts", opt_int(params.get("exchange")))
                bp = acc["commission_bp"] if acc and acc["kind"] == "exchange" else 0
            return calc.match(kind, to_pence(params.get("stake", ""), allow_zero=False), parse_odds(params.get("back_odds")),
                              parse_odds(params.get("lay_odds")), bp), ""
        except ValueError as exc:
            return None, str(exc)

    @router.get("/calculator")
    def calculator(request: Request):
        p = request.query_params
        m, err = calc_from(p) if p.get("stake") else (None, "")
        ex = calc_exchanges()
        chosen = next((e for e in ex["items"] if str(e["id"]) == p.get("exchange")), None) if "exchange" in p else \
            next((e for e in ex["items"] if e["id"] == ex["default"]), None)
        commission = (p.get("commission") or "").strip() or (chosen["commission"] if chosen else ("" if ex["items"] else "2"))
        return render(request, "calculator.html", {"m": m, "err": err, "p": dict(p), "chosen": chosen,
                                                   "commission": commission})

    @router.get("/api/calc")
    def api_calc(request: Request):
        m, err = calc_from(request.query_params)
        if not m:
            return JSONResponse({"error": err}, status_code=400)
        return {"lay_stake": plain(m.lay_stake), "liability": plain(m.liability), "if_back_wins": plain(m.if_back_wins),
                "if_lay_wins": plain(m.if_lay_wins), "expected": plain(m.expected),
                "retention_pct": str(m.retention_pct) if m.retention_pct is not None else None,
                "text": {"lay_stake": fmt(m.lay_stake), "liability": fmt(m.liability),
                         "if_back_wins": fmt(m.if_back_wins, signed=True), "if_lay_wins": fmt(m.if_lay_wins, signed=True),
                         "expected": fmt(m.expected, signed=True)}}

    # ------------------------------------------------------------ positions

    @router.get("/positions")
    def positions(request: Request):
        p = request.query_params
        where, params = [], []
        status = p.get("status", "open")
        if status in ("open", "settled", "void"):
            where.append("p.status = ?")
            params.append(status)
        if opt_int(p.get("account")):
            where.append("EXISTS (SELECT 1 FROM legs l WHERE l.position_id = p.id AND l.deleted_at IS NULL AND l.account_id = ?)")
            params.append(int(p["account"]))
        if opt_int(p.get("offer")):
            where.append("p.offer_id = ?")
            params.append(int(p["offer"]))
        for key, op in (("from", ">="), ("to", "<=")):
            if p.get(key):
                try:
                    params.append(times.from_local_input(p[key][:10] + ("T00:00" if key == "from" else "T23:59")))
                    where.append(f"coalesce(p.event_at, p.created_at) {op} ?")
                except ValueError:
                    pass
        with db.read() as conn:
            rows = position_rows(conn, ("AND " + " AND ".join(where)) if where else "", tuple(params), limit=500)
            if status == "incomplete":
                rows = [r for r in rows if r["incomplete"]]
            if status in ("open", "incomplete"):          # what is coming up: soonest start first, no start time last
                rows.sort(key=lambda r: (r["event_at"] is None, r["event_at"] or "", r["id"]))
            if p.get("result") == "profit":
                rows = [r for r in rows if (r["pnl"] or 0) > 0]
            elif p.get("result") == "loss":
                rows = [r for r in rows if (r["pnl"] or 0) < 0]
            now = times.now_iso()
            with_bets(conn, [r for r in rows if r["status"] == "open"])
            for r in rows:
                r["started"] = r["status"] == "open" and bool(r["event_at"]) and r["event_at"] <= now
            open_rows = [r for r in rows if r["status"] == "open" and r.get("if_back") is not None]
            counts = {"open": conn.execute("SELECT count(*) FROM positions WHERE deleted_at IS NULL AND status = 'open'").fetchone()[0],
                      "started": conn.execute("SELECT count(*) FROM positions WHERE deleted_at IS NULL AND status = 'open' "
                                              "AND event_at IS NOT NULL AND event_at <= ?", (now,)).fetchone()[0],
                      "locked": sum(min(r["if_back"], r["if_lay"]) for r in open_rows),
                      "liability": metrics.open_lay_liability(conn)}
            return render(request, "positions.html", {"rows": rows, "p": dict(p), "status": status, "counts": counts,
                                                      "accounts": accounts(conn, ("bookmaker", "exchange"), False),
                                                      "offers": [dict(r) for r in conn.execute(
                                                          "SELECT id, title FROM offers WHERE deleted_at IS NULL ORDER BY title")],
                                                      "total": sum(r["pnl"] or 0 for r in rows)})

    def position_ctx(conn, pid: int) -> dict:
        pos = services.row(conn, "positions", pid)
        if not pos or pos["deleted_at"]:
            raise StarletteHTTPException(404)
        legs = [dict(r) for r in conn.execute(
            "SELECT l.*, a.name AS account, a.kind AS account_kind, f.kind AS free_bet_kind FROM legs l "
            "JOIN accounts a ON a.id = l.account_id LEFT JOIN free_bets f ON f.id = l.free_bet_id "
            "WHERE l.position_id = ? AND l.deleted_at IS NULL ORDER BY l.side, l.id", (pid,))]
        token_kind = services.FREE_BET_KINDS.get(pos["kind"])
        for leg in legs:
            leg["liability"] = exposure.leg_liability(conn, leg, calc.lay_liability(leg["stake_pence"], Decimal(leg["odds"]))) if leg["side"] == "lay" else None
            if token_kind and leg["side"] == "back" and not leg["free_bet_id"]:
                leg["token_options"] = [dict(r) for r in conn.execute(
                    "SELECT f.*, o.title AS offer_title FROM free_bets f LEFT JOIN offers o ON o.id = f.offer_id "
                    "WHERE f.account_id = ? AND f.face_value_pence = ? AND f.kind = ? AND f.deleted_at IS NULL "
                    "AND f.status IN ('available', 'used') AND NOT EXISTS (SELECT 1 FROM legs x WHERE x.free_bet_id = f.id "
                    "AND x.deleted_at IS NULL AND x.result <> 'void') "
                    "ORDER BY f.expires_at IS NULL, f.expires_at, f.created_at, f.id",
                    (leg["account_id"], leg["stake_pence"], token_kind))]
        entries = [dict(r) for r in conn.execute(
            "SELECT e.*, a.name AS account FROM ledger_entries e JOIN accounts a ON a.id = e.account_id "
            "WHERE e.position_id = ? ORDER BY e.superseded_at IS NOT NULL, e.at, e.id", (pid,))]
        leg_ids = [l["id"] for l in legs] + [r[0] for r in conn.execute(
            "SELECT id FROM legs WHERE position_id = ? AND deleted_at IS NOT NULL", (pid,))]
        audit = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE (entity = 'position' AND entity_id = ?) OR (entity = 'leg' AND entity_id IN (%s)) "
            "ORDER BY id DESC LIMIT 100" % (",".join(str(int(i)) for i in leg_ids) or "0"), (pid,))]
        pos["multi_outcome"] = exposure.configured(conn, pid)
        outcome = exposure.outcomes(conn, pid, lambda: imports.outcomes({"kind": pos["kind"], "legs": legs})) if legs else (None, None)
        offer = services.row(conn, "offers", pos["offer_id"]) if pos["offer_id"] else None
        back_leg = next((l for l in legs if l["side"] == "back"), None)
        lay_leg = next((l for l in legs if l["side"] == "lay"), None)
        same = {"event": pos["event"], "selection": pos["selection"], "market": pos["market"], "kind": pos["kind"],
                "offer_id": pos["offer_id"] or "", "back_account": back_leg["account_id"] if back_leg else "",
                "lay_account": lay_leg["account_id"] if lay_leg else "",
                "commission": format(Decimal(lay_leg["commission_bp"]) / 100, "f") if lay_leg else ""}
        hedging = services.hedge_state(dict(pos), conn.execute(
            "SELECT count(*) FROM legs WHERE position_id = ? AND deleted_at IS NULL AND side = 'lay' AND result <> 'void'",
            (pid,)).fetchone()[0])
        return {"hedging": hedging, "pos": pos, "legs": legs, "entries": entries, "audit": audit, "offer": offer,
                "outcomes": brief.position_outcomes(conn, pid), "evidence_items": evidence.for_record(conn, "position", pid),
                "plan_of": conn.execute("SELECT id FROM bet_plans WHERE position_id = ?", (pid,)).fetchone(),
                "same_again": urlencode({k: v for k, v in same.items() if v not in ("", None)}),
                "others": [dict(r) for r in conn.execute(
                    "SELECT id, event, selection, status, event_at FROM positions WHERE deleted_at IS NULL AND id <> ? "
                    "AND (status = 'open' OR event = ? COLLATE NOCASE) ORDER BY event = ? COLLATE NOCASE DESC, "
                    "coalesce(event_at, created_at) DESC LIMIT 60", (pid, pos["event"], pos["event"]))],
                "pnl": services.position_pnl(conn, pid) if pos["status"] != "open" else None,
                "outcome": outcome, "accounts": accounts(conn, ("bookmaker", "exchange")),
                "offers": offers_open(conn) + ([dict(offer)] if offer and offer["status"] in ("complete", "expired", "abandoned") else [])}

    @router.get("/positions/{pid}")
    def position(request: Request, pid: int):
        with db.read() as conn:
            return render(request, "position.html", position_ctx(conn, pid))

    async def write(request: Request, fn, ok: str, back_to: str):
        """Run a service call in one transaction; UserError -> back with the message."""
        try:
            f = await form_of(request)
            with db.tx() as conn:
                result = fn(conn, f)
        except (UserError, imports.CandidateError) as exc:
            return back(back_to, err=str(exc))
        if isinstance(result, Response):
            return result
        return back(back_to, ok=ok)

    @router.post("/legs/{leg_id}/free-bet")
    async def leg_link_free_bet(request: Request, leg_id: int):
        def fn(conn, f):
            if not opt_int(f.get("free_bet_id")):
                raise UserError("Choose the free bet this bet used")
            services.link_free_bet(conn, leg_id, int(f["free_bet_id"]), note=f.get("note", "").strip())
            return back(f"/positions/{services.row(conn, 'legs', leg_id)['position_id']}",
                        ok="Free bet linked (recorded in the audit trail)")
        return await write(request, fn, "", "/positions")

    @router.post("/positions/{pid}")
    async def position_edit(request: Request, pid: int):
        def fn(conn, f):
            services.update_position(conn, pid, {
                "event": f.get("event", "").strip(), "market": f.get("market", "").strip(),
                "selection": f.get("selection", "").strip(), "event_at": times.from_local_input(f.get("event_at")),
                "offer_id": opt_int(f.get("offer_id")), "notes": f.get("notes", "").strip(),
                "kind": f.get("kind") if f.get("kind") in KIND_LABELS else "qualifying"}, note="edited")
        return await write(request, fn, "Position updated", f"/positions/{pid}")

    @router.post("/positions/{pid}/legs")
    async def add_leg(request: Request, pid: int):
        def fn(conn, f):
            raw = {"position_id": pid, "kind": services.row(conn, "positions", pid)["kind"],
                   "legs": [{"account_id": opt_int(f.get("account_id")), "side": f.get("side"), "stake": f.get("stake"),
                             "odds": f.get("odds"), "commission": f.get("commission") or None,
                             "placed_at": f.get("placed_at") or None, "external_ref": f.get("external_ref", ""),
                             "free_bet_id": opt_int(f.get("free_bet_id"))}]}
            candidate, _ = imports.normalise(conn, raw, "manual")
            imports.apply(conn, candidate)
        return await write(request, fn, "Bet added", f"/positions/{pid}")

    @router.post("/legs/{leg_id}")
    async def edit_leg(request: Request, leg_id: int):
        with db.read() as conn:
            leg = services.row(conn, "legs", leg_id)
        if not leg:
            raise StarletteHTTPException(404)

        def fn(conn, f):
            data = {"stake_pence": money_or_error(f.get("stake", ""), "Stake", allow_zero=False),
                    "odds": f.get("odds", "").strip(), "placed_at": times.from_local_input(f.get("placed_at")),
                    "external_ref": f.get("external_ref", "").strip()}
            if leg["side"] == "lay" and f.get("commission", "").strip() != "":
                data["commission_bp"] = parse_commission(f["commission"])
            services.update_leg(conn, leg_id, data, note=f.get("note", "").strip())
        return await write(request, fn, "Bet updated", f"/positions/{leg['position_id']}")

    @router.post("/legs/{leg_id}/delete")
    async def remove_leg(request: Request, leg_id: int):
        with db.read() as conn:
            leg = services.row(conn, "legs", leg_id)
        if not leg:
            raise StarletteHTTPException(404)
        return await write(request, lambda conn, f: services.delete_leg(conn, leg_id, f.get("note", "").strip()),
                           "Bet removed (kept in the audit trail)", f"/positions/{leg['position_id']}")

    @router.post("/legs/{leg_id}/settle")
    async def settle_leg(request: Request, leg_id: int):
        with db.read() as conn:
            leg = services.row(conn, "legs", leg_id)
        if not leg:
            raise StarletteHTTPException(404)

        def fn(conn, f):
            amount = commission = None
            if f.get("amount", "").strip():
                amount = money_or_error(f["amount"], "Amount", allow_negative=True)
            if f.get("commission_paid", "").strip():
                commission = money_or_error(f["commission_paid"], "Commission")
            services.settle_leg(conn, leg_id, f.get("result", ""), amount=amount, commission=commission,
                                at=times.from_local_input(f.get("settled_at")), note=f.get("note", "").strip())
            if f.get("result") != "pending":
                return back(f"/positions/{leg['position_id']}", ok="Bet settled", undo_token=f"leg:{leg_id}")
        return await write(request, fn, "Bet reopened", f"/positions/{leg['position_id']}")

    @router.post("/positions/{pid}/settle")
    async def settle_position(request: Request, pid: int):
        def fn(conn, f):
            services.quick_settle(conn, pid, f.get("outcome", ""))
            label = {"back_won": "back won", "back_lost": "back lost", "void": "void"}[f["outcome"]]
            event = services.row(conn, "positions", pid)["event"]
            if f.get("next"):                       # settled from the dashboard or the issues list: go back there
                target = safe_next(f["next"])
                sep = "&" if "?" in target else "?"
                return RedirectResponse(target + sep + urlencode({"ok": f"Settled {event}: {label}", "undo": f"settle:{pid}"}),
                                        status_code=303)
            return back(f"/positions/{pid}", ok=f"Settled: {label}", undo_token=f"settle:{pid}")
        return await write(request, fn, "", f"/positions/{pid}")

    @router.post("/positions/{pid}/delete")
    async def remove_position(request: Request, pid: int):
        def fn(conn, f):
            if not f.get("note", "").strip():
                raise UserError("Say why this position is being removed")
            services.delete_position(conn, pid, f["note"].strip())
            return back("/positions", ok="Position removed (kept in the audit trail)")
        return await write(request, fn, "", f"/positions/{pid}")

    # ------------------------------------------------------------ offers

    def offer_terms(f: dict) -> dict:
        """The optional structured terms from the offer form (blank = not set)."""
        def money_opt(name, label):
            return money_or_error(f[name], label, allow_zero=False) if f.get(name, "").strip() else None
        def int_opt(name, label):
            v = f.get(name, "").strip()
            if v and not v.isdigit():
                raise UserError(f"{label}: a whole number")
            return int(v) if v else None
        return {"qualifying_stake_pence": money_opt("qualifying_stake", "Qualifying stake"),
                "min_odds": f.get("min_odds", "").strip() or None,
                "reward_count": int_opt("reward_count", "Number of free bets"),
                "reward_each_pence": money_opt("reward_each", "Each free bet"),
                "reward_kind": f.get("reward_kind") or None,
                "reward_expiry_days": int_opt("reward_expiry_days", "Free bet expiry"),
                "reward_trigger": f.get("reward_trigger") or None}

    def offer_fields(f: dict) -> dict:
        return {"title": f.get("title", "").strip(), "account_id": opt_int(f.get("account_id")),
                "offer_type": f.get("offer_type") or "sign_up", "requirement": f.get("requirement", "").strip(),
                "reward": f.get("reward", "").strip(),
                "reward_value_pence": money_or_error(f.get("reward_value") or "0", "Reward value"),
                "starts_at": times.from_local_input(f.get("starts_at")),
                "expires_at": times.from_local_input(f.get("expires_at")), "notes": f.get("notes", "").strip()}

    @router.get("/offers")
    def offers(request: Request):
        show = request.query_params.get("show", "active")
        with db.read() as conn:
            q = ("SELECT o.*, a.name AS account, (SELECT count(*) FROM positions p WHERE p.offer_id = o.id AND p.deleted_at IS NULL) "
                 "AS positions FROM offers o LEFT JOIN accounts a ON a.id = o.account_id WHERE o.deleted_at IS NULL")
            if show == "active":
                q += " AND o.status NOT IN ('complete', 'expired', 'abandoned')"
            rows = [dict(r) for r in conn.execute(q + " ORDER BY o.expires_at IS NULL, o.expires_at, o.id DESC")]
            for r in rows:
                r["realised"] = services.offer_realised(conn, r["id"])
            done = [services.offer_realised(conn, r[0]) for r in conn.execute(
                "SELECT id FROM offers WHERE deleted_at IS NULL AND status = 'complete'")]
            stats = {"running": conn.execute("SELECT count(*) FROM offers WHERE deleted_at IS NULL AND status IN "
                                             "('qualifying', 'awaiting_reward', 'reward_received', 'converting')").fetchone()[0],
                     "done": len(done), "net": sum(done), "avg": (sum(done) // len(done)) if done else 0}
            return render(request, "offers.html", {"rows": rows, "show": show, "stats": stats,
                                                   "bookmakers": accounts(conn, ("bookmaker", "exchange"))})

    @router.post("/offers")
    async def create_offer(request: Request):
        def fn(conn, f):
            d = offer_fields(f)
            oid = services.create_offer(conn, title=d["title"], account_id=d["account_id"], offer_type=d["offer_type"],
                                        requirement=d["requirement"], reward=d["reward"],
                                        reward_value=d["reward_value_pence"], starts_at=d["starts_at"],
                                        expires_at=d["expires_at"], notes=d["notes"], terms=offer_terms(f))
            return back(f"/offers/{oid}", ok="Offer added")
        return await write(request, fn, "", "/offers")

    @router.get("/offers/{oid}")
    def offer(request: Request, oid: int):
        with db.read() as conn:
            o = services.row(conn, "offers", oid)
            if not o or o["deleted_at"]:
                raise StarletteHTTPException(404)
            pos = position_rows(conn, "AND (p.offer_id = ? OR p.id IN (SELECT l.position_id FROM legs l JOIN free_bets f "
                                      "ON f.id = l.free_bet_id WHERE f.offer_id = ? AND l.deleted_at IS NULL))", (oid, oid))
            fbs = [dict(r) for r in conn.execute("SELECT f.*, a.name AS account FROM free_bets f JOIN accounts a ON "
                                                 "a.id = f.account_id WHERE f.offer_id = ? AND f.deleted_at IS NULL", (oid,))]
            cash = [dict(r) for r in conn.execute("SELECT e.*, a.name AS account FROM ledger_entries e JOIN accounts a ON "
                                                  "a.id = e.account_id WHERE e.offer_id = ? AND e.superseded_at IS NULL", (oid,))]
            audit = [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE entity = 'offer' AND entity_id = ? "
                                                   "ORDER BY id DESC LIMIT 50", (oid,))]
            return render(request, "offer.html", {"o": o, "life": offer_state.derive(conn, o),
                                                  "positions": pos, "free_bets": fbs, "cash": cash, "audit": audit,
                                                  "realised": services.offer_realised(conn, oid),
                                                  "bookmakers": accounts(conn, ("bookmaker", "exchange"), False)})

    @router.post("/offers/{oid}")
    async def edit_offer(request: Request, oid: int):
        def fn(conn, f):
            if f.get("action") == "status":
                want = f.get("status") or "follow"
                o = services.row(conn, "offers", oid)
                if want == "follow":
                    if o["status"] in services.OFFER_CLOSURES:
                        data = {"status": "not_started"}        # reopen: it follows its records again
                    else:
                        return
                else:
                    data = {"status": want}
            elif f.get("action") == "qualified":
                offer_state.mark_qualified(conn, oid)
                return
            else:
                data = {**offer_fields(f), **offer_terms(f)}
            services.update_offer(conn, oid, data, note=f.get("note", "").strip())
        return await write(request, fn, "Offer updated", f"/offers/{oid}")

    @router.post("/offers/{oid}/reward")
    async def offer_reward(request: Request, oid: int):
        def fn(conn, f):
            exp = times.from_local_input(f.get("expires_at"))
            er = conn.execute("SELECT id FROM expected_rewards WHERE offer_id = ? AND status IN ('waiting', "
                              "'partially_received') ORDER BY id", (oid,)).fetchone()
            if er:                                         # links what already arrived, creates only what is missing
                got = rewards.mark_received(conn, er[0], expires_at=exp, use_candidates=False)
                ids = got.get("created") or []
                if not ids and not got.get("linked"):
                    raise UserError("That reward is already recorded")
            else:
                ids = services.receive_reward(conn, oid, expires_at=exp)
            msg = f"{len(ids)} free bet{'s' if len(ids) != 1 else ''} added" if ids else "Cash reward recorded"
            token = f"reward:{oid}:" + ",".join(map(str, ids)) if ids else ""
            if f.get("next"):
                target = safe_next(f["next"])
                params = {"ok": msg, **({"undo": token} if token else {})}
                return RedirectResponse(target + ("&" if "?" in target else "?") + urlencode(params), status_code=303)
            return back(f"/offers/{oid}", ok=msg, undo_token=token)
        return await write(request, fn, "", f"/offers/{oid}")

    # ------------------------------------------------------------ free bets

    @router.get("/free-bets")
    def free_bets(request: Request):
        show = request.query_params.get("show", "available")
        with db.tx() as conn:
            services.expire_overdue(conn)
        with db.read() as conn:
            q = ("SELECT f.*, a.name AS account, o.title AS offer_title, "
                 "(SELECT l.position_id FROM legs l WHERE l.free_bet_id = f.id AND l.deleted_at IS NULL LIMIT 1) AS position_id "
                 "FROM free_bets f JOIN accounts a ON a.id = f.account_id LEFT JOIN offers o ON o.id = f.offer_id "
                 "WHERE f.deleted_at IS NULL")
            if show == "available":
                q += " AND f.status = 'available'"
            rows = [dict(r) for r in conn.execute(q + " ORDER BY f.status <> 'available', f.expires_at IS NULL, f.expires_at")]
            for r in rows:
                r["realised"] = services.free_bet_value_realised(conn, r["id"])
                r["link_choices"] = [] if r["position_id"] else [dict(x) for x in conn.execute(
                    "SELECT p.id, p.event, l.free_bet_id FROM legs l JOIN positions p ON p.id = l.position_id WHERE "
                    "l.deleted_at IS NULL AND p.deleted_at IS NULL AND l.side = 'back' AND l.account_id = ? AND "
                    "l.stake_pence = ? AND p.kind IN ('free_bet_snr', 'free_bet_sr') ORDER BY p.id DESC LIMIT 30",
                    (r["account_id"], r["face_value_pence"]))]
            total = sum(r["face_value_pence"] for r in rows if r["status"] == "available")
            now_dt = times.now()
            soon72 = times.iso(now_dt + timedelta(hours=72))
            for r in rows:                                     # share of its life left, for the expiry bar
                if r["status"] == "available" and r["expires_at"] and r["credited_at"]:
                    life = (times.parse(r["expires_at"]) - times.parse(r["credited_at"])).total_seconds()
                    left = (times.parse(r["expires_at"]) - now_dt).total_seconds()
                    r["left"] = f"{max(0.0, min(1.0, left / life)):.3f}" if life > 0 else "0"
            ret = outlook.retention(conn)
            worth = sum(int(r["face_value_pence"] * float(ret[r["kind"]]["rate"])) for r in rows
                        if r["status"] == "available" and r["kind"] in ret)
            return render(request, "free_bets.html", {"rows": rows, "show": show, "total": total, "worth": worth,
                                                      "retention": ret,
                                                      "expiring": sum(r["face_value_pence"] for r in rows if r["status"] == "available"
                                                                      and r["expires_at"] and r["expires_at"] <= soon72),
                                                      "bookmakers": accounts(conn, ("bookmaker", "exchange")),
                                                      "offers": offers_open(conn),
                                                      "all_offers": [dict(r) for r in conn.execute(
                                                          "SELECT id, title, account_id, status FROM offers ORDER BY id DESC")],
                                                      "soon": times.iso(times.now() + timedelta(hours=72))})

    @router.post("/free-bets")
    async def create_free_bet(request: Request):
        def fn(conn, f):
            n = opt_int(f.get("count")) or 1
            fids = services.create_free_bets(conn, count=n, account_id=opt_int(f.get("account_id")) or 0,
                                      face_value=money_or_error(f.get("face_value", ""), "Value", allow_zero=False),
                                      kind=f.get("kind") or "snr", offer_id=opt_int(f.get("offer_id")),
                                      expires_at=times.from_local_input(f.get("expires_at")),
                                      notes=f.get("notes", "").strip())
            return back("/free-bets", ok=f"{n} free bets added" if n > 1 else "Free bet added",
                        undo_token="freebets:" + ",".join(map(str, fids)))
        return await write(request, fn, "", "/free-bets")

    @router.post("/free-bets/{fid}")
    async def edit_free_bet(request: Request, fid: int):
        def fn(conn, f):
            linked = services.token_in_use(conn, fid)
            data = {"status": f.get("status") or None, "expires_at": times.from_local_input(f.get("expires_at")),
                    "notes": f.get("notes", "").strip()}
            data = {k: v for k, v in data.items() if v is not None}
            if "offer_id" in f:                                   # the offer this token came from (or none)
                data["offer_id"] = opt_int(f.get("offer_id"))
            link_to = opt_int(f.get("position_id"))
            if data.get("status") == "used" and not linked:
                if not link_to:
                    raise UserError("Choose the position it was used in (a used free bet is linked to the bet that used it)")
                data.pop("status")
            if linked and data.get("status") in ("available", "void", "expired"):
                raise UserError("It is linked to a bet: change the bet's free bet, or unlink it, first")
            services.update_free_bet(conn, fid, data, note=f.get("note", "").strip())
            if link_to:
                result = ops.run(conn, "link_free_bet_to_position",
                                 {"free_bet_id": str(fid), "position_id": str(link_to), "reason": "linked on the Free bets page"},
                                 actor="user")
                if result["status"] == "blocked":
                    raise UserError(result["reason"])
                if result["level"] != "safe_edit":
                    raise UserError("That bet is recorded as a cash bet: use Link the free bet it used on the position page")
        return await write(request, fn, "Free bet updated", "/free-bets?show=all")

    # ------------------------------------------------------------ accounts, transfers, reconciliation

    @router.get("/accounts")
    def accounts_page(request: Request):
        with db.read() as conn:
            rows = metrics.account_rows(conn, include_inactive=request.query_params.get("show") == "all")
            for r in rows:
                r["last_check"] = conn.execute("SELECT max(at) FROM reconciliations WHERE account_id = ?",
                                               (r["id"],)).fetchone()[0]
            q = request.query_params
            transfers = readapi.list_transfers(conn, include_reversed=q.get("reversed") == "1", limit=40)
            for t in transfers:
                raw = services.row(conn, "transfers", t["id"])
                kinds = (services.row(conn, "accounts", raw["from_account_id"])["kind"],
                         services.row(conn, "accounts", raw["to_account_id"])["kind"])
                try:
                    t["allowed"] = sorted(services.allowed_capital_types(services.default_capital_type(*kinds)))
                except UserError:
                    t["allowed"] = [raw["capital_type"]]
                t["at_input"] = times.to_local_input(raw["at"])
            return render(request, "accounts.html", {"transfers": transfers, "show_reversed": q.get("reversed") == "1",
                "rows": rows, "s": metrics.summary(conn), "show": q.get("show", ""),
                "all_accounts": accounts(conn), "today": times.to_local_input(times.now_iso()),
                "dues": outlook.temporary_dues(conn), "transit": outlook.in_transit(conn),
                "pre": {"from": opt_int(q.get("from")), "to": opt_int(q.get("to")), "amount": q.get("amount", "")[:12],
                        "type": q.get("type", "") if q.get("type", "") in CAPITAL_LABELS else ""}})

    @router.post("/accounts")
    async def create_account(request: Request):
        def fn(conn, f):
            kind = f.get("kind", "")
            opening = money_or_error(f.get("opening_balance") or "0", "Opening balance") if kind != "bank" else 0
            services.create_account(conn, name=f.get("name", ""), kind=kind,
                                    commission_bp=parse_commission(f.get("commission") or "0") if kind == "exchange" else 0,
                                    notes=f.get("notes", ""), opening_balance=opening)
            return back("/settings" if f.get("from") == "settings" else "/accounts", ok="Account added")
        return await write(request, fn, "", "/accounts")

    @router.get("/accounts/check")
    def accounts_check(request: Request):
        cut = times.iso(times.now() - timedelta(days=issues.CHECK_STALE_DAYS))
        with db.read() as conn:
            rows = [r for r in metrics.account_rows(conn) if r["kind"] != "bank"]
            for r in rows:
                last = conn.execute("SELECT at, difference_pence FROM reconciliations WHERE account_id = ? "
                                    "ORDER BY at DESC, id DESC LIMIT 1", (r["id"],)).fetchone()
                r["last_check"] = dict(last) if last else None
                r["stale"] = bool(r["balance"]) and (not last or last["at"] < cut)
            return render(request, "accounts_check.html", {"rows": rows})

    @router.post("/accounts/check")
    async def accounts_check_save(request: Request):
        def fn(conn, f):
            done, adjusted, entries = 0, [], []
            adjust = f.get("adjust") == "1" if "adjust_shown" in f else True     # the form asks; older posts adjusted
            note = f.get("note", "").strip() or "balance check"
            for a in accounts(conn, ("bookmaker", "exchange")):
                typed = f.get(f"actual_{a['id']}", "").strip()
                basis = f.get(f"basis_{a['id']}") if a["kind"] == "exchange" and f.get(f"basis_{a['id']}") in (
                    "available", "total") else "total"
                expected = services.expected_balance(conn, a["id"], basis)["expected"]
                if typed:
                    actual = money_or_error(typed, f"{a['name']} balance", allow_negative=True)
                elif f.get(f"match_{a['id']}"):
                    actual = expected
                else:
                    continue
                entry = services.reconcile(conn, a["id"], actual, note=note, basis=basis, adjust=adjust)
                if entry:
                    entries.append(entry)
                done += 1
                if actual != expected:
                    adjusted.append(f"{a['name']} {fmt(actual - expected, signed=True)}")
            if not done:
                raise UserError("Tick the balances that match, or type the real balance, for at least one account")
            msg = f"{done} balance{'s' if done > 1 else ''} checked" + (
                (": " + ", ".join(adjusted) + (" recorded as reconciliation" if adjust else
                                               " left unresolved: nothing posted to profit (see Issues)"))
                if adjusted else ", all matched")
            return back("/accounts", ok=msg, undo_token="recs:" + ",".join(map(str, entries)) if entries else "")
        return await write(request, fn, "", "/accounts/check")

    @router.post("/undo")
    async def undo_last(request: Request):
        def fn(conn, f):
            msg, where = undo.undo(conn, f.get("token", ""))
            return back(where, ok=msg)
        return await write(request, fn, "", "/")

    @router.post("/legs/{leg_id}/move")
    async def move_leg(request: Request, leg_id: int):
        with db.read() as conn:
            leg = services.row(conn, "legs", leg_id)
        if not leg:
            raise StarletteHTTPException(404)

        def fn(conn, f):
            note = f.get("note", "").strip()
            if f.get("target") == "new":
                pid = services.split_leg(conn, leg_id, note)
            else:
                pid = opt_int(f.get("target")) or 0
                services.move_leg(conn, leg_id, pid, note)
            return back(f"/positions/{pid}", ok="Bet moved; its money moved with it")
        return await write(request, fn, "", f"/positions/{leg['position_id']}")

    @router.post("/positions/{pid}/merge")
    async def merge_position(request: Request, pid: int):
        def fn(conn, f):
            target = opt_int(f.get("target")) or 0
            services.merge_positions(conn, pid, target, f.get("note", "").strip())
            return back(f"/positions/{target}", ok="Positions merged")
        return await write(request, fn, "", f"/positions/{pid}")

    @router.get("/accounts/{aid}")
    def account(request: Request, aid: int):
        with db.read() as conn:
            acc = next((r for r in metrics.account_rows(conn, include_inactive=True) if r["id"] == aid), None)
            if not acc:
                raise StarletteHTTPException(404)
            entries = [dict(r) for r in conn.execute(
                "SELECT * FROM ledger_entries WHERE account_id = ? AND superseded_at IS NULL ORDER BY at, id", (aid,))]
            running = 0
            for e in entries:
                running += e["amount_pence"]
                e["running"] = running
            recs = [readapi.reconciliation(conn, r[0]) | {"raw_outcome": r[1]} for r in conn.execute(
                "SELECT id, outcome FROM reconciliations WHERE account_id = ? ORDER BY id DESC", (aid,))]
            expected = services.expected_balance(conn, aid) if acc["kind"] != "bank" else None
            signups = [dict(r) for r in conn.execute("SELECT id, title, status FROM offers WHERE account_id = ? AND "
                                                     "offer_type = 'sign_up' AND deleted_at IS NULL ORDER BY id DESC", (aid,))]
            return render(request, "account.html", {"a": acc, "entries": list(reversed(entries))[:200], "recs": recs,
                                                    "signups": signups, "promo": comm.promo(acc), "expected": expected,
                                                    "edit": request.query_params.get("edit") == "commission"})

    @router.post("/accounts/{aid}")
    async def edit_account(request: Request, aid: int):
        def fn(conn, f):
            ends = None
            if "commission_expires_shown" in f:
                try:
                    ends = comm.expiry_from_date(f.get("commission_expires")) or ""
                except ValueError as exc:
                    raise UserError(str(exc)) from None
            services.update_account(conn, aid, name=f.get("name"), commission_expires_at=ends,
                                    commission_bp=parse_commission(f["commission"]) if f.get("commission", "").strip() else None,
                                    active=f.get("active") == "1", notes=f.get("notes"),
                                    standing=f.get("standing") or None,
                                    verified=(f.get("verified") == "1") if "verified_shown" in f else None)
        return await write(request, fn, "Account updated", f"/accounts/{aid}")

    @router.post("/accounts/{aid}/reconcile")
    async def reconcile(request: Request, aid: int):
        def fn(conn, f):
            actual = money_or_error(f.get("actual", ""), "Actual balance", allow_negative=True)
            basis = f.get("basis") if f.get("basis") in ("available", "total") else "total"
            adjust = f.get("adjust") == "1" if "adjust_shown" in f else True     # the form asks; older posts adjusted
            entry = services.reconcile(conn, aid, actual, note=f.get("note", ""), basis=basis, adjust=adjust)
            rec = services.row(conn, "reconciliations", services.last_reconciliation_id(conn, aid))
            if rec["outcome"] == "unresolved":
                return back(f"/accounts/{aid}", ok=f"Recorded as unresolved: {fmt(rec['difference_pence'], signed=True)} is unexplained "
                                                    "(app expected " + fmt(rec["app_balance_pence"]) + "). Nothing was posted "
                                                    "to profit: find the missing transfer or bet, or resolve it under Issues.")
            return back(f"/accounts/{aid}", ok="Balance matched — no adjustment needed" if entry is None
                        else "Reconciliation adjustment recorded")
        return await write(request, fn, "", f"/accounts/{aid}")

    @router.post("/transfers")
    async def transfer(request: Request):
        def fn(conn, f):
            tid = services.create_transfer(conn, from_id=opt_int(f.get("from_id")) or 0, to_id=opt_int(f.get("to_id")) or 0,
                                           amount=money_or_error(f.get("amount", ""), "Amount", allow_zero=False),
                                           at=times.from_local_input(f.get("at")), capital_type=f.get("capital_type") or None,
                                           notes=f.get("notes", ""), due_at=times.from_local_input(f.get("due_at")),
                                           in_transit=bool(f.get("in_transit")))
            return back("/accounts", ok="Transfer recorded", undo_token=f"transfer:{tid}")
        return await write(request, fn, "", "/accounts")

    @router.post("/transfers/{tid}/due")
    async def transfer_due(request: Request, tid: int):
        return await write(request, lambda conn, f: services.set_transfer_due(
            conn, tid, times.from_local_input(f.get("due_at")), f.get("note", "").strip()),
            "Return date saved", "/accounts")

    @router.post("/withdrawals/snooze")
    async def withdrawals_snooze(request: Request):
        def fn(conn, f):
            ids = [int(x) for x in f.get("accounts", "").split(",") if x.strip().isdigit()]
            if not ids:
                raise UserError("Nothing to snooze")
            outlook.snooze_withdrawals(conn, ids)
            return RedirectResponse(BASE + "/?" + urlencode({"ok": f"Withdrawal reminder hidden for {outlook.SNOOZE_DAYS} days"}),
                                    status_code=303)
        return await write(request, fn, "", "/")

    @router.post("/transfers/{tid}/void")
    async def void_transfer(request: Request, tid: int):
        def fn(conn, f):
            if not f.get("note", "").strip():
                raise UserError("Say why the transfer is being voided")
            services.void_transfer(conn, tid, f["note"].strip())
        return await write(request, fn, "Transfer voided (kept in the ledger history)", "/ledger?category=transfer&superseded=1")

    # ------------------------------------------------------------ calendar and help

    @router.get("/calendar")
    def calendar_page(request: Request):
        days = 35
        until = times.iso(times.now() + timedelta(days=days))
        kinds = {"free-bet": ("Expires", "k-red", "/free-bets"), "offer": ("Deadline", "k-amb", "/offers/{id}"),
                 "position": ("Kick-off", "k-blue", "/positions/{id}"), "return": ("Due", "k-amb", "/accounts#borrowed"),
                 "commission": ("Commission", "k-amb", "/accounts/{id}")}
        today = times.now().astimezone(times.UK).date()
        by_day: dict = {}
        with db.read() as conn:
            for e in exports.deadline_events(conn):
                if e["at"] > until and not e["uid"].startswith("commission-"):   # rare: shown however far ahead
                    continue
                prefix, _, item = e["uid"].rpartition("-")
                kind, cls, href = kinds.get(prefix, ("", "k-ink", "/"))
                d = times.parse(e["at"]).astimezone(times.UK).date()
                by_day.setdefault(d, []).append({"kind": kind, "cls": cls, "href": href.replace("{id}", item),
                                                 "time": "All day" if e.get("all_day") else times.local(e["at"], "%H:%M"),
                                                 "title": e["title"].replace("Starts: ", "").replace("Offer deadline: ", ""),
                                                 "text": e.get("description") or ""})
        day_list = []
        for d in sorted(by_day):
            diff = (d - today).days
            day_list.append({"label": "Today" if diff == 0 else ("Tomorrow" if diff == 1 else d.strftime("%A")),
                             "date": d.strftime("%-d %b" if d.year == today.year else "%-d %b %Y"),
                             "events": by_day[d]})
        return render(request, "calendar.html", {"day_list": day_list, "days": days})

    @router.get("/operations")
    def operations_page(request: Request):
        with db.read() as conn:
            acts = brief.next_actions(conn)
            found = issues.collect(conn)
            start = times.iso(times.now().astimezone(times.UK).replace(hour=0, minute=0, second=0, microsecond=0))
            done = [done_line(conn, dict(r)) for r in conn.execute(
                "SELECT at, entity, entity_id, action, note, actor, after_json FROM audit_log WHERE at >= ? AND (entity = "
                "'change' OR (entity = 'position' AND action = 'status') OR (entity = 'plan' AND action IN ('created', "
                "'status'))) ORDER BY id DESC LIMIT 60", (start,))]
        return render(request, "operations.html", {
            "needs_me": [a for a in acts if a["needs_user_externally"]],
            "assistant": [a for a in acts if a["assistant_can_resolve"] and not a["needs_user_externally"]],
            "waiting": [a for a in acts if not a["assistant_can_resolve"] and not a["needs_user_externally"]],
            "problems": [i for i in found if i["severity"] in ("fix", "act")], "done": done,
            "LINKS": {"plan": "/plans/", "position": "/positions/", "offer": "/offers/", "free_bet": "/free-bets?show=all#fb-",
                      "transfer": "/accounts#transfers", "expected_reward": "/plans?show=all#r"}})

    @router.get("/plans")
    def plans_page(request: Request):
        show = request.query_params.get("show", "active")
        with db.read() as conn:
            return render(request, "plans.html", {
                "rows": brief.list_plans(conn, "active" if show == "active" else None, 100), "show": show,
                "bookmakers": accounts(conn, ("bookmaker", "exchange")), "exchanges": accounts(conn, ("exchange",)),
                "free_bets": [r for r in free_bets_available(conn) if not plans.live_for_free_bet(conn, r["id"])],
                "offers": offers_open(conn)})

    @router.get("/plans/{pid}")
    def plan_page(request: Request, pid: int):
        with db.read() as conn:
            try:
                p = brief.plan(conn, pid)
            except UserError:
                raise StarletteHTTPException(404) from None
        return render(request, "plan.html", {"p": p})

    @router.post("/evidence")
    async def evidence_upload(request: Request):
        form = await request.form()
        target = _next(str(form.get("next") or "/"))
        upload = form.get("file")
        data = await upload.read() if upload is not None and getattr(upload, "filename", "") else None
        try:
            with db.tx() as conn:
                eid, created = evidence.add(conn, entity=str(form.get("entity") or ""), entity_id=int(form.get("entity_id") or 0),
                                            kind=str(form.get("kind") or "other"), description=str(form.get("description") or ""),
                                            source="upload", file=(data, upload.filename) if data else None)
        except (ValueError, TypeError) as exc:
            return back(target, err=str(exc))
        return back(target, ok="Evidence attached" if created else "That evidence was already attached")

    @router.get("/evidence/{eid}/file")
    def evidence_file(eid: int):
        from fastapi.responses import FileResponse
        with db.read() as conn:
            found = evidence.file_of(conn, eid)
        if not found:
            raise StarletteHTTPException(404)
        return FileResponse(found[0], media_type=found[1], headers={"Content-Disposition": "inline"})

    @router.get("/still-to-come")
    def still_to_come_page(request: Request):
        with db.read() as conn:
            return render(request, "still_to_come.html", {"b": outlook.still_to_come_breakdown(conn)})

    @router.get("/help")
    def help_page(request: Request):
        from . import digest
        return render(request, "help.html", {"telegram": bool(digest.load_config(settings))})

    @router.post("/transfers/{tid}/arrived")
    async def transfer_arrived(request: Request, tid: int):
        def fn(conn, f):
            services.mark_arrived(conn, tid)
            if f.get("next"):
                target = safe_next(f["next"])
                return RedirectResponse(target + ("&" if "?" in target else "?") + urlencode({"ok": "Marked as arrived"}),
                                        status_code=303)
            return back("/accounts", ok="Marked as arrived")
        return await write(request, fn, "", "/accounts")

    # ------------------------------------------------------------ search

    @router.get("/search")
    def search(request: Request):
        q = request.query_params.get("q", "").strip()[:80]
        limit = 25
        groups = []
        if q:
            like = "%" + q.replace("\\", "").replace("%", "").replace("_", "") + "%"
            with db.read() as conn:
                pos = [dict(r) for r in conn.execute(
                    "SELECT DISTINCT p.id, p.event, p.selection, p.market, p.status, p.event_at FROM positions p "
                    "LEFT JOIN legs l ON l.position_id = p.id AND l.deleted_at IS NULL WHERE p.deleted_at IS NULL AND "
                    "(p.event LIKE :q OR p.selection LIKE :q OR p.market LIKE :q OR p.notes LIKE :q OR l.external_ref LIKE :q) "
                    f"ORDER BY coalesce(p.event_at, p.created_at) DESC LIMIT {limit}", {"q": like})]
                items = []
                for p in pos:
                    pnl = services.position_pnl(conn, p["id"]) if p["status"] != "open" else None
                    items.append({"href": f"/positions/{p['id']}", "title": p["event"],
                                  "text": " · ".join(x for x in (p["selection"], p["market"], p["status"].capitalize(),
                                                                 times.local(p["event_at"]) if p["event_at"] else "") if x),
                                  "amount": pnl, "signed": True, "small": "result"})
                groups.append({"label": "Positions", "items": items})
                groups.append({"label": "Offers", "items": [
                    {"href": f"/offers/{o['id']}", "title": o["title"],
                     "text": " · ".join(x for x in (o["account"], OFFER_STATUS_LABELS.get(o["status"], o["status"]),
                                                    o["requirement"]) if x),
                     "amount": None} for o in conn.execute(
                        "SELECT o.*, a.name AS account FROM offers o LEFT JOIN accounts a ON a.id = o.account_id "
                        "WHERE o.deleted_at IS NULL AND (o.title LIKE :q OR o.requirement LIKE :q OR o.reward LIKE :q "
                        f"OR o.notes LIKE :q OR a.name LIKE :q) ORDER BY o.id DESC LIMIT {limit}", {"q": like})]})
                groups.append({"label": "Accounts", "items": [
                    {"href": f"/accounts/{a['id']}", "title": a["name"], "text": a["kind"].capitalize(),
                     "amount": services.balance(conn, a["id"]) if a["kind"] != "bank" else None, "signed": False,
                     "small": "balance"} for a in conn.execute(
                        f"SELECT * FROM accounts WHERE name LIKE :q OR notes LIKE :q ORDER BY name LIMIT {limit}", {"q": like})]})
                groups.append({"label": "Free bets", "items": [
                    {"href": "/free-bets?show=all", "title": f"{f['account']} {fmt(f['face_value_pence'])} {f['kind'].upper()}",
                     "text": " · ".join(x for x in (f["status"].capitalize(), f["offer_title"],
                                                    ("expires " + times.local(f["expires_at"])) if f["expires_at"] else "",
                                                    f["notes"]) if x),
                     "amount": None} for f in conn.execute(
                        "SELECT f.*, a.name AS account, o.title AS offer_title FROM free_bets f JOIN accounts a ON a.id = f.account_id "
                        "LEFT JOIN offers o ON o.id = f.offer_id WHERE f.deleted_at IS NULL AND (a.name LIKE :q OR f.notes LIKE :q "
                        f"OR o.title LIKE :q) ORDER BY f.id DESC LIMIT {limit}", {"q": like})]})
                groups.append({"label": "Ledger", "items": [
                    {"href": (f"/positions/{e['position_id']}" if e["position_id"] else f"/ledger?account={e['account_id']}"),
                     "title": e["description"] or CATEGORY_LABELS.get(e["category"], e["category"]),
                     "text": f"{e['account']} · {CATEGORY_LABELS.get(e['category'], e['category'])} · {times.local(e['at'])}",
                     "amount": e["amount_pence"], "signed": True, "small": ""} for e in conn.execute(
                        "SELECT e.*, a.name AS account FROM ledger_entries e JOIN accounts a ON a.id = e.account_id "
                        "WHERE e.superseded_at IS NULL AND e.description LIKE :q "
                        f"ORDER BY e.at DESC LIMIT {limit}", {"q": like})]})
        return render(request, "search.html", {"q": q, "groups": groups, "limit": limit,
                                               "total": sum(len(g["items"]) for g in groups)})

    # ------------------------------------------------------------ issues and the balance check routine

    @router.get("/issues")
    def issues_page(request: Request):
        with db.read() as conn:
            s = metrics.summary(conn)
            found = annotate_repairs(conn, issues.collect(conn, s))
            reviewed = [i for i in issues.collect(conn, s, include_dismissed=True) if i.get("dismissed")]
            return render(request, "issues.html", {"issues": found, "n": issues.counts(found), "s": s,
                                                   "LABELS": issues.SEVERITY_LABELS, "reviewed": reviewed})

    # ------------------------------------------------------------ ledger

    @router.get("/ledger")
    def ledger(request: Request):
        p = request.query_params
        where, params = ["1 = 1"], []
        if not p.get("superseded"):
            where.append("e.superseded_at IS NULL")
        for key, col in (("account", "e.account_id"), ("position", "e.position_id"), ("offer", "e.offer_id")):
            if opt_int(p.get(key)):
                where.append(f"{col} = ?")
                params.append(int(p[key]))
        if p.get("category") in CATEGORY_LABELS:
            where.append("e.category = ?")
            params.append(p["category"])
        if p.get("class") in ("capital", "stake", "pnl"):
            where.append("e.class = ?")
            params.append(p["class"])
        for key, op in (("from", ">="), ("to", "<=")):
            if p.get(key):
                try:
                    params.append(times.from_local_input(p[key][:10] + ("T00:00" if key == "from" else "T23:59")))
                    where.append(f"e.at {op} ?")
                except ValueError:
                    pass
        for key, op in (("min", ">="), ("max", "<=")):
            if p.get(key):
                try:
                    params.append(to_pence(p[key], allow_negative=True))
                    where.append(f"abs(e.amount_pence) {op} abs(?)")
                except ValueError:
                    pass
        if p.get("q"):
            where.append("e.description LIKE ?")
            params.append("%" + p["q"][:80] + "%")
        with db.read() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT e.*, a.name AS account, a.kind AS account_kind FROM ledger_entries e JOIN accounts a "
                "ON a.id = e.account_id WHERE " + " AND ".join(where) + " ORDER BY e.at, e.id LIMIT 2000", params)]
            if opt_int(p.get("account")) and not p.get("superseded"):
                running = 0
                for r in rows:
                    running += r["amount_pence"]
                    r["running"] = running
            total = sum(r["amount_pence"] for r in rows if not r["superseded_at"])
            return render(request, "ledger.html", {"rows": list(reversed(rows)), "p": dict(p), "total": total,
                                                   "accounts": accounts(conn, active_only=False),
                                                   "betting_accounts": accounts(conn, ("bookmaker", "exchange")),
                                                   "offers": offers_open(conn),
                                                   "manual": services.MANUAL_CATEGORIES})

    @router.post("/ledger")
    async def manual_entry(request: Request):
        def fn(conn, f):
            amount = money_or_error(f.get("amount", ""), "Amount", allow_negative=True)
            eid = services.add_manual_entry(conn, account_id=opt_int(f.get("account_id")) or 0, amount=amount,
                                            category=f.get("category", ""), at=times.from_local_input(f.get("at")),
                                            description=f.get("description", ""), offer_id=opt_int(f.get("offer_id")),
                                            position_id=opt_int(f.get("position_id")))
            return back("/ledger", ok="Entry recorded", undo_token=f"entry:{eid}")
        return await write(request, fn, "", "/ledger")

    @router.post("/ledger/{eid}/reverse")
    async def reverse(request: Request, eid: int):
        return await write(request, lambda conn, f: services.reverse_entry(conn, eid, f.get("note", "")),
                           "Entry reversed (kept in the history)", "/ledger")

    # ------------------------------------------------------------ imports (Shortcut / OCR / CSV / assistant)

    @router.post("/api/import")
    async def api_import(request: Request):
        try:
            raw = json.loads((await request.body()) or b"{}")
        except ValueError:
            return JSONResponse({"error": "Send JSON"}, status_code=400)
        source = raw.pop("source", "api") if isinstance(raw, dict) else "api"
        source = "api" if source == "assistant" else source          # that label is the assistant token's
        try:
            with db.tx() as conn:
                iid, candidate, warnings = imports.store(conn, raw, source)
                pv = imports.preview(conn, candidate)
        except imports.CandidateError as exc:
            return JSONResponse({"error": "Not valid", "problems": exc.errors}, status_code=400)
        return JSONResponse({"id": iid, "status": "needs_confirmation", "warnings": warnings,
                             "confirm_url": f"{BASE}/imports/{iid}",
                             "preview": {"expected": plain(pv["expected"]) if pv["expected"] is not None else None,
                                         "legs": [{"account": l["account"], "side": l["side"], "stake": plain(l["stake_pence"]),
                                                   "odds": l["odds"]} for l in pv["legs"]]}}, status_code=201)

    SOURCE_LABELS = {"assistant": "Assistant (ChatGPT)", "api": "API", "shortcut": "iPhone Shortcut", "csv": "CSV",
                     "ocr": "Screenshot", "paste": "Pasted text", "manual": "Manual", "quick_add": "Quick Add"}

    def imports_context(conn, batch: dict | None = None) -> dict:
        rows = [dict(r) for r in conn.execute("SELECT * FROM import_candidates ORDER BY status <> "
                                              "'needs_confirmation', id DESC LIMIT 200")]
        for r in rows:
            r["candidate"] = json.loads(r["candidate_json"])
            r["title"] = imports.title(r["kind"], r["candidate"])
            r["issue"] = imports.check(conn, r) if r["status"] == "needs_confirmation" else None
        return {"rows": rows, "KIND_TITLES": imports.KIND_TITLES, "SOURCE_LABELS": SOURCE_LABELS, "batch": batch,
                "pending": sum(1 for r in rows if r["status"] == "needs_confirmation"),
                "trusted": imports.auto_confirm_settings(conn)}

    @router.get("/imports")
    def imports_page(request: Request):
        with db.read() as conn:
            return render(request, "imports.html", {**imports_context(conn), "held_changes": ops.held(conn)})

    @router.post("/imports/bulk")
    async def imports_bulk(request: Request):
        """Confirm selected (all or nothing) or Confirm all valid (the rest stay pending), in dependency order."""
        form = await request.form()
        action = form.get("action")
        if action not in ("selected", "all_valid"):
            return back("/imports", err="Choose Confirm selected or Confirm all valid")
        with db.read() as conn:
            if action == "selected":
                ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
            else:
                ids = [r[0] for r in conn.execute("SELECT id FROM import_candidates WHERE status = 'needs_confirmation'")]
        if not ids:
            return back("/imports", err="Nothing selected")
        batch = {"mode": action, "rolled_back": False}
        try:
            with db.tx() as conn:
                batch["results"] = imports.confirm_many(conn, ids, all_or_nothing=action == "selected")
        except imports.BatchError as exc:
            batch["results"], batch["rolled_back"] = exc.results, True
        with db.read() as conn:
            return render(request, "imports.html", imports_context(conn, batch))

    def kind_preview(conn, kind: str, c: dict) -> list[tuple[str, str]]:
        """What confirming will do, in the app's terms (signed-in page only, so balances may be shown)."""
        if kind == "account":
            rows = [("Name", c["name"]), ("Type", c["account_type"].capitalize())]
            if c["account_type"] == "exchange":
                rows.append(("Commission", commission_text(c["commission_bp"])))
            if c["account_type"] != "bank":
                rows.append(("Opening/current balance", fmt(c["balance_pence"]) + " (your capital, not profit)"))
            return rows
        if kind == "balance":
            app_balance = services.balance(conn, c["account_id"])
            diff = c["balance_pence"] - app_balance
            return [("Account", c["account"]), ("Reported balance", fmt(c["balance_pence"])),
                    ("Balance in the app now", fmt(app_balance)),
                    ("On confirmation", "No change — it matches" if not diff else
                     f"Adds a reconciliation entry of {fmt(diff, signed=True)} (visible in the ledger)")]
        if kind == "transfer":
            rows = [("From → to", f"{c['from']} → {c['to']}"), ("Amount", fmt(c["amount_pence"])),
                    ("Type", CAPITAL_LABELS.get(c["capital_type"], c["capital_type"]) + " — never profit or loss")]
            if c.get("due_at"):
                rows.append(("Return by", times.local(c["due_at"])))
            if c["capital_type"] == "temporary_return":
                temp = min(c["amount_pence"], services.outstanding_temporary(conn, c["to_id"]))
                rows.append(("Split", f"{fmt(temp)} repays temporary funding; "
                                      f"{fmt(c['amount_pence'] - temp)} is a bankroll withdrawal"))
            if c["at"]:
                rows.append(("When", times.local(c["at"])))
            return rows
        if kind == "offer":
            t = c.get("terms") or {}
            rows = [("Title", c["title"]), ("Bookmaker", c["bookmaker"] or "—"),
                    ("Type", OFFER_TYPE_LABELS.get(c["offer_type"], c["offer_type"])),
                    ("Requirement", c["requirement"] or "—"), ("Reward", c["reward"] or services.reward_text(t) or "—"),
                    ("Reward value", fmt(c["reward_value_pence"] or (t.get("reward_count") or 1) * (t.get("reward_each_pence") or 0))),
                    ("Expires", times.local(c["expires_at"]) if c["expires_at"] else "—")]
            if t.get("qualifying_stake_pence") or t.get("min_odds"):
                rows.append(("Qualifying bet", " at ".join(x for x in (
                    fmt(t["qualifying_stake_pence"]) if t.get("qualifying_stake_pence") else "",
                    f"odds {t['min_odds']}+" if t.get("min_odds") else "") if x)))
            if t.get("reward_expiry_days"):
                rows.append(("Free bets expire", f"{t['reward_expiry_days']} days after they arrive"))
            return rows
        if kind == "free_bet":
            return [("Bookmaker", c["bookmaker"]), ("Value", fmt(c["face_value_pence"])
                     + (f" each × {c['count']}" if (c.get("count") or 1) > 1 else "")),
                    ("Type", "Stake returned (SR)" if c["kind"] == "sr" else "Stake not returned (SNR)"),
                    ("Expires", times.local(c["expires_at"]) if c["expires_at"] else "—"), ("Offer", c["offer"] or "—")]
        if kind == "reward":
            what = (f"{fmt(c['each_pence'])} cash reward (completes the offer)" if c["reward_kind"] == "cash" else
                    f"{c['count']} × {fmt(c['each_pence'])} {c['reward_kind'].upper()} free bets linked to the offer")
            return [("Offer", c["offer"]), ("Creates", what),
                    ("Expires", times.local(c["expires_at"]) if c["expires_at"] else "—"),
                    ("Arrived", times.local(c["received_at"]) if c["received_at"] else "now")]
        if kind == "settlement":
            rows = [("Position", (f"#{c['position_id']} " if c["position_id"] else "") + c["event"]
                     + (f" — {c['selection']}" if c["selection"] else "")
                     + ("" if c["position_id"] else " (still pending: it is confirmed first)")),
                    ("Settled", times.local(c["settled_at"]) if c["settled_at"] else "—"),
                    ("Result", imports.OUTCOMES.get(c["outcome"]) or ", ".join(
                        f"{s} {c[s + '_result']}" for s in ("back", "lay") if c.get(s + "_result")))]
            for p in c["legs"]:
                rows.append((f"{p['side'].capitalize()} · {p['account']}", f"{p['result']}: {fmt(p['amount_pence'], signed=True)}"
                             + (f", commission {fmt(p['commission_pence'])}" if p["commission_pence"] else "")))
            if c["net_pence"] is not None:
                rows.append(("Net result for the position", fmt(c["net_pence"], signed=True)))
            if c["offer_change"]:
                rows.append(("Offer", c["offer_change"]))
            rows.append(("Undo", "Each bet can be reopened on the position page afterwards"))
            return rows
        if kind in ("amend", "reverse"):
            from . import changes
            return changes.preview_rows(kind, c)
        if kind == "note":
            table = {"account": "accounts", "offer": "offers", "position": "positions"}[c["target"]]
            target = services.row(conn, table, c["target_id"]) or {}
            name = target.get("name") or target.get("title") or target.get("event") or f"#{c['target_id']}"
            return [("Adds a note to", f"{c['target']} {name}"), ("Note", c["text"])]
        return []

    def edit_fields(kind: str, c: dict) -> tuple[list[dict], list[dict], list[dict]]:
        """Fields (with current values) for the Edit form; for positions also leg rows (+1 empty)."""
        spec = assistant.TYPES[kind]
        values = imports.to_raw(kind, c)
        fields = [{**x, "value": values.get(x["name"], "")} for x in spec["fields"] if x["type"] != "array"]
        legs = []
        if kind == "position":
            rows = values["legs"] + [{}]
            legs = [{"i": i, "values": row} for i, row in enumerate(rows)]
        return fields, spec.get("leg_fields", []), legs

    def item_of(conn, ref: str) -> dict:
        item = imports.find(conn, ref)
        if not item:
            raise StarletteHTTPException(404)
        return item

    @router.get("/imports/{ref}")
    def import_detail(request: Request, ref: str):
        with db.read() as conn:
            item = item_of(conn, ref)
            candidate = json.loads(item["candidate_json"])
            kind = item["kind"]
            fields, leg_fields, legs = edit_fields(kind, candidate)
            return render(request, "import.html", {
                "item": item, "c": candidate, "kind": kind, "title": imports.title(kind, candidate),
                "kind_title": imports.KIND_TITLES[kind], "source_label": SOURCE_LABELS.get(item["source"], item["source"]),
                "pv": imports.preview(conn, candidate) if kind == "position" else None,
                "rows": kind_preview(conn, kind, candidate) if kind != "position" else [],
                "warnings": json.loads(item["warnings_json"]), "fields": fields, "leg_fields": leg_fields,
                "legs": legs, "applied_url": applied_url(item)})

    def applied_url(item: dict) -> str | None:
        ref = item.get("applied_ref") or (f"position:{item['applied_position_id']}" if item.get("applied_position_id") else None)
        if not ref:
            return None
        entity, _, num = ref.partition(":")
        return {"position": f"/positions/{num}", "account": f"/accounts/{num}", "offer": f"/offers/{num}",
                "transfer": "/ledger?category=transfer", "free_bet": "/free-bets"}.get(entity)

    @router.post("/imports/{iid}")
    async def import_decide(request: Request, iid: int):
        def fn(conn, f):
            if f.get("decision") not in ("accept", "reject"):
                raise UserError("Choose confirm or reject")
            imports.decide(conn, iid, f.get("decision") == "accept")
            item = services.row(conn, "import_candidates", iid)
            if item["status"] == "rejected":
                return back("/imports", ok="Import rejected — nothing was saved")
            return back(applied_url(item) or "/imports", ok="Import confirmed and saved")
        return await write(request, fn, "", f"/imports/{iid}")

    @router.post("/imports/{iid}/edit")
    async def import_edit(request: Request, iid: int):
        def fn(conn, f):
            item = services.row(conn, "import_candidates", iid)
            if not item:
                raise UserError("Import not found")
            spec = assistant.TYPES[item["kind"]]
            raw = {x["name"]: f.get(f"f_{x['name']}", "").strip() for x in spec["fields"] if x["type"] != "array"}
            raw = {k: v for k, v in raw.items() if v != ""}
            if item["kind"] == "position":
                legs = []
                for i in range(13):
                    leg = {x["name"]: f.get(f"leg{i}_{x['name']}", "").strip() for x in spec["leg_fields"]}
                    leg["free_bet_id"] = f.get(f"leg{i}_free_bet_id", "").strip()
                    leg = {k: v for k, v in leg.items() if v != ""}
                    leg.pop("use_free_bet", None)
                    if leg.get("account") or leg.get("stake") or leg.get("odds"):
                        legs.append(leg)
                raw["legs"] = legs
            imports.edit(conn, iid, raw)
        return await write(request, fn, "Changes saved — check them, then confirm", f"/imports/{iid}")

    # ------------------------------------------------------------ maintenance changes (bets/ops.py) from the web app

    def _params(items) -> dict:
        return {k: v for k, v in items if k not in ("csrf_token", "next") and isinstance(v, str)}

    def _next(value: str | None, default: str = "/") -> str:
        target = safe_next(BASE + value if value and value.startswith("/") and not value.startswith(BASE) else value)
        return target[len(BASE):] or "/"

    @router.get("/ops/{name}")
    def op_preview(request: Request, name: str):
        if name not in ops.OPS:
            raise StarletteHTTPException(404)
        params = _params(request.query_params.items())
        back_to = _next(request.query_params.get("next"))
        try:
            with db.tx() as conn:
                result = ops.preview(conn, ops.plan_for(conn, name, params, require_reason=False))
        except ValueError as exc:
            return back(back_to, err=str(exc))
        return render(request, "change.html", {"mode": "preview", "r": result, "name": name, "params": params,
                                               "title": ops.TITLES[name], "back_to": back_to})

    @router.post("/ops/{name}")
    async def op_apply(request: Request, name: str):
        if name not in ops.OPS:
            raise StarletteHTTPException(404)
        f = await form_of(request)
        back_to = _next(f.get("next"))
        try:
            with db.tx() as conn:
                result = ops.run(conn, name, _params(f.items()), actor="user")
        except ValueError as exc:
            return back(back_to, err=str(exc))
        if result["status"] == "blocked":
            return back(back_to, err=result["reason"])
        return back(back_to, ok=("Already done: " if result["status"] == "already_done" else "Done: ") + result["summary"])

    @router.get("/changes/{public_id}")
    def held_change(request: Request, public_id: str):
        with db.read() as conn:
            r = conn.execute("SELECT * FROM assistant_ops WHERE public_id = ?", (public_id[:40],)).fetchone()
        if not r:
            raise StarletteHTTPException(404)
        op = dict(r)
        result = json.loads(op["result_json"]) if op["result_json"] else {}
        result.setdefault("level", op["level"])
        result.setdefault("summary", op["summary"])
        return render(request, "change.html", {"mode": "held", "r": result, "op": op, "name": op["op"],
                                               "title": ops.TITLES.get(op["op"], op["op"]), "back_to": "/imports"})

    @router.post("/changes/{public_id}/{decision}")
    async def held_change_decide(request: Request, public_id: str, decision: str):
        if decision not in ("confirm", "reject"):
            raise StarletteHTTPException(404)
        try:
            with db.tx() as conn:
                result = ops.decide(conn, public_id[:40], decision == "confirm")
        except ValueError as exc:
            return back(f"/changes/{public_id}", err=str(exc))
        if result["status"] == "rejected":
            return back("/imports", ok="Change rejected: nothing was changed")
        if result["status"] in ("blocked", "already_done"):
            return back("/imports", err=f"Not applied ({result['status'].replace('_', ' ')}): "
                                       + (result.get("reason") or "the records are already in that state"))
        return back("/imports", ok="Confirmed: " + result["summary"])

    def annotate_repairs(conn, found: list) -> list:
        for i in found:
            for r in i.get("repairs", []):
                try:
                    plan = ops.plan_for(conn, r["change"], r["params"])
                    r["level"], r["blocked"] = plan.level, plan.blocked
                except ValueError as exc:
                    r["level"], r["blocked"] = "safe_edit", str(exc)
                r["query"] = urlencode({**r["params"], "next": "/issues"})
        return found

    @router.post("/settings/assistant/permissions")
    async def assistant_permissions(request: Request):
        def fn(conn, f):
            ops.save_permissions(conn, f.get("read") == "on", f.get("safe_edit", "ask"), f.get("financial_reverse", "ask"),
                                 f.get("planning", "auto"))
        return await write(request, fn, "Assistant permissions saved", "/settings#permissions")

    # ------------------------------------------------------------ Assistant Intake API (bearer token, candidates only)

    ASSISTANT_URL = f"{ORIGIN}{BASE}/api/assistant"

    async def json_object(request: Request) -> dict | None:
        try:
            data = json.loads(await request.body() or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def rejected(kind: str, errors: list[str], status: int = 400, warnings=()) -> JSONResponse:
        return JSONResponse({"status": "rejected", "type": kind, "errors": errors, "warnings": list(warnings)},
                            status_code=status)

    @router.get("/api/assistant/schema")
    def assistant_schema():
        return assistant.schema_doc(ASSISTANT_URL)

    @router.get("/api/assistant/openapi.json")
    def assistant_openapi():
        return assistant.openapi(ASSISTANT_URL)

    @router.post("/api/assistant/calculate")
    async def assistant_calculate(request: Request):
        data = await json_object(request)
        if data is None:
            return rejected("calculation", ["Send a JSON object"])
        kind = data.get("position_type") or "qualifying"
        try:
            commission = data.get("commission")
            if commission in (None, "") and data.get("exchange"):
                with db.read() as conn:
                    acc = imports._account(conn, data["exchange"])
                if not acc or acc["kind"] != "exchange":
                    return rejected("calculation", [f"exchange: no exchange called '{str(data['exchange'])[:60]}'"])
                bp = acc["commission_bp"]
                warning = comm.stale_warning(acc, None)
            else:
                bp, warning = parse_commission(commission), ""
            m = calc.match(kind, to_pence(str(data.get("back_stake", "")), allow_zero=False),
                           parse_odds(data.get("back_odds")), parse_odds(data.get("lay_odds")), bp)
        except ValueError as exc:
            return rejected("calculation", [str(exc)])
        return {"position_type": kind, "commission": commission_text(bp), "lay_stake": plain(m.lay_stake),
                "liability": plain(m.liability), "if_back_wins": plain(m.if_back_wins),
                "if_lay_wins": plain(m.if_lay_wins), "expected_result": plain(m.expected),
                "retention_percent": str(m.retention_pct) if m.retention_pct is not None else None,
                "commission_warning": warning or None, "note": "Calculated by the app. Nothing was stored."}

    @router.post("/api/assistant/candidates/{kind}")
    async def assistant_submit(request: Request, kind: str):
        if kind not in imports.KINDS:
            return JSONResponse({"status": "rejected", "errors": [f"Unknown type; use one of {', '.join(imports.KINDS)}"]},
                                status_code=404)
        data = await json_object(request)
        if data is None:
            return rejected(kind, ["Send a JSON object with the fields for this type"])
        dry_run = data.pop("dry_run", False) is True
        key = data.pop("idempotency_key", None)
        if key is not None and not (isinstance(key, str) and 1 <= len(key) <= 100):
            return rejected(kind, ["idempotency_key: a string of up to 100 characters"])
        ignored = [f"ignored unknown field: {x}" for x in assistant.unknown_fields(kind, data)]
        token_id = request.state.assistant
        try:
            with db.tx() as conn:
                if dry_run:
                    candidate, warnings = imports.normalise_kind(conn, kind, data, "assistant", allow_pending=True)
                    return {"status": "valid", "dry_run": True, "type": kind,
                            "summary": imports.summary(kind, candidate), "warnings": warnings + ignored,
                            "note": "Checked only; nothing was stored."}
                if key:
                    old = conn.execute("SELECT * FROM import_candidates WHERE source = 'assistant' AND idempotency_key = ?",
                                       (key,)).fetchone()
                    if old:
                        if json.loads(old["raw_json"]) != data or old["kind"] != kind:
                            return rejected(kind, ["idempotency_key was already used for a different submission"], 409)
                        return {**assistant.candidate_response(dict(old), ORIGIN, BASE), "duplicate": True}
                pending = conn.execute("SELECT count(*) FROM import_candidates WHERE source = 'assistant' "
                                       "AND status = 'needs_confirmation'").fetchone()[0]
                if pending >= assistant.MAX_PENDING:
                    return rejected(kind, ["Too many candidates are waiting for confirmation; ask the user to "
                                           "review them in the app first"], 429)
                if not quota.take(token_id):
                    return rejected(kind, ["Too many submissions this hour; try again later"], 429)
                iid, candidate, warnings = imports.store(conn, data, "assistant", kind=kind, idempotency_key=key,
                                                         submitted_by=f"assistant:{token_id}")
                if ignored:
                    conn.execute("UPDATE import_candidates SET warnings_json = ? WHERE id = ?",
                                 (json.dumps(warnings + ignored), iid))
                trusted = imports.auto_confirm_settings(conn)
                if trusted["enabled"] and kind in trusted["kinds"]:
                    auto_confirm(conn, iid, kind, candidate, warnings + ignored, token_id)
                item = services.row(conn, "import_candidates", iid)
        except (imports.CandidateError, UserError) as exc:
            errors = exc.errors if isinstance(exc, imports.CandidateError) else [str(exc)]
            return rejected(kind, errors, warnings=ignored)
        return JSONResponse(assistant.candidate_response(item, ORIGIN, BASE), status_code=201)

    def auto_confirm(conn, iid: int, kind: str, candidate: dict, warnings: list[str], token_id: str) -> None:
        """Trusted Assistant mode: confirm a complete, unremarkable assistant submission straight away (same
        validation and services as Confirm); anything else stays pending with the reason recorded."""
        reason = imports.auto_confirm_hold(conn, kind, candidate, warnings)
        if reason is None:
            conn.execute("SAVEPOINT auto_confirm")
            try:
                imports.decide(conn, iid, True, decided_by="assistant-auto")
                conn.execute("RELEASE SAVEPOINT auto_confirm")
                services.audit(conn, "import", iid, "auto_confirmed", None,
                               {"kind": kind, "submitted_by": f"assistant:{token_id}"},
                               "Trusted Assistant mode: confirmed automatically for the Matched Betting Assistant")
                return
            except (ValueError, sqlite3.IntegrityError) as exc:
                conn.execute("ROLLBACK TO SAVEPOINT auto_confirm")
                conn.execute("RELEASE SAVEPOINT auto_confirm")
                reason = "could not be confirmed: " + (str(exc)[:200] or "validation failed")
        conn.execute("UPDATE import_candidates SET hold_reason = ? WHERE id = ?", (reason[:500], iid))
        services.audit(conn, "import", iid, "held", None, {"reason": reason[:500]}, "Trusted Assistant mode")

    # ------------------------------------------------------------ Assistant API: read records, maintenance changes

    READERS = {
        "accounts": lambda c, q: readapi.list_accounts(c, q.get("type") or None, q.get("include_inactive") == "true"),
        "account": lambda c, q: readapi.account(c, readapi.find(c, "account", q.get("ref") or q.get("name") or q.get("id"))),
        "account_balance_breakdown": lambda c, q: readapi.account(c, readapi.find(c, "account", q.get("ref") or q.get("account")))
        ["balances"],
        "exchange_exposure": lambda c, q: readapi.exchange_exposure(c, readapi.find(c, "account", q.get("ref") or q.get("account"))),
        "positions": lambda c, q: readapi.list_positions(c, status=q.get("status"), event=q.get("event"),
                                                         account=q.get("account"), limit=_lim(q)),
        "position": lambda c, q: readapi.position(c, readapi.find(c, "position", q.get("ref") or q.get("id"))),
        "free_bets": lambda c, q: readapi.list_free_bets(c, status=q.get("status"), account=q.get("account"), limit=_lim(q)),
        "free_bet": lambda c, q: readapi.free_bet(c, readapi.find(c, "free_bet", q.get("id"))),
        "transfers": lambda c, q: readapi.list_transfers(c, account=q.get("account"), funding_type=q.get("funding_type"),
                                                         include_reversed=q.get("include_reversed") == "true", limit=_lim(q)),
        "transfer": lambda c, q: readapi.transfer(c, readapi.find(c, "transfer", q.get("id"))),
        "reconciliations": lambda c, q: readapi.list_reconciliations(c, account=q.get("account"), limit=_lim(q)),
        "reconciliation": lambda c, q: readapi.reconciliation(c, readapi.find(c, "reconciliation", q.get("id"))),
        "offers": lambda c, q: readapi.list_offers(c, status=q.get("status"), limit=_lim(q)),
        "offer": lambda c, q: readapi.offer(c, readapi.find(c, "offer", q.get("id"))),
        "ledger": lambda c, q: readapi.ledger_entries(c, account=q.get("account"), position_id=q.get("position_id") or None,
                                                      category=q.get("category"),
                                                      include_superseded=q.get("include_superseded") == "true",
                                                      limit=_lim(q)),
        "dashboard": lambda c, q: readapi.dashboard_totals(c),
        "profit": lambda c, q: readapi.profit_breakdown(c),
        "still_to_come": lambda c, q: readapi.still_to_come_breakdown(c),
        "issues": lambda c, q: _issues_for_assistant(c),
        "history": lambda c, q: readapi.history(c, q.get("entity", ""), readapi.find(c, q.get("entity", ""), q.get("id")),
                                                limit=_lim(q, 30)),
        "operating_brief": lambda c, q: brief.operating_brief(c),
        "next_actions": lambda c, q: brief.next_actions(c),
        "bet_plans": lambda c, q: brief.list_plans(c, q.get("status") or None, _lim(q)),
        "bet_plan": lambda c, q: brief.plan(c, int(q.get("id", "0") or 0)),
        "funding_requirement": lambda c, q: brief.plan(c, int(q.get("id", "0") or 0))["funding"],
        "position_outcomes": lambda c, q: brief.position_outcomes(c, readapi.find(c, "position", q.get("ref") or q.get("id"))),
        "expected_rewards": lambda c, q: brief.expected_rewards(c, q.get("status") or None),
        "expected_reward": lambda c, q: brief.expected_reward(c, int(q.get("id", "0") or 0)),
        "evidence": lambda c, q: evidence.for_record(c, q.get("entity", ""), int(q.get("id", "0") or 0)),
        "retention_policy": lambda c, q: brief.retention_policy(c),
        "dismissed_issues": lambda c, q: issues.dismissed(c),
    }

    def _lim(q, default: int = 50) -> int:
        v = q.get("limit", "")
        return int(v) if v.isdigit() else default

    def _issues_for_assistant(conn) -> dict:
        found = issues.collect(conn)
        for i in found:
            for r in i["repairs"]:
                try:
                    plan = ops.plan_for(conn, r["change"], r["params"])
                    r["level"] = plan.level
                    r["blocked"] = plan.blocked
                except ValueError as exc:
                    r["blocked"] = str(exc)
            for k in ("settle", "position_id", "when"):
                i.pop(k, None)
        return {"count": len(found), "by_severity": issues.counts(found), "issues": found,
                "note": "Apply a repair with the change tool of the same name and its params (give a new request_id)."}

    @router.get("/api/assistant/read/{resource}")
    def assistant_read(request: Request, resource: str):
        if resource not in READERS:
            return JSONResponse({"error": f"Unknown resource; use one of {', '.join(READERS)}"}, status_code=404)
        q = {k: v[:200] for k, v in request.query_params.items()}
        with db.read() as conn:
            if not ops.permissions(conn)["read"]:
                return JSONResponse({"error": "reading_disabled", "message": "The user has switched off reading records "
                                     "for the assistant (Settings → Assistant permissions)."}, status_code=403)
            try:
                data = READERS[resource](conn, q)
            except (ValueError, KeyError) as exc:
                return JSONResponse({"error": "not_found", "message": str(exc)[:300]}, status_code=404)
        return {"resource": resource, "data": data, "money": "GBP", "times": "UK local"}

    @router.post("/api/assistant/hedge")
    async def assistant_hedge(request: Request):
        """Stateless hedge calculation (nothing stored): lay stakes for given bets and outcomes."""
        data = await json_object(request)
        if data is None:
            return rejected("hedge", ["Send a JSON object"])
        with db.read() as conn:
            if not ops.permissions(conn)["read"]:
                return JSONResponse({"error": "reading_disabled"}, status_code=403)
            try:
                return {"data": brief.hedge_calculation(conn, data)}
            except (ValueError, KeyError, TypeError) as exc:
                return rejected("hedge", [str(exc)[:300]])

    @router.post("/api/assistant/changes/{name}")
    async def assistant_change(request: Request, name: str):
        data = await json_object(request)
        if data is None:
            return rejected("change", ["Send a JSON object"])
        dry_run = data.pop("dry_run", False) is True
        request_id = data.pop("request_id", None)
        token_id = request.state.assistant
        with db.tx() as conn:
            if not dry_run and not conn.execute("SELECT 1 FROM assistant_ops WHERE request_key = ?",
                                                (f"{token_id}:{request_id}",)).fetchone() and not quota.take(token_id):
                return rejected("change", ["Too many changes this hour; try again later"], 429)
            code, body = ops.assistant_request(conn, name, data, token_id=token_id, request_id=request_id, dry_run=dry_run)
        if body.get("change_id") and body.get("status") == "needs_confirmation":
            body["confirmation_url"] = f"{ORIGIN}{BASE}/changes/{body['change_id']}"
        return JSONResponse(body, status_code=code)

    @router.get("/api/assistant/changes/{change_id}")
    def assistant_change_status(change_id: str):
        with db.read() as conn:
            r = conn.execute("SELECT * FROM assistant_ops WHERE public_id = ?", (change_id[:40],)).fetchone()
        if not r:
            return JSONResponse({"error": "No such change"}, status_code=404)
        return ops.stored_response(dict(r))

    @router.get("/api/assistant/candidates/{candidate_id}")
    def assistant_candidate(candidate_id: str):
        with db.read() as conn:
            r = conn.execute("SELECT * FROM import_candidates WHERE public_id = ? AND source = 'assistant'",
                             (candidate_id[:40],)).fetchone()
        if not r:
            return JSONResponse({"error": "No such candidate"}, status_code=404)
        return assistant.candidate_response(dict(r), ORIGIN, BASE)

    # ------------------------------------------------------------ exports and calendar

    def download(body: str, filename: str, media: str) -> Response:
        return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @router.get("/export/ledger.csv")
    def export_ledger():
        with db.read() as conn:
            return download(exports.ledger_csv(conn), f"bets-ledger-{times.now():%Y%m%d}.csv", "text/csv")

    @router.get("/export/positions.csv")
    def export_positions():
        with db.read() as conn:
            return download(exports.positions_csv(conn), f"bets-positions-{times.now():%Y%m%d}.csv", "text/csv")

    @router.get("/export/full.json")
    def export_json():
        with db.read() as conn:
            return download(exports.full_json(conn), f"bets-export-{times.now():%Y%m%d}.json", "application/json")

    @router.get("/calendar.ics")
    def calendar_all():
        with db.read() as conn:
            return download(exports.ics(exports.deadline_events(conn)), "bets-deadlines.ics", "text/calendar")

    @router.get("/calendar/{kind}/{item_id}.ics")
    def calendar_one(kind: str, item_id: int):
        if kind not in ("free_bet", "offer", "position", "return"):  # noqa: the page is /calendar
            raise StarletteHTTPException(404)
        with db.read() as conn:
            events = exports.deadline_events(conn, kind, item_id)
        if not events:
            raise StarletteHTTPException(404)
        return download(exports.ics(events), f"{kind}-{item_id}.ics", "text/calendar")

    # ------------------------------------------------------------ settings / audit

    @router.get("/settings")
    def settings_page(request: Request):
        with db.read() as conn:
            backups = sorted(settings.backup_dir.glob("bets-*.sqlite3")) if settings.backup_dir.exists() else []
            return render(request, "settings.html", {
                "accounts": accounts(conn, active_only=False), "schema": db.schema_version(),
                "backups": len(backups), "last_backup": backups[-1].name[5:21] if backups else None,
                "username": (load_credentials(settings.auth_file) or {}).get("username"),
                "assistant": assistant.status(settings.assistant_file),
                "trusted": imports.auto_confirm_settings(conn), "KINDS": imports.TRUSTABLE,
                "KIND_TITLES": imports.KIND_TITLES, "perms": ops.permissions(conn), "policy": plans.policy(conn)})

    @router.post("/settings/retention-policy")
    async def retention_policy_save(request: Request):
        def fn(conn, f):
            data = {}
            for kind, keys in (("snr", ("excellent", "very_good", "acceptable", "reject_below")),
                               ("sr", ("excellent", "very_good", "acceptable", "reject_below")),
                               ("qualifying", ("excellent_loss_pct", "good_loss_pct", "max_loss_pct", "unhedged_max_loss"))):
                vals = {k: f.get(f"{kind}_{k}") for k in keys if f.get(f"{kind}_{k}", "").strip()}
                if vals:
                    data[kind] = vals
            try:
                plans.save_policy(conn, data)
            except ValueError as exc:
                raise UserError(str(exc)) from None
        return await write(request, fn, "Retention policy saved", "/settings#policy")

    @router.post("/settings/assistant/auto-confirm")
    async def assistant_auto_confirm(request: Request):
        """Trusted Assistant mode: which kinds of assistant submissions are confirmed automatically (OFF by default)."""
        form = await request.form()
        kinds = [k for k in imports.TRUSTABLE if k in form.getlist("kinds")]
        enabled = form.get("enabled") == "on" and bool(kinds)
        with db.tx() as conn:
            before = imports.auto_confirm_settings(conn)
            services.set_setting(conn, imports.AUTO_SETTING, {"enabled": enabled, "kinds": kinds})
            services.audit(conn, "setting", None, "assistant_auto_confirm", before, {"enabled": enabled, "kinds": kinds},
                           "Trusted Assistant mode changed in Settings")
        if enabled:
            return back("/settings", ok="Trusted Assistant mode ON for: " +
                        ", ".join(imports.KIND_TITLES[k] for k in kinds))
        return back("/settings", ok="Trusted Assistant mode OFF: everything from the assistant waits for you")

    @router.post("/settings/assistant/disable")
    async def assistant_disable(request: Request):
        def fn(conn, f):
            tid = assistant.disable(settings.assistant_file)
            services.audit(conn, "assistant_token", None, "disabled", None, {"id": tid}, "from Settings")
        return await write(request, fn, "Assistant access disabled. Issue a new token on the Pi to turn it back on.",
                           "/settings")

    @router.get("/audit")
    def audit_page(request: Request):
        entity = request.query_params.get("entity", "")
        with db.read() as conn:
            q, params = "SELECT * FROM audit_log", []
            if entity:
                q += " WHERE entity = ?"
                params.append(entity)
            rows = [dict(r) for r in conn.execute(q + " ORDER BY id DESC LIMIT 300", params)]
            return render(request, "audit.html", {"rows": rows, "entity": entity})

    app.include_router(router)
    return app
