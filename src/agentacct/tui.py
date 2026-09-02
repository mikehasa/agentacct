"""The ``agentacct tui`` — a terminal mirror of the macOS work-receipt app.

A keyboard-native, four-pane terminal UI over the same authoritative local event
log the CLI and macOS app read (no credentials, no API calls). It shares the GUI's
design language: one cobalt accent, rationed green/amber/coral, and the evidence
grammar where a pip's SHAPE carries the tier (``◉ ● ◐ ○``) and colour is never the
only channel. Panes:

* **Dashboard** — the shift brief: what needs you (attention), how the agents are
  doing (recent work), and headroom/usage/trust at a glance.
* **Work** — receipts as a table (decision × evidence, cost), master→detail into
  one Task's full Work Receipt.
* **Usage** — provider capacity meters + recorded usage.
* **Sources** — what feeds the store and how well (ingestion health).

Presentation only: the numbers, vocabulary, and honesty rules come verbatim from
the shared modules (``usage_snapshot``, ``receipt``, ``work_ledger``,
``ingestion_health``), so this surface can never disagree with the CLI or app.

Refresh model (two timers): every ``refresh_seconds`` re-read the event log and,
only when its append-only count changed (or a refresh was forced), rebuild the
active pane; every second re-tick just the reset countdowns and "as of" ages.

Headless-testable with Textual's ``App.run_test()`` — see ``tests/test_tui.py``.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from rich.markup import escape as _escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import ContentSwitcher, Input, ListItem, ListView, Static

from .service import SentinelService
from .usage_snapshot import (
    ClientLimit,
    LiveSnapshot,
    UsagePage,
    build_client_limits,
    build_live_snapshot,
    build_usage_page,
    cost_text,
    format_tokens,
    humanize_seconds,
    limit_is_stale,
)

# One shared change key for every fingerprint-keyed cache (TUI + glance API);
# owned by glance.py so the two surfaces can never drift.
from .glance import events_fingerprint as _events_fingerprint  # noqa: E402

# ============================================================================ #
# Design tokens — the DESIGN.md v10 palette, both themes, verbatim.            #
# ============================================================================ #
#
# Chrome (backgrounds, borders, the active-tab fill) is styled through Textual
# theme variables in CSS, so it recolours with the theme automatically. Semantic
# CONTENT colour — evidence pips, decision badges, meters, cost grammar — is
# emitted as Rich markup with explicit hex from the ACTIVE palette below, and the
# whole surface re-renders on a theme switch. One palette per theme; the two must
# stay a mirror of the Swift ``Theme.Palette``.

_DARK: dict[str, str] = {
    "bg": "#0D1215", "chrome": "#141B1F", "panel": "#1B252A", "sel": "#223049",
    "line": "#313D44", "hair": "#2C363C",
    "ink": "#F2F4F3", "muted": "#A5B0B4", "dim": "#71808A",
    "accent": "#82A6FF", "green": "#78D5A8", "amber": "#E7C66A", "coral": "#FF9B88",
    "ta": "#24365C", "tg": "#1E3B2F", "tm": "#3D3420", "tc": "#412620",
    "tn": "#2A343B", "chip": "#232E34", "spark": "#31456F",
}
_LIGHT: dict[str, str] = {
    "bg": "#F4F1E9", "chrome": "#FCFBF7", "panel": "#FFFFFF", "sel": "#E7EDF8",
    "line": "#DDDACF", "hair": "#E4E1D7",
    "ink": "#171A1D", "muted": "#59636B", "dim": "#79848B",
    "accent": "#245BDB", "green": "#1F7653", "amber": "#7A5A00", "coral": "#B63F2F",
    "ta": "#E8EEFB", "tg": "#E2F0E9", "tm": "#F7EFDA", "tc": "#F8E5E1",
    "tn": "#EDEBE3", "chip": "#F7F5F0", "spark": "#B9CBF2",
}


def _theme(name: str, pal: dict[str, str], dark: bool) -> Theme:
    return Theme(
        name=name,
        dark=dark,
        primary=pal["accent"],
        secondary=pal["muted"],
        accent=pal["accent"],
        foreground=pal["ink"],
        background=pal["bg"],
        surface=pal["chrome"],
        panel=pal["panel"],
        success=pal["green"],
        warning=pal["amber"],
        error=pal["coral"],
        variables={
            "text-muted": pal["muted"],
            "block-cursor-background": pal["sel"],
            "block-cursor-foreground": pal["ink"],
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": pal["sel"],
            "block-cursor-blurred-foreground": pal["ink"],
            "border": pal["line"],
            "sel": pal["sel"],
        },
    )


_THEME_DARK = _theme("agentacct-dark", _DARK, dark=True)
_THEME_LIGHT = _theme("agentacct-light", _LIGHT, dark=False)


# ============================================================================ #
# The shared evidence / decision vocabulary — ONE table, mirroring the Swift    #
# EvidenceTierStyle + DecisionTintClass. Replaces the old six ad-hoc helpers.   #
# Every function takes the active palette so it recolours with the theme.       #
# ============================================================================ #

# Evidence tier grade -> (pip glyph, palette colour key, display label).
# Shape is the tier; colour is redundant (v10 rule 1). ``◉`` = a filled disc in
# a ring, ``●`` filled, ``◐`` half, ``○`` hollow.
_TIER: dict[str, tuple[str, str, str]] = {
    "externally_verified": ("◉", "green", "externally-verified"),
    "independently_checked": ("●", "ink", "independently-checked"),
    "self_checked": ("◐", "accent", "self-checked"),
    "claimed": ("○", "amber", "claimed"),
    "unchecked": ("○", "amber", "unchecked"),
    "none": ("○", "dim", "none"),
}


def tier_style(grade: str | None) -> tuple[str, str, str]:
    return _TIER.get(str(grade or "none"), ("○", "dim", str(grade or "none")))


def pip(grade: str | None, pal: dict[str, str]) -> str:
    """The evidence pip for a grade — shape carries the tier, colour is redundant."""

    glyph, key, _label = tier_style(grade)
    return f"[{pal[key]}]{glyph}[/]"


# Decision key families (Swift DecisionTintClass). (text colour, wash key or None).
# ``None`` wash = a live state, rendered as bare accent text (a terminal cannot
# draw the GUI's outline); every settled class wears a filled wash.
_DANGER = {"blocked", "failed", "finding"}
_LIVE = {"in_progress", "started", "checkpoint"}
_CLAIMED = {
    "reported", "resolved", "mostly_done", "handed_off", "finding_superseded",
    "finding_resolved_by_user", "blocker_resolved_by_user",
}
_INFERRED = {"ended_open"}
_VERIFIED = {"verified"}

# Work lifecycle tabs, verbatim from the Swift WorkGroup.forKey / forTask
# (apps/agentacct/Sources/agentacct/WorkPane.swift): a task lands in exactly one
# bucket, and ANY task with a currently-failing check escalates to Attention (the
# checks_failed rule) unless the finding is already settled.
_WORK_TABS: tuple[tuple[str, str], ...] = (
    ("all", "All"), ("attention", "Attention"), ("verified", "Verified"),
    ("reported", "Reported"), ("in_progress", "In progress"),
    ("observed", "Observed"), ("stopped", "Stopped"), ("other", "Other"),
)
_FORKEY_BUCKET: dict[str, str] = {
    "verified": "verified",
    "reported": "reported", "resolved": "reported", "mostly_done": "reported",
    "finding_superseded": "reported", "finding_resolved_by_user": "reported",
    "blocker_resolved_by_user": "reported",
    "in_progress": "in_progress", "started": "in_progress", "checkpoint": "in_progress",
    "observed": "observed",
    "handed_off": "stopped", "ended_open": "stopped", "inactive": "stopped",
}
_ATTENTION_KEYS = {"finding", "failed", "blocked"}
_SETTLED_FINDING_KEYS = {"finding_superseded", "finding_resolved_by_user"}


def needs_attention(decision_key: str | None, checks_failed: int | None) -> bool:
    """A task needs the user when its decision is a danger key, OR any check is
    currently failing and the finding is not already settled (Swift
    ``workReceiptNeedsAttention``)."""

    key = str(decision_key or "")
    return key in _ATTENTION_KEYS or (int(checks_failed or 0) > 0 and key not in _SETTLED_FINDING_KEYS)


def task_bucket(summary: dict) -> str:
    """The single lifecycle bucket a receipt-summary belongs to (Swift forTask)."""

    decision = summary.get("decision_status") or {}
    key = str(decision.get("key") or "")
    checks_failed = int((summary.get("evidence_strength") or {}).get("checks_failed") or 0)
    if needs_attention(key, checks_failed):
        return "attention"
    return _FORKEY_BUCKET.get(key, "other")

# Human labels for the decision keys (raw keys stay in the data).
_DECISION_LABEL: dict[str, str] = {
    "verified": "Verified",
    "in_progress": "In progress",
    "started": "In progress",
    "checkpoint": "In progress",
    "blocked": "Blocked",
    "failed": "Failed",
    "finding": "Open finding",
    "reported": "Agent reported",
    "resolved": "Resolved",
    "mostly_done": "Mostly done",
    "handed_off": "Handed off",
    "ended_open": "Ended open",
    "inactive": "Inactive",
    "finding_superseded": "Superseded",
    "finding_resolved_by_user": "Resolved (reviewed)",
    "blocker_resolved_by_user": "Resolved (reviewed)",
}


def decision_label(key: str | None) -> str:
    key = str(key or "")
    return _DECISION_LABEL.get(key, key.replace("_", " ").capitalize() or "—")


def _decision_colors(key: str, pal: dict[str, str]) -> tuple[str, str | None]:
    if key in _DANGER:
        return pal["coral"], pal["tc"]
    if key in _LIVE:
        return pal["accent"], pal["ta"]  # live: accent text on accent wash (a filled chip)
    if key in _CLAIMED:
        return pal["accent"], pal["ta"]
    if key in _INFERRED:
        return pal["amber"], pal["tm"]
    if key in _VERIFIED:
        return pal["green"], pal["tg"]
    # inactive + unknown → a quiet neutral badge: ink text on the neutral wash
    # (Swift DecisionTintClass.neutral) — never green, never alarming.
    return pal["ink"], pal["tn"]


def decision_badge(key: str | None, pal: dict[str, str], label: str | None = None) -> str:
    """A decision badge as a terminal chip: coloured text on a tint wash (or bare
    accent for a live state). No pip — the decision axis carries no evidence
    shape. Label text is fixed-vocabulary; any caller-supplied text is escaped."""

    key = str(key or "")
    text = _escape(label if label is not None else decision_label(key))
    fg, wash = _decision_colors(key, pal)
    if wash is None:
        return f"[b {fg}]{text}[/]"
    return f"[{fg} on {wash}] {text} [/]"


def check_mark(result: str | None, pal: dict[str, str]) -> tuple[str, str]:
    """(glyph, colour) for a machine-check result — the same vocabulary the GUI
    checks list uses: ✓ passed / ✗ failed·error / » skipped / • other."""

    r = str(result or "").lower()
    if r == "passed":
        return "✓", pal["green"]
    if r in ("failed", "error"):
        return "✗", pal["coral"]
    if r == "skipped":
        return "»", pal["amber"]
    return "•", pal["dim"]


def meter(fraction: float, width: int, pal: dict[str, str]) -> str:
    """A clean capacity meter as block characters — a filled run over a faint
    track, no tick glyphs (they read as noise at terminal widths; the exact
    percent sits beside the bar). Fill colour follows the v10 threshold rule
    (accent < 75% ≤ amber < 100% ≤ coral). ``fraction`` is 0..1 (clamped)."""

    frac = max(0.0, min(1.0, float(fraction)))
    width = max(4, int(width))
    filled = round(width * frac)
    pct = frac * 100
    fill_col = pal["coral"] if pct >= 100 else pal["amber"] if pct >= 75 else pal["accent"]
    return f"[{fill_col}]{'█' * filled}[/][{pal['line']}]{'░' * (width - filled)}[/]"


def caps(text: str, pal: dict[str, str]) -> str:
    """A caps eyebrow label — the v10 label species (dim, spaced, uppercase)."""

    return f"[{pal['dim']}]{_escape(text.upper())}[/]"


_MARKUP_TAG = re.compile(r"\[/?[^\]]*\]")


def _plainlen(markup: str) -> int:
    """Visible width of a Rich-markup string — tags stripped, escaped ``\\[``
    counted as one column. Used to right-justify a two-edge row by hand (a Static
    has no built-in justify)."""

    stripped = _MARKUP_TAG.sub("", markup)
    return len(stripped.replace("\\[", "["))


def _two_edge(left: str, right: str, width: int) -> str:
    """Left content + right content flush to ``width`` (the artifact's two-edge
    rows: a label on the left, a status/total pinned to the right)."""

    pad = max(1, width - _plainlen(left) - _plainlen(right))
    return f"{left}{' ' * pad}{right}"


# ---- cost grammar (v10 rule 5: every cost carries its basis) --------------- #

_REPORTED_BASES = {"client_reported", "provider_billed"}


def cost_display(
    usd: float | None,
    *,
    complete: bool | None,
    confidence: str | None,
    known_additive: float | None = None,
) -> str | None:
    """The app-wide cost grammar: ``$`` complete+reported, ``≈$`` complete
    estimate, ``~$`` known-partial subtotal, ``None`` when nothing is priced so
    the caller names the absence — never a fabricated ``$0``."""

    reported = str(confidence or "") in _REPORTED_BASES
    if complete and usd is not None:
        return _money(usd, "$" if reported else "≈$")
    if known_additive is not None:
        return _money(known_additive, "~$")
    if usd is not None:
        return _money(usd, "≈$")
    return None


def _money(value: float, prefix: str) -> str:
    return f"{prefix}{float(value):,.2f}"


def receipt_cost_text(cost: dict) -> str:
    """A receipt-summary cost dict → the honest cost string, or a named absence."""

    shown = cost_display(
        cost.get("estimated_cost_usd"),
        complete=cost.get("cost_complete"),
        confidence=cost.get("cost_confidence") or cost.get("cost_basis"),
        known_additive=cost.get("known_additive_cost_usd"),
    )
    return shown if shown is not None else "unpriced"


def abbr_tokens(value: Any) -> str:
    """A compact token count for the airy Dashboard headlines — ``2.4B`` /
    ``136.5M`` / ``22.5K`` — where a full thousands-separated integer would crowd
    the block. The Usage pane keeps the exact ``format_tokens`` count; this is a
    presentation shorthand only, never a different number."""

    if not (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return "—"
    n = float(value)
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= scale:
            trimmed = f"{n / scale:.1f}".rstrip("0").rstrip(".")
            return f"{trimmed}{suffix}"
    return f"{int(n):,}"


# Attention-reason kind → the leading reason word the Dashboard's primary card
# shows (mirrors build_attention_reason's "kind"); provenance source token → a
# human label for the "recorded via" cell.
_ATTENTION_REASON_LABEL: dict[str, str] = {
    "failed_check": "Failed check",
    "failed_step": "Failed step",
    "blocker": "Blocked",
}
_PROVENANCE_LABEL: dict[str, str] = {
    "mcp": "MCP record",
    "client_log": "Client log",
    "hook": "Client hook",
    "transcript_scan": "Transcript scan",
    "none": "—",
}


# ============================================================================ #
# Small kept helpers (data-layer adjacent; unit-tested directly).              #
# ============================================================================ #

_WINDOW_CYCLE: tuple[str, ...] = ("today", "7d", "30d", "all")
# The trailing ranges the Usage pane's `d` key cycles: (days, label). None = all.
_USAGE_RANGE_CYCLE: tuple[tuple[int | None, str], ...] = ((7, "7d"), (30, "30d"), (90, "90d"), (None, "all"))
_AUTO_IMPORT_ENV = "AGENTACCT_TUI_AUTO_IMPORT"
_RECEIPTS_LIMIT = 300
_SESSIONS_LIMIT = 500


def _auto_import_enabled() -> bool:
    value = os.environ.get(_AUTO_IMPORT_ENV)
    if value is None:
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


def _humanize_ago(ts: Any, now: float) -> str:
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0 and ts <= now:
        return f"{humanize_seconds(now - ts)} ago"
    return "—"


def sparkline(values: list[float], pal: dict[str, str]) -> str:
    """A block-character sparkline; the last bar is emphasised in accent, the
    prior few dimmed accent, the rest faint — the endpoint-emphasis a TUI can
    draw without a chart. Two cells per bar so short series still read."""

    ramp = " ▁▂▃▄▅▆▇█"
    nums = [max(0.0, float(v)) for v in values]
    if not nums:
        return f"[{pal['dim']}]no usage recorded[/]"
    peak = max(nums) or 1.0
    out: list[str] = []
    n = len(nums)
    for i, v in enumerate(nums):
        level = max(1, round(v / peak * 8))
        col = pal["accent"] if i == n - 1 else pal["spark"] if i >= n - 3 else pal["line"]
        out.append(f"[{col}]{ramp[level] * 2}[/]")
    return "".join(out)


# ============================================================================ #
# The help overlay.                                                            #
# ============================================================================ #

_HELP_ROWS: tuple[tuple[str, str], ...] = (
    ("1 – 4", "switch pane · Dashboard / Work / Usage / Sources"),
    ("↑ ↓ / j k", "move cursor · detail follows"),
    ("↵", "open · drill into a receipt"),
    ("esc", "close this help overlay"),
    ("/", "filter the current list (Work)"),
    ("[ ]", "previous / next status tab (Work)"),
    ("s", "cycle sort — attention / latest / cost (Work)"),
    ("d", "cycle range — 7d / 30d / 90d / all (Usage)"),
    ("T", "cycle theme · dark / light / auto"),
    ("r", "refresh · re-import from client logs"),
    ("p", "snapshot · save a shareable SVG"),
    ("q", "quit"),
)


class HelpScreen(ModalScreen):
    """A discoverable keymap overlay — the single biggest usability jump for a
    keyboard-native tool (there was no in-app help before)."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        pal = getattr(self.app, "pal", _DARK)
        lines = [f"[b {pal['ink']}]Keyboard[/]    [{pal['dim']}]esc to close[/]", ""]
        for key, desc in _HELP_ROWS:
            lines.append(f"  [b {pal['accent']}]{key:<12}[/][{pal['muted']}]{_escape(desc)}[/]")
        yield Vertical(Static("\n".join(lines), id="help-body"), id="help-box")

    def action_dismiss(self, result: Any = None) -> None:  # noqa: D401
        self.app.pop_screen()


# ============================================================================ #
# The application.                                                             #
# ============================================================================ #

_PANES: tuple[tuple[str, str], ...] = (
    ("dashboard", "Dashboard"),
    ("work", "Work"),
    ("usage", "Usage"),
    ("sources", "Sources"),
)

# Contextual keybind hints shown in the status bar per pane. (key, label) pairs;
# rendered with the key in accent. Global keys (?, q) are appended.
_PANE_HINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "dashboard": (("1-4", "pane"),),
    "work": (("↑↓", "move"), ("↵", "open"), ("/", "filter"), ("[ ]", "status"), ("s", "sort")),
    "usage": (("d", "range"), ("↑↓", "scroll")),
    "sources": (("↑↓", "source"),),
}


