"""Single source of truth for the one-line paste-to-agent install UX.

Three surfaces must always tell the same story:

- ``INSTALL.md`` at the repo root — the canonical agent-facing runbook the
  one-line prompt points at;
- ``agentacct setup prompt`` — emits the short one-liner, or (with
  ``--full``) a self-contained offline prompt for air-gapped/no-fetch agents;
- the README install section — leads with the same one-liner.

Every command block, note, and capability claim lives HERE as a constant.
INSTALL.md must embed these constants verbatim (a regression test compares
them), and the ``--full`` prompt is assembled from the same constants, so the
three surfaces cannot drift independently.

Rules for editing:

- Command blocks contain only static, copy-pasteable text (bare
  ``agentacct`` invocations, no machine-specific paths).
- Keep it SHORT. The length of the pre-Phase-1 install prompts existed to work
  around bugs (silent home-store fallback, relative MCP store paths, worktree
  stores, doctor writes) that are now fixed in code. Do not re-grow the prompt
  to patch a code problem — fix the code.
"""

from __future__ import annotations

DASHBOARD_URL = "http://127.0.0.1:8765"
REPO_URL = "https://github.com/mikehasa/agentacct"

# The one line a user pastes into any coding agent to install + set up agentacct.
# agentacct is published on PyPI; the git+https form installs the latest source
# straight from the public repository.
ONE_LINE_PROMPT = (
    "Install and set up agentacct in this repo — a local-first dashboard that reads my coding-agent "
    "logs read-only and shows honest token usage and cost. Run `pipx install agentacct` "
    "(or `pipx install git+https://github.com/mikehasa/agentacct`), then `agentacct onboard` from this "
    "repo root, then tell me the dashboard URL. Observe-only: never store, request, or echo any API key; "
    "all state stays local under `.agent-sentinel/`. Don't modify my global client config without showing "
    "the exact command first."
)

# Agents accepted by `setup prompt` / `setup mcp`. The last four share one
# INSTALL.md section (their MCP config lives in client-specific/profile
# locations, so agentacct previews instead of writing).
PROMPT_AGENTS = ("claude-code", "codex", "hermes", "opencode", "openclaw", "generic")
MCP_PREVIEW_AGENTS = ("hermes", "opencode", "openclaw", "generic")

# INSTALL.md section headings (level-3, under "Step 2").
INSTALL_MD_SECTION_TITLES = {
    "claude-code": "### Claude Code",
    "codex": "### Codex",
    "mcp-preview": "### Hermes / OpenCode / OpenClaw / other MCP clients",
}


def install_md_section_title(agent: str) -> str:
    key = "mcp-preview" if agent in MCP_PREVIEW_AGENTS else agent
    return INSTALL_MD_SECTION_TITLES[key]


# --- What is being installed (preamble shared by INSTALL.md and --full) ------

PREAMBLE_LINES = (
    "agentacct is local-first Agent Work Intelligence for coding agents:",
    "- a usage ledger of client-reported tokens, imported from the coding agent's own local session files;",
    "- an MCP work context: sections and events the agent records while working, joined to usage with honest confidence labels;",
    f"- a local dashboard (`agentacct serve`, {DASHBOARD_URL}).",
)

GROUND_RULES = (
    "Observe-only. NEVER store, request, or echo provider API keys — nothing in this install needs one.",
    "No telemetry. All state stays local to this machine: in the project store under `.agent-sentinel/` (gitignored; the store directory keeps the pre-rename name `.agent-sentinel/` for compatibility with existing stores) by default, or in the global store directory if you follow the optional Global install section, plus the client config files named below.",
    "Do not modify global/profile client configuration without showing the exact command and asking first.",
    "If `agentacct` is not on PATH, run it via its durable absolute path — never a throwaway temp venv. MCP config writes embed the absolute executable automatically.",
)

# --- Step 1: install the CLI -------------------------------------------------

CLI_INSTALL_PIPX_BLOCK = "pipx install agentacct"

CLI_INSTALL_UV_BLOCK = "uv tool install agentacct"

# Latest straight from the public repository (before/without a PyPI release).
CLI_INSTALL_SOURCE_BLOCK = "pipx install git+https://github.com/mikehasa/agentacct"

CLI_INSTALL_FALLBACK_BLOCK = '''python3 -m venv "$HOME/.agentacct"
"$HOME/.agentacct/bin/python" -m pip install agentacct'''

