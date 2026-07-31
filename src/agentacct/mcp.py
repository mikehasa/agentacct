from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .hooks import (
    _CONSUMER_ANCESTOR_MAX_DEPTH,
    CLAUDE_CODE_HOOK_CONTEXT_RELATIVE_PATH,
    HookContextSelection,
    process_ancestor_pids,
    select_claude_code_hook_context,
)
from .install_guide import MCP_SERVER_INSTRUCTIONS
from .service import RESERVED_CLIENT_CONTEXT_PROVENANCE_KEYS, SentinelService
from .storage import METADATA_MAX_BYTES, json_utf8_size, validate_run_id


WORK_KINDS = {"planning", "implementation", "debugging", "testing", "review", "docs", "refactor", "research", "other", "unknown"}
EVIDENCE_TYPES = {"test", "build", "lint", "typecheck", "smoke", "benchmark", "browser", "security", "artifact", "other"}
EVIDENCE_RESULTS = {"passed", "failed", "skipped", "error", "unknown"}

# A machine check's `name` has no length limit of its own, on purpose. A cap of
# 240 rejected a 241-4036 character band that recorded fine before it, and a
# ~300-character name (an agent recording the full pytest invocation it ran) is
# ordinary. The only ceiling is the shared metadata budget, which is where the
# name lands. It is never truncated either: `name` feeds the check-identity
# hash, so a truncated name forks the identity of the check it names.
#
# 4036 is measured, not assumed: binary-searching a {source, name, result} call
# puts the largest accepted `name` at 4036 characters and the first rejection at
# 4037, identically on this branch and on the 0.5.2 release it branched from.
# The band is that much narrower than the 8000 first claimed here because `name`
# lands in the budget TWICE — once as itself, once inside the summary below.
#
# Measured caveat: past the budget the size error names the LARGEST metadata
# field, and for a machine check that is the `summary` the server synthesizes
# as "<name>: <result>" — 4060 bytes against the name's own 4049 at the 4037
# boundary. So the blame is one step removed for EVERY over-budget name, not
# just extreme ones. It is still no worse than the un-named "metadata must be
# <= 8192 bytes" it replaced; the band that actually regressed is fixed.

# The files rule, published in every schema that takes `files`. It is the single
# biggest MCP rejection cause: agent harnesses hand out absolute paths, and the
# rule appeared in no description at all.
FILES_DESCRIPTION = (
    "Project-relative paths with forward slashes: no leading '/' or '~', no '..' segments. "
    "An absolute path is relativized and accepted ONLY when it lies under the project_dir supplied "
    "on this same call. One that is not is rejected rather than guessed at, except a Windows path "
    "(C:\\...), which is kept as-is with its separators normalized. An entry naming the project root "
    "itself ('.') is dropped rather than stored. Neither dropping nor a Windows path fails the call."
)

# Join keys that stay valid for a whole client session, so sections recorded on
# the same stdio server may inherit them from agentacct_attach_client_context.
# Turn/message/request ids are per-turn and must never be inherited.
INHERITABLE_CLIENT_CONTEXT_KEYS = ("client", "client_session_id", "client_transcript_id", "parent_client_session_id", "project_dir")
# The (session, transcript) id pair must come from a single inheritance source;
# mixing ids from the hook file and an agent attach could pair ids from two
# different sessions.
CLIENT_CONTEXT_ID_KEYS = ("client_session_id", "client_transcript_id")


# frozen: the sentinel_* tool names (and the sentinel_semantic_kind metadata
# key) survive the agentacct rename — historical stores/logs/transcripts
# carry them forever, and shipped instruction surfaces + user CLAUDE.md files
# quote them. Never rename; any future rename must be additive.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "agentacct_list_runs",
        "description": "List recent agentacct runs from the local store.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
            "additionalProperties": False,
        },
    },
    {
        "name": "agentacct_get_report",
        "description": "Return the JSON report for a run. Use run_id='latest' for the newest run.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string", "default": "latest"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "agentacct_record_machine_check",
        "description": "Record machine-check evidence for local work. A passed check may explicitly resolve one exact blocked event, but only as a reported resolution; it never fabricates a verified/completed outcome.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "default": "latest"},
                "name": {"type": "string", "default": "check"},
                "before_exit_code": {"type": ["integer", "null"]},
                "after_exit_code": {"type": ["integer", "null"]},
                "before_summary": {"type": ["string", "null"]},
                "after_summary": {"type": ["string", "null"]},
                "source": {"type": ["string", "null"], "maxLength": 80},
                "section_id": {"type": ["string", "null"], "maxLength": 120},
                "work_id": {"type": ["string", "null"], "maxLength": 120},
                "evidence_type": {"type": ["string", "null"], "enum": ["test", "build", "lint", "typecheck", "smoke", "benchmark", "browser", "security", "artifact", "other", None]},
                "result": {"type": ["string", "null"], "enum": ["passed", "failed", "skipped", "error", "unknown", None]},
                "summary": {"type": ["string", "null"], "maxLength": 1200},
                "command": {"type": ["string", "null"], "maxLength": 500},
                "exit_code": {"type": ["integer", "null"]},
                "artifact_ref": {"type": ["string", "null"], "maxLength": 240},
                "artifact_path": {"type": ["string", "null"], "maxLength": 500},
                "artifact_url": {"type": ["string", "null"], "maxLength": 500},
                "files": {"type": "array", "items": {"type": "string", "maxLength": 240}, "maxItems": 50, "default": [], "description": FILES_DESCRIPTION},
                "idempotency_key": {"type": ["string", "null"], "maxLength": 240},
                "client": {"type": ["string", "null"], "maxLength": 80},
                "client_session_id": {"type": ["string", "null"], "maxLength": 240},
                "project_dir": {"type": ["string", "null"], "maxLength": 1000},
                "resolves_blocked_event_id": {"type": ["string", "null"], "maxLength": 240},
                "resolution_scope": {"type": ["string", "null"], "enum": ["full", "partial", None]},
                "resolution_summary": {"type": ["string", "null"], "maxLength": 1200},
            },
            "additionalProperties": False,
        },
    },

    {
        "name": "agentacct_record_event",
        "description": "Record a redacted local integration event for the agentacct dashboard/event hub. Does not call paid APIs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "minLength": 1, "maxLength": 80},
                "event_type": {"type": "string", "minLength": 1, "maxLength": 80},
                "run_id": {"type": ["string", "null"], "maxLength": 128, "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"},
                "provider": {"type": ["string", "null"], "maxLength": 80},
                "model": {"type": ["string", "null"], "maxLength": 120},
                "estimated_input_tokens": {"type": ["integer", "null"], "minimum": 0},
                "estimated_output_tokens": {"type": ["integer", "null"], "minimum": 0},
                "estimated_cost_usd": {"type": ["number", "null"], "minimum": 0},
                "usage_confidence": {"type": ["string", "null"], "maxLength": 80},
                "cost_confidence": {"type": ["string", "null"], "maxLength": 80},
                "metadata": {"type": "object", "default": {}},
            },
            "required": ["source", "event_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agentacct_attach_client_context",
        "description": "Record local client/session/turn/message identifiers so MCP notes can later be joined to local usage logs. Does not call paid APIs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "minLength": 1, "maxLength": 80},
                "client": {"type": "string", "minLength": 1, "maxLength": 80},
                "client_session_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 240},
                "run_id": {"type": ["string", "null"], "maxLength": 128, "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"},
                "client_transcript_id": {"type": ["string", "null"], "maxLength": 240},
                "parent_client_session_id": {"type": ["string", "null"], "maxLength": 240},
                "project_dir": {"type": ["string", "null"], "maxLength": 1000},
                "turn_id": {"type": ["string", "null"], "maxLength": 240},
                "turn_index": {"type": ["integer", "null"], "minimum": 0},
                "message_id": {"type": ["string", "null"], "maxLength": 240},
                "request_id": {"type": ["string", "null"], "maxLength": 240},
                "client_event_timestamp": {"type": ["string", "null"], "maxLength": 80},
                "idempotency_key": {"type": ["string", "null"], "maxLength": 240},
                "metadata": {"type": "object", "default": {}},
            },
            "required": ["source", "client"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agentacct_record_section",
        "description": "Record a human-readable task section/chapter with optional client join keys. Use logs for token/cost truth; use this for semantic attribution.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "minLength": 1, "maxLength": 80},
                "section_id": {"type": "string", "minLength": 1, "maxLength": 120},
                "section_status": {
                    "type": "string",
                    "enum": ["started", "checkpoint", "completed", "blocked", "handed_off"],
                    "description": "started/checkpoint are in-progress; completed, blocked, and handed_off are terminal. Use handed_off (a clean stop) when the user hands the work off or continues in a new session, instead of leaving it started/checkpoint.",
                },
                "run_id": {"type": ["string", "null"], "maxLength": 128, "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"},
                "section_title": {
                    "type": ["string", "null"],
                    "maxLength": 160,
                    "description": "Short human goal for this section. `title` is accepted as an alias; if both are supplied, `section_title` wins.",
                },
                "title": {
                    "type": ["string", "null"],
                    "maxLength": 160,
                    "description": "Alias for `section_title` (the HTTP lane names this field `title`). Ignored when `section_title` is also supplied.",
                },
                "phase": {"type": ["string", "null"], "maxLength": 80},
                "kind": {"type": ["string", "null"], "enum": ["planning", "implementation", "debugging", "testing", "review", "docs", "refactor", "research", "other", "unknown", None]},
                "summary": {"type": ["string", "null"], "maxLength": 1200},
                "client": {"type": ["string", "null"], "maxLength": 80},
                "client_session_id": {"type": ["string", "null"], "maxLength": 240},
                "client_transcript_id": {"type": ["string", "null"], "maxLength": 240},
                "parent_client_session_id": {"type": ["string", "null"], "maxLength": 240},
                "project_dir": {"type": ["string", "null"], "maxLength": 1000},
                "turn_id": {"type": ["string", "null"], "maxLength": 240},
                "turn_index": {"type": ["integer", "null"], "minimum": 0},
                "message_id": {"type": ["string", "null"], "maxLength": 240},
                "request_id": {"type": ["string", "null"], "maxLength": 240},
                "client_event_timestamp": {"type": ["string", "null"], "maxLength": 80},
                "files": {"type": "array", "items": {"type": "string", "maxLength": 240}, "maxItems": 50, "default": [], "description": FILES_DESCRIPTION},
                "blocker": {"type": ["string", "null"], "maxLength": 1200},
                "next_step": {"type": ["string", "null"], "maxLength": 1200},
                "idempotency_key": {"type": ["string", "null"], "maxLength": 240},
                "metadata": {"type": "object", "default": {}},
            },
            "required": ["source", "section_id", "section_status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agentacct_record_agent_usage_debug",
        "description": "Record a debug-only usage snapshot that the agent can see about itself. This is join evidence, not billing truth, and does not add to agentacct cost totals.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "minLength": 1, "maxLength": 80},
                "client": {"type": "string", "minLength": 1, "maxLength": 80},
                "reporting_basis": {"type": "string", "enum": ["visible_client_usage", "estimated_by_agent", "unavailable"]},
                "run_id": {"type": ["string", "null"], "maxLength": 128, "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"},
                "provider": {"type": ["string", "null"], "maxLength": 80},
                "model": {"type": ["string", "null"], "maxLength": 120},
                "client_session_id": {"type": ["string", "null"], "maxLength": 240},
                "client_transcript_id": {"type": ["string", "null"], "maxLength": 240},
                "parent_client_session_id": {"type": ["string", "null"], "maxLength": 240},
                "turn_id": {"type": ["string", "null"], "maxLength": 240},
                "turn_index": {"type": ["integer", "null"], "minimum": 0},
                "message_id": {"type": ["string", "null"], "maxLength": 240},
                "request_id": {"type": ["string", "null"], "maxLength": 240},
                "client_event_timestamp": {"type": ["string", "null"], "maxLength": 80},
                "input_tokens": {"type": ["integer", "null"], "minimum": 0},
                "output_tokens": {"type": ["integer", "null"], "minimum": 0},
                "cache_creation_input_tokens": {"type": ["integer", "null"], "minimum": 0},
                "cache_read_input_tokens": {"type": ["integer", "null"], "minimum": 0},
                "cached_input_tokens": {"type": ["integer", "null"], "minimum": 0},
                "reasoning_output_tokens": {"type": ["integer", "null"], "minimum": 0},
                "total_tokens": {"type": ["integer", "null"], "minimum": 0},
                "cost_usd": {"type": ["number", "null"], "minimum": 0},
                "cost_currency": {"type": ["string", "null"], "maxLength": 16},
                "cost_basis": {"type": ["string", "null"], "maxLength": 80},
                "summary": {"type": ["string", "null"], "maxLength": 1000},
                "metadata": {"type": "object", "default": {}},
            },
            "required": ["source", "client", "reporting_basis"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agentacct_list_events",
        "description": "List recent local integration events recorded for the dashboard/event hub.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                "run_id": {"type": ["string", "null"], "maxLength": 128, "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "agentacct_get_event_summary",
        "description": "Summarize recent local integration events without returning raw event metadata. Useful for agent self-checks before continuing work.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 200},
                "run_id": {"type": ["string", "null"], "maxLength": 128, "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "agentacct_prepare_judge",
        "description": "Prepare an isolated judge package/prompt. This does not call an LLM or spend money.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "default": "latest"},
                "task_goal": {"type": "string"},
                "rubric": {"type": "string"},
                "write_package": {"type": "boolean", "default": True},
            },
            "required": ["task_goal", "rubric"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agentacct_compute_value",
        "description": "Compute advisory cost-efficiency value score from existing report, machine checks, and judge score. Does not call an LLM.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "default": "latest"},
                "budget_usd": {"type": ["number", "null"]},
            },
            "additionalProperties": False,
        },
    },
]


