"""Live-agent evaluation of the recording contract.

Runs REAL coding agents (Claude Code, Codex, opencode, Gemini CLI) on small
sandboxed repositories, with this checkout's MCP server and instruction block
as the only guidance about recording, and grades what each agent recorded.

The task an agent is given never mentions recording. Whatever it records, it
records because of what agentacct ships: the managed instruction block and the
tool descriptions. That is the thing under test.

Grading has two layers:

* OBJECTIVE, with no model in the loop. The harness re-runs the scenario's own
  verification command and diffs the repository itself, then compares that
  reality with what the record CLAIMS. A record that reports green while the
  suite is red is caught mechanically.
* A blind JUDGE, for the four questions a reviewer opens a record to answer:
  what was this for, did it work, can I trust it, what do I do now.

Nothing here inspects the agent's prose with word lists; the judge reads it the
way a reviewer would.

Costs real money and takes minutes, so nothing in CI runs an agent. CI runs the
deterministic half (scenarios are internally honest, the objective grader is
correct); the live half is opt-in:

    python -m benchmarks.agent_recording.run --agents claude --trials 1
"""
