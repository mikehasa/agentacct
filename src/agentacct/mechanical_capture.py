"""PostToolUse exit-code capture — the ONLY local source of *independent* checks.

When the agent runs a test / build / lint / typecheck command in the terminal,
the Claude Code PostToolUse hook records the exit code the HARNESS observed —
not one the agent typed into a summary. That is what lifts a step from
``self_checked`` (the agent's own word) to ``independently_checked``.

Privacy: only a coarse command CATEGORY (test/build/lint/typecheck), the runner
name (``pytest``, ``cargo``, …), and a sha256 DIGEST of the command are ever
recorded — never the command string, its arguments, its environment, or its
output. A command that is not a clear, recognized check runner is NOT captured:
a missed check is honest; a mislabeled one is not.

The path mirrors ``tool_activity`` exactly: the hook appends one tiny JSON line
to a per-store spool (no daemon, O(1), fail-open); ``agentacct usage
import-local`` later drains the spool into ``agent-chronicle.evidence.v2``
``machine_check_observed`` envelopes (source_type ``client_hook``) that the
existing ``mechanical_checks.build_mechanical_check_events`` ingestion projects
into Task checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

_SPOOL_RELATIVE_PATH = Path("spool") / "claude-mechanical-check.jsonl"

# The four check kinds the ingestion accepts (mechanical_checks._CHECK_KINDS).
CHECK_KINDS = {"test", "build", "lint", "typecheck"}

# The same validators the ingestion (mechanical_checks) enforces — a tick that
# would not survive projection is dropped at drain time rather than written.
_SAFE_RUNNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}$")
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")

# Leading tokens that are environment assignments or bare command wrappers to
# skip before the real runner (e.g. ``FOO=bar sudo pytest``, ``time pytest``).
# Only the bare wrapper NAME is skipped, not its options/arguments — a wrapper
# invoked with a flag or a value arg (``sudo -E pytest``, ``timeout 300 pytest``,
# ``nice -n 10 pytest``) is deliberately NOT recognized. That is a conservative
# miss (an honest un-captured check), never a false positive.
_WRAPPERS = {"sudo", "env", "time", "nice", "ionice", "command", "exec", "stdbuf", "nohup"}
# Wrappers whose FIRST argument ``run`` precedes the real runner.
_RUN_WRAPPERS = {"poetry", "uv", "pdm", "hatch", "rye", "pipenv", "npx", "bunx"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_SEPARATORS = {"&&", "||", ";", "|", "|&", "&"}
# Flags that mean the invocation runs NO verification (a help/version print or a
# dry listing). A command carrying one is not a check — recording its exit 0 as
# a passing check would forge the evidence tier.
_NO_OP_FLAGS = {"-h", "--help", "--version", "-V", "--collect-only", "--co", "--init"}


def _classify_segment(tokens: list[str]) -> tuple[str, str] | None:
    """Classify ONE simple command (already split from any shell operators)."""

    # Strip env-assignments and plain wrappers off the front.
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ENV_ASSIGN.match(token) or token in _WRAPPERS:
            index += 1
            continue
        break
    tokens = tokens[index:]
    if not tokens:
        return None
    head = tokens[0]
    # ``poetry run pytest`` / ``uv run pytest`` / ``npx eslint`` → drop the wrapper.
    base = os.path.basename(head)
    if base in _RUN_WRAPPERS:
        rest = tokens[1:]
        if base in {"npx", "bunx"}:
            return _classify_segment(rest)  # the tool follows directly
        if rest and rest[0] == "run":
            return _classify_segment(rest[1:])
        return _classify_segment(rest)
    args = tokens[1:]
    a0 = args[0] if args else ""
    # A help / version / dry-listing invocation runs no verification — not a check.
    if any(token in _NO_OP_FLAGS for token in args):
        return None

    if base in {"pytest", "py.test"}:
        return ("test", "pytest")
    if base in {"python", "python3"} and "-m" in args:
        module = args[args.index("-m") + 1] if args.index("-m") + 1 < len(args) else ""
        module = module.split(".")[0]
        return {
            "pytest": ("test", "pytest"),
            "unittest": ("test", "unittest"),
            "mypy": ("typecheck", "mypy"),
            "ruff": ("lint", "ruff"),
            "flake8": ("lint", "flake8"),
            "pylint": ("lint", "pylint"),
        }.get(module)
    if base in {"mypy"}:
        return ("typecheck", "mypy")
    if base in {"pyright", "pyright-python"}:
        return ("typecheck", "pyright")
    if base in {"tsc"}:
        return ("typecheck", "tsc")
    if base == "ruff":
        # ``ruff format`` REWRITES files (it verifies nothing) unless it is a
        # ``--check`` / ``--diff`` dry run; ``ruff check`` (or bare ruff) lints.
        if a0 == "format":
            return ("lint", "ruff") if ("--check" in args or "--diff" in args) else None
        return ("lint", "ruff")
    if base == "eslint":
        # ``eslint --fix`` auto-fixes (rewrites files) rather than only reporting.
        return None if "--fix" in args else ("lint", "eslint")
    if base in {"flake8", "pylint", "pyflakes"}:
        return ("lint", base)
    if base in {"jest", "vitest", "mocha", "ava"}:
        return ("test", base)
    if base in {"prettier"}:
        return ("lint", "prettier") if "--check" in args or "-c" in args else None
    if base in {"npm", "pnpm", "yarn", "bun"}:
        runner = base
        sub = a0
        target = args[1] if len(args) > 1 else ""
        if sub == "run":
            sub = target
        return {
            "test": ("test", runner),
            "build": ("build", runner),
            "lint": ("lint", runner),
            "typecheck": ("typecheck", runner),
            "type-check": ("typecheck", runner),
            "tsc": ("typecheck", runner),
        }.get(sub)
    if base == "cargo":
        return {
            "test": ("test", "cargo"),
            "build": ("build", "cargo"),
            "check": ("typecheck", "cargo"),
            "clippy": ("lint", "cargo"),
        }.get(a0)
    if base == "go":
        return {"test": ("test", "go"), "build": ("build", "go"), "vet": ("lint", "go")}.get(a0)
    if base == "swift":
        return {"test": ("test", "swift"), "build": ("build", "swift")}.get(a0)
    if base in {"gradle", "gradlew"} or head in {"./gradlew"}:
        # Gradle's ``check`` lifecycle task RUNS the test task, so it is a test
        # run, not a lint — mapping it to lint would mislabel the kind.
        for token in args:
            if token in {"test", "check", "build"}:
                return {"test": ("test", "gradle"), "check": ("test", "gradle"), "build": ("build", "gradle")}[token]
        return None
    if base == "mvn":
        for token in args:
            if token in {"test", "verify"}:
                return ("test", "maven")
            if token in {"package", "install"}:
                return ("build", "maven")
        return None
    if base == "make":
        return {
            "test": ("test", "make"),
            "build": ("build", "make"),
            "lint": ("lint", "make"),
            "typecheck": ("typecheck", "make"),
            "check": ("test", "make"),
        }.get(a0)
    return None


def classify_command(command: Any) -> tuple[str, str] | None:
    """Recognize a check runner in a shell command. Returns ``(check_kind,
    runner)`` or ``None`` when the command is not an unambiguous, single check
    whose exit code the harness actually observes.

    Conservative on THREE fronts, because the exit code the PostToolUse hook sees
    is the whole line's, not any one command's:
      * unparseable input, no recognized runner, or two DIFFERENT recognized
        runners → ``None`` (ambiguous);
      * a recognized runner whose failure would be MASKED by a later ``;``,
        ``||``, ``|`` (e.g. ``pytest || true``, ``pytest | tail``) → ``None``:
        the line can exit 0 while the check failed. Only a runner that is the
        exit-code-determining command counts — the last segment, or one joined
        to the end entirely by ``&&`` (where a failure short-circuits and
        propagates);
      * a non-verifying invocation (``--help``/``--version``/``--collect-only``,
        or a file-rewriting formatter) → ``None`` (see ``_classify_segment``).
    A missed check is honest; a mislabeled one is not.
    """

    text = command if isinstance(command, str) else ""
    text = text.strip()
    if not text:
        return None
    # A newline is a shell statement separator exactly like ``;`` — a runner on
    # an earlier line does NOT determine the whole script's exit code. shlex folds
    # newlines into ordinary whitespace and applies ``#`` comments per line, so
    # split into lines first, tokenize each on its own, and insert an explicit
    # ``;`` between lines. Without this, ``pytest\necho done`` would merge into
    # one segment and a red suite followed by a passing last line reads as a pass.
    tokens: list[str] = []
    for line_index, line in enumerate(re.split(r"[\r\n]+", text)):
        if line_index:
            tokens.append(";")
        try:
            tokens.extend(shlex.split(line, comments=True))
        except ValueError:
            return None
    if not tokens:
        return None
    # Split into (segment, operator-after) pairs, preserving the operator so we
    # can tell whether a runner's exit status reaches the line's exit code.
    segments: list[tuple[list[str], str | None]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            segments.append((current, token))
            current = []
        else:
            current.append(token)
    segments.append((current, None))  # final segment carries no trailing operator

    recognized: list[tuple[tuple[str, str], bool]] = []
    count = len(segments)
    for i, (segment, _operator) in enumerate(segments):
        result = _classify_segment(segment)
        if result is None:
            continue
        # Exit-determining iff every operator from this segment to the end is
        # ``&&`` (a failure short-circuits and propagates). The final segment has
        # no trailing operators, so it always qualifies.
        exit_determining = all(segments[j][1] == "&&" for j in range(i, count - 1))
        recognized.append((result, exit_determining))
    if not recognized:
        return None
    # If any recognized runner's exit is MASKED, we cannot trust the line's exit
    # code as that check's result — record nothing.
    if any(not exit_determining for _result, exit_determining in recognized):
        return None
    distinct = {result for result, _exit_determining in recognized}
    if len(distinct) == 1:
        return next(iter(distinct))
    return None  # zero or ambiguous (multiple different runners)


def command_digest(command: str) -> str:
    """A sha256 digest of the command — the ledger's identity for the check
    without ever storing the command text itself."""

    return "sha256:" + hashlib.sha256((command or "").encode("utf-8", "replace")).hexdigest()


def mechanical_check_spool_path(store_dir: Path | str) -> Path:
    return Path(store_dir) / _SPOOL_RELATIVE_PATH


def record_mechanical_check_tick(
    store_dir: Path | str,
    *,
    client: str,
    session_id: str,
    check_kind: str,
    runner: str,
    digest: str,
    exit_code: int,
    at: float,
) -> None:
    """Append ONE mechanical-check tick to the spool. Best-effort; never raises.

    Only small scalars are written — the check kind, the runner name, a sha256
    command digest, the exit code, the session, and a timestamp. Never the
    command string, its arguments, or its output.
    """

    try:
        if check_kind not in CHECK_KINDS:
            return
        client = str(client or "").strip()
        session_id = str(session_id or "").strip()
        if not client or not session_id:
            return
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return
        target = mechanical_check_spool_path(store_dir)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = json.dumps(
            {
                "c": client,
                "s": session_id,
                "k": check_kind,
                "r": str(runner or "")[:160],
                "d": str(digest or ""),
                "x": int(exit_code),
                "t": float(at),
            },
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


def _build_envelope(tick: Mapping[str, Any]) -> Any | None:
    """Turn one spool tick into a validated ``machine_check_observed`` Evidence-v2
    envelope, or ``None`` if it would not survive the ingestion's checks."""

    from .evidence import EvidenceEnvelope

    client = str(tick.get("c") or "").strip()
    session = str(tick.get("s") or "").strip()
    check_kind = str(tick.get("k") or "").strip()
    runner = str(tick.get("r") or "").strip()
    digest = str(tick.get("d") or "").strip()
    exit_code = tick.get("x")
    at = tick.get("t")
    if not client or not session or check_kind not in CHECK_KINDS:
        return None
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not -65_535 <= exit_code <= 65_535:
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)) or float(at) <= 0:
        return None
    if not _SAFE_RUNNER.fullmatch(runner) or not _SHA256.fullmatch(digest):
        return None
    # Name the wire the check came from honestly, per client: a hermes check arrives
    # on a hermes post_tool_call and an opencode check on a tool.execute.after — not a
    # Claude Code PostToolUse.
    if client == "hermes":
        source_schema, adapter = "hermes_post_tool_call.v1", "hermes_post_tool_call"
    elif client == "opencode":
        source_schema, adapter = "opencode_tool_execute_after.v1", "opencode_tool_execute_after"
    else:
        source_schema, adapter = "claude_code_post_tool_use.v1", "claude_code_post_tool_use"
    try:
        return EvidenceEnvelope.create(
            assertion="observed",
            event_type="machine_check_observed",
            source_type="client_hook",
            source_system=client,
            source_instance=session,
            source_schema=source_schema,
            adapter=adapter,
            event_timestamp=float(at),
            dimensions=["machine_check"],
            measurement_basis="hook_exit_code",
            subjects={"client_session_id": session},
            payload={
                "capture_basis": "host_hook",
                "attributes": {
                    "check_kind": check_kind,
                    "runner": runner,
                    "command_digest": digest,
                    "exit_code": int(exit_code),
                    "passed": int(exit_code) == 0,
                },
            },
            tags=["mechanical_capture"],
        )
    except Exception:  # noqa: BLE001 - a malformed tick must never break the drain.
        return None


