"""Host/Origin guard shared by the local HTTP surfaces (dashboard/API, cost proxy).

Binding to 127.0.0.1 stops remote *connections*, not the user's own browser
being used as a confused deputy:

- DNS rebinding: an attacker page at evil.example can flip its DNS record to
  127.0.0.1; the victim's browser then talks to the local server with
  ``Host: evil.example`` and same-origin (from the browser's perspective) JS
  can read every GET response. A Host allowlist on ALL requests kills this,
  because a rebound public domain always presents the attacker's own hostname.
- Cross-site request forgery: any web page can fire "simple" POSTs at
  ``http://127.0.0.1:<port>`` without a CORS preflight (auto-submitted forms,
  ``no-cors`` fetch with an untyped Blob body). An Origin allowlist on
  non-GET/HEAD/OPTIONS requests blocks the cross-SITE cases (remote origins and
  ``null``) while keeping non-browser clients working: curl, httpx, provider
  SDKs pointed at the proxy, and MCP sidecars send no Origin header at all, so
  absent Origin is allowed.

The Origin allowlist compares HOSTNAME only, not the full origin, so it is
same-SITE permissive on loopback: a POST from another local origin the developer
also runs (``http://localhost:3000``, ``http://127.0.0.1:<any port>``, or
``https://localhost``) is treated as same-site and allowed. Full-origin
(scheme+host+port) comparison is not available because the guard is installed at
app-construction time, before uvicorn binds a port, so the server's own port is
unknown here. The accepted residual: a second attacker-influenced localhost
origin could drive forced import scans / bounded ledger noise — but SOP still
blocks it from reading any response, and remote/null origins stay blocked.

The checks are header-only — the guard never reads the request body, so
streaming proxy payloads pass through untouched — and run OUTERMOST, before
any other middleware or route handler.
"""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Loopback-only defaults. Starlette's TestClient default ``Host: testserver`` is
# deliberately NOT shipped here — a test-only hostname has no business in a
# production allowlist. The suite re-injects it through the same
# ``extra_allowed_hosts`` seam real deployments use for ``--allow-host``.
DEFAULT_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Methods that cannot mutate state on these apps (every mutating route is a
# POST today; guard the complement so future PUT/DELETE/PATCH are covered).
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _normalize_allowlist_hostname(value: str) -> str | None:
    """Normalize an allowlist entry: lowercase, brackets stripped ([::1] == ::1)."""
    hostname = value.strip().lower()
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1]
    return hostname or None


def _host_header_hostname(value: str | None) -> str | None:
    """Hostname of a ``Host`` header (``host[:port]``), or None if malformed.

    Handles the bracketed IPv6 form correctly: ``[::1]:8765`` must yield
    ``::1``, not the ``[``-prefixed mangling a naive ``split(":")`` produces.
    """
    if not value:
        return None
    value = value.strip()
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            return None
        rest = value[closing + 1 :]
        if rest and not rest.startswith(":"):
            return None
        hostname = value[1:closing]
    elif value.count(":") > 1:
        # A bare (unbracketed) IPv6 address is not a valid Host header.
        return None
    else:
        hostname = value.partition(":")[0]
    hostname = hostname.strip().lower()
    return hostname or None


def _origin_hostname(value: str) -> str | None:
    """Hostname of an ``Origin`` header URL, or None for ``null``/malformed."""
    value = value.strip()
    if not value or value.lower() == "null":
        return None
    try:
        hostname = urlsplit(value).hostname
    except ValueError:
        return None
    return hostname or None


class LocalhostGuardMiddleware:
    """Pure ASGI middleware: Host allowlist on all requests, Origin allowlist
    on mutating ones. Rejects with 403 plain text before any handler runs."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: frozenset[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    def _host_allowed(self, headers: Headers) -> bool:
        hostname = _host_header_hostname(headers.get("host"))
        return hostname is not None and hostname in self.allowed_hosts

    def _origin_allowed(self, headers: Headers) -> bool:
        origin_value = headers.get("origin")
        if origin_value is None:
            return True  # non-browser clients (curl/SDK/MCP) send no Origin
        origin_hostname = _origin_hostname(origin_value)
        return origin_hostname is not None and origin_hostname in self.allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # Browsers do NOT enforce SOP on websocket connects, so the Host and
            # Origin headers are the primary cross-origin defense here. Reject a
            # disallowed handshake with a policy-violation close before the inner
            # app sees it. No websocket route exists today; this closes the
            # latent gap so adding one is guarded by default.
            headers = Headers(scope=scope)
            if not self._host_allowed(headers) or not self._origin_allowed(headers):
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host_value = headers.get("host")
        hostname = _host_header_hostname(host_value)
        if hostname is None or hostname not in self.allowed_hosts:
            response = PlainTextResponse(
                f"Host {host_value!r} is not an allowed local hostname for this agentacct server. "
                "Allowed hosts are localhost/127.0.0.1/[::1]; add others with --allow-host.",
                status_code=403,
            )
            await response(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        if method not in _SAFE_METHODS:
            origin_value = headers.get("origin")
            if origin_value is not None:
                origin_hostname = _origin_hostname(origin_value)
                if origin_hostname is None or origin_hostname not in self.allowed_hosts:
                    response = PlainTextResponse(
                        f"Cross-origin {method} blocked: Origin {origin_value!r} is not allowed on this "
                        "local agentacct server. Non-browser clients (curl/SDKs/MCP) send no Origin "
                        "and are unaffected.",
                        status_code=403,
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def install_localhost_guard(app: Any, extra_allowed_hosts: Iterable[str] = ()) -> None:
    """Install the Host/Origin guard on a FastAPI/Starlette app.

    Call this LAST in an app factory: Starlette runs the most recently added
    middleware first, so adding the guard after every other middleware makes
    it outermost — rejected requests never reach the inner middleware or any
    route handler.
    """
    allowed_hosts = frozenset(
        DEFAULT_ALLOWED_HOSTS
        | {hostname for value in extra_allowed_hosts if (hostname := _normalize_allowlist_hostname(value)) is not None}
    )
    app.add_middleware(LocalhostGuardMiddleware, allowed_hosts=allowed_hosts)