CLI_PATH_CHECK_BLOCK = "command -v agentacct"

CLI_PATH_CHECK_NOTE = (
    "If `command -v` prints nothing, use the absolute path for every command below: "
    "pipx installs to `$HOME/.local/bin/agentacct`; the dedicated-venv fallback is "
    "`$HOME/.agentacct/bin/agentacct`. Requires Python >= 3.11 "
    "on macOS or Linux (Windows via WSL)."
)

# --- Step 2: per-client command blocks ----------------------------------------
# Blocks are verbatim in INSTALL.md and in `setup prompt --full` output.

CLAUDE_CODE_BLOCK = """agentacct init --agent claude-code --write-mcp
agentacct hooks claude-code install
# REQUIRED (attribution keystone): activate the hook bridge — merge BOTH the
# "hooks" and "env" blocks from .claude/settings.agent-sentinel.example.json
# into .claude/settings.local.json (create that file with the example's exact
# contents if it does not exist; merge, never overwrite existing hooks or env
# keys).
agentacct hooks claude-code doctor
agentacct mcp doctor
agentacct usage import-local --client all --dry-run
agentacct serve"""

CLAUDE_CODE_NOTES = (
    "The hook bridge is REQUIRED here: it captures real session/transcript ids at session start (SessionStart) and on every tool call (PreToolUse) — the only path to exact/high-confidence joins between usage and recorded work — and its SessionStart response injects the record-your-work directive into every new session (delivery only: without the `env` block below, delivered directives still record nothing).",
    "Recorded work context needs all three levers of the merged settings, each covering a different failure: the SessionStart hook entry is delivery (without it the directive never reaches the session), `\"env\": {\"ENABLE_TOOL_SEARCH\": \"auto\"}` makes the agentacct MCP tools arrive directly callable (without it they stay deferred and un-primed sessions demonstrably record nothing), and the hook bridge itself supplies the join ids (without it recorded work is never session-linked).",
    "The example settings embed machine-local absolute paths, so `.claude/settings.local.json` (machine-local, not committed) is the right target.",
    "REQUIRED final step: start a NEW Claude Code session (or run `/mcp` to reconnect) after setup — MCP servers and hooks bind at session start, so the session that performed the install cannot see the agentacct tools and records nothing.",
    "`mcp doctor` saying `hook context: absent` right after install is expected — the context appears once the first hooked Claude Code session starts or runs a tool.",
    "Re-running `hooks claude-code install` over existing hook files requires `--force` (do re-run it after upgrades: the wrapper is regenerated in place and its filename never changes, so existing settings keep working).",
)

CODEX_BLOCK = """agentacct init --agent codex --write-mcp
agentacct doctor
agentacct mcp doctor
agentacct usage import-local --client all --dry-run
agentacct serve"""

CODEX_NOTES = (
    "`init --write-mcp` writes the project `.codex/config.toml` MCP block with an absolute store path.",
    "REQUIRED final step: start a NEW Codex session in this repo after install — Codex binds MCP servers at session start, so the agentacct tools never appear in the session that performed the install, and nothing is recorded until the first fresh session.",
    "This project's recorded work context appears on THIS project's dashboard (`agentacct serve` run here); other projects' dashboards will show these sessions as usage-only, labeled with this project's name.",
    "Codex cannot pass its own session id in-session; at usage import, agentacct pairs each agentacct-recorded event with the Codex session log that created it (client-log evidence), so recorded work earns high-confidence session links once the session's rollout is imported — never `exact`, and a section evidenced by more than one session links to none. The client name is implied the same way, so codex agents need not pass `client` explicitly (a wrong explicit name is conflict-vetoed, never trusted).",
    "Register the server as `agentacct` (every agentacct setup path writes that name). Pre-rename `agent-sentinel` registrations are equally recognized forever — existing installs do not need to re-register. Any OTHER custom registration name loses session links — skipped-but-counted in the dashboard insights, never guessed.",
)

MCP_PREVIEW_BLOCK_TEMPLATE = """agentacct init --agent {agent}
agentacct setup mcp --agent {agent}
agentacct mcp doctor
agentacct usage import-local --client all --dry-run
agentacct serve"""


