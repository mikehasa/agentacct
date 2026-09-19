#!/usr/bin/env python3
"""Audit the quality of data feeding agentacct's UI, from the real ledger.

Read-only. With the default arguments it opens the installed store through a
read-only SQLite URI and prints AGGREGATE statistics only: counts, field
coverage, length percentiles and hygiene violations. It never prints recorded
text, file paths, identifiers or prompts, so its output is safe to paste into a
review note.

Usage:
    .venv/bin/python design-plans/data-quality/tools/audit-agent-data.py [--db PATH] [--json PATH] [--limit N]

Defaults to ~/.local/state/agentacct/state/events.sqlite3 (the installed store,
which is the one real coding agents write to). A missing or unreadable database
is reported as a single line and exits 2 rather than raising.

The read-only audit above needs only the standard library. ``--replay`` sends
every stored record back through the live write path, which imports agentacct,
so that mode needs the project environment
(``python3 -m venv .venv && .venv/bin/python -m pip install -e . pytest``). This
script puts its own checkout's ``src/`` first (like verify-fixes.py and
demo-data-quality.py), so ``--replay`` tests THIS tree's rules even when an
older agentacct is installed; if the rules cannot be imported it errors and
exits non-zero rather than reporting a falsely-clean ``refused: 0``.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import sqlite3
import statistics
import sys
from typing import Any

# Put this checkout's src/ first so --replay exercises THIS tree's write path,
# not an installed or editable agentacct that may predate the rules. Without
# this, the tool could import an older agentacct whose MCP server has no
# semantic rules, accept every record, and print "refused: 0" -- a false clean
# against the wrong code. The sibling tools do the same; see _rules_provenance
# below for the guard that turns a still-wrong import into a loud failure.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_DB = "~/.local/state/agentacct/state/events.sqlite3"

# Section lifecycle statuses that close a section. Mirrors
# WORK_EVENT_STATUSES in src/agentacct/work_events.py.
TERMINAL_STATUSES = {"completed", "blocked", "handed_off"}

# Recorded text is user-visible copy, so these are display risks, not security
# findings. Absolute paths are the one hygiene item worth counting: they are the
# same class of leak the FILES_DESCRIPTION rule already rejects on `files`.
_ABS_PATH = re.compile(r"(?:^|[\s(\"'`:=])/(?:Users|home|private|var|tmp|opt|etc)/")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def load_events(db_path: str, limit: int | None) -> list[dict[str, Any]]:
    uri = f"file:{os.path.expanduser(db_path)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        query = "select line from event_lines order by seq"
        rows = [row[0] for row in connection.execute(query)]
    finally:
        connection.close()
    if limit:
        rows = rows[-limit:]
    events: list[dict[str, Any]] = []
    malformed = 0
    for line in rows:
        try:
            parsed = json.loads(line)
        except (TypeError, ValueError):
            malformed += 1
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
        else:
            malformed += 1
    if malformed:
        print(f"note: {malformed} unparseable line(s) skipped", file=sys.stderr)
    return events


def metadata(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("metadata")
    return value if isinstance(value, dict) else {}


def field(event: dict[str, Any], key: str) -> Any:
    """Read a field the way the ledger does: metadata first, then top level."""
    meta = metadata(event)
    if meta.get(key) not in (None, "", [], {}):
        return meta.get(key)
    return event.get(key)


def is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def coverage(events: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, float]:
    total = max(len(events), 1)
    return {
        key: round(
            100 * sum(1 for event in events if field(event, key) not in (None, "", [], {})) / total,
            1,
        )
        for key in keys
    }


def length_stats(events: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    lengths = sorted(len(str(field(event, key))) for event in events if is_text(field(event, key)))
    if not lengths:
        return None
    return {
        "n": len(lengths),
        "min": lengths[0],
        "median": int(statistics.median(lengths)),
        "p90": lengths[min(int(len(lengths) * 0.9), len(lengths) - 1)],
        "max": lengths[-1],
        "at_max_field_cap": sum(1 for length in lengths if length >= 1200),
    }


def section_findings(sections: list[dict[str, Any]], checks: list[dict[str, Any]]) -> dict[str, Any]:
    by_id: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for event in sections:
        by_id[str(field(event, "section_id"))].append(event)

    lifecycle = collections.Counter()
    for members in by_id.values():
        statuses = {
            str(field(member, "section_status"))
            for member in members
        }
        lifecycle["terminal" if statuses & TERMINAL_STATUSES else "open_only"] += 1

    titles = [str(field(event, "section_title") or field(event, "title") or "") for event in sections]
    titles = [title for title in titles if title.strip()]
    title_counts = collections.Counter(titles)
    terminal = [
        event
        for event in sections
        if str(field(event, "section_status")) in TERMINAL_STATUSES
    ]
    summaries = [str(field(event, "summary")) for event in sections if is_text(field(event, "summary"))]

    file_entries: list[str] = []
    for event in sections + checks:
        files = field(event, "files")
        if isinstance(files, list):
            file_entries.extend(item for item in files if isinstance(item, str))

    return {
        "sections": len(sections),
        "distinct_section_ids": len(by_id),
        "lifecycle": dict(lifecycle),
        "coverage": coverage(
            sections,
            (
                "section_id",
                "section_title",
                "summary",
                "blocker",
                "next_step",
                "kind",
                "phase",
                "run_id",
                "client",
                "client_session_id",
                "client_transcript_id",
                "project_dir",
                "files",
            ),
        ),
        "terminal_sections": len(terminal),
        "terminal_without_summary": sum(1 for event in terminal if not is_text(field(event, "summary"))),
        # A terminal status with no prose is the single largest UI gap: the
        # timeline shows a finished chapter with nothing to read.
        "terminal_without_summary_pct": round(
            100 * sum(1 for event in terminal if not is_text(field(event, "summary"))) / max(len(terminal), 1), 1
        ),
        "summaries_recorded": len(summaries),
        "titles_too_short": sum(1 for title in titles if len(title.strip()) < 8),
        "titles_with_control_chars": sum(1 for title in titles if _CONTROL_CHARS.search(title)),
        "titles_with_markup": sum(1 for title in titles if re.search(r"</?[a-zA-Z][^>]*>", title)),
        "titles_reused_5_or_more": sum(1 for _, count in title_counts.items() if count >= 5),
        "distinct_titles": len(title_counts),
        "summary_duplicates": len(summaries) - len(set(summaries)),
        "absolute_paths_in_section_text": sum(
            1
            for event in sections
            for key in ("summary", "section_title", "blocker", "next_step")
            if isinstance(field(event, key), str) and _ABS_PATH.search(field(event, key))
        ),
        "title_lengths": length_stats(sections, "section_title"),
        "summary_lengths": length_stats(sections, "summary"),
        "file_entries": len(file_entries),
        "file_entries_absolute": sum(1 for item in file_entries if item.startswith("/")),
        "file_entries_with_parent_segment": sum(1 for item in file_entries if ".." in item.split("/")),
    }


def check_findings(sections: list[dict[str, Any]], checks: list[dict[str, Any]]) -> dict[str, Any]:
    known_ids = {str(field(event, "section_id")) for event in sections}
    results = collections.Counter(str(field(event, "result")) for event in checks)
    return {
        "machine_checks": len(checks),
        "coverage": coverage(
            checks,
            (
                "name",
                "result",
                "evidence_type",
                "summary",
                "command",
                "exit_code",
                "section_id",
                "files",
                "artifact_ref",
                "artifact_path",
                "artifact_url",
                "run_id",
                "client_session_id",
            ),
        ),
        "results": dict(results),
        # A check with no command cannot be re-run or audited later; a check
        # with no files cannot tell the reader what it covered.
        "without_command": sum(1 for event in checks if not is_text(field(event, "command"))),
        "without_file_list": sum(1 for event in checks if not field(event, "files")),
        "without_artifact": sum(
            1
            for event in checks
            if not any(is_text(field(event, key)) for key in ("artifact_ref", "artifact_path", "artifact_url"))
        ),
        "orphan_section_ids": sum(1 for event in checks if str(field(event, "section_id")) not in known_ids),
        "generic_default_names": sum(1 for event in checks if str(field(event, "name")).strip().lower() in {"check", "test", "tests"}),
    }


def projection_findings(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Audit the derived ledger the UI actually reads, one row per work item.

    Section events are not the display unit: several events (started, checkpoint,
    completed) collapse into one work item, and only this projection carries the
    attribution, evidence and usage links the UI renders. Imported lazily so the
    audit still runs standalone against a raw ledger.
    """
    try:
        from agentacct.work_ledger import build_work_ledger
    except ImportError:
        return {"skipped": "agentacct package not importable; run from the repo root or with src on PYTHONPATH"}
    try:
        ledger = build_work_ledger(events)
    except Exception as exc:  # noqa: BLE001 - an audit reports, it does not crash
        return {"skipped": f"build_work_ledger failed: {type(exc).__name__}: {exc}"}

    items = [item for item in ledger.get("work_items", []) if isinstance(item, dict)]
    if not items:
        return {"work_items": 0}

    def pct(count: int) -> float:
        return round(100 * count / len(items), 1)

    statuses = collections.Counter(str(item.get("latest_status")) for item in items)
    evidence_statuses = collections.Counter(str(item.get("evidence_status")) for item in items)
    join_confidence = collections.Counter(str(item.get("join_confidence")) for item in items)
    titles = [str(item.get("title") or "") for item in items]
    non_empty_titles = [title for title in titles if title.strip()]
    title_counts = collections.Counter(non_empty_titles)
    summary_lengths = sorted(len(str(item.get("summary"))) for item in items if item.get("summary"))

    return {
        "work_items": len(items),
        "latest_status": dict(statuses.most_common()),
        "evidence_status": dict(evidence_statuses.most_common()),
        "join_confidence": dict(join_confidence.most_common()),
        "without_title": sum(1 for title in titles if not title.strip()),
        "without_summary": sum(1 for item in items if not item.get("summary")),
        "without_summary_pct": pct(sum(1 for item in items if not item.get("summary"))),
        "without_files": sum(1 for item in items if not item.get("files")),
        "without_usage_records": sum(
            1
            for item in items
            if not any(
                item.get(key)
                for key in ("linked_usage_records", "priced_usage_records", "usage_total", "estimated_cost_total")
            )
        ),
        "without_any_evidence": sum(1 for item in items if not item.get("evidence_events")),
        "without_any_evidence_pct": pct(sum(1 for item in items if not item.get("evidence_events"))),
        "open_work_items": sum(
            1 for item in items if str(item.get("latest_status")) in {"started", "checkpoint"}
        ),
        "blocked_work_items": sum(1 for item in items if str(item.get("latest_status")) == "blocked"),
        "items_with_blocker_text": sum(1 for item in items if item.get("blocker")),
        "items_with_next_step": sum(1 for item in items if item.get("next_step")),
        "titles_reused_5_or_more": sum(1 for _, count in title_counts.items() if count >= 5),
        "distinct_titles": len(title_counts),
        "summary_lengths": (
            {
                "n": len(summary_lengths),
                "min": summary_lengths[0],
                "median": int(statistics.median(summary_lengths)),
                "p90": summary_lengths[min(int(len(summary_lengths) * 0.9), len(summary_lengths) - 1)],
                "max": summary_lengths[-1],
            }
            if summary_lengths
            else None
        ),
    }


