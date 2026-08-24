"""Client-log-evidenced context linking (single source of truth).

At usage-import time agentacct can pair each agentacct-recorded MCP event with
the client session log that created it: the client's own transcript contains
the creation tool call AND the server's response carrying the new event_id.
That pairing is client-derived truth (the log is usage truth), but it is
post-hoc — computed at import time, not authored in-session — so it feeds the
unified matcher as its own provenance tier ``log_evidenced`` (high, never
``exact``; see join_rules).

Hard rules encoded here, in honesty order:

- **Allowlist first, shape second.** Event ids are extracted ONLY from the
  five creation tools' outputs, paired by call id. Read tools
  (list/summary/runs/report) echo OTHER sessions' event ids and are never
  donors; the shape check alone must also reject read-shaped payloads.
- **Derived-only markers.** Evidence links are computed fresh from trusted
  usage-import rows on every read. ``log_evidenced_*`` / ``log_evidence_*``
  keys are never read from stored event metadata, so MCP/HTTP writers cannot
  forge a link (the donor side is additionally gated by the usage-truth trust
  markers that ``strip_untrusted_usage_truth_metadata`` removes at write time).
- **Unproven Codex descendant replay is not a donor.** A forked rollout may
  replay its parent's old MCP responses. Child/internal donors require both a
  canonical event time and a trusted session start, with a five-second skew
  allowance; missing/conflicting time is excluded for descendants, and
  impossible ordering is excluded for every Codex donor instead of guessed.
- **Source namespaces fail closed.** The same evidenced event id observed by
  donor rows from different (or explicit-vs-missing) client-home namespaces
  links to neither. Canonical event-id collisions across semantic namespaces
  likewise refuse every donor while preserving raw evidence.
- **Conflicts veto both ways; ambiguity refuses.** A snapshot whose own
  claimed ids disagree with the evidenced session joins nothing; an event
  evidenced by more than one session links to none; refusals are counted,
  never silent.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any, Callable, Hashable, Iterable, Mapping

from .join_rules import namespace_join_compatible
from .usage_truth import (
    is_local_usage_import_event,
    local_session_observation_event_key,
    normalized_local_usage_session_id,
    reduce_local_session_observation_events,
    split_shadowed_legacy_usage_events,
)

# The ONLY tools whose responses may donate an evidenced event id. Every one
# of them returns exactly one created event under a top-level "event" key
# (mcp.py); read tools (agentacct_list_events / sentinel_list_events,
# agentacct_get_event_summary / sentinel_get_event_summary, ...) echo foreign
# ids and must never appear here.
# RECOGNIZE-MANY (this is a READ path over historical transcripts): BOTH
# tool-name generations are accepted forever — the new `agentacct_*` names
# that live installs now emit, AND the pre-rename `sentinel_*` names that
# historical Claude/Codex transcripts (and real user CLAUDE.md/AGENTS.md
# files) carry forever. Dropping the old names would silently unlink every
# pre-rename session on the next import, so this set only ever grows.
SENTINEL_CREATION_TOOLS = frozenset(
    {
        # agentacct (new) — what live installs now emit.
        "agentacct_record_event",
        "agentacct_attach_client_context",
        "agentacct_record_section",
        "agentacct_record_agent_usage_debug",
        "agentacct_record_machine_check",
        # sentinel_* (legacy) — historical transcripts/files carry these forever.
        "sentinel_record_event",
        "sentinel_attach_client_context",
        "sentinel_record_section",
        "sentinel_record_agent_usage_debug",
        "sentinel_record_machine_check",
    }
)

# \Z (not $) so a trailing newline is rejected: 'evt_deadbeef\n' must NOT
# pass at any of the .match() call sites ($ matches before a final newline).
EVIDENCED_EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{6,64}\Z")

# Real max observed in one session: 157 distinct created ids. The cap bounds
# hostile/huge sessions; the true total is always preserved alongside so
# overflow is visible, never silent.
EVIDENCED_EVENT_IDS_CAP = 200

# Codex fork rollouts can begin by replaying the parent's transcript, including
# old MCP creation responses.  A small clock-skew allowance keeps near-boundary
# facts joinable while still rejecting the impossible case: an event recorded
# well before the alleged donor session existed.
CODEX_REPLAY_CLOCK_SKEW_SECONDS = 5.0
CODEX_REPLAY_REJECTION_REASON = "codex_event_before_session_start"
CODEX_REPLAY_TIME_UNPROVEN_REASON = "codex_replay_timestamps_unproven"
SOURCE_NAMESPACE_CONFLICT_REASON = "source_namespace_conflict"
CROSS_KIND_DONOR_IDENTITY_CONFLICT_REASON = "cross_kind_donor_identity_conflict"
CANONICAL_EVENT_IDENTITY_CONFLICT_REASON = "canonical_event_identity_conflict"

# The documented MCP registration names (every current setup path writes
# "agentacct"). Claude Code transcripts carry the registration key verbatim in
# `mcp__<server>__<tool>`; Codex normalizes the config key to underscores in the
# rollout `namespace` field (`mcp__agentacct` / `mcp__agent_sentinel`; the
# hyphen-free `agentacct` normalizes to itself). A custom registration name
# loses evidence links (skipped-but-counted, never guessed) — documented in
# INSTALL.md.
#
# ALL THREE names are accepted FOREVER: `agentacct` is what new installs now
# register, and the pre-rename `agent-chronicle` / `agent-sentinel` keys stay
# accepted because historical client logs carry them forever and pre-rename
# registrations keep producing them in new sessions — dropping an old key would
# silently unlink every pre-rename session on the next import.
SENTINEL_SERVER_KEY = "agentacct"  # the name new installs register
ACCEPTED_SERVER_KEYS = frozenset({"agentacct", "agent-chronicle", "agent-sentinel"})
_CODEX_ACCEPTED_NAMESPACES = frozenset(
    {"agentacct", "agent-chronicle", "agent_chronicle", "agent-sentinel", "agent_sentinel"}
)
_CLAUDE_TOOL_PREFIX = "mcp__"


def extract_created_event_id_from_payload(payload: Any) -> str | None:
    """Extract one event id only from the exact creation-response shape."""

    if not isinstance(payload, dict):
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    if any(isinstance(payload.get(key), list) for key in ("events", "runs")):
        return None
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not EVIDENCED_EVENT_ID_RE.match(event_id):
        return None
    return event_id


def extract_created_event_id(text: str) -> str | None:
    """Strict single-created-event text check shared by every wire adapter.

    Accepts exactly the creation-tool payload shape: a JSON dict with a
    top-level ``"event"`` dict whose ``event_id`` is a well-formed evt id.
    Rejects (belt-and-braces — the tool-name allowlist already excludes read
    tools): list-shaped payloads carrying ``"events"``/``"runs"`` lists,
    error texts, truncated/torn JSON, outcome-only machine_check payloads,
    and any non-dict shape.
    """

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return extract_created_event_id_from_payload(payload)


# ---------------------------------------------------------------------------
# Refused recording attempts (read-time, privacy-bounded)
# ---------------------------------------------------------------------------
#
# When the MCP server rejects a recording call it persists NOTHING: no event,
# no counter, no log line. The client's own transcript is therefore the only
# record that the attempt happened, and this importer already reads it — the
# refusal arrives here as the same output text whose created-event shape check
# failed, and is folded into ``evidenced_outputs_skipped``.
#
# ``evidenced_outputs_skipped`` is NOT that number and must never be presented
# as one. It also counts server-registration-name rejections, wire-shape drift,
# successful-but-not-created-event payloads, refusals the CLIENT made before
# the call ever reached agentacct (codex's approval layer, Claude Code's input
# validator), and failures of the client's own transport that agentacct never
# saw at all (a timed-out or dropped call). Only the classification below
# claims "agentacct refused this"; everything else stays an unclassified skip,
# so the two numbers reconcile by subtraction instead of disagreeing.
#
# PRIVACY: the rejection path runs BEFORE the redactor (which only runs on a
# successful record_event), so the refusal text can carry raw user content —
# `unexpected argument(s): <agent-chosen key>`, `unknown run_id: <value>`,
# Claude Code's "You sent (first 200 of N characters) <payload>". Nothing
# derived here may carry it out: the tool comes from the creation-tool
# allowlist, the field name from the frozen argument vocabulary below, the
# reason from the frozen reason vocabulary, and the message itself is never
# retained, echoed, measured, or hashed.

# The documented tool arguments agentacct's own MCP schemas define. A refusal
# names a field only when it is one of these; an agent-invented key (which is
# what most `unexpected argument(s)` refusals carry) reports no field rather
# than leaking the key. Kept as a literal so this read path never imports the
# server module and never drifts into echoing whatever a caller sent.
RECORDING_ARGUMENT_NAMES = frozenset(
    {
        "after_exit_code", "after_summary", "artifact_path", "artifact_ref",
        "artifact_url", "before_exit_code", "before_summary", "blocker",
        "budget_usd", "cache_creation_input_tokens", "cache_read_input_tokens",
        "cached_input_tokens", "client", "client_event_timestamp",
        "client_session_id", "client_transcript_id", "command", "cost_basis",
        "cost_confidence", "cost_currency", "cost_usd", "estimated_cost_usd",
        "estimated_input_tokens", "estimated_output_tokens", "event_type",
        "evidence_type", "exit_code", "files", "idempotency_key",
        "input_tokens", "items", "kind", "limit", "message_id", "metadata",
        "model", "name", "next_step", "output_tokens",
        "parent_client_session_id", "phase", "project_dir", "provider",
        "reasoning_output_tokens", "reporting_basis", "request_id",
        "resolution_scope", "resolution_summary", "resolves_blocked_event_id",
        "result", "rubric", "run_id", "section_id", "section_status",
        "section_title", "source", "summary", "task_goal", "total_tokens",
        "turn_id", "turn_index", "usage_confidence", "work_id",
        "write_package",
    }
)

# Bounded reason vocabulary. Every recognised agentacct refusal maps to exactly
# one of these; an agentacct refusal whose wording is not matched lands in
# "other" so a server message change shows up as a rising "other" count instead
# of silently vanishing (or being guessed at).
REFUSED_RECORDING_REASON_CODES = (
    "narrative_over_limit",
    "files_not_project_relative",
    "unknown_argument",
    "missing_argument",
    "invalid_argument",
    "value_over_limit",
    "value_under_limit",
    "metadata_over_size",
    "incomplete_argument_group",
    "no_runs",
    "unknown_run_id",
    "other",
)

# The JSON-RPC error codes agentacct's OWN server can answer a tools/call with.
# Enumerated from mcp.py's dispatch: FileNotFoundError -> -32004, InvalidParams
# and ValueError (which is also how "Unknown tool" comes back) -> -32602, and
# every other exception — plus the store-less degraded server, whose whole job
# is to stay connected and refuse — -> -32000.
#
# Every other code on the wire belongs to the CLIENT's transport layer, not to
# this product: -32001 request timed out, -32603 internal error, -32600/-32700
# framing, -32601 method not found. A call that never got an answer is not a
# call agentacct refused, and counting one is the same overstatement the
# position anchor below exists to prevent. Keyed on the code rather than on the
# refusal wording so rewording a message in mcp.py cannot change the count.
AGENTACCT_MCP_ERROR_CODES = frozenset({-32000, -32004, -32602})

# -32000 is the JSON-RPC "implementation-defined server error" code, and the MCP
# client libraries reuse it for their own ConnectionClosed. agentacct's -32000s
# are the degraded-store refusal and unexpected server exceptions; the client's
# is this one fixed string, which no agentacct code path produces. Excluded by
# the CLIENT's wording — stable, and not prose this repo edits — because the
# code alone cannot separate the two.
_TRANSPORT_ERROR_MESSAGES = frozenset({"connection closed"})

# Both wire spellings of a JSON-RPC error from the agentacct MCP server:
# Claude Code renders "MCP error -32602: <message>", codex renders
# "Mcp error: -32602: <message>" inside its own "tool call failed for ..."
# wrapper. The tool-name/namespace allowlist upstream is what proves the
# error came from THIS server; this pattern only locates the code and message.
#
# Matched with .match at the two POSITIONS a real wire refusal can occupy —
# never .search over the whole output. A successful call's payload routinely
# quotes an agentacct error verbatim (an after_summary reading "green after
# fixing MCP error -32602: ..." is real recorded work), and a scan of the whole
# blob counts that success as a refusal: a number that lies in the one
# direction this surface exists to be honest about.
# The digit run is bounded: this reads untrusted transcript text, and int() on
# a several-thousand-digit run raises on the interpreter's own conversion limit.
# Every JSON-RPC code is five digits, so nothing real is lost.
_MCP_ERROR_MESSAGE_RE = re.compile(
    r"mcp error:?\s*(?P<code>-\d{1,6}):[ \t]*(?P<message>[^\r\n]+)", re.IGNORECASE
)
# The line codex's anyhow wrapper puts its cause on: "Caused by:\n    <cause>".
_CAUSED_BY_LINE = "caused by:"
# The tag Claude Code puts around a failed tool_result. Written both inline
# ("<tool_use_error>MCP error ...") and on a line of its own with the message
# beneath it. Stripped only where it leads the anchored line, so the anchor
# still holds: nothing but an error is ever wrapped in it, and a payload that
# merely quotes an error does not carry it.
_CLAUDE_TOOL_ERROR_TAG = "<tool_use_error>"
# codex wraps a refusal as: tool call failed for `<server key>/<tool>`. The
# server segment is matched loosely on purpose — historical rollouts carry
# whichever registration key was current when they were written — and the tool
# it yields is still checked against the creation-tool allowlist.
_CODEX_FAILED_TOOL_RE = re.compile(r"tool call failed for `[^`/\s]*/(?P<tool>[A-Za-z0-9_]+)`")
# Leading `<field>` or `<field>[<index>]` of a refusal message.
_REFUSAL_FIELD_RE = re.compile(r"^(?P<field>[a-z][a-z0-9_]*)(?:\[\d+\])?\b")
_UNEXPECTED_ARGUMENT_PREFIX = "unexpected argument(s):"
_MISSING_ARGUMENT_PREFIX = "missing required argument:"
_UNKNOWN_RUN_ID_PREFIX = "unknown run_id:"


def _first_carrying_line(lines: list[str]) -> str | None:
    """First line that carries anything, with the error tag peeled off.

    Blank lines and a bare ``<tool_use_error>`` line carry no content, so
    neither is the anchor: stopping on one only makes a real refusal invisible.
    Skipping them widens WHAT is recognised without widening WHERE it is looked
    for, which is the property the position anchor exists for.
    """

    for line in lines:
        stripped = line.strip().removeprefix(_CLAUDE_TOOL_ERROR_TAG).strip()
        if stripped:
            return stripped
    return None


def _anchored_mcp_error_message(text: str) -> str | None:
    """The refusal message when ``text`` IS a wire refusal from agentacct.

    Only two positions count. Claude Code puts the whole tool_result at
    "MCP error -32602: <message>", so the first line that carries anything
    holds it. codex wraps the same refusal in its own "tool call failed for
    ..." error and puts the cause under "Caused by:". Anywhere else the text is
    payload — quoted, echoed, or narrated by a call that SUCCEEDED — and must
    not be read as agentacct having refused anything.

    A "Caused by:" that follows nothing is not a wrapper's cause header, so it
    does not open an anchor; without that check the first line of any text
    could nominate the second.

    The located error must also carry a code THIS server emits
    (:data:`AGENTACCT_MCP_ERROR_CODES`). A client-layer failure — a timed-out
    or dropped call — is rendered in the same place and the same shape, and it
    is not a refusal: nothing reached agentacct to be refused.
    """

    lines = text.splitlines()
    candidates: list[str] = []
    leading = _first_carrying_line(lines)
    if leading is not None:
        candidates.append(leading)
    for index, line in enumerate(lines):
        if line.strip().lower() != _CAUSED_BY_LINE:
            continue
        if not any(previous.strip() for previous in lines[:index]):
            continue
        cause = _first_carrying_line(lines[index + 1:])
        if cause is not None:
            candidates.append(cause)
    for candidate in candidates:
        match = _MCP_ERROR_MESSAGE_RE.match(candidate)
        if match is None:
            continue
        if int(match.group("code")) not in AGENTACCT_MCP_ERROR_CODES:
            continue
        message = match.group("message").strip()
        if message and message.lower() not in _TRANSPORT_ERROR_MESSAGES:
            return message
    return None


def _refusal_reason_code(message: str) -> str:
    """Bounded reason code for one agentacct refusal message.

    Matched on message SHAPE only — no fragment of the message is retained by
    the caller, and the ordering runs most-specific first so the byte cap on
    ``metadata`` is not swallowed by the generic "must be <= N" rules.

    Maximum and minimum bounds are separate codes on purpose: mcp.py rejects
    ``input_tokens=-5`` with "input_tokens must be >= 0", and reporting that
    under a code whose label reads "above the allowed maximum" would state the
    opposite of what happened.
    """

    lowered = message.lower()
    if lowered.startswith(_UNEXPECTED_ARGUMENT_PREFIX):
        return "unknown_argument"
    if lowered.startswith(_MISSING_ARGUMENT_PREFIX):
        return "missing_argument"
    if lowered.startswith(_UNKNOWN_RUN_ID_PREFIX):
        return "unknown_run_id"
    if lowered == "no runs found":
        return "no_runs"
    if "must be project-relative" in lowered or "must be a normalized project-relative path" in lowered:
        return "files_not_project_relative"
    if re.search(r"must be <= \d+ bytes when json encoded", lowered):
        return "metadata_over_size"
    if re.search(r"must be <= \d+ characters", lowered):
        return "narrative_over_limit"
    if re.search(r"must (?:be (?:<=|<) -?\d|contain <= \d)", lowered):
        return "value_over_limit"
    if re.search(r"must be (?:a finite number )?(?:>=|>) -?\d", lowered):
        return "value_under_limit"
    if "must be supplied together" in lowered or "needs at least one join hint" in lowered:
        return "incomplete_argument_group"
    if "must be one of" in lowered or "must be a" in lowered or "must be an" in lowered:
        return "invalid_argument"
    if lowered.startswith("a blocker resolution requires"):
        return "incomplete_argument_group"
    return "other"


def _refusal_field_name(message: str, reason_code: str) -> str | None:
    """Allowlisted argument name a refusal is about, or None.

    Never returns a token the server does not itself define, so an
    agent-invented key (or any other caller-supplied text) cannot ride out of
    the rejection path on this field.
    """

    if reason_code == "unknown_run_id":
        return "run_id"
    if reason_code == "unknown_argument":
        remainder = message[len(_UNEXPECTED_ARGUMENT_PREFIX):]
    elif reason_code == "missing_argument":
        remainder = message[len(_MISSING_ARGUMENT_PREFIX):]
    else:
        remainder = message
    match = _REFUSAL_FIELD_RE.match(remainder.strip())
    if match is None:
        return None
    field = match.group("field")
    return field if field in RECORDING_ARGUMENT_NAMES else None


def classify_refused_recording(text: Any, *, tool: Any = None) -> tuple[str | None, str | None, str] | None:
    """``(tool, field, reason_code)`` when agentacct itself refused the call.

    Returns None — deliberately, not "other" — when the text is not an
    agentacct MCP error: a client-side refusal (codex's approval layer, Claude
    Code's input validator), a client TRANSPORT failure that never reached this
    server (request timed out, connection closed), a
    successful-but-not-created-event payload that merely QUOTES an agentacct
    error, or wire drift. Those are real skips but they are NOT recording
    attempts this product rejected, and counting them as such would overstate
    the number the whole surface exists to be honest about. The error is
    therefore located by position AND required to carry a code this server
    emits (:func:`_anchored_mcp_error_message`), never found anywhere in the
    blob and never trusted just for looking like an MCP error.

    ``tool`` is the caller's allowlisted creation-tool name; codex's own
    "tool call failed for `<server>/<tool>`" wrapper is used as a fallback and
    is likewise accepted only when it names an allowlisted creation tool.
    """

    if not isinstance(text, str) or not text:
        return None
    message = _anchored_mcp_error_message(text)
    if not message:
        return None
    reason_code = _refusal_reason_code(message)
    field = _refusal_field_name(message, reason_code)
    resolved_tool = tool if isinstance(tool, str) and tool in SENTINEL_CREATION_TOOLS else None
    if resolved_tool is None:
        wrapper = _CODEX_FAILED_TOOL_RE.search(text)
        if wrapper is not None and wrapper.group("tool") in SENTINEL_CREATION_TOOLS:
            resolved_tool = wrapper.group("tool")
    return resolved_tool, field, reason_code


def unwrap_codex_mcp_error(result: Any) -> str | None:
    """Codex mcp_tool_call_end ``result`` field -> the ``Err`` string.

    The mirror of :func:`unwrap_codex_mcp_result`: that one reads the success
    branch and returns None for a failure, so the refusal text it discards has
    to be recovered here. Any other shape returns None.
    """

    if not isinstance(result, dict):
        return None
    err = result.get("Err")
    return err if isinstance(err, str) and err else None


def codex_namespace_matches_sentinel(namespace: Any) -> bool:
    """True when a codex function_call namespace identifies the sentinel server.

    Absent namespace is accepted (older codex versions may omit it; the bare
    tool-name allowlist still gates). A present namespace must be exactly one
    of the pinned server keys (new or pre-rename) modulo codex's underscore
    normalization and the optional ``mcp__`` prefix — substring matches
    (``mcp__not_agentacct_fake``) are rejected.
    """

    if namespace is None:
        return True
    if not isinstance(namespace, str):
        return False
    remainder = namespace.removeprefix(_CLAUDE_TOOL_PREFIX)
    return remainder in _CODEX_ACCEPTED_NAMESPACES


def classify_codex_function_call(name: Any, namespace: Any) -> str | None:
    """"accepted" / "rejected" / None for a codex rollout function_call.

    "rejected" means the bare tool name is a creation tool but the namespace
    does not identify the sentinel server — its output is skipped-but-counted
    so custom registration names and wire drift surface in the counters.
    """

    if not isinstance(name, str) or name not in SENTINEL_CREATION_TOOLS:
        return None
    return "accepted" if codex_namespace_matches_sentinel(namespace) else "rejected"


def classify_claude_tool_use(name: Any) -> str | None:
    """"accepted" / "rejected" / None for a Claude Code tool_use block name.

    Accepted only for ``mcp__agent-chronicle__<creation tool>`` or the
    pre-rename ``mcp__agent-sentinel__<creation tool>`` exactly; a
    creation-tool name under any other server segment is rejected
    (skipped-but-counted). Non-MCP and non-creation names return None.
    """

    if not isinstance(name, str) or not name.startswith(_CLAUDE_TOOL_PREFIX):
        return None
    server, separator, tool = name.removeprefix(_CLAUDE_TOOL_PREFIX).partition("__")
    if not separator or tool not in SENTINEL_CREATION_TOOLS:
        return None
    return "accepted" if server in ACCEPTED_SERVER_KEYS else "rejected"


def claude_tool_use_creation_tool(name: Any) -> str | None:
    """Bare creation-tool name of a ``mcp__<server>__<tool>`` block, else None.

    Read-only companion to :func:`classify_claude_tool_use` for callers that
    already know a block was accepted and now need the tool name itself (the
    refusal breakdown reports it). Returns only allowlisted names, so nothing
    caller-supplied can ride out through this.
    """

    if not isinstance(name, str) or not name.startswith(_CLAUDE_TOOL_PREFIX):
        return None
    _server, separator, tool = name.removeprefix(_CLAUDE_TOOL_PREFIX).partition("__")
    if not separator or tool not in SENTINEL_CREATION_TOOLS:
        return None
    return tool


def opencode_tool_creation_tool(name: Any) -> str | None:
    """Bare creation-tool name of an OpenCode ``part.data.tool`` string, else None.

    OpenCode names an MCP tool ``<server>_<tool>`` (single-underscore join), so
    the agentacct record tools arrive as e.g.
    ``agentacct_agentacct_record_section``. Accept ONLY when the name is exactly
    an allowlisted server key joined to an allowlisted creation tool — the
    remainder after the server prefix must equal a creation tool, never merely
    contain one. Read/list tools (which echo OTHER sessions' ids) and any
    non-creation name return None and are structurally excluded, exactly as the
    codex/claude classifiers exclude them.
    """

    if not isinstance(name, str):
        return None
    for server in ACCEPTED_SERVER_KEYS:
        prefix = f"{server}_"
        if name.startswith(prefix):
            tool = name[len(prefix):]
            if tool in SENTINEL_CREATION_TOOLS:
                return tool
    return None


def classify_codex_mcp_call_identity(server: Any, tool: Any) -> str | None:
    """Classify a Codex MCP call as an accepted recording-tool identity.

    Applies equally to the legacy ``mcp_tool_call_end.invocation`` identity and
    the paginated ``item_completed.item`` identity. Same allowlist-first rule
    as :func:`classify_codex_function_call`, with ONE deliberate difference:
    terminal MCP carriers require a server, so None/absent is REJECTED
    (skipped-but-counted), never accepted like an absent legacy
    ``function_call`` namespace.
    """

    if not isinstance(tool, str) or tool not in SENTINEL_CREATION_TOOLS:
        return None
    if server is None:
        return "rejected"
    return "accepted" if codex_namespace_matches_sentinel(server) else "rejected"


def classify_codex_mcp_invocation(server: Any, tool: Any) -> str | None:
    """Compatibility name for :func:`classify_codex_mcp_call_identity`."""

    return classify_codex_mcp_call_identity(server, tool)


def unwrap_codex_mcp_result(result: Any) -> str | None:
    """Codex mcp_tool_call_end ``result`` field -> the MCP text-block string.

    Observed wire shape: ``{"Ok": {"content": [{"type": "text", "text":
    "<server JSON payload>"}]}}``. ``Err`` results and any other shape return
    None (skipped-but-counted upstream — a failed tool call created nothing).
    """

    if not isinstance(result, dict):
        return None
    ok = result.get("Ok")
    if not isinstance(ok, dict):
        return None
    content = ok.get("content")
    if not isinstance(content, list):
        return None
    return _first_text_block(content)


def unwrap_codex_output_text(output: Any) -> str | None:
    """Codex function_call_output ``output`` field -> the MCP text-block string.

    Observed wire shape: a string holding a human wrapper (``Wall time: ...\n
    Output:\n``) followed by a JSON array of MCP content blocks whose single
    text block carries the server's JSON payload. Bare-JSON strings and dict
    outputs with a ``content`` block list (future codex shapes) are also
    handled. Returns None for anything else (skipped-but-counted upstream).
    """

    if isinstance(output, dict):
        content = output.get("content")
        if isinstance(content, list):
            return _first_text_block(content)
        return None
    if not isinstance(output, str):
        return None
    candidates = [output]
    marker = "Output:\n"
    if marker in output:
        candidates.append(output.split(marker, 1)[1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            text = _first_text_block(parsed)
            if text is not None:
                return text
            continue
        if isinstance(parsed, dict):
            # Tolerate a direct payload (no content-block wrapper).
            return candidate
    return None


def _first_text_block(blocks: list[Any]) -> str | None:
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
    return None


class LogEvidenceAccumulator:
    """Ordered, deduped, capped event-id accumulation with honest counters."""

    def __init__(self, *, cap: int = EVIDENCED_EVENT_IDS_CAP) -> None:
        self._cap = cap
        self.evidenced_event_ids: list[str] = []
        self._seen: set[str] = set()
        self.evidenced_outputs_skipped = 0
        # (tool, field, reason_code) -> count. Cardinality is bounded by
        # construction: every component is drawn from a frozen vocabulary, so
        # no caller-supplied text can grow this map.
        self._refused: Counter[tuple[str | None, str | None, str]] = Counter()

    def add_output_text(self, text: str | None, *, tool: str | None = None) -> None:
        event_id = extract_created_event_id(text) if isinstance(text, str) else None
        if event_id is None:
            self.record_skip(text, tool=tool)
            return
        self.add_event_id(event_id)

    def add_event_id(self, event_id: str) -> None:
        """Add an already-normalized created-event id without retaining output."""

        if not isinstance(event_id, str) or not EVIDENCED_EVENT_ID_RE.match(event_id):
            self.record_skip()
            return
        if event_id in self._seen:
            return
        self._seen.add(event_id)
        if len(self.evidenced_event_ids) < self._cap:
            self.evidenced_event_ids.append(event_id)

    def record_skip(self, text: str | None = None, *, tool: str | None = None) -> None:
        """Count one output that donated no event id.

        ``text`` is the refusal/output text when the caller has it. It is
        classified and then dropped — only the bounded triple is kept, never
        the message.
        """

        self.evidenced_outputs_skipped += 1
        refusal = classify_refused_recording(text, tool=tool)
        if refusal is not None:
            self._refused[refusal] += 1

    def record_classified_skip(
        self,
        refusal: tuple[str | None, str | None, str] | None,
    ) -> None:
        """Count a skip after an adapter has dropped the original response text.

        The tuple is re-normalized through the same frozen vocabularies used at
        render time, so this path cannot turn adapter data into unbounded or
        caller-controlled metadata.
        """

        self.evidenced_outputs_skipped += 1
        if refusal is None:
            return
        rows = refused_recording_rows({refusal: 1})
        if not rows:
            return
        row = rows[0]
        self._refused[
            (row["tool"], row["field"], row["reason_code"])
        ] += 1

    @property
    def evidenced_event_id_total(self) -> int:
        return len(self._seen)

    @property
    def refused_recording_attempt_total(self) -> int:
        return sum(self._refused.values())

    @property
    def has_evidence(self) -> bool:
        return bool(self._seen) or self.evidenced_outputs_skipped > 0

    def refused_recording_attempts(self) -> list[dict[str, Any]]:
        """Stable, privacy-bounded ``{tool, field, reason_code, count}`` rows."""

        return refused_recording_rows(self._refused)

    def as_usage_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "evidenced_event_ids": list(self.evidenced_event_ids),
            "evidenced_event_id_total": self.evidenced_event_id_total,
            "evidenced_outputs_skipped": self.evidenced_outputs_skipped,
        }
        # Read-time diagnostic only: this rides the in-memory scan candidate so
        # live surfaces can render it, and is deliberately NOT written into
        # stored event metadata. Every scan re-derives it from the client logs
        # still on disk, which is what makes already-recorded refusals visible
        # retroactively without a migration.
        if self._refused:
            fields["refused_recording_attempts"] = self.refused_recording_attempts()
        return fields


def refused_recording_rows(
    counts: Mapping[tuple[str | None, str | None, str], int] | Counter[tuple[str | None, str | None, str]],
) -> list[dict[str, Any]]:
    """``{tool, field, reason_code, count}`` rows, largest first.

    Re-validates every component against the frozen vocabularies, so a row
    built from an untrusted mapping cannot smuggle caller text into a
    rendered surface. An unrecognised reason is kept as "other" (never
    dropped); an unrecognised tool/field becomes None.
    """

    rows: Counter[tuple[str | None, str | None, str]] = Counter()
    for key, count in counts.items():
        if not isinstance(key, tuple) or len(key) != 3:
            continue
        tool, field, reason_code = key
        safe_tool = tool if isinstance(tool, str) and tool in SENTINEL_CREATION_TOOLS else None
        safe_field = field if isinstance(field, str) and field in RECORDING_ARGUMENT_NAMES else None
        safe_reason = (
            reason_code
            if isinstance(reason_code, str) and reason_code in REFUSED_RECORDING_REASON_CODES
            else "other"
        )
        rows[(safe_tool, safe_field, safe_reason)] += _safe_nonnegative_int(count)
    return [
        {"tool": tool, "field": field, "reason_code": reason_code, "count": count}
        for (tool, field, reason_code), count in sorted(
            rows.items(),
            key=lambda item: (-item[1], item[0][2], item[0][0] or "", item[0][1] or ""),
        )
        if count > 0
    ]


def _refused_recording_counts(rows: Any) -> Counter[tuple[str | None, str | None, str]]:
    counts: Counter[tuple[str | None, str | None, str]] = Counter()
    if not isinstance(rows, (list, tuple)):
        return counts
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        reason_code = row.get("reason_code")
        counts[(row.get("tool"), row.get("field"), reason_code if isinstance(reason_code, str) else "other")] += (
            _safe_nonnegative_int(row.get("count"))
        )
    return counts


def summarize_refused_recording_attempts(candidates: Iterable[Any]) -> dict[str, Any]:
    """Scan-wide "agentacct refused this recording call" summary.

    Input is the live client-log scan candidates (usage rows or session
    observations); each carries the per-session breakdown its own transcript
    scan derived. Candidates are deduped per (source namespace, client, BASE
    session id) exactly as :func:`build_log_evidence_index` dedups donors: the
    same evidence triple rides every per-model lane row of one Claude Code
    transcript, so summing raw rows would multiply one session's refusals by
    its model-lane count.

    ``unclassified_outputs_skipped`` is the honest remainder — skipped outputs
    that are NOT provably agentacct refusals (client-side rejections, wire
    drift, non-created-event payloads). It is reported rather than folded in,
    so this summary can never overstate what agentacct itself refused.
    """

    seen: set[tuple[str, str, str]] = set()
    counts: Counter[tuple[str | None, str | None, str]] = Counter()
    outputs_skipped = 0
    sessions_with_refusals = 0
    for candidate in candidates:
        client = str(getattr(candidate, "client", "") or "")
        session_id = str(getattr(candidate, "client_session_id", "") or "")
        namespace = str(getattr(candidate, "source_namespace_fingerprint", "") or "")
        key = (namespace, client, normalized_local_usage_session_id(client, session_id))
        if key in seen:
            continue
        seen.add(key)
        outputs_skipped += _safe_nonnegative_int(getattr(candidate, "evidenced_outputs_skipped", 0))
        session_counts = _refused_recording_counts(getattr(candidate, "refused_recording_attempts", ()))
        if session_counts:
            sessions_with_refusals += 1
            counts.update(session_counts)
    rows = refused_recording_rows(counts)
    refused_total = sum(int(row["count"]) for row in rows)
    by_reason: Counter[str] = Counter()
    for row in rows:
        by_reason[str(row["reason_code"])] += int(row["count"])
    return {
        "sessions_with_refusals": sessions_with_refusals,
        "refused_attempt_total": refused_total,
        "by_reason": dict(
            sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))
        ),
        "rows": rows,
        "outputs_skipped": outputs_skipped,
        "unclassified_outputs_skipped": max(outputs_skipped - refused_total, 0),
    }


# ---------------------------------------------------------------------------
# Read-time index + applier (derived-only; unforgeable by MCP/HTTP writers)
# ---------------------------------------------------------------------------


class LogEvidenceIndex(dict[str, list[dict[str, Any]]]):
    """Evidence donor map plus privacy-safe build diagnostics.

    This remains a normal ``dict`` for existing consumers.  Diagnostics are
    attached out-of-band so rejected replay evidence never becomes a fake
    event id in the index, while callers that expose health/status can report
    the refusal instead of making it silently disappear.
    """

    def __init__(self) -> None:
        super().__init__()
        self.diagnostics: dict[str, Any] = {
            "rejected_donor_links": 0,
            "rejected_usage_rows": 0,
            "rejection_reasons": {},
            "clock_skew_tolerance_seconds": CODEX_REPLAY_CLOCK_SKEW_SECONDS,
        }


def build_log_evidence_index(events: list[dict[str, Any]]) -> LogEvidenceIndex:
    """event_id -> ordered unique donors [{client, client_session_id, client_transcript_id, usage_event_id}].

    Donor rows are ONLY server-trust-marked local usage import rows (after the
    shared shadowed-legacy-row exclusion); ``strip_untrusted_usage_truth_metadata``
    removes the trust markers from every MCP/HTTP write, so agent-recorded
    events can never mint a donor. Ids are re-validated and defensively
    re-capped; donors are deduped by (client, base session id) so per-model
    lane rows and re-imported rows of one session count as ONE donor.
    """

    kept_events, _shadowed = split_shadowed_legacy_usage_events(events)
    index = LogEvidenceIndex()
    canonical_created_at, canonical_identity_conflicts = _canonical_event_identity(events)
    pending: dict[str, list[dict[str, Any]]] = {}
    rejected_links: set[tuple[str, str, str, str, str]] = set()
    rejected_usage_rows: set[tuple[str, str, str, str]] = set()
    rejected_observation_rows: set[tuple[str, str, str, str]] = set()
    rejection_reasons: Counter[str] = Counter()

    def reject(raw_id: str, donor: dict[str, Any], reason: str) -> None:
        key = (
            reason,
            raw_id,
            str(donor.get("client") or ""),
            str(donor.get("client_session_id") or ""),
            str(donor.get("source_namespace_fingerprint") or ""),
        )
        if key not in rejected_links:
            rejected_links.add(key)
            rejection_reasons[reason] += 1
        rejected_rows = (
            rejected_usage_rows
            if donor.get("donor_kind") == "usage"
            else rejected_observation_rows
        )
        rejected_rows.add(
            (
                str(donor.get("donor_event_id") or donor.get("usage_event_id") or ""),
                str(donor.get("client") or ""),
                str(donor.get("client_session_id") or ""),
                str(donor.get("source_namespace_fingerprint") or ""),
            )
        )

    for event in kept_events:
        if not is_local_usage_import_event(event):
            continue
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        ids = metadata.get("evidenced_event_ids")
        if not isinstance(ids, list) or not ids:
            continue
        client = _optional_str(metadata.get("client"))
        raw_session = _optional_str(metadata.get("client_session_id"))
        if client is None or raw_session is None:
            continue
        session = normalized_local_usage_session_id(client, raw_session)
        transcript = _optional_str(metadata.get("client_transcript_id"))
        usage_event_id = _optional_str(event.get("event_id")) or ""
        session_started_at = _safe_positive_timestamp(metadata.get("started_at"))
        session_kind = str(metadata.get("client_session_kind") or "root").strip().lower()
        parent_session = _optional_str(metadata.get("parent_client_session_id"))
        source_namespace = _optional_str(metadata.get("source_namespace_fingerprint"))
        codex_descendant = client == "codex" and (
            session_kind in {"child", "internal"} or parent_session is not None
        )
        donor = {
            "client": client,
            "client_session_id": session,
            "client_transcript_id": transcript,
            "usage_event_id": usage_event_id,
            "donor_event_id": usage_event_id,
            "donor_kind": "usage",
            "source_namespace_fingerprint": source_namespace,
        }
        accepted = 0
        for raw_id in ids:
            if accepted >= EVIDENCED_EVENT_IDS_CAP:
                break
            if not isinstance(raw_id, str) or not EVIDENCED_EVENT_ID_RE.match(raw_id):
                continue
            accepted += 1
            if raw_id in canonical_identity_conflicts:
                reject(raw_id, donor, CANONICAL_EVENT_IDENTITY_CONFLICT_REASON)
                continue
            event_created_at = canonical_created_at.get(raw_id)
            if client == "codex":
                if codex_descendant and (event_created_at is None or session_started_at is None):
                    reject(raw_id, donor, CODEX_REPLAY_TIME_UNPROVEN_REASON)
                    continue
                if (
                    event_created_at is not None
                    and session_started_at is not None
                    and event_created_at + CODEX_REPLAY_CLOCK_SKEW_SECONDS < session_started_at
                ):
                    reject(raw_id, donor, CODEX_REPLAY_REJECTION_REASON)
                    continue
            pending.setdefault(raw_id, []).append(donor)

    selected_observations, _observation_diagnostics = reduce_local_session_observation_events(events)
    for event in selected_observations:
        metadata = event.get("metadata")
        observation_key = local_session_observation_event_key(event)
        if not isinstance(metadata, dict) or observation_key is None:
            continue
        ids = metadata.get("evidenced_event_ids")
        if not isinstance(ids, list) or not ids:
            continue
        client, raw_session = observation_key
        session = normalized_local_usage_session_id(client, raw_session)
        transcript = _optional_str(metadata.get("client_transcript_id"))
        observation_event_id = _optional_str(event.get("event_id")) or ""
        session_started_at = _safe_positive_timestamp(metadata.get("started_at"))
        session_kind = str(metadata.get("client_session_kind") or "root").strip().lower()
        parent_session = _optional_str(metadata.get("parent_client_session_id"))
        source_namespace = _optional_str(metadata.get("source_namespace_fingerprint"))
        codex_descendant = client == "codex" and (
            session_kind in {"child", "internal"} or parent_session is not None
        )
        donor = {
            "client": client,
            "client_session_id": session,
            "client_transcript_id": transcript,
            "donor_event_id": observation_event_id,
            "donor_kind": "session_observation",
            "source_namespace_fingerprint": source_namespace,
        }
        accepted = 0
        for raw_id in ids:
            if accepted >= EVIDENCED_EVENT_IDS_CAP:
                break
            if not isinstance(raw_id, str) or not EVIDENCED_EVENT_ID_RE.match(raw_id):
                continue
            accepted += 1
            if raw_id in canonical_identity_conflicts:
                reject(raw_id, donor, CANONICAL_EVENT_IDENTITY_CONFLICT_REASON)
                continue
            event_created_at = canonical_created_at.get(raw_id)
            if client == "codex":
                if codex_descendant and (event_created_at is None or session_started_at is None):
                    reject(raw_id, donor, CODEX_REPLAY_TIME_UNPROVEN_REASON)
                    continue
                if (
                    event_created_at is not None
                    and session_started_at is not None
                    and event_created_at + CODEX_REPLAY_CLOCK_SKEW_SECONDS < session_started_at
                ):
                    reject(raw_id, donor, CODEX_REPLAY_REJECTION_REASON)
                    continue
            pending.setdefault(raw_id, []).append(donor)

    for raw_id, all_candidates in pending.items():
        usage_candidates = [
            donor for donor in all_candidates if donor.get("donor_kind") == "usage"
        ]
        observation_candidates = [
            donor
            for donor in all_candidates
            if donor.get("donor_kind") == "session_observation"
        ]
        # Usage is the stronger donor only when the observation lane tells the
        # same identity story. Checking usage and observation cohorts
        # separately lets a usage row from home B silently overrule an
        # observation from home A (or a different session in the same home).
        # Validate the full source cohort first, then validate complete donor
        # identity across kinds before applying the priority rule.
        if not _source_namespace_cohort_compatible(all_candidates):
            for donor in all_candidates:
                reject(raw_id, donor, SOURCE_NAMESPACE_CONFLICT_REASON)
            continue
        if usage_candidates and observation_candidates and not all(
            _cross_kind_donor_identity_compatible(usage, observation)
            for usage in usage_candidates
            for observation in observation_candidates
        ):
            for donor in all_candidates:
                reject(
                    raw_id,
                    donor,
                    CROSS_KIND_DONOR_IDENTITY_CONFLICT_REASON,
                )
            continue
        candidates = usage_candidates or observation_candidates
        if not candidates:
            continue
        donors = index.setdefault(raw_id, [])
        for donor in candidates:
            if any(
                existing["client"] == donor["client"]
                and existing["client_session_id"] == donor["client_session_id"]
                and existing.get("source_namespace_fingerprint")
                == donor.get("source_namespace_fingerprint")
                for existing in donors
            ):
                continue
            donors.append(donor)

    rejected_count = len(rejected_links)
    index.diagnostics.update(
        {
            "rejected_donor_links": rejected_count,
            "rejected_usage_rows": len(rejected_usage_rows),
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
        }
    )
    if rejected_observation_rows:
        index.diagnostics["rejected_observation_rows"] = len(rejected_observation_rows)
    return index


def apply_log_evidence_to_snapshots(
    snapshots: list[dict[str, Any]],
    index: dict[str, list[dict[str, Any]]],
    *,
    group_key: Callable[[dict[str, Any]], Hashable | None] | None = None,
) -> dict[str, int]:
    """Enrich DERIVED snapshots with evidenced session keys; returns honesty counters.

    Decision table per evidenced snapshot (single donor unless noted):

    - >= 2 donors: ambiguous refusal — no implied key, ``log_evidence_ambiguous``.
    - claims conflict with the donor (session id differs, transcript differs
      from the donor transcript, or client differs): **veto both** — the live
      ``client_session_id`` is replaced by the donor session (evidenced
      grouping wins for display), the original claim is preserved as
      ``claimed_client_session_id`` (conflicting transcript/client claims
      likewise), and ``log_evidence_conflict`` makes join_rules.pair_match
      veto every id-key equality against this snapshot.
    - claim equals the donor: ``log_evidence_corroborated`` (keys and tiers
      untouched; explicit stays exact).
    - no session claim: implied key — ``client_session_id`` <- donor session,
      ``log_evidenced_join_keys`` = ["client_session_id"] (tier
      ``log_evidenced``, high, never exact), donor row recorded for audit.
      ``client_transcript_id`` is deliberately NOT implied.

    ``group_key`` (the work-events call site passes work_id) guards merged
    items: when one group's evidenced snapshots resolve to more than one
    distinct donor session, NO snapshot of that group receives an implied key
    (would-be-implied snapshots get ``log_evidence_ambiguous`` instead;
    conflict vetoes still stand — they are the stronger refusal), counted as
    ``item_conflicts``.

    ``group_key`` also drives the donor-cohort honesty rule (the mirror of the
    item guard): when one donor session's evidence links MORE THAN ONE distinct
    section (group), that session genuinely spans several sections and its
    tokens cannot be divided among them. Every implied-key snapshot of such a
    donor is stamped ``log_evidence_cohort_size`` (the distinct-section count)
    so ``join_rules.decide_attribution`` refuses the log-evidenced allocation as
    ambiguous EVEN WHEN only one section survived the id-conflict vetoes — the
    count that must drive the decision is the sections the donor's evidence
    links, not the sections surviving the veto. The implied key is still
    stamped (the section keeps grouping under its evidenced session for
    display); only the allocation refuses.

    The markers are computed fresh here on every build and never copied from
    stored event metadata — a hostile event that writes these keys into its
    metadata changes nothing.
    """

    counters = {
        "evidenced_snapshots": 0,
        "implied_session_keys": 0,
        "corroborated": 0,
        "conflicts": 0,
        "ambiguous_multi_session": 0,
        "item_conflicts": 0,
    }
    if not index:
        return counters

    resolutions: list[tuple[dict[str, Any], list[dict[str, Any]], Hashable | None]] = []
    group_donor_sessions: dict[Hashable, set[tuple[str, str, str | None]]] = {}
    # Distinct sections (groups) each single-donor session's evidence links.
    # This counts EVERY evidenced snapshot of that donor session — implied,
    # corroborated, or conflict-vetoed — because each is a section belonging to
    # the donor; a conflict-vetoed section still means the session spans it.
    donor_session_groups: dict[tuple[str, str, str | None], set[Hashable]] = {}
    for snapshot in snapshots:
        event_id = str(snapshot.get("event_id") or "")
        donors = index.get(event_id)
        if not donors:
            continue
        group = group_key(snapshot) if group_key is not None else None
        resolutions.append((snapshot, donors, group))
        if group is not None:
            sessions = group_donor_sessions.setdefault(group, set())
            for donor in donors:
                sessions.add(
                    (
                        donor["client"],
                        donor["client_session_id"],
                        donor.get("source_namespace_fingerprint"),
                    )
                )
            if len(donors) == 1:
                donor = donors[0]
                donor_session_groups.setdefault(
                    (
                        donor["client"],
                        donor["client_session_id"],
                        donor.get("source_namespace_fingerprint"),
                    ),
                    set(),
                ).add(group)
    refused_groups = {group for group, sessions in group_donor_sessions.items() if len(sessions) > 1}
    counters["item_conflicts"] = len(refused_groups)

    for snapshot, donors, group in resolutions:
        counters["evidenced_snapshots"] += 1
        if len(donors) > 1:
            snapshot["log_evidence_ambiguous"] = {"session_count": len(donors)}
            # Keep the donor identities as non-allocating display evidence.
            # Product projections may use them only to answer the coarser
            # question "do all observations belong to the same root Task?";
            # the exact section-to-session join remains deliberately unset.
            snapshot["log_evidence_candidate_sessions"] = [
                {
                    "client": client,
                    "client_session_id": session_id,
                    **(
                        {"source_namespace_fingerprint": source_namespace}
                        if source_namespace
                        else {}
                    ),
                }
                for client, session_id, source_namespace in sorted(
                    {
                        (
                            donor["client"],
                            donor["client_session_id"],
                            donor.get("source_namespace_fingerprint"),
                        )
                        for donor in donors
                    },
                    key=lambda value: (value[0], value[1], value[2] or ""),
                )
            ]
            counters["ambiguous_multi_session"] += 1
            continue
        donor = donors[0]
        snapshot["log_evidenced_source_namespace_fingerprint"] = _optional_str(
            donor.get("source_namespace_fingerprint")
        )
        claimed_client = _optional_str(snapshot.get("client"))
        claimed_session = _optional_str(snapshot.get("client_session_id"))
        claimed_transcript = _optional_str(snapshot.get("client_transcript_id"))
        session_conflict = claimed_session is not None and claimed_session != donor["client_session_id"]
        transcript_conflict = (
            claimed_transcript is not None
            and donor.get("client_transcript_id") is not None
            and claimed_transcript != donor["client_transcript_id"]
        )
        client_conflict = claimed_client is not None and claimed_client != donor["client"]
        if session_conflict or transcript_conflict or client_conflict:
            # Label the conflict by the key(s) that actually differ. Only the
            # conflicting claim is preserved + replaced by the donor value; a
            # key that agreed (or was absent) is left untouched so we never
            # record an equal claimed==evidenced pair or blame the wrong key.
            conflict: dict[str, Any] = {"conflicting_keys": []}
            if session_conflict:
                snapshot["claimed_client_session_id"] = claimed_session
                snapshot["client_session_id"] = donor["client_session_id"]
                conflict["conflicting_keys"].append("client_session_id")
                conflict["claimed_client_session_id"] = claimed_session
                conflict["evidenced_client_session_id"] = donor["client_session_id"]
            else:
                # Session agrees (or is absent): keep the live session under the
                # donor for display grouping, but DO NOT record a session-id
                # conflict pair — the disagreement is elsewhere.
                snapshot["client_session_id"] = donor["client_session_id"]
            if transcript_conflict:
                snapshot["claimed_client_transcript_id"] = claimed_transcript
                snapshot["client_transcript_id"] = None
                conflict["conflicting_keys"].append("client_transcript_id")
                conflict["claimed_client_transcript_id"] = claimed_transcript
                conflict["evidenced_client_transcript_id"] = donor.get("client_transcript_id")
            if client_conflict:
                snapshot["claimed_client"] = claimed_client
                snapshot["client"] = donor["client"]
                conflict["conflicting_keys"].append("client")
                conflict["claimed_client"] = claimed_client
                conflict["evidenced_client"] = donor["client"]
            snapshot["log_evidence_conflict"] = conflict
            counters["conflicts"] += 1
            continue
        if claimed_session is not None:
            snapshot["log_evidence_corroborated"] = True
            counters["corroborated"] += 1
            continue
        if group in refused_groups:
            snapshot["log_evidence_ambiguous"] = {"session_count": len(group_donor_sessions[group])}
            snapshot["log_evidence_candidate_sessions"] = [
                {
                    "client": client,
                    "client_session_id": session_id,
                    **(
                        {"source_namespace_fingerprint": source_namespace}
                        if source_namespace
                        else {}
                    ),
                }
                for client, session_id, source_namespace in sorted(
                    group_donor_sessions[group],
                    key=lambda value: (value[0], value[1], value[2] or ""),
                )
            ]
            counters["ambiguous_multi_session"] += 1
            continue
        snapshot["client_session_id"] = donor["client_session_id"]
        snapshot["log_evidenced_join_keys"] = ["client_session_id"]
        snapshot["log_evidenced_by_event_id"] = donor.get("donor_event_id")
        snapshot["log_evidence_donor_kind"] = donor.get("donor_kind")
        # Frozen compatibility field: existing usage donors keep precisely
        # the old key/value. Observation-only donors do not pretend to be
        # usage rows and leave it absent.
        if donor.get("usage_event_id"):
            snapshot["log_evidenced_by_usage_event_id"] = donor["usage_event_id"]
        if claimed_client is None:
            snapshot["client"] = donor["client"]
        # Donor-cohort size: the number of distinct sections this donor
        # session's evidence links (all snapshots, veto-survivors or not). >1
        # means the session spans multiple sections, so join_rules refuses the
        # log-evidenced allocation as ambiguous even when this is the only
        # section that survived the id-conflict vetoes. The implied key is
        # still stamped above so the section keeps grouping under the session.
        cohort = donor_session_groups.get(
            (
                donor["client"],
                donor["client_session_id"],
                donor.get("source_namespace_fingerprint"),
            )
        )
        if cohort is not None and len(cohort) > 1:
            snapshot["log_evidence_cohort_size"] = len(cohort)
        counters["implied_session_keys"] += 1
    return counters


def build_log_evidence_session_blocks(
    events: list[dict[str, Any]], index: dict[str, list[dict[str, Any]]]
) -> dict[tuple[str, str, str | None], dict[str, Any]]:
    """(client, session, source home) -> evidence counts and conflicts.

    Counts every evidenced event PRESENT in this store (including plain
    record_event/task events that never become work items) against its single
    donor session; multi-donor evidence counts toward no session (honest
    refusal). ``conflicts`` counts store events whose own metadata claims a
    session/transcript/client that disagrees with the evidenced donor —
    mirroring the applier's conflict rule for surfaces that have no snapshot.
    """

    blocks: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    if not index:
        return blocks
    for event in events:
        event_id = _optional_str(event.get("event_id"))
        if event_id is None:
            continue
        donors = index.get(event_id)
        if not donors or len(donors) != 1:
            continue
        donor = donors[0]
        key = (
            donor["client"],
            donor["client_session_id"],
            _optional_str(donor.get("source_namespace_fingerprint")),
        )
        block = blocks.setdefault(key, {"evidenced_event_count": 0, "by_event_type": {}, "conflicts": 0})
        block["evidenced_event_count"] += 1
        event_type = str(event.get("event_type") or "unknown")
        block["by_event_type"][event_type] = block["by_event_type"].get(event_type, 0) + 1
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        claimed_session = _optional_str(metadata.get("client_session_id"))
        claimed_transcript = _optional_str(metadata.get("client_transcript_id"))
        claimed_client = _optional_str(metadata.get("client"))
        if (
            (claimed_session is not None and claimed_session != donor["client_session_id"])
            or (
                claimed_transcript is not None
                and donor.get("client_transcript_id") is not None
                and claimed_transcript != donor["client_transcript_id"]
            )
            or (claimed_client is not None and claimed_client != donor["client"])
        ):
            block["conflicts"] += 1
    return blocks


def summarize_log_evidence_donor_rows(events: list[dict[str, Any]]) -> dict[str, int]:
    """Store-wide donor counters: rows carrying ids, id totals, skipped outputs."""

    kept_events, _shadowed = split_shadowed_legacy_usage_events(events)
    donor_usage_rows = 0
    donor_observation_rows = 0
    evidenced_event_ids_total = 0
    outputs_skipped = 0
    observation_evidenced_event_ids_total = 0
    observation_outputs_skipped = 0
    for event in kept_events:
        if not is_local_usage_import_event(event):
            continue
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        ids = metadata.get("evidenced_event_ids")
        valid_ids = [item for item in ids if isinstance(item, str) and EVIDENCED_EVENT_ID_RE.match(item)] if isinstance(ids, list) else []
        row_total = _safe_nonnegative_int(metadata.get("evidenced_event_id_total")) or len(valid_ids)
        if valid_ids:
            donor_usage_rows += 1
            evidenced_event_ids_total += max(row_total, len(valid_ids))
        outputs_skipped += _safe_nonnegative_int(metadata.get("evidenced_outputs_skipped"))
    selected_observations, _diagnostics = reduce_local_session_observation_events(events)
    for event in selected_observations:
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        ids = metadata.get("evidenced_event_ids")
        valid_ids = [
            item
            for item in ids
            if isinstance(item, str) and EVIDENCED_EVENT_ID_RE.match(item)
        ] if isinstance(ids, list) else []
        row_total = _safe_nonnegative_int(metadata.get("evidenced_event_id_total")) or len(valid_ids)
        if valid_ids:
            donor_observation_rows += 1
            observation_evidenced_event_ids_total += max(row_total, len(valid_ids))
        observation_outputs_skipped += _safe_nonnegative_int(metadata.get("evidenced_outputs_skipped"))
    return {
        "donor_usage_rows": donor_usage_rows,
        "donor_observation_rows": donor_observation_rows,
        "evidenced_event_ids_total": evidenced_event_ids_total,
        "outputs_skipped": outputs_skipped,
        "observation_evidenced_event_ids_total": observation_evidenced_event_ids_total,
        "observation_outputs_skipped": observation_outputs_skipped,
    }


def _canonical_event_identity(events: list[dict[str, Any]]) -> tuple[dict[str, float], set[str]]:
    """Return (unambiguous timestamps, conflicting canonical event ids).

    Local usage rows are donor claims, not canonical creation facts. Duplicate
    rows are harmless only when their timestamp and semantic namespace remain
    compatible. Conflicting timestamps, cross-namespace identity, or an
    explicit namespace missing its fingerprint make the event id unusable for
    every donor; raw rows remain untouched.
    """

    candidates: dict[str, list[tuple[float | None, dict[str, Any]]]] = {}
    for event in events:
        if is_local_usage_import_event(event):
            continue
        event_id = _optional_str(event.get("event_id"))
        if event_id is None:
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        candidates.setdefault(event_id, []).append(
            (
                _safe_positive_timestamp(event.get("created_at")),
                {
                    "namespace_fingerprint": metadata.get("session_namespace_fingerprint")
                    or metadata.get("namespace_fingerprint"),
                    "identity_scope_state": metadata.get("identity_scope_state"),
                },
            )
        )

    timestamps: dict[str, float] = {}
    conflicts: set[str] = set()
    for event_id, rows in candidates.items():
        valid_times = {timestamp for timestamp, _namespace in rows if timestamp is not None}
        missing_time = any(timestamp is None for timestamp, _namespace in rows)
        namespace_rows = [namespace for _timestamp, namespace in rows]
        if len(valid_times) > 1 or (len(rows) > 1 and missing_time) or not _namespace_cohort_compatible(namespace_rows):
            conflicts.add(event_id)
            continue
        if len(valid_times) == 1:
            timestamps[event_id] = next(iter(valid_times))
    return timestamps, conflicts


def _source_namespace_cohort_compatible(donors: list[dict[str, Any]]) -> bool:
    return _namespace_cohort_compatible(
        [
            {
                "namespace_fingerprint": donor.get("source_namespace_fingerprint"),
                "identity_scope_state": (
                    "explicit" if donor.get("source_namespace_fingerprint") else "unscoped"
                ),
            }
            for donor in donors
        ]
    )


def _cross_kind_donor_identity_compatible(
    usage: dict[str, Any], observation: dict[str, Any]
) -> bool:
    """Whether usage may safely supersede one observation donor.

    Source namespace compatibility is checked for the complete cohort by the
    caller. Client and normalized session must match exactly. Transcript is
    optional corroboration: two explicit, different transcript ids conflict;
    one missing transcript does not invent a disagreement.
    """

    if (
        _optional_str(usage.get("client"))
        != _optional_str(observation.get("client"))
        or _optional_str(usage.get("client_session_id"))
        != _optional_str(observation.get("client_session_id"))
    ):
        return False
    usage_transcript = _optional_str(usage.get("client_transcript_id"))
    observation_transcript = _optional_str(
        observation.get("client_transcript_id")
    )
    return not (
        usage_transcript is not None
        and observation_transcript is not None
        and usage_transcript != observation_transcript
    )


def _namespace_cohort_compatible(facts: list[dict[str, Any]]) -> bool:
    for index, left in enumerate(facts):
        if not namespace_join_compatible(left, left):
            return False
        for right in facts[index + 1 :]:
            if not namespace_join_compatible(left, right):
                return False
    return True


def _safe_positive_timestamp(value: Any) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    return timestamp


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _safe_nonnegative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0
