"""Managed setup preview must never run writers or expose unrelated values."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import agentacct.cli as cli
from agentacct import hooks, install_guide
from agentacct.setup_preview import CLIENTS, SCHEMA, build_setup_preview


def preview(tmp_path, client="codex", **kwargs):
    return build_setup_preview(client, home=tmp_path, store_dir=tmp_path / "uncreated-store",
                               command="/managed/agentacct", python_executable="/managed/python3",
                               environment={}, **kwargs)


def row(payload, kind):
    return next(item for item in payload["files"] if item["kind"] == kind)


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def snapshot(root):
    return {str(path.relative_to(root)): (path.is_dir(), None if path.is_dir() else path.read_bytes(), path.stat().st_mtime_ns)
            for path in [root, *root.rglob("*")]}


@pytest.mark.parametrize("client", CLIENTS)
def test_command_is_read_only_for_every_client(tmp_path, monkeypatch, client):
    home = tmp_path / "home"
    home.mkdir()
    write(home / ".codex/config.toml", '[mcp_servers.other]\ncommand="PRIVATE-OTHER-COMMAND"\n')
    write(home / ".claude.json", '{"account":"PRIVATE-ACCOUNT", "mcpServers":{}}')
    write(home / ".hermes/config.yaml", 'provider_key: PRIVATE-PROVIDER-KEY\n')
    write(home / ".config/opencode/opencode.json", '{"unrelated":"PRIVATE-OTHER-VALUE"}')
    before = snapshot(home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(cli, "_resolve_absolute_mcp_command", lambda: "/managed/agentacct")

    def forbidden(*args, **kwargs):
        pytest.fail("Preview invoked a mutating onboarding/runtime/writer path")

    for name in ("_onboard_global", "_onboard_global_codex", "_onboard_global_claude", "_onboard_global_opencode",
                 "_onboard_global_hermes", "_atomic_write_text", "_write_codex_mcp_config_at",
                 "_write_user_claude_mcp_config", "_write_opencode_mcp_config_at", "_write_hermes_mcp_config_at",
                 "_install_codex_hook", "_install_hermes_hook", "_managed_runtime", "_record_instrumentation_marker_best_effort",
                 "_write_kimi_code_mcp_config_at", "_write_kimi_code_home_mcp", "_onboard_global_kimi_code"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli, "setup_instructions", forbidden)
    store = tmp_path / "not-created-store"

    result = CliRunner().invoke(cli.app, ["setup", "preview", "--agent", client, "--user", "--json", "--store-dir", str(store)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == SCHEMA
    assert payload["client"] == client
    assert payload["scope"] == "user"
    assert payload["store_dir"] == str(store)
    assert "PRIVATE-" not in result.output
    assert snapshot(home) == before
    assert not store.exists()


def test_codex_snippets_use_the_real_generators_and_standard_legacy_actions(tmp_path):
    store = tmp_path / "uncreated-store"
    write(tmp_path / ".codex/config.toml", f'''
unrelated = "DO-NOT-RENDER"
[mcp_servers.agent-sentinel]
command = "/managed/agent-sentinel"
args = ["mcp", "serve", "--store-dir", {json.dumps(str(store))}]
env = {{ SECRET = "RETAIN-WITHOUT-RENDERING" }}
[mcp_servers.agent-chronicle]
command = "/old/agent-chronicle"
''')
    payload = preview(tmp_path)

    assert row(payload, "instructions")["proposed_content"] == install_guide.workflow_instruction_block() + "\n"
    assert row(payload, "mcp")["proposed_content"] == cli._codex_mcp_toml_block(store, command="/managed/agentacct")
    assert json.loads(row(payload, "hooks")["proposed_content"]) == hooks.codex_hooks_json_block(tmp_path / hooks.CODEX_HOOK_RELATIVE_PATH, python_executable="/managed/python3")
    assert row(payload, "wrapper")["proposed_content"] == hooks.render_codex_hook_wrapper("/managed/agentacct", store_dir=store)
    assert {x["name"]: x["action"] for x in row(payload, "mcp")["registrations"]} == {
        "agentacct": "add", "agent-chronicle": "remove", "agent-sentinel": "remove",
    }
    assert "DO-NOT-RENDER" not in json.dumps(payload)
    assert "RETAIN-WITHOUT-RENDERING" not in json.dumps(payload)


def test_codex_custom_sentinel_skips_the_whole_mcp_write(tmp_path):
    write(tmp_path / ".codex/config.toml", '[mcp_servers.agent-sentinel]\ncommand="CUSTOM-PRIVATE"\nargs=[]\n[mcp_servers.agent-chronicle]\ncommand="old"\n')

    payload = preview(tmp_path)

    assert {x["name"]: x["action"] for x in row(payload, "mcp")["registrations"]} == {
        "agentacct": "skip", "agent-chronicle": "preserve", "agent-sentinel": "preserve",
    }
    assert "CUSTOM-PRIVATE" not in json.dumps(payload)


def test_claude_custom_sentinel_is_preserved_but_new_registration_is_added(tmp_path):
    write(tmp_path / ".claude.json", json.dumps({"account": "PRIVATE-ACCOUNT", "mcpServers": {
        "agent-sentinel": {"command": "CUSTOM-SECRET", "args": []}, "agent-chronicle": {"command": "old"},
    }}))
    write(tmp_path / ".claude/settings.json", json.dumps({"env": {"ENABLE_TOOL_SEARCH": "PRIVATE-CONFLICT"}, "statusLine": {"command": "PRIVATE-STATUS"}}))

    payload = preview(tmp_path, "claude-code")

    assert {x["name"]: x["action"] for x in row(payload, "mcp")["registrations"]} == {
        "agentacct": "add", "agent-chronicle": "remove", "agent-sentinel": "preserve",
    }
    assert row(payload, "hooks")["existing_status"] == "unsupported"
    assert any("merge is skipped" in text for text in row(payload, "hooks")["conditions"])
    assert "PRIVATE-" not in json.dumps(payload)
    assert "CUSTOM-SECRET" not in json.dumps(payload)


def test_fenced_markers_are_preserved_and_real_legacy_block_is_replaced(tmp_path):
    markers = install_guide.LEGACY_CHRONICLE_INSTRUCTIONS_BEGIN_MARKER + "\nPRIVATE-INSTRUCTION\n" + install_guide.LEGACY_CHRONICLE_INSTRUCTIONS_END_MARKER
    write(tmp_path / ".codex/AGENTS.md", "```md\n" + markers + "\n```\n")
    assert row(preview(tmp_path), "instructions")["managed_block_action"] == "append"
    write(tmp_path / ".codex/AGENTS.md", "My own guidance\n\n" + markers + "\n")
    payload = preview(tmp_path)
    assert row(payload, "instructions")["managed_block_action"] == "replace"
    assert "PRIVATE-INSTRUCTION" not in json.dumps(payload)
    assert "My own guidance" not in json.dumps(payload)


@pytest.mark.parametrize("client,path,content", [
    ("codex", ".codex/config.toml", '[broken PRIVATE-TOML'),
    ("opencode", ".config/opencode/opencode.jsonc", '{// PRIVATE-COMMENT\n"mcp":{}}'),
    ("claude-code", ".claude.json", 'PRIVATE-INVALID-JSON'),
    ("kimi-code", ".kimi-code/mcp.json", '{"mcpServers": {"x": '),
])
def test_parser_failures_are_named_without_echoing_input(tmp_path, client, path, content):
    write(tmp_path / path, content)
    payload = preview(tmp_path, client)

    assert row(payload, "mcp")["existing_status"] == "parse_error"
    assert row(payload, "mcp")["registrations"][0]["action"] == "unresolved"
    assert "PRIVATE-" not in json.dumps(payload)
    if client == "codex":
        assert any("may still perform" in x for x in row(payload, "mcp")["conditions"])
    if client == "kimi-code":
        # The writer refuses an unreadable file outright; the preview must say so
        # rather than imply a partial merge.
        assert any("refuses to rewrite" in x for x in row(payload, "mcp")["conditions"])


def test_kimi_code_preview_shows_a_managed_write_with_legacy_actions(tmp_path):
    write(tmp_path / ".kimi-code/mcp.json", json.dumps({"mcpServers": {
        "linear": {"command": "linear-mcp"},
        "agentacct": {"command": "old", "args": ["mcp", "serve", "--store-dir", "/old"]},
        "agent-chronicle": {"command": "/old/agent-chronicle", "args": []},
        "agent-sentinel": {"command": "custom-recorder", "args": ["serve-elsewhere"]},
    }}))
    before = snapshot(tmp_path)

    payload = preview(tmp_path, "kimi-code")

    mcp = row(payload, "mcp")
    assert mcp["path"] == str(tmp_path / ".kimi-code" / "mcp.json")
    # The preview names the user-level file it would write, and the entry's shape.
    assert mcp["proposed_content"] == json.dumps({"mcpServers": {"agentacct": {
        "command": "/managed/agentacct",
        "args": ["mcp", "serve", "--store-dir", str(tmp_path / "uncreated-store")],
    }}}, indent=2) + "\n"
    assert {x["name"]: x["action"] for x in mcp["registrations"]} == {
        "agentacct": "update",  # managed write, not manual guidance
        "agent-chronicle": "remove",  # this tool's own prior name
        "agent-sentinel": "preserve",  # custom command/args are the user's
    }
    # The instruction proposal targets the same user-level home.
    assert row(payload, "instructions")["path"] == str(tmp_path / ".kimi-code" / "AGENTS.md")
    # Read-only: not one byte changes, and no writer ran.
    assert snapshot(tmp_path) == before
    assert "linear-mcp" not in json.dumps(payload)  # unrelated servers stay private


def test_opencode_only_collapses_recognized_legacy_commands(tmp_path):
    write(tmp_path / ".config/opencode/opencode.json", json.dumps({"mcp": {
        "agent-chronicle": {"command": ["/old/agent-chronicle", "mcp", "serve"]},
        "agent-sentinel": {"command": ["PRIVATE-CUSTOM"]},
    }}))
    payload = preview(tmp_path, "opencode")

    assert row(payload, "mcp")["path"].endswith("opencode.json")
    assert {x["name"]: x["action"] for x in row(payload, "mcp")["registrations"]} == {
        "agentacct": "add", "agent-chronicle": "remove", "agent-sentinel": "preserve",
    }
    assert "PRIVATE-CUSTOM" not in json.dumps(payload)


def test_hermes_preserves_legacy_names_and_shows_injected_instruction_body(tmp_path):
    write(tmp_path / ".hermes/config.yaml", 'mcp_servers:\n  agent-sentinel:\n    command: PRIVATE-LEGACY\n')
    payload = preview(tmp_path, "hermes")

    assert row(payload, "injected_instructions")["proposed_content"] == install_guide.workflow_instruction_body()
    assert not any(x["path"].endswith("AGENTS.md") for x in payload["files"])
    assert {x["name"]: x["action"] for x in row(payload, "mcp")["registrations"]} == {"agentacct": "add", "agent-sentinel": "preserve"}
    assert "PRIVATE-LEGACY" not in json.dumps(payload)


def test_unreadable_file_does_not_abort_other_generated_content(tmp_path):
    (tmp_path / ".codex/config.toml").mkdir(parents=True)
    payload = preview(tmp_path)
    assert row(payload, "mcp")["existing_status"] == "unreadable"
    assert row(payload, "hooks")["proposed_content"]


@pytest.mark.parametrize("arguments", [[], ["--user", "--store-dir", "relative"]])
def test_preview_refuses_ambiguous_scope_or_relative_store(arguments):
    result = CliRunner().invoke(cli.app, ["setup", "preview", "--agent", "codex", "--json", *arguments])
    assert result.exit_code != 0


def test_default_global_store_resolution_does_not_create_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    store = home / "new-global-store"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in ("AGENTACCT_GLOBAL_STORE_DIR", "AGENT_CHRONICLE_GLOBAL_STORE_DIR", "AGENT_SENTINEL_GLOBAL_STORE_DIR"):
        monkeypatch.setenv(name, str(store))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.setattr(cli, "_resolve_absolute_mcp_command", lambda: "/managed/agentacct")
    before = snapshot(home)

    result = CliRunner().invoke(cli.app, ["setup", "preview", "--agent", "codex", "--user", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["store_dir"] == str(store)
    assert snapshot(home) == before
    assert not store.exists()


@pytest.mark.parametrize("client,path,content", [
    ("codex", ".codex/config.toml", 'mcp_servers = ["PRIVATE-VALUE"]'),
    ("claude-code", ".claude.json", '{"mcpServers": "PRIVATE-VALUE"}'),
    ("opencode", ".config/opencode/opencode.json", '{"mcp": ["PRIVATE-VALUE"]}'),
    ("hermes", ".hermes/config.yaml", 'mcp_servers: PRIVATE-VALUE'),
])
def test_unsupported_mcp_shapes_remain_unresolved_without_leaking_values(tmp_path, client, path, content):
    write(tmp_path / path, content)
    before = snapshot(tmp_path)
    payload = preview(tmp_path, client)
    assert row(payload, "mcp")["existing_status"] == "unsupported"
    assert any("not an object" in text for text in row(payload, "mcp")["conditions"])
    assert "PRIVATE-VALUE" not in json.dumps(payload)
    assert snapshot(tmp_path) == before


def test_oversized_config_and_unreadable_instructions_leave_other_proposals_available(tmp_path, monkeypatch):
    import agentacct.setup_preview as module

    monkeypatch.setattr(module, "MAX_CONFIG_BYTES", 30)
    write(tmp_path / ".codex/config.toml", "PRIVATE-LARGE" * 10)
    (tmp_path / ".codex/AGENTS.md").mkdir()
    payload = preview(tmp_path)
    assert row(payload, "mcp")["existing_status"] == "unreadable"
    assert row(payload, "instructions")["managed_block_action"] == "unresolved"
    assert row(payload, "hooks")["proposed_content"]
    assert "PRIVATE-LARGE" not in json.dumps(payload)


def test_unknown_client_is_rejected_before_any_file_access(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported client must not inspect configuration")

    monkeypatch.setattr(Path, "stat", forbidden)
    with pytest.raises(ValueError, match="unsupported setup preview client"):
        preview(tmp_path, "unknown-client")