def mcp_preview_block(agent: str) -> str:
    if agent not in MCP_PREVIEW_AGENTS:
        raise ValueError(f"not an MCP-preview agent: {agent}")
    return MCP_PREVIEW_BLOCK_TEMPLATE.format(agent=agent)


MCP_PREVIEW_NOTES = (
    "These clients keep MCP config in profile/global or client-specific locations, so `setup mcp` PREVIEWS the exact registration command instead of writing it.",
    "Show the previewed command to the user and ask before modifying global agent configuration, then paste it into that client's own MCP setup flow.",
    "For an unlisted MCP-capable client, use `--agent generic` to get a portable stdio server definition.",
)

# Blocks that must appear VERBATIM in INSTALL.md (the mcp-preview section is
# written out for hermes; the other preview agents substitute the name).
INSTALL_MD_COMMAND_BLOCKS = {
    "claude-code": CLAUDE_CODE_BLOCK,
    "codex": CODEX_BLOCK,
    "hermes": mcp_preview_block("hermes"),
}


def agent_command_block(agent: str) -> str:
    if agent == "claude-code":
        return CLAUDE_CODE_BLOCK
    if agent == "codex":
        return CODEX_BLOCK
    return mcp_preview_block(agent)


def agent_notes(agent: str) -> tuple[str, ...]:
    if agent == "claude-code":
        return CLAUDE_CODE_NOTES
    if agent == "codex":
        return CODEX_NOTES
    return MCP_PREVIEW_NOTES


SERVE_NOTE = (
    f"`agentacct serve` is long-running: start it in the background or hand the command to the user; dashboard at {DASHBOARD_URL}. "
    "The dry-run import only previews; drop `--dry-run` (or click the dashboard's \"Refresh & save usage\" button) when the user wants the ledger populated — reloading a dashboard page writes nothing (the Overview/Tokens/Sessions pages render saved rows only; the read-only scan runs on the Raw data page)."
)

# --- Optional: global install (single-user machine) ---------------------------
# One machine-wide store instead of per-project stores. Everything below rides
# EXPLICIT absolute --store-dir args baked into the registrations: GUI-launched
# clients (Claude Code desktop, Codex.app) do not inherit shell environment
# variables, so env-var-based store selection cannot be the mechanism.

GLOBAL_INSTALL_SECTION_TITLE = "## Global install (single-user machine, optional)"

GLOBAL_INSTALL_INTRO = (
    "The per-agent sections above install agentacct per repository: each repo gets its own store, dashboard, and MCP registration. "
    "On a single-user machine you can instead register ONE user-scope MCP server, hook, and dashboard against ONE global store — "
    "machine-wide usage AND machine-wide work context on a single dashboard, no per-repo install step. "
    "State then lives in the global store directory below instead of each repository."
)

# Store dir choice: NOT ~/.agent-sentinel — that path was older versions'
# silent fallback store, and reusing it would silently mix legacy data in.
# The "$HOME/.agent-sentinel-global" name itself is frozen (pre-rename):
# existing global installs live there, and store names are plumbing, not brand.
# AGENTACCT_BIN is captured up front WITH a non-empty guard: for a non-PATH
# install, a bare "$(command -v agentacct)" would expand to an EMPTY
# argument and register a silently dead user-scope server.
GLOBAL_INSTALL_BLOCK = """mkdir -p "$HOME/.agent-sentinel-global/state"
AGENTACCT_BIN="$(command -v agentacct)"
[ -n "$AGENTACCT_BIN" ] || AGENTACCT_BIN="$HOME/.agentacct-cli/.venv/bin/agentacct"  # non-PATH install: substitute YOUR absolute path from Step 1 — an empty command would register a dead server
# the two `mcp add` lines below rewrite USER-LEVEL client config (~/.claude.json, ~/.codex/config.toml): show them to the user and ask first (ground rule)
claude mcp add --scope user agentacct -- "$AGENTACCT_BIN" mcp serve --store-dir "$HOME/.agent-sentinel-global/state"
codex mcp add agentacct -- "$AGENTACCT_BIN" mcp serve --store-dir "$HOME/.agent-sentinel-global/state"
agentacct hooks claude-code install --project-dir "$HOME/.agent-sentinel-global" --store-dir "$HOME/.agent-sentinel-global/state" --user-settings-example
# merge the printed "hooks" AND "env" blocks into ~/.claude/settings.json (user-level; ask the user first; merge, never overwrite existing keys)
# standing "record your work" instructions — this is what fills the dashboard with work context (writes ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md; ask the user first, --dry-run to preview)
agentacct setup instructions --agent claude-code --user --store-dir "$HOME/.agent-sentinel-global/state"
agentacct setup instructions --agent codex --user --store-dir "$HOME/.agent-sentinel-global/state"
agentacct serve --store-dir "$HOME/.agent-sentinel-global/state\""""

