from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .client_usage import cursor_state_db_path, discover_cursor_usage
from .confidence import COST_CLIENT_REPORTED, COST_UNKNOWN, USAGE_CLIENT_REPORTED, USAGE_UNKNOWN
from .source_paths import resolve_usage_source_paths

SourceStatus = Literal["found", "missing", "error"]


@dataclass(frozen=True)
class UsageSourceDiscovery:
    client: str
    display_name: str
    status: SourceStatus
    evidence: str
    paths: list[str]
    file_count: int
    session_count: int | None
    latest_updated_at: int | None
    usage_confidence: str
    cost_confidence: str
    importer: str | None
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def discover_usage_sources(
    *,
    codex_home: Path | None = None,
    claude_home: Path | None = None,
    opencode_home: Path | None = None,
    hermes_home: Path | None = None,
    openclaw_home: Path | None = None,
    cursor_home: Path | None = None,
) -> list[UsageSourceDiscovery]:
    """Detect known local stores without retaining prompt or response content."""

    return [
        _discover_claude_code_source(claude_home),
        _discover_codex_source(codex_home),
        _discover_opencode_source(opencode_home),
        _discover_hermes_source(hermes_home),
        _discover_openclaw_source(openclaw_home),
        _discover_cursor_source(cursor_home),
    ]


def _discover_claude_code_source(claude_home: Path | None) -> UsageSourceDiscovery:
    source_plan = resolve_usage_source_paths("claude-code", explicit_home=claude_home)
    homes = list(source_plan.homes)
    projects_roots = [home / "projects" for home in homes]
    files = _dedupe_paths(path for root in projects_roots for path in _matching_files(root, ["*.jsonl"]))
    paths = _existing_or_attempted_paths(projects_roots)
    status: SourceStatus = "found" if files else "missing"
    notes = ["assistant message usage rows; message text is not imported"]
    if source_plan.omitted_home_count:
        notes.append(f"source path plan omitted {source_plan.omitted_home_count} path(s) after its safety limit")
    return UsageSourceDiscovery(
        client="claude-code",
        display_name="Claude Code",
        status=status,
        evidence="projects-jsonl",
        paths=paths,
        file_count=len(files),
        session_count=len(files) if files else None,
        latest_updated_at=_latest_mtime(files),
        # File presence proves a session source exists, not that this scan has
        # parsed a client-reported usage row from it.
        usage_confidence=USAGE_UNKNOWN,
        cost_confidence=COST_UNKNOWN,
        importer="agentacct usage import-local --client claude-code" if files else None,
        notes=notes,
    )


def _discover_codex_source(codex_home: Path | None) -> UsageSourceDiscovery:
    source_plan = resolve_usage_source_paths("codex", explicit_home=codex_home)
    homes = list(source_plan.homes)
    session_roots = [path for home in homes for path in (home / "sessions", home / "archived_sessions")]
    active_session_roots = [home / "sessions" for home in homes]
    files_by_home = [
        _dedupe_paths(_matching_files(root, ["rollout-*.jsonl"]))
        for root in active_session_roots
    ]
    files = _dedupe_paths(path for home_files in files_by_home for path in home_files)
    db_paths_by_home = [
        home / "state_5.sqlite" if _is_file(home / "state_5.sqlite") else None
        for home in homes
    ]
    db_paths = [path for path in db_paths_by_home if path is not None]
    db_session_ids_by_home = [
        _sqlite_text_values(db_path, "threads", "id") if db_path is not None else set()
        for db_path in db_paths_by_home
    ]
    db_usage_counts = [
        _sqlite_count(db_path, "threads", where="tokens_used > 0") if db_path is not None else 0
        for db_path in db_paths_by_home
    ]
    db_error_count = sum(
        session_ids is None or usage_count is None
        for session_ids, usage_count in zip(db_session_ids_by_home, db_usage_counts, strict=True)
    )
    db_usage_count = sum(count or 0 for count in db_usage_counts)
    rollout_session_ids_by_home = [
        _codex_rollout_session_ids(home_files, root=root)
        for home_files, root in zip(files_by_home, active_session_roots, strict=True)
    ]
    session_count = sum(
        len((db_session_ids or set()) | rollout_session_ids)
        for db_session_ids, rollout_session_ids in zip(
            db_session_ids_by_home,
            rollout_session_ids_by_home,
            strict=True,
        )
    )
    paths = _existing_or_attempted_paths([*db_paths, *session_roots])
    latest = _latest_mtime([*files, *db_paths])
    # Rollout identity now produces an honest session observation even when a
    # state-db row or token_count event is absent. Source discovery is only a
    # presence scan, so a rollout file is enough to expose the import path;
    # parse/usage truth remains the ingestion health endpoint's job.
    found = session_count > 0
    notes = ["rollout session identity and token_count events; local cost usually unavailable"]
    if db_error_count:
        notes.append(
            f"{db_error_count} Codex state database(s) could not be read; inspect or repair state_5.sqlite"
        )
    if source_plan.omitted_home_count:
        notes.append(f"source path plan omitted {source_plan.omitted_home_count} path(s) after its safety limit")
    return UsageSourceDiscovery(
        client="codex",
        display_name="Codex",
        status="error" if db_error_count else "found" if found else "missing",
        evidence="state-db+rollout-jsonl",
        paths=paths,
        file_count=len(files),
        session_count=session_count or None,
        latest_updated_at=latest,
        usage_confidence=USAGE_CLIENT_REPORTED if db_usage_count else USAGE_UNKNOWN,
        cost_confidence=COST_UNKNOWN,
        importer="agentacct usage import-local --client codex" if found else None,
        notes=notes,
    )


