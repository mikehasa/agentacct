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
import uuid
from pathlib import Path
from typing import Any

from rich.cells import cell_len as _cell_len, set_cell_size as _set_cells
from rich.markup import escape as _escape
from rich.text import Text as _RText
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import ContentSwitcher, DataTable, Footer, Input, ListItem, ListView, Static

from .plural import count_noun
from .service import SentinelService
from .display_vocabulary import (
    ATTENTION_OPEN_ACTION,
    ATTENTION_REASON_LABELS,
    ATTENTION_SORT_TEXT,
    GROUP_DEFINITIONS,
    attention_count_text,
    decision_group,
    receipt_field_label,
    COST_BASIS_NOT_REPORTED,
    DECISION_LABELS,
    check_result_label,
    cost_display,
    check_result_tone,
    evidence_grade_label,
    RECEIPT_FIELD_LABELS,
    RECORDED_USAGE_TITLE,
    WINDOW_LABELS,
    data_age_text,
    decision_label,
    limit_used_text,
    relative_age_text,
    reset_text,
    source_label,
    source_tier_key,
)
from .usage_snapshot import (
    ClientLimit,
    LiveSnapshot,
    UsagePage,
    build_client_limits,
    build_live_snapshot,
    build_usage_page,
    cost_basis_caption,
    cost_text,
    format_tokens,
    headline_limit_choice,
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
    # Per-agent source hues (mirror Swift Theme.sourceColor): Claude=accent,
    # Codex=purple, OpenCode=teal, Hermes=magenta; every other client = muted.
    "codex": "#B6A2F0", "opencode": "#53C6D6", "hermes": "#E39AC8",
    "ta": "#24365C", "tg": "#1E3B2F", "tm": "#3D3420", "tc": "#412620",
    "tn": "#2A343B", "chip": "#232E34", "spark": "#31456F",
}
_LIGHT: dict[str, str] = {
    "bg": "#F4F1E9", "chrome": "#FCFBF7", "panel": "#FFFFFF", "sel": "#E7EDF8",
    "line": "#DDDACF", "hair": "#E4E1D7",
    "ink": "#171A1D", "muted": "#59636B", "dim": "#79848B",
    "accent": "#245BDB", "green": "#1F7653", "amber": "#7A5A00", "coral": "#B63F2F",
    # Per-agent source hues (mirror Swift Theme.sourceColor): Claude=accent,
    # Codex=purple, OpenCode=teal, Hermes=magenta; every other client = muted.
    "codex": "#6A4BC0", "opencode": "#0E8494", "hermes": "#A5457F",
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

# Evidence tier grade -> (pip glyph, palette colour key). Shape is the tier;
# colour is redundant (v10 rule 1). ``◉`` = a filled disc in a ring, ``●``
# filled, ``◐`` half, ``○`` hollow. The WORDS are never kept here: a tier's
# label comes from the shared vocabulary (``evidence_grade_label``), so the
# terminal cannot grow a hyphenated second spelling of "externally verified".
_TIER: dict[str, tuple[str, str]] = {
    "externally_verified": ("◉", "green"),
    "independently_checked": ("●", "ink"),
    "self_checked": ("◐", "accent"),
    "claimed": ("○", "amber"),
    "unchecked": ("○", "amber"),
    "none": ("○", "dim"),
}


def tier_style(grade: str | None) -> tuple[str, str, str]:
    """``(pip glyph, palette colour key, label)`` for one evidence grade. The
    label is the vocabulary's word for that grade, never a local spelling."""

    glyph, color = _TIER.get(str(grade or "none"), ("○", "dim"))
    return glyph, color, evidence_grade_label(grade)


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

# Work lifecycle tabs: the group keys and labels the shared vocabulary owns
# (the app's tabs read the same table). A task lands in exactly one group.
_WORK_TABS: tuple[tuple[str, str], ...] = (
    ("all", "All"),
    *((key, label) for key, (label, _definition) in GROUP_DEFINITIONS.items()),
)
def task_bucket(summary: dict) -> str:
    """The single filter group a receipt summary belongs to: the payload's
    ``group_key``, else the shared vocabulary's rule over the reducer's
    attention predicate (never a local mapping)."""

    group = str(summary.get("group_key") or "").strip()
    if group:
        return group
    decision = summary.get("decision_status") or {}
    open_now = summary.get("attention_open")
    if open_now is None and isinstance(summary.get("attention"), dict):
        open_now = summary["attention"].get("open")
    return decision_group(decision.get("key"), open_now)


def attention_rank(summary: dict) -> tuple[int, float]:
    """The ONE sort key every TUI surface uses for the queue, straight from the
    reducer: its ``attention_order`` class (0 failed checks and steps, 1
    blockers, 2 checks that could not run) then most recent. Stated once as
    ``ATTENTION_SORT_TEXT``; no surface adds a rule of its own."""

    order = summary.get("attention_order")
    rank = int(order) if isinstance(order, int) and not isinstance(order, bool) else 99
    return (rank, -float(summary.get("last_activity_at") or 0.0))


def attention_rows(summaries: list[dict]) -> list[dict]:
    """The Attention queue for a list of receipt summaries: the tasks whose
    payload group IS the queue, in the reducer's order. The Work tabs and the
    Dashboard's shift brief read the same two functions, so the terminal can
    never show a queue the app and ``/v1/attention`` disagree with."""

    return sorted((s for s in summaries if task_bucket(s) == "attention"), key=attention_rank)

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


def check_mark(
    result: str | None,
    pal: dict[str, str],
    tier_key: str | None = None,
) -> tuple[str, str]:
    """(glyph, colour) for a machine-check result, keyed on the shared result
    tone: ✓ pass / ✗ failure (coral, a recorded failure only) / − not run
    (muted: could not run, skipped, or no result recorded).

    A PASS wears the tier its source can support — green ONLY for externally
    verified, ink for an unknown or unattributed source (Swift
    ``CheckResultTone.tint(pass:)``). Green is the live connection and the
    externally-verified tier; a check passing under its own agent's word is not
    either, so it never turns the terminal green."""

    tone = check_result_tone(result)
    if tone == "pass":
        return "✓", pal[_TIER.get(str(tier_key or ""), ("", "ink"))[1]]
    if tone == "failure":
        return "✗", pal["coral"]
    return "−", pal["dim"]


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
    """Terminal CELL width of a Rich-markup string — tags stripped, escaped ``\\[``
    counted as one column, and every glyph measured by how many columns it
    actually occupies (a CJK/wide character is 2, a zero-width combining mark 0).
    Character COUNT would drift on non-ASCII text and misalign every column."""

    stripped = _MARKUP_TAG.sub("", markup).replace("\\[", "[")
    return _cell_len(stripped)


def _two_edge(left: str, right: str, width: int) -> str:
    """Left content + right content flush to ``width`` (the artifact's two-edge
    rows: a label on the left, a status/total pinned to the right)."""

    pad = max(1, width - _plainlen(left) - _plainlen(right))
    return f"{left}{' ' * pad}{right}"


def receipt_cost_text(cost: dict) -> str:
    """The cost string the PAYLOAD already carries — never re-derived here.

    Every receipt cost object (the detail dimension and each summary row) ships
    ``display_text`` from the one cost reducer, so the TUI prints it. A bare
    cube/usage bucket without that field falls back to the shared
    :func:`agentacct.usage_snapshot.cost_text`, which is the same grammar and —
    crucially — the same NAMED absence: a bucket with zero rows reads ``no usage
    recorded``, not ``unpriced``. Deriving it locally here is what let the Work
    card call a Task with no usage at all "unpriced" while its own receipt said
    "no usage recorded".
    """

    shown = str(cost.get("display_text") or "").strip()
    if shown:
        return shown
    return cost_text({**cost, "cost_confidence": cost.get("cost_confidence") or cost.get("cost_basis")})


def cost_string_or_absent(
    usd: Any,
    *,
    complete: Any,
    confidence: Any,
    known_additive: Any = None,
) -> str | None:
    """A worksets/timeline figure in the ONE cost grammar
    (:func:`agentacct.display_vocabulary.cost_display`), or ``None`` when nothing
    is priced so the CALLER names the absence — never a fabricated ``$0``.

    The worksets pane and the lane facts card grew their own copy of this
    grammar; they now read the shared vocabulary instead, so ``$``/``≈$``/``~$``
    and the named absence can only be spelled in one place.
    """

    shown = cost_display(
        usd,
        complete,
        confidence,
        partial_amount=known_additive if known_additive is not None else usd,
    )
    if shown["state"] in ("unpriced", "no_usage"):
        return None
    return str(shown["display_text"])


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
        return relative_age_text(now - ts)
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
    ("1 – 5", "switch pane · Dashboard / Work / Sessions / Usage / Diagnostics"),
    ("↑ ↓ / j k", "move cursor · detail follows"),
    ("↵", "open · a work group's timeline, or drill into a receipt"),
    ("esc", "close this help overlay"),
    ("/", "filter the current list (Sessions)"),
    ("[ ]", "previous / next status tab (Sessions)"),
    ("s", "cycle sort — attention / latest / cost (Sessions)"),
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


class WorksetDetailScreen(ModalScreen):
    """One work group's full cross-agent timeline, keyboard-zoomable. The honest
    terminal answer to the app's pinch/drag canvas: ↑↓ move a scrubber cursor
    (its session's facts read out below), +/− zoom around it (discrete steps, the
    exact WorksetZoomWindow math), 0 resets, ←→ pan. Positions snap to cells."""

    BINDINGS = [
        Binding("down", "focus(1)", "Move", key_display="↑↓"),
        Binding("up", "focus(-1)", "Move", show=False),
        Binding("k", "focus(-1)", show=False),
        Binding("j", "focus(1)", show=False),
        Binding("plus", "zoom(1.6)", "Zoom in"),
        Binding("equals_sign", "zoom(1.6)", show=False),
        Binding("minus", "zoom(0.625)", "Zoom out"),
        Binding("underscore", "zoom(0.625)", show=False),
        Binding("0", "reset_zoom", "Reset"),
        Binding("right", "pan(1)", "Pan", key_display="←→"),
        Binding("left", "pan(-1)", "Pan", show=False),
        Binding("h", "pan(-1)", show=False),
        Binding("l", "pan(1)", show=False),
        Binding("escape", "close", "Back"),
        Binding("q", "close", "Back", show=False),
    ]

    def __init__(self, card: dict, pal: dict[str, str]) -> None:
        super().__init__()
        self._card = card
        self._pal = pal
        self._lanes = [l for l in (card.get("sessions") or []) if isinstance(l, dict)]
        self._lo, self._hi = _timeline_bounds(self._lanes)
        self._zoom = 1.0
        self._pan = 0.5
        self._focus = 0
        self._recenter()
        self._body_text = self._body_markup()  # plain-string mirror for headless tests

    def compose(self) -> ComposeResult:
        # A scroll container (not a plain Vertical): the full timeline + facts can
        # exceed the box's max height, and an un-scrollable overflow fails to lay
        # out. Content is built in compose so the Static is populated on first
        # render.
        # The box must NOT take focus: a focusable VerticalScroll would swallow the
        # arrow keys for scrolling, so ↑↓ (move scrubber) / ←→ (pan) would never
        # reach the screen bindings — and the Footer, which mirrors the active
        # bindings, would drop them. With focus on the screen the scrubber drives
        # the arrows; the mouse wheel still scrolls a tall body.
        box = VerticalScroll(Static(self._body_text, id="ws-detail-body"), id="ws-detail-box")
        box.can_focus = False
        yield box
        yield Footer()

    def on_mount(self) -> None:
        # After the first layout the body Static has a real content width; repaint
        # against it so the axis/track fit exactly (the compose-time estimate is
        # conservative). Named on_mount (a message handler) — never _render.
        self.call_after_refresh(self._repaint)

    def _content_width(self) -> int:
        try:
            w = int(self.query_one("#ws-detail-body", Static).content_size.width)
            if w > 10:
                return w
        except Exception:  # noqa: BLE001
            pass
        app = getattr(self, "app", None)
        return max(48, int(getattr(getattr(app, "size", None), "width", 0) or 150) - 18)

    def _body_markup(self) -> str:
        return _workset_detail_markup(
            self._card, self._pal, self._content_width(), zoom=self._zoom, pan=self._pan, focus_key=self._focus_key()
        )

    def _focus_key(self) -> str | None:
        if 0 <= self._focus < len(self._lanes):
            return str(self._lanes[self._focus].get("session_key"))
        return None

    def _focus_time(self) -> float | None:
        if 0 <= self._focus < len(self._lanes):
            t = self._lanes[self._focus].get("first_activity_at")
            if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0:
                return float(t)
        return None

    def _recenter(self) -> None:
        ft = self._focus_time()
        if ft is not None and self._lo is not None and self._hi is not None and self._hi > self._lo:
            self._pan = min(1.0, max(0.0, (ft - self._lo) / (self._hi - self._lo)))

    def _repaint(self) -> None:
        # NB: never name this ``_render`` — Textual reserves ``Widget._render``
        # (it must return the visual; a None-returning override crashes the
        # compositor with 'NoneType has no attribute render_strips').
        body = self._body_markup()
        self._body_text = body
        try:
            self.query_one("#ws-detail-body", Static).update(body)
        except Exception:  # noqa: BLE001
            pass

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_focus(self, delta: int) -> None:
        if not self._lanes:
            return
        self._focus = min(len(self._lanes) - 1, max(0, self._focus + delta))
        self._recenter()
        self._repaint()

    def action_zoom(self, factor: float) -> None:
        if self._lo is None or self._hi is None or self._hi <= self._lo:
            return
        # After a recenter the focused session sits at window centre, so anchoring
        # the zoom at 0.5 keeps it fixed under the cursor.
        self._zoom, self._pan = _ZoomWindow.apply_zoom(self._zoom, self._pan, factor, 0.5, self._lo, self._hi)
        self._repaint()

    def action_reset_zoom(self) -> None:
        self._zoom, self._pan = 1.0, 0.5
        self._recenter()
        self._repaint()

    def action_pan(self, direction: int) -> None:
        if self._lo is None or self._hi is None or self._hi <= self._lo:
            return
        win = _ZoomWindow(self._lo, self._hi, self._zoom, self._pan)
        step = (win.end - win.start) * 0.25 * direction
        full = self._hi - self._lo
        self._pan = min(1.0, max(0.0, self._pan + (step / full if full else 0.0)))
        self._repaint()


class _WorksetPromptScreen(ModalScreen):
    """A one-line text prompt (rename). Enter submits, esc cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, initial: str, pal: dict[str, str], on_submit) -> None:
        super().__init__()
        self._title = title
        self._initial = initial
        self._pal = pal
        self._on_submit = on_submit

    def compose(self) -> ComposeResult:
        pal = self._pal
        with Vertical(id="ws-prompt-box"):
            yield Static(f"[b {pal['ink']}]{_escape(self._title)}[/]", id="ws-prompt-title")
            yield Input(value=self._initial, placeholder="Name", id="ws-prompt-input")
            yield Static(f"[{pal['dim']}][b {pal['accent']}]↵[/] save   [b {pal['accent']}]esc[/] cancel[/]")

    def on_mount(self) -> None:
        inp = self.query_one("#ws-prompt-input", Input)
        inp.focus()
        inp.cursor_position = len(self._initial)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ws-prompt-input":
            self.app.pop_screen()
            self._on_submit(event.value.strip())

    def action_cancel(self) -> None:
        self.app.pop_screen()


class _WorksetConfirmScreen(ModalScreen):
    """A destructive-action confirm (delete). y/Enter confirms, n/esc cancels."""

    BINDINGS = [
        Binding("y", "confirm", "Delete"),
        Binding("enter", "confirm", "Delete", show=False),
        Binding("n", "cancel", "Keep", show=False),
        Binding("escape", "cancel", "Keep"),
    ]

    def __init__(self, title: str, body: str, pal: dict[str, str], on_confirm) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._pal = pal
        self._on_confirm = on_confirm

    def compose(self) -> ComposeResult:
        pal = self._pal
        with Vertical(id="ws-confirm-box"):
            yield Static(f"[b {pal['coral']}]{_escape(self._title)}[/]", id="ws-confirm-title")
            yield Static(f"[{pal['muted']}]{_escape(self._body)}[/]")
            yield Static(f"[{pal['dim']}][b {pal['coral']}]y[/] delete   [b {pal['accent']}]n[/] keep[/]")

    def action_confirm(self) -> None:
        self.app.pop_screen()
        self._on_confirm()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class WorksetCreateScreen(ModalScreen):
    """The 'point at a folder' create flow: pick an ungrouped folder (its sessions
    across every agent join the group live), name it, and create. The write goes
    through record_workset_action like the app — never a raw event."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, candidates: list[dict], pal: dict[str, str], on_create) -> None:
        super().__init__()
        self._cands = candidates
        self._pal = pal
        self._on_create = on_create
        self._selected: str | None = None

    def compose(self) -> ComposeResult:
        pal = self._pal
        with Vertical(id="ws-create-box"):
            yield Static(f"[b {pal['ink']}]New work group[/]  [{pal['dim']}]point at a folder[/]", id="ws-create-title")
            yield ListView(id="ws-create-list")
            yield Input(placeholder="Group name", id="ws-create-name")
            yield Static(
                f"[{pal['dim']}][b {pal['accent']}]↑↓[/] pick folder   "
                f"[b {pal['accent']}]↵[/] on the name to create   [b {pal['accent']}]esc[/] cancel[/]"
            )

    def on_mount(self) -> None:
        pal = self._pal
        lv = self.query_one("#ws-create-list", ListView)
        for c in self._cands:
            label = c.get("label") or c.get("project_identity")
            sc = c.get("session_count")
            srcs = ", ".join(_agent_label(s) for s in (c.get("sources") or []))
            lv.append(ListItem(Static(
                f"[{pal['ink']}]{_escape(str(label))}[/]  "
                f"[{pal['dim']}]{sc} session{'s' if sc != 1 else ''} · {_escape(srcs)}[/]"
            )))
        if self._cands:
            lv.index = 0
            self._select(0)
        lv.focus()

    def _select(self, i: int) -> None:
        if 0 <= i < len(self._cands):
            self._selected = self._cands[i].get("project_identity")
            self.query_one("#ws-create-name", Input).value = str(self._cands[i].get("label") or "")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "ws-create-list" and event.list_view.index is not None:
            self._select(event.list_view.index)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Enter on a folder row moves to the name field.
        if event.list_view.id == "ws-create-list":
            self.query_one("#ws-create-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ws-create-name":
            name = event.value.strip()
            if self._selected and name:
                self.app.pop_screen()
                self._on_create(self._selected, name)

    def action_cancel(self) -> None:
        self.app.pop_screen()


# ============================================================================ #
# The application.                                                             #
# ============================================================================ #

# The five destinations, in the macOS app's order. The internal ids keep their
# original names (many call sites: the receipts pane is `work`, ingestion is
# `sources`) but the LABELS mirror the renamed GUI: the receipts collection is
# "Sessions", the new folder-anchored grouping pane is "Work" (`worksets`), and
# ingestion health is "Diagnostics".
_PANES: tuple[tuple[str, str], ...] = (
    ("dashboard", "Dashboard"),
    ("worksets", "Work"),
    ("work", "Sessions"),
    ("usage", "Usage"),
    ("sources", "Diagnostics"),
)



# Focusable pane widgets carry their OWN key bindings, so Textual's Footer shows
# exactly the keys that apply wherever focus is — the fix for "the keys vanish
# when I drill in". Actions resolve up the DOM to the App (bare action names), so
# the App keeps every action method and each pane just declares which keys apply.
# The list panes focus their inner ListView (its up/down/enter work); these
# pane-level keys bubble up from the focused list, and the Footer shows them all.
class _DashboardPane(VerticalScroll):
    can_focus = True
    BINDINGS = [Binding("enter", "app.dash_review", "Review queue")]


class _WorksetsPane(Vertical):
    can_focus = True
    BINDINGS = [
        Binding("g", "app.worksets_new", "New group"),
        Binding("e", "app.worksets_rename", "Rename"),
        Binding("x", "app.worksets_delete", "Delete"),
        # vim aliases for ↑↓ — the help overlay advertises j/k, so honour them.
        Binding("j", "app.list_nav(1)", show=False),
        Binding("k", "app.list_nav(-1)", show=False),
    ]


class _SessionsPane(Vertical):
    can_focus = True
    BINDINGS = [
        Binding("slash", "app.work_filter", "Filter"),
        Binding("left_square_bracket", "app.work_status(-1)", "Prev tab"),
        Binding("right_square_bracket", "app.work_status(1)", "Next tab"),
        Binding("s", "app.work_sort", "Sort"),
        # Scroll the receipt/steps detail (the right pane) from the list — its own
        # VerticalScroll never holds focus, so without this a tall receipt or steps
        # timeline is unreachable from the keyboard. ctrl+d/ctrl+u (not pagedown:
        # the focused ListView already binds pagedown to scroll ITSELF and would
        # shadow us).
        Binding("ctrl+d", "app.work_scroll(1)", "Scroll"),
        Binding("ctrl+u", "app.work_scroll(-1)", "Scroll", show=False),
        # vim aliases for ↑↓ (the help overlay advertises j/k).
        Binding("j", "app.list_nav(1)", show=False),
        Binding("k", "app.list_nav(-1)", show=False),
    ]


class _UsagePane(VerticalScroll):
    can_focus = True
    BINDINGS = [Binding("d", "app.usage_range", "Range")]


class _DiagnosticsPane(VerticalScroll):
    can_focus = True


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

    /* Master: a DataTable of receipts — one row per receipt, sortable columns and
       a proper row cursor. The detail pane (right) is authoritative for the rest. */
    #work-list { width: 46%; height: 1fr; background: $background; }
    #work-list > .datatable--header { background: $background; color: $text-muted; text-style: none; }
    #work-list > .datatable--cursor { background: $block-cursor-background; }
    #work-list > .datatable--hover { background: $background; }

    /* Worksets (the "Work" tab): a full-width stack of cards, each a selectable
       ListItem; the highlighted group wears the accent edge + selected wash. */
    #worksets-list { height: 1fr; background: $background; }
    #worksets-list > ListItem { padding: 0 0 1 0; height: auto; background: $background; }
    #worksets-list > ListItem > Static { padding: 1 2; background: $panel; border-left: wide $panel; }
    #worksets-list > ListItem.-highlight > Static,
    #worksets-list > ListItem.--highlight > Static { background: $block-cursor-background; border-left: wide $primary; }

    HelpScreen { align: center middle; }
    #help-box {
        width: 72; height: auto; padding: 1 2;
        background: $panel; border: round $primary;
    }
    #help-body { height: auto; }

    /* The zoomable work-group timeline overlay. */
    WorksetDetailScreen { align: center middle; }
    #ws-detail-box {
        width: 92%; max-width: 160; height: auto; max-height: 90%; padding: 1 2;
        background: $panel; border: round $primary;
    }
    #ws-detail-body { height: auto; }

    /* Worksets write overlays (create / rename / delete). */
    _WorksetPromptScreen, _WorksetConfirmScreen, WorksetCreateScreen { align: center middle; }
    #ws-prompt-box, #ws-confirm-box {
        width: 64; height: auto; padding: 1 2; background: $panel; border: round $primary;
    }
    #ws-create-box {
        width: 80; height: auto; max-height: 80%; padding: 1 2;
        background: $panel; border: round $primary;
    }
    #ws-create-list { height: auto; max-height: 12; background: $background; margin: 1 0; }
    #ws-create-list > ListItem { padding: 0 1; background: $background; }
    #ws-create-list > ListItem.-highlight, #ws-create-list > ListItem.--highlight { background: $block-cursor-background; }
    #ws-prompt-input, #ws-create-name {
        margin: 1 0; border: round $border; background: $background; padding: 0 1;
    }
    #ws-prompt-input:focus, #ws-create-name:focus { border: round $primary; }
    #ws-prompt-title, #ws-confirm-title, #ws-create-title { height: auto; }
    """

    # Global keys only. Pane-scoped keys live on the focusable pane widgets above
    # (_WorksetsPane, _SessionsPane, _UsagePane, _DashboardPane) so Textual's
    # Footer shows the right keys for wherever focus is — the fix for guidance
    # vanishing on drill-in. `escape` (back out of the steps sub-mode) stays here
    # because it is only meaningful in the Sessions pane and is gated in its action.
    BINDINGS = [
        Binding("1", "show_pane('dashboard')", "Dashboard"),
        Binding("2", "show_pane('worksets')", "Work"),
        Binding("3", "show_pane('work')", "Sessions"),
        Binding("4", "show_pane('usage')", "Usage"),
        Binding("5", "show_pane('sources')", "Diagnostics"),
        Binding("question_mark", "help", "Help"),
        Binding("r", "refresh", "Refresh"),
        Binding("T", "cycle_theme", "Theme", show=False),
        Binding("p", "screenshot", "Snapshot", show=False),
        Binding("q", "quit", "Quit"),
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
        self._work_sort: str = "latest"  # default to time order (newest first)
        self._work_filter: str = ""
        self._work_loading: bool = False
        self._work_built: bool = False
        self._work_visible_ids: list[str] = []
        self._work_visible_rows: list[dict] = []
        self._work_rows_text: str = ""  # plain-string mirror of the DataTable rows
        self._work_task_colkey = None   # DataTable ColumnKey for the resizable Task column
        # True only while _render_work_list is repopulating the DataTable, so the
        # transient RowHighlighted events that add_row/move_cursor post are ignored
        # (the final detail is set synchronously in the rebuild).
        self._rebuilding: bool = False
        self._selected_task_id: str | None = None
        # The highlighted work group, remembered by its stable workset_id so the
        # cursor survives the periodic rebuild instead of snapping back to the top.
        self._selected_workset_id: str | None = None
        self._work_detail_text: str = ""
        # Work detail sub-mode: the receipt (default) vs the sessions & steps
        # drill-down (Enter opens, esc backs out).
        self._work_detail_mode: str = "receipt"
        self._steps_text: str = ""
        self._selected_receipt: dict | None = None
        self._receipt_head: str = ""
        self._steps_head: str = ""

        # Worksets ("Work" tab) state — folder-anchored groupings across agents.
        self._worksets: list[dict] = []
        self._worksets_total: int = 0
        self._workset_candidates: list[dict] = []
        self._worksets_built: bool = False
        self._worksets_loading: bool = False
        self._worksets_err: str | None = None
        self._worksets_notice: str | None = None  # transient result of a write
        self._worksets_text: str = ""  # plain-string mirror for headless tests

        # Usage + Sources pane state / test hooks.
        self._usage_range_index: int = 0
        self._usage_text: str = ""
        self._sources_text: str = ""

        # Test/inspection hooks: the last composed text for key regions, so
        # headless tests assert against strings, never Rich renderable internals.
        self._topbar_text: str = ""
        self._dashboard_text: str = ""

    # -- lifecycle ----------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        yield Static("", id="toprule")
        with ContentSwitcher(initial="dashboard", id="switcher"):
            with _DashboardPane(id="dashboard", classes="pane"):
                yield Static("", id="dash-head")
                with Horizontal(id="dash-hero"):
                    yield Static("", id="dash-attention", classes="card")
                    yield Static("", id="dash-rail", classes="card")
                yield Static("", id="dash-recent", classes="card")
                yield Static("", id="dash-spark", classes="card")
            with _WorksetsPane(id="worksets", classes="pane"):
                yield Static("", id="worksets-head", classes="pane-block")
                yield ListView(id="worksets-list")
            with _SessionsPane(id="work", classes="pane"):
                yield Static("", id="work-head", classes="pane-block")
                yield Static("", id="work-tabs", classes="pane-block")
                yield Input(placeholder="Filter by task, client, or id", id="work-filter")
                with Horizontal(id="work-split"):
                    yield DataTable(id="work-list", cursor_type="row", zebra_stripes=False)
                    with VerticalScroll(id="work-detail"):
                        yield Static("", id="work-detail-head")
                        yield Static("", id="work-outcome", classes="card")
                        yield Static("", id="work-summary", classes="card")
                        yield Static("", id="work-dimensions", classes="card")
                        # The sessions & steps drill-down (Enter opens it, esc backs
                        # out); hidden until the receipt is expanded into it.
                        yield Static("", id="work-steps", classes="card")
            with _UsagePane(id="usage", classes="pane"):
                yield Static("", id="usage-head")
                yield Static("", id="usage-capacity", classes="card")
                yield Static("", id="usage-recorded", classes="card")
            with _DiagnosticsPane(id="sources", classes="pane"):
                yield Static("", id="sources-head")
                yield Static("", id="sources-connected", classes="card")
                yield Static("", id="sources-watcher", classes="card")
                yield Static("", id="sources-verifiers", classes="card")
                yield Static("", id="sources-issues", classes="card")
                yield Static("", id="sources-local", classes="card")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(_THEME_DARK)
        self.register_theme(_THEME_LIGHT)
        self._apply_theme(self._resolve_theme())
        self.title = "agentacct"
        self.query_one("#toprule", Static).update("─" * 400)
        self._render_topbar()
        self.query_one("#work-detail-head", Static).update(
            f"[{self.pal['dim']}]Select a receipt (↑↓) to read it; ↵ opens its sessions & steps.[/]"
        )
        self.query_one("#work-steps", Static).display = False
        self.refresh_data(force=True)
        self._start_import()
        self.set_interval(self.refresh_seconds, self.refresh_data)
        self.set_interval(1.0, self._tick)
        self._focus_pane("dashboard")  # so the Footer shows the Dashboard's keys

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
        pane = self.current_pane
        if pane == "dashboard":
            self._start_dashboard(force=True)
        elif pane == "worksets":
            self._start_worksets(force=True)
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
        if pane == "worksets":
            self._start_worksets()
        elif pane == "work":
            self._start_work()
        elif pane == "usage":
            self._render_usage()
        elif pane == "sources":
            self._render_sources()
        # Focus the pane (its list, if it has one) so Textual's Footer shows that
        # view's keys. Focusing the inner ListView keeps up/down/enter native while
        # the pane-level keys (g/e/x, sort/filter, range) bubble up into the Footer.
        self._focus_pane(pane)
        # A ContentSwitcher change can leave the docked chrome (top bar) un-
        # composited for an offscreen screenshot; force a clean full repaint.
        try:
            self.screen.refresh(repaint=True, layout=True)
        except Exception:  # noqa: BLE001
            pass

    def _focus_pane(self, pane: str) -> None:
        target = {"worksets": "#worksets-list", "work": "#work-list"}.get(pane, f"#{pane}")
        try:
            self.query_one(target).focus()
        except Exception:  # noqa: BLE001
            pass

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_dash_review(self) -> None:
        """The Dashboard's ↵ 'Review evidence' deep-link: jump to Sessions filtered
        to the attention queue, with its top item selected (the app's review-queue
        navigation). Gated to the Dashboard so the global ↵ does nothing elsewhere
        (list panes consume Enter for their own cursor)."""

        if self.current_pane != "dashboard":
            return
        self._work_status = "attention"
        self.action_show_pane("work")

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
        return f"{dot} Local data · {ago}"


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
        self._work_built = False
        self._worksets_built = False
        self._render_all()
        if self.current_pane == "dashboard":
            self._start_dashboard()
        elif self.current_pane == "worksets":
            self._start_worksets(force=True)
        elif self.current_pane == "work":
            self._start_work(force=True)

    def _render_all(self) -> None:
        try:
            self._render_topbar()
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
                res = build_attention_reason(
                    t, latest_store_activity_at=latest, session_starts=starts, task_title=_task_title(t)
                )
                if res is not None:
                    attention_details[str(t.get("public_task_id"))] = res[1]
            # A short by-period token series for the usage sparkline (oldest→newest).
            events: list[dict] | None = None
            try:
                events = SentinelService(self.store_dir, create=False).list_all_events()
                page = build_usage_page(events, days=90)
                dated = [p for p in page.by_period if p.get("period") != "unknown"]
                full = [float(p.get("fresh_tokens") or 0) for p in dated]
                series, history_total = full[-14:], sum(full)
            except Exception:  # noqa: BLE001
                series, history_total = [], 0.0
            try:
                from .ingestion_health import store_ingestion_snapshot

                ingestion = store_ingestion_snapshot(self.store_dir, events=events)
            except Exception:  # noqa: BLE001
                ingestion = {}
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
                f"[b {self.pal['ink']}]Sessions[/]  [{self.pal['dim']}]building… (a few seconds)[/]"
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

    # -- Worksets ("Work"): folder-anchored groupings across agents ----------- #

    def _start_worksets(self, force: bool = False) -> None:
        if self._worksets_built and not force:
            self._render_worksets_head()
            self._render_worksets_list()
            return
        if self._worksets_loading and not force:
            return
        self._worksets_loading = True
        try:
            self.query_one("#worksets-head", Static).update(
                f"[b {self.pal['ink']}]Work[/]  [{self.pal['dim']}]building… (a few seconds)[/]"
            )
        except Exception:  # noqa: BLE001
            pass
        self._build_worksets()

    @work(thread=True, exclusive=True, group="worksets")
    def _build_worksets(self) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()
        try:
            # The SAME shared assembly the macOS app's /v1/worksets route reads,
            # so a grouping can never render differently here than in the app.
            from .api import build_store_worksets

            payload = build_store_worksets(self.store_dir)
            worksets = [w for w in payload.get("worksets", []) if isinstance(w, dict)]
            total = int(payload.get("total") or len(worksets))
            candidates = [c for c in payload.get("candidates", []) if isinstance(c, dict)]
        except Exception as exc:  # noqa: BLE001
            if not worker.is_cancelled:
                self.call_from_thread(self._worksets_error, str(exc))
            return
        if worker.is_cancelled:
            return
        self.call_from_thread(self._populate_worksets, worksets, total, candidates)

    def _worksets_error(self, message: str) -> None:
        self._worksets_loading = False
        self._worksets_err = message
        try:
            self.query_one("#worksets-head", Static).update(
                f"[{self.pal['coral']}]could not build work groups:[/] {_escape(message)}"
            )
        except Exception:  # noqa: BLE001
            pass

    def _populate_worksets(self, worksets: list[dict], total: int, candidates: list[dict] | None = None) -> None:
        self._worksets_loading = False
        self._worksets_built = True
        self._worksets_err = None
        self._worksets = worksets
        self._worksets_total = total
        if candidates is not None:
            self._workset_candidates = candidates
        self._render_worksets_head()
        self._render_worksets_list()

    def _render_worksets_head(self) -> None:
        pal = self.pal
        n = len(self._worksets)
        ungrouped = sum(1 for c in self._workset_candidates if not c.get("existing_workset_id"))
        right = f"[b {pal['accent']}]g[/] [{pal['muted']}]new group[/]" if ungrouped else ""
        top = _two_edge(
            f"{caps('Work', pal)}  [{pal['dim']}]· {n} group{'s' if n != 1 else ''}[/]",
            right,
            max(40, int(getattr(self.size, "width", 0) or 150) - 8),
        )
        sub = self._worksets_notice or "Group a folder's sessions across every agent you run."
        sub_color = pal["accent"] if self._worksets_notice else pal["muted"]
        head = f"{top}\n[{sub_color}]{_escape(sub)}[/]"
        try:
            self.query_one("#worksets-head", Static).update(head)
        except Exception:  # noqa: BLE001
            pass

    def _render_worksets_list(self) -> None:
        pal = self.pal
        try:
            lv = self.query_one("#worksets-list", ListView)
        except Exception:  # noqa: BLE001
            return
        # The card's usable content width: subtract the pane gutter, the item
        # padding + accent edge, and the list scrollbar, so a full-width timeline
        # row never wraps (a wrapped row throws every bar's column off).
        width = max(48, int(getattr(self.size, "width", 0) or 150) - 14)
        lv.clear()
        if not self._worksets:
            lv.append(ListItem(Static(_worksets_empty_markup(pal))))
            self._worksets_text = "No work groups yet"
            return
        # One shared axis across every card, so bars are comparable card-to-card.
        axis_bounds = _worksets_axis_bounds(self._worksets)
        parts: list[str] = []
        for w in self._worksets:
            markup = _workset_card_markup(w, pal, width, axis_bounds=axis_bounds)
            parts.append(markup)
            lv.append(ListItem(Static(markup)))
        # Keep the cursor on the SAME group across a rebuild (the 5s refresh fires
        # whenever the store changes). Fall back to the top only if that group is
        # gone. Without this the highlight snapped back to the first card on every
        # refresh — unusable while live sessions are writing.
        target_idx = 0
        if self._selected_workset_id is not None:
            for i, w in enumerate(self._worksets):
                if str(w.get("workset_id")) == self._selected_workset_id:
                    target_idx = i
                    break
        try:
            lv.index = target_idx  # fires Highlighted → _selected_workset_id re-synced
        except Exception:  # noqa: BLE001
            pass
        self._selected_workset_id = str(self._worksets[target_idx].get("workset_id"))
        # Plain-string mirror for headless tests (markup stripped downstream).
        self._worksets_text = "\n".join(parts)

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
        else:  # attention: the reducer's attention order, then recency
            rows.sort(key=attention_rank)
        return rows

    def _render_work_head(self) -> None:
        pal = self.pal
        n = len(self._work_summaries)
        # "Sessions" is the pane's renamed title (_PANES): the receipts collection.
        # "Work" now belongs to the worksets pane, so this head must not claim it.
        text = (f"{caps('Sessions', pal)} [{pal['dim']}]· {n}[/]   "
                f"[{pal['dim']}]sort {self._work_sort}"
                + (f" · {ATTENTION_SORT_TEXT}" if self._work_sort == "attention" else "") + "[/]")
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

    # Fixed content widths for the non-Task columns; Task takes the remainder.
    _WORK_FIXED_W = {"outcome": 14, "evidence": 8, "cost": 9, "age": 9}

    def _work_task_col_width(self) -> int:
        """Content width for the Task cell/column. The list is ~46% of the terminal;
        reserve room for the fixed columns + DataTable per-cell padding (2 each × 5)
        so a long (CJK) title never forces a horizontal scroll (it ellipsizes)."""
        list_w = int((int(getattr(self.size, "width", 0) or 150)) * 0.46)
        return max(12, list_w - sum(self._WORK_FIXED_W.values()) - 12)

    def _render_work_list(self) -> None:
        pal = self.pal
        try:
            dt = self.query_one("#work-list", DataTable)
        except Exception:  # noqa: BLE001
            return
        tcol = self._work_task_col_width()
        if not dt.columns:  # one-time column setup, fixed widths so nothing grows
            self._work_task_colkey = dt.add_column("Task", key="task", width=tcol)
            dt.add_column("Outcome", key="outcome", width=self._WORK_FIXED_W["outcome"])
            dt.add_column("Evidence", key="evidence", width=self._WORK_FIXED_W["evidence"])
            dt.add_column(_RText("Cost", justify="right"), key="cost", width=self._WORK_FIXED_W["cost"])
            dt.add_column("Age", key="age", width=self._WORK_FIXED_W["age"])
        else:
            # Keep the Task column matched to the current terminal width (resize);
            # the fixed columns don't change. clear()+add_row below relays it out.
            col = dt.columns.get(getattr(self, "_work_task_colkey", None))
            if col is not None and col.width != tcol:
                col.width = tcol
                col.auto_width = False
        rows = self._filtered_work()
        self._work_visible_ids = [str(s.get("task_id")) for s in rows]
        self._work_visible_rows = rows
        # Was the user drilled into the steps view, and on which task? A rebuild
        # (the 5s refresh) rebuilds the receipt and would otherwise drop them back
        # to the receipt view — so we re-open steps for the SAME task afterwards.
        was_steps = self._work_detail_mode == "steps"
        steps_task = self._selected_task_id if was_steps else None
        target = None
        sel_idx = None
        if rows:
            target = self._selected_task_id if self._selected_task_id in self._work_visible_ids else self._work_visible_ids[0]
            sel_idx = self._work_visible_ids.index(target)
        # Repopulate under the rebuild guard: add_row re-homes the DataTable cursor
        # to row 0 and move_cursor then jumps to the target, each posting an async
        # RowHighlighted. Without the guard the intermediate row-0 highlight fires
        # _highlight_to_receipt(row0) and clobbers a sticky steps drill-in for any
        # non-top row. We set the final detail synchronously here and clear the
        # guard once those transient events have drained (call_after_refresh).
        self._rebuilding = True
        dt.clear()  # rows only — columns are kept
        mirror: list[str] = []
        for s in rows:
            cells, plain = _work_row_cells(s, pal, tcol)
            dt.add_row(*cells, key=str(s.get("task_id")))
            mirror.append(plain)
        # Plain-string mirror for headless tests (DataTable cells aren't a Static).
        self._work_rows_text = "\n".join(mirror)
        if rows:
            try:
                dt.move_cursor(row=sel_idx)
            except Exception:  # noqa: BLE001
                pass
            sticky = was_steps and target == steps_task
            # For a sticky in-place refresh keep the reader's scroll position (the
            # checks list is uncapped, so scroll matters); a genuine selection change
            # falls through to the default top-of-detail.
            self._show_receipt(target, preserve_scroll=sticky)
            if sticky:
                self._open_steps(preserve_scroll=True)
        else:
            self._selected_task_id = None
            self._rebuilding = False  # no rows → no transient highlights to guard
            # Drop any stale steps drill-in so it isn't left showing under the
            # "No receipts" head with blank receipt cards.
            self._work_detail_mode = "receipt"
            self._apply_detail_mode()
            self.query_one("#work-detail-head", Static).update(
                f"[{pal['dim']}]No receipts match this filter.[/]"
            )
            for wid in ("#work-outcome", "#work-summary", "#work-dimensions"):
                self.query_one(wid, Static).update("")

    def _highlight_to_receipt(self, task_id: str | None) -> None:
        # Cursor-follows: the detail tracks the highlighted row (↑↓ / j/k), like
        # lazygit. Re-highlighting the SAME row must not reset the detail — only a
        # move to a DIFFERENT receipt swaps it (and drops out of any steps drill-in).
        if not task_id or task_id == self._selected_task_id:
            return
        self._show_receipt(task_id)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "work-list" or event.row_key is None:
            return
        key = str(event.row_key.value)
        if self._rebuilding:
            # The rebuild sets the detail synchronously and posts transient
            # highlights (an intermediate row-0 from add_row, then the target from
            # move_cursor). Swallow them; clear the guard when the TARGET highlight
            # arrives (always posted last, FIFO — or the sole one when coalesced), so
            # a genuine later cursor move is honoured but the transient row-0 can't
            # clobber a sticky steps drill-in. Deterministic, not timing-based.
            if key == self._selected_task_id:
                self._rebuilding = False
            return
        self._highlight_to_receipt(key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter drills the highlighted receipt into its sessions & steps.
        if event.data_table.id == "work-list" and event.row_key is not None:
            self._highlight_to_receipt(str(event.row_key.value))
            self._open_steps()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "worksets-list":
            # Remember which group is highlighted so the cursor survives a rebuild.
            idx = event.list_view.index
            if idx is not None and 0 <= idx < len(self._worksets):
                self._selected_workset_id = str(self._worksets[idx].get("workset_id"))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "worksets-list":
            self._open_workset_detail(event.list_view.index)

    def _open_workset_detail(self, index: int | None) -> None:
        if index is None or not (0 <= index < len(self._worksets)):
            return  # the empty-state card is inert
        card = self._worksets[index]
        if not isinstance(card, dict) or not card.get("sessions"):
            return
        self.push_screen(WorksetDetailScreen(card, self.pal))

    # -- Worksets writes (create / rename / delete) -------------------------- #

    def _focused_workset(self) -> dict | None:
        try:
            i = self.query_one("#worksets-list", ListView).index
        except Exception:  # noqa: BLE001
            return None
        if i is None or not (0 <= i < len(self._worksets)):
            return None
        return self._worksets[i]

    def _record_workset(self, *, action: str, workset_id: str, name: str | None = None,
                        project_identity: str | None = None, expected_revision: int,
                        success_notice: str) -> None:
        """The ONLY sanctioned workset write: service.record_workset_action, which
        server-stamps a trusted grouping event (a raw record_event is stripped).
        A workset never re-grades a session's receipt or evidence — it is a human
        overlay. Optimistic revision: a concurrent change surfaces, never a silent
        overwrite."""

        try:
            svc = SentinelService(self.store_dir, create=False)
            if action in ("create", "redirect") and project_identity:
                # One group per folder (best-effort, mirrors the app's route); the
                # store stays the integrity authority.
                from .api import _grouped_workset_identities

                existing = _grouped_workset_identities(svc.list_all_events()).get(project_identity)
                if existing is not None and existing != workset_id:
                    self._worksets_notice = "A work group for this folder already exists."
                    self._start_worksets(force=True)
                    return
            svc.record_workset_action(
                action=action,
                workset_id=workset_id,
                name=name,
                project_identity=project_identity,
                expected_revision=expected_revision,
                idempotency_key=f"tui:workset:{workset_id}:{action}:{expected_revision}",
            )
            self._worksets_notice = success_notice
        except Exception as exc:  # noqa: BLE001
            self._worksets_notice = f"couldn't {action}: {exc}"
        # Re-read the store so the new grouping (and its live membership) appears.
        self.refresh_data(force=True)
        self._start_worksets(force=True)

    def action_worksets_new(self) -> None:
        if self.current_pane != "worksets":
            return
        self._worksets_notice = None
        ungrouped = [c for c in self._workset_candidates if not c.get("existing_workset_id")]
        if not ungrouped:
            self._worksets_notice = "No ungrouped folders yet — run an agent in a project first."
            self._render_worksets_head()
            return

        def on_create(identity: str, name: str) -> None:
            self._record_workset(action="create", workset_id=f"ws_{uuid.uuid4().hex}", name=name,
                                 project_identity=identity, expected_revision=0,
                                 success_notice=f"Created “{name}”.")

        self.push_screen(WorksetCreateScreen(ungrouped, self.pal, on_create))

    def action_worksets_rename(self) -> None:
        if self.current_pane != "worksets":
            return
        w = self._focused_workset()
        if not w:
            return
        self._worksets_notice = None

        def on_submit(name: str) -> None:
            if name and name != w.get("name"):
                self._record_workset(action="rename", workset_id=str(w.get("workset_id")), name=name,
                                     expected_revision=int(w.get("revision") or 0),
                                     success_notice=f"Renamed to “{name}”.")

        self.push_screen(_WorksetPromptScreen(f"Rename “{w.get('name')}”", str(w.get("name") or ""), self.pal, on_submit))

    def action_worksets_delete(self) -> None:
        if self.current_pane != "worksets":
            return
        w = self._focused_workset()
        if not w:
            return
        self._worksets_notice = None

        def on_confirm() -> None:
            self._record_workset(action="delete", workset_id=str(w.get("workset_id")),
                                 expected_revision=int(w.get("revision") or 0),
                                 success_notice=f"Deleted “{w.get('name')}”.")

        self.push_screen(_WorksetConfirmScreen(
            f"Delete work group “{w.get('name')}”?",
            "This only ungroups the folder — the sessions and their receipts are untouched.",
            self.pal, on_confirm,
        ))

    def _show_receipt(self, task_id: str, preserve_scroll: bool = False) -> None:
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
        self._apply_detail_mode(preserve_scroll=preserve_scroll)

    def _apply_detail_mode(self, preserve_scroll: bool = False) -> None:
        """Show the receipt cards or the sessions & steps card per the sub-mode,
        and swap the breadcrumb head to match. ``preserve_scroll`` keeps the
        detail's scroll position (an in-place refresh of the same view) instead of
        snapping to the top — so a reader deep in a long checks list isn't yanked
        up every time the store changes."""

        steps = self._work_detail_mode == "steps"
        try:
            for wid in ("#work-outcome", "#work-summary", "#work-dimensions"):
                self.query_one(wid, Static).display = not steps
            self.query_one("#work-steps", Static).display = steps
            self.query_one("#work-detail-head", Static).update(
                self._steps_head if steps else self._receipt_head
            )
            if not preserve_scroll:
                self.query_one("#work-detail", VerticalScroll).scroll_home(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def _open_steps(self, preserve_scroll: bool = False) -> None:
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
            parts = _build_steps_parts(receipt, checks, pal, int(getattr(self.size, "width", 0) or 150), task=task)
        except Exception as exc:  # noqa: BLE001
            parts = {"head": f"[{pal['dim']}]‹ Receipt[/]",
                     "title": "SESSIONS & STEPS", "body": f"[{pal['coral']}]could not render steps:[/] {_escape(str(exc))}"}
        self._steps_head = parts["head"]
        self._steps_text = parts["title"] + "\n" + parts["body"]
        self._set_card("#work-steps", parts["title"], parts["body"])
        self._work_detail_mode = "steps"
        self._apply_detail_mode(preserve_scroll=preserve_scroll)

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

    def action_list_nav(self, delta: int) -> None:
        """j/k cursor move for the focused master list (vim aliases for ↑↓). The
        Sessions list is a DataTable, the Work list a ListView — both expose
        action_cursor_up/down."""
        if self.current_pane == "work":
            widget: Any = None
            try:
                widget = self.query_one("#work-list", DataTable)
            except Exception:  # noqa: BLE001
                return
        elif self.current_pane == "worksets":
            try:
                widget = self.query_one("#worksets-list", ListView)
            except Exception:  # noqa: BLE001
                return
        else:
            return
        try:
            widget.action_cursor_down() if delta > 0 else widget.action_cursor_up()
        except Exception:  # noqa: BLE001
            pass

    def action_work_scroll(self, direction: int) -> None:
        """Page the receipt/steps detail pane (its VerticalScroll never has focus)."""
        if self.current_pane != "work":
            return
        try:
            ds = self.query_one("#work-detail", VerticalScroll)
            ds.scroll_page_down(animate=False) if direction > 0 else ds.scroll_page_up(animate=False)
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
                # #work-list is a DataTable now — a stale ListView type here raised
                # WrongType (swallowed), leaving focus stuck in the filter Input so
                # j/k/arrows typed into the box instead of navigating the list.
                self.query_one("#work-list", DataTable).focus()
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
            from .ingestion_health import store_ingestion_snapshot

            snapshot = store_ingestion_snapshot(self.store_dir)
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
                self._set_card("#sources-issues", parts["issues_title"], parts["issues"],
                               parts.get("issues_color") or pal["amber"])
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
# Worksets ("Work" tab): folder-anchored groupings + the cross-agent timeline. #
# Pure data+palette → Rich markup, so both the live render and the screenshot   #
# fixtures share one drawing path. Every figure is a session's own, verbatim    #
# from workset_session_lane / summarize_members — never re-graded here.         #
# ============================================================================ #

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# How many session lanes a card draws before it discloses the rest as a note —
# mirrors the GUI card's 8-row window (the full group opens in the detail view).
_CARD_TIMELINE_ROWS = 8
# The detail (zoomable) timeline shows more rows since it fills the screen.
_DETAIL_TIMELINE_ROWS = 20
_MAX_ZOOM = 64.0


class _ZoomWindow:
    """A sub-range of [lo, hi], ``zoom``× narrower, centred at ``pan`` (0…1) and
    clamped so it never leaves the data. A faithful Python port of the Swift
    ``WorksetZoomWindow`` so the terminal's zoom math matches the app's exactly
    (positions still snap to character cells — that is the honest ceiling)."""

    def __init__(self, lo: float, hi: float, zoom: float, pan: float) -> None:
        full = max(0.0, hi - lo)
        z = min(_MAX_ZOOM, max(1.0, zoom if _finite(zoom) else 1.0))
        width = full / z if z > 0 else full
        center_frac = min(1.0, max(0.0, pan if _finite(pan) else 0.5))
        center = lo + center_frac * full
        s = center - width / 2
        e = center + width / 2
        if s < lo:
            e = min(hi, e + (lo - s))
            s = lo
        if e > hi:
            s = max(lo, s - (e - hi))
            e = hi
        self.start = s
        self.end = e

    @staticmethod
    def lane_visible(first: Any, last: Any, start: float, end: float) -> bool:
        if not (isinstance(first, (int, float)) and not isinstance(first, bool) and first and first > 0):
            return True  # a timeless lane has no position — always kept, shown faded
        lo = float(first)
        hi = float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) and last else lo
        return not (hi < start or lo > end)

    def coverage(self, lo: float, hi: float) -> float:
        full = max(0.0, hi - lo)
        if full <= 0:
            return 1.0
        return min(1.0, max(0.0, (self.end - self.start) / full))

    @staticmethod
    def apply_zoom(zoom: float, pan: float, factor: float, anchor: float, lo: float, hi: float) -> tuple[float, float]:
        full = max(0.0, hi - lo)
        z0 = min(_MAX_ZOOM, max(1.0, zoom if _finite(zoom) else 1.0))
        center0 = min(1.0, max(0.0, pan if _finite(pan) else 0.5))
        if not (full > 0 and _finite(factor) and factor > 0):
            return z0, center0
        win = _ZoomWindow(lo, hi, z0, center0)
        a = min(1.0, max(0.0, anchor if _finite(anchor) else 0.5))
        anchor_time = win.start + a * (win.end - win.start)
        z1 = min(_MAX_ZOOM, max(1.0, z0 * factor))
        new_width = full / z1
        new_start = anchor_time - a * new_width
        new_center = new_start + new_width / 2
        return z1, min(1.0, max(0.0, (new_center - lo) / full if full else 0.5))


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf"))


def _agent_color(client: Any, pal: dict[str, str]) -> str:
    """Per-agent hue, mirroring Swift Theme.sourceColor: Claude=accent, Codex=
    purple, OpenCode=teal, Hermes=magenta, everything else muted."""

    c = str(client or "").strip().lower()
    if c in ("claude-code", "claude", "claude code"):
        return pal["accent"]
    if c in ("codex", "openai-codex", "codex-cli"):
        return pal["codex"]
    if c in ("opencode", "open-code"):
        return pal["opencode"]
    if c == "hermes":
        return pal["hermes"]
    return pal["muted"]


def _agent_label(client: Any) -> str:
    """A friendly agent name for the legend (mirrors WorksetFormat.sourceLabel)."""

    c = str(client or "").strip().lower()
    return {
        "claude-code": "Claude Code",
        "claude": "Claude Code",
        "claude code": "Claude Code",
        "codex": "Codex",
        "openai-codex": "Codex",
        "codex-cli": "Codex",
        "opencode": "OpenCode",
        "open-code": "OpenCode",
        "hermes": "Hermes",
        "dsh": "DeepSeek",
        "deepseek": "DeepSeek",
    }.get(c, (str(client).strip() or "unknown"))


def _lane_pip(status: Any, pal: dict[str, str]) -> str:
    """A session's own status as a coloured pip (SHAPE + colour, never colour
    alone): blocked/failed → coral ●, live → accent ●, a clean terminal → ink ●,
    and a bare observed/unknown → muted ○. Mirrors WorksetTimeline.pipColor."""

    s = str(status or "").strip().lower()
    if s in ("blocked", "failed"):
        return f"[{pal['coral']}]●[/]"
    if s in ("active", "in_progress", "started", "checkpoint", "handed_off"):
        return f"[{pal['accent']}]●[/]"
    if s in ("completed", "resolved"):
        return f"[{pal['ink']}]●[/]"
    return f"[{pal['dim']}]○[/]"


def _axis_date(ts: Any) -> str:
    """A local-time 'Mon D' axis label. The app's activity canvas renders dates with
    Calendar.current (WorkTimelineOverview), so the terminal uses local time too — a
    UTC label would read hours off from the work the user actually did. (The app's
    worksets-card axis happens to format in UTC; we prefer local everywhere here as
    the more honest, internally-consistent choice — the shift is date-only there.)"""

    if not (isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0):
        return ""
    t = time.localtime(float(ts))
    return f"{_MONTHS[t.tm_mon - 1]} {t.tm_mday}"


def _span_text(first: Any, last: Any) -> str:
    """A rough human span for the KPI row (mirrors WorksetFormat.span: '~4 days',
    '~3 hr', '~12 min'). A labelled approximation, never a precise duration."""

    if not (
        isinstance(first, (int, float)) and not isinstance(first, bool)
        and isinstance(last, (int, float)) and not isinstance(last, bool)
        and last >= first
    ):
        return "—"
    secs = float(last) - float(first)
    if secs >= 86400:
        n = round(secs / 86400)
        return f"~{n} day" + ("s" if n != 1 else "")
    if secs >= 3600:
        n = round(secs / 3600)
        return f"~{n} hr" + ("s" if n != 1 else "")
    n = max(1, round(secs / 60))
    return f"~{n} min"


def _workset_cost_text(summary: dict) -> str:
    """The cost KPI value under the shared cost grammar: a knowingly partial sum
    (some members unpriced) reads ``~$``, a complete estimate ``≈$``/``$`` per
    confidence, and nothing priced names its absence rather than a fake $0."""

    cost = summary.get("estimated_cost_usd")
    if cost is None:
        return "unpriced"
    priced = int(summary.get("priced_sessions") or 0)
    unpriced = int(summary.get("unpriced_sessions") or 0)
    conf = summary.get("cost_confidence")
    if unpriced > 0 and priced > 0:
        shown = cost_string_or_absent(None, complete=False, confidence=conf, known_additive=float(cost))
    else:
        shown = cost_string_or_absent(float(cost), complete=bool(summary.get("cost_complete")), confidence=conf)
    return shown if shown is not None else "unpriced"


def _trunc(text: str, n: int) -> str:
    """Truncate to ``n`` terminal CELLS (not characters) with a trailing ellipsis,
    so a wide/CJK title is cut at the right visual width and never over-runs its
    column. ``set_cell_size`` truncates on a cell boundary (it won't split a wide
    glyph) and reserves one cell for the ellipsis."""

    text = str(text)
    if _cell_len(text) <= n:
        return text
    return _set_cells(text, max(1, n - 1)) + "…"


def _pad_vis(markup: str, n: int) -> str:
    """Right-pad a markup string to ``n`` terminal CELLS (tags/escapes ignored),
    so a colour-tagged left label still aligns the timeline track — even when the
    title contains wide/CJK glyphs that occupy two columns each."""

    vis = _plainlen(markup)
    return markup + " " * max(0, n - vis) if vis < n else markup


def _lane_title(lane: dict) -> str:
    """A member session's display title, with the GUI's 'client · id[:8]' fallback
    so a nameless session still reads as itself, never blank."""

    title = lane.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    client = str(lane.get("client") or "session")
    tail = str(lane.get("client_session_id") or lane.get("session_key") or "")[:8]
    return f"{client} · {tail}" if tail else client


def _axis_stamp(ts: Any, span: float) -> str:
    """An axis label: 'Mon D', plus HH:MM once the window is narrow enough (a
    zoomed-in day) that the bare date would repeat on both ends."""

    if not (isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0):
        return ""
    t = time.localtime(float(ts))   # local time — see _axis_date
    base = f"{_MONTHS[t.tm_mon - 1]} {t.tm_mday}"
    if span and 0 < span < 36 * 3600:
        return f"{base} {t.tm_hour:02d}:{t.tm_min:02d}"
    return base


def _timeline_bounds(lanes: list[dict]) -> tuple[float | None, float | None]:
    """The full data window: earliest start → latest of any start/end."""

    firsts = [float(l["first_activity_at"]) for l in lanes
              if isinstance(l.get("first_activity_at"), (int, float)) and not isinstance(l.get("first_activity_at"), bool) and l["first_activity_at"] > 0]
    lasts = [float(l["last_activity_at"]) for l in lanes
             if isinstance(l.get("last_activity_at"), (int, float)) and not isinstance(l.get("last_activity_at"), bool) and l["last_activity_at"] > 0]
    w0 = min(firsts) if firsts else None
    w1 = max(firsts + lasts) if (firsts or lasts) else None
    return w0, w1


def _worksets_axis_bounds(worksets: list[dict]) -> tuple[float | None, float | None]:
    """The one axis every Work card shares: earliest start → latest activity across
    ALL groups. Threaded into each card so a bar at a given column means the same
    instant on every card, and you can read which group's work came first."""

    los: list[float] = []
    his: list[float] = []
    for w in worksets:
        lanes = [l for l in (w.get("sessions") or []) if isinstance(l, dict)]
        lo, hi = _timeline_bounds(lanes)
        if lo is not None:
            los.append(lo)
        if hi is not None:
            his.append(hi)
    return (min(los) if los else None, max(his) if his else None)