GLOBAL_INSTALL_NOTES = (
    "The store is `$HOME/.agent-sentinel-global/state` — deliberately NOT the legacy `~/.agent-sentinel` directory, which older versions used as a silent fallback store; reusing it would mix that stray data into global mode. The directory keeps its pre-rename `agent-sentinel` name for compatibility with existing global stores.",
    "Every registration embeds an explicit absolute `--store-dir` on purpose: GUI-launched clients (Claude Code desktop, Codex.app) do not inherit shell environment variables, so `AGENT_CHRONICLE_STORE_DIR` (or its pre-rename alias `AGENT_SENTINEL_STORE_DIR`) is shell convenience only — never the mechanism.",
    "The printed settings example includes `\"env\": {\"ENABLE_TOOL_SEARCH\": \"auto\"}` alongside the hooks — merge that block too (never overwriting env keys the user already has): without it the agentacct MCP tools stay deferred in Claude Code and un-primed sessions record nothing, however well the hooks are wired.",
    "REQUIRED cleanup in every repo you switch to global mode: remove the `agentacct` (or pre-rename `agent-chronicle`/`agent-sentinel`) entry from that repo's `.mcp.json` and the `[mcp_servers.agentacct]` (or pre-rename `[mcp_servers.agent-chronicle]` / `[mcp_servers.agent-sentinel]`) block from its `.codex/config.toml` (and stop merging its per-project hooks block) — a project-scope entry silently shadows the user-scope server and pins that repo's MCP context to its old per-project store.",
    "Point any `usage watch` / `usage import-local` daemons at the same store, ONE watch daemon per store: `agentacct usage watch --store-dir \"$HOME/.agent-sentinel-global/state\"`. By default each session is imported once at first observation and never updated; add `--refresh` if the daemon should keep growing sessions' totals current (the dashboard's \"Refresh & save usage\" replace semantics).",
    "REQUIRED to fill the dashboard with work context: `setup instructions` writes a short, idempotent 'record your work as sections' block into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` (merged inside `<!-- agent-chronicle:begin -->`/`<!-- agent-chronicle:end -->` markers, so your own content is never touched; re-run to update — pre-rename `agent-sentinel:begin` blocks are recognized and replaced — add `--remove` to strip it, `--dry-run` to preview). Without it, global mode records tokens but almost no work context, because global installs no standing instruction the way a per-project setup does.",
    "What you get and what stays behind: one dashboard with machine-wide usage and all NEW work context; MCP context already recorded in per-project stores can be folded in with `agentacct usage merge-store --from <repo>/.agent-sentinel/state --into \"$HOME/.agent-sentinel-global/state\"` (dedup-safe, additive-only, `--dry-run` first) — or browse an old store in place with `agentacct serve --store-dir <repo>/.agent-sentinel/state --port 8790`.",
    "As with every section: start NEW client sessions after registering — running sessions never see newly added MCP servers or hooks.",
)

# --- Recording contract (shared core for every recording surface) --------------
# The SAME recording contract drives two very different surfaces:
#   1. MCP_SERVER_INSTRUCTIONS — returned in the MCP `initialize` result, at the
#      tool layer, where Claude Code's tool-deferral barrier lives;
#   2. WORKFLOW_INSTRUCTION_LINES — merged into a user/project CLAUDE.md /
#      AGENTS.md so the guidance persists across sessions.
# Both must stay directive, honest, and identical about WHAT to record, so the
# per-task bullets live once in _RECORDING_CONTRACT_LINES and both surfaces embed
# them. Only the framing (tool-layer vs. instruction-file) and the deferral hint
# differ, so the two cannot drift on the actual contract.
#
# HARD constraints on this content:
# - It loads into EVERY session (both surfaces cost context tokens), so keep it
#   SHORT.
# - SECTIONS-ONLY (Phase 1 contract): never tell the agent to record
#   task_started/task_completed events.
# - Honest: MCP/event evidence is separate from token/cost claims; never
#   fabricate token or cost numbers.
# - Directive for REAL work, low-friction only for genuinely trivial throwaway
#   sessions (no easy blanket opt-out).
# - No absolute paths / no machine-specific text (mirrored verbatim into docs).

