"""INSTALL.md <-> install_guide consistency.

INSTALL.md is the canonical agent-facing runbook that the one-line install
prompt points at. Its command blocks, notes, and capability matrix are defined
once in ``agent_chronicle.install_guide`` (also used by ``setup prompt``); this
suite fails CI whenever the document drifts from the module.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_chronicle import install_guide
from agent_chronicle.cli import app

INSTALL_MD = Path("INSTALL.md")

runner = CliRunner()


@pytest.fixture(scope="module")
def install_md_text() -> str:
    assert INSTALL_MD.is_file(), "INSTALL.md must exist at the repo root (the one-line prompt fetches it)"
    return INSTALL_MD.read_text(encoding="utf-8")


def test_install_md_embeds_every_shared_command_block_verbatim(install_md_text: str) -> None:
    for name, block in install_guide.INSTALL_MD_COMMAND_BLOCKS.items():
        assert block in install_md_text, f"INSTALL.md drifted from install_guide block: {name}"
    assert install_guide.CLI_INSTALL_PIPX_BLOCK in install_md_text
    assert install_guide.CLI_INSTALL_UV_BLOCK in install_md_text
    assert install_guide.CLI_INSTALL_FALLBACK_BLOCK in install_md_text
    assert install_guide.CLI_PATH_CHECK_BLOCK in install_md_text
    assert install_guide.CLI_PATH_CHECK_NOTE in install_md_text


def test_install_md_has_a_section_for_every_prompt_agent(install_md_text: str) -> None:
    for title in install_guide.INSTALL_MD_SECTION_TITLES.values():
        assert title in install_md_text
    # The shared MCP-preview section is written out for hermes; the other
    # preview agents must at least be named as substitutions.
    for agent in install_guide.MCP_PREVIEW_AGENTS:
        assert agent in install_md_text


def test_install_md_embeds_preamble_rules_and_notes(install_md_text: str) -> None:
    for line in install_guide.PREAMBLE_LINES:
        assert line in install_md_text
    for rule in install_guide.GROUND_RULES:
        assert f"- {rule}" in install_md_text
    for agent in ("claude-code", "codex", "hermes"):
        for note in install_guide.agent_notes(agent):
            assert f"- {note}" in install_md_text
    assert install_guide.SERVE_NOTE in install_md_text


def test_install_md_closes_with_the_capability_matrix_and_relay_instruction(install_md_text: str) -> None:
    assert f"## {install_guide.CAPABILITY_MATRIX_TITLE}" in install_md_text
    for line in install_guide.CAPABILITY_MATRIX:
        assert f"- {line}" in install_md_text
    assert install_guide.RELAY_INSTRUCTION in install_md_text
    # Honesty pins: never promise invoices or exact Codex session joins.
    assert "never provider invoices" in install_md_text
    assert "client_reported" in install_md_text


def test_install_md_embeds_the_global_install_section(install_md_text: str) -> None:
    """Phase 2.6: global-mode recipe must ship verbatim from the module."""
    assert install_guide.GLOBAL_INSTALL_SECTION_TITLE in install_md_text
    assert install_guide.GLOBAL_INSTALL_INTRO in install_md_text
    assert install_guide.GLOBAL_INSTALL_BLOCK in install_md_text
    for note in install_guide.GLOBAL_INSTALL_NOTES:
        assert f"- {note}" in install_md_text


def test_global_install_block_never_relies_on_env_or_legacy_store() -> None:
    block = install_guide.GLOBAL_INSTALL_BLOCK
    # Explicit absolute store binding on every store-touching command: GUI
    # clients do not inherit shell env vars, so env selection cannot appear.
    assert "AGENT_CHRONICLE_STORE_DIR" not in block
    for line in block.splitlines():
        if "mcp serve" in line or line.startswith("agentacct serve") or "hooks claude-code install" in line:
            assert '--store-dir "$HOME/.agent-sentinel-global/state"' in line, line
    # The legacy silent-fallback path must never be recommended as the store.
    assert "$HOME/.agent-sentinel/state" not in block
    assert "~/.agent-sentinel/state" not in block
    # The scope-shadowing cleanup is a REQUIRED step, not a footnote.
    assert any(note.startswith("REQUIRED cleanup") for note in install_guide.GLOBAL_INSTALL_NOTES)


def test_global_block_guards_non_path_installs_and_asks_first() -> None:
    """Review fix round: a non-PATH install must never register an EMPTY
    command (silently dead user-scope server), and the user/global config
    mutations carry the ask-first step the document's own ground rule demands."""
    block = install_guide.GLOBAL_INSTALL_BLOCK
    assert 'AGENTACCT_BIN="$(command -v agentacct)"' in block
    assert '[ -n "$AGENTACCT_BIN" ] || AGENTACCT_BIN=' in block
    # The bare inline substitution (empty-arg hazard) must not return.
    assert '"$(command -v agentacct)" mcp serve' not in block
    assert "show them to the user and ask first" in block
    # Both registrations use the guarded variable.
    for line in block.splitlines():
        if "mcp add" in line and not line.lstrip().startswith("#"):
            assert '"$AGENTACCT_BIN"' in line, line