class AgentAcctTUI(App):
    """The agentacct terminal app — a four-pane mirror of the macOS work-receipt
    view over the same local event log."""

    CSS = """
    Screen { background: $background; }

    /* The chrome stacks vertically (NOT docked): docking a top bar above a
       ContentSwitcher intermittently dropped it from an offscreen screenshot on
       a pane switch. Plain vertical stacking keeps it the first row everywhere. */
    #topbar { height: 1; padding: 0 2; background: $surface; color: $foreground; }
    #toprule { height: 1; background: $surface; color: $border; }
    #statusbar { height: 1; padding: 0 2; background: $surface; color: $text-muted; }

    #switcher { height: 1fr; }

    /* Panes are plain scroll containers styled here — never custom widget
       subclasses with DEFAULT_CSS (that hangs Textual's layout solver here). */
    .pane { height: 1fr; padding: 1 2; }
    .pane-block { height: auto; padding: 0 0 1 0; }

    /* Panel cards: a filled surface with the title notched into the top border
       (the lazygit look, mirroring the GUI's cards). A Static can carry both a
       border and a border_title, so each card is one bordered Static. */
    .card {
        width: 1fr; background: $panel; height: auto; padding: 1 2; margin: 1 0 0 0;
        border: round $border; border-title-color: $text-muted; border-title-style: bold;
    }
    #dash-head { height: auto; padding: 0 1 1 1; }
    #dash-hero { height: auto; }
    /* Accent cards keep the subtle hairline but gain a coloured left edge + title
       (matching the GUI's edge-ruled cards) rather than a heavy full-colour box. */
    #dash-attention { border-left: outer $error; border-title-color: $error; }
    #dash-rail { margin-left: 2; }

    /* Work detail (right of the split) + Usage/Sources heads. */
    #work-detail-head { height: auto; padding: 0 1 1 1; }
    #work-outcome { border-left: outer $primary; border-title-color: $primary; }
    /* The detail cards are the densest region (they must fit the tool bars +
       footer in one screen), so they keep the horizontal breathing room but drop
       the vertical padding the other cards gain. */
    #work-outcome, #work-summary, #work-dimensions, #work-steps { padding: 0 2; }
    #usage-head, #sources-head { height: auto; padding: 0 1 1 1; }

    /* Work master/detail split. */
    /* A slim, muted filter (not a prominent boxed input) so the receipt cards
       start near the top of the column, closer to the mockup's density. */
    #work-filter { height: 1; margin: 0 0 1 0; border: none; background: $panel; color: $text-muted; padding: 0 1; }
    #work-filter:focus { background: $block-cursor-background; }
    #work-split { height: 1fr; }
    #work-detail { width: 1fr; height: 1fr; padding: 0 0 0 2; }

    /* Master: a selectable card list (not a table). Each receipt is a padded
       ListItem; the highlighted one wears the accent edge + selected wash. */
    #work-list { width: 46%; height: 1fr; background: $background; }
    #work-list > ListItem { padding: 0 1 0 1; height: auto; background: $background; }
    #work-list > ListItem > Static { padding: 1 1; border-left: wide $background; }
    #work-list > ListItem.-highlight, #work-list > ListItem.--highlight { background: $block-cursor-background; }
    #work-list > ListItem.-highlight > Static,
    #work-list > ListItem.--highlight > Static { background: $block-cursor-background; border-left: wide $primary; }

    HelpScreen { align: center middle; }
    #help-box {
        width: 72; height: auto; padding: 1 2;
        background: $panel; border: round $primary;
    }
    #help-body { height: auto; }
    """

    BINDINGS = [
        Binding("1", "show_pane('dashboard')", "Dashboard"),
        Binding("2", "show_pane('work')", "Work"),
        Binding("3", "show_pane('usage')", "Usage"),
        Binding("4", "show_pane('sources')", "Sources"),
        Binding("question_mark", "help", "Help"),
        Binding("r", "refresh", "Refresh"),
        Binding("T", "cycle_theme", "Theme"),
        Binding("p", "screenshot", "Snapshot"),
        Binding("q", "quit", "Quit"),
        # Work-pane keys (gated to the Work pane in their actions).
        Binding("left_square_bracket", "work_status(-1)", "Prev status", show=False),
        Binding("right_square_bracket", "work_status(1)", "Next status", show=False),
        Binding("s", "work_sort", "Sort", show=False),
        Binding("slash", "work_filter", "Filter", show=False),
        Binding("d", "usage_range", "Range", show=False),
        Binding("escape", "steps_back", "Back", show=False),
    ]

    def __init__(
        self,
        *,
        store_dir: Path,
        client: str | None = None,
        window_token: str = "7d",
        refresh_seconds: float = 5.0,
        subagent_projects_root: Path | str | None = None,
    ) -> None:
        super().__init__()
        self.store_dir = Path(store_dir)
        self.client = client
        self.window_token = window_token
        self.refresh_seconds = max(1.0, float(refresh_seconds))
        self.subagent_projects_root = subagent_projects_root

        # Active palette (mirrors the active theme) — the source of every content
        # colour. Swapped in _apply_palette() on a theme change.
        self.pal: dict[str, str] = _DARK
        # Explicit theme choice: None = follow terminal (auto), else pinned.
        self._theme_pref: str | None = None

        # Snapshot / cube caches (kept machinery).
        self._snapshot: LiveSnapshot | None = None
        self._last_fingerprint: int | None = None
        self._last_refresh_at: float | None = None
        self._flash_until: float = 0.0
        self._importing: bool = False

        # Work-ledger + plan caches, shared across panes and keyed on the event
        # fingerprint so a stale scale can't survive an import (kept discipline).
        self._work_ledger: dict | None = None
        self._work_ledger_fp: int | None = None

        # Dashboard build guard (mutated on the main thread only).
        self._dash_loading: bool = False
        self._error: str | None = None

        # Work pane state.
        self._work_summaries: list[dict] = []
        self._work_by_key: dict[str, dict] = {}
        self._work_latest: float | None = None
        self._work_starts: dict[str, float] = {}
        self._work_status: str = "all"
        self._work_sort: str = "attention"
        self._work_filter: str = ""
        self._work_loading: bool = False
        self._work_built: bool = False
        self._work_visible_ids: list[str] = []
        self._work_visible_rows: list[dict] = []
        self._expanded_index: int | None = None
        self._selected_task_id: str | None = None
        self._work_detail_text: str = ""
        # Work detail sub-mode: the receipt (default) vs the sessions & steps
        # drill-down (Enter opens, esc backs out).
        self._work_detail_mode: str = "receipt"
        self._steps_text: str = ""
        self._selected_receipt: dict | None = None
        self._receipt_head: str = ""
        self._steps_head: str = ""

        # Usage + Sources pane state / test hooks.
        self._usage_range_index: int = 0
        self._usage_text: str = ""
        self._sources_text: str = ""

        # Test/inspection hooks: the last composed text for key regions, so
        # headless tests assert against strings, never Rich renderable internals.
        self._status_text: str = ""
        self._topbar_text: str = ""
        self._dashboard_text: str = ""

    # -- lifecycle ----------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        yield Static("", id="toprule")
        with ContentSwitcher(initial="dashboard", id="switcher"):
            with VerticalScroll(id="dashboard", classes="pane"):
                yield Static("", id="dash-head")
                with Horizontal(id="dash-hero"):
                    yield Static("", id="dash-attention", classes="card")
                    yield Static("", id="dash-rail", classes="card")
                yield Static("", id="dash-recent", classes="card")
                yield Static("", id="dash-spark", classes="card")
            with Vertical(id="work", classes="pane"):
                yield Static("", id="work-head", classes="pane-block")
                yield Static("", id="work-tabs", classes="pane-block")
                yield Input(placeholder="Filter by task, client, or id", id="work-filter")
                with Horizontal(id="work-split"):
                    yield ListView(id="work-list")
                    with VerticalScroll(id="work-detail"):
                        yield Static("", id="work-detail-head")
                        yield Static("", id="work-outcome", classes="card")
                        yield Static("", id="work-summary", classes="card")
                        yield Static("", id="work-dimensions", classes="card")
                        # The sessions & steps drill-down (Enter opens it, esc backs
                        # out); hidden until the receipt is expanded into it.
                        yield Static("", id="work-steps", classes="card")
            with VerticalScroll(id="usage", classes="pane"):
                yield Static("", id="usage-head")
                yield Static("", id="usage-capacity", classes="card")
                yield Static("", id="usage-recorded", classes="card")
            with VerticalScroll(id="sources", classes="pane"):
                yield Static("", id="sources-head")
                yield Static("", id="sources-connected", classes="card")
                yield Static("", id="sources-watcher", classes="card")
                yield Static("", id="sources-verifiers", classes="card")
                yield Static("", id="sources-issues", classes="card")
                yield Static("", id="sources-local", classes="card")
        yield Static("", id="statusbar")

    def on_mount(self) -> None:
        self.register_theme(_THEME_DARK)
        self.register_theme(_THEME_LIGHT)
        self._apply_theme(self._resolve_theme())
        self.title = "agentacct"
        self.query_one("#toprule", Static).update("─" * 400)
        self._render_topbar()
        self._render_statusbar()
        self.query_one("#work-detail-head", Static).update(
            f"[{self.pal['dim']}]Select a receipt (↑↓) to read it; ↵ opens its sessions & steps.[/]"
        )
        self.query_one("#work-steps", Static).display = False
        self.refresh_data(force=True)
        self._start_import()
        self.set_interval(self.refresh_seconds, self.refresh_data)
        self.set_interval(1.0, self._tick)

    # -- theme --------------------------------------------------------------- #

    def _resolve_theme(self) -> str:
        # Explicit choice wins; "auto" (unpinned) defaults to dark — most terminals
        # are dark, a terminal's real background can't be probed reliably, and light
        # is one keypress (T) away. We default rather than guess.
        if self._theme_pref in ("dark", "light"):
            return f"agentacct-{self._theme_pref}"
        return "agentacct-dark"

    def _apply_theme(self, theme_name: str) -> None:
        self.theme = theme_name
        self._apply_palette()

    def _apply_palette(self) -> None:
        self.pal = _LIGHT if str(self.theme).endswith("light") else _DARK

    def action_cycle_theme(self) -> None:
        order = [None, "dark", "light"]
        self._theme_pref = order[(order.index(self._theme_pref) + 1) % len(order)]
        self._apply_theme(self._resolve_theme())
        # Re-render the chrome AND the active pane's content with the new palette
        # (semantic content colour is baked into the markup, so a bare refresh is
        # not enough — every pane must recompose from its cached data).
        self._render_topbar()
        self._render_statusbar()
        pane = self.current_pane
        if pane == "dashboard":
            self._start_dashboard(force=True)
        elif pane == "work":
            self._render_work_head()
            self._render_work_tabs()
            self._render_work_list()  # re-shows the selected receipt too
        elif pane == "usage":
            self._render_usage()
        elif pane == "sources":
            self._render_sources()
        # A runtime theme swap must repaint EVERY cell (unpainted cells keep the
        # old ground otherwise); a full screen refresh forces it.
        try:
            self.screen.refresh(repaint=True, layout=True)
        except Exception:  # noqa: BLE001
            pass

    # -- navigation ---------------------------------------------------------- #

    @property
    def current_pane(self) -> str:
        try:
            return str(self.query_one("#switcher", ContentSwitcher).current or "dashboard")
        except Exception:  # noqa: BLE001
            return "dashboard"

    def action_show_pane(self, pane: str) -> None:
        try:
            self.query_one("#switcher", ContentSwitcher).current = pane
        except Exception:  # noqa: BLE001
            return
        self._render_topbar()
        self._render_statusbar()
        if pane == "work":
            self._start_work()
            try:
                self.query_one("#work-list", ListView).focus()
            except Exception:  # noqa: BLE001
                pass
        elif pane == "usage":
            self._render_usage()
        elif pane == "sources":
            self._render_sources()
        # A ContentSwitcher change can leave the docked chrome (top bar) un-
        # composited for an offscreen screenshot; force a clean full repaint.
        try:
            self.screen.refresh(repaint=True, layout=True)
        except Exception:  # noqa: BLE001
            pass

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    # -- top bar + status bar ------------------------------------------------ #

    def _render_topbar(self) -> None:
        pal = self.pal
        current = self.current_pane
        parts = [f"[b {pal['accent']}]◆[/] [b {pal['ink']}]agentacct[/]  "]
        for i, (key, label) in enumerate(_PANES, start=1):
            if key == current:
                parts.append(f"[{pal['ink']} on {pal['sel']}] [{pal['accent']}]{i}[/] {label} [/]")
            else:
                parts.append(f" [{pal['dim']}]{i}[/] [{pal['muted']}]{label}[/] ")
        # Right-justify the freshness status to the far edge (the artifact's
        # two-edge nav). The bar has 2 cols of padding each side.
        width = max(60, int(getattr(self.size, "width", 0) or 150) - 5)
        text = _two_edge("".join(parts), self._freshness_text(), width)
        self._topbar_text = text
        try:
            self.query_one("#topbar", Static).update(text)
        except Exception:  # noqa: BLE001
            pass

    def _freshness_text(self) -> str:
        pal = self.pal
        if self._error is not None:
            return f"[{pal['coral']}]{_escape(self._error)}[/]"
        if self._importing:
            return f"[{pal['amber']}]⟳ importing…[/]"
        if self._last_refresh_at is None:
            return "loading…"
        ago = _humanize_ago(self._last_refresh_at, time.time())
        dot = f"[{pal['green']}]●[/]"
        label = "just now" if ago == "0s ago" else ago
        return f"{dot} Local data · {label}"

    def _render_statusbar(self) -> None:
        pal = self.pal

        def _cells(pairs):
            return "   ".join(f"[b {pal['accent']}]{k}[/] [{pal['muted']}]{v}[/]" for k, v in pairs)

        left = _cells(_PANE_HINTS.get(self.current_pane, ()))
        r_label = "rescan" if self.current_pane == "sources" else "refresh"
        right = _cells((("r", r_label), ("?", "help"), ("q", "quit")))
        # Extra margin: the left hints use symbol glyphs (↑↓ ↵ [ ]) that some
        # terminal fonts render wider than one column, so keep the right group
        # off the very edge rather than risk a clipped last word.
        width = max(60, int(getattr(self.size, "width", 0) or 150) - 8)
        text = _two_edge(left, right, width)
        self._status_text = text
        try:
            self.query_one("#statusbar", Static).update(text)
        except Exception:  # noqa: BLE001
            pass

    # -- refresh (the usage cube; drives the Dashboard signal rail) ---------- #

    def refresh_data(self, force: bool = False) -> None:
        """Re-read the event log; rebuild the snapshot when it changed (or forced).
        Fail-soft: an error is shown in the top bar and retried next tick, and the
        change key is only advanced after a successful rebuild."""

        try:
            events = SentinelService(self.store_dir, create=False).list_all_events()
        except Exception as exc:  # noqa: BLE001
            self._error = f"error reading store: {exc}"
            self._render_topbar()
            return
        fingerprint = _events_fingerprint(events)
        if (
            not force
            and self._snapshot is not None
            and self._error is None
            and fingerprint == self._last_fingerprint
        ):
            return
        try:
            snapshot = build_live_snapshot(
                events, client=self.client, breakdown_window=self.window_token
            )
        except Exception as exc:  # noqa: BLE001
            self._error = f"error building snapshot: {exc}"
            self._render_topbar()
            return
        self._error = None
        self._last_fingerprint = fingerprint
        self._snapshot = snapshot
        self._last_refresh_at = time.time()
        self._render_all()
        if self.current_pane == "dashboard":
            self._start_dashboard()

    def _render_all(self) -> None:
        try:
            self._render_topbar()
            self._render_statusbar()
            pane = self.current_pane
            if pane == "usage":
                self._render_usage()
            elif pane == "sources":
                self._render_sources()
        except Exception as exc:  # noqa: BLE001
            try:
                self.query_one("#topbar", Static).update(f"render error: {_escape(str(exc))}")
            except Exception:  # noqa: BLE001
                pass

    def _tick(self) -> None:
        """1-second tick: only the time-derived chrome (the top-bar freshness).

        Deliberately does NOT rebuild any pane — the Usage cube and limits are
        expensive (a full event-log read) and must never run once a second; their
        reset countdowns refresh on a manual refresh / range change instead."""

        try:
            self._render_topbar()
        except Exception:  # noqa: BLE001
            pass

    # -- usage import (store freshness) -------------------------------------- #

    def _start_import(self) -> None:
        if self._importing or not _auto_import_enabled():
            return
        self._importing = True
        self._render_topbar()
        self._import_usage()

    @work(thread=True, exclusive=True, group="import")
    def _import_usage(self) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()
        try:
            from .cli import _local_usage_import_payload

            _local_usage_import_payload(store_dir=self.store_dir, client="all", estimate_costs=True)
        except Exception:  # noqa: BLE001 - freshness is best-effort.
            pass
        if worker.is_cancelled:
            return
        self.call_from_thread(self._on_import_done)

    def _on_import_done(self) -> None:
        self._importing = False
        self.refresh_data(force=True)

    # -- Dashboard ----------------------------------------------------------- #

    def _start_dashboard(self, force: bool = False) -> None:
        if self._dash_loading and not force:
            return
        self._dash_loading = True
        self._build_dashboard()

    @work(thread=True, exclusive=True, group="dashboard")
    def _build_dashboard(self) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()
        try:
            from .api import _task_title, build_store_task_projection
            from .receipt import (
                build_attention_reason,
                build_receipt_summary,
                latest_store_activity,
                session_start_index,
            )

            projection = build_store_task_projection(self.store_dir)
            tasks = [
                t for t in projection.get("tasks", [])
                if isinstance(t, dict) and str(t.get("public_task_id") or "")
            ]
            latest = latest_store_activity(tasks)
            starts = session_start_index(tasks)
            tasks.sort(key=lambda t: float(t.get("last_activity_at") or 0.0), reverse=True)
            kept = tasks[:_RECEIPTS_LIMIT]
            summaries = [
                build_receipt_summary(
                    t,
                    public_task_id=str(t.get("public_task_id")),
                    title=_task_title(t),
                    latest_store_activity_at=latest,
                    session_starts=starts,
                )
                for t in kept
            ]
            # The leading attention reason per task (why it needs the user), for the
            # Dashboard's primary-attention grid — computed here where the raw tasks
            # live, then keyed by id for the pure parts builder.
            attention_details: dict[str, dict] = {}
            for t in kept:
                res = build_attention_reason(t, latest_store_activity_at=latest, session_starts=starts)
                if res is not None:
                    attention_details[str(t.get("public_task_id"))] = res[1]
            try:
                from .ingestion_health import IngestionHealthStore

                ingestion = IngestionHealthStore(self.store_dir).snapshot()
            except Exception:  # noqa: BLE001
                ingestion = {}
            # A short by-period token series for the usage sparkline (oldest→newest).
            try:
                events = SentinelService(self.store_dir, create=False).list_all_events()
                page = build_usage_page(events, days=90)
                dated = [p for p in page.by_period if p.get("period") != "unknown"]
                full = [float(p.get("total_tokens_including_cached") or 0) for p in dated]
                series, history_total = full[-14:], sum(full)
            except Exception:  # noqa: BLE001
                series, history_total = [], 0.0
        except Exception as exc:  # noqa: BLE001
            if not worker.is_cancelled:
                self.call_from_thread(self._dashboard_error, str(exc))
            return
        if worker.is_cancelled:
            return
        self.call_from_thread(
            self._render_dashboard, summaries, ingestion, series, history_total, attention_details
        )

    def _dashboard_error(self, message: str) -> None:
        self._dash_loading = False
        try:
            self.query_one("#dash-head", Static).update(
                f"[{self.pal['coral']}]could not build dashboard:[/] {_escape(message)}"
            )
        except Exception:  # noqa: BLE001
            pass

    def _set_card(self, wid: str, title: str, body: str, color: str | None = None) -> None:
        """Render a card with its caps title INSIDE the panel (on the card's own
        background, with a plain border above it) rather than notched into the top
        border — where the title text sits directly against the black pane
        background and reads as pasted-on. Matches the artifact's in-card headers."""

        try:
            card = self.query_one(wid, Static)
        except Exception:  # noqa: BLE001
            return
        card.border_title = ""
        if title:
            col = color or self.pal["dim"]
            card.update(f"[{col}]{_escape(title)}[/]\n{body}")
        else:
            card.update(body)

    def _render_dashboard(
        self,
        summaries: list[dict],
        ingestion: dict,
        series: list[float] | None = None,
        history_total: float = 0.0,
        attention_details: dict[str, dict] | None = None,
    ) -> None:
        self._dash_loading = False
        pal = self.pal
        width = int(getattr(self.size, "width", 0) or 150)
        parts = _build_dashboard_parts(
            summaries, ingestion, self._snapshot, self._client_limits(), pal,
            series or [], history_total, attention_details or {}, width,
        )
        # Combined mirror for headless tests (titles live on the borders).
        self._dashboard_text = "\n".join([
            parts["head"], parts["attn_title"], parts["attn"], parts["rail_title"], parts["rail"],
            parts["recent_title"], parts["recent"], parts["spark_title"], parts["spark"],
        ])
        try:
            self.query_one("#dash-head", Static).update(parts["head"])
            self._set_card("#dash-attention", parts["attn_title"], parts["attn"], pal["coral"])
            self._set_card("#dash-rail", parts["rail_title"], parts["rail"])
            self._set_card("#dash-recent", parts["recent_title"], parts["recent"])
            self._set_card("#dash-spark", parts["spark_title"], parts["spark"])
        except Exception:  # noqa: BLE001
            pass

    def _client_limits(self) -> list[ClientLimit]:
        return list(self._snapshot.limits) if self._snapshot is not None else []

    # -- Work: receipts list (master) + one Work Receipt (detail) ------------ #

    def _start_work(self, force: bool = False) -> None:
        if self._work_built and not force:
            self._render_work_head()
            self._render_work_tabs()
            self._render_work_list()
            return
        if self._work_loading and not force:
            return
        self._work_loading = True
        try:
            self.query_one("#work-head", Static).update(
                f"[b {self.pal['ink']}]Work receipts[/]  [{self.pal['dim']}]building… (a few seconds)[/]"
            )
        except Exception:  # noqa: BLE001
            pass
        self._build_work()

    @work(thread=True, exclusive=True, group="work")
    def _build_work(self) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()
        try:
            from .api import _task_title, build_store_task_projection
            from .receipt import (
                build_receipt_summary,
                latest_store_activity,
                session_start_index,
            )

            projection = build_store_task_projection(self.store_dir)
            tasks = [
                t for t in projection.get("tasks", [])
                if isinstance(t, dict) and str(t.get("public_task_id") or "")
            ]
            latest = latest_store_activity(tasks)
            starts = session_start_index(tasks)
            tasks.sort(key=lambda t: float(t.get("last_activity_at") or 0.0), reverse=True)
            by_key: dict[str, dict] = {}
            summaries: list[dict] = []
            for t in tasks[:_RECEIPTS_LIMIT]:
                tid = str(t.get("public_task_id"))
                by_key[tid] = t
                summaries.append(
                    build_receipt_summary(
                        t,
                        public_task_id=tid,
                        title=_task_title(t),
                        latest_store_activity_at=latest,
                        session_starts=starts,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            if not worker.is_cancelled:
                self.call_from_thread(self._work_error, str(exc))
            return
        if worker.is_cancelled:
            return
        self.call_from_thread(self._populate_work, summaries, by_key, latest, starts)

    def _work_error(self, message: str) -> None:
        self._work_loading = False
        try:
            self.query_one("#work-head", Static).update(
                f"[{self.pal['coral']}]could not build receipts:[/] {_escape(message)}"
            )
        except Exception:  # noqa: BLE001
            pass

    def _populate_work(
        self, summaries: list[dict], by_key: dict[str, dict], latest: float | None, starts: dict[str, float]
    ) -> None:
        self._work_loading = False
        self._work_built = True
        self._work_summaries = summaries
        self._work_by_key = by_key
        self._work_latest = latest
        self._work_starts = starts
        self._render_work_head()
        self._render_work_tabs()
        self._render_work_list()

    def _bucket_counts(self) -> dict[str, int]:
        counts = {tab: 0 for tab, _label in _WORK_TABS}
        for s in self._work_summaries:
            counts["all"] += 1
            counts[task_bucket(s)] += 1
        return counts

    def _visible_tab_ids(self) -> list[str]:
        # Every fixed tab is always shown; the catch-all "Other" only when it has
        # members (matching the GUI).
        counts = self._bucket_counts()
        return [t for t, _label in _WORK_TABS if t != "other" or counts.get("other", 0) > 0]

    def _filtered_work(self) -> list[dict]:
        active = self._work_status
        needle = self._work_filter.strip().lower()
        rows: list[dict] = []
        for s in self._work_summaries:
            if active != "all" and task_bucket(s) != active:
                continue
            if needle:
                hay = " ".join(str(x) for x in (
                    s.get("title"), s.get("task_id"),
                    (s.get("primary_root") or {}).get("client"), s.get("project"),
                )).lower()
                if needle not in hay:
                    continue
            rows.append(s)
        if self._work_sort == "cost":
            rows.sort(key=lambda s: float((s.get("cost") or {}).get("estimated_cost_usd") or 0.0), reverse=True)
        elif self._work_sort == "latest":
            rows.sort(key=lambda s: float(s.get("last_activity_at") or 0.0), reverse=True)
        else:  # attention: tasks needing the user first, then recency
            def _rank(s: dict) -> tuple[int, float]:
                key = str((s.get("decision_status") or {}).get("key"))
                cf = int((s.get("evidence_strength") or {}).get("checks_failed") or 0)
                return (0 if needs_attention(key, cf) else 1, -float(s.get("last_activity_at") or 0.0))
            rows.sort(key=_rank)
        return rows

    def _render_work_head(self) -> None:
        pal = self.pal
        n = len(self._work_summaries)
        text = (f"{caps('Work receipts', pal)} [{pal['dim']}]· {n}[/]   "
                f"[{pal['dim']}]sort {self._work_sort}[/]")
        try:
            self.query_one("#work-head", Static).update(text)
        except Exception:  # noqa: BLE001
            pass

    def _render_work_tabs(self) -> None:
        pal = self.pal
        counts = self._bucket_counts()
        cells: list[str] = []
        for bid, label in _WORK_TABS:
            c = counts.get(bid, 0)
            if bid == "other" and c == 0:
                continue  # the catch-all only appears when it has members
            cnum = f"[{pal['coral']}]{c}[/]" if (bid == "attention" and c) else f"[{pal['dim']}]{c}[/]"
            if bid == self._work_status:
                cells.append(f"[b {pal['accent']}]{label}[/] {cnum}")
            else:
                cells.append(f"[{pal['muted']}]{label}[/] {cnum}")
        try:
            self.query_one("#work-tabs", Static).update("   ".join(cells))
        except Exception:  # noqa: BLE001
            pass

    def _render_work_list(self) -> None:
        pal = self.pal
        try:
            lv = self.query_one("#work-list", ListView)
        except Exception:  # noqa: BLE001
            return
        rows = self._filtered_work()
        self._work_visible_ids = [str(s.get("task_id")) for s in rows]
        self._work_visible_rows = rows
        target = None
        sel_idx = None
        if rows:
            target = self._selected_task_id if self._selected_task_id in self._work_visible_ids else self._work_visible_ids[0]
            sel_idx = self._work_visible_ids.index(target)
        self._expanded_index = sel_idx
        # clear() queues removal of the current items (async); the fresh cards are
        # appended without ids so a re-render can't collide on a stale id. The
        # highlighted card is built EXPANDED, the rest compact.
        lv.clear()
        for i, s in enumerate(rows):
            lv.append(ListItem(Static(_work_card_markup(s, pal, expanded=(i == sel_idx)))))
        if rows:
            try:
                lv.index = sel_idx  # fires Highlighted → cursor-follows
            except Exception:  # noqa: BLE001
                pass
            self._show_receipt(target)
        else:
            self._selected_task_id = None
            self.query_one("#work-detail-head", Static).update(
                f"[{pal['dim']}]No receipts match this filter.[/]"
            )
            for wid in ("#work-outcome", "#work-summary", "#work-dimensions"):
                self.query_one(wid, Static).update("")

    def _highlight_to_receipt(self, index: int | None) -> None:
        if index is None:
            return
        ids = getattr(self, "_work_visible_ids", [])
        if 0 <= index < len(ids):
            self._reflow_expanded(index)
            self._show_receipt(ids[index])

    def _reflow_expanded(self, index: int) -> None:
        """Move the expanded-card treatment to the newly-highlighted row without
        rebuilding the whole list (cheap, and it avoids the async clear/append
        churn on every arrow key). Only the outgoing + incoming cards repaint."""

        old = getattr(self, "_expanded_index", None)
        if old == index:
            return
        rows = getattr(self, "_work_visible_rows", [])
        try:
            items = list(self.query_one("#work-list", ListView).children)
        except Exception:  # noqa: BLE001
            return
        for i in {old, index}:
            if i is None or not (0 <= i < len(items) and i < len(rows)):
                continue
            try:
                items[i].query_one(Static).update(_work_card_markup(rows[i], self.pal, expanded=(i == index)))
            except Exception:  # noqa: BLE001
                pass
        self._expanded_index = index

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # Cursor-follows: the detail tracks the highlighted card (j/k), like lazygit.
        if event.list_view.id == "work-list":
            self._highlight_to_receipt(event.list_view.index)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Enter drills the highlighted receipt into its sessions & steps.
        if event.list_view.id == "work-list":
            self._highlight_to_receipt(event.list_view.index)
            self._open_steps()

    def _show_receipt(self, task_id: str) -> None:
        self._selected_task_id = task_id
        # Moving to a receipt always returns to the receipt view (out of steps).
        self._work_detail_mode = "receipt"
        task = self._work_by_key.get(task_id)
        pal = self.pal
        if task is None:
            return
        receipt: dict | None = None
        try:
            from .api import _task_title
            from .receipt import build_receipt

            receipt = build_receipt(
                task,
                public_task_id=task_id,
                title=_task_title(task),
                latest_store_activity_at=self._work_latest,
                session_starts=self._work_starts,
            )
            parts = _build_receipt_parts(receipt, pal, int(getattr(self.size, "width", 0) or 150))
        except Exception as exc:  # noqa: BLE001
            parts = {
                "head": f"[{pal['coral']}]could not render receipt:[/] {_escape(str(exc))}",
                "outcome_title": "CURRENT OUTCOME", "outcome": f"[{pal['dim']}]—[/]",
                "summary_title": "", "summary": "",
                "dims_title": "RECEIPT DIMENSIONS", "dims": f"[{pal['dim']}]—[/]",
            }
        self._selected_receipt = receipt
        self._receipt_head = parts["head"]
        self._work_detail_text = "\n".join([
            parts["head"], parts["outcome_title"], parts["outcome"],
            parts["summary"], parts["dims_title"], parts["dims"],
        ])
        try:
            self._set_card("#work-outcome", parts["outcome_title"], parts["outcome"], pal["accent"])
            self.query_one("#work-summary", Static).update(parts["summary"])  # strip has no header
            self._set_card("#work-dimensions", parts["dims_title"], parts["dims"])
        except Exception:  # noqa: BLE001
            pass
        self._apply_detail_mode()

    def _apply_detail_mode(self) -> None:
        """Show the receipt cards or the sessions & steps card per the sub-mode,
        and swap the breadcrumb head to match."""

        steps = self._work_detail_mode == "steps"
        try:
            for wid in ("#work-outcome", "#work-summary", "#work-dimensions"):
                self.query_one(wid, Static).display = not steps
            self.query_one("#work-steps", Static).display = steps
            self.query_one("#work-detail-head", Static).update(
                self._steps_head if steps else self._receipt_head
            )
            self.query_one("#work-detail", VerticalScroll).scroll_home(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def _open_steps(self) -> None:
        """Drill the selected receipt into its sessions & steps (checks timeline)."""

        if self.current_pane != "work" or not self._selected_task_id:
            return
        task = self._work_by_key.get(self._selected_task_id)
        receipt = self._selected_receipt
        if task is None or receipt is None:
            return
        pal = self.pal
        try:
            from .receipt import _project_checks

            checks = _project_checks(task)
            parts = _build_steps_parts(receipt, checks, pal, int(getattr(self.size, "width", 0) or 150))
        except Exception as exc:  # noqa: BLE001
            parts = {"head": f"[{pal['dim']}]‹ Receipt[/]",
                     "title": "SESSIONS & STEPS", "body": f"[{pal['coral']}]could not render steps:[/] {_escape(str(exc))}"}
        self._steps_head = parts["head"]
        self._steps_text = parts["title"] + "\n" + parts["body"]
        self._set_card("#work-steps", parts["title"], parts["body"])
        self._work_detail_mode = "steps"
        self._apply_detail_mode()

    def action_steps_back(self) -> None:
        if self.current_pane == "work" and self._work_detail_mode == "steps":
            self._work_detail_mode = "receipt"
            self._apply_detail_mode()

    # -- Work actions -------------------------------------------------------- #

    def action_work_status(self, delta: int) -> None:
        if self.current_pane != "work":
            return
        ids = self._visible_tab_ids()
        cur = self._work_status if self._work_status in ids else "all"
        self._work_status = ids[(ids.index(cur) + delta) % len(ids)]
        self._render_work_tabs()
        self._render_work_list()

    def action_work_sort(self) -> None:
        if self.current_pane != "work":
            return
        order = ["attention", "latest", "cost"]
        self._work_sort = order[(order.index(self._work_sort) + 1) % len(order)]
        self._render_work_head()
        self._render_work_list()

    def action_work_filter(self) -> None:
        if self.current_pane != "work":
            return
        try:
            self.query_one("#work-filter", Input).focus()
        except Exception:  # noqa: BLE001
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "work-filter":
            return
        self._work_filter = event.value
        self._render_work_list()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "work-filter":
            try:
                self.query_one("#work-list", ListView).focus()
            except Exception:  # noqa: BLE001
                pass

    # -- Usage: provider capacity meters + recorded usage -------------------- #

    def _render_usage(self) -> None:
        self._build_usage()

    @work(thread=True, exclusive=True, group="usage")
    def _build_usage(self) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()
        days, label = _USAGE_RANGE_CYCLE[self._usage_range_index]
        try:
            events = SentinelService(self.store_dir, create=False).list_all_events()
            page = build_usage_page(events, days=days)
            limits = build_client_limits(events, client=self.client)
        except Exception as exc:  # noqa: BLE001
            if not worker.is_cancelled:
                self.call_from_thread(self._usage_error, str(exc))
            return
        if worker.is_cancelled:
            return
        self.call_from_thread(self._paint_usage, page, limits, label)

    def _usage_error(self, message: str) -> None:
        try:
            self.query_one("#usage-head", Static).update(
                f"[{self.pal['coral']}]could not build usage:[/] {_escape(message)}"
            )
        except Exception:  # noqa: BLE001
            pass

    def _paint_usage(self, page: UsagePage, limits: list[ClientLimit], label: str) -> None:
        parts = _build_usage_parts(page, limits, self._snapshot, label, self.pal,
                                   int(getattr(self.size, "width", 0) or 150))
        self._usage_text = "\n".join([
            parts["head"], parts["cap_title"], parts["cap"], parts["rec_title"], parts["rec"],
        ])
        try:
            self.query_one("#usage-head", Static).update(parts["head"])
            self._set_card("#usage-capacity", parts["cap_title"], parts["cap"])
            self._set_card("#usage-recorded", parts["rec_title"], parts["rec"])
        except Exception:  # noqa: BLE001
            pass

    def action_usage_range(self) -> None:
        if self.current_pane != "usage":
            return
        self._usage_range_index = (self._usage_range_index + 1) % len(_USAGE_RANGE_CYCLE)
        self._render_usage()

    # -- Sources: what feeds the store, how well (ingestion health) ---------- #

    def _render_sources(self) -> None:
        pal = self.pal
        try:
            from .ingestion_health import IngestionHealthStore

            snapshot = IngestionHealthStore(self.store_dir).snapshot()
        except Exception as exc:  # noqa: BLE001
            snapshot = {"_error": str(exc)}
        parts = _build_sources_parts(snapshot, self.store_dir, pal,
                                     int(getattr(self.size, "width", 0) or 150))
        self._sources_text = "\n".join([
            parts["head"], parts["connected_title"], parts["connected"],
            parts["watcher_title"], parts["watcher"],
            parts["issues_title"], parts["issues"], parts["local"],
        ])
        try:
            self.query_one("#sources-head", Static).update(parts["head"])
            self._set_card("#sources-connected", parts["connected_title"], parts["connected"])
            self._set_card("#sources-watcher", parts["watcher_title"], parts["watcher"])
            self._set_card("#sources-verifiers", parts["verifiers_title"], parts["verifiers"])
            iss = self.query_one("#sources-issues", Static)
            iss.display = bool(parts["issues"])
            if parts["issues"]:
                self._set_card("#sources-issues", parts["issues_title"], parts["issues"], pal["amber"])
            self._set_card("#sources-local", "LOCAL ONLY", parts["local"])
        except Exception:  # noqa: BLE001
            pass

    # -- actions ------------------------------------------------------------- #

    def action_refresh(self) -> None:
        self._flash_until = time.time() + 1.2
        self.refresh_data(force=True)
        self._start_import()
        pane = self.current_pane
        if pane == "dashboard":
            self._start_dashboard(force=True)
        elif pane == "work":
            self._start_work(force=True)
        elif pane == "usage":
            self._render_usage()
        elif pane == "sources":
            self._render_sources()

    def action_screenshot(self) -> None:
        """Save a shareable SVG of the current screen and toast the path. Written
        under the store's ``snapshots/`` dir, never the cwd, so pressing ``p``
        from a project repo can't litter the working tree."""

        saved: str | None = None
        try:
            snap_dir = self.store_dir / "snapshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            saved = self.save_screenshot(path=str(snap_dir))
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Could not save the snapshot: {exc}", severity="error", timeout=6)
        finally:
            # save_screenshot renders a full offscreen frame, which clears the live
            # compositor's dirty regions — the next incremental frame then leaves
            # stale cells ("white bar"). Force one full repaint to restore it.
            self.screen.refresh(repaint=True, layout=True)
        if saved is not None:
            self.notify(f"Saved a shareable snapshot →\n{saved}", title="◆ agentacct", timeout=6)


# ============================================================================ #
# Pane markup builders (pure: data + palette → Rich markup string).           #
# Kept separate from the App so they are unit-testable and reused by both the  #
# live render and the screenshot fixtures.                                     #
# ============================================================================ #

def _build_dashboard_parts(
    summaries: list[dict],
    ingestion: dict,
    snap: LiveSnapshot | None,
    limits: list[ClientLimit],
    pal: dict[str, str],
    series: list[float],
    history_total: float,
    attention_details: dict[str, dict] | None = None,
    width: int = 150,
) -> dict[str, str]:
    """The Dashboard as separate panels: a head plus four cards, each returned as
    (border title, body markup) so the app can draw them as bordered surfaces."""

    now = time.time()
    attention_details = attention_details or {}
    full_w = max(60, width - 6)     # a full-pane row's inner width
    card_w = max(60, width - 10)     # a bordered card's inner width
    half_w = max(30, width // 2 - 10)  # one hero card's inner width (two share the row)
    attention = [
        s for s in summaries
        if needs_attention(str((s.get("decision_status") or {}).get("key")),
                           (s.get("evidence_strength") or {}).get("checks_failed"))
    ]
    # Lead with the most ACTIONABLE item: one that carries a recorded next step
    # (a blocker with a remedy) before one that does not (a bare failed check).
    # Stable, so recency order is preserved within each group.
    attention.sort(key=lambda s: 0 if (attention_details.get(str(s.get("task_id"))) or {}).get("next_step") else 1)

    # head — the shift-brief eyebrow + the headline item (count pinned right).
    if attention:
        headline = str(attention[0].get("title") or attention[0].get("task_id") or "—")
        n_rev = len(attention)
        head = f"{caps('Shift brief', pal)}\n" + _two_edge(
            f"[b {pal['ink']}]{_escape(headline)}[/]",
            f"[{pal['dim']}]{n_rev} review item{'s' if n_rev != 1 else ''}[/]", full_w)
    else:
        head = f"{caps('Shift brief', pal)}\n" + _two_edge(
            f"[b {pal['ink']}]All clear[/]",
            f"[{pal['dim']}]nothing needs attention[/]", full_w)

    # attention card — the primary item, with a reason/observed/provenance grid,
    # a recorded-next-step inset, and the review affordances.
    if attention:
        top = attention[0]
        detail = attention_details.get(str(top.get("task_id"))) or {}
        attn_title = f"PRIMARY ATTENTION · 1 OF {len(attention)}"
        dkey = str((top.get("decision_status") or {}).get("key"))
        client = str((top.get("primary_root") or {}).get("client") or top.get("project") or "")
        rows = []
        if client:
            rows.append(f"[{pal['muted']}]{_escape(client)}[/]  {decision_badge(dkey, pal)}")
        else:
            rows.append(decision_badge(dkey, pal))
        statement = detail.get("summary") or (top.get("decision_status") or {}).get("statement")
        if statement:
            rows.append(f"[{pal['ink']}]{_escape(str(statement))}[/]")
        rows.append("")
        reason = _ATTENTION_REASON_LABEL.get(str(detail.get("kind")), decision_label(dkey))
        observed = _humanize_ago(detail.get("observed_at") or top.get("last_activity_at"), now)
        prov = _PROVENANCE_LABEL.get(str(detail.get("source")), str(detail.get("source") or "—"))
        rows.append(_kv_grid(
            [("Recorded reason", reason), ("Observed", observed), ("Recorded via", prov)], pal, 16))
        next_step = detail.get("next_step")
        if next_step:
            rows.append("")
            # A shaded inset box (the artifact's darker next-step panel): each line
            # padded to the card width and washed, so it reads as a filled block.
            rows.append(f"[{pal['dim']} on {pal['chip']}]{(' RECORDED NEXT STEP'):<{half_w}}[/]")
            for line in _wrap_words(str(next_step), half_w - 2):
                rows.append(f"[{pal['ink']} on {pal['chip']}]{(' ' + _escape(line)):<{half_w}}[/]")
        rows.append("")
        rows.append(f"[{pal['accent']} on {pal['ta']}] ↵ Review evidence [/]  "
                    f"[{pal['muted']} on {pal['chip']}] y Copy review brief [/]  "
                    f"[{pal['accent']}]View queue →[/]")
        attn = "\n".join(rows)
    else:
        attn_title = "PRIMARY ATTENTION"
        attn = f"[{pal['green']}]Nothing needs your review right now.[/]"

    # signal rail — four stacked metric blocks.
    active = sum(1 for s in summaries if str((s.get("decision_status") or {}).get("key")) in _LIVE)
    working_sub = ""
    if summaries:
        proj = str(summaries[0].get("project") or "")
        act = _humanize_ago(summaries[0].get("last_activity_at"), now)
        working_sub = " · ".join(p for p in (proj, (f"activity {act}" if act != "—" else "")) if p)
    rail_blocks = [_rail_block(
        "Working now", f"{active} active session(s)",
        working_sub or ("in progress" if active else "none in progress right now"), pal)]
    rail_blocks.append(_capacity_block(limits, pal))
    if snap is not None:
        rail_blocks.append(_rail_block(
            "Usage change", f"Today · {abbr_tokens(_window_total(snap, 'today'))} fresh",
            "client reported", pal))
    rail_blocks.append(_trust_block(ingestion, pal))
    rail_lines: list[str] = []
    for i, b in enumerate(b for b in rail_blocks if b):
        if i:  # a rule with air on both sides between the metric blocks
            rail_lines += ["", f"[{pal['line']}]{'─' * half_w}[/]", ""]
        rail_lines.append(b)
    rail = "\n".join(rail_lines)

    # recent work — an aligned table (Task / Outcome / Evidence / Cost).
    recent_title = f"RECENT WORK · {len(summaries)}"
    if not summaries:
        recent = f"[{pal['dim']}]No receipts yet — use your agents, or run `agentacct onboard`.[/]"
    else:
        tcol = 58
        trows: list[tuple[str, int, str, int, str, int, str]] = []
        for s in summaries[:5]:
            title = str(s.get("title") or s.get("task_id") or "—")
            dkey = str((s.get("decision_status") or {}).get("key"))
            ev_s = s.get("evidence_strength") or {}
            ekey = str(ev_s.get("key"))
            suffix = " · ".join(p for p in (
                str((s.get("primary_root") or {}).get("client") or s.get("project") or ""),
                _humanize_ago(s.get("last_activity_at"), now),
            ) if p and p != "—")
            # Truncate title so title+meta always fit the fixed TASK column, so the
            # OUTCOME and EVIDENCE columns snap to the same x on every row.
            avail = max(8, tcol - 4 - (len(suffix) if suffix else 0))
            tt = title if len(title) <= avail else title[: avail - 1] + "…"
            if suffix:
                tmarkup = f"[{pal['ink']}]{_escape(tt)}[/]  [{pal['dim']}]{_escape(suffix)}[/]"
                tvis = len(tt) + 2 + len(suffix)
            else:
                tmarkup = f"[{pal['ink']}]{_escape(tt)}[/]"
                tvis = len(tt)
            blabel = decision_label(dkey)
            _fg, wash = _decision_colors(dkey, pal)
            bvis = len(blabel) + (2 if wash is not None else 0)
            short = _coverage_short(ev_s)
            cov = f"{short} supported" if "/" in short else short
            ev = f"{pip(ekey, pal)} [{pal['muted']}]{_escape(cov)}[/]"
            evis = 2 + len(cov)
            trows.append((tmarkup, tvis, decision_badge(dkey, pal), bvis, ev, evis,
                          receipt_cost_text(s.get("cost") or {})))
        # Reserve margin for the pane's vertical scrollbar (~2 cols) plus the
        # ambiguous-width glyphs in each row (the ○/◐ pips, ·, … render wider than
        # one cell), so a row never overflows the card and wraps its cost.
        recent = _recent_table(trows, pal, max(60, card_w - 8))

    spark = _two_edge(
        sparkline(series, pal),
        f"[b {pal['ink']}]{abbr_tokens(history_total)}[/] [{pal['dim']}]total[/]", card_w)

    return {
        "head": head,
        "attn_title": attn_title, "attn": attn,
        "rail_title": "SIGNAL RAIL", "rail": rail,
        "recent_title": recent_title, "recent": recent,
        "spark_title": "USAGE HISTORY · FRESH TOKENS · 90D · CLIENT REPORTED", "spark": spark,
    }


def _coverage_short(ev: dict) -> str:
    """A compact coverage token for the list (the full headline is in the detail)."""

    if not ev.get("gradeable"):
        return "not gradeable"
    checkable = int(ev.get("checkable_total") or 0)
    if checkable == 0:
        return "no steps"
    return f"{int(ev.get('checked_total') or 0)}/{checkable}"


def _checks_cell(ev: dict, pal: dict[str, str]) -> str:
    total = int(ev.get("checks_total") or 0)
    passed = int(ev.get("checks_passed") or 0)
    failed = int(ev.get("checks_failed") or 0)
    if total == 0:
        return f"[{pal['dim']}]no check runs[/]"
    if failed:
        return f"[{pal['coral']}]{passed}/{total} · {failed} failed[/]"
    return f"[{pal['green']}]{passed}/{total} passed[/]"


def _prov_chips(names: list[str] | None, pal: dict[str, str]) -> str:
    if not names:
        return ""
    return "  " + " ".join(f"[{pal['muted']} on {pal['chip']}] {_escape(str(n))} [/]" for n in names)


def _checks_count(ev: dict, pal: dict[str, str]) -> tuple[str, str]:
    """(value, colour) for a compact checks count — ``6/6`` green, ``0/1`` coral
    when any failed, ``no runs`` dim when none."""

    total = int(ev.get("checks_total") or 0)
    passed = int(ev.get("checks_passed") or 0)
    failed = int(ev.get("checks_failed") or 0)
    if total == 0:
        return "no runs", pal["dim"]
    return f"{passed}/{total}", (pal["coral"] if failed else pal["green"])


def _work_card_markup(s: dict, pal: dict[str, str], *, expanded: bool = False) -> str:
    """One receipt as a master-list card. The highlighted card EXPANDS to a
    Claims/Checks grid (the GUI's selected card); the rest stay compact — a title,
    badge, and one pip-led meta line — so the list keeps a clear hierarchy."""

    tid = str(s.get("task_id"))
    dkey = str((s.get("decision_status") or {}).get("key"))
    ev = s.get("evidence_strength") or {}
    ekey = str(ev.get("key"))
    client = str((s.get("primary_root") or {}).get("client") or s.get("project") or "")
    cost = receipt_cost_text(s.get("cost") or {})
    age = _humanize_ago(s.get("last_activity_at"), time.time())
    title_line = f"[b {pal['ink']}]{_escape(str(s.get('title') or tid))}[/]"
    if expanded:
        cov = _coverage_short(ev)
        cval, ccol = _checks_count(ev, pal)
        col = 15
        grid = (f"[{pal['dim']}]{'CLAIMS':<{col}}CHECKS[/]\n"
                f"[{pal['ink']}]{pip(ekey, pal)} {_escape(cov):<{col - 2}}[/][{ccol}]{cval}[/]")
        meta = " · ".join(p for p in (client, cost, age) if p and p != "—")
        return f"{title_line}\n{decision_badge(dkey, pal)}\n{grid}\n[{pal['dim']}]{_escape(meta)}[/]"
    meta = " · ".join(p for p in (_coverage_short(ev), client, cost, age) if p and p != "—")
    return (f"{title_line}\n{decision_badge(dkey, pal)}\n"
            f"{pip(ekey, pal)} [{pal['dim']}]{_escape(meta)}[/]")


def _kv_grid(cells: list[tuple[str, str]], pal: dict[str, str], width: int) -> str:
    """A two-row label/value grid (caps label above a bold value), columns split by
    whitespace only — the artifact's RECORDED REASON / OBSERVED / PROVENANCE block
    (a lighter look than the KPI strips, which do carry ``│`` dividers)."""

    def pad(text: str) -> str:
        return _escape(text + " " * max(3, width - len(text)))

    labels = "".join(f"[{pal['dim']}]{pad(c[0].upper())}[/]" for c in cells)
    values = "".join(f"[{pal['ink']}]{pad(str(c[1]))}[/]" for c in cells)
    return f"{labels}\n{values}"


def _rail_block(label: str, value: str, sub: str, pal: dict[str, str], label_color: str | None = None) -> str:
    """One signal-rail metric: a caps label line, a bold value line, and a dim sub
    line — the artifact's stacked rail, not a single crammed row."""

    lc = label_color or pal["dim"]
    lines = [f"[{lc}]{_escape(label.upper())}[/]", f"[b {pal['ink']}]{value}[/]"]
    if sub:
        lines.append(f"[{pal['dim']}]{sub}[/]")
    return "\n".join(lines)


def _recent_table(rows: list[tuple[str, int, str, int, str, int, str]], pal: dict[str, str], tw: int = 134) -> str:
    """The RECENT WORK table: a caps header rule then one aligned row per receipt,
    with a blank line between rows for the artifact's airier spacing.

    Each row is (title_markup, title_vis, badge_markup, badge_vis, evidence_markup,
    evidence_vis, cost). The markup carries colour/wash while the ``_vis`` ints let
    us pad by VISIBLE width (a wash chip is wider than its label), so the OUTCOME /
    EVIDENCE / COST columns line up and cost sits flush right."""

    tcol, ocol, ecol = 58, 18, 24  # tcol MUST match the caller's title truncation width

    def pad_vis(markup: str, vis: int, target: int) -> str:
        return markup + " " * max(2, target - vis)

    def head_cell(text: str, target: int) -> str:
        return f"[{pal['dim']}]{_escape(text.upper())}[/]" + " " * max(2, target - len(text))

    header = (head_cell("Task", tcol) + head_cell("Outcome", ocol)
              + head_cell("Evidence", ecol) + f"[{pal['dim']}]{'COST':>{max(4, tw - tcol - ocol - ecol)}}[/]")
    out = [header, f"[{pal['line']}]{'─' * tw}[/]"]
    for tmarkup, tvis, badge, bvis, ev, evis, cost in rows:
        # The TASK cell is padded to EXACTLY tcol (titles are pre-truncated to fit),
        # so OUTCOME and EVIDENCE start at the same x on every row.
        title_cell = tmarkup + " " * max(1, tcol - tvis)
        left = title_cell + pad_vis(badge, bvis, ocol) + pad_vis(ev, evis, ecol)
        left_vis = tcol + max(ocol, bvis + 2) + max(ecol, evis + 2)
        gap = max(1, tw - left_vis - len(cost))
        out.append("")  # a blank line before each row — the artifact's airier rhythm
        out.append(left + " " * gap + f"[{pal['ink']}]{_escape(cost)}[/]")
    return "\n".join(out)


def _kpi_cells(cells: list[tuple[str, str, str]], pal: dict[str, str], width: int) -> str:
    """A row of KPI blocks — a caps label, a bold value, and a sub-label, stacked
    and aligned into fixed-width monospace columns (a terminal can't scale font
    size, so prominence comes from weight + the labelled block, not size)."""

    def pad(text: str) -> str:
        return _escape(text + " " * max(2, width - len(text)))

    sep = f"[{pal['line']}]│[/] "
    labels = sep.join(f"[{pal['dim']}]{pad(c[0].upper())}[/]" for c in cells)
    values = sep.join(f"[b {pal['ink']}]{pad(str(c[1]))}[/]" for c in cells)
    subs = sep.join(f"[{pal['dim']}]{pad(c[2])}[/]" for c in cells)
    return f"{labels}\n{values}\n{subs}"


def _tool_bars(counts: dict, pal: dict[str, str], width: int = 16, top: int = 4) -> str:
    """A by-type bar per tool (name or category), on a shared scale, with the count
    and its share — the artifact's ``Read ████ 38 · 47.5%`` breakdown."""

    items = sorted(counts.items(), key=lambda kv: -int(kv[1] or 0))[:top]
    peak = max((int(v or 0) for _, v in items), default=0) or 1
    grand = sum(int(v or 0) for v in counts.values()) or 1
    lines = []
    for name, value in items:
        value = int(value or 0)
        filled = max(0, min(width, round(width * value / peak)))
        pct = 100.0 * value / grand
        lines.append(
            f"  [{pal['muted']}]{_escape(f'{str(name)[:8]:<8}')}[/] "
            f"[{pal['accent']}]{'█' * filled}[/][{pal['line']}]{'░' * (width - filled)}[/] "
            f"[{pal['ink']}]{value}[/] [{pal['dim']}]· {pct:.1f}%[/]"
        )
    return "\n".join(lines)


def _wrap_words(text: str, width: int) -> list[str]:
    """Greedy word-wrap of PLAIN text to ``width`` columns (used before colouring,
    so a wrapped ledger cell keeps a clean hanging indent)."""

    words = str(text).split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def _ledger(rows: list[tuple[str, list[str]]], pal: dict[str, str], gutter: int, tw: int) -> str:
    """A label-gutter ledger: each row is a caps label in a fixed left column and
    its (already width-fit, pre-coloured) content lines to the right, with a thin
    rule between rows — the artifact's RECEIPT DIMENSIONS layout."""

    out: list[str] = []
    rule = f"[{pal['line']}]{'─' * tw}[/]"
    for i, (label, body) in enumerate(rows):
        if i:  # a rule with air on both sides between ledger rows
            out += ["", rule, ""]
        body = body or [""]
        out.append(f"[{pal['dim']}]{label.upper():<{gutter}}[/]{body[0]}")
        for cont in body[1:]:
            out.append(" " * gutter + cont)
    return "\n".join(out)


def _short_task_id(tid: str) -> str:
    """A readable short form of a task id for the breadcrumb — a raw 32-hex digest
    crowds the header, so keep the ``task_`` prefix and the first 8 hex (``task_
    327a6a5c…``); other ids just truncate."""

    tid = str(tid or "")
    body = tid[5:] if tid.startswith("task_") else tid
    if len(body) > 12 and all(c in "0123456789abcdefABCDEF" for c in body):
        return (("task_" if tid.startswith("task_") else "") + body[:8] + "…")
    return tid if len(tid) <= 22 else tid[:21] + "…"


def _build_receipt_parts(receipt: dict, pal: dict[str, str], width: int = 150) -> dict[str, str]:
    """One Task's Work Receipt as a head plus three bordered regions (Current
    outcome with the two KPI blocks, a four-cell summary strip, and the dimension
    ledger with provenance chips + tool-by-type bars). Replaces the text wall."""

    # The detail pane is the right ~54% of the split; a card inside it has border
    # + padding. This is the usable inner width for rules and wrapping.
    dw = max(46, int(width * 0.54) - 10)

    axes = receipt.get("axes") or {}
    dims = receipt.get("dimensions") or {}
    decision = axes.get("decision_status") or {}
    evidence = axes.get("evidence_strength") or {}
    ev_dim = dims.get("evidence") or {}
    actions = dims.get("actions") or {}
    cost = dims.get("cost") or {}
    dkey = str(decision.get("key"))
    actors = dims.get("actors") or {}
    tid = str(receipt.get("task_id") or "")
    short = _short_task_id(tid)

    # head — breadcrumb, title + decision badge, meta line.
    head_lines = [
        f"[{pal['accent']}]‹ All receipts[/]   [{pal['dim']}]WORK / {_escape(short.upper())}[/]",
        f"[b {pal['ink']}]{_escape(str(receipt.get('title') or 'Task'))}[/]  {decision_badge(dkey, pal)}",
    ]
    updated = _humanize_ago(receipt.get("last_activity_at"), time.time())
    meta = " · ".join(p for p in (
        short, str(actors.get("primary_agent") or ""), ", ".join(actors.get("models") or []),
        (f"updated {updated}" if updated != "—" else ""),
    ) if p)
    if meta:
        head_lines.append(f"[{pal['dim']}]{_escape(meta)}[/]")

    # outcome card — statement + the two KPI blocks + coverage ledger.
    out: list[str] = []
    statement = decision.get("statement")
    if statement:
        tail = f" [{pal['green']}]— {_escape(str(decision.get('asserted_by')))}[/]" if decision.get("asserted_by") else ""
        out.append(f"[{pal['ink']}]{_escape(str(statement))}[/]{tail}")
        out.append(f"[{pal['line']}]{'─' * dw}[/]")  # rule between the statement and the KPI blocks
    if evidence.get("gradeable"):
        claims_val = f"{int(evidence.get('checked_total') or 0)} of {int(evidence.get('checkable_total') or 0)}"
    else:
        claims_val = "not gradeable"
    ctot = int(ev_dim.get("checks_total") or 0)
    cfail = int(ev_dim.get("checks_failed") or 0)
    checks_val = f"{int(ev_dim.get('checks_passed') or 0)} of {ctot}" if ctot else "no runs"
    checks_sub = f"{cfail} failed" if cfail else ("check runs passed" if ctot else "no check runs")
    out.append(_kpi_cells(
        [("Claims supported", claims_val, "claims supported"), ("Check runs", checks_val, checks_sub)],
        pal, 26))
    # (The coverage-ledger one-liner is intentionally omitted here to keep the
    # detail short enough that the tool-by-type bars + footer stay above the fold;
    # the same information lives in the CLAIMS SUPPORTED block's sub-label.)

    # summary strip — a four-cell metric row (its own bordered card).
    tool_total = (sum(int(v or 0) for v in (actions.get("tool_category_counts") or {}).values())
                  or sum(int(v or 0) for v in (actions.get("tool_name_counts") or {}).values()))
    dur = receipt.get("duration_seconds")
    elapsed = humanize_seconds(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0 else "—"
    roots = int(actors.get("root_count") or 0)
    sessions = int(actors.get("session_count") or receipt.get("session_count") or roots or 0)
    summary = _kpi_cells([
        ("Actions", str(tool_total), "tool calls"),
        ("Est. cost", receipt_cost_text(cost), "estimate"),
        ("Elapsed", elapsed, ""),
        ("Sessions", str(sessions) if sessions else "—", f"{roots} root{'s' if roots != 1 else ''}" if roots else ""),
    ], pal, 15)

    # dimensions card — a label-gutter ledger (Task / Actors / Actions+bars /
    # Outcome), a thin rule between rows, then any gaps.
    def _prov(name: str) -> list[str]:
        return (dims.get(name) or {}).get("provenance") or []

    gutter = 9
    cw = max(24, dw - gutter)

    def _chips_line(name: str) -> list[str]:
        chips = _prov_chips(_prov(name), pal).strip()
        return [chips] if chips else []

    ledger_rows: list[tuple[str, list[str]]] = []
    task = dims.get("task") or {}
    objectives = "; ".join((task.get("objectives") or [])[:2]) or "no objective recorded"
    if (task.get("boundary") or {}).get("project"):
        objectives += f" · project {task['boundary']['project']}"
    task_lines = [f"[{pal['ink']}]{_escape(x)}[/]" for x in _wrap_words(objectives, cw)]
    ledger_rows.append(("Task", task_lines + _chips_line("task")))

    actor_parts = [p for p in (
        actors.get("primary_agent"),
        ", ".join(actors.get("models") or []) or None,
        (f"{actors.get('subagent_session_count')} subagents" if actors.get("subagent_session_count") else None),
    ) if p]
    actor_lines = [f"[{pal['ink']}]{_escape(x)}[/]" for x in _wrap_words(" · ".join(actor_parts) or "—", cw)]
    ledger_rows.append(("Actors", actor_lines + _chips_line("actors")))

    # Prefer the specific tool NAMES (Read / Edit / Bash / Grep — the artifact's
    # breakdown); fall back to the coarse categories when names were not captured.
    bar_counts = (actions.get("tool_name_counts") or {}) or (actions.get("tool_category_counts") or {})
    if bar_counts:
        act_lines = [f"[{pal['ink']}]{tool_total} tool calls captured[/][{pal['dim']}] · by type · shared scale[/]"]
        act_lines += _chips_line("actions")
        act_lines += _tool_bars(bar_counts, pal).split("\n")
        ledger_rows.append(("Actions", act_lines))

    # (OUTCOME is intentionally omitted here — it duplicates the CURRENT OUTCOME
    # hero card above; dropping it keeps the ledger short enough for the footer.)

    dl = [_ledger(ledger_rows, pal, gutter, dw)]
    gaps = (dims.get("gaps") or {}).get("items") or []
    if gaps:
        dl.append(f"[{pal['line']}]{'─' * dw}[/]")
        dl.append(f"[{pal['amber']}]{caps(f'Gaps · {len(gaps)} — what could not be proven', pal)}[/]")
        for gap in gaps[:6]:
            dl.append(f"  [{pal['dim']}]\\[{_escape(str(gap.get('dimension')))}][/] [{pal['muted']}]{_escape(str(gap.get('reason')))}[/]")

    # Footer affordance: drill into the sessions & steps behind this receipt.
    n_sessions = int(actors.get("session_count") or receipt.get("session_count") or 0)
    n_checks = int(ev_dim.get("checks_total") or 0)
    bits = []
    if n_sessions:
        bits.append(f"{n_sessions} session{'s' if n_sessions != 1 else ''}")
    if n_checks:
        bits.append(f"{n_checks} check{'s' if n_checks != 1 else ''}")
    tail = f" [{pal['dim']}]({' · '.join(bits)})[/]" if bits else ""
    dl.append(f"[{pal['line']}]{'─' * dw}[/]")
    dl.append(f"[{pal['accent']}]↵ Open sessions & steps{tail} [{pal['accent']}]→[/]")

    return {
        "head": "\n".join(head_lines),
        "outcome_title": "CURRENT OUTCOME", "outcome": "\n".join(out),
        "summary_title": "", "summary": summary,
        "dims_title": "RECEIPT DIMENSIONS", "dims": "\n".join(dl),
    }


def _check_rows(check: dict, pal: dict[str, str], now: float) -> list[str]:
    """One check as a two-line entry: a result-tagged headline (glyph + Result +
    kind + summary) and a dim meta line (exit code · source · age · provenance)."""

    glyph, col = check_mark(check.get("result"), pal)
    rlabel = str(check.get("result") or "check").capitalize()
    kind = str(check.get("kind") or "").capitalize()
    text = str(check.get("summary") or check.get("name") or "recorded check")
    head = (f"[{col}]{glyph} {rlabel}[/] [{pal['muted']}]{_escape(kind)}[/]  "
            f"[{pal['ink']}]{_escape(text)}[/]")
    meta: list[str] = []
    code = check.get("exit_code")
    if code is not None:
        meta.append(f"exit {int(code)}")
    src = _PROVENANCE_LABEL.get(str(check.get("source")), str(check.get("source") or ""))
    if src and src != "—":
        meta.append(src)
    ago = _humanize_ago(check.get("at"), now)
    if ago != "—":
        meta.append(ago)
    line2 = f"  [{pal['dim']}]{_escape(' · '.join(meta) or 'no metadata')}[/]"
    if check.get("artifact_ref"):
        line2 += f"  [{pal['muted']} on {pal['chip']}] {_escape(str(check['artifact_ref']))} [/]"
    elif check.get("command_redacted"):
        line2 += f"  [{pal['dim']}]· command redacted[/]"
    return [head, line2]


def _build_steps_parts(receipt: dict, checks: list[dict], pal: dict[str, str], width: int = 150) -> dict[str, str]:
    """The sessions & steps drill-down: a checks timeline grouped into NEEDS
    ATTENTION (failing) and OTHER CURRENT CHECKS (passed/skipped), plus files.
    Mirrors the artifact's sessions-&-steps frame."""

    now = time.time()
    dw = max(46, int(width * 0.54) - 10)
    axes = receipt.get("axes") or {}
    decision = axes.get("decision_status") or {}
    evidence = axes.get("evidence_strength") or {}
    dkey = str(decision.get("key"))
    ekey = str(evidence.get("key"))
    short = _short_task_id(str(receipt.get("task_id") or ""))
    title = str(receipt.get("title") or "Task")

    head = f"[{pal['accent']}]‹ Receipt[/]   [{pal['dim']}]WORK / {_escape(short.upper())} / SESSIONS[/]"

    def _cap(text: str, color: str) -> str:
        return f"[{color}]{_escape(text.upper())}[/]"

    attn = [c for c in checks if str(c.get("result")) in ("failed", "error")]
    other = [c for c in checks if str(c.get("result")) not in ("failed", "error")]
    passed = sum(1 for c in checks if str(c.get("result")) == "passed")
    skipped = sum(1 for c in checks if str(c.get("result")) == "skipped")

    body: list[str] = []
    # Session summary.
    body.append(f"{pip(ekey, pal)} [b {pal['ink']}]{_escape(title)}[/]  {decision_badge(dkey, pal)}")
    counts = " · ".join(p for p in (
        f"{passed} passed" if passed else "",
        f"{len(attn)} failed" if attn else "",
        f"{skipped} skipped" if skipped else "",
    ) if p) or "no checks recorded"
    updated = _humanize_ago(receipt.get("last_activity_at"), now)
    tail = f" · updated {updated}" if updated != "—" else ""
    body.append(f"[{pal['dim']}]{_escape(counts)} · {_escape(decision_label(dkey).lower())}{tail}[/]")
    statement = decision.get("statement")
    if statement:
        body.append(f"[{pal['accent']}]↳[/] [{pal['ink']}]{_escape(str(statement))}[/]")
    if attn and dkey not in _DANGER:
        body.append(f"[{pal['amber']}]Marked done, but a recorded check is currently failing.[/]")

    # Needs attention (failing checks).
    if attn:
        body.append("")
        body.append(f"[{pal['line']}]{'─' * dw}[/]")
        body.append(_cap(f"Needs attention · {len(attn)}", pal["coral"]))
        body.append("")
        for c in attn:
            body.extend(_check_rows(c, pal, now))
            body.append("")

    # Other current checks (passed / skipped), capped.
    body.append(f"[{pal['line']}]{'─' * dw}[/]")
    body.append(_cap(f"Other current checks · {len(other)}", pal["dim"]))
    body.append("")
    shown = other[:4]
    for c in shown:
        body.extend(_check_rows(c, pal, now))
        body.append("")
    if len(other) > len(shown):
        body.append(f"[{pal['accent']}]▾ Show {len(other) - len(shown)} more current checks[/]")
        body.append("")

    # Files touched by the checks.
    files: list[str] = []
    seen: set[str] = set()
    for c in checks:
        for f in (c.get("files") or []):
            f = str(f)
            if f and f not in seen:
                seen.add(f)
                files.append(f)
    if files:
        body.append(f"[{pal['line']}]{'─' * dw}[/]")
        body.append(_cap(f"Files · {len(files)}", pal["dim"]))
        for f in files[:4]:
            body.append(f"[{pal['muted']}]{_escape(f)}[/]")

    return {"head": head, "title": "SESSIONS & STEPS", "body": "\n".join(body).rstrip("\n")}


def _is_weekly(window: Any) -> bool:
    lab = str(getattr(window, "label", "")).lower()
    return "7" in lab or "week" in lab


def _order_windows(windows: Any) -> list:
    """Lead with the weekly window (the meaningful subscription budget, the GUI's
    choice), then the rest — the artifact's Weekly-then-5-hour order."""
    return sorted(list(windows or []), key=lambda w: 0 if _is_weekly(w) else 1)


def _window_label(window: Any) -> tuple[str, str]:
    """(name, compact-kind) for a capacity window — ``Weekly 7d`` / ``5-hour 5h``."""
    lab = str(getattr(window, "label", ""))
    if _is_weekly(window):
        return "Weekly", "7d"
    low = lab.lower()
    if "5" in low and ("hour" in low or "h" in low):
        return "5-hour", "5h"
    return lab, ""


def _reset_text(resets_at: Any, now: float, pal: dict[str, str]) -> str:
    if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool) and resets_at > 0:
        delta = resets_at - now
        return f"[{pal['dim']}]· resets in {humanize_seconds(delta)}[/]" if delta > 0 else f"[{pal['dim']}]· resets now[/]"
    return f"[{pal['dim']}]· reset time not reported[/]"


def _build_usage_parts(
    page: UsagePage,
    limits: list[ClientLimit],
    snap: LiveSnapshot | None,
    label: str,
    pal: dict[str, str],
    width: int = 150,
) -> dict[str, str]:
    now = time.time()
    cap_w = max(60, width - 10)
    head = f"[b {pal['ink']}]Usage & limits[/]   [{pal['dim']}]provider-reported capacity · locally recorded use[/]"
    if snap is not None:
        today_cost = cost_text(_window_totals(snap, "today"))
        head += (f"\n{caps('Today · all agents', pal)}  [b {pal['ink']}]{abbr_tokens(_window_total(snap, 'today'))}[/] "
                 f"[{pal['dim']}]fresh tokens[/]")
        if today_cost != "—":
            head += f"   [{pal['ink']}]{_escape(today_cost)}[/] [{pal['dim']}]est. this period[/]"

    by_client = {str(r.get("client")): r for r in (snap.usage.by_client if snap else [])}

    def _recorded_stack(rec: dict | None) -> list[str]:
        """The recorded-use right column, stacked (tokens / sessions / cost) so it
        aligns down the client's title + window rows — the artifact's right column."""
        if not rec:
            return []
        return [
            f"[b {pal['ink']}]{abbr_tokens(rec.get('total_tokens_including_cached'))}[/] [{pal['dim']}]fresh[/]",
            f"[{pal['dim']}]{format_tokens(rec.get('sessions'))} sessions[/]",
            f"[{pal['dim']}]{_escape(cost_text(rec))}[/]",
        ]

    fresh = [limit for limit in limits if not limit_is_stale(limit, now)]
    shown: set[str] = set()
    cap_lines: list[str] = []
    if not fresh and not by_client:
        cap_lines.append(f"[{pal['dim']}]No provider limit data recorded yet — use your agents, or run "
                         f"`agentacct usage watch`.[/]")
    else:
        cap_lines.append(_two_edge(
            f"[{pal['dim']}]{'CLIENT':<14}PROVIDER WINDOW[/]",
            f"[{pal['dim']}]RECORDED USE · {label.upper()}[/]", cap_w))
        cap_lines.append("")

    def _emit(left_rows: list[str], rec: dict | None) -> None:
        """Zip a client's left rows against its stacked recorded-use column so the
        figures right-align down the block."""
        right = _recorded_stack(rec)
        for i, lrow in enumerate(left_rows):
            cap_lines.append(_two_edge(lrow, right[i] if i < len(right) else "", cap_w))

    for limit in fresh:
        client = str(limit.client)
        shown.add(client)
        name = f"[b {pal['ink']}]{_escape(client)}[/]"
        if limit.plan_type:
            name += f"  [{pal['dim']}]{_escape(str(limit.plan_type))}[/]"
        left_rows = [name]
        for wi, window in enumerate(_order_windows(limit.windows)):
            if wi:  # a breath between a client's stacked meters (artifact rhythm)
                left_rows.append("")
            disp, kind = _window_label(window)
            wlabel = f"[{pal['muted']}]{disp:<7}[/][{pal['dim']}]{kind:<3}[/]"
            used = window.used_percent
            if used is None:
                left_rows.append(f"  {wlabel} [{pal['dim']}]not reported[/]")
                continue
            # Two rows per meter (the artifact's rhythm): the bar on the label row,
            # then the "% used · reset" caption on its own indented line below.
            left_rows.append(f"  {wlabel} {meter(used / 100.0, 26, pal)}")
            left_rows.append(f"            [{pal['accent']}]{used:.0f}% used[/]  "
                             f"{_reset_text(window.resets_at, now, pal)}")
        _emit(left_rows, by_client.get(client))
        cap_lines.extend(["", ""])  # two-line gutter between clients (artifact rhythm)
    # Clients with recorded usage but no provider limit (e.g. hermes): still shown,
    # with the honest "provider limit not reported" state (mirrors the artifact).
    for client, rec in by_client.items():
        if client in shown:
            continue
        _emit([f"[b {pal['ink']}]{_escape(client)}[/]",
               f"  [{pal['dim']}]provider limit not reported[/]"], rec)
        cap_lines.extend(["", ""])

    totals = page.totals or {}
    active_days = sum(
        1 for p in (page.by_period or [])
        if str(p.get("period")) != "unknown" and float(p.get("total_tokens_including_cached") or 0) > 0
    )
    period_days = len([p for p in (page.by_period or []) if str(p.get("period")) != "unknown"])
    rec_cells = [
        ("Tokens", abbr_tokens(totals.get("total_tokens_including_cached")), "fresh · client-reported"),
        ("Sessions", format_tokens(totals.get("sessions")), "with recorded usage"),
        ("Cost", cost_text(totals), "cost basis varies"),
    ]
    if period_days:
        rec_cells.append(("Active days", f"{active_days}/{period_days}", "with recorded usage"))
    rec_body = _kpi_cells(rec_cells, pal, 16)
    return {
        "head": head,
        "cap_title": "CURRENT CAPACITY", "cap": "\n".join(cap_lines).rstrip("\n") or f"[{pal['dim']}]—[/]",
        "rec_title": f"RECORDED USAGE · {label.upper()}", "rec": rec_body,
    }


def _monogram(name: str) -> str:
    parts = [p for p in name.replace("_", "-").split("-") if p]
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    if len(name) > 1:
        return (name[0] + name[-1]).upper()
    return name.upper()


def _loz(text: str, color: str, wash: str, glyph: str) -> str:
    return f"[{color} on {wash}] {glyph} {_escape(text)} [/]"


def _source_detail(s: dict, watcher_running: bool) -> str:
    parts: list[str] = []
    scope = s.get("scope")
    if scope:
        parts.append("configured" if (scope == "watched" and not watcher_running) else str(scope))
    if s.get("discovered") is not None:
        parts.append(f"{s.get('discovered')} discovered")
    if s.get("parsed") is not None:
        parts.append(f"{s.get('parsed')} parsed")
    if s.get("skipped"):
        parts.append(f"{s.get('skipped')} skipped")
    return " · ".join(parts) if parts else "no scan recorded"


def _source_lozenge(s: dict, running: bool, pal: dict[str, str]) -> str:
    state = str(s.get("state") or "unknown")
    parsed = int(s.get("parsed") or 0)
    if state == "healthy" and running and parsed > 0:
        return _loz("Reporting", pal["green"], pal["tg"], "●")
    if state == "healthy" and running:
        return _loz("Watching · no data yet", pal["muted"], pal["tn"], "○")
    if state == "healthy":
        return _loz("Idle", pal["muted"], pal["tn"], "○")
    if state == "degraded":
        return _loz("Degraded", pal["amber"], pal["tm"], "○")
    if state == "pending":
        return _loz("Pending", pal["muted"], pal["tn"], "○")
    return _loz(state.capitalize(), pal["muted"], pal["tn"], "○")


def _overall_lozenge(state: str, running: bool, pal: dict[str, str]) -> str:
    if state == "healthy" and running:
        return _loz("Reporting", pal["green"], pal["tg"], "●")
    if state == "healthy":
        return _loz("Idle", pal["muted"], pal["tn"], "○")
    if state == "degraded":
        return _loz("Degraded", pal["amber"], pal["tm"], "○")
    return _loz(state.capitalize() or "Unknown", pal["muted"], pal["tn"], "○")


def _watcher_lozenge(watcher: dict, pal: dict[str, str]) -> str:
    state = str(watcher.get("state") or "")
    if state == "running":
        return _loz("Running", pal["green"], pal["tg"], "●")
    if state == "stale":
        return _loz("Stale", pal["amber"], pal["tm"], "○")
    if state == "stopped":
        return _loz("Stopped", pal["coral"], pal["tc"], "○")
    if state == "not_configured":
        return _loz("Not configured", pal["muted"], pal["tn"], "○")
    return _loz("Unknown", pal["muted"], pal["tn"], "○")


def _watcher_detail(watcher: dict) -> str:
    if not watcher:
        return "The daemon reported no watcher block."
    heartbeat = _humanize_ago(watcher.get("heartbeat_at"), time.time())
    hb = f"last heartbeat {heartbeat}" if heartbeat != "—" else "no heartbeat recorded"
    cadence = watcher.get("interval_seconds")
    cad = f" · scans every {int(cadence)}s" if cadence else ""
    state = str(watcher.get("state") or "")
    if state == "running":
        return f"The importer keeps the store current in the background — {hb}{cad}"
    if state == "stale":
        return f"The importer's heartbeat is overdue — {hb}{cad}"
    if state == "stopped":
        return f"Importer stopped — {hb}. Start it with `agentacct start`."
    if state == "not_configured":
        return "No continuous sync configured — imports happen only on manual scans."
    return hb


def _build_sources_parts(snapshot: dict, store_dir: Any, pal: dict[str, str], width: int = 150) -> dict[str, str]:
    card_w = max(60, width - 10)
    head = (f"[b {pal['ink']}]Evidence sources[/]   "
            f"[{pal['dim']}]what feeds the store · capture is local only[/]")

    if snapshot.get("_error"):
        return {
            "head": head,
            "connected_title": "CONNECTED SOURCES",
            "connected": f"[{pal['muted']}]Source health unavailable — {_escape(str(snapshot['_error']))}[/]",
            "watcher_title": "CONTINUOUS SYNC", "watcher": f"[{pal['dim']}]—[/]",
            "verifiers_title": "VERIFIERS · NOT CONNECTED · UPGRADE SELF-CHECKED → VERIFIED", "verifiers": _verifiers_markup(pal),
            "issues_title": "", "issues": "",
            "local": _sources_local_markup(store_dir, pal, card_w),
        }

    state = str(snapshot.get("state") or "")
    watcher = snapshot.get("watcher") or {}
    running = str(watcher.get("state") or "") == "running"
    sources = sorted(snapshot.get("sources") or [], key=lambda s: str(s.get("source")))

    conn: list[str] = []
    if not sources:
        conn.append(f"[{pal['muted']}]No import sources configured — run `agentacct onboard` to wire your agents.[/]")
    for s in sources:
        errs = int(s.get("error_count") or 0)
        err = f"  [{pal['coral']}]{errs} error{'s' if errs != 1 else ''}[/]" if errs else ""
        # Name (with monogram) left, status lozenge pinned right — the artifact's
        # two-edge source row.
        left = (f"[{pal['muted']} on {pal['tn']}] {_monogram(str(s.get('source')))} [/] "
                f"[b {pal['ink']}]{_escape(str(s.get('source')))}[/]")
        conn.append(_two_edge(left, f"{_source_lozenge(s, running, pal)}{err}", card_w))
        ago = _humanize_ago(s.get("last_success_at"), time.time())
        detail = f"[{pal['dim']}]{_escape(_source_detail(s, running))}[/]"
        imp = f"[{pal['dim']}]last import {ago}[/]" if ago != "—" else f"[{pal['dim']}]no successful import yet[/]"
        conn.append(_two_edge("     " + detail, imp, card_w))

    watcher_body = _two_edge(
        f"[{pal['muted']}]{_escape(_watcher_detail(watcher))}[/]", _watcher_lozenge(watcher, pal), card_w)

    issues = snapshot.get("issues") or []
    issue_lines: list[str] = []
    for issue in issues:
        code = str(issue.get("code") or "issue").replace("_", " ")
        src = f" — {issue.get('source')}" if issue.get("source") else ""
        issue_lines.append(f"[{pal['amber']}]{_escape(code.capitalize() + src)}[/]")
        if issue.get("action"):
            issue_lines.append(f"  [{pal['dim']}]{_escape(str(issue.get('action')))}[/]")

    return {
        "head": head,
        "connected_title": f"CONNECTED SOURCES · {len(sources)}", "connected": "\n".join(conn),
        "watcher_title": "CONTINUOUS SYNC", "watcher": watcher_body,
        "verifiers_title": "VERIFIERS · NOT CONNECTED · UPGRADE SELF-CHECKED → VERIFIED", "verifiers": _verifiers_markup(pal, card_w),
        "issues_title": f"NEEDS ATTENTION · {len(issues)}", "issues": "\n".join(issue_lines),
        "local": _sources_local_markup(store_dir, pal, card_w),
    }


def _verifiers_markup(pal: dict[str, str], width: int = 74) -> str:
    """The evidence-upgrade path: what would raise self-checked steps to
    externally-verified (no such source is wired yet). Two two-edge rows — a
    verifier name on the left, its ``◉ → verified`` promotion pinned right; the
    'upgrade self-checked → verified' framing lives in the card's border title."""

    ev = f"[{pal['green']}]◉[/] [{pal['accent']}]→ verified[/]"
    return (
        _two_edge(f"[b {pal['ink']}]CI check runs[/]", ev, width) + "\n"
        f"[{pal['dim']}]  independent check results recorded against receipts[/]\n"
        + _two_edge(f"[b {pal['ink']}]Human reviewer[/]", ev, width) + "\n"
        f"[{pal['dim']}]  finding review and approval dispositions[/]"
    )


def _sources_local_markup(store_dir: Any, pal: dict[str, str], width: int = 74) -> str:
    head = _two_edge(
        f"[{pal['green']}]●[/] [b {pal['ink']}]Nothing leaves this machine[/]",
        f"[{pal['dim']}]store: {_escape(str(store_dir))}[/]", width)
    return (f"{head}\n"
            f"[{pal['dim']}]Reads tool names, commands, file paths, exit codes, timestamps, and token "
            f"counts from your agents' own local logs — never file contents or prompts.[/]")


def _capacity_block(limits: list[ClientLimit], pal: dict[str, str]) -> str:
    """The headroom rail block — prefer a weekly (7-day) window, the GUI's choice,
    since it is the meaningful subscription budget; fall back to any window."""

    now = time.time()
    fresh = [limit for limit in limits if not limit_is_stale(limit, now)]

    def _windows_by_preference(limit: ClientLimit):
        weekly = [w for w in limit.windows if "7" in str(w.label) or "week" in str(w.label).lower()]
        return weekly + [w for w in limit.windows if w not in weekly]

    for limit in fresh:
        for window in _windows_by_preference(limit):
            used = window.used_percent
            if used is not None:
                headroom = max(0.0, 100.0 - used)
                return _rail_block(
                    "Capacity", f"{_escape(str(limit.client))} · {headroom:.0f}% headroom",
                    f"{used:.0f}% of {_escape(str(window.label))} · provider reported", pal)
    return _rail_block("Capacity", "no limit reported", "run `agentacct usage watch`", pal)


def _trust_block(ingestion: dict, pal: dict[str, str]) -> str:
    state = str((ingestion or {}).get("state") or "")
    watcher = (ingestion or {}).get("watcher") or {}
    running = str(watcher.get("state") or "") == "running"
    sources = (ingestion or {}).get("sources") or []
    last = max((float(s.get("last_success_at") or 0) for s in sources), default=0.0)
    ingest_sub = f"last successful ingest {_humanize_ago(last, time.time())}" if last else "no ingest recorded yet"
    if state == "healthy" and running:
        return _rail_block("Evidence trust", "Sources healthy", ingest_sub, pal, label_color=pal["green"])
    if state == "healthy":
        return _rail_block("Evidence trust", "Sources idle", "watcher stopped", pal, label_color=pal["green"])
    if state == "degraded":
        return _rail_block("Evidence trust", "Sources degraded", ingest_sub, pal, label_color=pal["amber"])
    if not ingestion:
        return _rail_block("Evidence trust", "unavailable", "source health not reported", pal)
    return _rail_block("Evidence trust", state or "unknown", ingest_sub, pal)


def _window_total(snap: LiveSnapshot, label: str) -> Any:
    for window in snap.usage.windows:
        if str(window.label) == label:
            return window.totals.get("total_tokens_including_cached")
    return 0


def _window_totals(snap: LiveSnapshot, label: str) -> dict:
    for window in snap.usage.windows:
        if str(window.label) == label:
            return dict(window.totals or {})
    return {}


def _run() -> None:  # pragma: no cover - manual entrypoint parity
    raise SystemExit("Run via `agentacct tui`.")
