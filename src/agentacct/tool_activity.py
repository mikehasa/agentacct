"""Tool-activity capture for the Receipt's Actions dimension.

The Actions question a Receipt must answer is "what did the agent reach for".
This records, per tool call, a COARSE CATEGORY (read/edit/execute/…), the
tool's NAME — a builtin like ``Read``/``Bash``, an ``mcp__server__tool`` name, or
a custom command — and, for a file-EDIT tool, the repo-relative DESTINATION PATH it
wrote. It never records a tool's OTHER arguments, the file content or diff, or the
tool's output. The CATEGORY is derivable from the name ALONE — we do not tell ``git
commit`` from ``ls`` inside ``execute``, because that would require reading arguments.

Capturing the NAME (a deliberate move from the earlier category-only line) is
what lets the Receipt answer "which specific tool / connector did the agent
use" — the raw material a manager or a diagnostic pass needs to reason "this
tool caused that problem". The name is captured and stored LOCALLY like every
other field; what to REDACT when a Receipt travels OUTSIDE its origin is a
separate, later presentation concern, not a capture-time one.

The capture path mirrors the terminal-CLI statusLine spool
(``rate_limits.write_claude_statusline_spool``): the installed Claude Code
PreToolUse hook already sees every tool name and fails open, so it appends one
tiny category tick to a per-store spool (no daemon required, O(1), best-effort).
``agentacct usage import-local`` later drains the spool into additive
``tool_activity_observed`` events, which the work ledger reduces into a
per-session ``tool_category_counts`` and the Task projection sums into
``task["actions"]``. A session with no captured ticks honestly carries no
counts (the Receipt shows a Gap), never a fabricated zero.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable


# frozen: stored ``tool_activity_observed`` events carry this event_type forever.
TOOL_ACTIVITY_EVENT_TYPE = "tool_activity_observed"
TOOL_ACTIVITY_CAPTURE_BASIS = "client_hook_tool_category"
_SPOOL_RELATIVE_PATH = Path("spool") / "claude-tool-activity.jsonl"

# The complete, closed category set. Every tool name maps to exactly one of
# these; anything unrecognised is ``other`` (honest, never dropped silently).
TOOL_CATEGORIES: frozenset[str] = frozenset(
    {"read", "edit", "execute", "search", "network", "agent", "plan", "mcp", "other"}
)

# Name-only taxonomy. Keys are lower-cased tool names. Only the tool NAME is
# consulted — never its arguments — so this map can never leak a command, path,
# or payload. ``mcp__*`` tools collapse to ``mcp`` HERE (the coarse category); the
# specific ``mcp__server__tool`` name is preserved separately by
# ``normalize_tool_name`` for the Actions tool-name breakdown.
_CATEGORY_BY_TOOL_NAME: dict[str, str] = {
    "read": "read",
    "notebookread": "read",
    "edit": "edit",
    "write": "edit",
    "multiedit": "edit",
    "notebookedit": "edit",
    "bash": "execute",
    "bashoutput": "execute",
    "killshell": "execute",
    "killbash": "execute",
    "grep": "search",
    "glob": "search",
    "ls": "search",
    "webfetch": "network",
    "websearch": "network",
    "task": "agent",
    "agent": "agent",
    "todowrite": "plan",
    "exitplanmode": "plan",
    "enterplanmode": "plan",
    # Codex tool names (verified from real ~/.codex rollouts). Codex runs shell
    # through a JS ``exec`` runtime and a plain ``exec_command``, edits through
    # ``apply_patch`` (a patch body, not a file_path arg), and coordinates work
    # through the agent tools. Without these, every core Codex tool collapsed to
    # ``other``, so its Actions category breakdown was uninformative.
    "exec": "execute",
    "exec_command": "execute",
    "local_shell": "execute",
    "shell": "execute",
    "js": "execute",
    "apply_patch": "edit",
    "spawn_agent": "agent",
    "wait_agent": "agent",
    "list_agents": "agent",
    "interrupt_agent": "agent",
    "followup_task": "agent",
    "send_message": "agent",
    "update_plan": "plan",
    # Generic shell aliases other agents use (e.g. Hermes' ``terminal``).
    "terminal": "execute",
}


def tool_category(tool_name: Any) -> str:
    """Map a tool NAME to a coarse category. Never inspects arguments.

    Unknown names return ``other`` rather than being dropped, so the count of
    what the agent did stays honest. ``mcp__<server>__<tool>`` collapses to
    ``mcp`` by prefix for the CATEGORY only; the full name is kept separately by
    ``normalize_tool_name``.
    """

    name = str(tool_name or "").strip().lower()
    if not name:
        return "other"
    if name.startswith("mcp__"):
        return "mcp"
    return _CATEGORY_BY_TOOL_NAME.get(name, "other")


# A hard bound on the captured NAME. A tool name is short (``Read``,
# ``mcp__server__tool``); this only stops a pathological value from bloating the
# spool. It bounds the name, never its content interpretation.
_TOOL_NAME_MAX = 120


def normalize_tool_name(tool_name: Any) -> str | None:
    """The tiered tool-NAME captured alongside the category.

    Returns the tool's identity verbatim — a builtin (``Read``/``Bash``), an
    ``mcp__server__tool`` name, or a custom command — trimmed and length-bounded.
    It reads ONLY the name: never arguments, never a path, never output. Blank in
    -> ``None`` (nothing to record), so a missing name is an honest gap rather
    than an empty string in the counts.
    """

    name = str(tool_name or "").strip()
    if not name:
        return None
    return name[:_TOOL_NAME_MAX]


_TOUCHED_PATH_MAX = 240
# Hard cap on the number of distinct touched paths carried per drained batch, so a
# pathological session can never bloat one event. "At least this much", never more.
_TOUCHED_FILES_PER_BATCH_MAX = 200


def _normalize_touched_path(path: Any) -> str | None:
    """Defensive normalization of a caller-supplied touched-file path.

    The CALLER (the hook) already relativizes the edited file to the session cwd;
    this is the belt-and-suspenders gate that GUARANTEES the store never holds an
    absolute path, a Windows drive/UNC path, or an over-long string — so even a buggy
    caller can never leak an absolute prefix (home dir, username). A ``..`` escape and
    a ``~/`` home path are KEPT: the hook writes an out-of-tree edit as a ``../``-relative
    path and a home-file edit as ``~/…`` (no username), and neither carries an absolute
    prefix. Blank / unsafe -> ``None``."""

    text = str(path or "").strip()
    if not text or "\x00" in text:
        return None
    if re.match(r"^[A-Za-z]:", text):  # Windows drive (C:\...)
        return None
    # A leading slash OR backslash is absolute — one backslash is a Windows
    # drive-relative absolute (``\Users\bob\...``) that ``os.path.isabs`` misses on
    # POSIX, so it must be caught here before ``replace("\\","/")`` turns it into a
    # ``/…`` path whose empty leading segment would be silently dropped.
    if text.startswith(("\\", "/")):  # single/UNC backslash, or / and //
        return None
    normalized = text.replace("\\", "/")
    # ``..`` segments are kept (an out-of-tree edit is a legitimate ``../`` path); the
    # absolute/drive/UNC guards above already blocked every absolute-prefix leak.
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts:
        return None
    return "/".join(parts)[:_TOUCHED_PATH_MAX]


# A captured command is length-bounded (a pathological one-liner should not bloat the
# spool) and the number of DISTINCT commands per drained batch is capped — "at least
# this much", never more.
_COMMAND_MAX = 400
_COMMANDS_PER_BATCH_MAX = 100

_REDACTED = "‹redacted›"  # ‹redacted›
# Control / ANSI-escape bytes are stripped from a captured command so a crafted command
# cannot inject terminal escapes into the CLI/TUI Receipt render (\x00 already drops the
# whole command earlier). Whitespace is collapsed separately, so only non-space controls
# remain here to remove.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # C0 controls, DEL, and the C1 range (CSI/OSC)


def _is_nonsecret_value(value: str) -> bool:
    """A value that is definitionally NOT an inline secret: a shell variable reference
    (``$FOO`` / ``${FOO}``) is an indirection, never a literal token."""
    return value.startswith("$")


# Env-var NAMES that contain a credential keyword but whose VALUE is definitionally a
# path/handle (``GOOGLE_APPLICATION_CREDENTIALS``, ``*_TOKEN_FILE``, …), so must not be
# redacted — masking the path hides what the agent ran and protects nothing.
_NONSECRET_NAME_SUFFIXES = ("_FILE", "_PATH", "_DIR", "_CREDENTIALS", "_CREDENTIAL")
# A credential keyword must be a WHOLE ``_``-delimited name segment (``GITHUB_TOKEN``) —
# NOT a substring of a config name (``MAX_TOKENS``, ``TOKENIZERS_PARALLELISM``).
# (``PWD`` is deliberately excluded — it collides with the shell working-dir env ``PWD``/
# ``OLDPWD``; the common ``*_PASSWORD``/``PASSWD`` forms are covered instead.)
_CREDENTIAL_SEGMENTS = frozenset({"PASSWORD", "PASSWD", "TOKEN", "SECRET", "APIKEY", "CREDENTIAL", "CREDENTIALS"})
_CREDENTIAL_NAME_PARTS = ("API_KEY", "ACCESS_KEY", "AUTH_TOKEN", "SECRET_KEY", "SECRET_ACCESS_KEY")
# A value that is a config SCALAR (number/bool/null) is not a secret — masking it would
# corrupt common config like ``MAX_TOKENS=4096`` or ``token_count=5``.
_SCALAR_VALUE = re.compile(r"(?i)(?:\d+|true|false|none|null|yes|no)$")


def _redact_flag(match: re.Match[str]) -> str:
    flag, value = match.group(1), match.group(2)
    return match.group(0) if _is_nonsecret_value(value) else flag + _REDACTED


def _redact_env(match: re.Match[str]) -> str:
    name, value = match.group(1), match.group(2)  # name includes the trailing '='
    bare = name[:-1].upper()
    if bare.endswith(_NONSECRET_NAME_SUFFIXES) or _is_nonsecret_value(value) or _SCALAR_VALUE.match(value):
        return match.group(0)
    # A real credential name has the keyword as a whole ``_`` segment (GITHUB_TOKEN),
    # as a name SUFFIX (PGPASSWORD), or as a known two-word part (API_KEY) — not merely a
    # substring of a config name (MAX_TOKENS, TOKENIZERS_PARALLELISM).
    is_credential = (
        any(bare.endswith(kw) for kw in _CREDENTIAL_SEGMENTS)
        or bool(_CREDENTIAL_SEGMENTS & set(bare.split("_")))
        or any(part in bare for part in _CREDENTIAL_NAME_PARTS)
    )
    return name + _REDACTED if is_credential else match.group(0)


# Best-effort credential masking for a captured command. LOCAL capture keeps the command
# for readability (the owner's model: capture detailed locally, redact when a Receipt is
# SHARED); this only masks obvious secret VALUES in RECOGNIZABLE positions so a live token
# is not persisted verbatim. It is NOT a guarantee — share-time redaction is the real
# safety net — and it deliberately errs toward keeping the command legible. Each sub runs
# once; a ``repl`` may be a string or a function (to skip a non-secret value).
_SECRET_SUBS: tuple[tuple[re.Pattern[str], Any], ...] = (
    # Known token shapes (prefix-anchored), masked whole.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), _REDACTED),
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{12,}"), _REDACTED),
    (re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}"), _REDACTED),  # ghp/gho/ghr/ghs/ghu
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), _REDACTED),
    (re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}"), _REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), _REDACTED),
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), _REDACTED),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"), _REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}"), _REDACTED),  # JWT
    # A credential in an inline JSON/quoted body ({"password": "…"} / {"api_key":"…"}).
    (re.compile(r'(?i)("(?:password|passwd|pwd|secret|token|api[-_]?key|access[-_]?key|auth[-_]?token|credential)"\s*:\s*")[^"]*'), r"\1" + _REDACTED),
    # An Authorization / api-key header VALUE passed via curl ``-H``/``--header`` (quoted,
    # non-empty). Anchoring on the header FLAG + quote avoids corrupting a bare
    # ``authorization:`` grep pattern or an echoed header NAME, and the ``+`` (non-empty
    # value) avoids fabricating a redaction where the quoted string has no value at all.
    (re.compile(r"""(?i)(-H|--header)(=?\s*)(["'])(authorization|proxy-authorization|x-api-key|x-auth-token)(\s*:\s*)[^"']+"""), r"\1\2\3\4\5" + _REDACTED),
    # --flag=value or --flag value for credential-ish flags (a $var reference is kept).
    (re.compile(r"(?i)(--?(?:password|passwd|pwd|token|secret|api[-_]?key|apikey|access[-_]?key|auth[-_]?token|credential)[=\s])(\S+)"), _redact_flag),
    # KEY=VALUE env style for credential-ish names (a *_FILE/_PATH/… name or a $var value
    # is kept — its value is a path/handle, not an inline secret).
    (re.compile(r"(?i)\b([A-Za-z0-9_]*(?:PASSWORD|PASSWD|TOKEN|SECRET|APIKEY|API_KEY|ACCESS_KEY|AUTH_TOKEN|CREDENTIAL)[A-Za-z0-9_]*=)(\S+)"), _redact_env),
    # URL userinfo password (scheme://user:pass@host, or scheme://:pass@host).
    (re.compile(r"(://[^:/?\s@]*:)([^@/?\s]+)(@)"), r"\1" + _REDACTED + r"\3"),
)


def _scrub_command(command: str) -> str:
    """Mask obvious credential VALUES in a command (best-effort — see ``_SECRET_SUBS``)."""
    for pattern, repl in _SECRET_SUBS:
        command = pattern.sub(repl, command)
    return command


def _normalize_command(command: Any) -> str | None:
    """The scrubbed, single-line, length-bounded command an EXECUTE tool ran, or ``None``.

    Collapses whitespace to single spaces (a spool line is one line; the Actions summary
    wants one line too), masks obvious credential values, and bounds the length. It is
    NOT a promise that no secret survives — share-time redaction is the real safety net —
    only that a live token in a recognizable shape is not persisted verbatim. Blank ->
    ``None``."""

    text = str(command or "").strip()
    if not text or "\x00" in text:
        return None
    # Collapse whitespace (keeps words apart), then strip any remaining control/ANSI
    # bytes so a crafted command cannot inject terminal escapes into the Receipt render.
    text = _CONTROL_RE.sub("", " ".join(text.split()))
    # Bound the scrub INPUT before running the credential regexes: a few patterns can
    # backtrack superlinearly on a pathologically long token run, and only the first
    # ``_COMMAND_MAX`` chars are stored anyway, so scrubbing a modest lead is enough.
    return _scrub_command(text[: _COMMAND_MAX * 4])[:_COMMAND_MAX] or None


def tool_activity_spool_path(store_dir: Path | str) -> Path:
    return Path(store_dir) / _SPOOL_RELATIVE_PATH


def record_tool_activity_tick(
    store_dir: Path | str,
    *,
    client: str,
    session_id: str,
    category: str,
    name: Any = None,
    path: Any = None,
    command: Any = None,
    at: float,
) -> None:
    """Append ONE tick to the store spool. Best-effort; never raises.

    Writes the small scalars ``{c, s, k, t}`` — client, session id, category, and
    a timestamp — plus ``n``, the tool NAME, when one is given; ``p``, the cwd-relative
    path a file-EDIT tool wrote (relativized to cwd, keeping an out-of-tree edit as a
    ``../`` path, so no absolute prefix, home dir, or username is ever stored); and
    ``cmd``, the command an EXECUTE tool ran, when the caller extracted one (the Actions
    dimension's "what did the agent actually run" — single-line, length-bounded, and
    best-effort scrubbed of obvious credential values by ``_normalize_command``; the raw
    command is kept for readability per the local-capture model, but a live token in a
    recognizable shape is not persisted verbatim). Tool OUTPUT and non-command arguments
    are still never recorded. The line is a single tiny JSON object written with
    ``O_APPEND`` so concurrent hook processes never corrupt or interleave lines.
    """

    try:
        if category not in TOOL_CATEGORIES:
            category = "other"
        client = str(client or "").strip()
        session_id = str(session_id or "").strip()
        if not client or not session_id:
            return
        target = tool_activity_spool_path(store_dir)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"c": client, "s": session_id, "k": category, "t": float(at)}
        normalized_name = normalize_tool_name(name)
        if normalized_name:
            payload["n"] = normalized_name
        touched = _normalize_touched_path(path)
        if touched:
            payload["p"] = touched
        normalized_command = _normalize_command(command)
        if normalized_command:
            payload["cmd"] = normalized_command
        line = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - capture must never affect the hook.
        return