def test_ground_rule_scopes_state_to_this_machine_not_one_repo() -> None:
    """Review fix round: the hard rule must not contradict the Global install
    section — state is machine-local (project store by default, global store
    by choice), and the no-telemetry promise stays."""
    rule = next(rule for rule in install_guide.GROUND_RULES if rule.startswith("No telemetry"))
    assert "local to this machine" in rule
    assert "`.agent-sentinel/`" in rule
    assert "global store" in rule
    assert "stays in the repository" not in rule


def test_required_new_session_notes_for_both_write_mcp_clients() -> None:
    """The install-session-cannot-see-the-server failure (owner-hit) stays documented."""
    codex_note = next(note for note in install_guide.CODEX_NOTES if note.startswith("REQUIRED final step"))
    assert "NEW Codex session" in codex_note
    assert "session start" in codex_note
    claude_note = next(note for note in install_guide.CLAUDE_CODE_NOTES if note.startswith("REQUIRED final step"))
    assert "NEW Claude Code session" in claude_note


def test_install_md_contains_no_retired_workaround_patterns(install_md_text: str) -> None:
    # These patterns only existed to work around pre-Phase-1 bugs (silent
    # home-store fallback, doctor writes, worktree stores). Their return would
    # mean the runbook is patching code problems again.
    assert "mcp doctor --store-dir" not in install_md_text
    assert "CLAUDE_PROJECT_DIR" not in install_md_text
    assert 'SENTINEL_STORE="$PROJECT_ROOT' not in install_md_text
    assert "~/.agent-sentinel/state" not in install_md_text


def test_full_prompt_is_built_from_the_same_content_for_every_agent() -> None:
    for agent in install_guide.PROMPT_AGENTS:
        prompt = install_guide.full_prompt(agent)
        assert install_guide.agent_command_block(agent) in prompt
        assert install_guide.CLI_INSTALL_PIPX_BLOCK in prompt
        for line in install_guide.CAPABILITY_MATRIX:
            assert line in prompt
        for note in install_guide.agent_notes(agent):
            assert note in prompt
        assert install_guide.RELAY_INSTRUCTION in prompt
        assert install_guide.one_line_prompt(agent) == install_guide.ONE_LINE_PROMPT


def test_prompt_helpers_reject_unknown_agents() -> None:
    with pytest.raises(ValueError):
        install_guide.full_prompt("not-a-client")
    with pytest.raises(ValueError):
        install_guide.one_line_prompt("not-a-client")
    with pytest.raises(ValueError):
        install_guide.mcp_preview_block("claude-code")


def test_one_liner_is_a_single_public_install_line() -> None:
    # The public paste line installs agentacct from PyPI and stays ONE line.
    assert "pipx install agentacct" in install_guide.ONE_LINE_PROMPT
    assert "\n" not in install_guide.ONE_LINE_PROMPT, "the paste prompt must stay a single line"