def _codex_rollout_session_ids(paths: Iterable[Path], *, root: Path) -> set[str]:
    """Return first-row rollout identities without retaining transcript bodies."""

    session_ids: set[str] = set()
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return session_ids
    for path in paths:
        file_fd: int | None = None
        try:
            if path.is_symlink():
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(resolved_root):
                continue
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(path, flags)
            with os.fdopen(file_fd, "r", encoding="utf-8", errors="replace") as handle:
                file_fd = None
                line = handle.readline(65_537)
            if not line or len(line) > 65_536:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("type") != "session_meta":
                continue
            payload = row.get("payload")
            session_id = payload.get("id") if isinstance(payload, dict) else None
            if isinstance(session_id, str) and session_id.strip():
                session_ids.add(session_id.strip())
        except OSError:
            continue
        finally:
            if file_fd is not None:
                os.close(file_fd)
    return session_ids


def _discover_opencode_source(opencode_home: Path | None) -> UsageSourceDiscovery:
    homes = _paths_from_explicit_or_env(opencode_home, env_name="OPENCODE_DATA_DIR", defaults=[Path.home() / ".local" / "share" / "opencode"])
    json_files = _dedupe_paths(path for home in homes for path in _matching_files(home, ["*.jsonl", "*.json"]))
    db_files = _dedupe_paths(path for home in homes for path in _matching_files(home, ["*.db"]))
    files = [*json_files, *db_files]
    found = bool(files)
    notes = ["JSON event streams can be imported; native database parsing is pending"]
    multiple_homes = len(homes) > 1
    if multiple_homes:
        notes.append(
            "multiple OpenCode homes detected; select one explicitly with --opencode-home so raw session ids cannot cross namespaces"
        )
    importer = (
        "agentacct usage import-local --client opencode"
        if json_files and not multiple_homes
        else None
    )
    return UsageSourceDiscovery(
        client="opencode",
        display_name="OpenCode",
        status="found" if found else "missing",
        evidence="json-event-streams-or-db",
        paths=_existing_or_attempted_paths(homes),
        file_count=len(files),
        session_count=len(json_files) if json_files else None,
        latest_updated_at=_latest_mtime(files),
        usage_confidence=USAGE_UNKNOWN,
        cost_confidence=COST_UNKNOWN,
        importer=importer,
        notes=notes,
    )


