#!/usr/bin/env python3
"""Demonstrate the recording loop, then try to falsify every claim it makes.

Two audiences, two halves.

**Parts 1-3 are the showcase.** They drive the real code paths a client drives —
the MCP tools, the service, the CLI receipt renderer — on throwaway stores, and
print the exact text a user or an agent sees at each step: the refusal an agent
gets and can fix in one retry, what the store then holds, what the card and the
receipt render. Each step is an assertion; a wrong expectation exits non-zero.

**Part 4 is the falsification run.** It goes looking for what is still wrong and
prints what it finds, live, with the counts. Today it finds five defects. If one
of them stops reproducing, that is good news and the docs are stale — the line
says so instead of quietly passing.

So this file is not a certificate of perfection. It is a search for problems
that comes back with a list. Run it and read Part 4 before believing anything
about Part 1-3.

    .venv/bin/python design-plans/data-quality/tools/demo-data-quality.py

Needs the project environment (it imports agentacct), touches no real store and
writes nothing outside a temporary directory. The read-only audit in Part 4 opens
the installed ledger through a ``mode=ro`` URI and prints aggregate numbers only.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import sqlite3
import sys
import tempfile
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_STORE = "~/.local/state/agentacct/state/events.sqlite3"

# Scoring: `check` is an assertion about behaviour and fails the run; `observe`
# records what the falsification run found and never fails it.
FAILURES: list[str] = []
OBSERVATIONS: list[str] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    print(f"   [{'PASS' if passed else 'FAIL'}] {name}")
    if detail:
        print(f"          {detail}")
    if not passed:
        FAILURES.append(name)


def observe(name: str, found: bool, detail: str) -> None:
    marker = "CONFIRMED" if found else "GONE"
    print(f"   [{marker}] {name}")
    print(f"          {detail}")
    if not found:
        OBSERVATIONS.append(name)


def banner(text: str) -> None:
    print(f"\n{text}\n" + "-" * 72)


def show(label: str, text: str, *, indent: str = "          ") -> None:
    """Print the exact user-visible text, wrapped, with its source named."""
    print(f"{indent}{label}")
    for line in str(text).splitlines() or [""]:
        print(f"{indent}  | {line}")


# --- client drivers ----------------------------------------------------------


def mcp_call(server: Any, name: str, arguments: dict[str, Any], msg_id: int = 1) -> tuple[dict[str, Any] | None, str | None]:
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    if "error" in response:
        return None, str(response["error"].get("message"))
    return json.loads(response["result"]["content"][0]["text"]), None


def section_event(**fields: Any) -> dict[str, Any]:
    metadata = {
        "sentinel_semantic_kind": "section",
        "client": "codex",
        "client_session_id": "demo-session",
        "project_dir": "/tmp/demo-project",
    }
    metadata.update(fields)
    return {
        "event_id": f"evt_demo_{fields.get('section_id', 'x')}_{fields.get('section_status', 'x')}",
        "created_at": 1000.0,
        "source": str(fields.get("source") or "codex"),
        "event_type": f"section_{fields.get('section_status', 'started')}",
        "run_id": None,
        "metadata": metadata,
    }


def load_events(db_path: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Read the installed ledger without ever opening it for writing."""
    uri = f"file:{os.path.expanduser(db_path)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = [row[0] for row in connection.execute("select line from event_lines order by seq")]
    finally:
        connection.close()
    if limit:
        rows = rows[-limit:]
    events: list[dict[str, Any]] = []
    for line in rows:
        try:
            parsed = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


# --- part 1: one Codex task --------------------------------------------------


