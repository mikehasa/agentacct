"""Read-only, generated managed content for the native setup review.

This is deliberately not a full-file merge preview. Existing user values are
read only to classify recognized registrations and conditions, never returned.
No installer, writer, marker recorder, store opener, or runtime helper is called.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import hooks, install_guide
from . import version as version_info

SCHEMA = "agentacct.setup-preview.v1"
CLIENTS = ("codex", "claude-code", "opencode", "hermes", "dsh")
REGISTRATION_NAMES = ("agentacct", "agent-chronicle", "agent-sentinel")
MAX_CONFIG_BYTES = 4_000_000


def build_setup_preview(
    client: str,
    *,
    home: Path,
    store_dir: Path,
    command: str,
    python_executable: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Generate the proposed snippets and classify only known managed entries."""
    if client not in CLIENTS:
        raise ValueError("unsupported setup preview client")
    # Lazy import avoids a cycle with the CLI command registration. These are
    # existing pure generators/predicates shared with the actual writers.
    from . import cli

    environment = os.environ if environment is None else environment
    interpreter = python_executable or sys.executable or "python3"
    files: list[dict[str, Any]] = []
    reads: dict[Path, tuple[str | None, str, str | None]] = {}

    def read(path: Path) -> tuple[str | None, str, str | None]:
        if path not in reads:
            try:
                if path.stat().st_size > MAX_CONFIG_BYTES:
                    result = (None, "unreadable", "File exceeds the read-only preview size limit.")
                else:
                    result = (path.read_text(encoding="utf-8"), "inspected", None)
            except FileNotFoundError:
                result = ("", "absent", None)
            except (OSError, UnicodeError) as exc:
                # Exception text may contain user data. Return only its type.
                result = (None, "unreadable", f"File could not be read ({type(exc).__name__}).")
            reads[path] = result
        return reads[path]

    def item(path: Path, kind: str, title: str, content: str) -> tuple[dict[str, Any], str | None]:
        text, status, problem = read(path)
        row: dict[str, Any] = {
            "id": kind, "path": str(path), "kind": kind, "title": title,
            "proposed_content": content, "existing_status": status,
            "conditions": [problem] if problem else [], "registrations": [],
        }
        files.append(row)
        return row, text

    def parse(row: dict[str, Any], text: str | None, language: str) -> dict[str, Any] | None:
        if text is None:
            return None
        if row["existing_status"] == "absent":
            return {}
        try:
            if language == "toml":
                payload = tomllib.loads(text)
            elif language == "yaml":
                import yaml
                payload = yaml.safe_load(text)
            else:
                payload = json.loads(text)
            if isinstance(payload, dict):
                return payload
        except Exception:
            # Parser error strings can quote input values; do not expose them.
            pass
        row["existing_status"] = "parse_error"
        row["conditions"].append(f"Existing {language.upper()} could not be parsed as an object; existing managed entries are unresolved.")
        return None

    def registration(row: dict[str, Any], name: str, action: str, reason: str) -> None:
        row["registrations"].append({"name": name, "action": action, "reason": reason})

    def mcp_conditions(row: dict[str, Any], payload: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
        if payload is None:
            registration(row, "agentacct", "unresolved", "Generated content only; existing registration could not be inspected.")
            return None
        servers = payload.get(key, {})
        if servers is None and client in {"opencode", "hermes"}:
            servers = {}
        if not isinstance(servers, dict):
            row["existing_status"] = "unsupported"
            row["conditions"].append(f"The existing {key} value is not an object; registration changes are unresolved.")
            return None
        return servers

    def instructions(path: Path) -> None:
        row, text = item(path, "instructions", "Managed recording instructions", install_guide.workflow_instruction_block() + "\n")
        if text is None:
            row["managed_block_action"] = "unresolved"
            return
        present = install_guide.instruction_file_has_managed_block(text)
        row["managed_block_action"] = "unchanged" if install_guide.render_instruction_file(text, remove=False) == text else "replace" if present else "append"
        row["recognized_marker_families"] = ["agentacct", "agent-chronicle", "agent-sentinel"]
        row["conditions"].append("Only the first complete recognized block outside Markdown fences is replaced. Other instruction content is preserved; quoted or incomplete markers are not managed blocks.")

    generated = cli._mcp_server_config(store_dir, command=command)
    if client in {"codex", "claude-code"}:
        codex = client == "codex"
        instructions(home / (".codex/AGENTS.md" if codex else ".claude/CLAUDE.md"))
        path = home / (".codex/config.toml" if codex else ".claude.json")
        content = cli._codex_mcp_toml_block(store_dir, command=command) if codex else json.dumps({"mcpServers": {"agentacct": {"type": "stdio", **generated}}}, indent=2) + "\n"
        row, text = item(path, "mcp", "Proposed MCP registration", content)
        payload = parse(row, text, "toml" if codex else "json")
        servers = mcp_conditions(row, payload, "mcp_servers" if codex else "mcpServers")
        if codex and row["existing_status"] == "parse_error":
            row["conditions"].append("The Codex writer may still perform a textual section replacement on malformed TOML. Review the file before installing; this preview cannot classify its legacy settings.")
        if servers is not None:
            sentinel = servers.get("agent-sentinel")
            try:
                custom_sentinel = sentinel is not None and not cli._pre_rename_registration_matches_generated(sentinel, generated)
            except (TypeError, ValueError):
                custom_sentinel = True
            blocked = codex and custom_sentinel
            registration(row, "agentacct", "skip" if blocked else "update" if "agentacct" in servers else "add", "Custom agent-sentinel settings prevent the Codex MCP write." if blocked else "Set generated command and arguments; retained environment values are not displayed.")
            for name in REGISTRATION_NAMES[1:]:
                if name in servers:
                    preserve = blocked or (name == "agent-sentinel" and custom_sentinel)
                    registration(row, name, "preserve" if preserve else "remove", "Custom legacy settings remain unchanged." if preserve else "Collapse this recognized legacy registration into agentacct; retain supported environment settings.")
            if custom_sentinel:
                row["conditions"].append("Custom agent-sentinel registration is preserved." + (" The Codex MCP write is skipped." if codex else " Claude's agentacct registration is still added or updated alongside it."))
        row["conditions"].append("Unrelated entries and retained values are not displayed. This is generated managed content, not a full-file merge diff.")
        wrapper = home / (hooks.CODEX_HOOK_RELATIVE_PATH if codex else hooks.CLAUDE_HOOK_RELATIVE_PATH)
        block = hooks.codex_hooks_json_block(wrapper, python_executable=interpreter) if codex else hooks.claude_code_settings_example(interpreter, hook_path=wrapper)
        row, text = item(home / (".codex/hooks.json" if codex else ".claude/settings.json"), "hooks", "Proposed recording hooks" if codex else "Proposed hooks and settings", json.dumps(block, indent=2) + "\n")
        payload = parse(row, text, "json")
        if payload is not None and "hooks" in payload and not isinstance(payload["hooks"], dict):
            row["existing_status"] = "unsupported"
            row["conditions"].append("Existing hooks is not an object; it requires review before installation.")
        row["conditions"].append("Existing user hooks are preserved. Matching agentacct wrapper entries are updated.")
        if not codex:
            env = payload.get("env", {}) if payload is not None else {}
            if not isinstance(env, dict) or ("ENABLE_TOOL_SEARCH" in env and env["ENABLE_TOOL_SEARCH"] != "auto"):
                row["existing_status"] = "unsupported"
                row["conditions"].append("Existing env or ENABLE_TOOL_SEARCH conflicts with the generated settings. The Claude settings merge is skipped; the existing value is not shown or overwritten.")
            row["conditions"].append("The proposal adds ENABLE_TOOL_SEARCH=auto only when absent or already matching. A conflicting value prevents the settings merge. The generated statusLine is added only when one is absent; an existing status line is preserved.")
        renderer = hooks.render_codex_hook_wrapper if codex else hooks.render_claude_hook_wrapper
        item(wrapper, "wrapper", "Generated hook wrapper", renderer(command, store_dir=store_dir))
        if not codex:
            item(home / hooks.CLAUDE_SETTINGS_RELATIVE_PATH, "settings_example", "Generated settings example", json.dumps(block, indent=2) + "\n")

    elif client == "opencode":
        xdg = environment.get("XDG_CONFIG_HOME", "").strip()
        directory = (Path(xdg) if xdg else home / ".config") / "opencode"
        instructions(directory / "AGENTS.md")
        path = next((directory / name for name in ("opencode.jsonc", "opencode.json") if (directory / name).is_file()), directory / "opencode.jsonc")
        block = cli._opencode_mcp_entry(store_dir, command=command)
        row, text = item(path, "mcp", "Proposed MCP registration", json.dumps({"mcp": {"agentacct": block}}, indent=2) + "\n")
        payload = parse(row, text, "json")
        servers = mcp_conditions(row, payload, "mcp")
        if servers is not None:
            registration(row, "agentacct", "update" if "agentacct" in servers else "add", "Set the generated local command; preserve extra existing entry keys.")
            for name in REGISTRATION_NAMES[1:]:
                if name in servers:
                    owned = cli._looks_like_agentacct_opencode_entry(servers[name])
                    registration(row, name, "remove" if owned else "preserve", "Recognized agentacct-family command is collapsed." if owned else "Custom same-named server is left unchanged.")
        if row["existing_status"] in {"parse_error", "unsupported"}:
            row["conditions"].append("The MCP writer skips JSONC comments, malformed JSON, or a non-object mcp value. Register the server manually if this remains unresolved.")
        item(directory / "plugins/agentacct.js", "plugin", "Generated activity plugin", hooks.render_opencode_plugin(command, store_dir=store_dir))

    elif client == "dsh":
        dsh_env = (environment.get("DSH_HOME") or environment.get("DSH_DIR") or "").strip()
        dsh_home = Path(dsh_env).expanduser() if dsh_env else home / ".dsh"
        instructions(dsh_home / "AGENTS.md")
        # dsh's cordis.patch.yml is a top-level YAML LIST of loader patch ops (not
        # a servers mapping), applied over every profile the CLI boots.
        patch_path = dsh_home / "cordis.patch.yml"
        row, text = item(patch_path, "mcp", "Proposed MCP registration", cli._dsh_mcp_patch_block(store_dir, command=command))
        if text is None:
            registration(row, "agentacct", "unresolved", "Generated content only; the existing patch could not be inspected.")
        elif row["existing_status"] == "absent" or not text.strip():
            registration(row, "agentacct", "add", "Create the home patch with the agentacct MCP server (applies to every dsh profile).")
        elif cli._dsh_patch_has_agentacct(text):
            registration(row, "agentacct", "update", "An agentacct insert (id: mcp-agentacct) is already registered; it is left in place.")
        elif cli._dsh_patch_rows(text) is not None:
            registration(row, "agentacct", "add", "Append the agentacct insert op to the existing patch list; other patches are preserved.")
        else:
            row["existing_status"] = "unsupported"
            row["conditions"].append("The existing patch file is not a plain patch list agentacct can safely extend; the block is previewed for manual application.")
        row["conditions"].append("Writes to $DSH_HOME/cordis.patch.yml are non-destructive (append-only, tolerant of custom !!js tags). Whether dsh resolves the bundled @deepseek-ai/dsh-mcp-client plugin for every profile is verified on one machine only.")

    else:
        path = home / ".hermes/config.yaml"
        row, text = item(path, "mcp", "Proposed MCP registration", cli._hermes_mcp_yaml_block(store_dir, command=command, header=True, child_indent="  "))
        payload = parse(row, text, "yaml")
        servers = mcp_conditions(row, payload, "mcp_servers")
        unsupported = text is not None and any(re.match(r"^mcp_servers:[ \t]*[^\s#]", line) for line in text.splitlines())
        if unsupported:
            row["existing_status"] = "unsupported"
            row["conditions"].append("An inline mcp_servers block cannot be edited safely by the current writer.")
        if servers is not None:
            registration(row, "agentacct", "unresolved" if unsupported else "update" if "agentacct" in servers else "add", "Only the agentacct child is managed. Inline or tab-indented YAML can require manual changes.")
            for name in REGISTRATION_NAMES[1:]:
                if name in servers:
                    registration(row, name, "preserve", "The Hermes writer does not remove legacy sibling registrations.")
        wrapper = home / hooks.HERMES_HOOK_RELATIVE_PATH
        hook_command = cli._hermes_hook_command(wrapper, python_executable=interpreter)
        content = cli._hermes_hooks_yaml_block(hook_command, list(hooks.HERMES_HOOK_EVENT_SUBCOMMANDS), timeout=10, child_indent="  ")
        row, text = item(path, "hooks", "Proposed recording and instruction hooks", content)
        parse(row, text, "yaml")
        row["conditions"].append("Inline or tab-indented hooks can be skipped. Existing unrelated hooks remain. Hermes requires separate hook approval and a gateway restart or new session.")
        item(wrapper, "wrapper", "Generated hook wrapper", hooks.render_hermes_hook_wrapper(command, store_dir=store_dir))
        row, _ = item(path, "injected_instructions", "First-turn recording instructions", install_guide.workflow_instruction_body())
        row["conditions"].append("Hermes has no global instruction file. The pre_llm_call hook injects this directive on the first turn after hook approval; this text is not written into config.yaml.")

    return {
        "schema_version": SCHEMA, "client": client, "scope": "user",
        "cli_version": version_info.package_version(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "store_dir": str(store_dir), "command": command, "python_executable": interpreter,
        "basis": "Generated managed content with read-only inspection of recognized entries; not a full-file merge diff.",
        "limits": [
            "Executable and interpreter paths are resolved for this preview and may differ after installation.",
            "Existing retained environment values, unrelated configuration, and account data are never included.",
            "Files can change after preview. Installation re-evaluates its writers and reports actual results.",
        ],
        "files": files,
    }
