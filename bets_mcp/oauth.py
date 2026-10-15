"""OAuth 2.1 for the plugin (what ChatGPT's "Add custom MCP server" -> OAuth expects).

* ChatGPT registers itself (dynamic client registration); only ChatGPT's own callback URLs are
  accepted, so a code can never be sent anywhere else.
* Approving a connection needs a one-time connection code made on the Pi
  (scripts/mcp-access.sh connect-code). It is not the /bets/ password and is useless once used.
* Authorization code + PKCE (S256, checked by the SDK); tokens are bound to this server
  (`resource`), hashed at rest, short-lived (access 1 h) and rotated (refresh, reuse = revoke).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import time

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import SCOPE, Settings
from .store import Store


class Limiter:
    """Failures per client IP (and overall) in a window."""

    def __init__(self, per_ip: int, overall: int, window: int = 900):
        self.per_ip, self.overall, self.window = per_ip, overall, window
        self.hits: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        now = time.time()
        self.hits[key] = [t for t in self.hits.get(key, []) if now - t < self.window]
        return self.hits[key]

    def blocked(self, ip: str) -> bool:
        return len(self._recent(ip)) >= self.per_ip or len(self._recent("*")) >= self.overall

    def fail(self, ip: str) -> None:
        for key in (ip, "*"):
            self.hits.setdefault(key, []).append(time.time())


class Provider:
    def __init__(self, settings: Settings, store: Store, cimd_fetch=None):
        self.settings, self.store = settings, store
        self.cimd = None
        if settings.cimd:
            from .cimd import CimdResolver
            self.cimd = CimdResolver(settings, cimd_fetch)

    # ------------------------------------------------------------ registration

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if self.cimd and client_id.startswith("https://"):
            return await self.cimd.client(client_id)          # ChatGPT's own metadata document (validated)
        info = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or not all(self.settings.redirect_allowed(u) for u in uris):
            raise RegistrationError("invalid_redirect_uri", "Only ChatGPT can connect to this server")
        if client_info.scope and set(client_info.scope.split()) - {SCOPE}:
            raise RegistrationError("invalid_client_metadata", f"Only the {SCOPE} scope exists")
        if set(client_info.grant_types or []) - {"authorization_code", "refresh_token"}:
            raise RegistrationError("invalid_client_metadata", "Only authorization_code and refresh_token")
        if client_info.client_name and len(client_info.client_name) > 100:
            raise RegistrationError("invalid_client_metadata", "client_name too long")
        if not self.store.add_client(client_info.client_id, client_info.model_dump(mode="json"),
                                     self.settings.max_clients, self.settings.registrations_per_hour):
            raise RegistrationError("invalid_client_metadata", "Registration limit reached; try again later")

    # ------------------------------------------------------------ authorization (-> consent page)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self.store.enabled():
            raise AuthorizeError("access_denied", "Assistant access is turned off on the Pi")
        if params.resource not in (None, self.settings.resource):
            raise AuthorizeError("invalid_target", "Tokens are only issued for this MCP server")
        if not self.settings.redirect_allowed(str(params.redirect_uri)):
            raise AuthorizeError("invalid_request", "Unexpected redirect URI")
        if params.scopes and set(params.scopes) - {SCOPE}:
            raise AuthorizeError("invalid_scope", f"Only the {SCOPE} scope exists")
        rid = self.store.new_consent(client.client_id, {
            "state": params.state, "scopes": [SCOPE], "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri), "explicit": params.redirect_uri_provided_explicitly,
            "resource": self.settings.resource, "client_name": client.client_name or "ChatGPT"}, self.settings.consent_ttl)
        return f"{self.settings.issuer}/consent?request={rid}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        row = self.store.get_code(authorization_code)
        if not row or row["client_id"] != client.client_id:
            return None
        p = json.loads(row["params_json"])
        return AuthorizationCode(code=authorization_code, scopes=p["scopes"], expires_at=row["expires_at"],
                                 client_id=client.client_id, code_challenge=p["code_challenge"],
                                 redirect_uri=p["redirect_uri"], redirect_uri_provided_explicitly=p["explicit"],
                                 resource=p["resource"], subject=row["grant_id"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        row = self.store.take_code(authorization_code.code)                       # single use
        if not row or row["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Authorization code is invalid or already used")
        return self._tokens(row["grant_id"], client.client_id, authorization_code.scopes, authorization_code.resource)

    def _tokens(self, grant_id: str, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = self.store.issue_tokens(grant_id, client_id, scopes, resource or self.settings.resource,
                                                  self.settings.access_ttl, self.settings.refresh_ttl)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.settings.access_ttl,
                          refresh_token=refresh, scope=" ".join(scopes))

    # ------------------------------------------------------------ refresh (rotating) and access tokens

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        if not self.store.enabled():
            return None
        row = self.store.find_token(refresh_token, "refresh")
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"].split(),
                            expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["grant_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        if not self.store.mark_refresh_used(refresh_token.token):
            raise TokenError("invalid_grant", "Refresh token is invalid or already used")
        return self._tokens(refresh_token.subject, client.client_id, refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if not token.startswith("mbma_") or not self.store.enabled():
            return None
        row = self.store.find_token(token, "access")
        if not row:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(),
                           expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["grant_id"],
                           claims={"iss": self.settings.issuer})

    async def revoke_token(self, token) -> None:
        self.store.revoke_grant_of(token.token)


# ---------------------------------------------------------------- consent page

STYLE = ("body{font:16px/1.5 system-ui,sans-serif;background:#0f1216;color:#e6e9ee;margin:0;padding:24px 16px}"
         "main{max-width:440px;margin:0 auto}h1{font-size:1.3rem}ul{padding-left:20px}"
         ".box{background:#171b21;border:1px solid #2a313b;border-radius:12px;padding:16px;margin:16px 0}"
         "label{display:block;margin:12px 0 4px}input{width:100%;box-sizing:border-box;font:inherit;padding:10px;"
         "border-radius:8px;border:1px solid #3a4350;background:#0f1216;color:inherit;letter-spacing:.08em}"
         "button{font:inherit;padding:10px 16px;border-radius:8px;border:1px solid #3a4350;background:#222a34;"
         "color:inherit;margin:12px 8px 0 0}button.ok{background:#3d6fb8;border-color:#3d6fb8}"
         ".err{color:#f0a08f}.muted{color:#9aa3ae;font-size:.9rem}")
STYLE_HASH = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()


def _page(body: str, status: int = 200, form_targets: str = "") -> HTMLResponse:
    resp = HTMLResponse(f"<!doctype html><html lang=\"en-GB\"><head><meta charset=\"utf-8\">"
                        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                        f"<title>Connect Matched Betting Assistant</title><style>{STYLE}</style></head>"
                        f"<body><main>{body}</main></body></html>", status_code=status)
    resp.headers["Content-Security-Policy"] = (f"default-src 'none'; style-src 'sha256-{STYLE_HASH}'; "
                                               f"form-action 'self' {form_targets}; frame-ancestors 'none'; base-uri 'none'")
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def consent_routes(settings: Settings, store: Store, client_ip):
    failures = Limiter(per_ip=5, overall=20)
    targets = "https://chatgpt.com https://platform.openai.com " + " ".join(settings.extra_redirects)

    def form(rid: str, client_name: str, redirect_uri: str, error: str = "") -> str:
        host = html.escape(redirect_uri.split("/")[2])
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        return (f"<h1>Connect {html.escape(client_name)} to Matched Betting Assistant</h1>"
                f"<div class=\"box\"><p><b>It will be able to:</b></p><ul>"
                f"<li>read which details each kind of entry needs</li><li>use the lay calculator</li>"
                f"<li>submit accounts, balances, transfers, offers, free bets, bets and notes as <b>pending</b> "
                f"entries that you confirm, edit or reject at example.com/bets</li></ul>"
                f"<p><b>It will not be able to</b> read balances, profit, the ledger or your bets, confirm or "
                f"settle anything, delete, export, or change settings.</p></div>"
                f"<form method=\"post\" action=\"consent\" autocomplete=\"off\">"
                f"<input type=\"hidden\" name=\"request\" value=\"{html.escape(rid)}\">"
                f"<label for=\"code\">Connection code from the Pi</label>"
                f"<input id=\"code\" name=\"code\" autocapitalize=\"characters\" spellcheck=\"false\" "
                f"placeholder=\"XXXXX-XXXXX-XXXXX-XXXXX\" maxlength=\"40\">{err}"
                f"<button class=\"ok\" name=\"decision\" value=\"approve\">Connect</button>"
                f"<button name=\"decision\" value=\"deny\">Cancel</button></form>"
                f"<p class=\"muted\">After connecting you return to {host}. Make a code on the Pi with "
                f"<code>scripts/mcp-access.sh connect-code</code>; each code works once, for 15 minutes.</p>")

    expired = ("<h1>This connection link has expired</h1><p>Start again from ChatGPT "
               "(Plugins → Matched Betting Assistant → Connect).</p>")

    async def consent(request: Request) -> Response:
        if request.method == "GET":
            rid = request.query_params.get("request", "")
            found = store.get_consent(rid)
            if not found:
                return _page(expired, 400)
            _, p = found
            return _page(form(rid, p["client_name"], p["redirect_uri"]), form_targets=targets)
        ip = client_ip(request)
        if failures.blocked(ip):
            return _page("<h1>Too many attempts</h1><p>Wait 15 minutes, then start again from ChatGPT.</p>", 429)
        data = await request.form()
        rid = str(data.get("request", ""))[:100]
        found = store.get_consent(rid)
        if not found:
            return _page(expired, 400)
        client_id, p = found
        if data.get("decision") != "approve":
            store.drop_consent(rid)
            return RedirectResponse(construct_redirect_uri(p["redirect_uri"], error="access_denied", state=p["state"],
                                                           iss=settings.issuer), status_code=302)
        if not store.enabled():
            return _page("<h1>Assistant access is turned off</h1><p>Turn it on on the Pi first.</p>", 403)
        if not store.use_connect_code(str(data.get("code", ""))[:40]):
            failures.fail(ip)
            return _page(form(rid, p["client_name"], p["redirect_uri"], "That code is wrong, used or expired."),
                         400, form_targets=targets)
        code = store.new_grant_and_code(client_id, p["client_name"], p, settings.code_ttl)
        store.drop_consent(rid)
        resp = RedirectResponse(construct_redirect_uri(p["redirect_uri"], code=code, state=p["state"],
                                                       iss=settings.issuer), status_code=302)
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return consent