def part_one(root: pathlib.Path) -> None:
    from agentacct.hooks import capture_claude_code_client_context, claude_code_hook_context_path
    from agentacct.mcp import SentinelMCPServer

    banner("PART 1 — one Codex task, from a refused report to a record the UI can render")
    home = root / "codexhome"
    home.mkdir(parents=True)
    store = root / "task"
    server = SentinelMCPServer(store_dir=store)

    # 1.1 — the session identity the whole join depends on.
    written = capture_claude_code_client_context(
        json.dumps({"hook_event_name": "SessionStart", "session_id": "demo-codex-session", "cwd": str(root)}),
        store_dir=store,
        client="codex",
    )
    check(
        "1.1 a Codex SessionStart writes a Codex-specific context slot",
        written == claude_code_hook_context_path(store, "codex") and written is not None and written.name == "codex.json",
        f"wrote {written.relative_to(store) if written else None}",
    )

    started, error = mcp_call(
        server,
        "agentacct_record_section",
        {"source": "codex", "section_id": "rate-limit", "section_status": "started", "section_title": "Add rate-limit to login"},
    )
    check(
        "1.2 the opening report needs no other field",
        error is None and (started or {}).get("event", {}).get("metadata", {}).get("section_status") == "started",
        f"refusal: {error}" if error else "accepted with section_title and section_status alone",
    )

    # 1.3 — the agent finishes the work and reports it with no outcome at all.
    _payload, refusal = mcp_call(
        server,
        "agentacct_record_section",
        {"source": "codex", "section_id": "rate-limit", "section_status": "completed", "section_title": "Add rate-limit to login"},
    )
    check(
        "1.3 a completed section with no summary is refused, and the refusal is actionable",
        refusal is not None
        and "summary" in refusal
        and "agentacct_record_section(" in refusal
        and 'section_id="rate-limit"' in refusal,
        "this is the whole text the agent receives:",
    )
    if refusal:
        show("refusal:", refusal)

    # 1.4 — the agent retries with whitespace where the outcome should be.
    _payload, blank = mcp_call(
        server,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "rate-limit",
            "section_status": "completed",
            "section_title": "Add rate-limit to login",
            "summary": " \n ",
            "files": ["src/auth/login.py"],
        },
    )
    check(
        "1.4 a summary that is only whitespace is refused like a missing one",
        blank is not None and "requires `summary`" in blank,
        f"presence is judged on the text as it would be stored: {blank.splitlines()[0][:120] if blank else ''}",
    )

    # 1.5 — a summary that says nothing about the outcome. This is accepted,
    # and that limit belongs in the demo rather than in a footnote.
    _payload, prose = mcp_call(
        server,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "rate-limit",
            "section_status": "completed",
            "section_title": "Add rate-limit to login",
            "summary": "Inspected the login flow, the redirects and the related tests.",
            "files": ["src/auth/login.py"],
        },
    )
    check(
        "1.5 process prose is ACCEPTED — the rule requires prose, not usefulness",
        prose is None,
        "this is a known limit, not a bug in the demo: see Part 4.3 for how much of the store reads this way",
    )

    # 1.6 — the same call, with an outcome sentence.
    stored, error = mcp_call(
        server,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "rate-limit",
            "section_status": "completed",
            "section_title": "Add rate-limit to login",
            "summary": "Added a token-bucket limiter to the login endpoint and covered it with three tests.",
            "next_step": "Watch the first production hour for 429s.",
            "files": ["src/auth/login.py"],
        },
    )
    metadata = (stored or {}).get("event", {}).get("metadata", {})
    check(
        "1.6 the corrected report is accepted and carries the session that paid for it",
        error is None
        and metadata.get("client_session_id") == "demo-codex-session"
        and metadata.get("client_context_source") == "claude_code_hook",
        f"client_session_id={metadata.get('client_session_id')!r} client={metadata.get('client')!r} "
        f"context_source={metadata.get('client_context_source')!r}",
    )

    from agentacct.display_budget import CARD_TITLE_CHARACTERS, truncate_for_display

    title = metadata.get("section_title") or ""
    summary = metadata.get("summary") or ""
    check(
        "1.7 what the user sees: the card label fits the card, and the summary survives whole",
        len(truncate_for_display(title, limit=CARD_TITLE_CHARACTERS)) <= CARD_TITLE_CHARACTERS
        and summary == "Added a token-bucket limiter to the login endpoint and covered it with three tests.",
        f"card shows {truncate_for_display(title, limit=CARD_TITLE_CHARACTERS)!r} "
        f"({len(title)} stored characters); inspector gets all {len(summary)} summary characters",
    )


# --- part 2: the evidence lane ----------------------------------------------


