"""Pure renderers for opt-in host hook configuration.

Nothing in this module reads or writes a host configuration file.  Callers get
deterministic fragments and can present a diff before any separate installer is
authorized.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Mapping


OWNED_COMMAND_MARKER = "agent-chronicle capture hook"

_CLAUDE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SubagentStop",
    "SessionEnd",
)
_CODEX_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
_CURSOR_EVENTS = (
    "sessionStart",
    "sessionEnd",
    "beforeSubmitPrompt",
    "afterAgentResponse",
    "afterAgentThought",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "subagentStart",
    "subagentStop",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "beforeReadFile",
    "afterFileEdit",
    "preCompact",
    "stop",
)


@dataclass(frozen=True)
class RenderedHookManifest:
    vendor: str
    relative_path: str
    content: Mapping[str, Any]

    def to_json(self) -> str:
        return json.dumps(self.content, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.content))


def _validated_command(command: str) -> str:
    candidate = command.strip()
    if not candidate or len(candidate) > 512 or "\n" in candidate or "\r" in candidate or "\x00" in candidate:
        raise ValueError("hook command must be a bounded single line")
    return candidate


def _command(base: str, vendor: str, event: str) -> str:
    return f"{base} --vendor {vendor} --event {event}"


def render_hook_manifest(vendor: str, *, command: str = OWNED_COMMAND_MARKER) -> RenderedHookManifest:
    canonical = vendor.strip().lower().replace("_", "-")
    if canonical in {"claude", "claudecode", "cc"}:
        canonical = "claude-code"
    base = _validated_command(command)

    if canonical == "claude-code":
        hooks: dict[str, list[dict[str, Any]]] = {}
        for event in _CLAUDE_EVENTS:
            group: dict[str, Any] = {
                "hooks": [
                    {
                        "type": "command",
                        "command": _command(base, canonical, event),
                        "timeout": 5,
                    }
                ]
            }
            if event in {"PreToolUse", "PostToolUse"}:
                group["matcher"] = "*"
            hooks[event] = [group]
        return RenderedHookManifest(
            vendor=canonical,
            relative_path=".claude/settings.json",
            content={"hooks": hooks},
        )

    if canonical == "codex":
        hooks = {}
        for event in _CODEX_EVENTS:
            timeout = 30 if event == "Stop" else 10
            group = {
                "hooks": [
                    {
                        "type": "command",
                        "command": _command(base, canonical, event),
                        "timeout": timeout,
                    }
                ]
            }
            if event == "SessionStart":
                group["matcher"] = "startup|resume|clear"
            elif event in {"PreToolUse", "PostToolUse"}:
                group["matcher"] = "*"
            hooks[event] = [group]
        return RenderedHookManifest(
            vendor=canonical,
            relative_path=".codex/plugins/agent-chronicle/hooks/hooks.json",
            content={"hooks": hooks},
        )

    if canonical == "cursor":
        hooks = {
            event: [
                {
                    "command": _command(base, canonical, event),
                    "timeout": 5000,
                }
            ]
            for event in _CURSOR_EVENTS
        }
        return RenderedHookManifest(
            vendor=canonical,
            relative_path=".cursor/hooks.json",
            content={"version": 1, "hooks": hooks},
        )
    raise ValueError(f"unsupported capture vendor: {vendor!r}")


def merge_cursor_hooks(
    existing: Mapping[str, Any] | None,
    rendered: RenderedHookManifest | None = None,
) -> dict[str, Any]:
    """Return an idempotent Cursor merge while preserving foreign hooks."""

    desired = rendered or render_hook_manifest("cursor")
    if desired.vendor != "cursor":
        raise ValueError("merge_cursor_hooks requires a Cursor manifest")
    result = copy.deepcopy(dict(existing or {}))
    current_hooks = result.get("hooks")
    if not isinstance(current_hooks, Mapping):
        current_hooks = {}
    merged_hooks: dict[str, Any] = copy.deepcopy(dict(current_hooks))
    desired_hooks = desired.content.get("hooks")
    assert isinstance(desired_hooks, Mapping)

    desired_commands = {
        entry.get("command")
        for entries in desired_hooks.values()
        if isinstance(entries, list)
        for entry in entries
        if isinstance(entry, Mapping)
    }
    for event, entries in desired_hooks.items():
        foreign: list[Any] = []
        current = merged_hooks.get(event)
        if isinstance(current, list):
            for entry in current:
                command = entry.get("command") if isinstance(entry, Mapping) else None
                owned = isinstance(command, str) and (
                    OWNED_COMMAND_MARKER in command or command in desired_commands
                )
                if not owned:
                    foreign.append(copy.deepcopy(entry))
        merged_hooks[event] = foreign + copy.deepcopy(entries)
    result["version"] = result.get("version", desired.content.get("version", 1))
    result["hooks"] = merged_hooks
    return result


def merge_hook_manifest(
    existing: Mapping[str, Any] | None,
    rendered: RenderedHookManifest,
) -> dict[str, Any]:
    """Pure idempotent merge for any rendered host fragment."""

    if rendered.vendor == "cursor":
        return merge_cursor_hooks(existing, rendered)
    if rendered.vendor not in {"claude-code", "codex"}:
        raise ValueError(f"unsupported hook manifest vendor: {rendered.vendor!r}")

    result = copy.deepcopy(dict(existing or {}))
    current_hooks_value = result.get("hooks")
    current_hooks = dict(current_hooks_value) if isinstance(current_hooks_value, Mapping) else {}
    desired_hooks_value = rendered.content.get("hooks")
    assert isinstance(desired_hooks_value, Mapping)
    desired_commands = {
        hook.get("command")
        for groups in desired_hooks_value.values()
        if isinstance(groups, list)
        for group in groups
        if isinstance(group, Mapping)
        for hooks in (group.get("hooks"),)
        if isinstance(hooks, list)
        for hook in hooks
        if isinstance(hook, Mapping)
    }

    for event, desired_groups in desired_hooks_value.items():
        foreign_groups: list[Any] = []
        current_groups = current_hooks.get(event)
        if isinstance(current_groups, list):
            for group in current_groups:
                if not isinstance(group, Mapping):
                    foreign_groups.append(copy.deepcopy(group))
                    continue
                group_copy = copy.deepcopy(dict(group))
                commands = group_copy.get("hooks")
                if not isinstance(commands, list):
                    foreign_groups.append(group_copy)
                    continue
                foreign_commands = []
                for hook in commands:
                    command = hook.get("command") if isinstance(hook, Mapping) else None
                    owned = isinstance(command, str) and (
                        OWNED_COMMAND_MARKER in command or command in desired_commands
                    )
                    if not owned:
                        foreign_commands.append(hook)
                if foreign_commands:
                    group_copy["hooks"] = foreign_commands
                    foreign_groups.append(group_copy)
        current_hooks[event] = foreign_groups + copy.deepcopy(desired_groups)
    result["hooks"] = current_hooks
    return result


__all__ = [
    "OWNED_COMMAND_MARKER",
    "RenderedHookManifest",
    "merge_cursor_hooks",
    "merge_hook_manifest",
    "render_hook_manifest",
]
