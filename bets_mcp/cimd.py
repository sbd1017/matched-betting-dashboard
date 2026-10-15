"""Client ID Metadata Documents (CIMD): ChatGPT's client_id is a URL to its own client metadata.

Only ChatGPT's documents are ever fetched (never an arbitrary URL from a request, so no SSRF):
    https://chatgpt.com/oauth/client.json                  (when issuer identification is supported)
    https://chatgpt.com/oauth/<callback_id>/client.json    (otherwise)
The fetch is HTTPS only, follows no redirects, is size- and time-limited, and is cached (Cache-Control
max-age, 1 minute to 1 hour). A document is accepted only if:
  * its client_id equals the URL it was fetched from, exactly;
  * it holds no client secret;
  * it allows the public-client token method "none" (we use PKCE; private_key_jwt is not implemented);
  * it lists at least one allowed ChatGPT redirect URI (only those are kept);
  * it allows the authorization_code grant and the "code" response type.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx2
from mcp.shared.auth import OAuthClientInformationFull

from .config import SCOPE, Settings

log = logging.getLogger("bets_mcp")
CIMD_URL = re.compile(r"^https://chatgpt\.com/oauth/(?:[A-Za-z0-9_-]{1,128}/)?client\.json$")
MAX_BYTES = 16 * 1024


class CimdError(ValueError):
    pass


def validate(url: str, doc, settings: Settings) -> OAuthClientInformationFull:
    if not isinstance(doc, dict):
        raise CimdError("not a JSON object")
    if doc.get("client_id") != url:
        raise CimdError("client_id does not equal the document URL")
    if "client_secret" in doc or "client_secret_expires_at" in doc:
        raise CimdError("documents must not carry a client secret")
    methods = doc.get("token_endpoint_auth_methods_supported") or [doc.get("token_endpoint_auth_method")]
    if not isinstance(methods, list) or "none" not in methods:
        raise CimdError("the client does not support the public-client method 'none'")
    uris = doc.get("redirect_uris")
    if not isinstance(uris, list):
        raise CimdError("redirect_uris missing")
    allowed = [u for u in uris if isinstance(u, str) and settings.redirect_allowed(u)]
    if not allowed:
        raise CimdError("no allowed ChatGPT redirect URI")
    grants = doc.get("grant_types") or ["authorization_code"]
    if not isinstance(grants, list) or "authorization_code" not in grants:
        raise CimdError("authorization_code grant not allowed")
    responses = doc.get("response_types") or ["code"]
    if not isinstance(responses, list) or "code" not in responses:
        raise CimdError("'code' response type not allowed")
    name = doc.get("client_name") if isinstance(doc.get("client_name"), str) else "ChatGPT"
    return OAuthClientInformationFull(
        client_id=url, redirect_uris=allowed, token_endpoint_auth_method="none",
        grant_types=[g for g in ("authorization_code", "refresh_token") if g in grants],
        response_types=["code"], client_name=name[:100], scope=SCOPE)


class CimdResolver:
    def __init__(self, settings: Settings, fetch=None):
        self.settings = settings
        self.fetch = fetch or self._fetch
        self.cache: dict[str, tuple[float, OAuthClientInformationFull | None]] = {}

    async def client(self, url: str) -> OAuthClientInformationFull | None:
        if not CIMD_URL.match(url or ""):
            return None                                       # never fetched
        hit = self.cache.get(url)
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            doc, ttl = await self.fetch(url)
            info = validate(url, doc, self.settings)
        except (CimdError, httpx2.HTTPError, ValueError) as exc:
            log.info("cimd rejected url=%s reason=%s", url, str(exc)[:120])
            self.cache[url] = (time.time() + 60, None)
            return None
        self.cache[url] = (time.time() + ttl, info)
        log.info("cimd accepted url=%s redirects=%d", url, len(info.redirect_uris))
        return info

    @staticmethod
    async def _fetch(url: str) -> tuple[dict, int]:
        async with httpx2.AsyncClient(timeout=5, follow_redirects=False) as c:
            async with c.stream("GET", url, headers={"Accept": "application/json",
                                                     "User-Agent": "matched-betting-oauth/1.0"}) as r:
                if r.status_code != 200:
                    raise CimdError(f"HTTP {r.status_code}")
                if not r.headers.get("content-type", "").split(";")[0].strip().lower().endswith("json"):
                    raise CimdError("not JSON")
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise CimdError("document too large")
                ttl = 300
                m = re.search(r"max-age=(\d+)", r.headers.get("cache-control", ""))
                if m:
                    ttl = min(max(int(m.group(1)), 60), 3600)
        return json.loads(body), ttl
