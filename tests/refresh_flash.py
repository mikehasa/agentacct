"""Test helper for the dashboard refresh summary.

A dashboard refresh used to redirect to ``/?imported=…&scanned=…&…`` and tests
read the counts straight off ``response.headers["location"]``. The summary now
rides a one-time flash cookie so the URL stays a clean ``/``; this helper
re-exposes it as a urlencoded query string, so a ``"imported=3" in …`` style
assertion ports with a one-token change from ``response.headers["location"]`` to
``refresh_flash_qs(response)``.
"""

from __future__ import annotations

import http.cookies
import time
from urllib.parse import urlencode

from agentacct.api import REFRESH_FLASH_COOKIE, _decode_refresh_flash


def run_dashboard_refresh(client, *, timeout_s: float = 20.0):
    """Trigger a dashboard refresh and return the final ``303 -> /`` response
    (carrying the one-time flash cookie), following the background refresh's
    no-JS ``/refreshing`` progress loop.

    The refresh now runs on a background thread: POST /usage/import-local 303s to
    /refreshing, which self-polls until the worker finishes and then 303s home
    with the flash. Tests that used to read counts off the POST redirect call
    this instead and read them off the returned response, unchanged."""

    posted = client.post("/usage/import-local", follow_redirects=False)
    assert posted.status_code == 303, posted.status_code
    location = posted.headers.get("location")
    if location == "/":
        # Back-compat: a synchronous path would already be home.
        return posted
    assert location == "/refreshing", location
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        polled = client.get("/refreshing", follow_redirects=False)
        if polled.status_code == 303 and polled.headers.get("location") == "/":
            return polled
        assert polled.status_code in (200, 303), polled.status_code
        time.sleep(0.02)
    raise AssertionError("dashboard refresh did not complete within timeout")


def refresh_flash_status(response) -> dict:
    """The decoded refresh summary carried by the flash cookie ({} if absent)."""

    raw = response.cookies.get(REFRESH_FLASH_COOKIE)
    if raw is None:
        # Some clients don't surface a redirect response's cookies on the
        # object; fall back to the raw Set-Cookie header.
        jar: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
        jar.load(response.headers.get("set-cookie", ""))
        morsel = jar.get(REFRESH_FLASH_COOKIE)
        raw = morsel.value if morsel is not None else None
    return _decode_refresh_flash(raw) or {}


def refresh_flash_qs(response) -> str:
    """The refresh summary as a urlencoded query string (``imported=3&…``)."""

    return urlencode(refresh_flash_status(response))