def test_public_install_surfaces_do_not_reference_the_deleted_repository() -> None:
    for path in (
        Path("README.md"),
        Path("INSTALL.md"),
        Path("CONTRIBUTING.md"),
        Path("pyproject.toml"),
        Path("src/agent_chronicle/install_guide.py"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "mikehasa/agent-chronicle" not in text, path
        assert "raw.githubusercontent.com/mikehasa/agent-chronicle" not in text, path

    # The NEW public repo is legitimately referenced; the pipx block is the
    # plain PyPI form (the git+https source install lives in its own block).
    assert "mikehasa/agentacct" in install_guide.REPO_URL
    assert install_guide.CLI_INSTALL_PIPX_BLOCK == "pipx install agentacct"
    assert "git+https://" not in install_guide.CLI_INSTALL_PIPX_BLOCK


# --- Phase 2.8: standing workflow instructions -------------------------------


def test_global_install_recipe_installs_standing_instructions() -> None:
    """Global mode's whole gap is that it installs no standing 'record work'
    instruction; the recipe must now include `setup instructions` for both
    write-mcp clients, and the mirror must land in INSTALL.md."""
    block = install_guide.GLOBAL_INSTALL_BLOCK
    assert "agentacct setup instructions --agent claude-code --user" in block
    assert "agentacct setup instructions --agent codex --user" in block
    md = INSTALL_MD.read_text(encoding="utf-8")
    assert "agentacct setup instructions --agent claude-code --user" in md
    assert "agentacct setup instructions --agent codex --user" in md
    # The note explaining WHY this is what fills the dashboard must ship too.
    assert any("fill the dashboard with work context" in note for note in install_guide.GLOBAL_INSTALL_NOTES)


def test_global_install_instructions_bind_the_marker_store_explicitly() -> None:
    """Fix round (Phase 2.10): a user-level `setup instructions` run spans
    projects, so without an explicit --store-dir the instrumentation marker
    is SKIPPED — the documented global recipe must deliver the marker to the
    global store, or global mode never gets pre/post honesty."""
    block = install_guide.GLOBAL_INSTALL_BLOCK
    for line in block.splitlines():
        if "setup instructions" in line and not line.lstrip().startswith("#"):
            assert '--store-dir "$HOME/.agent-sentinel-global/state"' in line, line
    md = INSTALL_MD.read_text(encoding="utf-8")
    for agent in ("claude-code", "codex"):
        assert (
            f'agentacct setup instructions --agent {agent} --user --store-dir "$HOME/.agent-sentinel-global/state"'
            in md
        )


def test_global_install_notes_reference_the_merge_tool() -> None:
    """The 'no merge tool yet' claim is now false — the note must point at it."""
    joined = " ".join(install_guide.GLOBAL_INSTALL_NOTES)
    assert "usage merge-store" in joined
    assert "no merge tool yet" not in joined
    assert "usage merge-store" in INSTALL_MD.read_text(encoding="utf-8")


def test_workflow_instruction_block_is_sections_only_and_concise() -> None:
    block = install_guide.workflow_instruction_block()
    # SECTIONS-ONLY contract (Phase 1): never instruct task lifecycle events.
    assert "task_started" not in block
    assert "task_completed" not in block
    # It must name the sections tool and the machine-check tool.
    assert "sentinel_record_section" in block
    assert "sentinel_record_machine_check" in block
    # Concise: it loads into every session. Keep it short (bounded line count).
    body_lines = [line for line in block.splitlines() if line.strip()]
    assert len(body_lines) <= 14, f"instruction block grew to {len(body_lines)} non-blank lines; keep it short"
    # Honest low-friction escape hatch: recording nothing is allowed.
    assert "recording nothing" in block or "record nothing" in block


def test_workflow_instruction_lines_are_directive_and_beat_deferral() -> None:
    """Phase 2.9: the file-level instruction must be directive about real work
    (no easy blanket opt-out) and carry the load-if-deferred hint so the agent
    discovers deferred Chronicle tools instead of recording nothing."""
    block = install_guide.workflow_instruction_block()
    # Directive lead-in: record what the session does (not a soft "if available").
    assert "Record what this session does" in block
    # The deferral-beating hint travels on the file surface too.
    assert "not directly available" in block
    assert "load them first" in block
    assert "deferred" in block
    # Honesty guardrail present and explicit about NOT fabricating figures.
    assert "never fabricate" in block
    # Low-friction stays NARROW: only a genuinely trivial throwaway session skips,
    # and it still tells the agent to record real work.
    assert "genuinely trivial throwaway session" in block
    assert "do record real work" in block
    # The old easy-opt-out framing is gone.
    assert "If the Agent Chronicle MCP tools are available" not in block
    assert "skip all of this for trivial throwaway sessions" not in block


def test_mcp_server_instructions_are_directive_honest_and_deferral_aware() -> None:
    """Phase 2.9: MCP_SERVER_INSTRUCTIONS is the InitializeResult.instructions
    string. It must be directive, tool-aware, honest, and concise."""
    text = install_guide.MCP_SERVER_INSTRUCTIONS
    assert text == install_guide.mcp_server_instructions()
    assert text.strip()
    # States plainly WHY: it records what the session did for the local dashboard.
    assert "records what this session actually did" in text
    assert "dashboard" in text
    # Directs the two recording tools.
    assert "sentinel_record_section" in text
    assert "section_status=started" in text
    assert "sentinel_record_machine_check" in text
    # THE deferral line — the key nudge that beats Claude Code's tool deferral.
    assert "not directly available" in text
    assert "load them first" in text
    assert "deferred" in text
    # Honesty guardrail: MCP evidence separate from token/cost; never fabricate.
    assert "token/cost" in text
    assert "never fabricate" in text
    # SECTIONS-ONLY contract holds at the tool layer too.
    assert "task_started" not in text
    assert "task_completed" not in text
    # Concise: it costs context tokens every session. Bound the line count.
    lines = [line for line in text.splitlines() if line.strip()]
    assert 6 <= len(lines) <= 10, f"MCP instructions have {len(lines)} lines; keep them ~6-10"
    # Low-friction stays narrow (no blanket opt-out).
    assert "genuinely trivial throwaway session" in text


def test_recording_surfaces_share_one_contract_core() -> None:
    """All THREE surfaces must embed the SAME per-task directive bullets so the
    tool layer, the instruction file, and the SessionStart hook can never drift
    on WHAT to record."""
    contract = install_guide._RECORDING_CONTRACT_LINES
    assert contract, "shared recording-contract core must be non-empty"
    mcp_text = install_guide.MCP_SERVER_INSTRUCTIONS
    workflow_text = "\n".join(install_guide.WORKFLOW_INSTRUCTION_LINES)
    session_start_text = install_guide.SESSION_START_ADDITIONAL_CONTEXT
    for line in contract:
        assert line in mcp_text, f"MCP instructions dropped a shared contract line: {line!r}"
        assert line in workflow_text, f"workflow lines dropped a shared contract line: {line!r}"
        assert line in session_start_text, f"session-start context dropped a shared contract line: {line!r}"
    # The load-if-deferred hint is shared verbatim by all three.
    assert install_guide._LOAD_IF_DEFERRED_LINE in mcp_text
    assert install_guide._LOAD_IF_DEFERRED_LINE in workflow_text
    assert install_guide._LOAD_IF_DEFERRED_LINE in session_start_text


def test_recording_contract_directs_a_section_before_the_first_tool_call() -> None:
    """Phase 2.10: the contract is explicit about WHEN — open a section BEFORE
    the first other tool call, not vaguely 'at the start of the session'
    (un-primed sessions read the vague form and never record)."""
    first_bullet = install_guide._RECORDING_CONTRACT_LINES[0]
    assert "BEFORE your first other tool call" in first_bullet
    for surface in (
        install_guide.MCP_SERVER_INSTRUCTIONS,
        "\n".join(install_guide.WORKFLOW_INSTRUCTION_LINES),
        install_guide.SESSION_START_ADDITIONAL_CONTEXT,
    ):
        assert "BEFORE your first other tool call" in surface


def test_session_start_additional_context_is_directive_honest_and_concise() -> None:
    """Phase 2.10: the SessionStart hook injects this text into every new root
    session — the most prominent delivery channel. Same bar as the MCP
    instructions: directive, deferral-aware, honest, sections-only, short."""
    text = install_guide.SESSION_START_ADDITIONAL_CONTEXT
    assert text == install_guide.session_start_additional_context()
    assert text.strip()
    # Says WHY (dashboard, work-not-tokens) and directs the recording tools.
    assert "dashboard" in text
    assert "sentinel_record_section" in text
    assert "sentinel_record_machine_check" in text
    # Deferral-aware: the tools may need loading first.
    assert "load them first" in text
    # Honesty guardrail + sections-only contract.
    assert "never fabricate" in text
    assert "task_started" not in text
    assert "task_completed" not in text
    # Concise: it is injected into every session's context.
    lines = [line for line in text.splitlines() if line.strip()]
    assert 6 <= len(lines) <= 10, f"session-start context has {len(lines)} lines; keep it ~6-10"
    assert "genuinely trivial throwaway session" in text


def test_workflow_instruction_block_is_wrapped_in_idempotency_markers() -> None:
    block = install_guide.workflow_instruction_block()
    assert block.startswith(install_guide.INSTRUCTIONS_BEGIN_MARKER)
    assert block.rstrip().endswith(install_guide.INSTRUCTIONS_END_MARKER)


def test_render_instruction_file_adds_block_to_empty_and_is_idempotent() -> None:
    once = install_guide.render_instruction_file("", remove=False)
    assert install_guide.INSTRUCTIONS_BEGIN_MARKER in once
    assert once.endswith("\n")
    twice = install_guide.render_instruction_file(once, remove=False)
    assert twice == once, "re-render must be a pure replace, not accretion"
    assert twice.count(install_guide.INSTRUCTIONS_BEGIN_MARKER) == 1


def test_render_instruction_file_preserves_surrounding_content() -> None:
    original = "# My rules\n\n- do X\n- do Y\n"
    added = install_guide.render_instruction_file(original, remove=False)
    assert added.startswith("# My rules\n\n- do X\n- do Y\n"), "user content must be preserved verbatim as a prefix"
    removed = install_guide.render_instruction_file(added, remove=True)
    assert removed == original, "add then remove must round-trip byte-for-byte"


def test_render_instruction_file_replaces_only_the_marked_block() -> None:
    original = "# rules\n\n- keep me\n"
    with_block = install_guide.render_instruction_file(original, remove=False)
    # Simulate a hand-edit to the body inside the markers, then re-render.
    tampered = with_block.replace("record your work", "RECORD YOUR WORK")
    fixed = install_guide.render_instruction_file(tampered, remove=False)
    assert fixed == with_block, "re-render restores the canonical block, leaving outside content untouched"
    assert "- keep me" in fixed


def test_setup_instructions_cli_writes_merges_and_removes(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    target.write_text("# owner content\n\n- rule one\n", encoding="utf-8")
    original = target.read_text(encoding="utf-8")

    # dry-run writes nothing but shows a diff.
    dry = runner.invoke(app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "Dry run" in dry.output
    assert "sentinel_record_section" in dry.output
    assert target.read_text(encoding="utf-8") == original, "dry-run must not touch the file"

    # apply merges without clobbering owner content.
    applied = runner.invoke(app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target)])
    assert applied.exit_code == 0, applied.output
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# owner content\n\n- rule one\n")
    assert install_guide.INSTRUCTIONS_BEGIN_MARKER in text

    # idempotent re-run.
    again = runner.invoke(app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target)])
    assert again.exit_code == 0
    assert "No change" in again.output
    assert target.read_text(encoding="utf-8") == text

    # remove restores the original exactly.
    removed = runner.invoke(app, ["setup", "instructions", "--agent", "claude-code", "--path", str(target), "--remove"])
    assert removed.exit_code == 0, removed.output
    assert target.read_text(encoding="utf-8") == original


