"""Hook-captured commands (Actions dimension).

An EXECUTE tool's command is captured by the tool-activity hook, single-lined and
best-effort scrubbed of obvious credential values (the local-capture model: keep the
command for readability, mask a live token; share-time redaction is the real safety net),
spooled, drained onto the tool_activity_observed event, aggregated per session, and
unioned into the Task's Actions commands for the Receipt.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from agentacct.hooks import _execute_command, capture_tool_activity
from agentacct.receipt import _actions_dimension, commands_preview
from agentacct.task_projection import build_task_projection
from agentacct.tool_activity import (
    _COMMAND_MAX,
    _normalize_command,
    build_commands_by_session,
    drain_tool_activity_spool,
)


def _store() -> Path:
    return Path(tempfile.mkdtemp())


def _bash_event(command: str, *, session: str = "s1") -> str:
    return json.dumps(
        {"tool_name": "Bash", "session_id": session, "cwd": "/r", "tool_input": {"command": command}}
    )


# ---------------------------------------------------------------------------
# scrub / normalize
# ---------------------------------------------------------------------------


def test_scrub_masks_bearer_and_authorization_header() -> None:
    out = _normalize_command('curl -H "Authorization: Bearer sk-abcdefghijklmnop" https://x')
    assert "sk-abcdefghijklmnop" not in out
    assert "‹redacted›" in out
    assert "curl" in out and "https://x" in out  # command structure kept


def test_scrub_masks_credential_flags() -> None:
    out = _normalize_command("mysql --password=Secret123 -u root")
    assert "Secret123" not in out and "--password=" in out  # value masked, flag name kept
    assert "tok_abcdef123456" not in _normalize_command("deploy --api-key tok_abcdef123456")


def test_scrub_masks_env_assignment() -> None:
    out = _normalize_command("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIexample123 aws s3 ls")
    assert "wJalrXUtnFEMIexample123" not in out and "AWS_SECRET_ACCESS_KEY=" in out


def test_scrub_masks_known_token_shapes() -> None:
    for token in (
        "sk-abcdefghijklmnopqrstuv",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-1234567890-abcdefghij",
    ):
        assert token not in _normalize_command(f"echo {token}"), token


def test_scrub_masks_url_userinfo() -> None:
    out = _normalize_command("git clone https://user:ghp_abcdefghijklmnopqrstuvwxyz012345@github.com/x")
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "user:" in out and "@github.com" in out


def test_normalize_collapses_whitespace_and_bounds_length() -> None:
    assert _normalize_command("pytest   -q\n\n&&  git   commit") == "pytest -q && git commit"
    assert len(_normalize_command("echo " + "a" * 1000)) <= _COMMAND_MAX


def test_normalize_drops_blank_and_nul() -> None:
    assert _normalize_command("   ") is None
    assert _normalize_command("a\x00b") is None
    assert _normalize_command(None) is None


def test_plain_command_unchanged() -> None:
    assert _normalize_command("pytest -q") == "pytest -q"
    assert "‹redacted›" not in _normalize_command("git commit -m 'add feature'")


def test_scrub_keeps_credential_named_path_and_var_values() -> None:
    # A *_FILE/_CREDENTIALS name or a $var value is a path/indirection, not an inline
    # secret — masking it would hide what the agent ran and protect nothing.
    for cmd, kept in [
        ("GOOGLE_APPLICATION_CREDENTIALS=/etc/gcp/key.json gcloud auth", "/etc/gcp/key.json"),
        ("AWS_SHARED_CREDENTIALS_FILE=/home/app/.aws/credentials aws s3 ls", "/home/app/.aws/credentials"),
        ("FOO_TOKEN=$GITHUB_TOKEN deploy", "$GITHUB_TOKEN"),
        ("deploy --token $MY_TOKEN", "$MY_TOKEN"),
    ]:
        assert kept in _normalize_command(cmd), cmd


def test_scrub_does_not_redact_english_or_grep_pattern() -> None:
    # No standalone bearer/basic scheme sub (it wrecked plain English); an ``authorization:``
    # header NAME in a grep/echo (quoted OR unquoted) is not a header being SENT — the
    # header sub anchors on a curl -H/--header flag, so these are kept verbatim.
    assert (
        _normalize_command('git commit -m "add basic authentication support"')
        == 'git commit -m "add basic authentication support"'
    )
    assert _normalize_command("grep -i authorization: access.log") == "grep -i authorization: access.log"
    assert _normalize_command('grep "Authorization:" access.log') == 'grep "Authorization:" access.log'
    assert _normalize_command('echo "x-api-key: is the header name"') == 'echo "x-api-key: is the header name"'


def test_scrub_masks_curl_header_flag_value() -> None:
    # A real header VALUE sent via curl -H/--header (quoted) is masked; an opaque token
    # with no known prefix is caught here even though the prefix subs miss it.
    assert "opaqueTok123456" not in _normalize_command('curl -H "Authorization: Bearer opaqueTok123456" https://x')
    assert "opaqueKey987" not in _normalize_command('curl --header="X-Api-Key: opaqueKey987" https://x')


def test_scrub_env_distinguishes_credential_from_config() -> None:
    # A credential env (keyword as a whole segment / name suffix) is masked; a config var
    # that merely CONTAINS the keyword, or a scalar value, or the shell PWD, is kept.
    for cmd, secret in [
        ("PGPASSWORD=hunter2 psql", "hunter2"),
        ("DB_PASSWORD=hunter2 app", "hunter2"),
        ("GITHUB_TOKEN=ghp_realtoken123456 gh", "ghp_realtoken123456"),
        ("SECRET_KEY=abc123xyz789 ./app", "abc123xyz789"),
    ]:
        assert secret not in _normalize_command(cmd), cmd
    for cmd, kept in [
        ("MAX_TOKENS=4096 python run.py", "MAX_TOKENS=4096"),
        ("TOKENIZERS_PARALLELISM=false python train.py", "TOKENIZERS_PARALLELISM=false"),
        ("python train.py token_count=5 batch=32", "token_count=5"),
        ("PWD=/home/user run", "PWD=/home/user"),
        ("OLDPWD=/tmp run", "OLDPWD=/tmp"),
    ]:
        assert kept in _normalize_command(cmd), cmd


def test_scrub_masks_json_body_credentials() -> None:
    assert "hunter2" not in _normalize_command('curl -d {"password":"hunter2"} https://x')
    assert "supersecret123" not in _normalize_command('curl -d {"api_key":"supersecret123"}')


def test_scrub_masks_additional_token_prefixes() -> None:
    assert "ghr_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in _normalize_command("echo ghr_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    assert "pypi-AgEIcHlwaS5vcmcExampleToken1234" not in _normalize_command("twine upload -p pypi-AgEIcHlwaS5vcmcExampleToken1234")


def test_scrub_masks_url_userinfo_without_username() -> None:
    assert "s3cretpassword" not in _normalize_command("psql redis://:s3cretpassword@localhost:6379/0")


def test_normalize_strips_control_and_ansi_bytes() -> None:
    # A crafted command with a raw ESC/BEL (C0) or CSI/OSC (C1) byte must not carry a
    # terminal escape into the Receipt render — control bytes are stripped, words stay apart.
    out = _normalize_command("curl x\x1b[2Kgit status\x07\x9b2K\x9dosc")
    assert not any(c in out for c in ("\x1b", "\x07", "\x9b", "\x9d"))
    assert "curl" in out and "git status" in out


# ---------------------------------------------------------------------------
# _execute_command gate
# ---------------------------------------------------------------------------


def test_execute_command_only_for_execute_category() -> None:
    ev = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert _execute_command(ev, "execute") == "ls"
    assert _execute_command(ev, "read") is None  # non-execute category
    assert _execute_command({"tool_input": {"file_path": "/x"}}, "execute") is None  # no command arg
    assert _execute_command({"tool_input": {"command": 123}}, "execute") is None  # non-str


# ---------------------------------------------------------------------------
# pipeline: capture -> drain -> commands
# ---------------------------------------------------------------------------


def test_capture_records_commands_deduped_execute_only() -> None:
    store = _store()
    capture_tool_activity(_bash_event("pytest -q"), store_dir=store, client="claude-code")
    capture_tool_activity(_bash_event("pytest -q"), store_dir=store, client="claude-code")  # dup
    capture_tool_activity(_bash_event("npm run build"), store_dir=store, client="claude-code")
    # a non-execute tool records no command
    capture_tool_activity(
        json.dumps({"tool_name": "Read", "session_id": "s1", "tool_input": {"file_path": "/r/a.py"}}),
        store_dir=store,
        client="claude-code",
    )
    meta = drain_tool_activity_spool(store)[0]["metadata"]
    assert meta["commands"] == ["pytest -q", "npm run build"]


def test_capture_scrubs_at_the_tick() -> None:
    store = _store()
    capture_tool_activity(
        _bash_event('curl -H "Authorization: Bearer sk-livetokenabcdef123" https://x'),
        store_dir=store,
        client="claude-code",
    )
    meta = drain_tool_activity_spool(store)[0]["metadata"]
    assert not any("sk-livetokenabcdef123" in c for c in meta["commands"])


def test_build_commands_by_session_unions_and_rescrubs() -> None:
    def _event(cmds: list[str]) -> dict[str, Any]:
        return {
            "event_type": "tool_activity_observed",
            "metadata": {"client": "opencode", "client_session_id": "ses_x", "commands": cmds},
        }

    result = build_commands_by_session([_event(["pytest", "ruff"]), _event(["ruff", "mypy"])])
    assert result[("opencode", "ses_x")] == ["pytest", "ruff", "mypy"]  # additive + deduped
    # a command that slipped through un-scrubbed is re-masked on read
    leaky = build_commands_by_session([_event(["deploy --token abcdef1234567890"])])
    assert not any("abcdef1234567890" in c for c in leaky[("opencode", "ses_x")])


# ---------------------------------------------------------------------------
# projection + receipt
# ---------------------------------------------------------------------------


def _session_with_commands(cmds: list[str]) -> dict[str, Any]:
    return {
        "client": "claude-code",
        "client_session_id": "s1",
        "identity_scope_state": "unscoped",
        "namespace_fingerprint": None,
        "session_kind": "root",
        "last_activity_at": 1.0,
        "related": {"parent": None},
        "usage": {"rows": 1, "priced_rows": 1, "unpriced_rows": 0, "fresh_tokens": 5, "total_tokens": 5, "estimated_cost_usd": 0.01, "model_lanes": [{"model": "claude"}]},
        "commands": cmds,
    }


def test_projection_unions_session_commands_into_actions() -> None:
    proj = build_task_projection([_session_with_commands(["pytest -q", "git commit"])], [])
    actions = proj["tasks"][0]["actions"]
    assert actions["commands"] == ["pytest -q", "git commit"]
    assert actions["command_count"] == 2


def test_receipt_actions_surfaces_commands_with_preview_cap() -> None:
    task = {"actions": {"commands": [f"cmd{i}" for i in range(20)], "command_count": 20}}
    d = _actions_dimension(task)
    assert len(d["commands_preview"]) == 12
    assert d["commands_elided"] == 8
    assert "hook" in d["provenance"]  # commands are a hook-captured signal


def test_commands_preview_helper() -> None:
    shown, elided = commands_preview({"commands": ["a", "b", "c"]}, limit=2)
    assert shown == ["a", "b"] and elided == 1
    assert commands_preview({}) == ([], 0)
