#!/usr/bin/env python3
"""Regenerate the two worked-example docs from real Work Receipts.

The GEO diagnosis asked for two concrete, linkable examples that answer the
broad buyer questions the models stayed generic on:

  - docs/examples/compare-claude-code-and-codex.md — the same task run on two
    agents, side by side, compared by outcome, evidence and cost.
  - docs/examples/when-an-agent-says-done.md — a trajectory (a failed check, a
    re-run, a later edit, an outdated check) with each event labeled by source
    and what agentacct actually captures.

Both docs are GENERATED here from synthetic-but-honest seed data run through the
real receipt engine (``build_receipt`` -> ``render_receipt_markdown``), so the
examples cannot claim anything the product does not actually produce. The data
is invented (clearly labeled), but every number, tier, provenance label and
timeline row is the receipt engine's own output, not hand-written prose.

    PYTHONPATH=src <venv>/python scripts/gen_worked_examples.py

``tests/test_docs_generated.py`` fails CI if a committed doc drifts from this.
The seed uses a FIXED base time and the renderer uses relative timestamps, so
the output is byte-for-byte reproducible.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentacct.api import _task_title, build_store_task_projection  # noqa: E402
from agentacct.receipt import build_receipt, latest_store_activity, session_start_index  # noqa: E402
from agentacct.receipt_markdown import render_receipt_markdown  # noqa: E402
from agentacct.service import SentinelService  # noqa: E402

# A fixed base time so the seeded ledger — and therefore every rendered doc — is
# byte-for-byte reproducible. The receipt engine compares against the store's own
# newest event, never the wall clock, so a fixed base is faithful, not a fudge.
BASE = 1_780_000_000.0
DOCS = REPO_ROOT / "docs" / "examples"


# --- self-contained seed helpers (no dependency on the screenshot generator) --

def _usage(svc, *, client, session, model, project, tokens, cost, ns):
    svc.record_event(
        {
            "event_id": f"evt_usage_{session}",
            "created_at": BASE,
            "source": f"{client}-local-session-import",
            "event_type": "model_usage",
            "run_id": None,
            "provider": client,
            "model": model,
            "estimated_input_tokens": tokens,
            "estimated_output_tokens": tokens // 4,
            "estimated_cost_usd": cost,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "cost_basis": "pricing_table",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": client,
                "client_session_id": session,
                "project_dir": f"/work/{project}",
                "started_at": BASE,
                "updated_at": BASE,
                "session_namespace_fingerprint": ns,
                "identity_scope_state": "explicit",
                "source_namespace_fingerprint": ns,
            },
        },
        trusted_usage_import=True,
    )


def _section(svc, *, client, session, project, ns, section_id, title, status, kind, at, files, summary=""):
    svc.record_event(
        {
            "event_id": f"evt_section_{session}_{section_id}_{status}",
            "created_at": float(at),
            "source": client,
            "event_type": f"section_{status}",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "section",
                "client": client,
                "client_session_id": session,
                "client_context_keys_authored": ["client_session_id"],
                "project_dir": f"/work/{project}",
                "session_namespace_fingerprint": ns,
                "identity_scope_state": "explicit",
                "demo_occurred_at": float(at),
                "section_id": section_id,
                "section_status": status,
                "section_title": title,
                "objective": title,
                "kind": kind,
                "summary": summary,
                "files": files,
            },
        }
    )


def _check(svc, *, client, session, project, ns, section_id, result, at, summary, command, exit_code, name="pytest"):
    # A machine check recorded through the MCP path. The ledger hard-codes such a
    # check's independence to self-checked (only a client hook or real CI raises
    # the tier), so this is an honest agent-recorded check. It must carry the same
    # namespace identity as the work items, or it will not attach to the Task.
    svc.record_event(
        {
            "event_id": f"evt_check_{session}_{section_id}_{result}_{int(at)}",
            "created_at": float(at),
            "source": client,
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": client,
                "client_session_id": session,
                "project_dir": f"/work/{project}",
                "session_namespace_fingerprint": ns,
                "identity_scope_state": "explicit",
                "demo_occurred_at": float(at),
                "section_id": section_id,
                "evidence_type": "test",
                "result": result,
                "name": name,
                "summary": summary,
                "command": command,
                "exit_code": exit_code,
            },
        }
    )


def _tool_activity(svc, *, client, session, at, basis, categories, names, touched, commands):
    svc.record_event(
        {
            "event_id": f"evt_toolact_{session}_{int(at)}",
            "created_at": float(at),
            "source": client,
            "event_type": "tool_activity_observed",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "tool_activity",
                "client": client,
                "client_session_id": session,
                "demo_occurred_at": float(at),
                "capture_basis": basis,
                "captured_at": float(at),
                "tool_category_counts": categories,
                "tool_names": [{"name": n, "count": c} for n, c in names],
                "touched_files": touched,
                "commands": commands,
            },
        }
    )


def _backdate(store: Path) -> None:
    """Rewrite the store's server-stamped created_at to the scripted times, so the
    projection reads the seeded ordering rather than insertion time."""

    import json
    import sqlite3

    con = sqlite3.connect(store / "events.sqlite3")
    for seq, line in con.execute("SELECT seq, line FROM event_lines").fetchall():
        ev = json.loads(line)
        meta = ev.get("metadata") or {}
        at = meta.get("demo_occurred_at") or meta.get("updated_at")
        if not at:
            continue
        ev["created_at"] = float(at)
        con.execute(
            "UPDATE event_lines SET created_at = ?, line = ? WHERE seq = ?",
            (float(at), json.dumps(ev, sort_keys=True), seq),
        )
    con.commit()
    con.close()


def _receipt_for_single_task(store: Path, *, display_task_id: str, title: str, include_timeline: bool) -> str:
    _backdate(store)
    projection = build_store_task_projection(store)
    tasks = [t for t in projection.get("tasks", []) if isinstance(t, dict)]
    all_tasks = list(tasks)
    task = max(tasks, key=lambda t: float(t.get("last_activity_at") or 0.0))
    receipt = build_receipt(
        task,
        public_task_id=str(task.get("public_task_id")),
        title=title or _task_title(task),
        latest_store_activity_at=latest_store_activity(all_tasks),
        session_starts=session_start_index(all_tasks),
    )
    # Replace the content-hashed task id with a stable, readable label: the id is
    # meaningless in a synthetic example, and pinning it keeps the doc reproducible.
    receipt["task_id"] = display_task_id
    return render_receipt_markdown(receipt, include_timeline=include_timeline, heading_level=3)


# --- Example A: same task, two agents -----------------------------------------

_TASK_A = "Add retry-with-backoff to the payments HTTP client"


def _seed_claude_code_a(store: Path) -> None:
    svc = SentinelService(store)
    c, s, p, ns = "claude-code", "cc-retry", "payments-svc", "sha256:ex-a-cc"
    _usage(svc, client=c, session=s, model="claude-opus-4-8", project=p, tokens=1_900_000, cost=27.40, ns=ns)
    steps = [
        ("plan", "Plan the retry policy", "completed", "planning", ["src/payments/client.py"],
         "Chose exponential backoff with full jitter, 5 attempts, 30s cap."),
        ("tests", "Write the retry tests", "completed", "testing", ["tests/test_client.py"],
         "Added 8 cases: transient 503, timeout, giving up, no-retry on 400."),
        ("impl", "Implement backoff + jitter", "completed", "implementation", ["src/payments/client.py"],
         "Implemented the backoff wrapper; the 8 tests pass."),
    ]
    for i, (sid, title, status, kind, files, summary) in enumerate(steps):
        _section(svc, client=c, session=s, project=p, ns=ns, section_id=sid, title=title,
                 status=status, kind=kind, at=BASE + 60 + i * 200, files=files, summary=summary)
    # An agent-recorded test run that postdates the last edit (self-checked).
    _check(svc, client=c, session=s, project=p, ns=ns, section_id="impl", result="passed", at=BASE + 700,
           summary="8 passed", command="pytest tests/test_client.py -q", exit_code=0)
    _tool_activity(svc, client=c, session=s, at=BASE + 650, basis="client_hook_tool_category",
                   categories={"read": 12, "edit": 5, "execute": 6, "search": 3},
                   names=[("Read", 12), ("Edit", 5), ("Bash", 6), ("Grep", 3)],
                   touched=["src/payments/client.py", "tests/test_client.py"],
                   commands=["pytest tests/test_client.py -q", "ruff check src/"])


def _seed_codex_a(store: Path) -> None:
    svc = SentinelService(store)
    c, s, p, ns = "codex", "cx-retry", "payments-svc", "sha256:ex-a-cx"
    _usage(svc, client=c, session=s, model="gpt-5.6-sol", project=p, tokens=1_600_000, cost=3.10, ns=ns)
    steps = [
        ("impl", "Add backoff to the HTTP client", "completed", "implementation", ["src/payments/client.py"],
         "Wrapped the request in an exponential-backoff retry with jitter."),
        ("verify", "Run the payment tests", "completed", "testing", ["tests/test_client.py"],
         "Ran the suite; recorded the result via the MCP check."),
    ]
    for i, (sid, title, status, kind, files, summary) in enumerate(steps):
        _section(svc, client=c, session=s, project=p, ns=ns, section_id=sid, title=title,
                 status=status, kind=kind, at=BASE + 60 + i * 200, files=files, summary=summary)
    # No hook fires for Codex, so the check is agent-recorded through MCP -> self checked.
    _check(svc, client=c, session=s, project=p, ns=ns, section_id="verify", result="passed", at=BASE + 500,
           summary="8 passed", command="pytest tests/test_client.py -q", exit_code=0)
    _tool_activity(svc, client=c, session=s, at=BASE + 450, basis="transcript_scan_tool_activity",
                   categories={"read": 9, "edit": 3, "execute": 4, "search": 2},
                   names=[("read_file", 9), ("apply_patch", 3), ("exec_command", 4), ("grep", 2)],
                   touched=["src/payments/client.py", "tests/test_client.py"],
                   commands=["pytest tests/test_client.py -q"])


def _render_example_a() -> str:
    with tempfile.TemporaryDirectory() as cc_dir, tempfile.TemporaryDirectory() as cx_dir:
        cc_store, cx_store = Path(cc_dir), Path(cx_dir)
        _seed_claude_code_a(cc_store)
        _seed_codex_a(cx_store)
        cc_md = _receipt_for_single_task(cc_store, display_task_id="task_cc_retry", title=_TASK_A, include_timeline=False)
        cx_md = _receipt_for_single_task(cx_store, display_task_id="task_cx_retry", title=_TASK_A, include_timeline=False)

    out: list[str] = []
    out.append("# Compare Claude Code and Codex by task outcome, evidence and cost")
    out.append("")
    out.append("<!-- GENERATED by scripts/gen_worked_examples.py — do not edit by hand. -->")
    out.append("")
    out.append(
        "> The data below is a synthetic demo workspace — invented projects and pricing-table cost "
        "**estimates**, never billed figures. Every number, evidence tier, provenance label and gap is the "
        "real receipt engine's own output; only the underlying work is invented."
    )
    out.append("")
    out.append(
        f"The same task — **\"{_TASK_A}\"**, from the same starting commit — was run once on Claude Code and "
        "once on Codex. agentacct turns each into one Work Receipt. Read them side by side: not by whose "
        "summary sounds more confident, but by what actually happened, how well it is proven, and what it cost."
    )
    out.append("")
    out.append(
        "![The three receipts in the agentacct macOS app's Work table: \"Plan the retry policy\" (Claude Code) "
        "and \"Add backoff to the HTTP client\" (Codex) both read Verified, 1/2 claims supported (self-checked), "
        "1/1 check passed — at an estimated $27.40 and $3.10 respectively — beside \"Reproduce the flaky total\" "
        "(Reported, $18.60).](assets/example-a-receipts-table.png)"
    )
    out.append("")
    out.append("## Claude Code")
    out.append("")
    out.append(
        "![The Claude Code receipt in the agentacct macOS app: \"Plan the retry policy\", decision Verified "
        "(machine checked), 1 of 2 claims supported, evidence self-checked, actions captured via a client hook "
        "and MCP, cost an estimated $27.40, one gap — a completed step with no linked passing "
        "check.](assets/example-a-claude-code-receipt.png)"
    )
    out.append("")
    out.append(cc_md)
    out.append("## Codex")
    out.append("")
    out.append(
        "![The Codex receipt in the agentacct macOS app: \"Add backoff to the HTTP client\", decision Verified "
        "(machine checked), 1 of 2 claims supported, evidence self-checked, actions captured via a transcript "
        "scan and MCP, cost an estimated $3.10.](assets/example-a-codex-receipt.png)"
    )
    out.append("")
    out.append(cx_md)
    out.append("## Reading the difference")
    out.append("")
    out.append(
        "Both agents finished the task, both recorded a passing test, and both receipts read **verified** "
        "with **self-checked** evidence — the receipt gives neither a stronger badge than it earned. What "
        "differs is *how the work was observed* and *what it cost*:"
    )
    out.append("")
    out.append("| | Claude Code | Codex |")
    out.append("| --- | --- | --- |")
    out.append("| Actions provenance | `hook` — a live client hook observed the tool categories | `transcript_scan` — read back from Codex's own on-disk session store (no hook fires) |")
    out.append("| Evidence tier | self-checked (agent-recorded via MCP) | self-checked (agent-recorded via MCP) |")
    out.append("| Cost (estimate) | $27.40 — an Opus-class model | $3.10 — a smaller model |")
    out.append("")
    out.append(
        "Both checks are **self-checked**: the agent recorded the passing run itself. To raise a check to "
        "*independently-checked*, a client hook (or CI) has to observe it — the "
        "[coverage matrix](../coverage-matrix.md) shows which agents support that. The point of the receipt "
        "is that it says *self-checked* here, instead of painting an agent-recorded pass the same green as an "
        "independently-verified one."
    )
    out.append("")
    out.append(
        "Note the two axes staying separate: the decision reads **verified** because the latest check passes "
        "and postdates the newest work, while the evidence coverage (1/2) shows not every step is individually "
        "checked. A clean decision word never hides partial coverage."
    )
    out.append("")
    out.append("**What this does not tell you (the honest unknowns):**")
    out.append("")
    out.append("- The costs are pricing-table estimates, not provider invoices.")
    out.append("- Self-checked does not mean wrong — it means no independent check observed the run.")
    out.append("- Neither receipt judges code quality or whether the retry design is right; they record what ran, what passed, and what it cost.")
    out.append("")
    out.append(
        "See also [When an agent says done](when-an-agent-says-done.md) for how the timeline exposes a "
        "re-run and an outdated check, and the [coverage matrix](../coverage-matrix.md) for what each agent's "
        "lanes can and cannot prove."
    )
    out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# --- Example B: when an agent says done ----------------------------------------

_TASK_B = "Fix the flaky checkout-total test"


def _seed_example_b(store: Path) -> None:
    svc = SentinelService(store)
    c, s, p, ns = "claude-code", "cc-flaky", "storefront", "sha256:ex-b"
    _usage(svc, client=c, session=s, model="claude-opus-4-8", project=p, tokens=1_200_000, cost=18.60, ns=ns)
    # 1) Plan.
    _section(svc, client=c, session=s, project=p, ns=ns, section_id="plan", title="Reproduce the flaky total",
             status="completed", kind="planning", at=BASE + 60, files=["tests/test_checkout.py"],
             summary="Reproduced: rounding drifts when a line item has a 3-decimal unit price.")
    # 2) Failing check — the "failed command".
    _section(svc, client=c, session=s, project=p, ns=ns, section_id="fix", title="Fix the rounding in the total",
             status="completed", kind="implementation", at=BASE + 240, files=["src/checkout/total.py"],
             summary="Rounded each line item before summing.")
    _check(svc, client=c, session=s, project=p, ns=ns, section_id="fix", result="failed", at=BASE + 300,
           summary="3 failed (red)", command="pytest tests/test_checkout.py -q", exit_code=1)
    # 3) Re-run after the fix — passes, superseding the red run (the "retry").
    _check(svc, client=c, session=s, project=p, ns=ns, section_id="fix", result="passed", at=BASE + 520,
           summary="14 passed", command="pytest tests/test_checkout.py -q", exit_code=0)
    # 4) A LATER edit, after that green — now the passing check predates the newest
    #    work: the "outdated check".
    _section(svc, client=c, session=s, project=p, ns=ns, section_id="refactor",
             title="Extract a rounding helper", status="completed", kind="implementation",
             at=BASE + 760, files=["src/checkout/total.py", "src/checkout/money.py"],
             summary="Moved rounding into money.round_half_up(); the agent reported done here.")
    _tool_activity(svc, client=c, session=s, at=BASE + 700, basis="client_hook_tool_category",
                   categories={"read": 10, "edit": 6, "execute": 4, "search": 2},
                   names=[("Read", 10), ("Edit", 6), ("Bash", 4), ("Grep", 2)],
                   touched=["src/checkout/total.py", "src/checkout/money.py", "tests/test_checkout.py"],
                   commands=["pytest tests/test_checkout.py -q"])


def _render_example_b() -> str:
    with tempfile.TemporaryDirectory() as b_dir:
        b_store = Path(b_dir)
        _seed_example_b(b_store)
        receipt_md = _receipt_for_single_task(b_store, display_task_id="task_flaky_total", title=_TASK_B, include_timeline=True)

    out: list[str] = []
    out.append("# When an agent says done: inspect the claim, the last edit and the last check")
    out.append("")
    out.append("<!-- GENERATED by scripts/gen_worked_examples.py — do not edit by hand. -->")
    out.append("")
    out.append(
        "> Synthetic demo workspace — invented project, pricing-table cost **estimate**. Every timeline row, "
        "evidence tier, decision status and gap is the real receipt engine's output."
    )
    out.append("")
    out.append(
        f"An agent worked on **\"{_TASK_B}\"** and reported it done. Here is the receipt. Read the timeline "
        "top to bottom: a check went red, a re-run went green, and then the agent made one more edit **after** "
        "that green run. So the last thing that was proven is no longer the last thing that happened — the "
        "passing check is now outdated."
    )
    out.append("")
    out.append(
        "![The flaky-total receipt in the agentacct macOS app: decision Reported (not Verified); under Sessions "
        "& steps the \"Fix the rounding\" step is self-checked with one historical (superseded) check, while the "
        "later \"Extract a rounding helper\" edit has no check; evidence coverage is 1 of 2 with a gap, and the "
        "passing check is the agent's own (MCP), not independent.](assets/example-b-receipt.png)"
    )
    out.append("")
    out.append(receipt_md)
    out.append("## How to read this")
    out.append("")
    out.append(
        "The agent's own message says the task is done. The receipt does not take its word for it. Three "
        "separate facts sit in three separate places:"
    )
    out.append("")
    out.append("- **The claim** — the agent recorded every step `completed`. That is a report, asserted by the agent.")
    out.append("- **The last check** — a recorded `pytest` run went red, then green. The green run *superseded* the red one for the same command, so the header counts only the current frontier (1 passing check) while the timeline preserves the red run it replaced.")
    out.append("- **The last edit** — the `Extract a rounding helper` step landed **after** the green run. Nothing has re-run the tests since, so the newest code carries no passing check.")
    out.append("")
    out.append("That is why the decision status is not a clean \"verified\": the evidence coverage counts the last edit as the 1 unchecked step, and the timeline shows exactly where the check fell behind the code.")
    out.append("")
    out.append("| | What the receipt supports | What it does not |")
    out.append("| --- | --- | --- |")
    out.append("| The fix | A recorded run went from 3 failed to 14 passed | It is self-checked (agent-recorded), not independently verified |")
    out.append("| The refactor | It happened (a recorded, completed edit) | No check ran after it — the last green predates it |")
    out.append("| \"Done\" | The agent reported done | No machine evidence covers the final state |")
    out.append("")
    out.append("**Follow-up the receipt makes obvious:** re-run `pytest tests/test_checkout.py -q` after the refactor. If it passes and postdates the last edit, the last edit earns a check; until then, \"done\" is a claim, not proof.")
    out.append("")
    out.append(
        "Each timeline row is sourced from the client that recorded it (here, Claude Code); the **Lane** "
        "column separates the work steps (`primary`) from the checks (`evidence`). The checks are "
        "**self-checked** — agent-recorded through MCP, shown by the Evidence row's `mcp` source — while the "
        "tool categories were hook-captured (the Actions row's `hook` source). What each source can and "
        "cannot prove is in the [coverage matrix](../coverage-matrix.md); the capture boundaries (agentacct "
        "stores tool categories, files and commands — never full prompts or transcripts) are in the "
        "[privacy threat model](../multi-source-privacy-threat-model.md)."
    )
    out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# --- entry points -------------------------------------------------------------

def render_worked_examples() -> dict[str, str]:
    """Return {repo-relative path: content} for every generated example doc."""

    return {
        "docs/examples/compare-claude-code-and-codex.md": _render_example_a(),
        "docs/examples/when-an-agent-says-done.md": _render_example_b(),
    }


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    for rel, content in render_worked_examples().items():
        path = REPO_ROOT / rel
        path.write_text(content, encoding="utf-8")
        print(f"wrote {rel}")


if __name__ == "__main__":
    main()