class InvalidParams(ValueError):
    pass


def _require_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidParams("arguments must be an object")
    return value


def _reject_unknown_keys(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise InvalidParams("unexpected argument(s): " + ", ".join(unknown))


def _limit_error(key: str, *, limit: int, received: int, unit: str = "characters") -> InvalidParams:
    """The single shape every size/limit rejection in this module uses.

    It always states the limit AND what arrived. Without the received size a
    caller can only shrink blindly — the real incident behind this helper cost
    five retries (2039 -> 1904 -> 1664 -> 1614 -> 1477 -> 1172 accepted). The
    HTTP lane already reports both via pydantic; this matches it. Sizes only:
    never echo caller content back into an error string.
    """
    return InvalidParams(f"{key} must be <= {limit} {unit} (received {received})")


def _optional_str(args: dict[str, Any], key: str, default: str) -> str:
    value = args.get(key, default)
    if not isinstance(value, str) or not value:
        raise InvalidParams(f"{key} must be a non-empty string")
    return value


def _required_str(args: dict[str, Any], key: str) -> str:
    if key not in args:
        raise InvalidParams(f"missing required argument: {key}")
    return _optional_str(args, key, "")


def _optional_int(args: dict[str, Any], key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    value = args.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidParams(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise InvalidParams(f"{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise InvalidParams(f"{key} must be <= {maximum}")
    return value


def _optional_nullable_int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidParams(f"{key} must be an integer or null")
    return value


def _optional_nullable_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidParams(f"{key} must be a string or null")
    return value



def _optional_nonnegative_int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidParams(f"{key} must be an integer or null")
    if value < 0:
        raise InvalidParams(f"{key} must be >= 0")
    return value


def _optional_nonnegative_float(args: dict[str, Any], key: str) -> float | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise InvalidParams(f"{key} must be a number or null")
    value = float(value)
    if value < 0 or not math.isfinite(value):
        raise InvalidParams(f"{key} must be a finite number >= 0")
    return value


def _optional_limited_str(args: dict[str, Any], key: str, default: str | None = None, *, max_length: int = 120) -> str | None:
    value = args.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidParams(f"{key} must be a non-empty string or null")
    if len(value) > max_length:
        raise _limit_error(key, limit=max_length, received=len(value))
    return value


def _required_limited_str(args: dict[str, Any], key: str, *, max_length: int = 120) -> str:
    if key not in args:
        raise InvalidParams(f"missing required argument: {key}")
    value = _optional_limited_str(args, key, None, max_length=max_length)
    if value is None:
        raise InvalidParams(f"{key} must be a non-empty string")
    return value


def _optional_run_id(args: dict[str, Any], key: str, default: str | None = None) -> str | None:
    value = args.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidParams(f"{key} must be a non-empty string or null")
    try:
        validate_run_id(value)
    except ValueError as exc:
        raise InvalidParams(str(exc)) from exc
    return value


def _optional_metadata(args: dict[str, Any]) -> dict[str, Any]:
    value = args.get("metadata", {})
    if not isinstance(value, dict):
        raise InvalidParams("metadata must be an object")
    _validate_metadata_size(value)
    return value


def _json_utf8_size(value: Any) -> int:
    """This lane's strictness applied to the shared measure.

    The measure itself lives in storage.py because all three write surfaces
    have to reach the same accept/reject decision; ``allow_nan=False`` is the
    part that is local to MCP and the CLI, whose callers may not send
    non-standard JSON constants.
    """
    return json_utf8_size(value, allow_nan=False)


def _metadata_size_error(value: dict[str, Any], size: int) -> InvalidParams:
    """Size rejection that names the field to shrink.

    The old message named `metadata` — a parameter most callers never passed,
    since the bulk is usually a validated argument (summary, blocker,
    next_step, name) the server merged in. Naming the largest field, with its
    byte size, is the difference between a fixable error and a guessing game,
    and it is why no argument needs a private length cap on top of this one.
    The key name is caller-controlled, so it is clipped before it reaches the
    error string.
    """
    largest_key = ""
    largest_size = 0
    for key, item in value.items():
        item_size = _json_utf8_size({key: item})
        if item_size > largest_size:
            largest_key, largest_size = str(key), item_size
    detail = f"; largest field is {largest_key[:60]} at {largest_size} bytes" if largest_key else ""
    return InvalidParams(
        f"metadata must be <= {METADATA_MAX_BYTES} bytes when JSON encoded (received {size} bytes){detail}"
    )


def _validate_metadata_size(value: dict[str, Any]) -> None:
    try:
        size = _json_utf8_size(value)
    except ValueError as exc:
        raise InvalidParams("metadata must be strict JSON") from exc
    if size > METADATA_MAX_BYTES:
        raise _metadata_size_error(value, size)


# --- Mangled tool-call detection (WARN ONLY) ---------------------------------
# When a client's tool-call parser mangles a call, arguments arrive absorbed
# into another argument's value as literal text ("...done.</files>"). Observed
# three times in one session, twice AFTER the agent had already diagnosed it.
# agentacct warns and records the suspicion; it never rejects and never
# repairs, because repairing would fabricate fields the agent never wrote.

# Server-authored marker (listed in RESERVED_CONTEXT_STRIP_KEYS, so a caller
# cannot stamp its own events with it).
MANGLED_TOOL_CALL_METADATA_KEY = "mangled_tool_call_suspected_fields"

# The free-text arguments a mangled parameter can be absorbed into, per tool.
SECTION_NARRATIVE_KEYS = ("summary", "blocker", "next_step", "section_title", "title")
MACHINE_CHECK_NARRATIVE_KEYS = ("summary", "before_summary", "after_summary", "resolution_summary", "command")

# Property names that are NOT eligible to be suspected, however they appear in
# free text: the ones that are also element names in the HTML or SVG
# vocabularies, which is what an agent describing web work actually writes.
# Hand-picking `title` alone was the inconsistency — it excluded one markup
# name by that reasoning and left `summary` and `metadata`, which are markup
# names by the same reasoning, armed.
#
# Calibrated READ-ONLY against the real 6261-event global ledger, scoped to the
# 3535 events these two tools write (1956 section, 1579 evidence) and to the
# narrative values the detector actually reads:
#
#   * `</title>`, `</metadata>`, `</source>`: 0 occurrences.
#   * `</summary>`: 1 occurrence per lane, both inside confirmed mangled calls
#     that DID supply `summary`, so the detector could not have fired on it.
#   * `<title>` (opening form): 2 occurrences in ordinary prose — "tooltips =
#     SVG <title>" and "dashboard <title>". That is the measured evidence that
#     these names collide with real writing; only the closing form has yet to
#     turn up in 3535 events.
#
# So excluding these four costs zero measured true positives. `source` is in the
# set on the same footing as the other three: `<source>` is an HTML element name,
# and it is the property name that collides with real writing most often here —
# a bare-word detector fires on it in 170 of the 1956 section events, ahead of
# `files` at 130 and every other property name. Its membership is load-bearing
# rather than bookkeeping: `source` is required on agentacct_record_section, but
# agentacct_record_machine_check declares no required properties and reads it as
# optional, so a machine-check call that omits it leaves `source` genuinely
# unsupplied and therefore eligible to be suspected.
MANGLE_DETECTOR_INELIGIBLE_PROPERTIES = frozenset({"title", "summary", "metadata", "source"})


def _tool_property_names(tool_name: str) -> frozenset[str]:
    for tool in TOOLS:
        if tool.get("name") == tool_name:
            return frozenset(tool["inputSchema"].get("properties", {}))
    return frozenset()


def _detect_mangled_tool_call_fields(tool_name: str, args: dict[str, Any], narrative_keys: Sequence[str]) -> list[str]:
    """This tool's own argument names that appear as CLOSING tags in free text.

    Closing tags only, and only for properties the call did NOT supply and that
    are not in MANGLE_DETECTOR_INELIGIBLE_PROPERTIES. The looser forms were
    re-measured against the real 6261-event ledger with the CURRENT property
    set, and are unusable: scoped to the 1956 section events, a bare-word
    detector fires on "source" in 170 of them and on "files" in 130, and an
    opening-tag detector fires on `<title>` in 2 events of ordinary prose about
    SVG tooltips and the dashboard's page title.

    The closing-tag form scores 2 true positives and 0 false positives across
    all 3535 section+evidence events. Exactly two events contain any closing
    tag at all, and both are genuine mangled calls that absorbed
    `</summary><next_step>...</next_step><files>[...]</files><project_dir>...`
    into the summary; the names that fired are files, next_step and
    project_dir. (The previous figure here, "1 true positive across 1955
    section events", undercounted: the second true positive is in the evidence
    lane, and the bare-word counts it quoted were measured over all 6261
    events rather than the section subset it credited them to.)

    Two limits, stated because the numbers alone overstate the case. No
    property name has EVER appeared as a closing tag in real prose, so this
    ledger licenses no exclusion on its own — the ineligible set above rests on
    the measured `<title>` prose plus the vocabulary rule that generalizes it.
    And the corpus is agentacct dogfooding itself: only 51 of the 3535 events
    mention SVG/HTML/XML at all, so it is weak evidence that the names left
    armed are safe on markup-heavy prose. `client` is the known gap — it fires
    on XML prose but is in neither vocabulary and has 0 occurrences here, so
    nothing measured justifies removing it. The detector will also still fire
    on meta-work about agentacct itself, which is acceptable for a warning that
    never rejects and never repairs.
    """
    missing = _tool_property_names(tool_name) - set(args) - MANGLE_DETECTOR_INELIGIBLE_PROPERTIES
    if not missing:
        return []
    suspected: set[str] = set()
    for key in narrative_keys:
        value = args.get(key)
        if isinstance(value, str):
            suspected.update(name for name in missing if f"</{name}>" in value)
    return sorted(suspected)


def _mangled_tool_call_warnings(fields: Sequence[str]) -> list[str]:
    return [
        f"Possible mangled tool call: a closing </{field}> tag appears inside a free-text argument, but "
        f"{field} was not supplied as an argument. agentacct recorded your text verbatim and did NOT "
        f"repair it; re-send with {field} as its own argument if it was meant to be one."
        for field in fields
    ]


# Context keys whose collision with free-form metadata is treated as an
# attempted smuggle of join identity / semantic kind / provenance: stripped
# and recorded in reserved_context_keys_stripped, INCLUDING when the caller
# never passed the validated argument (absence is server-owned too).
# Benign display-field collisions (summary, files, section_title, ...) are
# NOT reserved: the server's validated value simply wins when present, and
# the caller's metadata value is kept when the argument was not supplied —
# honest agent metadata must not be deleted or labelled as smuggling.
RESERVED_CONTEXT_STRIP_KEYS = frozenset(
    {
        "sentinel_semantic_kind",
        "usage_join_strategy",
        "client",
        "client_session_id",
        "client_transcript_id",
        "parent_client_session_id",
        MANGLED_TOOL_CALL_METADATA_KEY,
        *RESERVED_CLIENT_CONTEXT_PROVENANCE_KEYS,
    }
)


def _metadata_with_context(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Merge free-form metadata with the server-built context. Context wins.

    Join-id/provenance keys are server-owned, INCLUDING their absence: a
    caller must not be able to smuggle them (client_session_id,
    client_transcript_id, provenance markers, ...) through free-form metadata
    when it did not pass the validated argument. Smuggled keys are stripped
    and recorded in metadata.reserved_context_keys_stripped so the write
    stays debuggable. Benign display keys colliding with supplied context are
    plainly overwritten by the server value; when the validated argument was
    not supplied, the caller's metadata value is kept.
    """

    metadata = dict(_optional_metadata(args))
    # The strip record itself is server-authored.
    metadata.pop("reserved_context_keys_stripped", None)
    stripped = sorted(key for key in context if key in metadata and key in RESERVED_CONTEXT_STRIP_KEYS)
    for key in stripped:
        metadata.pop(key, None)
    if stripped:
        metadata["reserved_context_keys_stripped"] = stripped
    for key, value in context.items():
        if value is not None and value != []:
            metadata[key] = value
    _validate_metadata_size(metadata)
    return metadata


def _required_choice(args: dict[str, Any], key: str, choices: set[str], *, max_length: int = 80) -> str:
    value = _required_limited_str(args, key, max_length=max_length)
    if value not in choices:
        raise InvalidParams(f"{key} must be one of: " + ", ".join(sorted(choices)))
    return value


def _optional_choice(args: dict[str, Any], key: str, choices: set[str], default: str | None = None, *, max_length: int = 80) -> str | None:
    value = _optional_limited_str(args, key, default, max_length=max_length)
    if value is None:
        return None
    if value not in choices:
        raise InvalidParams(f"{key} must be one of: " + ", ".join(sorted(choices)))
    return value


def _optional_limited_str_list(args: dict[str, Any], key: str, *, max_items: int = 50, max_length: int = 240) -> list[str]:
    value = args.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidParams(f"{key} must be an array")
    if len(value) > max_items:
        raise _limit_error(key, limit=max_items, received=len(value), unit="items")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise InvalidParams(f"{key}[{index}] must be a non-empty string")
        if len(item) > max_length:
            raise _limit_error(f"{key}[{index}]", limit=max_length, received=len(item))
        result.append(item)
    return result


_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:/")


def _looks_absolute(path: str) -> bool:
    """Absolute for validation purposes: a POSIX root, a home shortcut, or a
    Windows drive letter. The drive-letter case matters because the value has
    already had backslashes folded to forward slashes, so PurePosixPath alone
    would read ``C:/repo/a.py`` as a relative path and store it as one."""

    return path.startswith(("/", "~")) or bool(_WINDOWS_DRIVE_PREFIX.match(path))


def _relative_offset_under_project_dir(path: str, root: str | None) -> int | None:
    """Index in ``path`` where the part relative to ``root`` begins, or None when
    containment is not provable.

    An offset rather than the substring itself, because the caller holds two
    index-aligned views of the same value — what arrived, and the
    backslash-folded copy the containment check reads — and both have to be cut
    at the same place for the stored path to keep the separators the caller
    sent. See _optional_project_relative_files for why that matters.

    Purely lexical: no filesystem access and no symlink resolution. That is the
    correct boundary here — this decides which STRING lands in the ledger, not
    which file gets opened, and project_dir routinely names a directory that
    does not exist on the machine reading the store later. Both values are
    caller-controlled, so nothing is inferred: a root that is not itself
    absolute, or a path that is not under it, returns None and the caller
    decides. Any ``..`` surviving past the offset is caught by the escape check
    at the call site.
    """
    if not root or not _looks_absolute(root) or root.startswith("~"):
        return None
    if path == root:
        # The project root named absolutely. The empty remainder is what the
        # caller sees dropped, so "/repo" and "/repo/" behave alike instead of
        # one being fatal and the other cosmetic.
        return len(path)
    prefix = root + "/"
    if not path.startswith(prefix):
        return None
    # A redundant separator ("/repo//etc/passwd") is the same POSIX path as
    # "/repo/etc/passwd", but leaving the leading slash on the remainder would
    # rebuild an absolute-looking "//etc/passwd" in the ledger.
    offset = len(prefix)
    while offset < len(path) and path[offset] == "/":
        offset += 1
    return offset


def _not_project_relative_error(key: str, index: int) -> InvalidParams:
    return InvalidParams(
        f"{key}[{index}] must be project-relative: forward slashes, no leading '/' or '~', "
        "no '..' segments. Pass the path relative to the repository root, or supply "
        "project_dir on this call so an absolute path under it can be relativized."
    )


def _optional_project_relative_files(
    args: dict[str, Any], key: str = "files", *, project_dir: str | None = None
) -> list[str]:
    """Validate the files list and normalize it into project-relative paths.

    Absolute paths are the single biggest MCP rejection cause — agent harnesses
    instruct absolute paths and the rule was published nowhere — so an absolute
    path is relativized instead of rejected when it provably lies under the
    project_dir declared ON THIS CALL. Only the explicit argument counts: an
    inherited or session-attached project_dir could belong to a different
    repository, and a path relativized against the wrong root is a silently
    wrong ledger row, which is worse than a rejection. The relativized
    remainder is then put through the SAME check as any other value, so
    "/repo/~/x" cannot deposit a "~/x" the published rule calls impossible.

    Three rules keep a stored value honest about the file it names:

    * Backslashes are folded for VALIDATION only, and on EVERY branch. On POSIX
      ``weird\\name.py`` is a legal filename, and folding it into the stored
      value would merge it with a genuinely different file called
      ``weird/name.py``. That held for a relative entry but not for a
      relativized one, so ``/repo/weird\\name.py`` under project_dir="/repo"
      stored ``weird/name.py`` — the same silent merge, one branch over. Both
      branches now cut the arrived value and the folded copy at the same index.
      The exception is a Windows path, where a backslash IS a separator: there
      the folded copy is the value, so ``C:\\repo\\src\\a.py`` under
      project_dir="C:\\repo" still stores ``src/a.py`` rather than fragmenting
      against the same file recorded with forward slashes.
    * An entry naming the project root itself (".", "./", or an absolute path
      equal to project_dir) is DROPPED, not rejected. It names no file, so
      storing it would be a row for a path that does not exist — but failing
      the whole call, and losing a real section or machine check, over one
      cosmetic entry is the behaviour this validator exists to stop.
    * A Windows absolute path that no project_dir on the call can relativize is
      STORED, not rejected — verbatim but for the separator folding above, so
      ``C:\\repo\\src\\a.py`` lands as ``C:/repo/src/a.py``. Recognizing drive
      letters as absolute was right — it stopped ``C:/repo/a.py`` being filed as
      a relative path — but pairing it with a rejection turned a shape that
      recorded fine before (measured: the 0.5.2 release stores it, with the
      backslashes it arrived with) into a whole-call failure, which is exactly
      the anti-pattern this validator exists to remove. It is the one absolute
      form with no POSIX reading, so keeping it invents nothing; a POSIX
      absolute path without a usable project_dir stays a rejection, as it was
      before this branch. The escape check below still applies, so
      ``C:\\repo\\..\\x`` is still fatal.
    """
    files = _optional_limited_str_list(args, key, max_items=50, max_length=240)
    root = project_dir.replace("\\", "/").rstrip("/") if project_dir else None
    normalized_files: list[str] = []
    for index, item in enumerate(files):
        folded = item.replace("\\", "/")
        # On a Windows path a backslash is a separator, so the folded copy IS
        # the value; anywhere else it is an ordinary filename character and
        # folding it would merge two different POSIX files.
        windows_path = bool(_WINDOWS_DRIVE_PREFIX.match(folded))
        stored = folded if windows_path else item
        if _looks_absolute(folded):
            offset = _relative_offset_under_project_dir(folded, root)
            if offset is not None:
                # Cut both index-aligned views at the same place: `folded` is
                # what the remaining checks read, `stored` is what the ledger
                # gets, and only the latter keeps the caller's separators.
                folded, stored = folded[offset:], stored[offset:]
                if _looks_absolute(folded):
                    # "/repo/~/x" -> "~/x", "/repo/C:/x" -> "C:/x": still not a
                    # project-relative path, and the schema says so.
                    raise _not_project_relative_error(key, index)
            elif not windows_path:
                raise _not_project_relative_error(key, index)
            # else: a Windows absolute path this call cannot relativize. Stored
            # as sent rather than rejected — see the third rule above.
        if ".." in PurePosixPath(folded).parts:
            raise InvalidParams(f"{key}[{index}] must not escape the project directory: '..' segments are not allowed")
        stored_parts = PurePosixPath(stored).parts
        if not stored_parts:
            continue
        normalized_files.append("/".join(stored_parts))
    return normalized_files


def _client_context_metadata(args: dict[str, Any], *, require_client: bool, require_session: bool) -> dict[str, Any]:
    client = (
        _required_limited_str(args, "client", max_length=80)
        if require_client
        else _optional_limited_str(args, "client", None, max_length=80)
    )
    client_session_id = (
        _required_limited_str(args, "client_session_id", max_length=240)
        if require_session
        else _optional_limited_str(args, "client_session_id", None, max_length=240)
    )
    return {
        "client": client,
        "client_session_id": client_session_id,
        "client_transcript_id": _optional_limited_str(args, "client_transcript_id", None, max_length=240),
        "parent_client_session_id": _optional_limited_str(args, "parent_client_session_id", None, max_length=240),
        "project_dir": _optional_limited_str(args, "project_dir", None, max_length=1000),
        "turn_id": _optional_limited_str(args, "turn_id", None, max_length=240),
        "turn_index": _optional_nonnegative_int(args, "turn_index"),
        "message_id": _optional_limited_str(args, "message_id", None, max_length=240),
        "request_id": _optional_limited_str(args, "request_id", None, max_length=240),
        "client_event_timestamp": _optional_limited_str(args, "client_event_timestamp", None, max_length=80),
        "idempotency_key": _optional_limited_str(args, "idempotency_key", None, max_length=240),
    }


def _agent_usage_debug_metadata(args: dict[str, Any], *, provider: str | None, model: str | None) -> dict[str, Any]:
    reporting_basis = _required_choice(args, "reporting_basis", {"visible_client_usage", "estimated_by_agent", "unavailable"})
    summary = _optional_limited_str(args, "summary", None, max_length=1000) or f"Agent usage debug snapshot: {reporting_basis}"
    cost_usd = _optional_nonnegative_float(args, "cost_usd")
    cost_currency = _optional_limited_str(args, "cost_currency", None, max_length=16)
    if cost_usd is not None and cost_currency is None:
        cost_currency = "USD"
    return {
        "sentinel_semantic_kind": "agent_usage_debug",
        "usage_join_strategy": "agent_reported_usage_debug",
        "reporting_basis": reporting_basis,
        "summary": summary,
        "provider": provider,
        "model": model,
        "agent_reported_input_tokens": _optional_nonnegative_int(args, "input_tokens"),
        "agent_reported_output_tokens": _optional_nonnegative_int(args, "output_tokens"),
        "agent_reported_cache_creation_input_tokens": _optional_nonnegative_int(args, "cache_creation_input_tokens"),
        "agent_reported_cache_read_input_tokens": _optional_nonnegative_int(args, "cache_read_input_tokens"),
        "agent_reported_cached_input_tokens": _optional_nonnegative_int(args, "cached_input_tokens"),
        "agent_reported_reasoning_output_tokens": _optional_nonnegative_int(args, "reasoning_output_tokens"),
        "agent_reported_total_tokens": _optional_nonnegative_int(args, "total_tokens"),
        "agent_reported_cost_usd": cost_usd,
        "agent_reported_cost_currency": cost_currency,
        "agent_reported_cost_basis": _optional_limited_str(args, "cost_basis", None, max_length=80),
        **_client_context_metadata(args, require_client=True, require_session=False),
    }


def _context_join_hint_quality(context: dict[str, Any], *, run_id: str | None) -> str:
    inherited = set(context.get("client_context_inherited_keys") or [])
    if (context.get("client_session_id") and "client_session_id" not in inherited) or (
        context.get("client_transcript_id") and "client_transcript_id" not in inherited
    ):
        return "exact"
    if context.get("client_session_id") or context.get("client_transcript_id"):
        if context.get("client_context_source") == "claude_code_hook":
            # Ids captured by the Claude Code hook: client-derived and fresh,
            # but not bound to this MCP server's session, so still not exact.
            return "client_derived"
        # Ids exist but only via inheritance from a prior attach on this MCP
        # server; freshness is unproven, so this never counts as exact.
        return "inherited"
    if run_id:
        return "partial"
    if context.get("parent_client_session_id") or context.get("project_dir"):
        return "weak"
    return "missing"


def _recommended_context_next_step(quality: str) -> str:
    if quality == "exact":
        return "Record work sections with the same client_session_id so agentacct can join imported usage exactly."
    if quality == "client_derived":
        return "Hook-captured ids attribute at high confidence; pass client_session_id explicitly on sections for exact attribution."
    if quality == "inherited":
        return "Pass client_session_id explicitly (or re-attach with the current session ids) to upgrade attribution from medium to exact."
    if quality == "partial":
        return "Record work sections with this run_id and add client_session_id when the client exposes it."
    if quality == "weak":
        return "Record work sections with client_session_id when possible; project_dir-only joins stay low confidence."
    return "Attach client_session_id, run_id, or project_dir before recording work."


def _context_join_warnings(quality: str) -> list[str]:
    if quality == "exact":
        return []
    if quality == "client_derived":
        # Hook-derived ids are the intended good path for Claude Code; the
        # ledger attributes them at high confidence with clear provenance.
        return []
    if quality == "inherited":
        return [
            "Join keys were inherited from the last agentacct_attach_client_context on this MCP session; agentacct attributes inherited joins at medium confidence, never exact, because freshness across conversations cannot be proven. Pass client_session_id/client_transcript_id explicitly, or re-attach at the start of each conversation, for exact attribution."
        ]
    if quality == "partial":
        return [
            "run_id groups agentacct events but never attributes imported usage. Add client_session_id or client_transcript_id for exact attribution."
        ]
    return [
        "No client_session_id or client_transcript_id: imported usage cannot be attributed to this context. "
        "Pass the client session id if the client exposes it (Claude Code hooks receive session_id/transcript_path; Codex does not expose its thread id in-band)."
    ]


def _machine_result_from_exit_code(exit_code: int | None) -> str:
    if exit_code is None:
        return "unknown"
    return "passed" if exit_code == 0 else "failed"


def _optional_bool(args: dict[str, Any], key: str, default: bool) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise InvalidParams(f"{key} must be a boolean")
    return value


def _optional_positive_float(args: dict[str, Any], key: str) -> float | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise InvalidParams(f"{key} must be a positive number or null")
    value = float(value)
    if value <= 0:
        raise InvalidParams(f"{key} must be > 0")
    return value


# sentinel-object default (the programming pattern, not the old brand)
# distinguishing "not supplied" from an explicit None in the hook-binding test
# seams below.
_AUTO: Any = object()


class SentinelMCPServer:
    def __init__(
        self,
        *,
        store_dir: Path | str,
        hook_env_session_id: str | None | Any = _AUTO,
        hook_consumer_ancestor_pids: Sequence[int] | None | Any = _AUTO,
    ) -> None:
        # No silent home-store default: callers must resolve the store first
        # (see agentacct.store_resolution.resolve_store_dir).
        self.service = SentinelService(store_dir)
        # Bindings used to prove WHICH concurrent Claude Code session's hook
        # context belongs to this server. Captured once at construction; the
        # keyword seams exist for tests and default to production behavior.
        if hook_env_session_id is _AUTO:
            self._hook_env_session_id: str | None = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
        else:
            self._hook_env_session_id = hook_env_session_id
        self._hook_consumer_ancestor_pids_seed = hook_consumer_ancestor_pids
        self._hook_consumer_ancestor_pids_cache: list[int] | None = None
        # Session-scoped join context. The most recent attach_client_context
        # describes the current client session. Every attach attempt REPLACES
        # it (never merges, cleared even on a failed attach) so a new session
        # on a long-lived server cannot keep a stale session id. Limitation:
        # clients that reuse one server process across conversations (e.g.
        # Claude Code /clear) leak the old ids to sections if the new
        # conversation never attaches; inherited keys therefore always carry
        # client_context_inherited_keys provenance in event metadata.
        self._attached_client_context: dict[str, Any] = {}
        self._attached_client_context_event_id: str | None = None

    def _remember_attached_client_context(self, context: dict[str, Any], event_id: str | None) -> None:
        self._attached_client_context = {
            key: context[key] for key in INHERITABLE_CLIENT_CONTEXT_KEYS if context.get(key) is not None
        }
        self._attached_client_context_event_id = event_id

    def _inherit_attached_client_context(self, context: dict[str, Any], *, keys: tuple[str, ...] = INHERITABLE_CLIENT_CONTEXT_KEYS) -> list[str]:
        """Fill missing session-scoped join keys from the last attached context.

        Explicit arguments always win; only keys the caller left unset are
        filled. Inherited keys are reported so the event can carry provenance.
        """
        inherited: list[str] = []
        for key in keys:
            if context.get(key) is None and self._attached_client_context.get(key) is not None:
                context[key] = self._attached_client_context[key]
                inherited.append(key)
        return inherited

    def _consumer_ancestor_pids(self) -> Sequence[int]:
        """This server's own process ancestry, for hook-context pid lineage.

        Computed lazily and cached: select_claude_code_hook_context only
        invokes this when several fresh contexts need disambiguation, so the
        common single-session path never pays for a ps call.
        """
        if self._hook_consumer_ancestor_pids_seed is not _AUTO:
            seed = self._hook_consumer_ancestor_pids_seed
            return list(seed) if seed else []
        if self._hook_consumer_ancestor_pids_cache is None:
            self._hook_consumer_ancestor_pids_cache = process_ancestor_pids(max_depth=_CONSUMER_ANCESTOR_MAX_DEPTH)
        return self._hook_consumer_ancestor_pids_cache

    def _select_hook_client_context(self) -> HookContextSelection:
        return select_claude_code_hook_context(
            self.service.store.root,
            env_session_id=self._hook_env_session_id,
            consumer_ancestor_pids=self._consumer_ancestor_pids,
        )

    def _inherit_hook_client_context(
        self, context: dict[str, Any], *, source: str | None = None, allow_ids: bool = True
    ) -> tuple[list[str], HookContextSelection | None]:
        """Fill missing join keys from the hook-captured client context.

        Applies only when the effective client is claude-code, or the client
        is unset and the section source names a claude client. Hook ids are
        client-derived — the same source the usage importer reads — so they
        outrank agent-reported attach context, but they are still not
        session-bound to this MCP server, so downstream attribution stays
        below exact. Ids are only filled when allow_ids is True (the caller
        supplied neither id): the (session, transcript) pair must never mix
        caller-supplied and inherited values.

        Concurrent-session guard: when several fresh hook contexts exist and
        none can be proven to belong to this server's session, NOTHING is
        inherited (not even client) and the refusal is stamped so the missing
        ids are explained — missing attribution always beats wrong.
        """
        effective_client = context.get("client") or self._attached_client_context.get("client")
        if effective_client is not None:
            if effective_client != "claude-code":
                return [], None
        elif not str(source or "").lower().replace("_", "-").startswith("claude"):
            return [], None
        selection = self._select_hook_client_context()
        if selection.status == "none":
            return [], None
        if selection.status == "refused":
            if allow_ids:
                # Server-authored refusal record; only stamped when
                # inheritance would actually have been attempted (a caller
                # that passed its own id never needed the hook context).
                context["client_context_inheritance_refused"] = "concurrent_claude_code_hook_contexts"
                context["hook_context_fresh_count"] = selection.fresh_count
            return [], selection
        hook_context = selection.context or {}
        # An explicitly passed id that disagrees with the hook context means
        # the caller is describing a different session; stay out entirely.
        for key in CLIENT_CONTEXT_ID_KEYS:
            if context.get(key) is not None and hook_context.get(key) and context[key] != hook_context[key]:
                return [], None
        # Privacy: the hook context carries no path fields (only a basename
        # project_label for display), so sections never inherit a raw
        # project_dir from the hook.
        keys = ("client", *CLIENT_CONTEXT_ID_KEYS) if allow_ids else ("client",)
        inherited: list[str] = []
        for key in keys:
            if context.get(key) is None and hook_context.get(key):
                context[key] = hook_context[key]
                inherited.append(key)
        return inherited, selection

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        try:
            if method == "initialize":
                return self._response(msg_id, build_initialize_result(message.get("params", {})))
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                return self._response(msg_id, {"tools": TOOLS})
            if method == "tools/call":
                raw_params = message.get("params", {})
                params = _require_object(raw_params)
                arguments = params.get("arguments", {})
                if arguments is None:
                    arguments = {}
                result = self.call_tool(params.get("name"), arguments)
                return self._response(msg_id, result)
            return self._error(msg_id, -32601, f"Unknown method: {method}")
        except FileNotFoundError as exc:
            return self._error(msg_id, -32004, str(exc))
        except InvalidParams as exc:
            return self._error(msg_id, -32602, str(exc))
        except ValueError as exc:
            return self._error(msg_id, -32602, str(exc))
        except Exception as exc:  # noqa: BLE001 - MCP errors must be serialized, not crash stdio.
            return self._error(msg_id, -32000, str(exc))

    def call_tool(self, name: Any, arguments: Any) -> dict[str, Any]:
        arguments = _require_object(arguments)
        if name == "agentacct_list_runs":
            _reject_unknown_keys(arguments, {"limit"})
            limit = _optional_int(arguments, "limit", 20, minimum=1, maximum=100)
            payload = {"runs": self.service.list_runs(limit=limit)}
        elif name == "agentacct_get_report":
            _reject_unknown_keys(arguments, {"run_id"})
            payload = self.service.get_report(_optional_str(arguments, "run_id", "latest"))
        elif name == "agentacct_record_machine_check":
            _reject_unknown_keys(
                arguments,
                {
                    "run_id",
                    "name",
                    "before_exit_code",
                    "after_exit_code",
                    "before_summary",
                    "after_summary",
                    "source",
                    "section_id",
                    "work_id",
                    "evidence_type",
                    "result",
                    "summary",
                    "command",
                    "exit_code",
                    "artifact_ref",
                    "artifact_path",
                    "artifact_url",
                    "files",
                    "idempotency_key",
                    "client",
                    "client_session_id",
                    "project_dir",
                    "resolves_blocked_event_id",
                    "resolution_scope",
                    "resolution_summary",
                },
            )
            payload: dict[str, Any] = {}
            has_evidence_fields = any(
                key in arguments
                for key in {
                    "source",
                    "section_id",
                    "work_id",
                    "evidence_type",
                    "result",
                    "summary",
                    "command",
                    "exit_code",
                    "artifact_ref",
                    "artifact_path",
                    "artifact_url",
                    "files",
                    "idempotency_key",
                    "client",
                    "client_session_id",
                    "project_dir",
                    "resolves_blocked_event_id",
                    "resolution_scope",
                    "resolution_summary",
                }
            )
            has_outcome_fields = any(key in arguments for key in {"before_exit_code", "after_exit_code", "before_summary", "after_summary"}) or not has_evidence_fields
            run_id = _optional_str(arguments, "run_id", "latest")
            check_name = _optional_str(arguments, "name", "check")
            before_exit_code = _optional_nullable_int(arguments, "before_exit_code")
            after_exit_code = _optional_nullable_int(arguments, "after_exit_code")
            before_summary = _optional_nullable_str(arguments, "before_summary")
            after_summary = _optional_nullable_str(arguments, "after_summary")
            resolved_run_id = self.service.store.latest_run_id() if has_outcome_fields and run_id == "latest" else run_id
            if has_outcome_fields:
                payload["outcome"] = self.service.record_machine_check(
                    resolved_run_id,
                    name=check_name,
                    before_exit_code=before_exit_code,
                    after_exit_code=after_exit_code,
                    before_summary=before_summary,
                    after_summary=after_summary,
                )
            source = _optional_limited_str(arguments, "source", None, max_length=80)
            if source is not None or has_evidence_fields or has_outcome_fields:
                result = _optional_choice(arguments, "result", EVIDENCE_RESULTS, None)
                if result is None:
                    result = _machine_result_from_exit_code(_optional_nullable_int(arguments, "exit_code"))
                if result == "unknown" and after_exit_code is not None:
                    result = _machine_result_from_exit_code(after_exit_code)
                evidence_summary = (
                    _optional_limited_str(arguments, "summary", None, max_length=1200)
                    or after_summary
                    or before_summary
                    or f"{check_name}: {result}"
                )
                resolves_blocked_event_id = _optional_limited_str(
                    arguments, "resolves_blocked_event_id", None, max_length=240
                )
                resolution_scope = _optional_choice(
                    arguments, "resolution_scope", {"full", "partial"}, None
                )
                resolution_summary = _optional_limited_str(
                    arguments, "resolution_summary", None, max_length=1200
                )
                resolution_requested = any(
                    value is not None
                    for value in (
                        resolves_blocked_event_id,
                        resolution_scope,
                        resolution_summary,
                    )
                )
                if resolution_requested and not all(
                    value is not None
                    for value in (
                        resolves_blocked_event_id,
                        resolution_scope,
                        resolution_summary,
                    )
                ):
                    raise InvalidParams(
                        "resolves_blocked_event_id, resolution_scope, and resolution_summary must be supplied together"
                    )
                evidence_exit_code = _optional_nullable_int(arguments, "exit_code")
                resolution_objective_basis: str | None = None
                if resolution_requested:
                    if result != "passed":
                        raise InvalidParams("a blocker resolution requires result=passed")
                    if source is None:
                        raise InvalidParams("a blocker resolution requires an explicit source")
                    if _optional_limited_str(arguments, "section_id", None, max_length=120) is None:
                        raise InvalidParams("a blocker resolution requires section_id")
                    if _optional_limited_str(arguments, "project_dir", None, max_length=1000) is None:
                        raise InvalidParams("a blocker resolution requires project_dir")
                    if evidence_exit_code == 0:
                        resolution_objective_basis = "exit_code"
                    else:
                        for key in ("artifact_ref", "artifact_path", "artifact_url"):
                            if _optional_limited_str(
                                arguments,
                                key,
                                None,
                                max_length=500 if key != "artifact_ref" else 240,
                            ) is not None:
                                resolution_objective_basis = key
                                break
                    if resolution_objective_basis is None:
                        raise InvalidParams(
                            "a blocker resolution requires exit_code=0 or an artifact_ref/artifact_path/artifact_url"
                        )
                evidence_project_dir = _optional_limited_str(arguments, "project_dir", None, max_length=1000)
                mangled_fields = _detect_mangled_tool_call_fields(
                    "agentacct_record_machine_check", arguments, MACHINE_CHECK_NARRATIVE_KEYS
                )
                evidence_context = {
                    "sentinel_semantic_kind": "evidence",
                    "evidence_type": _optional_choice(arguments, "evidence_type", EVIDENCE_TYPES, "other"),
                    "result": result,
                    "summary": evidence_summary,
                    "name": check_name,
                    "section_id": _optional_limited_str(arguments, "section_id", None, max_length=120),
                    "work_id": _optional_limited_str(arguments, "work_id", None, max_length=120),
                    "command": _optional_limited_str(arguments, "command", None, max_length=500),
                    "exit_code": evidence_exit_code,
                    "artifact_ref": _optional_limited_str(arguments, "artifact_ref", None, max_length=240),
                    "artifact_path": _optional_limited_str(arguments, "artifact_path", None, max_length=500),
                    "artifact_url": _optional_limited_str(arguments, "artifact_url", None, max_length=500),
                    "files": _optional_project_relative_files(arguments, project_dir=evidence_project_dir),
                    "idempotency_key": _optional_limited_str(arguments, "idempotency_key", None, max_length=240),
                    "client": _optional_limited_str(arguments, "client", None, max_length=80),
                    "client_session_id": _optional_limited_str(arguments, "client_session_id", None, max_length=240),
                    # Not accepted as arguments on this tool; listed so callers
                    # cannot smuggle join ids through free-form metadata.
                    "client_transcript_id": None,
                    "parent_client_session_id": None,
                    "project_dir": evidence_project_dir,
                    "resolves_blocked_event_id": resolves_blocked_event_id,
                    "resolution_scope": resolution_scope,
                    "resolution_summary": resolution_summary,
                    "resolution_objective_basis": resolution_objective_basis,
                    # Server-authored; listed even when empty so a caller cannot
                    # stamp the marker through free-form metadata.
                    MANGLED_TOOL_CALL_METADATA_KEY: mangled_fields or None,
                }
                payload["event"] = self.service.record_event(
                    {
                        # frozen source string (pre-rename): stored in events forever.
                        "source": source or "agent-sentinel-mcp",
                        "event_type": "machine_check",
                        "run_id": None if run_id == "latest" and not has_outcome_fields else resolved_run_id,
                        "metadata": _metadata_with_context(arguments, evidence_context),
                    },
                    trusted_blocker_resolution=resolution_requested,
                    transport="mcp",
                )
                recorded_mangled = payload["event"].get("metadata", {}).get(MANGLED_TOOL_CALL_METADATA_KEY) or []
                if recorded_mangled:
                    payload["warnings"] = _mangled_tool_call_warnings(recorded_mangled)
        elif name == "agentacct_record_event":
            allowed = {
                "source",
                "event_type",
                "run_id",
                "provider",
                "model",
                "estimated_input_tokens",
                "estimated_output_tokens",
                "estimated_cost_usd",
                "usage_confidence",
                "cost_confidence",
                "metadata",
            }
            _reject_unknown_keys(arguments, allowed)
            event = {
                "source": _required_limited_str(arguments, "source", max_length=80),
                "event_type": _required_limited_str(arguments, "event_type", max_length=80),
                "run_id": _optional_run_id(arguments, "run_id"),
                "provider": _optional_limited_str(arguments, "provider", None, max_length=80),
                "model": _optional_limited_str(arguments, "model", None, max_length=120),
                "estimated_input_tokens": _optional_nonnegative_int(arguments, "estimated_input_tokens"),
                "estimated_output_tokens": _optional_nonnegative_int(arguments, "estimated_output_tokens"),
                "estimated_cost_usd": _optional_nonnegative_float(arguments, "estimated_cost_usd"),
                "usage_confidence": _optional_limited_str(arguments, "usage_confidence", None, max_length=80),
                "cost_confidence": _optional_limited_str(arguments, "cost_confidence", None, max_length=80),
                "metadata": _optional_metadata(arguments),
            }
            payload = {"event": self.service.record_event(event, transport="mcp")}
        elif name == "agentacct_attach_client_context":
            # Fail safe: any attach attempt invalidates the prior context so a
            # failed re-attach in a new client session cannot leak the previous
            # session's ids into sections recorded afterwards.
            self._attached_client_context = {}
            self._attached_client_context_event_id = None
            allowed = {
                "source",
                "client",
                "client_session_id",
                "run_id",
                "client_transcript_id",
                "parent_client_session_id",
                "project_dir",
                "turn_id",
                "turn_index",
                "message_id",
                "request_id",
                "client_event_timestamp",
                "idempotency_key",
                "metadata",
            }
            _reject_unknown_keys(arguments, allowed)
            run_id = _optional_run_id(arguments, "run_id")
            context = {
                "sentinel_semantic_kind": "client_context",
                "usage_join_strategy": "agent_reported_client_context",
                **_client_context_metadata(arguments, require_client=True, require_session=False),
            }
            join_hint_quality = _context_join_hint_quality(context, run_id=run_id)
            if join_hint_quality == "missing":
                raise InvalidParams("attach_client_context needs at least one join hint: client_session_id, client_transcript_id, parent_client_session_id, run_id, or project_dir")
            context["join_hint_quality"] = join_hint_quality
            # Server-authored provenance: mark which id keys entered through
            # validated tool arguments so downstream joins can distinguish
            # them from ids smuggled through generic recording paths.
            authored_id_keys = sorted(key for key in CLIENT_CONTEXT_ID_KEYS if context.get(key) is not None)
            if authored_id_keys:
                context["client_context_keys_authored"] = authored_id_keys
            metadata = _metadata_with_context(arguments, context)
            # Provenance keys are server-authored: whatever this call did not
            # set itself is removed, so callers can never forge provenance.
            for key in RESERVED_CLIENT_CONTEXT_PROVENANCE_KEYS:
                if context.get(key) is None:
                    metadata.pop(key, None)
            event = {
                "source": _required_limited_str(arguments, "source", max_length=80),
                "event_type": "client_context_attached",
                "run_id": run_id,
                "metadata": metadata,
            }
            recorded = self.service.record_event(
                event,
                preserve_client_context_provenance=True,
                transport="mcp",
            )
            recorded_metadata = recorded.get("metadata") if isinstance(recorded.get("metadata"), dict) else {}
            # Idempotent replays return the stored event. Report and remember
            # what is actually persisted, not what this call asked for, so the
            # payload, inherited context, and store never disagree.
            recorded_quality = recorded_metadata.get("join_hint_quality") or _context_join_hint_quality(recorded_metadata, run_id=recorded.get("run_id"))
            self._remember_attached_client_context(recorded_metadata, recorded.get("event_id"))
            payload = {
                "context_attached": True,
                "join_hint_quality": recorded_quality,
                "recommended_next_step": _recommended_context_next_step(recorded_quality),
                "warnings": _context_join_warnings(recorded_quality),
                "event": recorded,
            }
        elif name == "agentacct_record_section":
            allowed = {
                "source",
                "section_id",
                "section_status",
                "run_id",
                "section_title",
                # `title` is the HTTP lane's name for the same field, and it is
                # what every shipped instruction surface told agents to pass
                # until this release. Rendered CLAUDE.md/AGENTS.md files are
                # written once at onboard and never refreshed, so the alias is
                # what keeps already-onboarded machines recording at all.
                "title",
                "phase",
                "kind",
                "summary",
                "client",
                "client_session_id",
                "client_transcript_id",
                "parent_client_session_id",
                "project_dir",
                "turn_id",
                "turn_index",
                "message_id",
                "request_id",
                "client_event_timestamp",
                "files",
                "blocker",
                "next_step",
                "idempotency_key",
                "metadata",
            }
            _reject_unknown_keys(arguments, allowed)
            section_status = _required_choice(arguments, "section_status", {"started", "checkpoint", "completed", "blocked", "handed_off"})
            # Both spellings are validated even when only one is used, so a
            # malformed alias is never silently ignored. section_title wins.
            section_title = _optional_limited_str(arguments, "section_title", None, max_length=160)
            section_title_alias = _optional_limited_str(arguments, "title", None, max_length=160)
            section_project_dir = _optional_limited_str(arguments, "project_dir", None, max_length=1000)
            mangled_fields = _detect_mangled_tool_call_fields(
                "agentacct_record_section", arguments, SECTION_NARRATIVE_KEYS
            )
            context = {
                "sentinel_semantic_kind": "section",
                "usage_join_strategy": "agent_reported_section_context",
                "section_id": _required_limited_str(arguments, "section_id", max_length=120),
                "section_status": section_status,
                "section_title": section_title if section_title is not None else section_title_alias,
                "phase": _optional_limited_str(arguments, "phase", None, max_length=80),
                "kind": _optional_choice(arguments, "kind", WORK_KINDS, "unknown"),
                "summary": _optional_limited_str(arguments, "summary", None, max_length=1200),
                "files": _optional_project_relative_files(arguments, project_dir=section_project_dir),
                "blocker": _optional_limited_str(arguments, "blocker", None, max_length=1200),
                "next_step": _optional_limited_str(arguments, "next_step", None, max_length=1200),
                # Server-authored; listed even when empty so a caller cannot
                # stamp the marker through free-form metadata.
                MANGLED_TOOL_CALL_METADATA_KEY: mangled_fields or None,
                **_client_context_metadata(arguments, require_client=False, require_session=False),
            }
            section_source = _required_limited_str(arguments, "source", max_length=80)
            # Inheritance rules: ids are never inherited when the caller
            # supplied either id (the pair must not mix sources), the pair
            # comes from a single inheritance source, and hook-captured
            # context (client-derived) outranks agent-reported attach context.
            caller_supplied_id = any(context.get(key) is not None for key in CLIENT_CONTEXT_ID_KEYS)
            hook_inherited, hook_selection = self._inherit_hook_client_context(
                context, source=section_source, allow_ids=not caller_supplied_id
            )
            hook_provided_ids = any(key in hook_inherited for key in CLIENT_CONTEXT_ID_KEYS)
            attach_keys = (
                tuple(key for key in INHERITABLE_CLIENT_CONTEXT_KEYS if key not in CLIENT_CONTEXT_ID_KEYS)
                if caller_supplied_id or hook_provided_ids
                else INHERITABLE_CLIENT_CONTEXT_KEYS
            )
            attach_inherited = self._inherit_attached_client_context(context, keys=attach_keys)
            inherited_keys = sorted([*hook_inherited, *attach_inherited])
            if inherited_keys:
                context["client_context_inherited_keys"] = inherited_keys
                if hook_inherited:
                    context["client_context_source"] = "claude_code_hook"
                if hook_provided_ids:
                    context["context_freshness"] = "client_derived"
                    selected_path = None
                    if hook_selection is not None and hook_selection.context is not None:
                        selected_path = hook_selection.context.get("context_path")
                    context["client_context_inherited_from"] = selected_path or str(CLAUDE_CODE_HOOK_CONTEXT_RELATIVE_PATH)
                    if hook_selection is not None:
                        # Auditability: WHY inheriting this context was safe
                        # (single_fresh | env_session_match | pid_lineage_match).
                        context["client_context_selection"] = hook_selection.reason
                elif any(key in attach_inherited for key in CLIENT_CONTEXT_ID_KEYS) and self._attached_client_context_event_id:
                    context["client_context_inherited_from_event_id"] = self._attached_client_context_event_id
            # Server-authored provenance: every id present here entered through
            # a validated path (explicit tool argument or server-side
            # inheritance). Ids without this marker are capped below exact by
            # the join rules, closing the metadata-smuggling path.
            authored_id_keys = sorted(key for key in CLIENT_CONTEXT_ID_KEYS if context.get(key) is not None)
            if authored_id_keys:
                context["client_context_keys_authored"] = authored_id_keys
            run_id = _optional_run_id(arguments, "run_id")
            size_warnings: list[str] = []
            try:
                metadata = _metadata_with_context(arguments, context)
            except InvalidParams:
                refusal_stamps = {
                    key: context[key]
                    for key in ("client_context_inheritance_refused", "hook_context_fresh_count")
                    if context.get(key) is not None
                }
                if not inherited_keys and not refusal_stamps:
                    raise
                # A call that fit the metadata size limit before inheritance
                # must keep working: drop the inherited context, not the section.
                for key in inherited_keys:
                    context[key] = None
                for key in RESERVED_CLIENT_CONTEXT_PROVENANCE_KEYS:
                    context.pop(key, None)
                inherited_keys = []
                hook_provided_ids = False
                # Explicitly supplied ids survive the inherited-context drop
                # and keep their server-authored marker.
                remaining_authored = sorted(key for key in CLIENT_CONTEXT_ID_KEYS if context.get(key) is not None)
                if remaining_authored:
                    context["client_context_keys_authored"] = remaining_authored
                # Re-apply the concurrent-session refusal record (the reserved
                # -key cleanup above pops ALL reserved keys): the marker is
                # tiny and explains the missing ids. If even the marker does
                # not fit, drop it too rather than fail a call that fit
                # before this feature existed.
                context.update(refusal_stamps)
                try:
                    metadata = _metadata_with_context(arguments, context)
                except InvalidParams:
                    if not refusal_stamps:
                        raise
                    for key in refusal_stamps:
                        context.pop(key, None)
                    metadata = _metadata_with_context(arguments, context)
                size_warnings.append(f"Inherited client context was dropped: metadata would exceed {METADATA_MAX_BYTES} bytes when JSON encoded. Trim metadata or pass join keys explicitly.")
            # Provenance keys are server-authored: whatever this call did not
            # set itself is removed, so callers can never forge inheritance.
            for key in RESERVED_CLIENT_CONTEXT_PROVENANCE_KEYS:
                if context.get(key) is None:
                    metadata.pop(key, None)
            event = {
                "source": section_source,
                "event_type": f"section_{section_status}",
                "run_id": run_id,
                "metadata": metadata,
            }
            recorded = self.service.record_event(
                event,
                preserve_client_context_provenance=True,
                transport="mcp",
            )
            recorded_metadata = recorded.get("metadata") if isinstance(recorded.get("metadata"), dict) else {}
            join_hint_quality = _context_join_hint_quality(recorded_metadata, run_id=recorded.get("run_id"))
            # Read the marker back off the PERSISTED event so an idempotent
            # replay warns exactly like the write that stored it.
            recorded_mangled = recorded_metadata.get(MANGLED_TOOL_CALL_METADATA_KEY) or []
            payload = {
                "event": recorded,
                "join_hint_quality": join_hint_quality,
                "warnings": [
                    *size_warnings,
                    *_mangled_tool_call_warnings(recorded_mangled),
                    *_context_join_warnings(join_hint_quality),
                ],
            }
            # Describe what was PERSISTED (idempotent replays return the stored
            # event), so payload and store never disagree about inheritance —
            # or about a refusal.
            recorded_refusal = recorded_metadata.get("client_context_inheritance_refused")
            if recorded_refusal:
                # The refusal covers HOOK-context inheritance. Ids inherited
                # from a prior attach on this MCP server can still be present
                # (process-bound, capped at medium) — the note must not claim
                # the section stays unattributed when they are.
                refusal_inherited = recorded_metadata.get("client_context_inherited_keys") or []
                attach_ids_present = any(key in refusal_inherited for key in CLIENT_CONTEXT_ID_KEYS)
                if attach_ids_present:
                    refusal_note = (
                        "Multiple concurrent Claude Code sessions were detected for this project store; "
                        "hook-context id inheritance was refused to avoid attributing one session's usage "
                        "to another (missing beats wrong). This section still carries join ids inherited "
                        "from the last agentacct_attach_client_context on this MCP server (capped at medium "
                        "confidence); pass ids explicitly for exact attribution."
                    )
                    refusal_warning = (
                        "Concurrent Claude Code sessions detected: hook-context id inheritance was refused for this "
                        "section; attach-inherited ids were used instead (medium confidence at most). "
                        "Pass client_session_id explicitly for exact attribution."
                    )
                else:
                    refusal_note = (
                        "Multiple concurrent Claude Code sessions were detected for this project store; "
                        "join ids were not inherited to avoid attributing one session's usage to another "
                        "(missing beats wrong). Usage for this section stays unattributed unless ids are "
                        "passed explicitly."
                    )
                    refusal_warning = (
                        "Concurrent Claude Code sessions detected: hook-context id inheritance was refused for this section. "
                        "Pass client_session_id explicitly to attribute usage."
                    )
                payload["refused_client_context"] = {
                    "reason": recorded_refusal,
                    "fresh_context_count": recorded_metadata.get("hook_context_fresh_count"),
                    "note": refusal_note,
                }
                payload["warnings"].append(refusal_warning)
            recorded_inherited = recorded_metadata.get("client_context_inherited_keys") or []
            if recorded_inherited:
                source_label = recorded_metadata.get("client_context_source") or "attach_client_context"
                payload["inherited_client_context"] = {
                    "keys": recorded_inherited,
                    "source": source_label,
                    "from": recorded_metadata.get("client_context_inherited_from") or recorded_metadata.get("client_context_inherited_from_event_id"),
                    "note": (
                        "Join keys were captured from the Claude Code hook (client-derived); agentacct attributes them at high confidence."
                        if source_label == "claude_code_hook"
                        else "Join keys were inherited from the last agentacct_attach_client_context on this MCP session. If this is a new conversation, attach the current session ids first."
                    ),
                }
        elif name == "agentacct_record_agent_usage_debug":
            allowed = {
                "source",
                "client",
                "reporting_basis",
                "run_id",
                "provider",
                "model",
                "client_session_id",
                "client_transcript_id",
                "parent_client_session_id",
                "turn_id",
                "turn_index",
                "message_id",
                "request_id",
                "client_event_timestamp",
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "cached_input_tokens",
                "reasoning_output_tokens",
                "total_tokens",
                "cost_usd",
                "cost_currency",
                "cost_basis",
                "summary",
                "metadata",
            }
            _reject_unknown_keys(arguments, allowed)
            provider = _optional_limited_str(arguments, "provider", None, max_length=80)
            model = _optional_limited_str(arguments, "model", None, max_length=120)
            event = {
                "source": _required_limited_str(arguments, "source", max_length=80),
                "event_type": "agent_usage_debug_reported",
                "run_id": _optional_run_id(arguments, "run_id"),
                "provider": provider,
                "model": model,
                "estimated_input_tokens": None,
                "estimated_output_tokens": None,
                "estimated_cost_usd": None,
                "usage_confidence": "unknown",
                "cost_confidence": "unknown",
                "metadata": _metadata_with_context(arguments, _agent_usage_debug_metadata(arguments, provider=provider, model=model)),
            }
            payload = {"event": self.service.record_event(event, transport="mcp")}
        elif name == "agentacct_list_events":
            _reject_unknown_keys(arguments, {"limit", "run_id"})
            limit = _optional_int(arguments, "limit", 20, minimum=1, maximum=200)
            run_id = _optional_run_id(arguments, "run_id")
            payload = {"events": self.service.list_events(limit=limit, run_id=run_id)}
        elif name == "agentacct_get_event_summary":
            _reject_unknown_keys(arguments, {"limit", "run_id"})
            limit = _optional_int(arguments, "limit", 200, minimum=1, maximum=200)
            run_id = _optional_run_id(arguments, "run_id")
            payload = {"summary": self.service.summarize_events(limit=limit, run_id=run_id)}
        elif name == "agentacct_prepare_judge":
            _reject_unknown_keys(arguments, {"run_id", "task_goal", "rubric", "write_package"})
            payload = self.service.prepare_judge(
                _optional_str(arguments, "run_id", "latest"),
                task_goal=_required_str(arguments, "task_goal"),
                rubric=_required_str(arguments, "rubric"),
                write_package=_optional_bool(arguments, "write_package", True),
            )
        elif name == "agentacct_compute_value":
            _reject_unknown_keys(arguments, {"run_id", "budget_usd"})
            payload = {
                "value": self.service.compute_value(
                    _optional_str(arguments, "run_id", "latest"),
                    budget_usd=_optional_positive_float(arguments, "budget_usd"),
                )
            }
        else:
            raise ValueError(f"Unknown tool: {name}")
        return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}]}

    @staticmethod
    def _response(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _read_balanced_json(stdin: BinaryIO, first_byte: bytes) -> dict[str, Any] | None:
    """Read one raw JSON-RPC object from stdio clients that do not use headers.

    Claude Code 2.1.x and Codex CLI 0.139.0 send newline-less raw JSON
    objects over stdio for local MCP servers. The MCP SDK examples often use
    Content-Length framing instead. Support both so agentacct can interoperate
    with real clients and the existing local workflow smoke.
    """
    data = bytearray(first_byte)
    depth = 0
    in_string = False
    escape = False
    for byte in first_byte:
        char = chr(byte)
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    while depth > 0 or in_string:
        chunk = stdin.read(1)
        if chunk == b"":
            return None
        data.extend(chunk)
        char = chunk.decode("utf-8", errors="ignore")
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return json.loads(bytes(data).decode("utf-8"))


def read_mcp_message_with_framing(stdin: BinaryIO) -> tuple[dict[str, Any], str] | None:
    first = stdin.read(1)
    while first in {b" ", b"\t", b"\r", b"\n"}:
        first = stdin.read(1)
    if first == b"":
        return None
    if first == b"{":
        message = _read_balanced_json(stdin, first)
        return (message, "json") if message is not None else None

    header = bytearray(first)
    while not (header.endswith(b"\r\n\r\n") or header.endswith(b"\n\n")):
        chunk = stdin.read(1)
        if chunk == b"":
            return None
        header.extend(chunk)
    headers: dict[str, str] = {}
    header_text = bytes(header).decode("ascii")
    for line in header_text.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    content_length = int(headers.get("content-length", "0"))
    if content_length <= 0:
        return None
    body = stdin.read(content_length)
    return json.loads(body.decode("utf-8")), "content-length"


def read_mcp_message(stdin: BinaryIO) -> dict[str, Any] | None:
    framed = read_mcp_message_with_framing(stdin)
    return framed[0] if framed is not None else None


def write_mcp_message(stdout: BinaryIO, message: dict[str, Any], *, framing: str = "content-length") -> None:
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if framing == "json":
        stdout.write(body + b"\n")
    else:
        stdout.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stdout.flush()


def _tool_payload(response: dict[str, Any]) -> dict[str, Any]:
    try:
        text = response["result"]["content"][0]["text"]
        payload = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("MCP tool response did not contain JSON text content") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("MCP tool response payload was not an object")
    return payload


def run_mcp_event_workflow_smoke(*, store_dir: Path | str | None = None, run_id: str = "mcp_workflow_smoke") -> dict[str, Any]:
    """Exercise the local MCP event workflow an agent client should use.

    This is a zero-network, zero-paid-token release-gate helper. It uses normal
    JSON-RPC method dispatch rather than calling service methods directly:
    initialize -> tools/list -> agentacct_record_event -> agentacct_list_events ->
    agentacct_get_event_summary.

    Without an explicit ``store_dir`` the smoke runs against a THROWAWAY
    temporary store: it records real events, and a write-y smoke must never
    land in a production ledger by default.
    """
    validate_run_id(run_id)
    store_is_temporary = store_dir is None
    if store_dir is None:
        # frozen prefix + smoke identifiers below (pre-rename): the smoke's
        # source string is a registered DIAGNOSTIC_EVENT_SOURCES value stored
        # in events forever — never rename.
        store_dir = tempfile.mkdtemp(prefix="agent-sentinel-workflow-smoke-")
    server = SentinelMCPServer(store_dir=store_dir)
    request_id = 1

    def call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal request_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        request_id += 1
        response = server.handle_message(message)
        if response is None:
            raise RuntimeError(f"MCP method returned no response: {method}")
        if "error" in response:
            raise RuntimeError(f"MCP method failed: {method}: {response['error']}")
        return response

    initialized = call("initialize", {"clientInfo": {"name": "agent-sentinel-workflow-smoke"}})
    tools_response = call("tools/list", {})
    tool_names = [tool.get("name") for tool in tools_response["result"].get("tools", [])]
    required_tools = {"agentacct_record_event", "agentacct_list_events", "agentacct_get_event_summary"}
    missing_tools = sorted(required_tools - set(tool_names))
    if missing_tools:
        raise RuntimeError("MCP workflow missing required tool(s): " + ", ".join(missing_tools))

    record_response = call(
        "tools/call",
        {
            "name": "agentacct_record_event",
            "arguments": {
                # Diagnostic signature: this type/source pair MUST stay
                # registered in usage_truth.DIAGNOSTIC_EVENT_TYPES/_SOURCES or
                # smoke events leak into user-facing ledger views.
                "source": "agent-sentinel-mcp-workflow-smoke",
                "event_type": "workflow_smoke",
                "run_id": run_id,
                "provider": "local",
                "model": "mcp-workflow-smoke",
                "estimated_input_tokens": 0,
                "estimated_output_tokens": 0,
                "estimated_cost_usd": 0,
                "usage_confidence": "estimated",
                "cost_confidence": "estimated",
                "metadata": {"summary": "safe local MCP workflow smoke event", "api_key": "fake-key-for-redaction-test"},
            },
        },
    )
    event = _tool_payload(record_response)["event"]
    event_id = event.get("event_id")
    if not event_id:
        raise RuntimeError("MCP workflow event did not include event_id")

    list_response = call("tools/call", {"name": "agentacct_list_events", "arguments": {"run_id": run_id, "limit": 20}})
    events = _tool_payload(list_response)["events"]
    event_round_tripped = any(item.get("event_id") == event_id for item in events)

    summary_response = call("tools/call", {"name": "agentacct_get_event_summary", "arguments": {"run_id": run_id, "limit": 20}})
    summary = _tool_payload(summary_response)["summary"]
    summary_ok = summary.get("event_count", 0) >= 1 and summary.get("by_source", {}).get("agent-sentinel-mcp-workflow-smoke", 0) >= 1
    metadata_redacted = event.get("metadata", {}).get("api_key") == "[REDACTED]"

    ok = bool(event_round_tripped and summary_ok and metadata_redacted)
    return {
        "ok": ok,
        "run_id": run_id,
        "store_dir": str(server.service.store.root),
        "store_is_temporary": store_is_temporary,
        "event_id": event_id,
        "event_round_tripped": event_round_tripped,
        "summary_ok": summary_ok,
        "metadata_redacted": metadata_redacted,
        "tool_names": tool_names,
        "initialize": initialized["result"],
        "event": event,
        "summary": summary,
    }


def build_initialize_result(params: Any) -> dict[str, Any]:
    """The static MCP ``initialize`` result, shared by the live and degraded servers.

    A degraded (store-less) server must answer ``initialize`` and ``tools/list``
    byte-identically to the live server so the host stays fully connected;
    factoring the payload here keeps the two from drifting.
    """
    requested_protocol = params.get("protocolVersion") if isinstance(params, dict) else None
    protocol_version = requested_protocol if isinstance(requested_protocol, str) and requested_protocol else "2024-11-05"
    return {
        "protocolVersion": protocol_version,
        # Pre-rename registrations still launch this server under the old
        # config key; pairing accepts both (log_evidence), so the
        # serverInfo name can advertise the new brand unconditionally.
        "serverInfo": {"name": "agentacct", "version": "0.1.0"},
        "capabilities": {"tools": {}},
        # Directive, tool-aware guidance delivered at the tool
        # layer, where Claude Code's tool-deferral barrier lives:
        # tells the agent to record its work (and to load these
        # tools first if they are deferred) even when a
        # background CLAUDE.md instruction would not reach it.
        "instructions": MCP_SERVER_INSTRUCTIONS,
    }


# The tool-call error a degraded server returns when no store could be
# resolved. It must be legible in a host's error surface and tell the user
# exactly how to recover — WITHOUT the server ever creating or picking a store
# on its own (agentacct's honesty rule).
DEGRADED_NO_STORE_MESSAGE = (
    "agentacct: no store configured — restart the MCP server with "
    "--store-dir <abs path> (or set an absolute AGENTACCT_STORE_DIR). "
    "The server is connected but cannot record work until a store is set; "
    "it will not create or pick a store on its own."
)


def degraded_store_unwritable_message(store_dir: Path | str | None, error: object) -> str:
    """Tool-call error when a configured --store-dir cannot be used (mkdir failed)."""
    return (
        f"agentacct: configured store directory {store_dir} is not usable ({error}). "
        "Restart the MCP server with a writable --store-dir <abs path> "
        "(or an absolute AGENTACCT_STORE_DIR). The server is connected but "
        "cannot record work until a usable store is set."
    )


class _DegradedMCPServer:
    """Store-less stand-in that keeps an MCP stdio session alive and honest.

    When the store cannot be resolved — or the configured ``--store-dir`` is
    not usable — constructing the real server would raise before the read loop,
    and a server that exits at startup looks CRASHED to the host (the most
    likely cause of the reported "it crashed my opencode"). Instead we run this:
    it answers ``initialize`` and ``tools/list`` exactly like the live server so
    the host stays connected, but every tool call returns a legible JSON-RPC
    error explaining how to configure a store. It NEVER creates or selects a
    store — staying connected while refusing to record is the entire point.
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            return SentinelMCPServer._response(msg_id, build_initialize_result(message.get("params", {})))
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return SentinelMCPServer._response(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            # Same serialized-error shape the live server uses for unexpected
            # failures: a clear message, never a crash, never a silent store.
            return SentinelMCPServer._error(msg_id, -32000, self._reason)
        return SentinelMCPServer._error(msg_id, -32601, f"Unknown method: {method}")


def serve_stdio(
    *,
    store_dir: Path | str | None,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    degraded_reason: str | None = None,
) -> None:
    stdin_stream: BinaryIO = stdin or sys.stdin.buffer
    stdout_stream: BinaryIO = stdout or sys.stdout.buffer
    server: SentinelMCPServer | _DegradedMCPServer
    if degraded_reason is not None:
        # The store was unresolvable upstream (cli.mcp_serve): run degraded
        # WITHOUT touching the filesystem. Never construct a store here.
        server = _DegradedMCPServer(degraded_reason)
    else:
        try:
            server = SentinelMCPServer(store_dir=store_dir)
        except Exception as exc:  # noqa: BLE001 - a store we cannot build must degrade, not crash the server.
            # An unwritable/invalid --store-dir (RunStore.mkdir failed) must
            # surface as a JSON-RPC error at the first tool call, NOT an
            # uncaught startup exception that the host reads as a dead server.
            server = _DegradedMCPServer(degraded_store_unwritable_message(store_dir, exc))
    while True:
        framed = read_mcp_message_with_framing(stdin_stream)
        if framed is None:
            return
        message, framing = framed
        response = server.handle_message(message)
        if response is not None:
            write_mcp_message(stdout_stream, response, framing=framing)