# The per-task directive bullets, shared verbatim by every recording surface.
_RECORDING_CONTRACT_LINES = (
    "- Open a section with `sentinel_record_section` (`section_status=started`) BEFORE your first other tool call, and again before each meaningful task; set `title` to a short human goal (e.g. \"add rate-limit to login\"). Complete it (`section_status=completed`) or block it (`blocked`) when done.",
    "- For a long task, send `section_status=checkpoint` updates rather than one giant section.",
    "- After running tests or a build, record the objective result with `sentinel_record_machine_check`.",
    "- Keep MCP/event evidence separate from token/cost claims: MCP events prove what work happened; a token or cost figure is only real if it comes from actual client usage the importer read — never fabricate one.",
)

# The load-if-deferred hint. This is the KEY line for Claude Code, whose client
# DEFERS MCP tools when many are installed: the sentinel tools arrive listed but
# not directly callable until their schema is loaded. Without this line the agent
# never takes that discovery step and records nothing.
_LOAD_IF_DEFERRED_LINE = (
    "- If these agentacct tools are not directly available, load them first "
    "(they may be provided as searchable/deferred tools) before recording."
)

# The low-friction escape hatch — deliberately narrow so real work is never
# skipped. Directive by omission: everything that is not a genuinely trivial
# throwaway session should be recorded.
_LOW_FRICTION_LINE = (
    "- Low-friction: skip recording only for a genuinely trivial throwaway "
    "session — recording nothing is better than recording noise, but do record "
    "real work."
)


# --- MCP server instructions (returned in the `initialize` result) -------------
# This is the InitializeResult.instructions field the MCP protocol defines for a
# server to describe how to use its tools. It is delivered AT THE TOOL LAYER, so
# it reaches the agent even when the tools themselves are deferred — the exact
# barrier a background CLAUDE.md instruction cannot reliably clear. Keep it
# concise: it costs context tokens on every session.
MCP_SERVER_INSTRUCTIONS = "\n".join(
    (
        "agentacct records what this session actually did — the work, not just tokens — so the local dashboard can show it. Record your work as you go:",
        _LOAD_IF_DEFERRED_LINE,
        *_RECORDING_CONTRACT_LINES,
        _LOW_FRICTION_LINE,
    )
)


def mcp_server_instructions() -> str:
    """The `instructions` string the MCP server returns in its initialize result.

    Single source of truth: ``mcp.py`` imports this so the tool-layer guidance
    never drifts from the recording contract shared with the instruction files.
    """
    return MCP_SERVER_INSTRUCTIONS


# --- SessionStart hook additional context (per-session, injected by the hook) --
# Claude Code SessionStart hooks may return additionalContext that the client
# injects at the START of the conversation — the most prominent instruction
# channel available (a background CLAUDE.md block and even the tool-layer MCP
# instructions demonstrably fail to make un-primed sessions record). Delivered
# by `hooks claude-code session-start`, so it reaches every hooked session with
# zero model cooperation required for DELIVERY; recording itself still needs
# the agent to act, which is exactly what this text directs.
SESSION_START_ADDITIONAL_CONTEXT = "\n".join(
    (
        "agentacct is installed on this machine: it records what each session actually did — the work, not just tokens — for a local dashboard. Record your work in this session:",
        _LOAD_IF_DEFERRED_LINE,
        *_RECORDING_CONTRACT_LINES,
        _LOW_FRICTION_LINE,
    )
)


def session_start_additional_context() -> str:
    """The additionalContext string the SessionStart hook returns.

    Single source of truth: ``hooks.py`` imports this so the session-start
    guidance never drifts from the recording contract shared with the MCP
    server instructions and the instruction files.
    """
    return SESSION_START_ADDITIONAL_CONTEXT


# --- Standing workflow instructions (per-session, user or project level) -------
# In global mode agentacct installs the MCP server, hooks, and store but NOT any
# standing "record your work" instruction, so agents never open sections and the
# global dashboard shows tokens with almost no work context. These blocks are the
# fix: a short instruction merged into a user- or project-level agent
# instructions file so every session records sections.