def part_two(root: pathlib.Path) -> None:
    from agentacct.mcp import SentinelMCPServer

    banner("PART 2 — the same discipline for machine checks")
    server = SentinelMCPServer(store_dir=root / "checks")

    def record(**arguments: Any) -> str | None:
        defaults: dict[str, Any] = {"source": "codex", "result": "passed", "evidence_type": "test"}
        defaults.update(arguments)
        _payload, error = mcp_call(server, "agentacct_record_machine_check", defaults)
        return error

    unnamed = record(name=None, command=None, files=[], exit_code=None)
    check(
        "2.1 a check with no name and no pointer is refused, and says what to send instead",
        unnamed is not None
        and "records no `name`" in unnamed
        and 'name="percentage() rounds half-up"' in unnamed
        and 'command="python -m pytest tests/test_percent.py"' in unnamed,
        "the example shows a LABEL beside a command; a command IN the name field is what produced two "
        "byte-identical cards in the real ledger",
    )

    unanchored = record(name="login smoke test", command=None, files=[], exit_code=None)
    check(
        "2.2 a name with no pointer and no exit code is refused",
        unanchored is not None and "records nothing a reviewer can re-run" in unanchored,
        "and it names the honest alternative for a manual observation",
    )

    tolerated = record(name="login smoke test", exit_code=0)
    check(
        "2.3 the tolerated shape — a name plus an exit code — is accepted",
        tolerated is None,
        "11 of the 336 checks in the real ledger have exactly this shape; refusing them would discard real evidence",
    )

    root_only = record(name="root files check", command=None, files=["."], exit_code=None)
    check(
        "2.4 files: [\".\"] does not satisfy reproducibility",
        root_only is not None and "command" in root_only,
        "it survives the schema but stores nothing a reviewer can open, so it cannot be the evidence a check rests on",
    )

    blank_repair = record(name="check", before_summary="", after_summary="", before_exit_code=1, after_exit_code=0)
    check(
        "2.5 blank before/after summaries are not evidence, so the repair lane is held to the rule too",
        blank_repair is not None,
        "an empty string is the shape a client produces when it means 'nothing here'",
    )

    run_scoped = record(
        name="check",
        before_summary="2 failed in the rate-limit suite",
        after_summary="3 passed in the rate-limit suite",
        before_exit_code=1,
        after_exit_code=0,
    )
    check(
        "2.6 the before/after lane is run-scoped: with no run in the store it says so",
        run_scoped is not None and "no runs found" in run_scoped,
        f"refusal: {run_scoped!r} — a lane requirement, not a rule about the text",
    )

    from typer.testing import CliRunner

    from agentacct.cli import app

    guarded = CliRunner().invoke(app, ["run", "--store-dir", str(root / "checks"), "--", "true"])
    repaired = record(
        name="check",
        before_summary="2 failed in the rate-limit suite",
        after_summary="3 passed in the rate-limit suite",
        before_exit_code=1,
        after_exit_code=0,
    )
    check(
        "2.7 once a run exists, a repair is accepted with the default name and no command",
        guarded.exit_code == 0 and repaired is None,
        "the two summaries and their exit codes ARE the evidence, so the default name is not carrying it",
    )


# --- part 3: try to bypass it ------------------------------------------------