def drain_mechanical_check_spool(
    store_dir: Path | str,
    *,
    now: float | None = None,
    token: str | None = None,
) -> list[Any]:
    """Consume the spool and return one Evidence-v2 envelope per valid tick.

    The spool is atomically renamed aside before being read, so ticks that land
    during the drain go to a freshly recreated spool and are picked up by the
    NEXT drain (mirrors ``tool_activity.drain_tool_activity_spool``). Each
    envelope's idempotency key is derived from its content, so re-importing an
    identical check is deduped by the Evidence store, never double-counted.
    """

    spool = mechanical_check_spool_path(store_dir)
    if not spool.exists():
        return []
    batch_token = token or uuid.uuid4().hex
    consuming = spool.with_name(f".{spool.name}.consuming-{os.getpid()}-{batch_token}")
    try:
        os.replace(spool, consuming)
    except (FileNotFoundError, OSError):
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
    envelopes: list[Any] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tick = json.loads(line)
        except ValueError:
            continue
        if not isinstance(tick, Mapping):
            continue
        envelope = _build_envelope(tick)
        if envelope is not None:
            envelopes.append(envelope)
    return envelopes


def build_discovery_mechanical_check_envelopes(
    ticks: Iterable[Mapping[str, Any]],
) -> list[Any]:
    """Build ``machine_check_observed`` envelopes from discovery-derived ticks.

    A client whose observe-only hook does not fire (OpenCode, whose plugin barely
    records) can still have its checks recovered from its own transcript/DB at
    import time. Each tick has the SAME ``{c, s, k, r, d, x, t}`` shape the spool
    holds, so this reuses ``_build_envelope`` — the per-client adapter/schema and
    EVERY validator (safe runner, sha256 digest, exit-code bounds) are byte-
    identical to the hook path, and there is no second wire format to keep in sync.

    The caller MUST stamp each tick's ``t`` with a STABLE source time (e.g. the
    DB's recorded call-end time in epoch seconds), never a fresh clock: the
    envelope's idempotency key is content+``event_timestamp`` derived, so a stable
    timestamp dedupes a re-imported check in the Evidence store instead of double-
    recording it. Returns the built envelopes (invalid ticks are dropped)."""

    envelopes: list[Any] = []
    for tick in ticks:
        if not isinstance(tick, Mapping):
            continue
        envelope = _build_envelope(tick)
        if envelope is not None:
            envelopes.append(envelope)
    return envelopes


def ingest_mechanical_check_spool(
    store_dir: Path | str,
    *,
    evidence: Any,
    now: float | None = None,
    token: str | None = None,
) -> int:
    """Drain the spool and append each check as a ``client_hook`` Evidence-v2
    envelope via ``evidence.append``. Never raises; returns how many were written.
    ``evidence`` is ``service.evidence`` (the fail-open EvidenceRuntime)."""

    try:
        envelopes = drain_mechanical_check_spool(store_dir, now=now, token=token)
    except Exception:  # noqa: BLE001 - a spool drain must never fail the import.
        return 0
    written = 0
    for envelope in envelopes:
        try:
            evidence.append(envelope)
            written += 1
        except Exception:  # noqa: BLE001 - one bad append must not abort the rest.
            continue
    return written


__all__ = [
    "CHECK_KINDS",
    "classify_command",
    "command_digest",
    "mechanical_check_spool_path",
    "record_mechanical_check_tick",
    "drain_mechanical_check_spool",
    "ingest_mechanical_check_spool",
    "build_discovery_mechanical_check_envelopes",
]
