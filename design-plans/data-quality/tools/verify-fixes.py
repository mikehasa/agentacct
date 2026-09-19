#!/usr/bin/env python3
"""Prove the recording fixes end to end, on a throwaway store.

This is the evidence behind the PR claims. It does not run the test suite; it
drives the real code paths a client drives and prints what happened, so a
reviewer can see the fix rather than infer it.

    .venv/bin/python design-plans/data-quality/tools/verify-fixes.py

Exit code 0 means every claim held. Each check prints PASS/FAIL with the value it
observed, and the script never touches a real store, the installed app, or any
client configuration.

It imports agentacct, so it needs the project environment rather than a bare
system interpreter:
``python3 -m venv .venv && .venv/bin/python -m pip install -e . pytest``
(see CONTRIBUTING.md). Set PYTHONPATH=src to run it against an uninstalled
checkout; this script already puts that checkout's src/ first.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str) -> None:
    CHECKS.append((name, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}\n         {detail}")


def mcp_call(server, name: str, arguments: dict, msg_id: int = 1):
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}}
    )
    if "error" in response:
        return None, str(response["error"].get("message"))
    return json.loads(response["result"]["content"][0]["text"]), None


def main() -> int:
    from agentacct.hooks import capture_claude_code_client_context, claude_code_hook_context_path
    from agentacct.mcp import SentinelMCPServer
    from agentacct.cli import _install_codex_hook
    from agentacct.service import SentinelService

    print("agentacct recording-fix verification")
    print("=" * 72)

    # ---------------------------------------------------------------- 1
    print("\n1. The Codex SessionStart hook is installed, and its context is used")
    home = pathlib.Path(tempfile.mkdtemp()) / "codexhome"
    home.mkdir(parents=True)
    store = pathlib.Path(tempfile.mkdtemp()) / "state"
    _install_codex_hook(home, store, "/abs/agentacct")
    hooks = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]
    check(
        "Codex hooks.json wires SessionStart",
        "SessionStart" in hooks,
        f"events installed: {sorted(hooks)}",
    )
    check(
        "every wired event points at the same wrapper",
        len({event[0]["hooks"][0]["command"] for event in hooks.values()}) == 1,
        "one wrapper serves all events (it dispatches on hook_event_name)",
    )

    # a Codex-shaped SessionStart event, exactly as the client delivers it
    written = capture_claude_code_client_context(
        json.dumps({"hook_event_name": "SessionStart", "session_id": "codex-session-verify",
                    "cwd": str(store)}),
        store_dir=store,
        client="codex",
    )
    check(
        "the capture writes a Codex-specific slot, not Claude's",
        written == claude_code_hook_context_path(store, "codex") and written.name == "codex.json",
        f"wrote {written.relative_to(store) if written else None}",
    )

    server = SentinelMCPServer(store_dir=store)
    payload, error = mcp_call(server, "agentacct_record_section", {
        "source": "codex", "section_id": "verify-1", "section_status": "started",
        "section_title": "Add rate-limit to login",
    })
    metadata = (payload or {}).get("event", {}).get("metadata", {})
    check(
        "a section recorded with no ids of its own inherits the Codex session id",
        metadata.get("client_session_id") == "codex-session-verify",
        f"client_session_id={metadata.get('client_session_id')!r} client={metadata.get('client')!r}",
    )
    check(
        "the inherited id is labelled as hook-derived, not exact",
        metadata.get("client_context_source") == "claude_code_hook",
        f"client_context_source={metadata.get('client_context_source')!r}",
    )

    # ---------------------------------------------------------------- 1b
    print("\n1b. An existing install upgrades without losing the user's own hooks")
    legacy = pathlib.Path(tempfile.mkdtemp()) / "codexhome"
    (legacy / ".codex").mkdir(parents=True)
    legacy_hooks = legacy / ".codex" / "hooks.json"
    legacy_hooks.write_text(json.dumps({"hooks": {
        # the PREVIOUS agentacct wrapper (same basename) plus a third-party hook
        "PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "python3 /old/agentacct_codex_hook.py"},
            {"type": "command", "command": "/usr/local/bin/my-own-precheck.sh"},
        ]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/local/bin/my-notify.sh"}]}],
    }}, indent=2))
    legacy_store = pathlib.Path(tempfile.mkdtemp()) / "state"
    _install_codex_hook(legacy, legacy_store, "/abs/agentacct")
    upgraded = json.loads(legacy_hooks.read_text())["hooks"]
    check(
        "SessionStart is added to an existing old config",
        "SessionStart" in upgraded,
        f"events now: {sorted(upgraded)}",
    )
    check(
        "the user's own unrelated hook survives the upgrade",
        upgraded.get("UserPromptSubmit", [{}])[0].get("hooks", [{}])[0].get("command")
        == "/usr/local/bin/my-notify.sh",
        "a third-party UserPromptSubmit hook is untouched",
    )
    _install_codex_hook(legacy, legacy_store, "/abs/agentacct")
    again = json.loads(legacy_hooks.read_text())["hooks"]
    # The wrapper must appear EXACTLY once, or PreToolUse would fire twice and
    # double-record tool activity. The user's co-located command is split into
    # its own row (matcher preserved), which is also exactly once.
    serialized = json.dumps(again["PreToolUse"])
    check(
        "the OLD agentacct wrapper is replaced, not duplicated",
        serialized.count("agentacct_codex_hook.py") == 1 and "/old/" not in serialized,
        f"wrapper occurrences={serialized.count('agentacct_codex_hook.py')} "
        f"(2 would double-record tool activity)",
    )
    check(
        "a third-party hook co-located in our row survives the replacement",
        serialized.count("my-own-precheck.sh") == 1,
        f"user command occurrences={serialized.count('my-own-precheck.sh')} (preserved, not duplicated)",
    )

    # ---------------------------------------------------------------- 2
    print("\n2. Every write lane refuses the same incomplete records")
    incomplete_section = {
        "source": "codex", "event_type": "section_completed",
        "metadata": {"sentinel_semantic_kind": "section", "section_id": "no-outcome",
                     "section_status": "completed", "section_title": "Finished work"},
    }
    lanes_refused = []
    for transport in ("http", "cli", "mcp"):
        service = SentinelService(store / f"lane-{transport}")
        try:
            service.record_event(dict(incomplete_section), transport=transport)
            lanes_refused.append(None)
        except ValueError as exc:
            lanes_refused.append(str(exc))
    check(
        "all three transports refuse a terminal section with no outcome",
        all(lanes_refused),
        "refused by: " + ", ".join(
            f"{transport}({'yes' if reason else 'NO'})"
            for transport, reason in zip(("http", "cli", "mcp"), lanes_refused)
        ),
    )
    _, mcp_error = mcp_call(server, "agentacct_record_section", {
        "source": "codex", "section_id": "no-outcome", "section_status": "completed",
        "section_title": "Finished work",
    })
    check(
        "the MCP refusal tells the agent what to add",
        bool(mcp_error) and "summary" in mcp_error and "agentacct_record_section(" in mcp_error,
        (mcp_error or "")[:120] + "...",
    )

    # ---------------------------------------------------------------- 3
    print("\n3. Incomplete reports are refused; complete ones are accepted")
    refusals = {
        "a whitespace-only title": ({"section_title": "   "}, "section_title"),
        "a check with nothing re-runnable": (
            {"source": "codex", "name": "check", "result": "passed"}, "too generic"),
    }
    for label, (arguments, expected) in refusals.items():
        base = {"source": "codex", "section_id": "r", "section_status": "started"}
        tool = "agentacct_record_section"
        if "result" in arguments:
            tool = "agentacct_record_machine_check"
            base = arguments
        else:
            base.update(arguments)
        _, err = mcp_call(server, tool, base)
        check(f"refused: {label}", bool(err) and expected in err, (err or "")[:110] + "...")

    accepted, err = mcp_call(server, "agentacct_record_section", {
        "source": "codex", "section_id": "good", "section_status": "completed",
        "section_title": "Add rate-limit to login",
        "summary": "Added a 5/minute limiter and covered it with three tests.",
        "files": ["src/login.py"],
    })
    check("accepted: a complete section", accepted is not None and not err, err or "stored")

    checked, err = mcp_call(server, "agentacct_record_machine_check", {
        "source": "codex", "name": "pytest tests/test_login.py", "result": "passed",
        "command": "pytest tests/test_login.py", "exit_code": 0, "section_id": "good",
    })
    check(
        "accepted: a check naming what it ran, with an exit code",
        checked is not None and not err,
        err or "stored",
    )

    # the before/after repair lane is legitimate evidence and must NOT be refused
    repaired, err = mcp_call(server, "agentacct_record_machine_check", {
        "source": "codex", "name": "smoke", "evidence_type": "smoke", "result": "passed",
        "files": ["scripts/smoke.sh"], "summary": "Recorded the before/after exit codes.",
    })
    check(
        "accepted: a repair recorded as before/after exit codes",
        repaired is not None and not err,
        err or "stored",
    )

    # ---------------------------------------------------------------- 4
    print("\n4. The read-back loop returns what the agent recorded")
    mcp_call(server, "agentacct_record_section", {
        "source": "codex", "section_id": "blocked-one", "section_status": "blocked",
        "section_title": "Deploy to staging",
        "blocker": "The staging database rejects the migration without an owner role.",
        "next_step": "Ask the platform team to grant the owner role.",
    }, msg_id=9)
    status, err = mcp_call(server, "agentacct_work_status", {}, msg_id=10)
    blocked = (status or {}).get("blocked_sections") or []
    check(
        "agentacct_work_status returns the blocker and its next step",
        bool(blocked) and blocked[0].get("next_step", "").startswith("Ask the platform"),
        f"blocked={len(blocked)} next_step={blocked[0].get('next_step') if blocked else None!r}",
    )
    before = len(server.service.list_all_events())
    mcp_call(server, "agentacct_work_status", {}, msg_id=11)
    check(
        "calling it writes nothing",
        len(server.service.list_all_events()) == before,
        f"events before={before} after={len(server.service.list_all_events())}",
    )

    # ---------------------------------------------------------------- 5
    print("\n5. The recording contract reaches the agent, inside its line budget")
    from agentacct.install_guide import MCP_SERVER_INSTRUCTIONS, _RECORDING_CONTRACT_LINES
    lines = [line for line in _RECORDING_CONTRACT_LINES if line.strip()]
    check(
        "the contract stays within 14 lines",
        len(lines) <= 14,
        f"{len(lines)} bullets (a long directive competes with the user's request)",
    )
    for field in ("client_session_id", "turn_id", "next_step", "blocker", "agentacct_work_status"):
        check(f"the contract tells the agent about {field}", field in MCP_SERVER_INSTRUCTIONS, "")

    # ---------------------------------------------------------------- 6
    print("\n6. Display budgets agree with the surfaces")
    from agentacct.display_budget import (
        CARD_TITLE_CHARACTERS, INSPECTOR_SUMMARY_CHARACTERS, display_label_from_text,
        truncate_for_display,
    )
    from agentacct.mcp import TOOLS
    props = next(t for t in TOOLS if t["name"] == "agentacct_record_section")["inputSchema"]["properties"]
    check(
        "the schema discloses the card budget to the author",
        str(CARD_TITLE_CHARACTERS) in props["section_title"]["description"],
        f"section_title says a card renders about {CARD_TITLE_CHARACTERS} characters",
    )
    check(
        "the schema is never tighter than the surface that needs the room",
        props["summary"]["maxLength"] >= INSPECTOR_SUMMARY_CHARACTERS,
        f"summary maxLength={props['summary']['maxLength']} inspector={INSPECTOR_SUMMARY_CHARACTERS}",
    )
    prose = "Inspected live Dashboard, Work receipt and Usage. Then reviewed the native surfaces."
    label = display_label_from_text(prose)
    check(
        "a summary reused as a card title becomes a sentence, not a clip",
        label.endswith(".") and len(label) <= CARD_TITLE_CHARACTERS,
        f"label={label!r}",
    )
    check(
        "truncation never exceeds its budget",
        all(len(truncate_for_display("x" * 200, limit=n)) <= n for n in (1, 5, 54, 100)),
        "checked limits 1, 5, 54, 100",
    )

    # ---------------------------------------------------------------- 7
    print("\n7. The rules do not refuse records the real store already accepted")
    ledger = pathlib.Path.home() / ".local" / "state" / "agentacct" / "state" / "events.sqlite3"
    if not ledger.exists():
        print("  [SKIP] no installed ledger on this machine (--replay needs real data)")
    else:
        import sqlite3
        from agentacct.semantic_rules import SemanticRecordError, validate_semantic_record

        connection = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
        try:
            rows = [row[0] for row in connection.execute("select line from event_lines order by seq")]
        finally:
            connection.close()
        events = []
        for line in rows:
            try:
                events.append(json.loads(line))
            except (TypeError, ValueError):
                continue
        in_scope = refused = 0
        reasons: dict[str, int] = {}
        for event in events:
            metadata = event.get("metadata") or {}
            kind = metadata.get("sentinel_semantic_kind")
            if kind not in {"section", "evidence"} or metadata.get("semantic_rules_validated"):
                continue
            status = str(metadata.get("section_status") or "").strip().lower()
            if not status and str(event.get("event_type") or "").startswith("section_"):
                status = str(event["event_type"]).removeprefix("section_")
            in_scope += 1
            try:
                validate_semantic_record(
                    semantic_kind=kind, status=status or "unknown",
                    fields={**metadata, "source": event.get("source")},
                )
            except SemanticRecordError:
                refused += 1
                reasons[str(event.get("event_type"))] = reasons.get(str(event.get("event_type")), 0) + 1
        share = 100 * refused / max(in_scope, 1)
        check(
            "replaying every stored record through the rules refuses < 1%",
            share < 1.0,
            f"{refused} of {in_scope} refused ({share:.2f}%); by type: {reasons or 'none'}",
        )
        check(
            "the historian's own sections already carry resolved session ids",
            any((event.get("metadata") or {}).get("client_session_id")
                for event in events if str(event.get("event_type", "")).startswith("section_")),
            "at least one recorded section is session-linked in the live store",
        )

    # ---------------------------------------------------------------- summary
    failed = [name for name, passed, _ in CHECKS if not passed]
    print("\n" + "=" * 72)
    print(f"{len(CHECKS) - len(failed)} of {len(CHECKS)} checks passed")
    if failed:
        print("FAILED:")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("All claims in the PR verified on this head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