def part_three(root: pathlib.Path) -> None:
    from agentacct.mcp import SentinelMCPServer
    from agentacct.service import SentinelService

    banner("PART 3 — adversarial: every lane, hostile text, and the false-positive guard")

    incomplete = section_event(section_id="no-outcome", section_status="completed", section_title="Finished work")
    refusals: dict[str, str | None] = {}
    for transport in ("http", "cli", "mcp"):
        service = SentinelService(root / f"lane-{transport}")
        try:
            service.record_event(dict(incomplete), transport=transport)
            refusals[transport] = None
        except ValueError as exc:
            refusals[transport] = str(exc)
    check(
        "3.1 all three write lanes refuse the same incomplete record",
        all(refusals.values()),
        "refused by: " + ", ".join(sorted(refusals)) + " — no lane is a way around the rules",
    )
    check(
        "3.2 the refusal is identical on every lane, keyed on the section id",
        len({text.split("Received:")[0] for text in refusals.values() if text}) == 1,
        "one message to learn, whichever transport the agent happens to use",
    )

    server = SentinelMCPServer(store_dir=root / "hostile")
    calls = {
        "control characters and an ANSI escape": {"section_title": "Fix\x1b[31m UI\x07 spacing"},
        "a right-to-left override": {"section_title": "Add\u202erate limit to \u202blogin"},
        "emoji and a 4-byte character": {"section_title": "Add \U0001F680 rate-limit \U0001F512"},
        "160 characters padded with whitespace": {"section_title": "  " + ("a" * 79) + "\t \n" + ("b" * 79) + "  "},
        "200 characters": {"section_title": "a" * 200},
    }
    outcomes: list[str] = []
    for label, override in calls.items():
        arguments = {"source": "codex", "section_id": "hostile", "section_status": "started"}
        arguments.update(override)
        stored, refusal = mcp_call(server, "agentacct_record_section", arguments)
        stored_title = ""
        if refusal is None and isinstance(stored, dict):
            stored_title = str(stored["event"]["metadata"].get("section_title") or "")
        clean = not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", stored_title)
        outcomes.append(
            f"{label}: {'refused' if refusal else f'stored {len(stored_title)} chars, control-free={clean}'}"
        )
        check(
            f"3.3 {label} never crashes and never stores a control character",
            clean and len(stored_title) <= 160,
            outcomes[-1],
        )

    # The guard that matters most: unusual but legitimate work must still record.
    legitimate, error = mcp_call(
        server,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "legitimate",
            "section_status": "completed",
            "section_title": "Réduire la latence du panier (checkout)",
            "summary": (
                "Cut checkout latency from 840 ms to 260 ms by batching the inventory lookups.\n\n"
                "- 3 requests instead of 14\n- covered by tests/test_checkout.py"
            ),
            "files": ["src/checkout/basket.py"],
            "kind": "implementation",
        },
    )
    check(
        "3.4 a legitimate, unusual record is accepted — unicode title, multiline summary, real files",
        error is None and (legitimate or {}).get("event", {}).get("metadata", {}).get("summary", "").count("\n") == 3,
        "the line structure a reader needs is preserved, not flattened",
    )

    service = SentinelService(root / "machine")
    exempt = True
    refusal_text = ""
    try:
        service.record_event(
            {
                "event_id": "evt_demo_usage",
                "created_at": 1000.0,
                "source": "claude-code-local-session-import",
                "event_type": "model_usage",
                "run_id": None,
                "provider": "claude-code",
                "model": "claude-opus-4-8",
                "estimated_input_tokens": 10,
                "estimated_output_tokens": 5,
                "estimated_cost_usd": 0.5,
                "usage_confidence": "client_reported",
                "cost_confidence": "estimated_from_tokens",
                "cost_basis": "pricing_table",
                "metadata": {},
            },
            trusted_usage_import=True,
            transport="http",
        )
    except ValueError as exc:
        exempt, refusal_text = False, str(exc)
    check(
        "3.5 machine-recorded facts are exempt — a refusal would drop a fact, not correct a report",
        exempt,
        "imported usage with no summary records normally" if exempt else f"refused: {refusal_text}",
    )


# --- part 4: the falsification run ------------------------------------------


_CHANGE_VERBS = (
    "added", "created", "implemented", "fixed", "removed", "renamed", "moved", "wired", "saved",
    "wrote", "replaced", "extracted", "introduced", "updated", "switched", "deleted", "enabled",
    "split", "merged", "cut", "lowered", "raised", "dropped",
)
_PROCESS_VERBS = (
    "inspected", "reviewed", "read", "checked", "verified", "ran", "looked", "examined", "scanned",
    "audited", "tested", "searched", "grepped", "listed", "confirmed", "investigated", "walked",
)
_STATUS_PREFIX = ("completed", "done", "finished", "worked on", "progress")


def classify_first_sentence(summary: str) -> str:
    """Which of the four things a first sentence can do. One definition, printed
    with its result, so a reader can disagree with the classifier rather than
    with a number that came from nowhere."""
    first = summary.strip().split("\n", 1)[0].strip()
    for terminator in (". ", "! ", "? "):
        index = first.find(terminator)
        if index > 0:
            first = first[: index + 1]
            break
    lowered = first.lower().lstrip("-*# ").strip()
    words = re.findall(r"[a-z]+", lowered)
    if not words:
        return "other"
    if lowered.startswith(_STATUS_PREFIX) or words[0] in {"completed", "done", "finished"}:
        return "status"
    # A change verb anywhere in the first sentence counts: "The redirect now
    # saves the return path" states an outcome without leading with the verb.
    if any(word in _CHANGE_VERBS for word in words):
        return "change"
    if words[0] in _PROCESS_VERBS or any(word in _PROCESS_VERBS for word in words[:2]):
        return "process"
    return "other"