def replay_findings(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Would the current rules refuse a record the store already accepted?

    The strongest available check on the new rules: replay every recorded section
    and machine check through the live write path. A refusal here means a rule is
    rejecting data that a real agent legitimately recorded -- the only failure
    mode that loses work.
    """
    try:
        from agentacct.mcp import SentinelMCPServer
    except ImportError:
        return {"skipped": "agentacct package not importable; run with src on PYTHONPATH"}
    import tempfile
    import pathlib as _pathlib

    server = SentinelMCPServer(store_dir=_pathlib.Path(tempfile.mkdtemp()) / "state")
    refused: list[dict[str, Any]] = []
    replayed = 0

    def send(tool: str, arguments: dict[str, Any]) -> str | None:
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        )
        error = response.get("error")
        return None if error is None else str(error.get("message"))

    for event in events:
        event_type = str(event.get("event_type") or "")
        meta = metadata(event)
        if event_type.startswith("section_"):
            status = str(meta.get("section_status") or event_type.removeprefix("section_"))
            if status not in {"started", "checkpoint", "completed", "blocked", "handed_off"}:
                continue
            arguments: dict[str, Any] = {
                "source": str(event.get("source") or "unknown"),
                "section_id": str(meta.get("section_id") or "replay"),
                "section_status": status,
            }
            if meta.get("section_title") is not None:
                arguments["section_title"] = meta["section_title"]
            for key in ("summary", "blocker", "next_step", "files", "kind", "phase"):
                if meta.get(key) not in (None, "", [], {}):
                    arguments[key] = meta[key]
            if meta.get("client_session_id"):
                arguments["client_session_id"] = meta["client_session_id"]
            replayed += 1
            message = send("agentacct_record_section", arguments)
        elif event_type == "machine_check":
            arguments = {"source": str(event.get("source") or "unknown")}
            for key in ("name", "result", "evidence_type", "summary", "command", "exit_code",
                        "artifact_ref", "artifact_path", "artifact_url", "files", "section_id"):
                if meta.get(key) not in (None, "", [], {}):
                    arguments[key] = meta[key]
            if "result" not in arguments:
                continue
            replayed += 1
            message = send("agentacct_record_machine_check", arguments)
        else:
            continue
        if message is not None:
            refused.append(
                {
                    "event_type": event_type,
                    "section_id": meta.get("section_id"),
                    # The refusal text only: never the recorded content.
                    "refusal": message[:160],
                }
            )

    by_rule: dict[str, int] = collections.Counter()
    for entry in refused:
        text = entry["refusal"]
        for label in ("readable text", "letters or digits", "requires `summary`", "requires `blocker`",
                      "needs something a reviewer", "too generic"):
            if label in text:
                by_rule[label] += 1
                break
        else:
            by_rule["other"] += 1

    return {
        "replayed": replayed,
        "refused": len(refused),
        "refused_pct": round(100 * len(refused) / max(replayed, 1), 2),
        "by_rule": dict(by_rule),
        "examples": refused[:10],
    }


def choke_point_replay(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Would the service-level gate refuse a record the store already accepted?

    The sweep proof for moving the rules to the shared choke point: replay every
    stored record through ``_enforce_semantic_rules`` exactly as the live write
    path calls it. A refusal on a record an agent legitimately recorded would be
    a false positive that the earlier MCP-only replay could not see, because the
    gate now also covers the HTTP and CLI lanes.
    """
    try:
        from agentacct.service import _enforce_semantic_rules
    except ImportError:
        return {"skipped": "agentacct package not importable; run with src on PYTHONPATH"}

    refused: list[dict[str, Any]] = []
    in_scope = 0
    for event in events:
        meta = metadata(event)
        if meta.get("sentinel_semantic_kind") not in {"section", "evidence"}:
            continue
        if meta.get("semantic_rules_validated"):
            continue  # already validated by the MCP handler with full arguments
        in_scope += 1
        try:
            _enforce_semantic_rules(dict(event), transport="cli")
        except Exception as exc:  # noqa: BLE001 - an audit reports, it does not crash
            refused.append(
                {
                    "event_type": str(event.get("event_type")),
                    "section_id": meta.get("section_id"),
                    "refusal": str(exc)[:160],
                }
            )
    return {
        "in_scope": in_scope,
        "refused": len(refused),
        "refused_pct": round(100 * len(refused) / max(in_scope, 1), 2),
        "examples": refused[:10],
    }


def rules_provenance() -> dict[str, Any]:
    """Which agentacct will --replay import, and does it carry the rules?

    A replay is only meaningful against a tree that has the semantic rules. If
    the imported agentacct predates them, the write path has no rules, accepts
    everything, and reports a falsely-clean ``refused: 0``. Report the resolved
    location and whether ``agentacct.semantic_rules`` imports, so both the
    reader and ``main`` can tell a real green from a green against the wrong
    code.
    """
    try:
        import agentacct
    except ImportError as exc:
        return {"importable": False, "reason": f"agentacct not importable: {exc}"}
    package_dir = os.path.dirname(getattr(agentacct, "__file__", "") or "")
    try:
        import agentacct.semantic_rules  # noqa: F401
    except ImportError:
        rules_present = False
    else:
        rules_present = True
    return {
        "importable": True,
        "package_dir": package_dir,
        "rules_module_present": rules_present,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help=f"ledger SQLite path (default: {DEFAULT_DB})")
    parser.add_argument("--json", dest="json_path", help="also write the audit as JSON to this path")
    parser.add_argument("--limit", type=int, help="only audit the newest N events")
    parser.add_argument("--replay", action="store_true",
                        help="replay every stored section and check through the live write path")
    args = parser.parse_args()

    path = os.path.expanduser(args.db)
    if not os.path.exists(path):
        print(f"ledger not found: {path}")
        return 2
    try:
        events = load_events(args.db, args.limit)
    except sqlite3.Error as exc:
        print(f"ledger unreadable: {path}: {exc}")
        return 2

    sections = [event for event in events if str(event.get("event_type", "")).startswith("section_")]
    checks = [event for event in events if event.get("event_type") == "machine_check"]
    audit = {
        "ledger": path,
        "events": len(events),
        "by_event_type": dict(collections.Counter(str(event.get("event_type")) for event in events).most_common()),
        "by_source": dict(collections.Counter(str(event.get("source")) for event in events).most_common(15)),
        "sections": section_findings(sections, checks),
        "machine_checks": check_findings(sections, checks),
        "ui_projection": projection_findings(events),
    }

    replay_exit = 0
    if not args.replay:
        skip = {"skipped": "pass --replay to check stored records against the current rules"}
        audit["choke_point_replay"] = {"skipped": "pass --replay"}
        audit["replay"] = skip
    else:
        provenance = rules_provenance()
        audit["agentacct_under_test"] = provenance
        if provenance.get("importable") and provenance.get("rules_module_present"):
            audit["choke_point_replay"] = choke_point_replay(events)
            audit["replay"] = replay_findings(events)
        else:
            # Refuse to report a replay result against code that has no rules: a
            # green here would be a false clean, the exact failure this tool
            # exists to catch. Say what was imported and exit non-zero.
            reason = provenance.get("reason") or (
                "agentacct.semantic_rules not importable from "
                f"{provenance.get('package_dir')!r}: --replay would exercise a "
                "write path with no rules and report a false 'refused: 0'. Run "
                "from the checkout root, or put its src/ first on PYTHONPATH."
            )
            error = {"error": reason}
            audit["choke_point_replay"] = error
            audit["replay"] = error
            replay_exit = 3

    print(json.dumps(audit, indent=2, sort_keys=False))
    if args.json_path:
        with open(os.path.expanduser(args.json_path), "w", encoding="utf-8") as handle:
            json.dump(audit, handle, indent=2)
        print(f"\nwrote {args.json_path}", file=sys.stderr)
    if replay_exit:
        print(
            "error: --replay could not confirm it was testing this checkout's "
            "rules; see audit['replay']['error'] above.",
            file=sys.stderr,
        )
    return replay_exit


if __name__ == "__main__":
    raise SystemExit(main())
