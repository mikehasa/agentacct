from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .install_guide import session_start_additional_context
from .store_resolution import (
    ENV_STORE_DIR,
    StoreResolutionError,
    claude_worktree_owner_path_text,
    store_env_dir_value,
    worktree_owner_root,
)

HookAction = Literal["allow", "checkpoint", "block"]


@dataclass(frozen=True)
class HookDecision:
    decision: HookAction
    reason: str
    risk: Literal["low", "medium", "high"] = "low"

    def to_json(self) -> dict[str, Any]:
        # frozen: the "agent_sentinel" decision key predates the Agent
        # agentacct rename; installed hook wrappers/consumers parse this exact
        # field forever, so it is wire vocabulary, not branding.
        return {
            "agent_sentinel": {
                "decision": self.decision,
                "risk": self.risk,
                "reason": self.reason,
            }
        }


_DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+-[^\n;|&]*r[^\n;|&]*f\b"),
    re.compile(r"\bsudo\s+(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r"\bmkfs(\.|\s|$)"),
    re.compile(r"\bdd\s+.*\bof=/dev/"),
    re.compile(r"\bchmod\s+-R\s+777\b"),
]

_NETWORK_PATTERNS = [
    re.compile(r"\bcurl\b.*\|\s*(sh|bash)\b"),
    re.compile(r"\bwget\b.*\|\s*(sh|bash)\b"),
    re.compile(r"\bnpm\s+install\b"),
    re.compile(r"\bpip\s+install\b"),
]

_WRITE_OR_GIT_PATTERNS = [
    re.compile(r"\bgit\s+push\b"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+clean\s+-"),
]


def _command_from_event(event: dict[str, Any]) -> str:
    tool_input = event.get("tool_input") or event.get("input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return str(tool_input.get("command") or tool_input.get("cmd") or "")


def evaluate_claude_code_event(event: dict[str, Any]) -> HookDecision:
    """Evaluate a Claude Code-style hook event.

    This is an intentionally small first-pass policy engine. It does not try to
    replace Claude Code permissions; it provides a reusable agentacct decision
    shape that can later be wired into hook installers, budgets, and audits.
    """
    tool_name = str(event.get("tool_name") or event.get("tool") or "").lower()
    command = _command_from_event(event).strip()

    if tool_name in {"read", "grep", "glob", "ls"}:
        return HookDecision("allow", "read-only tool allowed", "low")

    if tool_name == "bash" or command:
        lowered = command.lower()
        for pattern in _DESTRUCTIVE_PATTERNS:
            if pattern.search(lowered):
                return HookDecision("block", f"blocked destructive shell command: {pattern.pattern}", "high")
        for pattern in _NETWORK_PATTERNS:
            if pattern.search(lowered):
                return HookDecision("checkpoint", "network/install shell command needs human review", "medium")
        for pattern in _WRITE_OR_GIT_PATTERNS:
            if pattern.search(lowered):
                return HookDecision("checkpoint", "repository-mutating command needs human review", "medium")
        return HookDecision("allow", "shell command allowed by initial policy", "low")

    if tool_name in {"write", "edit", "multiedit"}:
        return HookDecision("checkpoint", "file-mutating tool needs review in initial policy", "medium")

    return HookDecision("allow", "no blocking rule matched", "low")


def evaluate_stdin_json(raw: str) -> dict[str, Any]:
    try:
        event = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return HookDecision("block", f"invalid hook event JSON: {exc}", "high").to_json()
    if not isinstance(event, dict):
        return HookDecision("block", "hook event must be a JSON object", "high").to_json()
    return evaluate_claude_code_event(event).to_json()


# --- Claude Code hook -> agentacct client-context bridge -----------------------
#
# Claude Code exposes session_id/transcript_path ONLY to hooks (stdin JSON),
# never to the model or MCP servers. The hook persists a small, secret-free
# "current client context" file under the project store so the MCP server can
# hand real, client-derived join ids to sections without the model guessing.

CLAUDE_CODE_HOOK_CONTEXT_RELATIVE_PATH = Path("client-context/claude-code.json")
# Per-session context files (one per client session) live next to the legacy
# single slot. The legacy slot is still dual-written so MCP server processes
# running pre-multi-slot code keep working until they restart.
CLAUDE_CODE_HOOK_CONTEXT_DIR_RELATIVE_PATH = Path("client-context/claude-code")
# frozen: historical stores/logs/files carry this schema string forever (the
# live hook writes it into store files); stored identifiers are data format,
# not branding — never rename.
CLAUDE_CODE_HOOK_CONTEXT_SCHEMA = "agent-sentinel.client-context.v1"
# The PreToolUse hook fires before every tool call (matcher "*"), including the
# MCP section call itself, so during active work the file is seconds old. The
# TTL only bounds the tail risk of a context surviving into a later session
# (e.g. /clear followed by MCP-only work that never fires a hooked tool).
CLAUDE_CODE_HOOK_CONTEXT_MAX_AGE_SECONDS = 1800.0
_MAX_CONTEXT_ID_LENGTH = 240
# Bound on live per-session context files; oldest are pruned beyond this.
_HOOK_CONTEXT_MAX_FILES = 16
# Ancestry captured by the hook CLI (wrapper python -> optional sh -> claude
# -> terminal ...): depth 12 leaves a large margin above the claude process.
_HOOK_ANCESTOR_MAX_DEPTH = 12
# Ancestry used by the consuming MCP server: its parent (or grandparent when
# spawned via a non-exec'ing shell) is its own claude process. Keeping this
# shallow prevents false lineage matches through shared distant ancestors
# (terminal emulator, launchd).
_CONSUMER_ANCESTOR_MAX_DEPTH = 2
# Orphaned atomic-write temp files older than this are cleaned up.
_HOOK_CONTEXT_TMP_MAX_AGE_SECONDS = 300.0
# Persisted ancestor pid lists longer than this are dropped (not trusted).
_MAX_HOOK_ANCESTOR_PIDS = 16


def claude_code_hook_context_path(store_dir: Path | str) -> Path:
    return Path(store_dir) / CLAUDE_CODE_HOOK_CONTEXT_RELATIVE_PATH


def claude_code_hook_context_dir(store_dir: Path | str) -> Path:
    return Path(store_dir) / CLAUDE_CODE_HOOK_CONTEXT_DIR_RELATIVE_PATH


def process_ancestor_pids(pid: int | None = None, *, max_depth: int = _HOOK_ANCESTOR_MAX_DEPTH) -> list[int]:
    """Ancestor pids of ``pid`` (default: this process), nearest first.

    Privacy: integers only — process names/commands are never read, so nothing
    beyond pid numbers can reach persisted state. Returns ``[]`` on ANY
    failure (missing ps, timeout, parse error) and never raises; a missing
    ancestry simply disables pid-lineage disambiguation.
    """
    try:
        start = int(pid) if pid is not None else os.getpid()
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode != 0:
            return []
        parent_of: dict[int, int] = {}
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                parent_of[int(parts[0])] = int(parts[1])
            except ValueError:
                continue
        ancestors: list[int] = []
        seen = {start}
        current = start
        while len(ancestors) < max_depth:
            parent = parent_of.get(current)
            if parent is None or parent <= 1 or parent in seen:
                break
            ancestors.append(parent)
            seen.add(parent)
            current = parent
        return ancestors
    except Exception:  # noqa: BLE001 - ancestry is best-effort and must never break hook or server flows.
        return []


def _hook_context_filename(session_id: str) -> str:
    """Deterministic, traversal-safe per-session filename.

    The same session always maps to the same file (a session's repeated hooks
    overwrite one slot); the digest keeps distinct sessions collision-safe
    even when their sanitized prefixes coincide.
    """
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:8] or "session"
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}.json"


def _safe_path_component(value: str) -> PurePosixPath:
    """Parse a path string separator-agnostically for basename extraction.

    Hook input can carry Windows-style paths (e.g. under WSL) while agentacct
    runs on a POSIX host, where pathlib treats backslashes as ordinary
    characters — Path(r"C:\\Users\\x\\proj").name would be the FULL raw path.
    Normalizing both separators guarantees only the last component survives.
    """
    return PurePosixPath(value.replace("\\", "/"))


def _is_home_directory_text(path_text: str) -> bool:
    """True when the path names the home directory itself (label must be '~')."""
    try:
        return Path(path_text.rstrip("/\\")).expanduser() == Path.home()
    except (OSError, RuntimeError, ValueError):
        return False


