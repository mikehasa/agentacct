import importlib.util
from pathlib import Path

_EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "examples" / "agent_like_openrouter_loop.py"
_SPEC = importlib.util.spec_from_file_location("agent_like_openrouter_loop", _EXAMPLE_PATH)
example = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(example)

build_messages = example.build_messages
run_loop = example.run_loop


class FakeClient:
    def __init__(self, statuses):
        self.statuses = statuses
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json or {}, "timeout": timeout})
        status, body = self.statuses[len(self.calls) - 1]
        return FakeResponse(status, body)


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_build_messages_carries_running_state():
    messages = build_messages(
        task="Design a safety layer",
        step_name="policy",
        running_state="architecture summary",
    )
    joined = "\n".join(message["content"] for message in messages)

    assert "Design a safety layer" in joined
    assert "policy" in joined
    assert "architecture summary" in joined


def test_run_loop_stops_on_budget_block():
    client = FakeClient(
        [
            (200, {"choices": [{"message": {"content": "step one"}}], "agent_sentinel": {"actual_provider_cost_usd": 0.001, "decision": "forwarded"}}),
            (402, {"error": {"type": "agent_sentinel_budget_exceeded", "message": "blocked"}, "agent_sentinel": {"decision": "blocked"}}),
        ]
    )

    summary = run_loop(
        client=client,
        proxy_base_url="http://127.0.0.1:8787",
        model="openai/gpt-4o-mini",
        task="Design a safety layer",
        steps=["one", "two", "three"],
        run_id="run_example_test",
        max_tokens=64,
        timeout=3,
    )

    assert summary["status"] == "blocked"
    assert summary["steps_attempted"] == 2
    assert summary["steps_forwarded"] == 1
    assert summary["events"][1]["status_code"] == 402
    assert client.calls[0]["headers"]["X-Agent-Sentinel-Run-Id"] == "run_example_test"


def test_run_loop_handles_proxy_errors_without_traceback():
    class BrokenClient:
        def post(self, *args, **kwargs):
            raise RuntimeError("proxy unavailable")

    summary = run_loop(
        client=BrokenClient(),
        proxy_base_url="http://127.0.0.1:9",
        model="openai/gpt-4o-mini",
        task="Design a safety layer",
        steps=["one", "two"],
        run_id="run_example_test",
        max_tokens=64,
        timeout=0.1,
    )

    assert summary["status"] == "error"
    assert summary["steps_attempted"] == 1
    assert summary["steps_forwarded"] == 0
    assert summary["events"][0]["decision"] == "proxy_error"


def test_run_loop_completes_all_steps():
    client = FakeClient(
        [
            (200, {"choices": [{"message": {"content": "a"}}], "agent_sentinel": {"actual_provider_cost_usd": 0.001, "decision": "forwarded"}}),
            (200, {"choices": [{"message": {"content": "b"}}], "agent_sentinel": {"actual_provider_cost_usd": 0.002, "decision": "forwarded"}}),
        ]
    )

    summary = run_loop(
        client=client,
        proxy_base_url="http://127.0.0.1:8787",
        model="openai/gpt-4o-mini",
        task="Design a safety layer",
        steps=["one", "two"],
        run_id="run_example_test",
        max_tokens=64,
        timeout=3,
    )

    assert summary["status"] == "completed"
    assert summary["steps_attempted"] == 2
    assert summary["steps_forwarded"] == 2
    assert summary["actual_provider_cost_usd"] == 0.003
