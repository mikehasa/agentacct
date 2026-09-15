# Hooks, MCP surface, and whether the agent uses the system well

Audited 2026-09-12 against the installed store (8,369 events) and the code. Every
claim is measured or cited; inferences are labelled.

## 1. The finding that mattered most

**Codex recorded 1,161 sections and not ONE carried a `client_session_id`.**

That single number explains the app's worst user-visible weakness — 61% of work
items never joining to usage — and it was not the agent's fault. Two independent
defects stacked:

1. **The installer wired no Codex `SessionStart` hook** (`codex_hooks_json_block`
   registered only `PreToolUse` + `SessionEnd`). Codex exposes eight hook events
   and its SessionStart output is injected into the model's context; the
   recording contract relies on exactly that event for both identity capture and
   directive delivery. See the [Codex hooks reference](https://raw.githubusercontent.com/shanraisshan/codex-cli-hooks/main/.codex/hooks/HOOKS-README.md).
2. **The context validator hardcoded `client == "claude-code"`**, so even a
   captured Codex context would have been discarded before inheritance. The
   MCP inheritance gate had the same hardcoded check, and the loader read only
   Claude Code's slot.

Both are fixed: Codex now gets `SessionStart`, contexts are per-client
(`client-context/codex.json`), and the validator, loader and inheritance gate
accept every bridged client. Verified end to end — a Codex SessionStart event now
yields a section carrying its session id, with two regression tests.

**Proof the mechanism works when wired:** 5 real Claude Code sections in the
installed store now carry session ids and a `client-context/claude-code.json`
provenance. Those are production sessions, not fixtures.

## 2. Hook inventory — what exists, and the asymmetry

| Client | Events wired | Identity capture | Turn boundary | Default install |
| --- | --- | --- | --- | --- |
| Claude Code | `PreToolUse`, `SessionStart`, `PostToolUse`, `SessionEnd` | yes | **no** | yes (global + project) |
| Codex | `SessionStart` (new), `PreToolUse`, `SessionEnd` | **now yes** | no | global only |
| Hermes | `pre_tool_call`, `post_tool_call`, `on_session_end`, `pre_llm_call` | yes | **yes** | global, needs consent + gateway restart |
| OpenCode | plugin `tool.execute.before/.after` | yes | no | global |

**Gaps that remain, honestly:**

- **No checked revision is captured anywhere.** A stored check is
  `{client, session, kind, runner, digest, exit, time}` — you cannot say which
  commit passed. This is the single largest hole in the product's credibility
  (see ASSESSMENT.md §7).
- **No link from a check to the tool call or file change it validated.**
  `PreToolUse` records an edit destination, but no `tool_call_id`/`turn_id`
  correlates it with the check. Correlation is timestamp-only (inferred).
- **Turn boundaries exist only for Hermes.** Claude Code's and Codex's adapters
  claim `UserPromptSubmit`/`Stop` support that no installed settings block wires,
  so usage joins at session level and `turn_id` arrives only if the agent
  volunteers it.
- **The generic Evidence v2 manifest is inert**: `capture manifest` renders a
  manifest and nothing installs it.
- **Project-scope Codex gets no hook at all** (global only).

## 3. MCP surface — 9 tools, one important gap, one misleading tool

The tools are: `list_runs`, `get_report`, `record_machine_check`, `record_event`,
`attach_client_context`, `record_section`, `record_agent_usage_debug`,
`list_events`, `get_event_summary`.

**The gap I closed: `next_step` and `blocker` were write-only.** Agents were
asked for them and the ledger stored them, but *no MCP tool ever returned them*.
An agent could not recover a previous session's continuation point, and could not
see its own unfinished work. Added `agentacct_work_status`: read-only, scoped to
the session via the inherited hook context, returning open sections, blockers,
their next steps, and completed work with no check behind it — plus a
`what_to_do_next` list. Three tests cover the reporting, the read-only guarantee,
and session scoping.

**Descriptor quality was uneven and is improved.** `client_session_id`,
`client_transcript_id` and `project_dir` had **no description at all** — the
fields that decide whether usage ever joins. They now explain what they do, why
they matter, and that a hook bridge fills them in automatically so an agent must
never guess. `summary`, `blocker` and `next_step` now state the shape the reader
sees. `agentacct_record_event` now says when NOT to use it and that numbers put
there are excluded from totals.

**One audit concern was wrong, and I checked:** the suspicion that
`record_event`'s `estimated_cost_usd` could leak invented cost into totals is
false — measured directly, a fabricated 42.5 USD and 999,999 tokens leave
`total_tokens`, `estimated_cost_total` and `usage_event_count` all at 0, because
`strip_untrusted_usage_truth_metadata` removes the provenance. The real defect
was the *silence* about it, which is now fixed.

**No status tool existed before; none is needed beyond the one added.** The
remaining nine all have a clear job, and none is redundant.

## 4. The prompt was the wrong shape

`_RECORDING_CONTRACT_LINES` was five bullets whose first packed WHEN + WHAT +
every required argument + an example into one ~640-character sentence, and never
asked for `turn_id`, `next_step` or `blocker` — the fields that unlock
attribution and resumption. Right content, wrong shape.

It is now eight bullets, one idea each, reason first, with an explicit answer to
"what if I am unsure" ("record it; a short section beats a gap"). It stayed
inside the enforced budgets: the guide test caps the block at 14 lines and the
session-start variant at 10, because a long directive competes with the user's
actual request for the agent's attention. The status tool is named inside the
completion bullet rather than adding a line.

## 5. Configuration, and where setup still asks the user to do work

| Client | Scope | Client-side step remaining |
| --- | --- | --- |
| Claude Code | global + project | merge the hooks+env settings block (skipped silently without `--yes`) |
| Codex | **global only** | one-time hook **trust**, then a new session |
| Hermes | global | one-time **consent** + **gateway restart** |
| OpenCode | global | JSONC config → manual `opencode mcp add` in some setups |
| OpenClaw | manual | nothing installed automatically |

**Honest assessment:** three of five clients require a manual step, and each
fails *silently* if skipped — the user sees recorded work with no session link
and no explanation. The new `agentacct_work_status` output partially fixes the
diagnosis ("No session id is in scope... install the client hook bridge"), but
the setup path should tell the user this at install time rather than leaving them
to infer it from missing data later.

## 6. Ranked recommendations

1. **Capture the checked revision mechanically** (branch + commit + dirty state
   at check time, from the hook or CLI environment). No field, no capture path
   exists today. This is what turns "a check passed" into "this revision passed".
2. **Wire the turn-boundary hooks that adapters already claim** (Claude Code
   `UserPromptSubmit`/`Stop`, Codex equivalents) so per-turn attribution stops
   depending on the agent volunteering `turn_id`.
3. **Carry `tool_call_id` into the activity tick** so a check can be tied to the
   edit it validated, instead of correlating by timestamp.
4. **Give project-scope Codex the hook**, or make the global-only limitation
   explicit in the setup output.
5. **Delete or install the Evidence v2 manifest path** — render-only code that
   promises capture and performs none is worse than absent.

## 7. What I am not claiming

- The hook and prompt changes are **forward-only**. The 1,161 unlinked Codex
  sections already in the store stay unlinked; re-recording cannot fix history.
- The Codex fix is **untested against a live Codex session** here. It is verified
  by unit tests, by direct invocation with a Codex-shaped event, and by the
  mechanism demonstrably working for Claude Code in production — not by watching
  Codex itself record a linked section. The installer must be re-run for the new
  hook to reach an existing machine.
- The audit's client-capability detail beyond Codex's event list comes from
  in-repo adapters and docs, not from live clients.