def _discover_hermes_source(hermes_home: Path | None) -> UsageSourceDiscovery:
    homes = _paths_from_explicit_or_env(hermes_home, env_name="HERMES_HOME", defaults=[Path.home() / ".hermes"])
    db_paths = [
        home if home.name == "state.db" else home / "state.db"
        for home in homes
    ]
    existing_dbs, unresolved_db_count = _dedupe_regular_file_inodes(db_paths)
    requires_explicit_selection = (
        hermes_home is None
        and len(homes) > 1
        and (unresolved_db_count > 0 or len(existing_dbs) != 1)
    )
    session_count = sum(_sqlite_count(db_path, "sessions", where="model is not null and trim(model) != ''") or 0 for db_path in existing_dbs)
    model_count = sum(_sqlite_distinct_count(db_path, "sessions", "model", where="model is not null and trim(model) != ''") or 0 for db_path in existing_dbs)
    positive_usage_count = sum(
        _sqlite_positive_numeric_count(
            db_path,
            "sessions",
            (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
            ),
        )
        or 0
        for db_path in existing_dbs
    )
    positive_cost_count = sum(
        _sqlite_count(
            db_path,
            "sessions",
            where="(actual_cost_usd is not null and actual_cost_usd > 0) or (estimated_cost_usd is not null and estimated_cost_usd > 0)",
        )
        or 0
        for db_path in existing_dbs
    )
    found = bool(existing_dbs and session_count)
    notes = ["SQLite state.db sessions; costs remain client_reported, not provider_billed"]
    if requires_explicit_selection:
        notes.append(
            "multiple Hermes homes detected; select one explicitly with --hermes-home so raw session ids cannot cross namespaces"
        )
    if model_count:
        notes.append(f"{model_count} model(s) detected")
    if found and not positive_cost_count:
        notes.append("no positive Hermes cost fields detected")
    if found and not positive_usage_count:
        notes.append("no positive Hermes token fields detected")
    return UsageSourceDiscovery(
        client="hermes",
        display_name="Hermes",
        status="found" if found else "missing",
        evidence="state-db",
        paths=_existing_or_attempted_paths(db_paths),
        file_count=len(existing_dbs),
        session_count=session_count or None,
        latest_updated_at=_latest_mtime(existing_dbs),
        usage_confidence=USAGE_CLIENT_REPORTED if positive_usage_count else USAGE_UNKNOWN,
        cost_confidence=COST_CLIENT_REPORTED if positive_cost_count else COST_UNKNOWN,
        importer=(
            "agentacct usage import-local --client hermes"
            if found and not requires_explicit_selection
            else None
        ),
        notes=notes,
    )


def _discover_openclaw_source(openclaw_home: Path | None) -> UsageSourceDiscovery:
    homes = _paths_from_explicit_or_env(
        openclaw_home,
        env_name="OPENCLAW_DIR",
        defaults=[Path.home() / ".openclaw", Path.home() / ".clawdbot", Path.home() / ".moltbot", Path.home() / ".moldbot"],
    )
    files = _dedupe_paths(path for home in homes for path in _matching_files(home, ["*.jsonl", "*.jsonl.deleted.*", "*.jsonl.reset.*"]))
    found = bool(files)
    notes = ["assistant usage rows; message text is not imported"]
    return UsageSourceDiscovery(
        client="openclaw",
        display_name="OpenClaw",
        status="found" if found else "missing",
        evidence="jsonl-logs",
        paths=_existing_or_attempted_paths(homes),
        file_count=len(files),
        session_count=len(files) if files else None,
        latest_updated_at=_latest_mtime(files),
        usage_confidence=USAGE_UNKNOWN,
        cost_confidence=COST_UNKNOWN,
        importer="agentacct usage import-local --client openclaw" if found else None,
        notes=notes,
    )


def _discover_cursor_source(cursor_home: Path | None) -> UsageSourceDiscovery:
    """Use the importer's exact parser/trust boundary for source discovery."""

    stats: dict[str, object] = {}
    observations = []
    discover_cursor_usage(
        cursor_home=cursor_home,
        # Source discovery reports exact pre-limit cardinality from diagnostics;
        # the bounded observations themselves are discarded here.
        limit_sessions=200,
        _discovery_stats=stats,
        _session_observations=observations,
    )
    error_codes = [
        str(code)
        for code in stats.get("error_codes") or []
        if isinstance(code, str)
    ]
    discovered = int(stats.get("discovered") or 0)
    source_present = bool(stats.get("source_present"))
    path = cursor_state_db_path(cursor_home)
    notes = [
        "observation-only composer identities and allowlisted model metadata; private content, usage, and costs are not selected"
    ]
    if error_codes:
        notes.append("Cursor source requires recovery: " + ", ".join(error_codes))
    elif source_present and not discovered:
        notes.append("primary state.vscdb exists but contains no composerData session rows")
    return UsageSourceDiscovery(
        client="cursor",
        display_name="Cursor",
        status="error" if error_codes else "found" if source_present else "missing",
        evidence="primary-state-vscdb-composer-observations",
        paths=[str(path)],
        file_count=1 if source_present else 0,
        session_count=discovered if source_present else None,
        latest_updated_at=(
            int(stats.get("watermark")) if stats.get("watermark") else None
        ),
        usage_confidence=USAGE_UNKNOWN,
        cost_confidence=COST_UNKNOWN,
        importer=(
            "agentacct usage import-local --client cursor"
            if source_present and not error_codes
            else None
        ),
        notes=notes,
    )