def drain_tool_activity_spool(
    store_dir: Path | str,
    *,
    now: float | None = None,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Consume the spool and return additive ``tool_activity_observed`` events.

    The spool is atomically renamed aside before being read, so hook ticks that
    land during the drain go to a freshly recreated spool and are picked up by
    the NEXT drain. Each returned event is one batch of per-session category
    counts; because batches are additive and carry a unique id, re-reducing the
    event log is idempotent and re-importing never double counts. A few ticks
    appended between the read and the rename can be lost — an acceptable, honest
    undercount (Actions means "at least this much"), never an overcount.
    """

    spool = tool_activity_spool_path(store_dir)
    if not spool.exists():
        return []
    batch_token = token or uuid.uuid4().hex
    consuming = spool.with_name(f".{spool.name}.consuming-{os.getpid()}-{batch_token}")
    try:
        os.replace(spool, consuming)
    except FileNotFoundError:
        return []
    except OSError:
        return []
    try:
        raw = consuming.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    finally:
        try:
            consuming.unlink()
        except OSError:
            pass

    counts: dict[tuple[str, str], dict[str, int]] = {}
    name_counts: dict[tuple[str, str], dict[str, int]] = {}
    touched_paths: dict[tuple[str, str], list[str]] = {}
    commands: dict[tuple[str, str], list[str]] = {}
    latest: dict[tuple[str, str], float] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, Mapping):
            continue
        client = str(row.get("c") or "").strip()
        session = str(row.get("s") or "").strip()
        category = str(row.get("k") or "").strip()
        if not client or not session:
            continue
        if category not in TOOL_CATEGORIES:
            category = "other"
        key = (client, session)
        bucket = counts.setdefault(key, {})
        bucket[category] = bucket.get(category, 0) + 1
        # Tool NAME (present only on ticks written after name capture shipped —
        # older lines simply have no ``n``, an honest undercount of names, never
        # a wrong one).
        name = normalize_tool_name(row.get("n"))
        if name:
            name_bucket = name_counts.setdefault(key, {})
            name_bucket[name] = name_bucket.get(name, 0) + 1
        # Touched file path (present only on edit-tool ticks written after path
        # capture shipped). Deduped, insertion-ordered, and hard-capped per batch.
        touched = _normalize_touched_path(row.get("p"))
        if touched:
            path_bucket = touched_paths.setdefault(key, [])
            if touched not in path_bucket and len(path_bucket) < _TOUCHED_FILES_PER_BATCH_MAX:
                path_bucket.append(touched)
        # Command an execute tool ran (present only on ticks written after command
        # capture shipped). Deduped, insertion-ordered, hard-capped per batch.
        command = _normalize_command(row.get("cmd"))
        if command:
            cmd_bucket = commands.setdefault(key, [])
            if command not in cmd_bucket and len(cmd_bucket) < _COMMANDS_PER_BATCH_MAX:
                cmd_bucket.append(command)
        stamp = row.get("t")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            latest[key] = max(latest.get(key, 0.0), float(stamp))

    events: list[dict[str, Any]] = []
    for (client, session), bucket in sorted(counts.items()):
        created_at = latest.get((client, session)) or (float(now) if now is not None else 0.0)
        digest = uuid.uuid5(
            uuid.NAMESPACE_URL, f"{batch_token}\0{client}\0{session}"
        ).hex
        metadata: dict[str, Any] = {
            "client": client,
            "client_session_id": session,
            "tool_category_counts": dict(sorted(bucket.items())),
            "capture_basis": TOOL_ACTIVITY_CAPTURE_BASIS,
            "captured_at": created_at,
            "sentinel_semantic_kind": "tool_activity",
        }
        names = name_counts.get((client, session))
        if names:
            # Tool names ride as list VALUES, not dict KEYS. The store's secret
            # redaction blanks the VALUE of any credential-shaped KEY, so a
            # connector like ``mcp__vault__get_token`` used as a key would be
            # silently redacted out. As ``{"name": …, "count": …}`` objects the
            # name is a value under the innocuous key ``name`` and survives.
            metadata["tool_names"] = [
                {"name": name, "count": count} for name, count in sorted(names.items())
            ]
        touched = touched_paths.get((client, session))
        if touched:
            # Paths ride as a plain list of string VALUES (same redaction-safe shape
            # rationale as tool_names): a path is never a dict KEY, so the store's
            # secret redaction can never blank it.
            metadata["touched_files"] = list(touched)
        cmds = commands.get((client, session))
        if cmds:
            # Commands ride as a plain list of string VALUES too — the store's secret
            # redaction is KEY-based, so a command placed under a value never triggers it
            # (the command was already best-effort scrubbed at ``_normalize_command``).
            metadata["commands"] = list(cmds)
        events.append(
            {
                "event_id": f"toolact:{digest}",
                "created_at": created_at,
                "source": client,
                "event_type": TOOL_ACTIVITY_EVENT_TYPE,
                "run_id": None,
                "metadata": metadata,
            }
        )
    return events


# A client whose hook does not capture tool activity can still have it derived
# from its own transcript at discovery time (currently Codex, from the rollout).
# These events carry a DISTINCT capture_basis so they are honestly labelled as
# transcript-scan-derived (not hook-observed) and can be refreshed as a scoped
# cohort — replaced per (client, session) on each import instead of accumulated.
DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS = "transcript_scan_tool_activity"

# A tool-activity event's ``capture_basis`` -> the Receipt provenance TOKEN naming
# where the Actions came from. The tokens are the receipt ``SOURCE_*`` values
# (``hook`` / ``transcript_scan``); an unknown basis maps to nothing so the Receipt
# falls back to its own default rather than inventing a source. This is the single
# place the two capture bases are translated to the user-facing provenance word, so
# a scan-derived Action is never mislabelled as hook-observed.
TOOL_ACTIVITY_CAPTURE_SOURCE: dict[str, str] = {
    TOOL_ACTIVITY_CAPTURE_BASIS: "hook",
    DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS: "transcript_scan",
}


def is_discovery_tool_activity_event(event: Mapping[str, Any]) -> bool:
    """Whether an event is a discovery-derived (transcript-scan) tool-activity row."""

    if event.get("event_type") != TOOL_ACTIVITY_EVENT_TYPE:
        return False
    metadata = event.get("metadata")
    return (
        isinstance(metadata, Mapping)
        and metadata.get("capture_basis") == DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS
    )


def build_discovery_tool_activity_event(
    *,
    client: str,
    session_id: str,
    activity: Mapping[str, Any] | None,
    captured_at: float,
) -> dict[str, Any] | None:
    """Build ONE ``tool_activity_observed`` event from transcript-scan-derived
    Actions signals, in the exact shape the hook drain emits so it flows through
    the same client-agnostic Actions build. Returns ``None`` when there is no real
    signal. The event_id is stable per (client, session): a scoped ``replace_events``
    refreshes it on each import, so a still-growing session is never frozen and a
    re-import never double-counts."""

    if not activity:
        return None
    metadata: dict[str, Any] = {
        "client": client,
        "client_session_id": session_id,
        "capture_basis": DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS,
        "captured_at": captured_at,
        "sentinel_semantic_kind": "tool_activity",
    }
    category_counts = activity.get("tool_category_counts")
    if isinstance(category_counts, Mapping) and category_counts:
        metadata["tool_category_counts"] = {
            str(key): int(value)
            for key, value in sorted(category_counts.items())
            if isinstance(value, int) and not isinstance(value, bool)
        }
    names = activity.get("tool_names")
    if isinstance(names, list) and names:
        # Same redaction-safe shape as the drain: names ride as list VALUES.
        metadata["tool_names"] = [
            {"name": str(entry.get("name")), "count": int(entry.get("count") or 0)}
            for entry in names
            if isinstance(entry, Mapping) and entry.get("name")
        ]
    touched = activity.get("touched_files")
    if isinstance(touched, list) and touched:
        metadata["touched_files"] = [str(path) for path in touched if str(path).strip()]
    commands = activity.get("commands")
    if isinstance(commands, list) and commands:
        metadata["commands"] = [str(cmd) for cmd in commands if str(cmd).strip()]
    # Only the five base keys means nothing survived normalization → no event.
    if len(metadata) <= 5:
        return None
    digest = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS}\0{client}\0{session_id}",
    ).hex
    return {
        "event_id": f"toolact:{digest}",
        "created_at": captured_at,
        "source": client,
        "event_type": TOOL_ACTIVITY_EVENT_TYPE,
        "run_id": None,
        "metadata": metadata,
    }


def ingest_tool_activity_spool(
    store_dir: Path | str,
    *,
    record: Callable[[dict[str, Any]], Any],
    now: float | None = None,
    token: str | None = None,
) -> int:
    """Drain the spool and hand each batch event to ``record``. Never raises.

    Mirrors ``rate_limits.record_snapshots_transitionally``'s
    ``record=service.record_event`` contract so the usage importer can call it
    the same way it records rate-limit snapshots.
    """

    try:
        events = drain_tool_activity_spool(store_dir, now=now, token=token)
    except Exception:  # noqa: BLE001 - a spool drain must never fail the import.
        return 0
    written = 0
    for event in events:
        try:
            record(event)
            written += 1
        except Exception:  # noqa: BLE001 - one bad record must not abort the rest.
            continue
    return written


def _sum_additive_tool_activity(
    events: Iterable[Mapping[str, Any]],
    *,
    extract: Callable[[Mapping[str, Any]], Iterable[tuple[str, int]]],
) -> dict[tuple[str, str], dict[str, int]]:
    """Sum a per-(client, session) additive tool-activity field across events.

    Batches are additive, so the per-session total is stable across import runs.
    ONE exception keeps the sum honest: a transcript-scan-derived event (a
    client whose hook does not capture tool activity, so it is scanned from the
    transcript — currently Codex) is the FULL-transcript SUPERSET of whatever the
    same session's PreToolUse hook also observed, so summing both would
    double-count the overlapping calls. For a session that has any transcript-scan
    event, only its transcript-scan events contribute; the hook events for that
    session are dropped (regardless of event order in the log).
    """

    result: dict[tuple[str, str], dict[str, int]] = {}
    scan_superseded: set[tuple[str, str]] = set()
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("event_type") != TOOL_ACTIVITY_EVENT_TYPE:
            continue
        metadata = event.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        client = str(metadata.get("client") or "").strip()
        session = str(metadata.get("client_session_id") or "").strip()
        if not client or not session:
            continue
        key = (client, session)
        is_scan = (
            metadata.get("capture_basis") == DISCOVERY_TOOL_ACTIVITY_CAPTURE_BASIS
        )
        if key in scan_superseded and not is_scan:
            # A transcript scan already covers this session's full tool activity;
            # its hook events would double-count the overlap.
            continue
        if is_scan and key not in scan_superseded:
            # Drop any hook contributions accumulated before the scan was seen.
            result.pop(key, None)
            scan_superseded.add(key)
        bucket = result.setdefault(key, {})
        for name, value in extract(metadata):
            bucket[name] = bucket.get(name, 0) + value
    return {key: value for key, value in result.items() if value}


def _category_count_pairs(metadata: Mapping[str, Any]) -> Iterable[tuple[str, int]]:
    counts = metadata.get("tool_category_counts")
    if not isinstance(counts, Mapping):
        return
    for category, value in counts.items():
        if category not in TOOL_CATEGORIES:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        yield str(category), value


def _tool_name_count_pairs(metadata: Mapping[str, Any]) -> Iterable[tuple[str, int]]:
    names = metadata.get("tool_names")
    if not isinstance(names, list):
        return
    for entry in names:
        if not isinstance(entry, Mapping):
            continue
        name = normalize_tool_name(entry.get("name"))
        if not name:
            continue
        value = entry.get("count")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        yield name, value


def build_tool_activity_by_session(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, int]]:
    """Sum ``tool_activity_observed`` batch counts per (client, session).

    Batches are additive: reducing the whole event log sums every recorded
    batch, so the per-session total is stable no matter how many import runs
    produced it. Only known categories and positive integer counts are kept. A
    transcript-scan event supersedes the same session's hook events (see
    ``_sum_additive_tool_activity``) so the two sources never double-count.
    """

    return _sum_additive_tool_activity(events, extract=_category_count_pairs)


def build_tool_names_by_session(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, int]]:
    """Sum ``tool_activity_observed`` per-NAME counts per (client, session).

    Mirrors ``build_tool_activity_by_session`` for the specific tool names
    (``Read``, ``Bash``, ``mcp__server__tool``, …). Reads the event's
    ``tool_names`` list of ``{"name": …, "count": …}`` objects — the name is a
    VALUE, not a dict key, so a credential-shaped connector name is never
    redacted out by the store's secret redaction. Batches are additive, so the
    per-session total is stable across imports (a transcript scan supersedes the
    session's hook events, as above). Names are an OPEN set, so unlike categories
    they are not filtered against a fixed vocabulary — but they are normalized
    (trimmed, length-bounded) and only positive integer counts are kept. A
    session recorded before name capture shipped simply has no names — an honest
    gap, never a fabricated zero.
    """

    return _sum_additive_tool_activity(events, extract=_tool_name_count_pairs)


def build_touched_files_by_session(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], list[str]]:
    """Collect ``tool_activity_observed`` touched-file paths per (client, session).

    Mirrors ``build_tool_names_by_session``: reads each event's ``touched_files``
    list (cwd-relative paths a file-edit tool wrote, or ``~/…`` for a home file).
    Batches are additive and the
    union is deduped (insertion-ordered) across imports. A session recorded before
    path capture shipped simply has none — an honest gap, never a fabricated entry.
    Every path is re-run through ``_normalize_touched_path`` here (the third gate,
    after the tick and the drain): it rejects any absolute/drive/UNC/NUL path (never an
    absolute prefix), but KEEPS a ``../`` escape so an out-of-tree edit survives — the
    projection's ``_safe_relative_posix_path`` is stricter and drops ``..`` (it guards a
    different, section-reported input).
    """

    result: dict[tuple[str, str], list[str]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("event_type") != TOOL_ACTIVITY_EVENT_TYPE:
            continue
        metadata = event.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        client = str(metadata.get("client") or "").strip()
        session = str(metadata.get("client_session_id") or "").strip()
        if not client or not session:
            continue
        paths = metadata.get("touched_files")
        if not isinstance(paths, list):
            continue
        bucket = result.setdefault((client, session), [])
        for candidate in paths:
            normalized = _normalize_touched_path(candidate)
            if normalized and normalized not in bucket:
                bucket.append(normalized)
    return {key: value for key, value in result.items() if value}


def build_commands_by_session(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], list[str]]:
    """Collect ``tool_activity_observed`` commands per (client, session).

    Mirrors ``build_touched_files_by_session``: reads each event's ``commands`` list
    (single-line, best-effort-scrubbed command strings an execute tool ran). Batches are
    additive and the union is deduped (insertion-ordered) across imports. Each command is
    re-run through ``_normalize_command`` here (a second scrub + bound), so a command
    recorded before a scrub rule shipped is re-masked on read. A session recorded before
    command capture shipped simply has none — an honest gap, never a fabricated entry."""

    result: dict[tuple[str, str], list[str]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("event_type") != TOOL_ACTIVITY_EVENT_TYPE:
            continue
        metadata = event.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        client = str(metadata.get("client") or "").strip()
        session = str(metadata.get("client_session_id") or "").strip()
        if not client or not session:
            continue
        cmds = metadata.get("commands")
        if not isinstance(cmds, list):
            continue
        bucket = result.setdefault((client, session), [])
        for candidate in cmds:
            normalized = _normalize_command(candidate)
            if normalized and normalized not in bucket:
                bucket.append(normalized)
    return {key: value for key, value in result.items() if value}


def build_tool_activity_capture_basis_by_session(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], list[str]]:
    """The SET of tool-activity CAPTURE BASES present per (client, session).

    Which source captured a session's Actions — a client hook, a transcript scan,
    or (rarely) both — so the Receipt can name Actions provenance HONESTLY instead
    of assuming a hook. The sibling ``build_*_by_session`` reducers drop the
    ``capture_basis`` (they only need the counts/paths); this keeps it. Unlike the
    additive counters it is NOT superseded: it reports every basis that contributed
    a signal, which is the honest answer for the dimension (a scan supersedes a
    hook's category counts, but the hook's commands/paths still unioned in). Each
    basis is translated once, here, to its user-facing provenance token
    (``hook`` / ``transcript_scan``) via ``TOOL_ACTIVITY_CAPTURE_SOURCE``; an
    unrecognised basis contributes nothing. Returned as a sorted, deduped list."""

    result: dict[tuple[str, str], set[str]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("event_type") != TOOL_ACTIVITY_EVENT_TYPE:
            continue
        metadata = event.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        client = str(metadata.get("client") or "").strip()
        session = str(metadata.get("client_session_id") or "").strip()
        if not client or not session:
            continue
        source = TOOL_ACTIVITY_CAPTURE_SOURCE.get(str(metadata.get("capture_basis") or ""))
        if source is None:
            continue
        result.setdefault((client, session), set()).add(source)
    return {key: sorted(value) for key, value in result.items() if value}


__all__ = [
    "TOOL_ACTIVITY_CAPTURE_BASIS",
    "TOOL_ACTIVITY_CAPTURE_SOURCE",
    "TOOL_ACTIVITY_EVENT_TYPE",
    "TOOL_CATEGORIES",
    "build_commands_by_session",
    "build_tool_activity_by_session",
    "build_tool_activity_capture_basis_by_session",
    "build_tool_names_by_session",
    "build_touched_files_by_session",
    "drain_tool_activity_spool",
    "ingest_tool_activity_spool",
    "normalize_tool_name",
    "record_tool_activity_tick",
    "tool_activity_spool_path",
    "tool_category",
]