# Idempotent merge markers. Re-running `setup instructions` replaces ONLY the
# text between these markers; `--remove` strips it; content outside is untouched.
# Two comment styles because Codex AGENTS.md is Markdown (HTML comments render
# invisibly) and CLAUDE.md is Markdown too — a single HTML-comment marker works
# for both. Kept as one constant so the merge logic has a single source.
INSTRUCTIONS_BEGIN_MARKER = "<!-- agent-chronicle:begin (managed block — edit via `agent-chronicle setup instructions`) -->"
INSTRUCTIONS_END_MARKER = "<!-- agent-chronicle:end -->"
# Pre-rename marker pair: RECOGNIZED FOREVER (user files sync across machines
# upgrading at different times). Rewrites always WRITE the new pair, so any
# re-run of `setup instructions` migrates a legacy block in place; `--remove`
# strips either pair. Mixed pairs (old begin + new end) are accepted
# defensively.
LEGACY_INSTRUCTIONS_BEGIN_MARKER = "<!-- agent-sentinel:begin (managed block — edit via `agent-sentinel setup instructions`) -->"
LEGACY_INSTRUCTIONS_END_MARKER = "<!-- agent-sentinel:end -->"
INSTRUCTIONS_BEGIN_MARKERS = (INSTRUCTIONS_BEGIN_MARKER, LEGACY_INSTRUCTIONS_BEGIN_MARKER)
INSTRUCTIONS_END_MARKERS = (INSTRUCTIONS_END_MARKER, LEGACY_INSTRUCTIONS_END_MARKER)

# The instruction body. Same guidance for every agent (the recording contract is
# client-agnostic); the surrounding heading differs only in the target filename.
# Shares the directive bullets with MCP_SERVER_INSTRUCTIONS so the two surfaces
# cannot drift on the contract; adds the load-if-deferred hint (Claude Code defers
# MCP tools) and a narrow low-friction note without inviting a blanket skip.
WORKFLOW_INSTRUCTION_LINES = (
    "Record what this session does so the dashboard can show it, not just tokens:",
    _LOAD_IF_DEFERRED_LINE,
    *_RECORDING_CONTRACT_LINES,
    _LOW_FRICTION_LINE,
)

WORKFLOW_INSTRUCTION_HEADING = "## agentacct — record your work"


def workflow_instruction_body() -> str:
    """The heading + bullet body, WITHOUT the idempotency markers."""
    return WORKFLOW_INSTRUCTION_HEADING + "\n\n" + "\n".join(WORKFLOW_INSTRUCTION_LINES)


def workflow_instruction_block() -> str:
    """The full managed block: begin marker, body, end marker.

    This exact text is what `setup instructions` writes between the markers and
    what INSTALL.md embeds verbatim (a consistency test compares them).
    """
    return "\n".join(
        (
            INSTRUCTIONS_BEGIN_MARKER,
            workflow_instruction_body(),
            INSTRUCTIONS_END_MARKER,
        )
    )


# Default user-level instruction file per agent (resolved against the user's
# home at call time; tests and non-standard setups override with --path).
INSTRUCTION_USER_FILES = {
    "claude-code": ".claude/CLAUDE.md",
    "codex": ".codex/AGENTS.md",
}

# Default project-level instruction file per agent (relative to the repo root).
INSTRUCTION_PROJECT_FILES = {
    "claude-code": "CLAUDE.md",
    "codex": "AGENTS.md",
}

INSTRUCTION_AGENTS = tuple(INSTRUCTION_USER_FILES)


def _line_marker(line: str, markers: tuple[str, ...]) -> bool:
    """True when a marker line is a REAL managed marker: one of the recognized
    marker strings (new or pre-rename) sits at the start of the line (ignoring
    leading whitespace) and nothing but whitespace follows it. Markers
    appearing mid-line (e.g. quoted inside a sentence) never count — they are
    ordinary user content."""

    return line.strip() in markers


def _fence_token(line: str) -> str | None:
    """The fence delimiter (``` or ~~~) opening/closing a Markdown code fence on
    this line, or None. A fence marker is >=3 of the same char at line start
    (after optional indentation); the info string after it is ignored."""

    stripped = line.lstrip()
    for fence in ("```", "~~~"):
        if stripped.startswith(fence):
            return fence[0] * 3
    return None