def test_setup_instructions_cli_creates_missing_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "AGENTS.md"
    result = runner.invoke(app, ["setup", "instructions", "--agent", "codex", "--path", str(target)])
    assert result.exit_code == 0, result.output
    assert target.is_file()
    assert install_guide.INSTRUCTIONS_BEGIN_MARKER in target.read_text(encoding="utf-8")


def test_setup_instructions_cli_rejects_unknown_agent(tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "instructions", "--agent", "not-a-client", "--path", str(tmp_path / "x.md")])
    assert result.exit_code != 0


def test_setup_instructions_both_agents_share_the_same_body(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    codex = tmp_path / "AGENTS.md"
    runner.invoke(app, ["setup", "instructions", "--agent", "claude-code", "--path", str(claude)])
    runner.invoke(app, ["setup", "instructions", "--agent", "codex", "--path", str(codex)])
    assert claude.read_text(encoding="utf-8") == codex.read_text(encoding="utf-8")


# --- FIX 2 (MAJOR — instruction merge must not delete user content) ----------

BEGIN = install_guide.INSTRUCTIONS_BEGIN_MARKER
END = install_guide.INSTRUCTIONS_END_MARKER


def _fenced_documentation() -> str:
    """User content that DOCUMENTS the feature inside a Markdown code fence,
    quoting the marker strings verbatim. This must be treated as ordinary
    content — never as a managed block to delete."""
    return (
        "# My rules\n"
        "\n"
        "Chronicle manages a block that looks like this:\n"
        "\n"
        "```markdown\n"
        f"{BEGIN}\n"
        "  ... some example body a user pasted while explaining it ...\n"
        "  KEEP THIS LINE — it is user documentation, not the managed block.\n"
        f"{END}\n"
        "```\n"
        "\n"
        "- keep this rule too\n"
    )


def test_render_touches_only_the_real_block_not_fenced_documentation() -> None:
    """The reviewer's exact case: a user CLAUDE.md that DOCUMENTS the feature in
    a fenced code block (verbatim markers) AND has a real managed block
    elsewhere. Re-render must rewrite ONLY the real block; the fenced
    documentation is byte-preserved."""
    doc = _fenced_documentation()
    # A real managed block appended after the documentation.
    with_real = install_guide.render_instruction_file(doc, remove=False)
    assert with_real.startswith(doc.rstrip("\n"))
    assert "KEEP THIS LINE" in with_real

    # Re-render: only the real (last) block is replaced; the fenced copy stays.
    tampered = with_real.replace("record your work", "MANGLED")
    fixed = install_guide.render_instruction_file(tampered, remove=False)
    assert fixed == with_real, "re-render restores the canonical block only"
    assert "KEEP THIS LINE" in fixed, "fenced documentation must never be deleted"
    # The fenced example markers plus the ONE real block => 2 begin markers total.
    assert fixed.count(BEGIN) == 2

    # --remove strips ONLY the real block; the fenced documentation survives.
    removed = install_guide.render_instruction_file(with_real, remove=True)
    assert "KEEP THIS LINE" in removed
    assert removed.count(BEGIN) == 1, "the fenced (documentation) marker remains"
    assert removed == doc, "remove restores the user's file byte-for-byte"


def test_markers_only_inside_fence_are_not_a_block_write_appends_fresh() -> None:
    """Markers appear ONLY inside a code fence and there is NO real block:
    write must APPEND a fresh block and leave the fenced content untouched."""
    doc = _fenced_documentation()
    # No real block present yet.
    assert install_guide._split_around_managed_block(doc) == (doc, False)

    added = install_guide.render_instruction_file(doc, remove=False)
    assert "KEEP THIS LINE" in added, "fenced content untouched"
    assert added.count(BEGIN) == 2, "a fresh real block is appended alongside the fenced copy"
    # A --remove on the fenced-only file is a no-op for the documentation.
    removed = install_guide.render_instruction_file(doc, remove=True)
    assert removed == doc


def test_midline_marker_never_triggers_a_block() -> None:
    """A marker string appearing MID-LINE (not at column 0) is ordinary content
    and never defines/deletes a block."""
    text = f"# rules\n\nSee the marker {BEGIN} inline here, and {END} too.\n\n- keep me\n"
    stripped, present = install_guide._split_around_managed_block(text)
    assert present is False
    assert stripped == text
    # Rendering appends a fresh real block and preserves the inline text.
    added = install_guide.render_instruction_file(text, remove=False)
    assert f"See the marker {BEGIN} inline here" in added
    assert added.rstrip().endswith(END)


# --- Phase 3: the proven Claude Code recipe is productized -------------------


def test_claude_code_recipe_names_all_three_levers() -> None:
    """Owner A/B verified (Phase 2.10): the SessionStart directive alone did
    NOT make sessions record — the tools must also arrive directly callable
    (ENABLE_TOOL_SEARCH=auto), and the hook bridge supplies the join ids. The
    runbook must teach the whole causal chain and never over-credit one lever."""
    # The merge step covers BOTH settings blocks.
    assert '"hooks" and "env" blocks' in install_guide.CLAUDE_CODE_BLOCK
    # One note states the honest three-lever chain.
    joined_notes = " ".join(install_guide.CLAUDE_CODE_NOTES)
    assert "ENABLE_TOOL_SEARCH" in joined_notes
    assert "record nothing" in joined_notes
    # The old over-credit of the directive alone is retired.
    assert "which is what makes Claude Code sessions actually record" not in joined_notes


def test_global_install_covers_the_env_lever_too() -> None:
    """The proven live config sits in user-level ~/.claude/settings.json, so
    the global recipe must carry the env merge as well."""
    assert '"hooks" AND "env" blocks' in install_guide.GLOBAL_INSTALL_BLOCK
    assert any("ENABLE_TOOL_SEARCH" in note for note in install_guide.GLOBAL_INSTALL_NOTES)


def test_capability_matrix_claude_code_line_is_honest_about_the_recipe() -> None:
    """The Claude Code claim names what recorded work context actually
    REQUIRES (all three levers) and what degrades without each."""
    claude_line = next(line for line in install_guide.CAPABILITY_MATRIX if line.startswith("Claude Code:"))
    assert "SessionStart and PreToolUse" in claude_line
    assert "all three levers" in claude_line
    assert "ENABLE_TOOL_SEARCH=auto" in claude_line
    assert "record nothing" in claude_line
    assert "never session-linked" in claude_line
    assert "automatic high-confidence" in claude_line
    assert "Exact attribution still requires" in claude_line
    # Honest split (G2): the SessionStart lever must NOT overclaim that without
    # it "no ids are captured" — PreToolUse captures ids independently on every
    # tool call, and the MCP initialize result also delivers the directive.
    assert "no ids are captured" not in claude_line
    assert "PreToolUse still captures" in claude_line


def test_serve_note_names_the_refresh_button_not_a_page_reload() -> None:
    """G1: a dashboard page reload is a read-only scan; only the
    'Refresh & save usage' button writes. The note must name the button, never
    tell the user to 'refresh the dashboard' to populate the ledger."""
    assert "Refresh & save usage" in install_guide.SERVE_NOTE
    assert "refresh the dashboard" not in install_guide.SERVE_NOTE.lower()
    assert "read-only scan" in install_guide.SERVE_NOTE


def test_readme_embeds_the_capability_matrix_verbatim() -> None:
    """Work item 4 (Phase 3): the README shows the same per-client capability
    matrix as INSTALL.md, from the same constants, so the two cannot drift."""
    readme = Path("README.md").read_text(encoding="utf-8")
    for line in install_guide.CAPABILITY_MATRIX:
        assert f"- {line}" in readme, f"README drifted from install_guide.CAPABILITY_MATRIX: {line[:60]}..."


# --- Phase 3: honest OS-support claims (item 3) --------------------------------


def test_os_claims_say_posix_only_everywhere() -> None:
    """The product is POSIX-only (fcntl.flock in service.py, process groups and
    POSIX signals in runner.py), so every OS claim must say macOS/Linux with
    Windows only via WSL — never OS Independent."""
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "Operating System :: OS Independent" not in pyproject
    assert '"Operating System :: MacOS"' in pyproject
    assert '"Operating System :: POSIX :: Linux"' in pyproject

    readme = Path("README.md").read_text(encoding="utf-8")
    assert "Requires Python >= 3.11 on macOS or Linux; Windows is supported only via WSL." in readme

    # The CLI-path note carries the same requirement; INSTALL.md mirrors it
    # verbatim (enforced by test_install_md_embeds_every_shared_command_block_verbatim).
    assert "Requires Python >= 3.11 on macOS or Linux (Windows via WSL)." in install_guide.CLI_PATH_CHECK_NOTE
