"""Mixed authentication ("OAuth or no authentication" in ChatGPT's plugin builder).

* initialize and tools/list are anonymous; tool schemas carry no user data.
* Each tool declares `securitySchemes` (top level of the tool descriptor, as OpenAI's Apps SDK documents;
  mirrored in `_meta`): `noauth` or `oauth2` with the bets.intake scope.
* A protected tool runs only with a valid token: ours (issuer), unexpired, issued for this resource
  (audience) and carrying the scope. Otherwise it returns an error result with
  `_meta["mcp/www_authenticate"]`, which makes ChatGPT start OAuth linking.
The OAuth server (registration, authorize/consent, token, metadata) is the same as for /bets/mcp.
"""
from __future__ import annotations

import json
import time

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.routes import build_resource_metadata_url
from mcp_types import CallToolResult, TextContent
from starlette.routing import Route

from .config import SCOPE, Settings

NOAUTH = [{"type": "noauth"}]
OAUTH = [{"type": "oauth2", "scopes": [SCOPE]}]
# Anonymous tools also carry an OAuth scope tag (OpenAI's "optional auth" form): ChatGPT's builder warns
# "OAuth scope tags are not defined for every selected tool" when a tool declares only noauth.
OPTIONAL = NOAUTH + OAUTH


def challenge(settings: Settings, description: str) -> str:
    prm = str(build_resource_metadata_url(settings.resource))
    return (f'Bearer resource_metadata="{prm}", error="insufficient_scope", scope="{SCOPE}", '
            f'error_description="{description}"')


def auth_required(settings: Settings, description: str = "Sign in to Matched Betting to continue") -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text="Authentication required: " + description)],
                          is_error=True, _meta={"mcp/www_authenticate": [challenge(settings, description)]})


def check_token(settings: Settings) -> CallToolResult | None:
    """None when the request carries a token we issued, for this resource, unexpired, with the scope."""
    token = get_access_token()
    if token is None:
        return auth_required(settings)
    claims = token.claims or {}
    if claims.get("iss") != settings.issuer:
        return auth_required(settings, "This sign-in was not issued by this server")
    if token.resource != settings.resource:
        return auth_required(settings, "This sign-in is for a different server")
    if not token.expires_at or token.expires_at <= int(time.time()):
        return auth_required(settings, "Your sign-in has expired")
    if SCOPE not in (token.scopes or []):
        return auth_required(settings, f"The {SCOPE} permission is needed")
    return None


def open_mcp_route(app, mcp_path: str) -> None:
    """Serve the MCP endpoint without the SDK's whole-endpoint 401 (bearer tokens are still parsed and
    verified by its middleware for every request, and each protected tool checks them)."""
    for i, route in enumerate(app.router.routes):
        if getattr(route, "path", None) == mcp_path:
            inner = getattr(route.endpoint, "app", None)
            if inner is None:
                raise RuntimeError("MCP route is not wrapped by RequireAuthMiddleware")
            app.router.routes[i] = Route(mcp_path, endpoint=inner)
            return
    raise RuntimeError("MCP route not found")


class ToolSecuritySchemes:
    """Adds the top-level `securitySchemes` to tools/list responses (the SDK's Tool model has no such
    field). Only for the MCP endpoint's JSON responses; unknown tools get oauth2 (fail closed)."""

    def __init__(self, app, mcp_path: str, schemes: dict[str, list]):
        self.app, self.mcp_path, self.schemes = app, mcp_path, schemes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != self.mcp_path or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        start, chunks = None, []

        async def capture(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                await self._flush(start, b"".join(chunks), send)

        await self.app(scope, receive, capture)

    async def _flush(self, start, body: bytes, send):
        headers = dict((k.lower(), v) for k, v in start.get("headers", []))
        if headers.get(b"content-type", b"").startswith(b"application/json") and body:
            try:
                data = json.loads(body)
                tools = (data.get("result") or {}).get("tools") if isinstance(data, dict) else None
                if isinstance(tools, list):
                    for tool in tools:
                        tool["securitySchemes"] = self.schemes.get(tool.get("name"), OAUTH)
                    body = json.dumps(data, separators=(",", ":")).encode()
            except ValueError:
                pass
        out = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
        out.append((b"content-length", str(len(body)).encode()))
        await send({**start, "headers": out})
        await send({"type": "http.response.body", "body": body})