def _split_around_managed_block(text: str) -> tuple[str, bool]:
    """Return (text_with_managed_block_removed, block_was_present).

    Everything between a recognized begin marker and a recognized end marker
    (inclusive) is removed; content outside the markers is preserved verbatim.
    BOTH marker generations are recognized — the new `agent-chronicle` pair
    and the pre-rename `agent-sentinel` pair (mixed pairs accepted
    defensively) — while writes always emit the new pair, so re-running
    `setup instructions` migrates a legacy block in place.

    Marker detection is ROBUST against user content that merely QUOTES the
    marker strings (e.g. a user documenting this feature inside a fenced code
    block in their CLAUDE.md/AGENTS.md). A marker occurrence counts as a real
    managed marker ONLY when it is on its own line at column 0 (ignoring leading
    whitespace) AND is NOT inside a fenced code block (``` / ~~~ fences are
    tracked while scanning). Markers inside a fence, or mid-line, are treated as
    ordinary user content and never define or delete a block.

    Only the FIRST well-formed block (a real begin line followed by a later real
    end line, both outside fences) is recognized; anything else — a dangling
    begin without an end, an end before any begin, or markers only inside fences
    — is treated as "no managed block" so we never corrupt hand-edited files.
    """

    lines = text.split("\n")
    begin_idx: int | None = None
    end_idx: int | None = None
    in_fence = False
    open_fence: str | None = None
    for idx, line in enumerate(lines):
        fence = _fence_token(line)
        if fence is not None:
            if not in_fence:
                in_fence = True
                open_fence = fence
            elif fence == open_fence:
                in_fence = False
                open_fence = None
            # A different fence char inside an open fence is literal content.
            continue
        if in_fence:
            continue
        if begin_idx is None:
            if _line_marker(line, INSTRUCTIONS_BEGIN_MARKERS):
                begin_idx = idx
            continue
        if _line_marker(line, INSTRUCTIONS_END_MARKERS):
            end_idx = idx
            break

    if begin_idx is None or end_idx is None:
        return text, False

    # Reassemble from lines, dropping the block (inclusive). Rejoining with "\n"
    # reproduces the original bytes for every untouched line.
    before = "\n".join(lines[:begin_idx])
    after = "\n".join(lines[end_idx + 1 :])
    if before and after:
        combined = before + "\n" + after
    else:
        combined = before + after
    # Collapse the blank line that separated the block from surrounding content
    # so repeated add/remove cycles do not accrete blank lines.
    if before.endswith("\n") and after.startswith("\n"):
        combined = before.rstrip("\n") + "\n" + after.lstrip("\n")
    return combined, True


def instruction_file_has_managed_block(text: str) -> bool:
    """True when the text carries a well-formed managed block (same detection
    as the renderer: real markers only, fenced/quoted markers never count).
    The CLI uses this to distinguish a FRESH instruction install (which may
    auto-record a command-time instrumentation marker) from a re-run over an
    already-installed file (which must not re-date the install)."""

    return _split_around_managed_block(text)[1]


def render_instruction_file(existing_text: str, *, remove: bool) -> str:
    """Idempotently add/replace/remove the managed block in a file's text.

    - remove=False: strip any existing managed block, then append a fresh one at
      the end (so re-running is a pure replace of the marked region; a file with
      no block gains one; content outside the markers is never touched).
    - remove=True: strip the managed block and leave everything else intact.

    The returned text always ends with exactly one trailing newline when
    non-empty, matching how editors save these files.
    """
    stripped, _ = _split_around_managed_block(existing_text)
    if remove:
        result = stripped.rstrip("\n")
        return result + "\n" if result else ""
    base = stripped.rstrip("\n")
    block = workflow_instruction_block()
    if base:
        return base + "\n\n" + block + "\n"
    return block + "\n"


# --- Honest close: what this setup can and cannot claim -----------------------

CAPABILITY_MATRIX_TITLE = "What this setup can and cannot claim"