def derive_claude_code_client_context(event: dict[str, Any]) -> dict[str, Any] | None:
    """Derive joinable client context from a Claude Code hook event.

    Only identity fields are kept: never tool_input, prompts, or other payload
    data. The transcript is reduced to its file stem — the same value the local
    usage importer records as client_transcript_id — so no raw transcript path
    is persisted.
    """
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > _MAX_CONTEXT_ID_LENGTH:
        return None
    transcript_path = event.get("transcript_path")
    transcript_id: str | None = None
    if isinstance(transcript_path, str) and transcript_path:
        stem = _safe_path_component(transcript_path).stem
        if stem and len(stem) <= _MAX_CONTEXT_ID_LENGTH:
            transcript_id = stem
    # Privacy: never persist the raw cwd — only its basename as a display
    # label. Store resolution may USE the full cwd in memory, but the full
    # path must not be written to the context file or inherited into events.
    # Worktree remap (same rule as store resolution and read-time labels): a
    # cwd inside `<owner>/.claude/worktrees/<name>` labels as the OWNER repo,
    # not the meaningless worktree folder name. Pure string parse; the full
    # path is still never persisted, so this is the only chance to remap.
    cwd = event.get("cwd")
    project_label: str | None = None
    if isinstance(cwd, str) and cwd:
        owner_text = claude_worktree_owner_path_text(cwd)
        label_source = owner_text or cwd
        # Redaction rule (same as the read-time labeler): when the labeled
        # directory IS the home directory — a cwd of $HOME, or a worktree
        # under $HOME/.claude/worktrees whose owner remaps to $HOME — the
        # label is "~", never the account username (the last path segment IS
        # the username there).
        if _is_home_directory_text(label_source):
            project_label = "~"
        else:
            project_label = _safe_path_component(label_source).name or None
    hook_event_name = event.get("hook_event_name")
    return {
        "schema_version": CLAUDE_CODE_HOOK_CONTEXT_SCHEMA,
        "client": "claude-code",
        "client_session_id": session_id,
        "client_transcript_id": transcript_id,
        "project_label": project_label or None,
        "source": "claude_code_hook",
        "hook_event_name": hook_event_name if isinstance(hook_event_name, str) else None,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    # The pid in the tmp name removes the cross-process rename race when two
    # concurrent sessions' hooks write under the same directory.
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _prune_hook_context_dir(
    context_dir: Path,
    *,
    now: float,
    max_age_seconds: float = CLAUDE_CODE_HOOK_CONTEXT_MAX_AGE_SECONDS,
) -> None:
    """Best-effort cleanup of the per-session context dir. Never raises."""
    try:
        entries = list(context_dir.iterdir())
    except OSError:
        return
    valid: list[tuple[float, Path]] = []
    for entry in entries:
        try:
            if entry.name.endswith(".tmp"):
                if now - entry.stat().st_mtime > _HOOK_CONTEXT_TMP_MAX_AGE_SECONDS:
                    entry.unlink()
                continue
            if entry.suffix != ".json" or not entry.is_file():
                continue
            try:
                payload = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                payload = None
            normalized = (
                _validate_hook_context_payload(payload, now=now, max_age_seconds=float("inf"))
                if payload is not None
                else None
            )
            if normalized is None:
                # Invalid or undecodable: delete only once its mtime is past
                # the TTL — a recent unreadable file may be a newer schema.
                if now - entry.stat().st_mtime > max_age_seconds:
                    entry.unlink()
                continue
            if now - normalized["observed_at"] > max_age_seconds:
                entry.unlink()
                continue
            valid.append((normalized["observed_at"], entry))
        except OSError:
            continue
    if len(valid) > _HOOK_CONTEXT_MAX_FILES:
        valid.sort(key=lambda item: item[0])
        for _, stale_entry in valid[: len(valid) - _HOOK_CONTEXT_MAX_FILES]:
            try:
                stale_entry.unlink()
            except OSError:
                continue


def write_claude_code_hook_context(store_dir: Path | str, context: dict[str, Any], *, now: float | None = None) -> Path:
    current = time.time() if now is None else float(now)
    payload = {**context, "observed_at": current}
    # Legacy single slot: dual-written so old MCP server processes (which read
    # only this file) keep today's behavior until they restart on new code.
    path = claude_code_hook_context_path(store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, payload)
    session_id = context.get("client_session_id")
    if isinstance(session_id, str) and session_id:
        try:
            context_dir = claude_code_hook_context_dir(store_dir)
            context_dir.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(context_dir / _hook_context_filename(session_id), payload)
            _prune_hook_context_dir(context_dir, now=current)
        except Exception:  # noqa: BLE001 - per-session bookkeeping must never break the legacy slot write.
            pass
    return path


# Shared with the CLI store resolver (store_resolution.resolve_store_dir) so
# hook capture and CLI commands agree on which store a worktree session uses.
_worktree_owner_root = worktree_owner_root


def _hook_store_dir_from_event(event: dict[str, Any]) -> Path | None:
    """Locate the project-local store the hook should write to.

    AGENTACCT_STORE_DIR (or its pre-rename aliases AGENT_CHRONICLE_STORE_DIR /
    AGENT_SENTINEL_STORE_DIR) comes FIRST, with the exact store_resolution
    semantics (absolute path required): every consumer of hook context — the
    MCP server, CLI commands, doctor — resolves env-first, so hook capture
    must publish into the same store or the env split would let one project's
    session ids be inherited by another project's server (cross-project
    attribution). A relative env value is invalid for consumers (the resolver
    refuses it), so the hook captures nothing rather than diverging.

    CLAUDE_PROJECT_DIR — the project root Claude Code sets for hook commands —
    is authoritative next: if set, only that project's store is considered, so
    a session can never publish its ids into a different project's store. The
    hook event cwd is a fallback for non-Claude-Code callers, walking up at
    most to the nearest repository boundary. Only EXISTING .agent-sentinel/state
    dirs are used on those paths: a hook firing in an uninitialized project
    never creates agentacct state as a side effect. (An explicit env store is
    user intent, not a side effect, mirroring resolve_store_dir.)

    Worktree remap (deliberate Phase 1 decision): a Claude Code worktree at
    ``<owner>/.claude/worktrees/<name>`` belongs to its owning project, so a
    session running there publishes its context into the OWNER's store (the
    worktree's own store still wins when it exists). This replaces the old
    stance that worktree sessions never publish to the root store.
    """
    try:
        env_value = store_env_dir_value(os.environ)
    except StoreResolutionError:
        # Conflicting new/legacy store env values: consumers refuse to resolve,
        # so the hook captures nothing rather than diverging from them.
        return None
    if env_value:
        env_path = Path(env_value).expanduser()
        if not env_path.is_absolute():
            # Same contract as store_resolution.resolve_store_dir: a relative
            # env value is refused there, so capturing into a diverging store
            # here would split hook capture from every consumer.
            return None
        return env_path
    project_env = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_env:
        project_root = Path(project_env)
        store = project_root / ".agent-sentinel" / "state"
        if store.is_dir():
            return store
        owner = _worktree_owner_root(project_root)
        if owner is not None:
            owner_store = owner / ".agent-sentinel" / "state"
            if owner_store.is_dir():
                return owner_store
        return None
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        candidate = Path(cwd).resolve()
    except OSError:
        return None
    while True:
        store = candidate / ".agent-sentinel" / "state"
        if store.is_dir():
            return store
        if (candidate / ".git").exists():
            owner = _worktree_owner_root(candidate)
            if owner is not None:
                # Continue the walk from the owning repo root; owner is
                # strictly shallower than the worktree, so this terminates.
                candidate = owner
                continue
            # Repository boundary without a store: do not leak this session's
            # ids into an ancestor project's store.
            return None
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def capture_claude_code_client_context(raw: str, *, store_dir: Path | str | None = None) -> Path | None:
    """Best-effort context capture from raw hook stdin. Never raises."""
    try:
        event = json.loads(raw or "{}")
        if not isinstance(event, dict):
            return None
        context = derive_claude_code_client_context(event)
        if context is None:
            return None
        target = Path(store_dir) if store_dir is not None else _hook_store_dir_from_event(event)
        if target is None:
            return None
        # Ancestry of the hook CLI process (wrapper python -> optional sh ->
        # claude -> ...) lets the MCP server later prove which concurrent
        # session's context is its own. Integers only; added here so
        # derive_claude_code_client_context stays a pure ids-only function.
        ancestor_pids = process_ancestor_pids()
        if ancestor_pids:
            context["hook_ancestor_pids"] = ancestor_pids
        return write_claude_code_hook_context(target, context)
    except Exception:  # noqa: BLE001 - a capture failure must never affect the hook decision.
        return None


def capture_tool_activity(
    raw: str, *, store_dir: Path | str | None = None, client: str = "claude-code"
) -> None:
    """Record ONE tool-activity tick for this PreToolUse. Never raises.

    The PreToolUse hook already receives the tool name (and fails open), so this
    is the cheapest honest place to observe what the agent reached for. The
    category AND the tool NAME are spooled — never arguments or any payload — so
    the Receipt's Actions dimension can show both what KIND of tool ran and which
    SPECIFIC tool/connector. The spool lands in the SAME store as the
    client-context bridge so the usage importer drains both from one place.

    ``client`` labels which agent emitted the tick. Codex's PreToolUse hook uses
    the same STDIN shape (``session_id``/``tool_name``), and its ``session_id``
    equals the rollout id the usage importer keys on, so a ``codex`` tick
    attributes straight to the Codex session with no log pairing.
    """

    try:
        from .tool_activity import record_tool_activity_tick, tool_category

        event = json.loads(raw or "{}")
        if not isinstance(event, dict):
            return
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > _MAX_CONTEXT_ID_LENGTH:
            return
        target = Path(store_dir) if store_dir is not None else _hook_store_dir_from_event(event)
        if target is None:
            return
        tool_name = event.get("tool_name") or event.get("tool")
        category = tool_category(tool_name)
        record_tool_activity_tick(
            target,
            client=client,
            session_id=session_id,
            category=category,
            name=tool_name,
            at=time.time(),
        )
    except Exception:  # noqa: BLE001 - activity capture must never affect the hook decision.
        return


def capture_session_end(
    raw: str, *, store_dir: Path | str | None = None, client: str = "claude-code"
) -> None:
    """Record ONE session-end tick for this SessionEnd hook. Never raises.

    Spools only content-free identity + timing — client, session id, the end
    ``reason`` (a short enum like ``clear``/``logout``), and a timestamp — so the
    reducer can infer an ``ended_open`` disposition for a step whose session
    stopped without a recorded terminal. Never reads conversation content. The
    SessionEnd hook fires only for a ROOT session, so no subagent-end noise.

    ``client`` labels the emitting agent. Codex's SessionEnd hook carries the
    same ``session_id`` (equal to the rollout id), so a ``codex`` end fact keys
    to the right Codex session for the ``ended_open`` inference.
    """

    try:
        from .session_lifecycle import record_session_end_tick

        event = json.loads(raw or "{}")
        if not isinstance(event, dict):
            return
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > _MAX_CONTEXT_ID_LENGTH:
            return
        target = Path(store_dir) if store_dir is not None else _hook_store_dir_from_event(event)
        if target is None:
            return
        record_session_end_tick(
            target,
            client=client,
            session_id=session_id,
            reason=event.get("reason"),
            at=time.time(),
        )
    except Exception:  # noqa: BLE001 - session-end capture must never affect the hook.
        return


def _hook_exit_code(event: dict[str, Any]) -> int | None:
    """Read the Bash exit code from a PostToolUse event, tolerant of the
    tool_output/tool_response key and exit_code/exitCode/code spellings. 0 is a
    valid value, so a missing code (None) is kept distinct from success (0)."""

    for container_key in ("tool_output", "tool_response", "toolOutput"):
        container = event.get(container_key)
        if not isinstance(container, dict):
            continue
        for code_key in ("exit_code", "exitCode", "returnCode", "code"):
            value = container.get(code_key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
    return None


def capture_mechanical_check(raw: str, *, store_dir: Path | str | None = None) -> None:
    """Record ONE mechanical-check tick for this PostToolUse. Never raises.

    When the agent ran a recognized test / build / lint / typecheck command, spool
    the exit code the HARNESS observed — the only local source of a check that is
    independent of the agent's own report, and what lifts a step to
    ``independently_checked``. Only a coarse check kind, the runner name, and a
    sha256 command DIGEST are spooled — never the command string, its arguments,
    or its output. The spool lands in the SAME store as the tool-activity and
    client-context bridges so the usage importer drains them from one place.
    """

    try:
        from .mechanical_capture import (
            classify_command,
            command_digest,
            record_mechanical_check_tick,
        )

        event = json.loads(raw or "{}")
        if not isinstance(event, dict):
            return
        if str(event.get("tool_name") or event.get("tool") or "").lower() != "bash":
            return
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > _MAX_CONTEXT_ID_LENGTH:
            return
        tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
        command = tool_input.get("command")
        classified = classify_command(command)
        if classified is None:
            return
        exit_code = _hook_exit_code(event)
        if exit_code is None:
            return
        target = Path(store_dir) if store_dir is not None else _hook_store_dir_from_event(event)
        if target is None:
            return
        check_kind, runner = classified
        record_mechanical_check_tick(
            target,
            client="claude-code",
            session_id=session_id,
            check_kind=check_kind,
            runner=runner,
            digest=command_digest(str(command or "")),
            exit_code=exit_code,
            at=time.time(),
        )
    except Exception:  # noqa: BLE001 - capture must never affect the hook.
        return


def is_subagent_session_start(event: Any) -> bool:
    """True when a SessionStart event fired inside a subagent session.

    Claude Code populates agent identity fields (``agent_id``/``agent_type``)
    when SessionStart fires in a subagent (Task tool) session. Subagent
    sessions are skipped entirely: injecting the record-your-work directive
    there would multiply sections per user task (child sessions opening their
    own sections is noise — the ROOT session owns the semantic goal), and
    capturing context for a burst of subagents could evict the root session's
    context file from the capped per-session dir.
    """
    if not isinstance(event, dict):
        return False
    for key in ("agent_id", "agent_type"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return True
    return False


def claude_session_start_response(raw: str) -> dict[str, Any]:
    """The JSON response for a Claude Code SessionStart hook event.

    Root sessions get additionalContext directing the agent to record its
    work as sections (single source: ``install_guide``, shared recording
    contract). Subagent sessions and malformed events get an empty object —
    the documented no-op SessionStart response. Never raises.
    """
    try:
        event = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(event, dict) or event.get("hook_event_name") != "SessionStart":
        # Only events that declare themselves SessionStart get the directive:
        # real events always carry the field (the wrapper dispatches on it),
        # and anything else is malformed input this hook must no-op on.
        return {}
    if is_subagent_session_start(event):
        return {}
    context = session_start_additional_context()
    # On a resumed/compacted start — the moment work is most likely mid-handoff —
    # append a targeted reminder to close the loop with a terminal status. This is
    # the injection-capable hook (SessionEnd/PreCompact cannot inject); it lifts
    # the AGENT-asserted quality, while SessionEnd's inferred ``ended_open`` is the
    # safety net for when the reminder is not acted on.
    source = event.get("source")
    if isinstance(source, str) and source in ("resume", "compact"):
        from .install_guide import session_resume_additional_context

        context = f"{context}\n\n{session_resume_additional_context()}"
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def _validate_hook_context_payload(payload: Any, *, now: float, max_age_seconds: float) -> dict[str, Any] | None:
    """Validate and normalize one hook-context payload (any slot)."""
    if not isinstance(payload, dict) or payload.get("client") != "claude-code" or payload.get("source") != "claude_code_hook":
        return None
    session_id = payload.get("client_session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > _MAX_CONTEXT_ID_LENGTH:
        return None
    observed_at = payload.get("observed_at")
    if not isinstance(observed_at, int | float) or isinstance(observed_at, bool):
        return None
    if now - float(observed_at) > max_age_seconds or float(observed_at) - now > 60:
        return None
    transcript_id = payload.get("client_transcript_id")
    if transcript_id is not None and (not isinstance(transcript_id, str) or not transcript_id or len(transcript_id) > _MAX_CONTEXT_ID_LENGTH):
        transcript_id = None
    project_label = payload.get("project_label")
    ancestors_raw = payload.get("hook_ancestor_pids")
    hook_ancestor_pids: list[int] = []
    if (
        isinstance(ancestors_raw, list)
        and len(ancestors_raw) <= _MAX_HOOK_ANCESTOR_PIDS
        and all(isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 for pid in ancestors_raw)
    ):
        # A malformed pid list is dropped without invalidating the context:
        # it only disables pid-lineage disambiguation for this candidate.
        hook_ancestor_pids = [int(pid) for pid in ancestors_raw]
    return {
        "client": "claude-code",
        "client_session_id": session_id,
        "client_transcript_id": transcript_id,
        "project_label": project_label if isinstance(project_label, str) and project_label else None,
        "observed_at": float(observed_at),
        "hook_ancestor_pids": hook_ancestor_pids,
    }


def load_claude_code_hook_context(
    store_dir: Path | str,
    *,
    now: float | None = None,
    max_age_seconds: float = CLAUDE_CODE_HOOK_CONTEXT_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    """Load the LEGACY single-slot context if present, valid, and fresh.

    Kept with its exact pre-multi-slot shape and semantics for back-compat;
    new consumers use select_claude_code_hook_context.
    """
    path = claude_code_hook_context_path(store_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    current = time.time() if now is None else float(now)
    normalized = _validate_hook_context_payload(payload, now=current, max_age_seconds=max_age_seconds)
    if normalized is None:
        return None
    normalized.pop("hook_ancestor_pids", None)
    return normalized


def load_claude_code_hook_contexts(
    store_dir: Path | str,
    *,
    now: float | None = None,
    max_age_seconds: float = CLAUDE_CODE_HOOK_CONTEXT_MAX_AGE_SECONDS,
) -> list[dict[str, Any]]:
    """Load every valid fresh hook context (per-session dir + legacy slot).

    Deduped by client_session_id keeping the larger observed_at; on an exact
    tie the LEGACY slot entry wins (in production dual-write both copies are
    identical, so the preference only pins behavior for hand-edited slots).
    Sorted newest-first. Each context carries context_path: the store-relative
    posix path of the file it came from.
    """
    root = Path(store_dir)
    current = time.time() if now is None else float(now)
    candidates: dict[str, dict[str, Any]] = {}

    def _consider(path: Path, *, prefer_on_tie: bool) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return
        normalized = _validate_hook_context_payload(payload, now=current, max_age_seconds=max_age_seconds)
        if normalized is None:
            return
        try:
            normalized["context_path"] = path.relative_to(root).as_posix()
        except ValueError:
            normalized["context_path"] = path.as_posix()
        existing = candidates.get(normalized["client_session_id"])
        if (
            existing is None
            or normalized["observed_at"] > existing["observed_at"]
            or (prefer_on_tie and normalized["observed_at"] == existing["observed_at"])
        ):
            candidates[normalized["client_session_id"]] = normalized

    context_dir = claude_code_hook_context_dir(root)
    try:
        entries = sorted(entry for entry in context_dir.iterdir() if entry.suffix == ".json" and entry.is_file())
    except OSError:
        entries = []
    for entry in entries:
        _consider(entry, prefer_on_tie=False)
    legacy_path = claude_code_hook_context_path(root)
    if legacy_path.is_file():
        _consider(legacy_path, prefer_on_tie=True)
    return sorted(candidates.values(), key=lambda context: context["observed_at"], reverse=True)


@dataclass(frozen=True)
class HookContextSelection:
    """Outcome of binding the MCP server to at most one hook context.

    status "selected": context is the proven-safe candidate. status "none":
    no fresh context exists (inherit nothing, no refusal). status "refused":
    multiple fresh contexts could not be disambiguated — inherit NOTHING and
    record the machine-readable reason (missing beats wrong).
    """

    context: dict[str, Any] | None
    status: Literal["selected", "none", "refused"]
    reason: str
    fresh_count: int


def select_claude_code_hook_context(
    store_dir: Path | str,
    *,
    now: float | None = None,
    max_age_seconds: float = CLAUDE_CODE_HOOK_CONTEXT_MAX_AGE_SECONDS,
    env_session_id: str | None = None,
    consumer_ancestor_pids: Sequence[int] | Callable[[], Sequence[int]] | None = None,
) -> HookContextSelection:
    """Select the hook context the consumer may safely inherit, if any.

    Selection succeeds only on (1) exactly one fresh candidate (the common
    single-session flow, identical to the pre-multi-slot behavior), (2) an
    exact CLAUDE_CODE_SESSION_ID env match that is also STRICTLY newest, or
    (3) a unique process-lineage match. Anything else refuses inheritance.
    """
    candidates = load_claude_code_hook_contexts(store_dir, now=now, max_age_seconds=max_age_seconds)
    if not candidates:
        return HookContextSelection(None, "none", "no_fresh_context", 0)
    if len(candidates) == 1:
        # No pid/env checks here: old-format files without pid info (e.g. a
        # live store written by a pre-upgrade hook) keep working unchanged.
        return HookContextSelection(candidates[0], "selected", "single_fresh", 1)
    if env_session_id:
        matches = [candidate for candidate in candidates if candidate["client_session_id"] == env_session_id]
        if len(matches) == 1:
            match = matches[0]
            # Strict recency: the env id was captured when the server spawned
            # and can be stale after /clear rotates the session id. The
            # current session's context was just refreshed by the PreToolUse
            # hook for this very MCP call, so a stale env candidate is never
            # strictly newest — any env anomaly degrades to refusal, never a
            # wrong pick.
            if all(match["observed_at"] > other["observed_at"] for other in candidates if other is not match):
                return HookContextSelection(match, "selected", "env_session_match", len(candidates))
    # Pid lineage applies only when EVERY candidate carries an ancestry
    # chain: a chain-less candidate cannot be ruled out, so selecting around
    # it could pick the wrong session (mixed-version/nested-session hole).
    # The consumer chain is resolved lazily (it may cost a ps call).
    chain: list[int] = []
    if all(candidate.get("hook_ancestor_pids") for candidate in candidates):
        chain_source = consumer_ancestor_pids() if callable(consumer_ancestor_pids) else consumer_ancestor_pids
        for pid in list(chain_source or [])[:_CONSUMER_ANCESTOR_MAX_DEPTH]:
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
                chain.append(pid)
    if chain:
        ranked: list[tuple[int, dict[str, Any]]] = []
        for candidate in candidates:
            ancestors = set(candidate["hook_ancestor_pids"])
            rank = next((index for index, pid in enumerate(chain) if pid in ancestors), None)
            if rank is not None:
                ranked.append((rank, candidate))
        if ranked:
            best_rank = min(rank for rank, _ in ranked)
            best = [candidate for rank, candidate in ranked if rank == best_rank]
            if len(best) == 1:
                return HookContextSelection(best[0], "selected", "pid_lineage_match", len(candidates))
    return HookContextSelection(None, "refused", "concurrent_contexts_ambiguous", len(candidates))


def claude_code_hook_context_status(
    store_dir: Path | str,
    *,
    now: float | None = None,
    max_age_seconds: float = CLAUDE_CODE_HOOK_CONTEXT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Summarize hook-context availability for doctor/onboarding output."""
    path = claude_code_hook_context_path(store_dir)
    current = time.time() if now is None else float(now)
    fresh_candidates = load_claude_code_hook_contexts(store_dir, now=current, max_age_seconds=max_age_seconds)
    if fresh_candidates:
        newest = fresh_candidates[0]
        return {
            "status": "fresh",
            "age_seconds": max(0.0, current - float(newest["observed_at"])),
            "client_session_id": newest["client_session_id"],
            "client_transcript_id": newest["client_transcript_id"],
            "fresh_count": len(fresh_candidates),
        }
    if not path.exists():
        return {"status": "absent", "age_seconds": None, "client_session_id": None, "client_transcript_id": None, "fresh_count": 0}
    stale = load_claude_code_hook_context(store_dir, now=current, max_age_seconds=float("inf"))
    if stale is not None:
        return {
            "status": "stale",
            "age_seconds": max(0.0, current - float(stale["observed_at"])),
            "client_session_id": stale["client_session_id"],
            "client_transcript_id": stale["client_transcript_id"],
            "fresh_count": 0,
        }
    return {"status": "invalid", "age_seconds": None, "client_session_id": None, "client_transcript_id": None, "fresh_count": 0}


# The wrapper FILENAME is frozen (`claude_pre_tool_use.py`): doctor and the
# settings-command matcher recognize an installed hook by this basename, so
# pre-rename and pre-relocation installs keep being recognized forever.
#
# The wrapper DIRECTORY moved OUT of the store dir (was `.agent-sentinel/hooks/`,
# now `.claude/hooks/`). Rationale (F3): the store directory is a movable thing —
# renaming/moving it is a normal admin action — and a wrapper that lives INSIDE
# the store vanishes when the store moves. Claude Code then runs `python <gone>`,
# which fails before the wrapper's own fail-open can run, and a `"*"` PreToolUse
# hook that fails closed blocks EVERY tool call in every running session. Homing
# the wrapper under `.claude/` (a sibling of the settings that reference it, never
# the store) keeps a store move from bricking sessions. Existing installs keep
# their old `.agent-sentinel/hooks/` path until re-run; doctor recognizes both.
CLAUDE_HOOK_RELATIVE_PATH = Path(".claude/hooks/claude_pre_tool_use.py")
# Pre-relocation installs (F3) kept the wrapper under the store dir. Doctor still
# finds and inspects it there so an un-migrated install stays diagnosable, and
# `install --force` migrates it to the new home. Same frozen basename, so the
# settings-command matcher recognizes both without a second name.
LEGACY_CLAUDE_HOOK_RELATIVE_PATH = Path(".agent-sentinel/hooks/claude_pre_tool_use.py")
CLAUDE_SETTINGS_RELATIVE_PATH = Path(".claude/settings.agent-sentinel.example.json")
# Active settings files doctor also inspects for agentacct hook commands. The
# example file is safe to rewrite; these are user-owned and only ever read.
CLAUDE_ACTIVE_SETTINGS_RELATIVE_PATHS = (
    Path(".claude/settings.json"),
    Path(".claude/settings.local.json"),
)

# agentacct is the published primary console command; the pre-rename binary
# names are kept forever so wrappers and doctor keep recognizing pre-rename
# installs (the package still ships those aliases for them).
_AGENT_CHRONICLE_EXECUTABLE_NAMES = (
    "agentacct",
    "agentacct.exe",
    "agent-chronicle",
    "agent-chronicle.exe",
    "agent-sentinel",
    "agent-sentinel.exe",
)

_CLAUDE_HOOK_WRAPPER_TEMPLATE = '''#!/usr/bin/env python3
"""Project-local Claude Code hook wrapper for agentacct.

One wrapper serves all three hook events (dispatch on the event's own
`hook_event_name`, so a single settings command line covers them):
- PreToolUse: writes a agentacct allow/checkpoint/block decision JSON and
  captures per-session client context (session/transcript ids) for joins.
- SessionStart: captures the same client context at session start and returns
  additionalContext directing the agent to record its work as sections.
- PostToolUse: observes the exit code of test/build/lint/typecheck commands as
  an independent machine check. Read-only — returns an empty response, never
  blocks or edits the tool result.

This wrapper intentionally shells out to the installed `agentacct` CLI so
projects can inspect or customize it before wiring it into their Claude Code
settings. It reads the Claude hook event JSON from stdin and writes the
event-appropriate response JSON to stdout.

Fail-open contract: if no agentacct executable can be started, or the CLI
exits without producing output, this wrapper prints an explicit no-op response
(an "allow" decision for PreToolUse, an empty object for SessionStart) and
exits 0 so a broken or moved agentacct install can never break (or silently
block) sessions or tool calls. Diagnose install problems with:
agentacct hooks claude-code doctor
"""

import json
import os
import shutil
import subprocess
import sys

# Tried in order. The absolute path is embedded at install time because Claude
# Code hook commands often run without the PATH of the shell that installed
# agentacct (e.g. virtualenv-only installs); the bare names are a fallback for
# projects that later move to a different machine or venv (the pre-rename
# "agent-sentinel" binary name is tried last, forever).
AGENT_CHRONICLE_CANDIDATES = __AGENT_CHRONICLE_CANDIDATES__

# Extra args appended to the `hooks claude-code <subcommand>` call. Global-store
# installs embed an explicit ["--store-dir", "<absolute path>"] here because
# GUI-launched Claude Code sessions do not inherit shell environment
# variables, so env-based store selection cannot be relied on.
AGENT_CHRONICLE_HOOK_ARGS = __AGENT_CHRONICLE_HOOK_ARGS__

# The parsed hook event name, hoisted to module scope so the last-resort
# handler under __main__ can fail open with the EVENT-APPROPRIATE response
# shape ({} for SessionStart, an allow decision otherwise).
HOOK_EVENT = ""


def resolve_agentacct():
    for candidate in AGENT_CHRONICLE_CANDIDATES:
        if not candidate:
            continue
        if os.path.basename(candidate) != candidate:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def fail_open(reason, hook_event=""):
    sys.stderr.write("agentacct hook: %s; failing open\\n" % reason)
    if hook_event in ("SessionStart", "PostToolUse", "SessionEnd"):
        # SessionStart / PostToolUse / SessionEnd no-op: an empty object.
        # PostToolUse in particular must never block or edit the tool result on
        # failure; SessionEnd output is ignored by Claude Code either way.
        sys.stdout.write("{}\\n")
    else:
        # "agent_sentinel" is a frozen wire key (pre-rename): consumers parse it.
        decision = {"agent_sentinel": {"decision": "allow", "risk": "low", "reason": "%s; failing open" % reason}}
        sys.stdout.write(json.dumps(decision) + "\\n")
    return 0


def main():
    global HOOK_EVENT
    # Bytes + errors="replace": undecodable stdin degrades to a JSON parse
    # failure INSIDE main(), never a UnicodeDecodeError that escapes to the
    # last-resort handler before the hook event is known.
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        event = json.loads(raw)
        if isinstance(event, dict):
            value = event.get("hook_event_name")
            if isinstance(value, str):
                HOOK_EVENT = value
    except ValueError:
        pass
    subcommand = {
        "SessionStart": "session-start",
        "PostToolUse": "post-tool-use",
        "SessionEnd": "session-end",
    }.get(HOOK_EVENT, "pre-tool-use")
    executable = resolve_agentacct()
    if executable is None:
        return fail_open("agentacct executable not found (re-run: agentacct hooks claude-code install --force)", HOOK_EVENT)
    # Equivalent shell command:
    # agentacct hooks claude-code <pre-tool-use|post-tool-use|session-start> [extra args]
    try:
        proc = subprocess.run(
            [executable, "hooks", "claude-code", subcommand, *AGENT_CHRONICLE_HOOK_ARGS],
            input=raw,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        return fail_open("agentacct failed to start (%s)" % exc, HOOK_EVENT)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode == 0 and proc.stdout.strip():
        sys.stdout.write(proc.stdout)
        return 0
    return fail_open("agentacct exited with code %s without output" % proc.returncode, HOOK_EVENT)


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:  # fail-open: a wrapper bug must never break sessions.
        exit_code = fail_open("unexpected hook wrapper error (%s: %s)" % (type(exc).__name__, exc), HOOK_EVENT)
    raise SystemExit(exit_code)
'''


def resolve_agentacct_executable() -> str | None:
    """Absolute path of the agentacct CLI belonging to this interpreter.

    Prefers the console script next to sys.executable (the venv that is
    actually running the install) over a PATH lookup, so the embedded path
    keeps working in hook contexts that never see that venv's PATH.
    """
    if sys.executable:
        # No resolve(): a venv python is a symlink, and following it would
        # escape the venv bin dir where the console script actually lives.
        bin_dir = Path(sys.executable).parent
        for name in _AGENT_CHRONICLE_EXECUTABLE_NAMES:
            candidate = bin_dir / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return shutil.which("agentacct") or shutil.which("agent-chronicle") or shutil.which("agent-sentinel")


def render_claude_hook_wrapper(agentacct_executable: str | None = None, *, store_dir: Path | str | None = None) -> str:
    candidates: list[str] = []
    if agentacct_executable:
        candidates.append(str(agentacct_executable))
    # Bare-name fallbacks: new binary name first, pre-rename aliases after.
    for bare_name in ("agentacct", "agent-chronicle", "agent-sentinel"):
        if bare_name not in candidates:
            candidates.append(bare_name)
    hook_args: list[str] = []
    if store_dir is not None:
        # Explicit store binding for global-store installs: hook processes of
        # GUI-launched clients see neither shell env vars nor a predictable
        # cwd, so the store must ride the command line.
        hook_args = ["--store-dir", str(store_dir)]
    rendered = _CLAUDE_HOOK_WRAPPER_TEMPLATE.replace("__AGENT_CHRONICLE_CANDIDATES__", repr(candidates))
    return rendered.replace("__AGENT_CHRONICLE_HOOK_ARGS__", repr(hook_args))


# PATH-lookup-only rendering, kept for callers that used the old constant.
CLAUDE_HOOK_WRAPPER = render_claude_hook_wrapper()


# Codex reads a user-level ``~/.codex/hooks.json``; its wrapper lives beside it
# under ``~/.codex/hooks/`` (NOT the store), so a store move can never vanish the
# wrapper and brick Codex tool calls — the same lesson as the Claude Code path.
CODEX_HOOK_RELATIVE_PATH = Path(".codex/hooks/agentacct_codex_hook.py")
CODEX_HOOKS_JSON_RELATIVE_PATH = Path(".codex/hooks.json")

_CODEX_HOOK_WRAPPER_TEMPLATE = '''#!/usr/bin/env python3
"""Codex hook wrapper for agentacct (observe-only).

One wrapper serves the installed Codex hook events (dispatch on the event's own
`hook_event_name`):
- PreToolUse: spool the tool NAME + category for the Receipt's Actions dimension
  (observe-only — agentacct never makes an allow/block decision for Codex; Codex
  owns its own sandbox/approval layer).
- SessionEnd: spool a content-free session-end fact for the ended_open inference.

Shells out to the installed `agentacct` CLI. Fail-open: if no agentacct
executable can be started, or the CLI exits without output, this wrapper prints
an empty no-op response `{}` and exits 0, so a broken or moved install can never
block a Codex tool call. Reinstall with: agentacct hooks codex install --force
"""

import json
import os
import shutil
import subprocess
import sys

AGENTACCT_CANDIDATES = __AGENTACCT_CANDIDATES__
AGENTACCT_HOOK_ARGS = __AGENTACCT_HOOK_ARGS__


def resolve_agentacct():
    for candidate in AGENTACCT_CANDIDATES:
        if not candidate:
            continue
        if os.path.basename(candidate) != candidate:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def no_op(reason=""):
    if reason:
        sys.stderr.write("agentacct codex hook: %s; failing open\\n" % reason)
    sys.stdout.write("{}\\n")
    return 0


def main():
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    hook_event = ""
    try:
        event = json.loads(raw)
        if isinstance(event, dict):
            value = event.get("hook_event_name")
            if isinstance(value, str):
                hook_event = value
    except ValueError:
        pass
    subcommand = "session-end" if hook_event == "SessionEnd" else "pre-tool-use"
    executable = resolve_agentacct()
    if executable is None:
        return no_op("agentacct executable not found (re-run: agentacct hooks codex install --force)")
    try:
        proc = subprocess.run(
            [executable, "hooks", "codex", subcommand, *AGENTACCT_HOOK_ARGS],
            input=raw,
            text=True,
            capture_output=True,
            # A bounded wait is part of the never-block contract: fail-open covers
            # the CLI RETURNING an error, but a hung child (stalled store mount,
            # held SQLite lock, cold-import stall) would otherwise block the Codex
            # tool call forever. A timeout turns that into a no-op {} instead.
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return no_op("agentacct timed out")
    except OSError as exc:
        return no_op("agentacct failed to start (%s)" % exc)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode == 0 and proc.stdout.strip():
        sys.stdout.write(proc.stdout)
        return 0
    return no_op("agentacct exited with code %s without output" % proc.returncode)


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:  # fail-open: a wrapper bug must never break Codex sessions.
        exit_code = no_op("unexpected codex hook wrapper error (%s: %s)" % (type(exc).__name__, exc))
    raise SystemExit(exit_code)
'''


def render_codex_hook_wrapper(agentacct_executable: str | None = None, *, store_dir: Path | str | None = None) -> str:
    candidates: list[str] = []
    if agentacct_executable:
        candidates.append(str(agentacct_executable))
    for bare_name in ("agentacct", "agent-chronicle", "agent-sentinel"):
        if bare_name not in candidates:
            candidates.append(bare_name)
    hook_args: list[str] = []
    if store_dir is not None:
        # GUI-launched Codex (ChatGPT.app) sees neither shell env vars nor a
        # predictable cwd, so the store binds on the command line.
        hook_args = ["--store-dir", str(store_dir)]
    rendered = _CODEX_HOOK_WRAPPER_TEMPLATE.replace("__AGENTACCT_CANDIDATES__", repr(candidates))
    return rendered.replace("__AGENTACCT_HOOK_ARGS__", repr(hook_args))


def codex_hooks_json_block(wrapper_path: Path | str, *, python_executable: str | None = None) -> dict[str, Any]:
    """The ``~/.codex/hooks.json`` content wiring PreToolUse + SessionEnd to the
    agentacct Codex wrapper. Codex uses the SAME hooks.json schema as Claude Code
    (``{"hooks": {"<Event>": [{"matcher"?, "hooks": [{"type": "command", ...}]}]}}``),
    so this mirrors the Claude settings block shape. One wrapper serves both
    events (it dispatches on ``hook_event_name``)."""
    python_command = python_executable or sys.executable or "python3"
    command = f"{shlex.quote(python_command)} {shlex.quote(str(wrapper_path))}"
    return {
        "hooks": {
            "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": command}]}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": command}]}],
        }
    }


# ---------------------------------------------------------------------------
# Hermes shell-hook adapter (observe-only)
# ---------------------------------------------------------------------------
#
# Hermes (Nous Research personal agent) dispatches shell hooks from the
# ``hooks:`` block of ``~/.hermes/config.yaml`` for both its CLI/TUI and its
# gateway. Its wire STDIN is a single serialization point
# (``agent/shell_hooks.py:_serialize_payload``) whose top-level keys
# ``hook_event_name`` / ``tool_name`` / ``session_id`` are the SAME snake_case as
# Claude Code + Codex, and its hook ``session_id`` EQUALS the ``state.db``
# sessions.id the usage importer keys on — so hermes ticks attribute straight to
# the session with no log pairing (verified on the real machine 2026-08-13).
#
# Three events are wired, all observe-only (agentacct never blocks a hermes tool
# call — hermes owns its own approval layer):
#   pre_tool_call  -> tool NAME + category (Receipt Actions dimension)          [#1]
#   post_tool_call -> the harness exit code of a recognized check command       [#3]
#   on_session_end -> a TURN-boundary heartbeat (see below)                     [#4]
#
# NOTE on #4: hermes fires ``on_session_end`` at the end of EVERY turn
# (``run_conversation`` runs once per user message), NOT at real session close.
# So it is captured as a distinct ``turn_boundary_observed`` liveness heartbeat,
# never fed into the ``ended_open`` inference (a per-turn end would falsely close
# a still-live session between turns). See session_lifecycle.record_turn_boundary_tick.

HERMES_HOOK_RELATIVE_PATH = Path(".hermes/hooks/agentacct_hermes_hook.py")
HERMES_CONFIG_RELATIVE_PATH = Path(".hermes/config.yaml")

# The hermes shell-hook event names this adapter wires, mapped to the agentacct
# CLI subcommand each dispatches to. The wrapper routes on ``hook_event_name``.
HERMES_HOOK_EVENT_SUBCOMMANDS: dict[str, str] = {
    "pre_tool_call": "pre-tool-call",
    "post_tool_call": "post-tool-call",
    "on_session_end": "session-end",
}

# hermes's shell/terminal tool name (its exit-code-bearing command tool). The #3
# exit-code check only observes this tool, mirroring the Claude Code path gating
# on ``bash``.
_HERMES_SHELL_TOOL_NAMES = frozenset({"terminal"})

_HERMES_HOOK_WRAPPER_TEMPLATE = '''#!/usr/bin/env python3
"""Hermes shell-hook wrapper for agentacct (observe-only).

One wrapper serves the installed Hermes hook events (dispatch on the event's own
`hook_event_name`):
- pre_tool_call:  spool the tool NAME + category for the Receipt's Actions dimension.
- post_tool_call: spool the harness exit code of a recognized check command.
- on_session_end: spool a content-free turn-boundary heartbeat.

All observe-only — agentacct never makes an allow/block decision for Hermes
(Hermes owns its own approval layer). Shells out to the installed `agentacct`
CLI. Fail-open: on any error, a hung child, or an unknown event, this wrapper
writes an empty JSON object `{}` (Hermes reads that as "hook contributed nothing")
and exits 0, so a broken or moved install can never block a Hermes tool call.
Reinstall with: agentacct hooks hermes install --force
"""

import json
import os
import shutil
import subprocess
import sys

AGENTACCT_CANDIDATES = __AGENTACCT_CANDIDATES__
AGENTACCT_HOOK_ARGS = __AGENTACCT_HOOK_ARGS__
EVENT_SUBCOMMANDS = __EVENT_SUBCOMMANDS__


def resolve_agentacct():
    for candidate in AGENTACCT_CANDIDATES:
        if not candidate:
            continue
        if os.path.basename(candidate) != candidate:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def no_op(reason=""):
    if reason:
        sys.stderr.write("agentacct hermes hook: %s; failing open\\n" % reason)
    # Hermes observe-only contract: an empty JSON object contributes nothing to
    # the dispatcher (never a block, never injected context).
    sys.stdout.write("{}\\n")
    return 0


def main():
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    hook_event = ""
    try:
        event = json.loads(raw)
        if isinstance(event, dict):
            value = event.get("hook_event_name")
            if isinstance(value, str):
                hook_event = value
    except ValueError:
        pass
    subcommand = EVENT_SUBCOMMANDS.get(hook_event)
    if subcommand is None:
        return no_op()  # not one of ours (or unparseable): silent no-op
    executable = resolve_agentacct()
    if executable is None:
        return no_op("agentacct executable not found (re-run: agentacct hooks hermes install --force)")
    try:
        proc = subprocess.run(
            [executable, "hooks", "hermes", subcommand, *AGENTACCT_HOOK_ARGS],
            input=raw,
            text=True,
            capture_output=True,
            # A bounded wait is part of the never-block contract: a hung child
            # (stalled store mount, held SQLite lock, cold-import stall) would
            # otherwise stall the Hermes tool call up to hermes's own hook
            # timeout. 5s < the installed per-hook timeout, so we fail-open first.
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return no_op("agentacct timed out")
    except OSError as exc:
        return no_op("agentacct failed to start (%s)" % exc)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode == 0 and proc.stdout.strip():
        sys.stdout.write(proc.stdout)
        return 0
    return no_op("agentacct exited with code %s without output" % proc.returncode)


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:  # fail-open: a wrapper bug must never break Hermes sessions.
        exit_code = no_op("unexpected hermes hook wrapper error (%s: %s)" % (type(exc).__name__, exc))
    raise SystemExit(exit_code)
'''


def render_hermes_hook_wrapper(agentacct_executable: str | None = None, *, store_dir: Path | str | None = None) -> str:
    candidates: list[str] = []
    if agentacct_executable:
        candidates.append(str(agentacct_executable))
    for bare_name in ("agentacct", "agent-chronicle", "agent-sentinel"):
        if bare_name not in candidates:
            candidates.append(bare_name)
    hook_args: list[str] = []
    if store_dir is not None:
        # Hermes hooks fire from the CLI/TUI and the gateway, neither of which
        # guarantees agentacct's env vars or cwd, so the store binds on the
        # command line (mirrors the Codex wrapper).
        hook_args = ["--store-dir", str(store_dir)]
    rendered = _HERMES_HOOK_WRAPPER_TEMPLATE.replace("__AGENTACCT_CANDIDATES__", repr(candidates))
    rendered = rendered.replace("__AGENTACCT_HOOK_ARGS__", repr(hook_args))
    return rendered.replace("__EVENT_SUBCOMMANDS__", repr(dict(HERMES_HOOK_EVENT_SUBCOMMANDS)))


def _hermes_tool_exit_code(event: dict[str, Any]) -> int | None:
    """Read the terminal exit code from a hermes ``post_tool_call`` payload.

    Hermes nests the result under ``extra.result`` — a dict
    ``{"output", "exit_code", "error"}`` for the terminal tool, or a JSON string
    for some tools. 0 is a valid value, so a missing/unusable code returns None
    (kept distinct from success) so the check is only recorded when real."""

    extra = event.get("extra")
    result = extra.get("result") if isinstance(extra, dict) else None
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return None
    if not isinstance(result, dict):
        return None
    value = result.get("exit_code")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def capture_hermes_tool_check(raw: str, *, store_dir: Path | str | None = None) -> None:
    """Record ONE mechanical-check tick for a hermes ``post_tool_call``. Never raises.

    Mirrors ``capture_mechanical_check`` for hermes's wire shape: the check runs
    only for the terminal tool, the command comes from top-level ``tool_input``,
    and the harness exit code from ``extra.result`` (see ``_hermes_tool_exit_code``).
    When the command is a recognized test/build/lint runner, the exit code the
    HARNESS observed is spooled (client ``hermes``) — the local signal that lifts a
    step to ``independently_checked``. Only a coarse check kind, the runner name,
    and a sha256 command DIGEST are spooled — never the command, arguments, or output.
    """

    try:
        from .mechanical_capture import (
            classify_command,
            command_digest,
            record_mechanical_check_tick,
        )

        event = json.loads(raw or "{}")
        if not isinstance(event, dict):
            return
        tool_name = event.get("tool_name") or event.get("tool")
        if str(tool_name or "").strip() not in _HERMES_SHELL_TOOL_NAMES:
            return
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > _MAX_CONTEXT_ID_LENGTH:
            return
        tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
        command = tool_input.get("command")
        classified = classify_command(command)
        if classified is None:
            return
        exit_code = _hermes_tool_exit_code(event)
        if exit_code is None:
            return
        target = Path(store_dir) if store_dir is not None else _hook_store_dir_from_event(event)
        if target is None:
            return
        check_kind, runner = classified
        record_mechanical_check_tick(
            target,
            client="hermes",
            session_id=session_id,
            check_kind=check_kind,
            runner=runner,
            digest=command_digest(str(command or "")),
            exit_code=exit_code,
            at=time.time(),
        )
    except Exception:  # noqa: BLE001 - capture must never affect the hook.
        return


def capture_hermes_turn_boundary(raw: str, *, store_dir: Path | str | None = None) -> None:
    """Record ONE turn-boundary heartbeat for a hermes ``on_session_end``. Never raises.

    Hermes fires ``on_session_end`` at the end of EVERY turn, not at session
    close, so this is spooled as a content-free ``turn_boundary_observed``
    liveness fact — client, session id, timestamp, and the terminal ``completed`` /
    ``interrupted`` flags — and is NEVER consumed as a session close (the reducer
    would otherwise falsely infer ``ended_open`` between turns). No conversation
    content. Latest-per-session wins on drain.
    """

    try:
        from .session_lifecycle import record_turn_boundary_tick

        event = json.loads(raw or "{}")
        if not isinstance(event, dict):
            return
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > _MAX_CONTEXT_ID_LENGTH:
            return
        target = Path(store_dir) if store_dir is not None else _hook_store_dir_from_event(event)
        if target is None:
            return
        extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
        completed = extra.get("completed")
        interrupted = extra.get("interrupted")
        record_turn_boundary_tick(
            target,
            client="hermes",
            session_id=session_id,
            at=time.time(),
            completed=completed if isinstance(completed, bool) else None,
            interrupted=interrupted if isinstance(interrupted, bool) else None,
        )
    except Exception:  # noqa: BLE001 - capture must never affect the hook.
        return


def claude_code_settings_example(python_executable: str | None = None, *, hook_path: Path | str | None = None) -> dict[str, Any]:
    """Example Claude Code settings block invoking the agentacct hook wrapper.

    Default (project install): the command references the wrapper via
    ``$CLAUDE_PROJECT_DIR`` so the block works from the project's own
    ``.claude/settings(.local).json``. With ``hook_path`` (user-level install,
    e.g. ``~/.claude/settings.json``): the command embeds that ABSOLUTE
    wrapper path — user-level hooks run for every project, so
    ``$CLAUDE_PROJECT_DIR`` would point at whichever project the session is
    in, not at the wrapper's home.
    """
    python_command = python_executable or sys.executable or "python3"
    if hook_path is not None:
        command = f"{shlex.quote(python_command)} {shlex.quote(str(hook_path))}"
    else:
        command = f'{shlex.quote(python_command)} "$CLAUDE_PROJECT_DIR/{CLAUDE_HOOK_RELATIVE_PATH.as_posix()}"'
    # ONE wrapper serves both events (it dispatches on hook_event_name), so both
    # entries share the same command. SessionStart has no matcher on purpose:
    # it fires for every source (startup/resume/clear/compact), so the
    # record-your-work context survives resumes and compactions too. Clients
    # too old to know SessionStart ignore the extra key harmlessly.
    # The top-level "env" block is the third lever of the proven recipe:
    # ENABLE_TOOL_SEARCH=auto makes the agentacct MCP tools arrive directly
    # callable instead of deferred — without it, un-primed sessions
    # demonstrably record nothing even with the directive delivered. The
    # merge-never-clobber rule applies to "env" exactly as to "hooks": whoever
    # merges this example must preserve the env keys the user already has.
    # The statusLine is a lightweight, always-fast command (imports only
    # agentacct.rate_limits) that captures Claude's rate-limit reading for
    # terminal-CLI users — where the desktop plan-usage file does not exist — and
    # prints a compact status bar. It runs as a direct command (NOT the hook
    # wrapper, which dispatches on hook_event_name), so it is `python -m` and the
    # same string works for project and user-level installs.
    statusline_command = f"{shlex.quote(python_command)} -m agentacct.statusline_hook"
    return {
        "env": {"ENABLE_TOOL_SEARCH": "auto"},
        "statusLine": {
            "type": "command",
            "command": statusline_command,
            "padding": 0,
        },
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ],
                }
            ],
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ],
                }
            ],
            # PostToolUse observes the exit code of test/build/lint/typecheck
            # commands (source client_hook) — the only local INDEPENDENT check,
            # what lifts a step to independently_checked. Read-only: the wrapper
            # returns an empty response and never blocks or edits a tool result.
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ],
                }
            ],
            # SessionEnd fires once when a root session terminates. The wrapper
            # spools a content-free session-end fact (client, session id, reason,
            # time); the reducer later infers an ``ended_open`` disposition for a
            # step whose session stopped without a recorded terminal. Fire-and-
            # forget: Claude Code ignores the output, so it never blocks anything.
            "SessionEnd": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ],
                }
            ],
        }
    }


def claude_code_hook_paths(project_dir: Path | str) -> tuple[Path, Path]:
    root = Path(project_dir)
    return root / CLAUDE_HOOK_RELATIVE_PATH, root / CLAUDE_SETTINGS_RELATIVE_PATH


@dataclass(frozen=True)
class ClaudeCodeHookInstall:
    hook_path: Path
    settings_path: Path
    agentacct_executable: str | None
    python_command: str
    store_dir: Path | None = None
    user_settings_example: bool = False

    @property
    def settings_example(self) -> dict[str, Any]:
        return claude_code_settings_example(
            self.python_command, hook_path=self.hook_path if self.user_settings_example else None
        )


def install_claude_code_hook(
    project_dir: Path | str,
    *,
    dry_run: bool = False,
    force: bool = False,
    agentacct_executable: str | None = None,
    python_executable: str | None = None,
    store_dir: Path | str | None = None,
    user_settings_example: bool = False,
) -> ClaudeCodeHookInstall:
    """Write the hook wrapper + example settings under ``project_dir``.

    ``store_dir`` (absolute) embeds an explicit ``--store-dir`` into the
    wrapper's hook command — required for global-store installs where the
    hook must not depend on env vars or cwd walk-ups. ``user_settings_example``
    renders the example settings with the wrapper's ABSOLUTE path (ready to
    merge into user-level ``~/.claude/settings.json``) instead of
    ``$CLAUDE_PROJECT_DIR``. Never writes any user-owned file itself.
    """
    if user_settings_example:
        # A user-level settings block runs from arbitrary cwds: the wrapper
        # path it embeds must be absolute, whatever form project_dir took.
        project_dir = Path(project_dir).expanduser().resolve()
    hook_path, settings_path = claude_code_hook_paths(project_dir)
    resolved_executable = agentacct_executable or resolve_agentacct_executable()
    resolved_python = python_executable or sys.executable or "python3"
    resolved_store_dir = Path(store_dir).expanduser() if store_dir is not None else None
    if resolved_store_dir is not None and not resolved_store_dir.is_absolute():
        raise ValueError(
            "hook --store-dir must be an absolute path: hook processes run without your shell cwd/env, "
            "so a relative store path would silently split the ledger"
        )
    result = ClaudeCodeHookInstall(
        hook_path,
        settings_path,
        resolved_executable,
        resolved_python,
        store_dir=resolved_store_dir,
        user_settings_example=user_settings_example,
    )
    existing = [path for path in (hook_path, settings_path) if path.exists()]
    if existing and not force:
        raise FileExistsError("already exists: " + ", ".join(str(path) for path in existing))
    if dry_run:
        return result
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    # Explicit UTF-8: Python parses hook source as UTF-8 regardless of locale,
    # so a locale-encoded write of a non-ASCII embedded path would produce a
    # wrapper that SyntaxErrors before its fail-open logic can run.
    hook_path.write_text(render_claude_hook_wrapper(resolved_executable, store_dir=resolved_store_dir), encoding="utf-8")
    hook_path.chmod(0o755)
    settings_path.write_text(json.dumps(result.settings_example, indent=2) + "\n", encoding="utf-8")
    return result


# --- Hook doctor: detect hook commands that cannot resolve at hook time ------
#
# Dogfooding finding (2026-07-04): Claude Code desktop sessions run hook
# commands without the shell profile PATH, so a venv-only `agent-chronicle` or a
# bare `python` errors on every tool call. Doctor flags anything that depends
# on PATH lookup or points at a path that no longer exists.

_INSTALL_REMEDIATION = "re-run: agentacct hooks claude-code install --force (from the environment where agentacct is installed)"


def _is_bare_name(token: str) -> bool:
    return os.path.basename(token) == token


def _expand_project_dir(token: str, project_dir: Path) -> str:
    root = str(project_dir)
    return token.replace("${CLAUDE_PROJECT_DIR}", root).replace("$CLAUDE_PROJECT_DIR", root)


def wrapper_executable_candidates(wrapper_text: str) -> list[str]:
    """Extract agentacct executable references from a wrapper's source.

    Statically parses string constants so both the installed template (the
    AGENT_CHRONICLE_CANDIDATES list — or a pre-rename wrapper's
    AGENT_SENTINEL_CANDIDATES list, whose binary names are still in the
    recognized tuple) and hand-rolled wrappers that pass a literal to
    subprocess are covered, without executing project-local code.
    """
    try:
        tree = ast.parse(wrapper_text)
    except SyntaxError:
        return []
    candidates: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            base = os.path.basename(value.replace("\\", "/").rstrip("/"))
            if base in _AGENT_CHRONICLE_EXECUTABLE_NAMES and value not in candidates:
                candidates.append(value)
    return candidates


def _first_on_path(names: list[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def diagnose_wrapper_executables(wrapper_text: str, *, project_dir: Path | str | None = None) -> tuple[str, str]:
    """Judge whether the hook wrapper can start agentacct at hook time."""
    base = Path(project_dir) if project_dir is not None else Path(".")
    candidates = wrapper_executable_candidates(wrapper_text)
    if not candidates:
        return "warn", "no agentacct executable reference found (custom wrapper? verify it can start agentacct without shell PATH)"
    path_like = [c for c in candidates if not _is_bare_name(c)]
    for candidate in path_like:
        candidate_path = Path(candidate)
        if not candidate_path.is_absolute():
            candidate_path = base / candidate_path
        if candidate_path.is_file() and os.access(candidate_path, os.X_OK):
            return "ok", f"embedded path resolves: {candidate_path}"
    bare = [c for c in candidates if _is_bare_name(c)]
    resolved_bare = _first_on_path(bare)
    if path_like:
        detail = f"embedded path missing: {path_like[0]}"
        if resolved_bare:
            detail += f"; PATH fallback works in this shell ({resolved_bare}) but Claude Code hooks may not share this PATH"
        return "warn", f"{detail} — {_INSTALL_REMEDIATION}"
    if resolved_bare:
        return "warn", f"relies on PATH lookup ({resolved_bare} in this shell), which Claude Code hooks may not share — {_INSTALL_REMEDIATION}"
    return "warn", f"agentacct not resolvable (no embedded absolute path, not on PATH); the hook cannot start agentacct at hook time — {_INSTALL_REMEDIATION}"


def diagnose_hook_command(command: str, project_dir: Path | str) -> tuple[str, str]:
    """Judge whether a settings hook command can start without shell PATH."""
    root = Path(project_dir)
    if re.match(r"^[A-Za-z]:[\\/]", command.strip()) or command.strip().startswith("\\\\"):
        # POSIX shlex would mangle backslash paths into a misleading verdict.
        return "warn", "Windows-style command; doctor assumes a POSIX shell and cannot verify hook-time resolution here"
    if re.search(r"'[^']*\$\{?CLAUDE_PROJECT_DIR", command):
        return "warn", "single quotes prevent $CLAUDE_PROJECT_DIR expansion at hook time — quote the wrapper path with double quotes"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        return "warn", f"unparseable command ({exc})"
    if not tokens:
        return "warn", "empty command"
    executable = _expand_project_dir(tokens[0], root)
    notes: list[str] = []
    if _is_bare_name(executable):
        found = shutil.which(executable)
        if found is None:
            return "warn", f"'{executable}' not found on PATH — {_INSTALL_REMEDIATION}"
        status = "warn"
        notes.append(f"'{executable}' resolves in this shell ({found}) but Claude Code hooks may not share this PATH — {_INSTALL_REMEDIATION}")
    else:
        exe_path = Path(executable)
        if not exe_path.is_absolute():
            exe_path = root / exe_path
        if not (exe_path.is_file() and os.access(exe_path, os.X_OK)):
            return "warn", f"executable not found: {exe_path} — {_INSTALL_REMEDIATION}"
        status = "ok"
        notes.append(f"executes {exe_path}")
    for token in tokens[1:]:
        if token.endswith(CLAUDE_HOOK_RELATIVE_PATH.name):
            script = Path(_expand_project_dir(token, root))
            if not script.is_absolute():
                script = root / script
            if not script.is_file():
                return "warn", f"hook wrapper script missing: {script} — {_INSTALL_REMEDIATION}"
            notes.append(f"wrapper script present: {script}")
    return status, "; ".join(notes)


def _iter_hook_commands(settings: Any) -> Iterator[str]:
    if not isinstance(settings, dict):
        return
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event_entries in hooks.values():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            if not isinstance(entry, dict):
                continue
            entry_hooks = entry.get("hooks")
            if not isinstance(entry_hooks, list):
                continue
            for hook in entry_hooks:
                if isinstance(hook, dict) and hook.get("type") == "command":
                    command = hook.get("command")
                    if isinstance(command, str) and command.strip():
                        yield command


def _enable_tool_search_loads_tools_upfront(value: object) -> bool:
    """True iff an ENABLE_TOOL_SEARCH value keeps tools loaded upfront (good).

    Claude Code's ENABLE_TOOL_SEARCH toggles tool-search DEFERRAL:
    ``false`` disables deferral (all tools upfront), ``auto`` / ``auto:N`` defer
    only above a tool-count threshold (upfront for a normal install), and
    ``true`` forces deferral (agentacct tools listed-but-not-callable). Only the
    documented enabling forms count; every other value warns so the diagnostic
    never reads a false green on the recorded-work recipe's core lever.
    """

    text = str(value).strip().lower()
    if text in {"false", "auto"}:
        return True
    if text.startswith("auto:") and text[len("auto:") :].isdigit():
        return True
    return False


def claude_code_hook_doctor_checks(project_dir: Path | str) -> list[dict[str, str]]:
    """Doctor rows for the project-local hook install: (status, name, details)."""
    root = Path(project_dir)
    hook_path, settings_path = claude_code_hook_paths(root)
    # Un-migrated install: the wrapper still lives at the pre-relocation store
    # path. Inspect it there so doctor keeps diagnosing it (the settings-command
    # matcher already recognizes both by the frozen basename).
    if not hook_path.exists():
        legacy_hook_path = root / LEGACY_CLAUDE_HOOK_RELATIVE_PATH
        if legacy_hook_path.exists():
            hook_path = legacy_hook_path
    checks: list[dict[str, str]] = [
        {"status": "ok" if hook_path.exists() else "warn", "name": "hook wrapper", "details": str(hook_path)},
        {"status": "ok" if settings_path.exists() else "warn", "name": "example settings", "details": str(settings_path)},
    ]
    if hook_path.exists():
        wrapper_text: str | None
        try:
            wrapper_text = hook_path.read_text(encoding="utf-8")
            status, details = diagnose_wrapper_executables(wrapper_text, project_dir=root)
        except (OSError, UnicodeDecodeError) as exc:
            wrapper_text = None
            status, details = "warn", f"unreadable wrapper: {exc}"
        checks.append({"status": status, "name": "wrapper executable", "details": details})
        if wrapper_text is not None:
            # Version skew: a pre-SessionStart wrapper wired to the new
            # settings entry answers session starts with a PreToolUse
            # allow-decision. The dispatch marker is the '"session-start"'
            # subcommand literal every current template embeds.
            if '"session-start"' in wrapper_text:
                skew_status, skew_details = "ok", "wrapper dispatches SessionStart events"
            else:
                skew_status, skew_details = (
                    "warn",
                    "hook wrapper predates SessionStart support — re-run: agentacct hooks claude-code install --force",
                )
            checks.append({"status": skew_status, "name": "wrapper SessionStart support", "details": skew_details})
    settings_files = [settings_path, *(root / relative for relative in CLAUDE_ACTIVE_SETTINGS_RELATIVE_PATHS)]
    active_settings_payloads: list[tuple[str, Any]] = []
    for settings_file in settings_files:
        if not settings_file.is_file():
            continue
        name = f"hook command ({settings_file.name})"
        try:
            payload = json.loads(settings_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            checks.append({"status": "warn", "name": name, "details": f"unreadable settings: {exc}"})
            continue
        if settings_file != settings_path:
            active_settings_payloads.append((settings_file.name, payload))
        for command in _iter_hook_commands(payload):
            # Recognize commands referencing either binary name: pre-rename
            # installed settings say "agent-sentinel" forever.
            if (
                "agent-chronicle" not in command
                and "agent-sentinel" not in command
                and CLAUDE_HOOK_RELATIVE_PATH.name not in command
            ):
                continue
            status, details = diagnose_hook_command(command, root)
            checks.append({"status": status, "name": name, "details": details or command})
    # ENABLE_TOOL_SEARCH is the third lever of the recorded-work recipe: without
    # it in a settings `env` block the agentacct MCP tools load deferred and
    # un-primed sessions record nothing. Only ACTIVE settings files count (the
    # example always carries the key), and with no active file there is nothing
    # to judge — the settings merge may not have happened yet, or may live in
    # user-level ~/.claude/settings.json, which this project-dir-scoped doctor
    # deliberately never reads.
    if active_settings_payloads:
        set_values = {
            file_name: payload["env"]["ENABLE_TOOL_SEARCH"]
            for file_name, payload in active_settings_payloads
            if isinstance(payload, dict)
            and isinstance(payload.get("env"), dict)
            and "ENABLE_TOOL_SEARCH" in payload["env"]
        }
        if set_values:
            details = "; ".join(f"{file_name}: {value}" for file_name, value in set_values.items())
            non_enabling = {
                file_name: value
                for file_name, value in set_values.items()
                if not _enable_tool_search_loads_tools_upfront(value)
            }
            if non_enabling:
                # A PRESENT key is not enough: only values that keep tools loaded
                # upfront (`auto`, `auto:N`, or `false` — which disables
                # tool-search deferral) count. `true` (and any other value)
                # forces deferral, so the agentacct MCP tools stay listed-but-not-
                # callable and un-primed sessions record nothing — the exact
                # failure this row exists to catch, so it must NOT read ok.
                offending = "; ".join(f"{file_name}: {value}" for file_name, value in non_enabling.items())
                checks.append(
                    {
                        "status": "warn",
                        "name": "env.ENABLE_TOOL_SEARCH",
                        "details": (
                            f"{offending} forces Claude Code to defer the agentacct MCP tools "
                            "(un-primed sessions record nothing); set ENABLE_TOOL_SEARCH to `auto` "
                            "(or `false`) in the settings `env` block so the tools load upfront and "
                            "are directly callable."
                        ),
                    }
                )
            else:
                checks.append({"status": "ok", "name": "env.ENABLE_TOOL_SEARCH", "details": details})
        else:
            file_names = ", ".join(file_name for file_name, _ in active_settings_payloads)
            checks.append(
                {
                    "status": "warn",
                    "name": "env.ENABLE_TOOL_SEARCH",
                    "details": (
                        f"not set in {file_names} — without ENABLE_TOOL_SEARCH=auto in a settings `env` block, "
                        "the agentacct MCP tools load deferred and un-primed Claude Code sessions record nothing. "
                        "This doctor only reads project-level settings, so the key may already be set in user-level "
                        "~/.claude/settings.json; if not, merge the example's \"env\" block (never overwriting "
                        "existing env keys)."
                    ),
                }
            )
    return checks
