"""Host/Origin guard on the local HTTP surface (dashboard/API).

The guard defends localhost binds against the user's own browser acting as a
confused deputy: DNS rebinding (evil Host header on any request) and CSRF
(cross-site browser Origin on mutating requests). Non-browser clients send no
Origin header and must keep working untouched.
"""

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentacct.api import create_local_api_app
from agentacct.cli import app as cli_app
from agentacct.localhost_guard import LocalhostGuardMiddleware
from agentacct.service import SentinelService


def _api_client(tmp_path, **kwargs):
    return TestClient(create_local_api_app(store_dir=tmp_path, **kwargs))


def test_default_test_client_get_allowed_on_the_local_api(tmp_path):
    # TestClient sends Host: testserver and no Origin; the guard must be
    # active yet transparent for it (regression sentinel for every existing
    # TestClient call site).
    assert _api_client(tmp_path).get("/health").status_code == 200


def test_evil_host_get_blocked_even_without_origin(tmp_path):
    # DNS rebinding: the rebound request carries the attacker's hostname on a
    # plain GET (no Origin header at all). The Host rule applies to ALL
    # requests, not just mutating ones.
    response = _api_client(tmp_path).get("/overview", headers={"Host": "evil.example"})
    assert response.status_code == 403
    assert "not an allowed local hostname" in response.text


def test_cross_origin_post_blocked_and_writes_nothing(tmp_path):
    client = _api_client(tmp_path)

    response = client.post(
        "/events",
        json={"source": "evil-page", "event_type": "note"},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert "Cross-origin" in response.text
    assert SentinelService(tmp_path).list_events(limit=10) == []


def test_null_origin_post_blocked(tmp_path):
    client = _api_client(tmp_path)

    response = client.post(
        "/events",
        json={"source": "evil-page", "event_type": "note"},
        headers={"Origin": "null"},
    )

    assert response.status_code == 403
    assert SentinelService(tmp_path).list_events(limit=10) == []


def test_absent_origin_post_allowed_and_recorded(tmp_path):
    # curl/SDK/MCP-sidecar path: non-browser clients send no Origin header.
    client = _api_client(tmp_path)

    response = client.post("/events", json={"source": "sidecar", "event_type": "note"})

    assert response.status_code == 200
    events = SentinelService(tmp_path).list_events(limit=10)
    assert len(events) == 1
    assert events[0]["source"] == "sidecar"


def test_same_origin_mutating_post_allowed(tmp_path):
    # A browser page served from the local surface itself sends a matching
    # Origin on mutating requests; both canonical spellings must pass.
    client = _api_client(tmp_path)

    for origin, host in (
        ("http://127.0.0.1:8765", "127.0.0.1:8765"),
        ("http://localhost:8765", "localhost:8765"),
    ):
        response = client.post(
            "/events",
            json={"source": "local-page", "event_type": "note"},
            headers={"Origin": origin, "Host": host},
        )
        assert response.status_code == 200, (origin, host)


def test_host_header_port_and_bracketed_ipv6_parse_correctly(tmp_path):
    # [::1]:8765 must yield ::1 — a naive split(":") mangles the bracketed
    # IPv6 form (the reason this guard is hand-rolled).
    client = _api_client(tmp_path)

    assert client.get("/health", headers={"Host": "127.0.0.1:8765"}).status_code == 200
    assert client.get("/health", headers={"Host": "[::1]:8765"}).status_code == 200
    assert client.get("/health", headers={"Host": "[::1]"}).status_code == 200


def test_extra_allowed_hosts_factory_kwarg(tmp_path):
    blocked = _api_client(tmp_path).get("/health", headers={"Host": "myhost.local:8765"})
    assert blocked.status_code == 403

    allowed = _api_client(tmp_path, extra_allowed_hosts=("myhost.local",)).get(
        "/health", headers={"Host": "myhost.local:8765"}
    )
    assert allowed.status_code == 200


def test_extra_allowed_host_origin_accepted_on_mutating_request(tmp_path):
    client = _api_client(tmp_path, extra_allowed_hosts=("myhost.local",))

    response = client.post(
        "/events",
        json={"source": "lab", "event_type": "note"},
        headers={"Origin": "http://myhost.local:8765", "Host": "myhost.local:8765"},
    )

    assert response.status_code == 200


def test_cli_allow_host_reaches_the_app_factories(tmp_path, monkeypatch):
    # `serve` and `api serve` must plumb --allow-host into the
    # factory kwarg. Capture the app uvicorn would run instead of serving.
    import uvicorn

    served_apps = []
    monkeypatch.setattr(uvicorn, "run", lambda served_app, **kwargs: served_apps.append(served_app))

    for command in (
        ["serve", "--store-dir", str(tmp_path), "--allow-host", "myhost.local"],
        ["api", "serve", "--store-dir", str(tmp_path), "--allow-host", "myhost.local"],
    ):
        result = CliRunner().invoke(cli_app, command)
        assert result.exit_code == 0, (command, result.output)

    assert len(served_apps) == 2
    for served_app in served_apps:
        client = TestClient(served_app)
        assert client.get("/health", headers={"Host": "myhost.local"}).status_code == 200
        assert client.get("/health", headers={"Host": "evil.example"}).status_code == 403


def test_guard_is_outermost_middleware_on_the_local_api(tmp_path):
    # Starlette runs the most recently added middleware first; the guard must
    # reject before the pricing env-var middleware touches process state.
    app = create_local_api_app(store_dir=tmp_path)

    assert app.user_middleware[0].cls is LocalhostGuardMiddleware
    assert len(app.user_middleware) >= 2


def test_testserver_is_not_in_the_shipped_default_allowlist():
    # A test-only hostname must never ride into a production allowlist; the
    # suite injects it via extra_allowed_hosts instead (autouse conftest fixture).
    from agentacct.localhost_guard import DEFAULT_ALLOWED_HOSTS

    assert "testserver" not in DEFAULT_ALLOWED_HOSTS
    assert DEFAULT_ALLOWED_HOSTS == frozenset({"localhost", "127.0.0.1", "::1"})


def _drive_websocket(middleware, headers):
    """Feed one websocket scope through the middleware; return (inner_called, sent)."""
    import asyncio

    scope = {"type": "websocket", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
    inner_called = {"value": False}

    async def inner(_scope, _receive, _send):
        inner_called["value"] = True

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():  # pragma: no cover - not reached in these cases
        return {"type": "websocket.connect"}

    guard = middleware(inner, allowed_hosts=frozenset({"127.0.0.1", "localhost"}))
    asyncio.run(guard(scope, receive, send))
    return inner_called["value"], sent


def test_websocket_with_disallowed_host_is_closed_before_the_inner_app():
    inner_called, sent = _drive_websocket(
        LocalhostGuardMiddleware, {"host": "evil.example", "origin": "https://evil.example"}
    )
    assert inner_called is False
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_websocket_with_cross_origin_is_closed_before_the_inner_app():
    inner_called, sent = _drive_websocket(
        LocalhostGuardMiddleware, {"host": "127.0.0.1:8765", "origin": "https://evil.example"}
    )
    assert inner_called is False
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_websocket_with_allowed_host_and_no_origin_reaches_the_inner_app():
    inner_called, sent = _drive_websocket(LocalhostGuardMiddleware, {"host": "127.0.0.1:8765"})
    assert inner_called is True
    assert sent == []