def _work_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The projection the UI renders. Pure computation over the events the demo
    already read; it never touches a store."""
    try:
        from agentacct.work_ledger import build_work_ledger

        return [item for item in build_work_ledger(events).get("work_items", []) if isinstance(item, dict)]
    except Exception as exc:  # noqa: BLE001 - the demo reports, it does not crash
        print(f"   [note] work ledger not built: {type(exc).__name__}: {exc}")
        return []


def _real_receipt_count_forms(db_path: str) -> str:
    """Render one real Task's receipt from a COPY of the ledger, and report the
    counting strings it contains. The copy keeps the installed store read-only."""
    import shutil

    source = pathlib.Path(os.path.expanduser(db_path))
    if not source.exists():
        return ""
    staging = pathlib.Path(tempfile.mkdtemp()) / "store-copy"
    staging.mkdir(parents=True)
    shutil.copy2(source, staging / source.name)
    for sidecar in ("events.authoritative", "events.jsonl"):
        candidate = source.parent / sidecar
        if candidate.exists():
            shutil.copy2(candidate, staging / sidecar)
    try:
        from typer.testing import CliRunner

        from agentacct.cli import app

        runner = CliRunner()
        listed = json.loads(runner.invoke(app, ["receipts", "--json", "--store-dir", str(staging)]).output)
        tasks = listed.get("tasks") or []
        if not tasks:
            return ""
        markdown = runner.invoke(
            app, ["receipt", str(tasks[0]["task_id"]), "--markdown", "--store-dir", str(staging)]
        ).output
    except Exception:  # noqa: BLE001 - the demo reports, it does not crash
        return ""
    return ", ".join(
        sorted(set(re.findall(r"\b\d+ checks\b|\b\d+ file\(s\)|\b\d+ command\(s\)|\b\d+ step\(s\)", markdown)))
    )


def part_four(db_path: str) -> None:
    banner("PART 4 — the falsification run: what is still wrong")
    print("   These are not assertions. Each line reports what the code does today.")

    events = load_events(db_path)
    if not events:
        print(f"   no ledger at {db_path} — run with --db PATH to point at one")
        return

    # 4.1 — counting copy, reproduced through the real renderer and then in the
    # wild. The renderer is a pure function of the payload, so a one-check payload
    # isolates the defect instead of depending on which task happens to have one.
    from agentacct.receipt_markdown import render_receipt_markdown

    one_of_each = {
        "title": "One of each",
        "task_id": "task_demo",
        "axes": {
            "decision_status": {"key": "reported", "asserted_by": "agent_report", "statement": "s"},
            "evidence_strength": {
                "gradeable": True,
                "checks_total": 1,
                "checks_passed": 1,
                "checks_failed": 0,
                "definition": "Counts are passing checks over checkable steps.",
            },
            "handoff": {"handed_off": False},
            "orthogonality_note": "note",
        },
        "dimensions": {
            "task": {"objectives": ["a"], "boundary": {}, "provenance": ["mcp"]},
            "actors": {"provenance": ["client_log"]},
            "actions": {
                "tool_category_counts": {"execute": 1},
                "touched_file_count": 1,
                "command_count": 1,
                "tool_names_preview": [],
                "provenance": ["hook"],
            },
            "cost": {"provenance": ["none"]},
            "evidence": {"checks_total": 1, "checks_passed": 1, "checks_failed": 0, "provenance": ["mcp"]},
            "outcome": {"decision_status": "reported", "asserted_by": "agent_report", "provenance": ["mcp"]},
            "gaps": {"items": [], "count": 0},
            "provenance": {"legend": {"mcp": "desc"}},
        },
        "timeline": {"events": [], "shown": 0, "total": 0},
    }
    rendered = render_receipt_markdown(one_of_each)
    tally = re.findall(r"\b\d+ checks\b", rendered)
    s_forms = re.findall(r"\b\d+ file\(s\)|\b\d+ command\(s\)|\b\d+ step\(s\)", rendered)
    observe(
        "4.1 counting copy — a receipt with one check says \"1 checks\"",
        "1 checks" in rendered,
        f"the real renderer produced {tally} and {s_forms}; the nouns never agree with the number",
    )
    live_forms = _real_receipt_count_forms(db_path)
    if live_forms:
        print(f"          in the wild: the same strings on a real Task from the installed store — {live_forms}")

    # 4.2 — the card budget against the real titles. Measured on the work-item
    # projection, because the card renders work items, not raw events; the
    # event-layer count is larger and would overstate what a reader sees.
    from agentacct.display_budget import CARD_TITLE_CHARACTERS, truncate_for_display

    ledger_items = _work_items(events)
    titles = [str(item.get("title") or "") for item in ledger_items]
    titles = [title for title in titles if title.strip()]
    over = [title for title in titles if len(title) > CARD_TITLE_CHARACTERS]
    example = max(over, key=len) if over else ""
    observe(
        f"4.2 card budget — {len(over)} of {len(titles)} real work-item titles exceed the card's "
        f"{CARD_TITLE_CHARACTERS} characters",
        bool(over),
        f"longest is {len(example)} characters and renders as "
        f"{truncate_for_display(example, limit=CARD_TITLE_CHARACTERS)!r} on the card"
        if over
        else "every stored title fits the card",
    )
    print(
        "          the budget is derived from the card's geometry (200 pt, 14 pt semibold, lineLimit(2)), "
        "not from a screen capture"
    )

    # 4.3 — summary quality, with the classifier printed above.
    summaries = [
        str((event.get("metadata") or {}).get("summary") or "")
        for event in events
        if str(event.get("event_type")) in {"section_completed", "section_handed_off", "section_blocked"}
    ]
    summaries = [summary.strip() for summary in summaries if summary.strip()]
    kinds = collections.Counter(classify_first_sentence(summary) for summary in summaries)
    if summaries:
        share = {kind: round(100 * count / len(summaries), 1) for kind, count in kinds.most_common()}
        observe(
            "4.3 summary quality — most summaries still describe process, not outcome",
            share.get("change", 0.0) < 50,
            f"first sentence of {len(summaries)} stored summaries, by this file's classifier: {share}; "
            "the rules require a summary to exist and be readable, never that it be useful",
        )
        print(
            f"          this classifier counts a change verb anywhere in the first sentence, so its "
            f"{share.get('change', 0.0)}% is the generous reading. ASSESSMENT.md section Q2 measures the same "
            "population two ways: 2.9% strict (the verb leads), ~15% with the change verbs the contract asks "
            "for folded in. Either reading leaves outcome-stating summaries in the minority"
        )

    # 4.4 — the join and evidence reality, through the projection the UI renders.
    if ledger_items:
        unjoined = [
            item
            for item in ledger_items
            if not any(
                item.get(key)
                for key in ("linked_usage_records", "priced_usage_records", "usage_total", "estimated_cost_total")
            )
        ]
        blind = [item for item in ledger_items if not item.get("evidence_events")]
        blocked = [item for item in ledger_items if item.get("blocker")]
        observe(
            "4.4 the join — most recorded work still cannot be tied to the usage that paid for it",
            bool(ledger_items) and len(unjoined) / len(ledger_items) > 0.25,
            f"{len(ledger_items)} work items: {len(unjoined)} ({round(100 * len(unjoined) / len(ledger_items), 1)}%) "
            f"carry no usage record, {len(blind)} ({round(100 * len(blind) / len(ledger_items), 1)}%) carry no "
            f"evidence at all, {len(blocked)} carry a blocker",
        )
    else:
        print("   [skip] the work ledger did not build; Part 4.2 and 4.4 need it")

    # 4.5 — the rules do not rewrite history.
    past = [
        event
        for event in events
        if str(event.get("event_type")) in {"section_completed", "section_handed_off"}
        and not str((event.get("metadata") or {}).get("summary") or "").strip()
    ]
    observe(
        "4.5 history — records that today's rules would refuse are still stored and still render",
        bool(past),
        f"{len(past)} stored terminal sections carry no summary and were not repaired: "
        "the rules bind new writes only, which is why the store stays measured separately from the rules",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_STORE, help=f"ledger for the read-only audit (default: {DEFAULT_STORE})")
    parser.add_argument("--skip-store", action="store_true", help="run only Parts 1-3, against temporary stores")
    args = parser.parse_args()

    print("agentacct recording loop — demo and falsification run")
    print("=" * 72)
    root = pathlib.Path(tempfile.mkdtemp()) / "demo"

    part_one(root)
    part_two(root)
    part_three(root)

    if not args.skip_store:
        part_four(args.db)

    print("\n" + "=" * 72)
    print(f"behaviour cases: {len(FAILURES)} failed")
    print(f"falsification run: {len(OBSERVATIONS)} of the five known defects no longer reproduce")
    if OBSERVATIONS:
        for name in OBSERVATIONS:
            print(f"  - {name} — good news; the design record is now stale and should be updated")
    print("\nWhat this demo does NOT claim:")
    print("  - no live Codex session has run through the new hook (see the runbook in the handoff)")
    print("  - the card budget is geometry, not a screen measurement")
    print("  - the rules bind new writes only; Part 4.5 shows stored history is untouched")
    print("  - Part 4 is the honest half: the defects it confirms are still open")
    if FAILURES:
        print("\nFAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("\nParts 1-3 behaved exactly as asserted. Part 4 is why this is not perfect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
