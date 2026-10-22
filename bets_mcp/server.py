"""The Matched Betting Assistant MCP server (Streamable HTTP at /bets/mcp, OAuth-protected).

Run: python -m bets_mcp.server (systemd: matched-betting-mcp, 127.0.0.1:8762).
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import uvicorn
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import SCOPE, Settings, load_settings
from .mixed import ToolSecuritySchemes, open_mcp_route
from .oauth import Provider, consent_routes
from .store import Store
from .tools import Limits, register
from .upstream import Upstream

INSTRUCTIONS = (
    "Matched Betting Assistant records what the user tells you into their private matched-betting dashboard as "
    "candidates. Normally nothing is recorded until the user confirms it in /bets/; if a result says auto_confirmed "
    "(the user's Trusted Assistant mode) it has been recorded. Never place bets or say you did. "
    "Never invent names, amounts, odds, dates or events: ask for missing details, one or two at a time. The server "
    "does all arithmetic. Transfers of the user's own money are never profit.\n\n"
    "Workflow: use get_intake_schema if unsure what an entry needs. Money is in the dashboard's currency (get_dashboard_totals says which) and times are UK local unless the "
    "user says otherwise. New account: submit_account_candidate (ask bookmaker/exchange/bank and current balance). "
    "Balance of an existing account: submit_balance_candidate (a reconciliation the user confirms). Money moved: "
    "submit_transfer_candidate (ask temporary funding or permanent bankroll). Bets placed: "
    "submit_position_candidate (ask for the event if only a team was named; leave lay_stake empty unless the user "
    "stated it; a free-bet position_type uses one of the user's free-bet tokens, chosen when they confirm; set "
    "free_bet_already_marked_used only if the user says they marked that token used by hand). Results of bets the "
    "user placed: submit_settlement_candidate with the result and settlement date the user states — never guess "
    "them. Offers / free bets / notes have their own tools. Use calculate_match for figures without "
    "recording. If a tool returns unknown_account, ask whether to add that account — never guess; once submitted, "
    "entries using it can follow (they are confirmed after it). After a submission, say briefly what was sent, list "
    "any warnings, and give the confirmation_url; say whether it is waiting for confirmation, held for review (and "
    "why) or recorded automatically. Use dry_run when the user asks "
    "for a check. Never try to confirm, settle, delete or bypass confirmation, and never reveal tokens.")


PLUGIN_MANIFEST = Path(__file__).resolve().parent.parent / "plugin" / "matched-betting-assistant" / "plugin.json"


def plugin_version() -> str:
    try:
        return str(json.loads(PLUGIN_MANIFEST.read_text())["version"])
    except (OSError, ValueError, KeyError):
        return "0.0.0"


def tools_fingerprint(server) -> str:
    """A short hash of exactly what tools/list advertises (names, titles, descriptions, schemas, annotations,
    _meta, security schemes)."""
    tools = sorted(server._tool_manager.list_tools(), key=lambda t: t.name)
    blob = json.dumps([[t.name, t.title, t.description, t.parameters,
                        t.annotations.model_dump(exclude_none=True) if t.annotations else None, t.meta]
                       for t in tools], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


def server_version(server) -> str:
    """serverInfo.version in initialize: the plugin's version plus a fingerprint of the tool manifest (semver build
    metadata), e.g. 1.4.0+tools.1a2b3c4d. It changes by itself whenever any tool or schema changes, so a client that
    caches the tool list by server name + version (ChatGPT's connector) always sees that there is something new.
    It was a constant "1.0.0" from 9 Oct to 15 Oct while the tools went from ~40 to 96."""
    return f"{plugin_version()}+tools.{tools_fingerprint(server)}"


def build(settings: Settings | None = None, *, upstream: Upstream | None = None, cimd_fetch=None):
    """The production server: mixed authentication (anonymous initialize/tools/list; per-tool securitySchemes;
    protected tools need a valid bets.intake token) with CIMD client registration (bets_mcp/mixed.py, cimd.py)."""
    settings = settings or load_settings()
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(settings.db_path)
    provider = Provider(settings, store, cimd_fetch)
    upstream = upstream or Upstream(settings.upstream, settings.assistant_token_file)
    server = MCPServer(
        name="matched-betting-assistant", title="Matched Betting Assistant", version=plugin_version(),
        instructions=INSTRUCTIONS, log_level="WARNING", auth_server_provider=provider,
        auth=AuthSettings(issuer_url=settings.issuer, resource_server_url=settings.resource,
                          required_scopes=[SCOPE], validate_token_resource=True,
                          client_registration_options=ClientRegistrationOptions(
                              enabled=settings.dcr, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
                          revocation_options=RevocationOptions(enabled=True)))
    schemes = register(server, upstream, Limits(settings.calls_per_minute, settings.calls_per_day), settings)
    server._lowlevel_server.version = server_version(server)     # every tool is registered: fingerprint them

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else ""
        return request.headers.get("x-real-ip", peer) if peer == settings.trusted_proxy else peer

    server.custom_route("/consent", methods=["GET", "POST"])(consent_routes(settings, store, client_ip))
    app = server.streamable_http_app(
        streamable_http_path=settings.mcp_path, json_response=True, stateless_http=True,
        max_request_body_size=32 * 1024, host=settings.host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=settings.allowed_hosts,
            allowed_origins=[settings.public_origin, "https://chatgpt.com", "https://chat.openai.com"]))
    _metadata_route(app, server, settings)
    _authorize_iss(app, settings)
    open_mcp_route(app, settings.mcp_path)            # tokens are still verified; each protected tool checks them
    wrapped = SecurityHeaders(ToolSecuritySchemes(app, settings.mcp_path, schemes))
    wrapped.starlette = app                           # its lifespan runs the MCP session manager
    return wrapped, server, store


# ChatGPT is a public OAuth client (PKCE, no client secret). With CIMD its client_id is the URL of its own
# metadata document, so there is no registration endpoint and the only token method is "none". Issuer
# identification (RFC 9207) is advertised: every authorization response that goes back to the client carries
# `iss` equal to the metadata issuer -- the SDK's error redirects get it from _authorize_iss, the consent page's
# approve/deny redirects add it themselves. ChatGPT then uses its stable CIMD document
# (https://chatgpt.com/oauth/client.json) and redirect URI; the per-connector ones stay allowed.
def authorization_server_metadata(server, settings: Settings) -> dict:
    auth = server.settings.auth
    metadata = build_metadata(auth.issuer_url, auth.service_documentation_url, auth.client_registration_options,
                              auth.revocation_options).model_dump(mode="json", exclude_none=True)
    metadata["token_endpoint_auth_methods_supported"] = list(settings.token_auth_methods)
    metadata["revocation_endpoint_auth_methods_supported"] = list(settings.token_auth_methods)
    if settings.cimd:
        metadata["client_id_metadata_document_supported"] = True
    metadata["authorization_response_iss_parameter_supported"] = True
    return metadata


def with_iss(location: str, settings: Settings) -> str:
    """RFC 9207: add iss to a redirect back to the client (never to our own consent page). An iss already there
    must be ours: anything else is a bug, so it fails loudly instead of sending a mismatched issuer."""
    parts = urlsplit(location)
    if f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{settings.issuer}/consent":
        return location
    query = parse_qsl(parts.query, keep_blank_values=True)
    issuers = [v for k, v in query if k == "iss"]
    if issuers:
        if issuers != [settings.issuer]:
            raise RuntimeError("authorization response carries a different iss")
        return location
    return urlunsplit(parts._replace(query=urlencode([*query, ("iss", settings.issuer)])))


def _authorize_iss(app, settings: Settings) -> None:
    """Wrap the SDK's /authorize so its redirects (errors such as invalid_scope, access_denied, invalid_target or
    a missing PKCE challenge, once client and redirect URI are known) carry iss like ours do."""
    for route in app.router.routes:
        if getattr(route, "path", None) == "/authorize":
            inner = route.app

            async def wrapped(scope, receive, send):
                async def send_iss(message):
                    if message["type"] == "http.response.start" and 300 <= message["status"] < 400:
                        headers = [(k, with_iss(v.decode("latin-1"), settings).encode("latin-1"))
                                   if k.lower() == b"location" else (k, v) for k, v in message.get("headers", [])]
                        message = {**message, "headers": headers}
                    await send(message)
                await inner(scope, receive, send_iss)

            route.app = wrapped
            return
    raise RuntimeError("authorization route not found")


def _metadata_route(app, server, settings: Settings) -> None:
    """Serve our authorization-server document instead of the SDK's."""
    document = authorization_server_metadata(server, settings)

    async def handler(request: Request):
        return JSONResponse(document, headers={"Cache-Control": "no-store"})

    for i, route in enumerate(app.router.routes):
        if getattr(route, "path", None) == "/.well-known/oauth-authorization-server":
            app.router.routes[i] = Route(route.path, endpoint=cors_middleware(handler, ["GET", "OPTIONS"]),
                                         methods=["GET", "OPTIONS"])
            return
    raise RuntimeError("authorization server metadata route not found")


class SecurityHeaders:
    """Pure ASGI: no-store, nosniff, no framing and a default CSP on every response (the consent
    page sets its own stricter-but-specific CSP)."""

    DEFAULTS = {b"cache-control": b"no-store", b"x-content-type-options": b"nosniff", b"x-frame-options": b"DENY",
                b"referrer-policy": b"no-referrer",
                b"content-security-policy": b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server"]
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in self.DEFAULTS.items() if k not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


def configure_logging() -> None:
    """Only our one-line tool/outcome records at INFO; libraries (which may echo payloads at
    DEBUG/INFO) only at WARNING. Tokens and amounts are never logged."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("bets_mcp").setLevel(logging.INFO)
    for noisy in ("mcp", "mcp_types", "httpx2", "httpcore2", "uvicorn", "uvicorn.error", "sse_starlette", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    settings = load_settings()
    app, _, _ = build(settings)
    # Loopback only: reachable through nginx at /bets/mcp. No access log (paths only live in nginx's log).
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, access_log=False, proxy_headers=False,
                server_header=False, log_config=None, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