def _workset_timeline(
    lanes: list[dict],
    pal: dict[str, str],
    width: int,
    *,
    win_start: float | None = None,
    win_end: float | None = None,
    axis_bounds: tuple[float | None, float | None] | None = None,
    max_rows: int = _CARD_TIMELINE_ROWS,
    focus_key: str | None = None,
) -> dict[str, Any]:
    """The shared cross-agent axis: one row per member session — a status pip +
    title in a fixed left column, then a track where a block glyph sits at the
    session's start time, coloured by its agent. With ``win_start/win_end`` it
    draws a ZOOMED window (lanes outside it are culled, timeless lanes kept).
    ``axis_bounds`` instead pins the axis to an OUTER window (e.g. one shared
    across every Work card) without culling — every lane is drawn, positioned
    against that common range so bars line up card-to-card. ``focus_key`` bolds
    the row whose session_key matches (the scrubber cursor). Positions snap to
    character cells — a duration under one cell quantises to one column (the
    honest terminal ceiling). Returns markup rows + counts + geometry."""

    left_col = max(20, min(34, width // 3))
    track = max(12, width - left_col - 1)
    data0, data1 = _timeline_bounds(lanes)
    zoomed = win_start is not None and win_end is not None
    shared = (not zoomed and axis_bounds is not None
              and axis_bounds[0] is not None and axis_bounds[1] is not None
              and axis_bounds[1] > axis_bounds[0])
    if zoomed:
        a0, a1 = win_start, win_end
    elif shared:
        a0, a1 = axis_bounds
    else:
        a0, a1 = data0, data1

    if zoomed:
        visible = [l for l in lanes if _ZoomWindow.lane_visible(l.get("first_activity_at"), l.get("last_activity_at"), a0, a1)]
    else:
        visible = list(lanes)
    hidden_by_window = len(lanes) - len(visible)
    shown = visible[:max_rows]
    hidden = max(0, len(visible) - len(shown))
    span = (a1 - a0) if (a0 is not None and a1 is not None and a1 > a0) else 0.0

    rows: list[str] = []
    timeless = 0
    for i, lane in enumerate(shown):
        focused = focus_key is not None and str(lane.get("session_key")) == focus_key
        title = _escape(_trunc(_lane_title(lane), left_col - 2))
        title_m = f"[b {pal['accent']}]{title}[/]" if focused else f"[{pal['ink']}]{title}[/]"
        left = _pad_vis(f"{_lane_pip(lane.get('status'), pal)} {title_m}", left_col)
        agent = _agent_color(lane.get("client"), pal)
        t = lane.get("first_activity_at")
        if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 and a0 is not None:
            # A session is a BAR spanning first→last activity (not a point): a long
            # run reads as a long bar, a quick one as a single cell. Mirrors the
            # GUI's leftFraction/widthFraction. Clamped to the (possibly zoomed)
            # window so a session overflowing the edge stays on-axis.
            last = lane.get("last_activity_at")
            end_t = float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) and last and last >= t else float(t)
            start_col = min(track - 1, max(0, round(((float(t) - a0) / span) * (track - 1)))) if span > 0 else 0
            end_col = min(track - 1, max(0, round(((end_t - a0) / span) * (track - 1)))) if span > 0 else start_col
            end_col = max(end_col, start_col)  # never narrower than one cell
            bar_w = end_col - start_col + 1
            line = pal["accent"] if focused else pal["hair"]
            trackstr = (f"[{line}]{'─' * start_col}[/][{agent}]{'█' * bar_w}[/]"
                        f"[{line}]{'─' * (track - end_col - 1)}[/]")
        else:
            timeless += 1
            trackstr = f"[{pal['dim']}]█[/][{pal['hair']}]{'─' * (track - 1)}[/]"
        rows.append(f"{left} {trackstr}")

    if a0 is not None and a1 is not None:
        axis = " " * (left_col + 1) + _two_edge(
            f"[{pal['dim']}]{_axis_stamp(a0, span)}[/]", f"[{pal['dim']}]{_axis_stamp(a1, span)}[/]", track
        )
    else:
        axis = ""
    return {
        "rows": rows,
        "axis": axis,
        "timeless": timeless,
        "hidden": hidden,
        "hidden_by_window": hidden_by_window,
        "shown": len(shown),
        "left_col": left_col,
        "track": track,
    }


def _workset_card_markup(w: dict, pal: dict[str, str], width: int = 140,
                         *, axis_bounds: tuple[float | None, float | None] | None = None) -> str:
    """One folder grouping as a card: name + 'grouped by folder' chip, the KPI
    SUM row, the agent legend, the shared cross-agent timeline, and the honesty
    note (the total is a labelled sum of independent receipts, not a verdict).
    ``axis_bounds`` pins this card's timeline to a window shared across every
    card, so a bar's horizontal position is comparable from one card to the next."""

    summary = w.get("summary") if isinstance(w.get("summary"), dict) else {}
    lanes = [l for l in (w.get("sessions") or []) if isinstance(l, dict)]
    name = str(w.get("name") or w.get("project_identity") or "work group")

    header = (
        f"[b {pal['ink']}]{_escape(name)}[/]  "
        f"[{pal['accent']} on {pal['ta']}] grouped by folder [/]"
    )

    sc = int(summary.get("session_count") or len(lanes))
    sources = summary.get("sources") if isinstance(summary.get("sources"), list) else []
    cost_label = "cost, sum of receipts" if summary.get("cost_complete") else "cost, partial sum"
    col_w = max(18, min(30, width // 4))
    kpi = _kpi_cells(
        [
            ("sessions", str(sc), ""),
            ("sources", str(len(sources)), ""),
            ("span", _span_text(summary.get("first_activity_at"), summary.get("last_activity_at")), ""),
            (cost_label, _workset_cost_text(summary), ""),
        ],
        pal,
        col_w,
    )

    legend = "   ".join(
        f"[{_agent_color(s.get('client'), pal)}]■[/] [{pal['muted']}]{_escape(_agent_label(s.get('client')))}[/]"
        for s in sources
        if isinstance(s, dict)
    )

    tl = _workset_timeline(lanes, pal, width, axis_bounds=axis_bounds)

    notes = [
        f"[{pal['dim']}]ⓘ Grouped because you pointed this at a folder. Each session keeps its own "
        f"receipt and evidence; the total is a sum of {sc} session{'s' if sc != 1 else ''}, "
        f"not a combined verdict.[/]"
    ]
    if int(summary.get("unpriced_sessions") or 0) > 0:
        notes.append(f"[{pal['dim']}]Some sessions here carry no imported cost, so the total is a partial sum.[/]")
    if tl["hidden"] or w.get("sessions_truncated"):
        total = int(w.get("sessions_total") or len(lanes))
        notes.append(f"[{pal['dim']}]Showing {tl['shown']} of {total} sessions — press ↵ to open the group's full timeline.[/]")
    if tl["timeless"]:
        n = tl["timeless"]
        notes.append(f"[{pal['dim']}]{n} session{'s' if n != 1 else ''} with no recorded time — shown faded at the start, not a real position.[/]")

    blocks = [header, "", kpi]
    if legend:
        blocks += ["", legend]
    if tl["rows"]:
        blocks += [""] + tl["rows"] + ([tl["axis"]] if tl["axis"] else [])
    blocks += [""] + notes
    return "\n".join(blocks)


def _worksets_empty_markup(pal: dict[str, str]) -> str:
    """The empty state: what a work group is and how to make one."""

    return (
        f"[b {pal['ink']}]No work groups yet[/]\n"
        f"[{pal['muted']}]A work group gathers one folder's sessions across every agent you run "
        f"— Claude Code, Codex, OpenCode — onto a single cross-agent timeline. Each session keeps "
        f"its own receipt; the group only sums them.[/]\n"
        f"[{pal['dim']}]Press [b {pal['accent']}]g[/][{pal['dim']}] to point at a folder and create one.[/]"
    )


def _workset_scrubber(lanes: list[dict], pal: dict[str, str], left_col: int, track: int,
                      lo: float | None, hi: float | None, win_start: float, win_end: float) -> str:
    """The overview rail: the full range with a tick per session start and an
    accent bar marking the currently-visible zoom window (the GUI's scrubber,
    which drags to pan — here it's a read-only orientation strip driven by keys)."""

    if lo is None or hi is None or hi <= lo:
        return ""
    full = hi - lo
    cells = [f"[{pal['hair']}]┈[/]"] * track

    def col_of(t: float) -> int:
        return min(track - 1, max(0, round((t - lo) / full * (track - 1))))

    s_col, e_col = col_of(win_start), col_of(win_end)
    for c in range(min(s_col, e_col), max(s_col, e_col) + 1):
        cells[c] = f"[{pal['accent']}]━[/]"
    for lane in lanes:
        t = lane.get("first_activity_at")
        if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0:
            cells[col_of(float(t))] = f"[{pal['muted']}]╽[/]"
    return " " * (left_col + 1) + "".join(cells)


def _lane_facts(lane: dict, pal: dict[str, str], width: int) -> str:
    """The focused session's own facts (the terminal stand-in for the GUI's hover
    card): every figure verbatim from the lane, never re-graded or combined."""

    title = _escape(_trunc(_lane_title(lane), max(24, width - 30)))
    head = (f"{_lane_pip(lane.get('status'), pal)} [b {pal['ink']}]{title}[/]  "
            f"[{pal['dim']}]{_escape(_agent_label(lane.get('client')))} · {_escape(str(lane.get('status') or 'observed'))}[/]")
    dur = lane.get("duration_seconds")
    cost = cost_string_or_absent(lane.get("estimated_cost_usd"), complete=True, confidence=lane.get("cost_confidence"))
    toks = lane.get("total_tokens")
    checks = lane.get("checks")
    facts = [
        ("duration", humanize_seconds(float(dur)) if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0 else "—"),
        ("cost", cost or "unpriced"),
        ("tokens", abbr_tokens(toks) if isinstance(toks, (int, float)) and not isinstance(toks, bool) and toks else "—"),
        # The NOUN is the vocabulary's ("Tool calls"); this row owns only the
        # casing, so the absence budget can never find a second spelling here.
        (receipt_field_label("actions").lower(),
         str(lane.get("tool_calls")) if lane.get("tool_calls") else "—"),
        ("steps", str(lane.get("steps")) if lane.get("steps") else "—"),
        ("checks", f"{checks} · {lane.get('checks_failed') or 0} failed" if checks else "—"),
    ]
    factline = "   ".join(f"[{pal['dim']}]{k}[/] [{pal['ink']}]{v}[/]" for k, v in facts)
    return f"{head}\n{factline}"


def _workset_detail_markup(card: dict, pal: dict[str, str], width: int = 140, *,
                           zoom: float = 1.0, pan: float = 0.5, focus_key: str | None = None) -> str:
    """The zoomable/scrubbable detail body for one work group: header, a zoom
    coverage line, the windowed cross-agent timeline, the overview scrubber, and
    the focused session's facts. Same honesty as the card — a labelled sum, never
    a combined verdict; every lane figure is that session's own."""

    lanes = [l for l in (card.get("sessions") or []) if isinstance(l, dict)]
    summary = card.get("summary") if isinstance(card.get("summary"), dict) else {}
    name = str(card.get("name") or card.get("project_identity") or "work group")
    sources = summary.get("sources") if isinstance(summary.get("sources"), list) else []
    sc = int(summary.get("session_count") or len(lanes))

    lo, hi = _timeline_bounds(lanes)
    windowed = lo is not None and hi is not None and hi > lo
    if windowed:
        win = _ZoomWindow(lo, hi, zoom, pan)
        ws, we = win.start, win.end
        coverage = win.coverage(lo, hi)
        tl = _workset_timeline(lanes, pal, width, win_start=ws, win_end=we,
                               max_rows=_DETAIL_TIMELINE_ROWS, focus_key=focus_key)
    else:
        ws = we = lo if lo is not None else 0.0
        coverage = 1.0
        tl = _workset_timeline(lanes, pal, width, max_rows=_DETAIL_TIMELINE_ROWS, focus_key=focus_key)

    cov_text = "full range" if coverage >= 0.999 else f"~{max(1, round(coverage * 100))}% of range"
    header = (f"[b {pal['ink']}]{_escape(name)}[/]  "
              f"[{pal['dim']}]{sc} session{'s' if sc != 1 else ''} · "
              f"{len(sources)} source{'s' if len(sources) != 1 else ''} · "
              f"{_span_text(summary.get('first_activity_at'), summary.get('last_activity_at'))}[/]")
    legend = "   ".join(
        f"[{_agent_color(s.get('client'), pal)}]■[/] [{pal['muted']}]{_escape(_agent_label(s.get('client')))}[/]"
        for s in sources if isinstance(s, dict)
    )
    zoom_line = f"[{pal['dim']}]zoom[/] [{pal['accent']}]{cov_text}[/]"

    blocks: list[str] = [header]
    if legend:
        blocks.append(legend)
    blocks += ["", zoom_line, ""]
    blocks += tl["rows"]
    if tl["axis"]:
        blocks.append(tl["axis"])
    scrub = _workset_scrubber(lanes, pal, tl["left_col"], tl["track"], lo, hi, ws, we)
    if scrub:
        blocks += ["", scrub]

    notes: list[str] = []
    if tl["hidden_by_window"]:
        notes.append(f"[{pal['dim']}]{count_noun(int(tl['hidden_by_window']), 'session')} outside this range — zoom out (−) to see them.[/]")
    if tl["hidden"]:
        notes.append(f"[{pal['dim']}]Showing {tl['shown']} rows in view — zoom into a range to see the rest.[/]")
    if tl["timeless"]:
        n = tl["timeless"]
        notes.append(f"[{pal['dim']}]{n} session{'s' if n != 1 else ''} with no recorded time — shown faded at the start.[/]")
    if notes:
        blocks += [""] + notes

    focused = next((l for l in lanes if str(l.get("session_key")) == focus_key), None)
    if focused is not None:
        blocks += ["", _lane_facts(focused, pal, width)]

    # No inline key hint: the screen docks a native Footer driven by the real
    # bindings (Move / Zoom / Reset / Pan / Back), so guidance stays visible and
    # never scrolls away with the body.
    return "\n".join(blocks)


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
    # The queue and its order both come from the payload (``attention_rows``):
    # the reducer decides what is open and in what order, so the Dashboard's
    # shift brief, the Work tabs and /v1/attention can never disagree about
    # which Task leads. A local "most actionable" tweak here would be a second
    # sort rule beside the one ATTENTION_SORT_TEXT states.
    attention = attention_rows(summaries)

    # head — the shift-brief eyebrow + the headline item (count pinned right).
    if attention:
        headline = str(attention[0].get("title") or attention[0].get("task_id") or "—")
        n_rev = len(attention)
        head = f"{caps('Shift brief', pal)}\n" + _two_edge(
            f"[b {pal['ink']}]{_escape(headline)}[/]",
            f"[{pal['dim']}]{_escape(attention_count_text(n_rev))}[/]", full_w)
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
        reason = ATTENTION_REASON_LABELS.get(str(detail.get("kind")), decision_label(dkey))
        observed = _humanize_ago(detail.get("observed_at") or top.get("last_activity_at"), now)
        prov = source_label(detail.get("source"))
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
        # Only ↵ (dash_review) is wired; the old " y Copy review brief " chip had no
        # binding or clipboard path, so it is dropped rather than left a dead cue.
        rows.append(f"[{pal['accent']} on {pal['ta']}] ↵ Review evidence [/]  "
                    f"[{pal['accent']}]{_escape(ATTENTION_OPEN_ACTION)} →[/]")
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
        "Working now", count_noun(int(active), "active session"),
        working_sub or ("in progress" if active else "none in progress right now"), pal)]
    rail_blocks.append(_capacity_block(limits, pal))
    if snap is not None:
        rail_blocks.append(_rail_block(
            "Usage change", f"Today · {abbr_tokens(_window_total(snap, 'today'))} fresh tokens",
            # A basis only beside a priced figure; an absence names itself.
            cost_basis_caption(_window_totals(snap, "today"), COST_BASIS_NOT_REPORTED)
            or cost_text(_window_totals(snap, "today")), pal))
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
            avail = max(8, tcol - 4 - (_cell_len(suffix) if suffix else 0))
            tt = _trunc(title, avail)
            if suffix:
                tmarkup = f"[{pal['ink']}]{_escape(tt)}[/]  [{pal['dim']}]{_escape(suffix)}[/]"
                tvis = _cell_len(tt) + 2 + _cell_len(suffix)
            else:
                tmarkup = f"[{pal['ink']}]{_escape(tt)}[/]"
                tvis = _cell_len(tt)
            blabel = decision_label(dkey)
            _fg, wash = _decision_colors(dkey, pal)
            bvis = _cell_len(blabel) + (2 if wash is not None else 0)
            short = _coverage_short(ev_s)
            cov = f"{short} supported" if "/" in short else short
            ev = f"{pip(ekey, pal)} [{pal['muted']}]{_escape(cov)}[/]"
            evis = 2 + _cell_len(cov)
            trows.append((tmarkup, tvis, decision_badge(dkey, pal), bvis, ev, evis,
                          receipt_cost_text(s.get("cost") or {})))
        # Reserve margin for the pane's vertical scrollbar (~2 cols) plus the
        # ambiguous-width glyphs in each row (the ○/◐ pips, ·, … render wider than
        # one cell), so a row never overflows the card and wraps its cost.
        recent = _recent_table(trows, pal, max(60, card_w - 8))

    spark = _two_edge(
        sparkline(series, pal),
        f"[b {pal['ink']}]{abbr_tokens(history_total)}[/] [{pal['dim']}]fresh tokens[/]", card_w)

    return {
        "head": head,
        "attn_title": attn_title, "attn": attn,
        "rail_title": "SIGNAL RAIL", "rail": rail,
        "recent_title": recent_title, "recent": recent,
        "spark_title": "USAGE HISTORY · FRESH TOKENS · 90D · CLIENT REPORTED", "spark": spark,
    }


def _coverage_short(ev: dict) -> str:
    """A compact coverage token for the list (the full headline is in the detail)."""

    tile = ev.get("coverage_tile") if isinstance(ev.get("coverage_tile"), dict) else {}
    if str(tile.get("value") or "").strip():
        return str(tile["value"])
    if str(tile.get("absent") or "").strip():
        return str(tile["absent"])
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
    tally = _escape(str(ev.get("check_tally_text") or "").strip())
    if total == 0:
        return f"[{pal['dim']}]{tally or 'no checks recorded'}[/]"
    if failed:
        return f"[{pal['coral']}]{tally or f'{passed}/{total} · {failed} failed'}[/]"
    return f"[{pal['green']}]{tally or f'{passed}/{total} passed'}[/]"


def _prov_chips(names: list[str] | None, pal: dict[str, str]) -> str:
    if not names:
        return ""
    return "  " + " ".join(f"[{pal['muted']} on {pal['chip']}] {_escape(source_label(n))} [/]" for n in names)


def _checks_count(ev: dict, pal: dict[str, str]) -> tuple[str, str]:
    """(value, colour) for a compact checks count — ``6/6`` green, ``0/1`` coral
    when any failed, ``no runs`` dim when none."""

    total = int(ev.get("checks_total") or 0)
    passed = int(ev.get("checks_passed") or 0)
    failed = int(ev.get("checks_failed") or 0)
    tile = ev.get("checks_tile") if isinstance(ev.get("checks_tile"), dict) else {}
    shown = str(tile.get("value") or "").strip()
    if total == 0:
        return shown or "no runs", pal["dim"]
    return shown or f"{passed}/{total}", (pal["coral"] if failed else pal["green"])


def _work_row_cells(s: dict, pal: dict[str, str], tcol: int) -> tuple[list[Any], str]:
    """One receipt as a DataTable row: Task / Outcome / Evidence / Cost / Age, each
    a single-line Rich Text so columns align and sort. The Task title is truncated
    cell-width-aware (CJK-safe) to ``tcol`` so it never forces a horizontal scroll.
    Also returns a plain-text mirror of the row for headless tests. The detail pane
    is authoritative for everything a compact row can't show."""

    tid = str(s.get("task_id"))
    dkey = str((s.get("decision_status") or {}).get("key"))
    ev = s.get("evidence_strength") or {}
    ekey = str(ev.get("key"))
    cost = receipt_cost_text(s.get("cost") or {})
    age = _humanize_ago(s.get("last_activity_at"), time.time())
    cov = _coverage_short(ev)
    full_title = str(s.get("title") or tid)
    title = _trunc(full_title, tcol)
    outcome = decision_label(dkey)
    fg, _wash = _decision_colors(dkey, pal)

    task_cell = _RText(title, style=pal["ink"], no_wrap=True, overflow="ellipsis")
    outcome_cell = _RText(_trunc(outcome, 14), style=fg, no_wrap=True)
    # Evidence: the pip shape carries the grade; add a compact P/T checks count only
    # when the receipt is GRADEABLE and checks ran. Never show a green passing tally
    # next to an ungradeable receipt (its checks are unattributed pool runs, not
    # evidence for this task) — that would overstate. The hollow pip says the rest.
    cval, ccol = _checks_count(ev, pal)
    ev_text = _RText.from_markup(pip(ekey, pal))
    if ev.get("gradeable") and cval != "no runs":
        ev_text.append(" ")
        ev_text.append(cval, style=ccol)
    cost_cell = _RText(cost, style=pal["dim"], justify="right")
    # The "Age" header already says "ago"; drop the suffix so the cell fits.
    age_cell = _RText(age.replace(" ago", ""), style=pal["dim"], no_wrap=True)
    cells: list[Any] = [task_cell, outcome_cell, ev_text, cost_cell, age_cell]
    # Mirror carries the FULL title (not the width-truncated cell) so headless
    # tests can assert row content independent of the terminal width.
    plain = " · ".join(p for p in (full_title, outcome, cov, cost, age) if p and p != "—")
    return cells, plain


def _kv_grid(cells: list[tuple[str, str]], pal: dict[str, str], width: int) -> str:
    """A two-row label/value grid (caps label above a bold value), columns split by
    whitespace only — the artifact's RECORDED REASON / OBSERVED / PROVENANCE block
    (a lighter look than the KPI strips, which do carry ``│`` dividers)."""

    def pad(text: str) -> str:
        return _escape(text + " " * max(3, width - _cell_len(text)))

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
        return f"[{pal['dim']}]{_escape(text.upper())}[/]" + " " * max(2, target - _cell_len(text.upper()))

    header = (head_cell(receipt_field_label("task"), tcol) + head_cell(receipt_field_label("decision"), ocol)
              + head_cell(receipt_field_label("coverage"), ecol)
              + f"[{pal['dim']}]{receipt_field_label('cost').upper():>{max(4, tw - tcol - ocol - ecol)}}[/]")
    out = [header, f"[{pal['line']}]{'─' * tw}[/]"]
    for tmarkup, tvis, badge, bvis, ev, evis, cost in rows:
        # The TASK cell is padded to EXACTLY tcol (titles are pre-truncated to fit),
        # so OUTCOME and EVIDENCE start at the same x on every row.
        title_cell = tmarkup + " " * max(1, tcol - tvis)
        left = title_cell + pad_vis(badge, bvis, ocol) + pad_vis(ev, evis, ecol)
        left_vis = tcol + max(ocol, bvis + 2) + max(ecol, evis + 2)
        gap = max(1, tw - left_vis - _cell_len(cost))
        out.append("")  # a blank line before each row — the artifact's airier rhythm
        out.append(left + " " * gap + f"[{pal['ink']}]{_escape(cost)}[/]")
    return "\n".join(out)


def _kpi_cells(cells: list[tuple[str, str, str]], pal: dict[str, str], width: int) -> str:
    """A row of KPI blocks — a caps label, a bold value, and a sub-label, stacked
    and aligned into fixed-width monospace columns (a terminal can't scale font
    size, so prominence comes from weight + the labelled block, not size)."""

    def pad(text: str) -> str:
        return _escape(text + " " * max(2, width - _cell_len(text)))

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
        if cur and _cell_len(cur) + 1 + _cell_len(w) > width:
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

    from .receipt_markdown import receipt_attention_lines, receipt_lead

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
        f"[{pal['accent']}]‹ All receipts[/]   [{pal['dim']}]SESSIONS / {_escape(short.upper())}[/]",
        f"[b {pal['ink']}]{_escape(str(receipt.get('title') or 'Task'))}[/]  {decision_badge(dkey, pal)}",
    ]
    updated = _humanize_ago(receipt.get("last_activity_at"), time.time())
    meta = " · ".join(p for p in (
        short, str(actors.get("primary_agent") or ""), ", ".join(actors.get("models") or []),
        (f"updated {updated}" if updated != "—" else ""),
    ) if p)
    if meta:
        head_lines.append(f"[{pal['dim']}]{_escape(meta)}[/]")

    # outcome card — statement + the two KPI blocks + the attention block. Every
    # label and sentence is the payload's own (the same strings the app, the CLI
    # and --markdown print), via the shared receipt text helpers.
    lead = receipt_lead(receipt)
    labels = lead["field_labels"]
    out: list[str] = []
    if lead["decision_statement"]:
        tail = f" [{pal['muted']}]— asserted by {_escape(lead['asserted_by_phrase'])}[/]"
        out.append(f"[{pal['ink']}]{_escape(lead['decision_statement'])}[/]{tail}")
    for line in (lead["gap_line"], lead["coverage_ledger"], lead["outcome_summary_line"], lead["next_step_line"]):
        if line:
            out.extend(f"[{pal['muted']}]{_escape(x)}[/]" for x in _wrap_words(line, dw))
    if out:
        out.append(f"[{pal['line']}]{'─' * dw}[/]")  # rule between the statement and the KPI blocks
    coverage_tile = evidence.get("coverage_tile") or {}
    checks_tile = ev_dim.get("checks_tile") or evidence.get("checks_tile") or {}
    out.append(_kpi_cells(
        [
            (labels["coverage"],
             str(coverage_tile.get("value") or coverage_tile.get("absent") or lead["coverage_hero"]),
             str(coverage_tile.get("qualifier") or "")),
            (labels["checks"],
             str(checks_tile.get("value") or checks_tile.get("absent") or ev_dim.get("check_tally_text") or ""),
             str(checks_tile.get("qualifier") or "")),
        ],
        pal, 26))
    attention_lines = receipt_attention_lines(receipt)
    if attention_lines:
        reason, label, *rest = attention_lines
        out.append(f"[{pal['line']}]{'─' * dw}[/]")
        out.append(f"[b {pal['ink']}]{_escape(reason)}[/]" + (f" [{pal['muted']}]· {_escape(label)}[/]" if label else ""))
        for line in rest:
            out.extend(f"  [{pal['muted']}]{_escape(x)}[/]" for x in _wrap_words(line, dw - 2))

    # summary strip — a four-cell metric row (its own bordered card).
    # The tool-call tile is the reducer's: a count, or its named absence.
    actions_tile = actions.get("actions_tile") or {}
    tool_value = str(actions_tile.get("value") or actions_tile.get("absent") or "not recorded")
    dur = receipt.get("duration_seconds")
    elapsed = humanize_seconds(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0 else "not recorded"
    boundary = (dims.get("task") or {}).get("boundary") or {}
    raw_sessions = boundary.get("session_count")
    sessions = raw_sessions if isinstance(raw_sessions, int) and not isinstance(raw_sessions, bool) else None
    roots = int(boundary.get("root_count") or 0)
    summary = _kpi_cells([
        (labels["actions"], tool_value, str(actions_tile.get("qualifier") or "")),
        (labels["cost"], str(cost.get("display_text") or receipt_cost_text(cost)),
         "" if str(cost.get("state") or "") in {"no_usage", "unpriced"} else str(cost.get("basis_label") or "")),
        ("Elapsed", elapsed, ""),
        ("Sessions", str(sessions) if sessions else "not recorded",
         f"{roots} root{'s' if roots != 1 else ''}" if roots else ""),
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
    actor_lines = [f"[{pal['ink']}]{_escape(x)}[/]"
                   for x in _wrap_words(" · ".join(actor_parts) or "no agent recorded", cw)]
    ledger_rows.append((labels["agents"], actor_lines + _chips_line("actors")))

    # Prefer the specific tool NAMES (Read / Edit / Bash / Grep — the artifact's
    # breakdown); fall back to the coarse categories when names were not captured.
    bar_counts = (actions.get("tool_name_counts") or {}) or (actions.get("tool_category_counts") or {})
    if bar_counts:
        synopsis = actions.get("actions_synopsis") or {}
        headline = str(synopsis.get("headline") or tool_value)
        act_lines = [f"[{pal['ink']}]{_escape(headline)}[/][{pal['dim']}] · by type · shared scale[/]"]
        act_lines += _chips_line("actions")
        act_lines += _tool_bars(bar_counts, pal).split("\n")
        ledger_rows.append((labels["actions"], act_lines))

    # (OUTCOME is intentionally omitted here — it duplicates the CURRENT OUTCOME
    # hero card above; dropping it keeps the ledger short enough for the footer.)

    dl = [_ledger(ledger_rows, pal, gutter, dw)]
    gaps = (dims.get("gaps") or {}).get("items") or []
    if gaps:
        dl.append(f"[{pal['line']}]{'─' * dw}[/]")
        dl.append(f"[{pal['amber']}]{caps(f'Gaps · {len(gaps)} — what could not be proven', pal)}[/]")
        for gap in gaps[:6]:
            gap_label = str(gap.get("dimension_label") or receipt_field_label(gap.get("dimension")))
            dl.append(f"  [{pal['dim']}]{_escape(gap_label)}[/] [{pal['muted']}]{_escape(str(gap.get('reason')))}[/]")

    # Footer affordance: drill into the sessions & steps behind this receipt.
    n_sessions = sessions or 0
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

    glyph, col = check_mark(check.get("result"), pal, source_tier_key(check.get("source")))
    rlabel = str(check.get("result_label") or check_result_label(check.get("result")))
    kind = str(check.get("kind") or "").capitalize()
    text = str(check.get("summary") or check.get("name") or "recorded check")
    head = (f"[{col}]{glyph} {rlabel}[/] [{pal['muted']}]{_escape(kind)}[/]  "
            f"[{pal['ink']}]{_escape(text)}[/]")
    meta: list[str] = []
    code = check.get("exit_code")
    if code is not None:
        meta.append(f"exit {int(code)}")
    if check.get("source"):
        meta.append(source_label(check.get("source")))
    ago = _humanize_ago(check.get("at"), now)
    if ago != "—":
        meta.append(ago)
    line2 = f"  [{pal['dim']}]{_escape(' · '.join(meta) or 'no metadata')}[/]"
    if check.get("artifact_ref"):
        line2 += f"  [{pal['muted']} on {pal['chip']}] {_escape(str(check['artifact_ref']))} [/]"
    elif check.get("command_state_text"):
        line2 += f"  [{pal['dim']}]· {_escape(str(check['command_state_text']))}[/]"
    return [head, line2]


def _activity_mark(event: dict, pal: dict[str, str]) -> tuple[str, str]:
    """(glyph, colour) for one activity event — a check wears its result mark
    (✓/✗/»), other work a status pip (● live/done, ○ observed, coral if failed)."""

    kind = str(event.get("kind") or "")
    status = str(event.get("status") or "").lower()
    if kind == "check":
        return check_mark(status, pal)
    if status in ("blocked", "failed"):
        return "●", pal["coral"]
    if status in ("active", "in_progress", "started", "checkpoint"):
        return "●", pal["accent"]
    if status in ("completed", "resolved"):
        return "●", pal["ink"]
    return "○", pal["muted"]


def _task_activity_timeline(events: list[dict], pal: dict[str, str], width: int, max_rows: int = 12) -> dict[str, Any]:
    """One task's recorded work + checks on a shared time axis — the terminal
    stand-in for the app's Activity canvas. Each event is a row (its result mark
    + title) with a block at its occurred_at; a timeless event sits faded at the
    start. Chronological (oldest first), matching build_timeline_events order."""

    left_col = max(24, min(44, width // 2))
    track = max(12, width - left_col - 1)
    times = [float(e["occurred_at"]) for e in events
             if isinstance(e.get("occurred_at"), (int, float)) and not isinstance(e.get("occurred_at"), bool) and e["occurred_at"] > 0]
    w0 = min(times) if times else None
    w1 = max(times) if times else None
    span = (w1 - w0) if (w0 is not None and w1 is not None and w1 > w0) else 0.0
    shown = events[:max_rows]
    rows: list[str] = []
    timeless = 0
    for e in shown:
        glyph, color = _activity_mark(e, pal)
        title = _escape(_trunc(str(e.get("title") or "event"), left_col - 2))
        left = _pad_vis(f"[{color}]{glyph}[/] [{pal['ink']}]{title}[/]", left_col)
        t = e.get("occurred_at")
        if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 and w0 is not None:
            frac = ((float(t) - w0) / span) if span > 0 else 0.0
            col = min(track - 1, max(0, round(frac * (track - 1))))
            trackstr = f"[{pal['hair']}]{'─' * col}[/][{color}]█[/][{pal['hair']}]{'─' * (track - col - 1)}[/]"
        else:
            timeless += 1
            trackstr = f"[{pal['dim']}]█[/][{pal['hair']}]{'─' * (track - 1)}[/]"
        rows.append(f"{left} {trackstr}")
    if w0 is not None and w1 is not None:
        axis = " " * (left_col + 1) + _two_edge(
            f"[{pal['dim']}]{_axis_stamp(w0, span)}[/]", f"[{pal['dim']}]{_axis_stamp(w1, span)}[/]", track
        )
    else:
        axis = ""
    return {"rows": rows, "axis": axis, "hidden": max(0, len(events) - len(shown)), "timeless": timeless}


def _build_steps_parts(receipt: dict, checks: list[dict], pal: dict[str, str], width: int = 150,
                       task: dict | None = None) -> dict[str, str]:
    """The sessions & steps drill-down: a per-task ACTIVITY timeline (recorded
    work + checks on a shared axis) atop a checks list grouped into NEEDS
    ATTENTION (failing) and OTHER CURRENT CHECKS (passed/skipped), plus files.
    Mirrors the app's activity timeline + sessions-&-steps frame."""

    now = time.time()
    dw = max(46, int(width * 0.54) - 10)
    axes = receipt.get("axes") or {}
    decision = axes.get("decision_status") or {}
    evidence = axes.get("evidence_strength") or {}
    dkey = str(decision.get("key"))
    ekey = str(evidence.get("key"))
    short = _short_task_id(str(receipt.get("task_id") or ""))
    title = str(receipt.get("title") or "Task")

    head = f"[{pal['accent']}]‹ Receipt[/]   [{pal['dim']}]SESSIONS / {_escape(short.upper())} / STEPS[/]"

    def _cap(text: str, color: str) -> str:
        return f"[{color}]{_escape(text.upper())}[/]"

    attn = [c for c in checks if check_result_tone(c.get("result")) == "failure"]
    other = [c for c in checks if check_result_tone(c.get("result")) != "failure"]

    body: list[str] = []
    # Session summary.
    body.append(f"{pip(ekey, pal)} [b {pal['ink']}]{_escape(title)}[/]  {decision_badge(dkey, pal)}")
    # The reducer's one tally (named remainders: could not run, superseded,
    # earlier runs failed), never a local recount.
    counts = str(evidence.get("check_tally_text") or "no checks recorded")
    updated = _humanize_ago(receipt.get("last_activity_at"), now)
    tail = f" · updated {updated}" if updated != "—" else ""
    body.append(f"[{pal['dim']}]{_escape(counts)} · {_escape(decision_label(dkey).lower())}{tail}[/]")
    statement = decision.get("statement")
    if statement:
        body.append(f"[{pal['accent']}]↳[/] [{pal['ink']}]{_escape(str(statement))}[/]")
    if attn and dkey not in _DANGER:
        body.append(f"[{pal['amber']}]Marked done, but a recorded check is currently failing.[/]")

    # Activity timeline: the task's recorded work + checks on a shared time axis
    # (the app's Activity canvas, as a keyboard-friendly lane view).
    if task is not None:
        try:
            from .task_timeline import build_timeline_events

            # Let build_timeline_events use the RAW task checks (checks=None), exactly
            # like the app's /v1 timeline route. The projected `checks` list carries
            # its time as "at", but build_timeline_events reads "created_at" — so
            # passing it made every check timeless (all bunched, faded, at the start:
            # the old 'messy' activity timeline). The raw checks carry created_at.
            events = build_timeline_events(task)
        except Exception:  # noqa: BLE001
            events = []
        if events:
            # Size the timeline to the DETAIL COLUMN (dw), not the full terminal.
            # This card renders in the ~54% right column, so a full-width track
            # would overflow and wrap — and a wrapped row throws every block off
            # its axis (the old 'messy' timeline). Match the section rules' width.
            tl = _task_activity_timeline(events, pal, dw)
            body.append("")
            body.append(f"[{pal['line']}]{'─' * dw}[/]")
            body.append(_cap(f"Activity · {len(events)} events", pal["dim"]))
            body.append("")
            body.extend(tl["rows"])
            if tl["axis"]:
                body.append(tl["axis"])
            tail_notes = []
            if tl["hidden"]:
                tail_notes.append(f"{count_noun(int(tl['hidden']), 'earlier event')} not shown")
            if tl["timeless"]:
                tail_notes.append(f"{tl['timeless']} with no recorded time (faded at start)")
            if tail_notes:
                body.append(f"[{pal['dim']}]{_escape(' · '.join(tail_notes))}[/]")

    # Needs attention (failing checks).
    if attn:
        body.append("")
        body.append(f"[{pal['line']}]{'─' * dw}[/]")
        body.append(_cap(f"Needs attention · {len(attn)}", pal["coral"]))
        body.append("")
        for c in attn:
            body.extend(_check_rows(c, pal, now))
            body.append("")

    # Other current checks (passed / skipped) — all of them. The detail pane
    # scrolls by keyboard (ctrl+d / ctrl+u — pagedown is taken by the list cursor),
    # so we list the full set like the app does rather than capping at 4 behind a
    # "▾ Show more" cue that nothing could open.
    body.append(f"[{pal['line']}]{'─' * dw}[/]")
    body.append(_cap(f"Other current checks · {len(other)}", pal["dim"]))
    body.append("")
    for c in other:
        body.extend(_check_rows(c, pal, now))
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


def _window_label(window: Any) -> str:
    """The shared display name for a capacity window (``7-day limit`` /
    ``5-hour limit``), keyed on the window kind."""
    kind = str(getattr(window, "kind", ""))
    return WINDOW_LABELS.get(kind, str(getattr(window, "label", "")) or "limit window")


def _reset_text(resets_at: Any, now: float, pal: dict[str, str]) -> str:
    return f"[{pal['dim']}]· {_escape(reset_text(resets_at, now))}[/]"


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
    head = f"[b {pal['ink']}]Usage & limits[/]   [{pal['dim']}]provider-reported capacity · locally recorded usage[/]"
    if snap is not None:
        today_cost = cost_text(_window_totals(snap, "today"))
        head += (f"\n{caps('Today · all agents', pal)}  [b {pal['ink']}]{abbr_tokens(_window_total(snap, 'today'))}[/] "
                 f"[{pal['dim']}]fresh tokens[/]")
        head += f"   [{pal['ink']}]{_escape(today_cost)}[/] [{pal['dim']}]this period[/]"

    by_client = {str(r.get("client")): r for r in (snap.usage.by_client if snap else [])}

    def _recorded_stack(rec: dict | None) -> list[str]:
        """The recorded-use right column, stacked (tokens / sessions / cost) so it
        aligns down the client's title + window rows — the artifact's right column."""
        if not rec:
            return []
        return [
            f"[b {pal['ink']}]{abbr_tokens(rec.get('fresh_tokens'))}[/] [{pal['dim']}]fresh tokens[/]",
            f"[{pal['dim']}]{_cache_read_text(rec)} cache-read tokens[/]",
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
            f"[{pal['dim']}]{RECORDED_USAGE_TITLE.upper()} · {label.upper()}[/]", cap_w))
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
            wlabel = f"[{pal['muted']}]{_escape(_window_label(window)):<13}[/]"
            used = window.used_percent
            if used is None:
                left_rows.append(f"  {wlabel} [{pal['dim']}]not reported[/]")
                continue
            # Two rows per meter (the artifact's rhythm): the bar on the label row,
            # then the "% used · reset" caption on its own indented line below.
            left_rows.append(f"  {wlabel} {meter(used / 100.0, 26, pal)}")
            left_rows.append(f"                [{pal['accent']}]{used:.0f}% used[/]  "
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
        ("Fresh tokens", abbr_tokens(totals.get("fresh_tokens")), "input + output"),
        ("Cache reads", _cache_read_text(totals), "cache-read tokens"),
        ("Sessions", format_tokens(totals.get("sessions")), "with recorded usage"),
    ]
    if period_days:
        rec_cells.append(("Active days", f"{active_days}/{period_days}", "with recorded usage"))
    # Cost last: its basis label is the longest sub-label and must not push a
    # following column out of alignment.
    rec_cells.append(("Cost", cost_text(totals), cost_basis_caption(totals, COST_BASIS_NOT_REPORTED)))
    rec_body = _kpi_cells(rec_cells, pal, 20)
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
    """The per-source lozenge: the payload's ``state_title`` (the shared source
    copy), tinted by the live-fact rule — green only for a reporting source."""

    from .ingestion_health import source_state_copy

    state = str(s.get("state") or "unknown")
    title = str(s.get("state_title") or source_state_copy(s, watcher_running=running)["state_title"])
    if state == "healthy" and running and int(s.get("parsed") or 0) > 0:
        return _loz(title, pal["green"], pal["tg"], "●")
    if state == "degraded":
        return _loz(title, pal["amber"], pal["tm"], "○")
    # Every remaining state (pending, unknown, one this version does not name)
    # still gets its NAMED title from the shared source copy — never a raw
    # ``state.capitalize()``, which would print an unnamed absence.
    return _loz(title, pal["muted"], pal["tn"], "○")


def _overall_lozenge(state: str, running: bool, pal: dict[str, str]) -> str:
    # Severity-graded, matching the app's calmer Diagnostics: only a real "error"
    # (or a genuinely degraded source) is loud; a transient "attention" is amber,
    # and an advisory (a self-healing blip) never turns the surface red.
    if state == "healthy" and running:
        return _loz("Reporting", pal["green"], pal["tg"], "●")
    if state == "healthy":
        return _loz("Idle", pal["muted"], pal["tn"], "○")
    if state == "error":
        return _loz("Needs attention", pal["coral"], pal["tc"], "○")
    if state == "degraded":
        return _loz("Degraded", pal["amber"], pal["tm"], "○")
    if state == "attention":
        return _loz("Attention", pal["amber"], pal["tm"], "○")
    return _loz(state.capitalize() or "Unknown", pal["muted"], pal["tn"], "○")


# Ingestion issue severity → (content colour, wash) and a loud-first sort rank.
_ISSUE_SEV_RANK = {"error": 0, "attention": 1, "advisory": 2}


def _issue_severity_color(sev: str, pal: dict[str, str]) -> str:
    return {"error": pal["coral"], "attention": pal["amber"]}.get(sev, pal["dim"])


def _watcher_lozenge(watcher: dict, pal: dict[str, str]) -> str:
    from .ingestion_health import watcher_state_copy

    state = str(watcher.get("state") or "")
    title = str(watcher.get("state_title") or watcher_state_copy(watcher)["state_title"])
    if state == "running":
        return _loz(title, pal["green"], pal["tg"], "●")
    if state == "stale":
        return _loz(title, pal["amber"], pal["tm"], "○")
    if state == "stopped":
        return _loz(title, pal["coral"], pal["tc"], "○")
    return _loz(title, pal["muted"], pal["tn"], "○")


def _watcher_detail(watcher: dict) -> str:
    """The watcher's shared state sentence plus its recorded heartbeat and
    cadence (facts, not vocabulary)."""

    from .ingestion_health import watcher_state_copy

    if not watcher:
        return "The daemon reported no watcher block."
    heartbeat = _humanize_ago(watcher.get("heartbeat_at"), time.time())
    hb = f"last heartbeat {heartbeat}" if heartbeat != "—" else "no heartbeat recorded"
    cadence = watcher.get("interval_seconds")
    cad = f" · scans every {int(cadence)}s" if cadence else ""
    detail = str(watcher.get("state_detail") or watcher_state_copy(watcher)["state_detail"])
    if str(watcher.get("state") or "") == "not_configured":
        return detail
    return f"{detail} {hb}{cad}"


def _build_sources_parts(snapshot: dict, store_dir: Any, pal: dict[str, str], width: int = 150) -> dict[str, str]:
    card_w = max(60, width - 10)
    head = (f"[b {pal['ink']}]Diagnostics[/]   "
            f"[{pal['dim']}]what feeds the store · recording health · capture is local only[/]")

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

    from .ingestion_health import ingestion_state_copy

    copy = ingestion_state_copy(snapshot)
    overall_title = str(snapshot.get("state_title") or copy["state_title"])
    overall_detail = str(snapshot.get("state_detail") or copy["state_detail"])
    conn: list[str] = [
        f"[b {pal['ink']}]{_escape(overall_title)}[/]  [{pal['dim']}]{_escape(overall_detail)}[/]",
    ]
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
    issues_sorted = sorted(issues, key=lambda i: _ISSUE_SEV_RANK.get(str(i.get("severity") or "error"), 0))
    issue_lines: list[str] = []
    worst = "advisory"
    for issue in issues_sorted:
        sev = str(issue.get("severity") or "error")
        if _ISSUE_SEV_RANK.get(sev, 0) < _ISSUE_SEV_RANK.get(worst, 2):
            worst = sev
        color = _issue_severity_color(sev, pal)
        code = str(issue.get("code") or "issue").replace("_", " ")
        affected = issue.get("affected_sources")
        src_names = issue.get("source") or (", ".join(str(a) for a in affected) if isinstance(affected, list) and affected else "")
        src = f" — {src_names}" if src_names else ""
        tag = "" if sev == "error" else f" [{pal['dim']}]· {sev}[/]"
        issue_lines.append(f"[{color}]{_escape(code.capitalize() + src)}[/]{tag}")
        if issue.get("action"):
            issue_lines.append(f"  [{pal['dim']}]{_escape(str(issue.get('action')))}[/]")
    # Loud (error) → coral card, an attention blip → amber, advisory-only → quiet.
    issues_color = pal["coral"] if worst == "error" else pal["amber"] if worst == "attention" else pal["dim"]

    return {
        "head": head,
        "connected_title": f"CONNECTED SOURCES · {len(sources)}", "connected": "\n".join(conn),
        "watcher_title": "CONTINUOUS SYNC", "watcher": watcher_body,
        "verifiers_title": "VERIFIERS · NOT CONNECTED · UPGRADE SELF-CHECKED → VERIFIED", "verifiers": _verifiers_markup(pal, card_w),
        "issues_title": f"NEEDS ATTENTION · {len(issues)}", "issues": "\n".join(issue_lines),
        "issues_color": issues_color,
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
    """The capacity rail block: the ONE headline window every glance surface
    leads with (:func:`headline_limit_choice` — the most constrained live
    reading), worded ``99% used`` like the app."""

    now = time.time()
    choice = headline_limit_choice(
        (
            ((limit, window), limit_is_stale(limit, now), window.used_percent, window.window_minutes, window.resets_at)
            for limit in limits
            for window in limit.windows
        ),
        now,
    )
    if choice is not None:
        limit, window = choice
        return _rail_block(
            "Capacity", f"{_escape(str(limit.client))} · {_escape(limit_used_text(window.used_percent))}",
            f"{_escape(str(window.label))} · provider reported · {_escape(data_age_text(limit.captured_at, now))}", pal)
    return _rail_block("Capacity", "no live limit reported", "run `agentacct usage watch`", pal)


def _trust_block(ingestion: dict, pal: dict[str, str]) -> str:
    state = str((ingestion or {}).get("state") or "")
    watcher = (ingestion or {}).get("watcher") or {}
    running = str(watcher.get("state") or "") == "running"
    sources = (ingestion or {}).get("sources") or []
    last = max((float(s.get("last_success_at") or 0) for s in sources), default=0.0)
    ingest_sub = (
        f"last successful ingest {_humanize_ago(last, time.time())}"
        if last
        else _escape(str((ingestion or {}).get("state_detail") or "no ingest recorded yet"))
    )
    if state == "healthy" and running:
        return _rail_block("Evidence trust", "Sources healthy", ingest_sub, pal, label_color=pal["green"])
    if state == "healthy":
        return _rail_block("Evidence trust", "Sources idle", "watcher stopped", pal, label_color=pal["green"])
    if state == "degraded":
        return _rail_block("Evidence trust", "Sources degraded", ingest_sub, pal, label_color=pal["amber"])
    if not ingestion:
        return _rail_block("Evidence trust", "unavailable", "source health not reported", pal)
    # Never a bare state key as the title: the reducer's state copy names it.
    title = str(ingestion.get("state_title") or "Source status unavailable")
    return _rail_block("Evidence trust", title, ingest_sub, pal)


def _window_total(snap: LiveSnapshot, label: str) -> Any:
    """A window's headline measure: fresh tokens (input + output), the same
    measure the app and ``agentacct now`` lead with. Cache reads are named
    separately wherever they are shown."""
    for window in snap.usage.windows:
        if str(window.label) == label:
            return window.totals.get("fresh_tokens")
    return 0


def _cache_read_text(bucket: dict) -> str:
    """Cache-read tokens, or ``not reported`` when no row reported the counter."""
    if bucket.get("cache_read_reporting") in {"not_reported", "unknown"}:
        return "not reported"
    return abbr_tokens(bucket.get("cache_read_tokens") or 0)


def _window_totals(snap: LiveSnapshot, label: str) -> dict:
    for window in snap.usage.windows:
        if str(window.label) == label:
            return dict(window.totals or {})
    return {}


def _run() -> None:  # pragma: no cover - manual entrypoint parity
    raise SystemExit("Run via `agentacct tui`.")