CAPABILITY_MATRIX = (
    "Claude Code: automatic high-confidence joins between usage and recorded work via the installed hook bridge (SessionStart and PreToolUse capture real session/transcript ids). Exact attribution still requires ids authored explicitly on the recording call; hook-derived ids are not bound to that MCP request. Recorded work context needs all three levers: the merged hooks settings entry including SessionStart (delivery — the SessionStart hook is the only path proven to make un-primed sessions record work, and it adds session-start/resume id capture; PreToolUse still captures the session/transcript ids on every tool call), `ENABLE_TOOL_SEARCH=auto` in the settings `env` block (discoverability — without it the agentacct tools stay deferred and un-primed sessions record nothing), and the hook bridge itself (join keys — without it recorded sections fall back to project-level context, never session-linked).",
    "Codex: session-linked work context via client-log evidence (high) — at usage import, agentacct pairs each agentacct-recorded event with the Codex session log that created it (creation responses only, never read-tool echoes). Sections still never earn `exact` (the link is evidenced from the log at import time, not client-authored in-session), a section evidenced by more than one session links to none, and sessions whose rollouts have not been imported fall back to project-level context only.",
    "Hermes: local `state.db` usage import plus a manual MCP registration preview; agentacct does not yet install Hermes mechanical hooks.",
    "OpenCode: captured/exported JSON usage import plus a manual MCP registration preview; agentacct does not yet parse the current official SQLite store or install a realtime plugin.",
    "OpenClaw: local JSONL usage import plus a manual MCP registration preview; agentacct does not yet join `sessions.json` routing metadata or install typed plugin hooks.",
    "Cursor: the primary `User/globalStorage/state.vscdb` can produce observation-only composer sessions through an explicit local import/refresh. It never emits usage or cost, never scans backups or ai-tracking stores, and onboarding does not install or activate it. Metadata-only hook payload normalization remains a separate manual primitive.",
    "Generic MCP clients: recorded work context only unless a separate trusted usage importer exists; join confidence depends on ids the client actually exposes.",
    "Mechanical Claude Code/Codex/Cursor capture, when manually wired, writes Evidence v2 and projects bounded session activity into the homepage as an observed Task with models/checks when present. It is not activated by onboarding, does not report token/cost truth, and does not invent named work steps; MCP remains the richer semantic source.",
    "All clients: imported tokens are client_reported (read from the client's own local session files); costs are estimates from a local pricing table — never provider invoices.",
    "Always: local-first, observe-only, no telemetry, no provider API keys stored or requested.",
)

RELAY_INSTRUCTION = (
    "Finish the installation by relaying this capability matrix to the user, "
    "along with the files you changed, whether `mcp doctor` passed, and the dashboard command."
)


def capability_matrix_text() -> str:
    return "\n".join(f"- {line}" for line in CAPABILITY_MATRIX)


# --- Prompt assembly -----------------------------------------------------------


def one_line_prompt(agent: str | None = None) -> str:
    """The short prompt. Identical for every agent by design: the per-agent
    branching lives in INSTALL.md, not in the line the user has to copy."""
    if agent is not None and agent not in PROMPT_AGENTS:
        raise ValueError(f"unsupported agent target: {agent}")
    return ONE_LINE_PROMPT


def full_prompt(agent: str) -> str:
    """Self-contained offline install prompt for one agent.

    Assembled from the exact constants INSTALL.md embeds, so it cannot drift
    from the runbook. Use when the target agent cannot (or should not) fetch
    INSTALL.md from the network.
    """
    if agent not in PROMPT_AGENTS:
        raise ValueError(f"unsupported agent target: {agent}")
    notes = "\n".join(f"- {note}" for note in agent_notes(agent))
    rules = "\n".join(f"- {rule}" for rule in GROUND_RULES)
    preamble = "\n".join(PREAMBLE_LINES)
    return f"""Install and set up agentacct in this repository, in observe-only mode. agentacct is public: install it with `pipx install agentacct` (or `pipx install git+https://github.com/mikehasa/agentacct`). You are the installing agent: run the commands yourself, from the repository root.

{preamble}

Hard rules:
{rules}

Step 1 — install the CLI (skip if `agentacct` already runs):

```bash
{CLI_INSTALL_PIPX_BLOCK}
```

or `{CLI_INSTALL_UV_BLOCK}`. Fallback without pipx/uv:

```bash
{CLI_INSTALL_FALLBACK_BLOCK}
```

Verify: `{CLI_PATH_CHECK_BLOCK}`. {CLI_PATH_CHECK_NOTE}

Step 2 — from the repository root, run:

```bash
{agent_command_block(agent)}
```

Notes:
{notes}
- {SERVE_NOTE}

{CAPABILITY_MATRIX_TITLE}:
{capability_matrix_text()}

{RELAY_INSTRUCTION}
"""