def _paths_from_explicit_or_env(explicit: Path | None, *, env_name: str, defaults: list[Path]) -> list[Path]:
    if explicit is not None:
        return [explicit.expanduser()]
    env_value = os.environ.get(env_name)
    if env_value:
        return [
            Path(value.strip()).expanduser()
            for value in env_value.split(",")
            if value.strip()
        ]
    return defaults


def _matching_files(root: Path, patterns: list[str]) -> list[Path]:
    if not _is_dir(root):
        return []
    files: list[Path] = []
    for pattern in patterns:
        try:
            files.extend(path for path in root.rglob(pattern) if _is_file(path))
        except OSError:
            continue
    return files


def _existing_or_attempted_paths(paths: list[Path]) -> list[str]:
    existing = [str(path) for path in paths if path.exists()]
    return existing or [str(path) for path in paths]


def _latest_mtime(paths: list[Path]) -> int | None:
    latest: float | None = None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        latest = mtime if latest is None else max(latest, mtime)
    return int(latest) if latest is not None else None


def _sqlite_count(db_path: Path, table: str, *, where: str | None = None) -> int | None:
    return _sqlite_scalar_count(db_path, f"select count(*) from {table}" + (f" where {where}" if where else ""))


def _sqlite_distinct_count(db_path: Path, table: str, column: str, *, where: str | None = None) -> int | None:
    return _sqlite_scalar_count(db_path, f"select count(distinct {column}) from {table}" + (f" where {where}" if where else ""))


def _sqlite_positive_numeric_count(
    db_path: Path,
    table: str,
    columns: tuple[str, ...],
) -> int | None:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            available = {
                str(row[1])
                for row in con.execute(f"pragma table_info({table})").fetchall()
            }
            selected = [column for column in columns if column in available]
            if not selected:
                return 0
            where = " or ".join(
                f"coalesce({column}, 0) > 0" for column in selected
            )
            value = con.execute(
                f"select count(*) from {table} where {where}"
            ).fetchone()[0]
        finally:
            con.close()
    except (OSError, sqlite3.DatabaseError, TypeError):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sqlite_text_values(db_path: Path, table: str, column: str) -> set[str] | None:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(f"select {column} from {table}").fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.DatabaseError, TypeError):
        return None
    return {
        str(row[0]).strip()
        for row in rows
        if row and row[0] is not None and str(row[0]).strip()
    }


def _sqlite_scalar_count(db_path: Path, query: str) -> int | None:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            value = con.execute(query).fetchone()[0]
        finally:
            con.close()
    except (OSError, sqlite3.DatabaseError, TypeError):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _dedupe_regular_file_inodes(
    paths: Iterable[Path],
) -> tuple[list[Path], int]:
    """Keep one deterministic, regular Hermes-root path per database inode."""

    deduped: list[Path] = []
    seen: set[tuple[int, int]] = set()
    unresolved = 0
    for path in sorted(
        paths,
        key=lambda candidate: os.path.normcase(
            os.path.abspath(os.fspath(candidate))
        ),
    ):
        # client_usage opens the configured Hermes home itself with
        # O_NOFOLLOW before opening state.db beneath it. Match that boundary:
        # a root-directory symlink must not be advertised as importable here.
        if not _is_dir(path.parent) or not _is_file(path):
            unresolved += 1
            continue
        try:
            observed = path.stat(follow_symlinks=False)
        except OSError:
            unresolved += 1
            continue
        identity = (int(observed.st_dev), int(observed.st_ino))
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(path)
    return deduped, unresolved


def _is_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir() and not path.is_symlink()
    except OSError:
        return False
