"""The single owner of agentacct's display text.

Every human-facing word that more than one surface prints lives here: the cost
grammar (``$`` / ``≈$`` / ``~$`` and the named absences), cost-basis spellings,
dates and reset phrases, limit-window names, share percentages, source and
evidence-tier labels, decision labels, receipt field labels and the effect
sentence of each disposition action.

The CLI, the TUI, the usage snapshot, the Markdown receipt and the payloads the
macOS app renders import from this module, so no two surfaces can word the same
fact differently. Raw keys stay in the data; only these labels are shown.

This module is a leaf: it imports nothing from the rest of agentacct, so any
reducer can depend on it without an import cycle.
"""

from __future__ import annotations

import math
import re as _re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    """The value as a finite float, or ``None`` (rejects bool, inf, nan)."""

    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    return None


# ---------------------------------------------------------------------------
# cost grammar (every cost carries its basis)
# ---------------------------------------------------------------------------

# Confidence / basis keys whose complete figure is a reported or billed amount
# (a bare ``$``). Anything else that is complete is an estimate (``≈$``).
# The cost-basis keys that name the same reported/billed kind of number are
# included so a row carrying only its basis reads the same as its record.
REPORTED_COST_CONFIDENCES: frozenset[str] = frozenset(
    {"client_reported", "provider_billed", "local_client_session", "provider_invoice"}
)

COST_PREFIX_REPORTED = "$"
COST_PREFIX_ESTIMATE = "≈$"
COST_PREFIX_PARTIAL = "~$"

COST_ABSENT_NO_USAGE = "no usage recorded"
COST_ABSENT_UNPRICED = "unpriced"

# A legend pair is one unit of meaning: its glyph and the words that define it
# must never land on different lines. The words inside a pair are joined with
# NO-BREAK SPACE (U+00A0) so every surface that wraps this string (the menu
# popover, the TUI, the CLI) can only break at the " · " separators (K25).
def _legend(*pairs: str) -> str:
    return " · ".join(pair.replace(" ", " ") for pair in pairs)


COST_LEGEND = _legend("~$ partial subtotal", "≈$ estimate", "$ reported or billed")
# The chart's legend row: the glyph grammar plus the partial-bar mark.
COST_CHART_LEGEND = _legend("~$ partial subtotal", "open cap = partial")
# A chart's cost unit, named once in its caption (ticks carry no glyph).
COST_CHART_UNIT = "USD"

# The ONE name for the locally recorded-usage lane, wherever it is titled: the
# menu section, the Usage pane section, the capacity table column and the TUI
# all say "recorded usage" — never "tracked usage" or "recorded use" (K51).
RECORDED_USAGE_TITLE = "Recorded usage"

# The storage noun for one imported usage row, in words a reviewer reads.
USAGE_RECORD_NOUN = "usage record"
COST_HELD_TEXT = "excluded usage records · not totaled"


def usage_records_text(count: int) -> str:
    """``1 usage record`` / ``3 usage records``."""

    return f"{count} {USAGE_RECORD_NOUN}" + ("" if count == 1 else "s")


def cost_unpriced_text(rows: Any, unpriced_rows: Any) -> str | None:
    """``3 of 5 usage records unpriced``; None when nothing is unpriced."""

    total = _finite(rows)
    unpriced = _finite(unpriced_rows)
    if total is None or unpriced is None or unpriced <= 0 or total <= 0:
        return None
    return f"{int(unpriced)} of {usage_records_text(int(total))} unpriced"


def cost_total_label(state: Any, *, rows: Any = None, unpriced_rows: Any = None) -> str:
    """What a range cost figure may call itself, keyed on the cube's cost
    state. Only a complete figure is a ``total``; a partial one is a
    ``Partial subtotal · N of M usage records unpriced``; nothing priced is a
    named absence, never ``total``."""

    key = str(state or "")
    if key == "complete":
        return "total"
    if key == "partial":
        unpriced = cost_unpriced_text(rows, unpriced_rows)
        return "Partial subtotal" + (f" · {unpriced}" if unpriced else "")
    if key == "none_recorded":
        return COST_ABSENT_NO_USAGE
    if key == "held":
        return COST_HELD_TEXT
    unpriced = cost_unpriced_text(rows, rows if unpriced_rows is None else unpriced_rows)
    return f"no priced usage · {unpriced}" if unpriced else COST_ABSENT_UNPRICED

# What "superseded" means on a check run (and on a finding it settled). One
# sentence shared by the receipt's check rows, the superseded-finding decision
# statement and every surface's help text.
SUPERSEDED_CHECK_DEFINITION = (
    "A recorded check failed, but a later same-scope check passed; the finding is kept "
    "in history and is not a verified outcome."
)

# One spelling per cost basis / cost confidence key.
COST_BASIS_LABELS: dict[str, str] = {
    "pricing_table": "pricing estimate",
    "estimated_from_tokens": "pricing estimate",
    "client_reported": "client-reported",
    "local_client_session": "client-reported",
    "provider_billed": "provider-billed",
    "provider_invoice": "provider-billed",
    "user_subscription": "subscription",
    "subscription_unavailable": "subscription cost unavailable",
    "mixed": "mixed basis",
    "none": "cost basis not reported",
    "subscription_equivalent": "subscription equivalent",
    "approximate_subscription_allocation": "approximate subscription share",
    "unknown": "cost basis not reported",
}
COST_BASIS_NOT_REPORTED = COST_BASIS_LABELS["unknown"]


_SAME_AS_AMOUNT: Any = object()


def format_dollars(amount: float, prefix: str) -> str:
    """``prefix`` plus a thousands-grouped two-decimal amount (``$1,554.67``)."""

    return f"{prefix}{float(amount):,.2f}"


def cost_display(
    amount: Any,
    complete: Any,
    confidence: Any,
    *,
    partial_amount: Any = _SAME_AS_AMOUNT,
    has_usage: bool = True,
) -> dict[str, Any]:
    """The cost grammar for one figure.

    * complete and reported/billed → ``$1,554.67``
    * complete estimate → ``≈$1,554.67``
    * a partial subtotal (not complete: some rows unpriced or held) → ``~$1,554.67``
    * nothing priced → a named absence: ``no usage recorded`` when there were no
      usage rows (``has_usage=False``), otherwise ``unpriced``.

    ``amount`` is the complete figure; ``partial_amount`` is the known priced
    subtotal to fall back on when the figure is not complete (defaults to
    ``amount``). Returns ``{display_text, prefix, state}`` where ``prefix`` is
    ``None`` for an absence and ``state`` is one of ``complete`` / ``partial`` /
    ``unpriced`` / ``no_usage``.
    """

    total = _finite(amount)
    if complete is True and total is not None:
        prefix = COST_PREFIX_REPORTED if str(confidence or "") in REPORTED_COST_CONFIDENCES else COST_PREFIX_ESTIMATE
        return {"display_text": format_dollars(total, prefix), "prefix": prefix, "state": "complete"}
    subtotal = total if partial_amount is _SAME_AS_AMOUNT else _finite(partial_amount)
    if subtotal is not None:
        return {
            "display_text": format_dollars(subtotal, COST_PREFIX_PARTIAL),
            "prefix": COST_PREFIX_PARTIAL,
            "state": "partial",
        }
    if not has_usage:
        return {"display_text": COST_ABSENT_NO_USAGE, "prefix": None, "state": "no_usage"}
    return {"display_text": COST_ABSENT_UNPRICED, "prefix": None, "state": "unpriced"}


def cost_basis_label(key: Any) -> str:
    """One spelling for a cost basis / confidence key; an unknown or missing key
    reads ``cost basis not reported``. An unmapped key is shown de-snaked rather
    than dropped."""

    text = str(key or "").strip()
    if not text:
        return COST_BASIS_NOT_REPORTED
    return COST_BASIS_LABELS.get(text, text.replace("_", " "))


def cost_confidence_display(mixed: Any, dominant: Any) -> str:
    """A bucket's cost confidence: ``mixed · mostly pricing estimate`` when rows
    disagree and one basis dominates, ``mixed`` when none dominates, otherwise the
    single basis label."""

    if mixed:
        if str(dominant or "").strip() and str(dominant) not in {"unknown", "mixed"}:
            return f"mixed · mostly {cost_basis_label(dominant)}"
        return "mixed"
    return cost_basis_label(dominant)


# ---------------------------------------------------------------------------
# dates, durations, resets
# ---------------------------------------------------------------------------


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts))


def display_date(ts: Any) -> str:
    """A calendar date in LOCAL time (``Sep 14``) — the same day basis the usage
    buckets use. A missing/invalid timestamp reads ``date not recorded``."""

    value = _finite(ts)
    if value is None:
        return "date not recorded"
    moment = _local(value)
    return f"{moment:%b} {moment.day}"


#: The named absence of an activity window (either edge missing or unusable).
TIME_SPAN_NOT_RECORDED = "window not recorded"


def display_time_span(start: Any, end: Any) -> str:
    """One activity window in LOCAL time: ``Sep 12 17:21-17:38`` when both edges
    fall on the same calendar day, ``Sep 12-Sep 14`` when they do not, and the
    named absence ``window not recorded`` when either edge is missing.

    Used wherever a surface has to say WHAT PERIOD a measurement covers — the
    difference between "60 tool calls" and "60 tool calls covering 17 minutes of
    a two-day task".
    """

    first = _finite(start)
    last = _finite(end)
    if first is None or last is None or first <= 0 or last <= 0:
        return TIME_SPAN_NOT_RECORDED
    if last < first:
        first, last = last, first
    began, ended = _local(first), _local(last)
    if began.date() == ended.date():
        if f"{began:%H:%M}" == f"{ended:%H:%M}":
            return f"{display_date(first)} {began:%H:%M}"
        return f"{display_date(first)} {began:%H:%M}–{ended:%H:%M}"
    return f"{display_date(first)}–{display_date(last)}"


#: A bucket key the cube could not date.
PERIOD_LABEL_UNDATED = "time not recorded"
#: The prefix a weekly bucket wears so its date cannot read as a single day.
PERIOD_LABEL_WEEK_PREFIX = "week of"


def period_label(period_key: Any, granularity: Any = None) -> str:
    """The ONE name for a usage bucket, wherever a chart, axis or readout says
    it: ``Sep 12`` for a day and ``week of Sep 12`` for a week.

    The key itself (``2026-09-12``) is the cube's identity, not display text; a
    bare ``09-12`` reads as 12 September in most of the world and as December 9
    in the US, and a weekly bucket labelled with one date reads as one day
    (K79). Both charts render this string rather than slicing the key.
    """

    key = str(period_key or "").strip()
    parts = key.split("-")
    if len(parts) != 3:
        return PERIOD_LABEL_UNDATED
    try:
        moment = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return PERIOD_LABEL_UNDATED
    day = f"{moment:%b} {moment.day}"
    if str(granularity or "").strip() == "weekly":
        return f"{PERIOD_LABEL_WEEK_PREFIX} {day}"
    return day


def display_clock(ts: Any) -> str:
    """A 12-hour local clock time (``10:50 PM``)."""

    value = _finite(ts)
    if value is None:
        return "time not recorded"
    moment = _local(value)
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment:%M} {'AM' if moment.hour < 12 else 'PM'}"


def humanize_seconds(seconds: float) -> str:
    """A compact ``2d 3h`` / ``4h 5m`` / ``6m`` / ``<1m`` duration string."""

    total = int(max(0, seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


# Freshness: one threshold and one phrase for "this just happened". Every
# relative-age stamp (the app's poll stamps, the TUI, the CLI) reads
# ``just now`` below this many seconds, never ``0s ago`` / ``1s ago``.
FRESHNESS_JUST_NOW_SECONDS = 5
FRESHNESS_JUST_NOW_TEXT = "just now"
# The two independent freshness facts on a capacity + usage header, joined
# with `` · `` (never merged: they refresh separately).
CAPACITY_CHECKED_LABEL = "capacity checked"
RECORDED_USAGE_REFRESHED_LABEL = "recorded usage refreshed"
FRESHNESS_SEPARATOR = " · "


def relative_age_text(seconds: Any) -> str:
    """``just now`` below :data:`FRESHNESS_JUST_NOW_SECONDS`, else ``1d 15h ago``."""

    value = _finite(seconds)
    if value is None:
        return "time not recorded"
    if value < FRESHNESS_JUST_NOW_SECONDS:
        return FRESHNESS_JUST_NOW_TEXT
    return f"{humanize_seconds(value)} ago"


def data_age_text(captured_at: Any, now: float) -> str:
    """How old a provider reading is, from its capture time: ``as of 1d 15h
    ago`` (the CLI, the TUI and the app print this one phrase). A reading with
    no capture time names that absence."""

    value = _finite(captured_at)
    if value is None or value <= 0:
        return "capture time not recorded"
    return f"as of {relative_age_text(max(0.0, float(now) - value))}"


RESET_NOT_REPORTED = "reset time not reported"


def reset_text(resets_at: Any, now: float) -> str:
    """One reset phrase with three named states: ``resets in 4d 3h`` (future),
    ``reset passed Sep 13, 10:50 PM`` (a reported instant already past) and
    ``reset time not reported`` (no reset instant at all). A past reset is never
    collapsed into "not reported"."""

    value = _finite(resets_at)
    if value is None or value <= 0:
        return RESET_NOT_REPORTED
    delta = value - float(now)
    if delta > 0:
        return f"resets in {humanize_seconds(delta)}"
    return f"reset passed {display_date(value)}, {display_clock(value)}"


# ---------------------------------------------------------------------------
# limit windows and shares
# ---------------------------------------------------------------------------

WINDOW_LABELS: dict[str, str] = {"5h": "5-hour limit", "7d": "7-day limit"}
# The compact kind tokens a one-line teaser uses (``codex: 5h 12% · 7d 26%``).
WINDOW_SHORT_LABELS: dict[str, str] = {"5h": "5h", "7d": "7d"}

# Limit feed origin → a short human label distinguishing the Claude feeds.
ORIGIN_LABELS: dict[str, str] = {
    "claude_plan_usage": "desktop app",
    "claude_statusline": "CLI",
}


def _whole_percent(value: float) -> str:
    if 0 < value < 1:
        return "<1%"
    return f"{int(value + 0.5)}%"


LIMIT_PERCENT_NOT_REPORTED = "used percent not reported"


def limit_used_text(used_percent: Any) -> str:
    """The ONE capacity wording: ``99% used`` (``<1% used`` for a real but tiny
    share), with the two terminal states named: ``100% used · limit reached``
    and ``104% used · limit exceeded``."""

    value = _finite(used_percent)
    if value is None:
        return LIMIT_PERCENT_NOT_REPORTED
    if value < 0:
        return "invalid provider percentage"
    text = f"{_whole_percent(value)} used"
    if value > 100:
        return f"{text} · limit exceeded"
    if value == 100:
        return f"{text} · limit reached"
    return text


def limit_value_text(used_percent: Any, *, reset_passed: bool) -> str:
    """A window's value phrase. Once its reset instant has passed the old share
    no longer describes the current window, so it reads ``last reported 3%``
    rather than a present-tense ``3% used``."""

    value = _finite(used_percent)
    if reset_passed and value is not None and value >= 0:
        return f"last reported {_whole_percent(value)}"
    return limit_used_text(used_percent)


# ---------------------------------------------------------------------------
# weekly plan share (one table; every payload emits chip_text / sentence_text)
# ---------------------------------------------------------------------------

# calibration state -> the short chip, the sentence a row or receipt prints,
# and the plain-language conclusion a detail view leads with (the technical
# fit basis stays behind a disclosure). ``row_no_share`` is the row-level case
# of a CALIBRATED client whose session or Task carries no share of its own —
# a different fact from ``out_of_band`` (the fit will not calibrate at all).
PLAN_SHARE_STATES: dict[str, dict[str, str]] = {
    "calibrated": {
        "chip": "plan share ready",
        "sentence": "weekly plan share estimated",
        "headline": "Weekly plan share is estimated from your recorded 7-day limit history",
    },
    "calibrating": {
        "chip": "calibrating",
        "sentence": "calibrating — not enough 7-day history yet",
        "headline": "Calibrating: not enough 7-day limit history recorded yet",
    },
    "out_of_band": {
        "chip": "plan share unavailable",
        "sentence": "won't calibrate at current ratio",
        "headline": "Won't calibrate: recorded usage doesn't track the weekly % closely enough",
    },
    "never": {
        "chip": "no weekly share",
        "sentence": "not applicable for this client",
        "headline": "Not applicable: this client reports no weekly plan meter",
    },
    "row_no_share": {
        "chip": "no share for this session",
        "sentence": "no priced usage in this session",
        "headline": "No priced usage in this session to estimate a share from",
    },
}
PLAN_SHARE_NOT_REPORTED = "plan share not reported"


def plan_share_state_text(state: Any) -> dict[str, str]:
    """``{chip_text, sentence_text, headline}`` for one plan-share state; an
    unknown or missing state is the named ``plan share not reported``."""

    entry = PLAN_SHARE_STATES.get(str(state or ""))
    if entry is None:
        return {
            "chip_text": PLAN_SHARE_NOT_REPORTED,
            "sentence_text": PLAN_SHARE_NOT_REPORTED,
            "headline": PLAN_SHARE_NOT_REPORTED,
        }
    return {"chip_text": entry["chip"], "sentence_text": entry["sentence"], "headline": entry["headline"]}


def plan_share_pct_text(pct: Any) -> str | None:
    """``≈12.2% of weekly plan`` (``≈<0.1%`` band, honest ``≈0%``); None when
    no share is reported."""

    value = _finite(pct)
    if value is None:
        return None
    if value >= 0.1:
        shown = f"≈{value:.1f}%"
    elif value > 0:
        shown = "≈<0.1%"
    else:
        shown = "≈0%"
    return f"{shown} of weekly plan"


def plan_share_fields(pct: Any, state: Any) -> dict[str, str]:
    """A session / Task plan share's words. Calibrated-or-nothing: a share
    only when calibrated; a calibrated row without one is ``row_no_share``."""

    key = str(state or "")
    shown = plan_share_pct_text(pct) if key == "calibrated" else None
    if shown is not None:
        return {"chip_text": shown, "sentence_text": shown, "headline": shown}
    fields = plan_share_state_text("row_no_share" if key == "calibrated" else key)
    # A row's headline is its sentence (the plain conclusion is client-level).
    return {**fields, "headline": fields["sentence_text"]}


# Plan-share measure: the estimate weighs cache reads, so its token figure
# includes them — named, so it never reads as the fresh-token column.
PLAN_SHARE_TOKENS_SUFFIX = "tokens incl. cache-read"
# By-model rows count a session once per model it used; their session column
# is not a partition of the range total.
BY_MODEL_SESSIONS_FOOTNOTE = "sessions using several models count once per model"



def recorded_usage_sessions_text(sessions: Any) -> str:
    """``Usage from 2406 sessions recorded (all time)`` — the store-wide count
    names its span so it never reads beside a ranged count as the same fact."""

    count = int(sessions) if isinstance(sessions, int) and not isinstance(sessions, bool) and sessions >= 0 else 0
    return f"Usage from {count} {'session' if count == 1 else 'sessions'} recorded (all time)"


# The one measure vocabulary for usage charts: labels, order and resting rule.
USAGE_SERIES: tuple[dict[str, str], ...] = (
    {"key": "tokens", "label": "Fresh tokens"},
    {"key": "cost", "label": "Cost"},
)
# The resting measure when nothing is persisted: Cost when any bucket carries a
# priced figure, else Fresh tokens.
USAGE_SERIES_DEFAULT_RULE = "cost_when_priced"


def percent_share(fraction: Any) -> str:
    """A 0..1 share as a whole percent: exactly 0 → ``0%``; a real but tiny share
    (below half a percent) → ``<1%`` so it never reads as none; otherwise the
    rounded integer percent."""

    value = _finite(fraction)
    if value is None or value <= 0:
        return "0%"
    if value < 0.005:
        return "<1%"
    return f"{int(value * 100 + 0.5)}%"


# ---------------------------------------------------------------------------
# evidence tiers and sources
# ---------------------------------------------------------------------------

TIER_TABLE: tuple[dict[str, str], ...] = (
    {
        "key": "externally_verified",
        "label": "externally verified",
        "definition": "CI or the model provider ran the check outside the agent's session and reported the result.",
    },
    {
        "key": "independently_checked",
        "label": "independently checked",
        "definition": "An agentacct client hook saw the check run in the agent's session, apart from the agent's own report.",
    },
    {
        "key": "self_checked",
        "label": "self-checked",
        "definition": "The agent ran the check itself and reported the result.",
    },
    {
        "key": "unchecked",
        "label": "unchecked",
        "definition": "No one ran a passing check for this step; only the agent's report says it is done.",
    },
)
TIER_LABELS: dict[str, str] = {row["key"]: row["label"] for row in TIER_TABLE}

# One step's evidence grade key → the words shown next to it. The proven grades
# reuse the tier words; a done step with only the agent's claim is "unchecked";
# a step that has not reached a terminal success is not graded at all.
EVIDENCE_GRADE_NOT_GRADED = "not graded"
EVIDENCE_GRADE_NOT_CHECK_RELEVANT = "not check-relevant"
EVIDENCE_GRADE_LABELS: dict[str, str] = {
    **TIER_LABELS,
    "claimed": TIER_LABELS["unchecked"],
    "none": EVIDENCE_GRADE_NOT_GRADED,
}


def step_not_graded_reason(status_label: str) -> str:
    """Why one step carries no grade: ``Handed off before completion — not
    graded``. Steps are ``not graded``; a whole Task is ``not gradeable``."""

    return f"{status_label} before completion — {EVIDENCE_GRADE_NOT_GRADED}"


# ---------------------------------------------------------------------------
# coverage, gap and ledger words (one tier noun: "unchecked")
# ---------------------------------------------------------------------------

# A Task whose coverage ratio has a zero denominator.
NOT_GRADEABLE_TEXT = "not gradeable"
# The reason in ``not gradeable (<reason>)``, named after the ledger bucket that
# made the denominator zero.
NOT_GRADEABLE_NO_STEPS = "no steps recorded"
NOT_GRADEABLE_NO_CHECKABLE_STEPS = "no checkable steps"
NOT_GRADEABLE_NO_FINISHED_STEPS = "no finished checkable steps"
# A completed checkable step with no passing check (the ``unchecked`` tier).
UNCHECKED_STEP_WORDS = TIER_LABELS["unchecked"]
STILL_OPEN_WORDS = "still open"
UNLINKED_CHECK_WORDS = "not linked to a step"
NOT_CHECK_RELEVANT_WORDS = EVIDENCE_GRADE_NOT_CHECK_RELEVANT
# The one definition of the scope term, shipped once (never inside each part).
NOT_CHECK_RELEVANT_DEFINITION = "Not check-relevant: review, research, planning, docs"
# Terminal stop status key -> the words after ``N step`` in the ledger.
STOP_LABELS: dict[str, str] = {
    "blocked": "blocked",
    "handed_off": "handed off",
    "failed": "failed",
}
# The label over the unproven part of the evidence gap, and the neutral label a
# text surface prints over an older payload's gap that carries no label.
GAP_LABEL_NOT_YET_PROVEN = "Not yet proven"
GAP_LABEL_FALLBACK = "Gap"


def sentence_case(text: str) -> str:
    """Upper-case the first character only (``not gradeable`` -> ``Not
    gradeable``; ``0/1 checked`` is unchanged)."""

    return f"{text[:1].upper()}{text[1:]}" if text else text


def evidence_grade_label(grade: Any, *, checkable: bool = True) -> str:
    """The label for one step's evidence grade key. A done step that owes no
    check (a review/research/planning/docs step with none attached) reads
    ``not check-relevant`` rather than ``unchecked``."""

    text = str(grade or "").strip() or "none"
    if text == "claimed" and not checkable:
        return EVIDENCE_GRADE_NOT_CHECK_RELEVANT
    return EVIDENCE_GRADE_LABELS.get(text, text.replace("_", " "))

_NOT_A_CHECK_SOURCE = "not a check source"

SOURCE_LABELS: dict[str, dict[str, Any]] = {
    "mcp": {
        "label": "Agent-reported",
        "legend": "Recorded by the agent through agentacct's MCP tools (sections, files, checks).",
        "tier_key": "self_checked",
        "tier_label": TIER_LABELS["self_checked"],
    },
    "hook": {
        "label": "Hook-captured",
        "legend": "Captured by an agentacct client hook (tool categories, mechanical checks).",
        "tier_key": "independently_checked",
        "tier_label": TIER_LABELS["independently_checked"],
    },
    "ci": {
        "label": "CI or provider",
        "legend": "Reported by external CI or the model provider.",
        "tier_key": "externally_verified",
        "tier_label": TIER_LABELS["externally_verified"],
    },
    "client_log": {
        "label": "Client log",
        "legend": "Observed in the agent's own local session / usage log.",
        "tier_key": None,
        "tier_label": _NOT_A_CHECK_SOURCE,
    },
    "transcript_scan": {
        "label": "Transcript scan",
        "legend": "Derived by agentacct from the client's own transcript / session store on disk (no live hook).",
        "tier_key": None,
        "tier_label": _NOT_A_CHECK_SOURCE,
    },
    "git": {
        "label": "Git",
        "legend": "Derived from the git repository.",
        "tier_key": None,
        "tier_label": _NOT_A_CHECK_SOURCE,
    },
    "human": {
        "label": "You",
        "legend": "Asserted by a person (review, approval, or finding disposition).",
        "tier_key": None,
        "tier_label": _NOT_A_CHECK_SOURCE,
    },
    "inferred": {
        "label": "Inferred",
        "legend": "Inferred by agentacct from an ambient signal (e.g. the session ended); not the agent's word.",
        "tier_key": None,
        "tier_label": _NOT_A_CHECK_SOURCE,
    },
    "none": {
        "label": "No source recorded",
        "legend": "Not captured by any source — recorded here as a gap, never guessed.",
        "tier_key": None,
        "tier_label": _NOT_A_CHECK_SOURCE,
    },
}


def source_label(key: Any) -> str:
    """The display label for a provenance source key. A missing key is the named
    ``No source recorded``; an unmapped key is shown de-snaked, never dropped."""

    text = str(key or "").strip()
    if not text:
        return SOURCE_LABELS["none"]["label"]
    entry = SOURCE_LABELS.get(text)
    return str(entry["label"]) if entry else text.replace("_", " ")


def source_tier_key(key: Any) -> str | None:
    """The evidence tier a provenance source can support, or ``None`` when that
    source is not a check source at all. Stated once here so no surface invents
    its own source→tier table (the app reads ``tier_key`` off the payload; the
    TUI, which renders a raw check row, calls this)."""

    entry = SOURCE_LABELS.get(str(key or "").strip())
    tier = entry.get("tier_key") if entry else None
    return str(tier) if tier else None


# ---------------------------------------------------------------------------
# decisions, attention, receipt fields, dispositions
# ---------------------------------------------------------------------------

# Sentence case, one label per decision-status key and per session work-status
# key. Every surface prints these; no surface re-cases a raw key.
DECISION_LABELS: dict[str, str] = {
    "verified": "Verified",
    "finding": "Finding",
    "finding_superseded": "Finding superseded",
    "finding_resolved_by_user": "Finding resolved",
    "blocker_resolved_by_user": "Blocker resolved",
    "failed": "Failed",
    "blocked": "Blocked",
    "reported": "Reported",
    "resolved": "Resolved",
    "mostly_done": "Mostly done",
    "handed_off": "Handed off",
    "ended_open": "Ended open",
    "inactive": "Inactive",
    "in_progress": "In progress",
    # A Task agentacct saw with no work steps recorded at all — named for the
    # absent thing, never "Observed" (that word is provenance/timestamp only).
    "observed": "No work recorded",
    "unknown": "No outcome recorded",
}

# A step's or session's recorded WORK status — the agent's own report of where
# the work stands. Kept apart from DECISION_LABELS: "Completed" is a claim, not
# a decision, and a surface that shows a Task's decision must never print it.
WORK_STATUS_LABELS: dict[str, str] = {
    "started": "In progress",
    "checkpoint": "In progress",
    "in_progress": "In progress",
    "completed": "Completed",
    "blocked": "Blocked",
    "handed_off": "Handed off",
    "failed": "Failed",
}


def decision_label(key: Any) -> str:
    """The sentence-case label for a decision status key. A missing key is
    ``No outcome recorded``; an unmapped key is de-snaked in sentence case."""

    text = str(key or "").strip()
    if not text:
        return DECISION_LABELS["unknown"]
    return DECISION_LABELS.get(text, text.replace("_", " ").capitalize())


def work_status_label(key: Any) -> str:
    """The sentence-case label for a recorded step/session work status
    (``started`` / ``checkpoint`` → ``In progress``); other keys fall back to
    the decision label spelling."""

    text = str(key or "").strip()
    return WORK_STATUS_LABELS.get(text) or decision_label(text)


# asserted_by key -> the short chip/column label, and the grammatical phrase a
# sentence prints after "asserted by" (a chip label never lands in prose).
ASSERTED_BY_LABELS: dict[str, str] = {
    "machine": "Machine check",
    "human": "You",
    "agent_report": "Agent-reported",
    "inferred": "Inferred",
    "none": "No source recorded",
}
ASSERTED_BY_PHRASES: dict[str, str] = {
    "machine": "a machine check",
    "human": "you",
    "agent_report": "the agent's report",
    "inferred": "agentacct's inference",
    "none": "no recorded source",
}


def asserted_by_label(key: Any) -> str:
    """The display label for an ``asserted_by`` key (unmapped keys de-snaked)."""

    text = str(key or "").strip()
    if not text:
        return ASSERTED_BY_LABELS["none"]
    return ASSERTED_BY_LABELS.get(text, text.replace("_", " "))


def asserted_by_phrase(key: Any) -> str:
    """The phrase after ``asserted by`` for an ``asserted_by`` key."""

    text = str(key or "").strip()
    if not text:
        return ASSERTED_BY_PHRASES["none"]
    return ASSERTED_BY_PHRASES.get(text, text.replace("_", " "))


# Attention-reason kind (an internal sort key) → the reason words shown.
ATTENTION_REASON_LABELS: dict[str, str] = {
    "failed_check": "Failed check",
    "blocker": "Blocker",
    "failed_step": "Failed run",
    # A check whose recorded result is ``error``: it could not run, so it
    # proves nothing either way. A named evidence gap, never a finding.
    "check_not_run": "Check could not run",
}

# ---------------------------------------------------------------------------
# check results
# ---------------------------------------------------------------------------

# A recorded check result key → the words every surface prints for it. The
# agent contract defines ``failed`` as "the check shows a defect in the work"
# and ``error`` as "the check could not run" — so ``error`` is never worded,
# counted or colored as a failure.
CHECK_RESULT_LABELS: dict[str, str] = {
    "passed": "Passed",
    "failed": "Failed",
    "error": "Could not run",
    "skipped": "Skipped",
    "unknown": "Result not recorded",
    # A probe that RAN and could not reproduce the reported problem. It is not a
    # defect (``failed`` would mark the task a Finding) and not a failure to run
    # (``error``): the check worked and the question stayed open. Naming it is
    # what stops a clean investigation from coloring a whole task coral.
    "not_reproduced": "Could not reproduce",
}

# A result key → the tone key a surface maps to its glyph and color:
# ``failure`` (coral, a cross) only for a recorded failure; ``not_run`` (muted,
# a minus) for a check that proved nothing; ``pass`` (the source's tier color).
CHECK_RESULT_TONES: dict[str, str] = {
    "passed": "pass",
    "failed": "failure",
    "error": "not_run",
    "skipped": "not_run",
    "unknown": "not_run",
    # Proved nothing about a defect, so it is muted like every other result that
    # is neither a pass nor a recorded failure. Never coral.
    "not_reproduced": "not_run",
}

# Result keys that assert a defect in the work (a Finding), and result keys
# that record a check which could not run (a named gap).
FAILED_CHECK_RESULTS: frozenset[str] = frozenset({"failed"})
NOT_RUN_CHECK_RESULTS: frozenset[str] = frozenset({"error"})

CHECK_NOT_RUN_WORDS = "could not run"


def check_result_key(result: Any) -> str:
    """The normalized result key (``unknown`` for a missing or unmapped one)."""

    text = str(result or "").strip().lower()
    return text if text in CHECK_RESULT_LABELS else "unknown"


def check_result_label(result: Any) -> str:
    """The display words for one recorded check result."""

    return CHECK_RESULT_LABELS[check_result_key(result)]


def check_result_tone(result: Any) -> str:
    """The tone key (``pass`` / ``failure`` / ``not_run``) for one result."""

    return CHECK_RESULT_TONES[check_result_key(result)]


def check_result_note(result: Any, exit_code: Any) -> str | None:
    """A named disagreement between a check's recorded result and its exit code
    (display only — the recorded result is never re-graded). ``None`` when they
    agree or either is missing."""

    code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
    if code is None:
        return None
    key = check_result_key(result)
    if key == "failed" and code == 0:
        return "Recorded as failed although the command exited 0."
    if key == "passed" and code != 0:
        return f"Recorded as passed although the command exited {code}."
    return None


def more_attention_text(*, findings: int = 0, not_run: int = 0, blockers: int = 0) -> str | None:
    """The count sentence for open attention items beyond the one shown:
    ``1 more open finding`` / ``2 more checks could not run`` / ``3 more open
    items`` when the kinds mix. ``None`` when there are none."""

    total = max(0, findings) + max(0, not_run) + max(0, blockers)
    if total <= 0:
        return None
    if total == findings:
        return f"{total} more open finding" + ("" if total == 1 else "s")
    if total == not_run:
        return f"{total} more check{'' if total == 1 else 's'} {CHECK_NOT_RUN_WORDS}"
    if total == blockers:
        return f"{total} more step{'' if total == 1 else 's'} with recorded blockers"
    return f"{total} more open item" + ("" if total == 1 else "s")

# One header per receipt field and per receipt dimension key. Every surface
# (CLI, Markdown, TUI, app tables, gap groups) prints these; a dimension key is
# never shown raw.
RECEIPT_FIELD_LABELS: dict[str, str] = {
    "task": "Task",
    "decision": "Decision",
    "outcome": "Decision",
    "coverage": "Coverage",
    "checks": "Checks",
    "evidence": "Checks",
    "cost": "Cost",
    "agents": "Agents",
    "actors": "Agents",
    "actions": "Tool calls",
    "weekly_plan": "Weekly plan",
    # The four questions a reviewer arrives with, as headings. They are section
    # names, not dimension names: `goal` heads the one sentence saying what the
    # work was FOR, and the other three head the sections answering "did it
    # work", "can I trust it" and "what do I do now". They live here so the
    # macOS app stops spelling `ReceiptSection(title: "Usage")` in Swift and
    # every surface can only print the same word.
    "goal": "Goal",
    "outcome_section": "Outcome",
    "evidence_section": "Evidence",
    "next_section": "Next",
}

#: The keys above that head a record-page SECTION rather than naming a receipt
#: DIMENSION. Split out (not kept in a second table) so the labels stay in one
#: place while a surface that renders dimensions can iterate the dimension keys
#: without owing a heading for a section it does not have.
RECORD_SECTION_LABEL_KEYS: frozenset[str] = frozenset(
    {"goal", "outcome_section", "evidence_section", "next_section"}
)

# The extra columns a LIST of tasks names beside the receipt's own fields: who
# recorded the work, when it last moved, and why it is in the review queue. A
# single receipt has no such columns, so they ship with the task list rather
# than with the receipt — but they live here, so a table header, a
# screen-reader field name and a TUI column cannot drift into three different
# words for one column.
TASK_LIST_FIELD_LABELS: dict[str, str] = {
    **RECEIPT_FIELD_LABELS,
    "client": "Client",
    "updated": "Updated",
    "attention": "Attention",
}


def receipt_field_label(key: Any) -> str:
    """The label for one receipt field or dimension key (an unmapped key is
    de-snaked in sentence case, never printed raw)."""

    text = str(key or "").strip()
    if not text:
        return "Receipt"
    return RECEIPT_FIELD_LABELS.get(text, sentence_case(text.replace("_", " ")))


# ---------------------------------------------------------------------------
# what the record was FOR, and the absence budget
# ---------------------------------------------------------------------------

# The one named absence that is EXEMPT from the absence budget below. Every
# other "we did not capture this" collapses into one line, but a record with no
# stated purpose is one a reviewer should distrust, so this sentence keeps its
# own slot under the title. It is the page head, not a section.
TASK_GOAL_ABSENT = "No goal was recorded for this task."

# The second exempt absence: the record's only remaining print of "where does
# this go next".
NEXT_STEP_ABSENT = "No next step recorded."

#: The separator every composed meta line breaks on. One character, so a
#: reader (and a line-wrapper) can only break a composed line where the
#: vocabulary intended, exactly as ``_legend`` and the freshness line do.
META_SEPARATOR = " · "


def _joined_meta(parts: Sequence[Any]) -> str:
    """``a · b · c`` from the parts that are actually present.

    Nothing is padded, invented or punctuated as a sentence: the measured
    failure this replaces was ``Passed. Exit 0. test. Agent-reported`` —
    four fragments given four full stops, which reads as four broken
    sentences rather than one line of four facts.
    """

    return META_SEPARATOR.join(
        text for text in (str(part).strip() for part in parts if part is not None) if text
    )


# --- the absence budget -----------------------------------------------------
# RULE: absence stays a NAMED state (a record must never imply it captured
# something it did not), but it collapses to ONE line with the detail behind a
# disclosure. Absence may never occupy more space than the facts it is absent
# from. Measured on task_5f7dbea9, the tail this replaces was 42.8% named
# absence against 9.7% new fact about the work.

NOT_CAPTURED_PREFIX = "not captured"

#: The noun for each thing a record can fail to capture. DECLARATION ORDER IS
#: RENDER ORDER, so two records can never word the same set differently — the
#: line is a vocabulary, not a sentence composed per record. A key this table
#: does not know is DROPPED rather than de-snaked: a raw payload key printed to
#: a reviewer is a second vocabulary.
NOT_CAPTURED_NOUNS: dict[str, str] = {
    "model": "model",
    "cost": "cost",
    "weekly_plan_share": "weekly plan share",
    "tool_calls": "tool calls",
    "tool_call_order": "ordered tool calls",
    "session_identity": "session identity",
    "revision": "revision",
    "project": "project",
}

#: How many nouns print inline before the rest become a count. Four is the
#: measured point at which the line still reads as a list rather than a
#: paragraph at the record page's ~1150pt measure.
NOT_CAPTURED_INLINE_MAX = 4

#: The tail for the nouns past the inline budget. They are not lost: the
#: disclosure behind this line carries every one of them, worded in full.
NOT_CAPTURED_OVERFLOW = "and {count} more"

#: When the STRONGER absence on the left is present, the WEAKER one on the right
#: says the same thing a second time and is dropped from the LINE (never from
#: the disclosure, which keeps every named absence in full).
#:
#: ``tool_calls`` means no tool call was captured at all; ``tool_call_order``
#: means the ones captured carry no order. A record that captured none cannot
#: also be missing their order as a separate fact -- printing both is exactly the
#: "say each fact once" failure this line exists to fix. The pair is declared
#: here, beside the nouns, because it is a claim about what the WORDS mean, and
#: it is deliberately a table rather than a rule so a reviewer can read off every
#: collapse the line performs.
#: ``weekly_plan_share`` is a percentage OF a priced cost, so a record with no
#: priced cost cannot separately be missing its share -- that is one absence
#: worded twice.
NOT_CAPTURED_SUBSUMED_BY: dict[str, str] = {
    "tool_call_order": "tool_calls",
    "weekly_plan_share": "cost",
}


def collapse_not_captured_keys(keys: Sequence[str]) -> list[str]:
    """The absence keys that still say something distinct, in declaration order.

    Drops a key whose stronger form is also absent (see
    ``NOT_CAPTURED_SUBSUMED_BY``) and anything the noun table does not know.
    The DISCLOSURE behind the line is built from the uncollapsed set: an absence
    is never deleted, only spared a second wording on the one line.
    """

    seen = {
        text
        for text in (str(key or "").strip() for key in keys or ())
        if text in NOT_CAPTURED_NOUNS
    }
    kept = {
        key
        for key in seen
        if NOT_CAPTURED_SUBSUMED_BY.get(key) not in seen
    }
    return [key for key in NOT_CAPTURED_NOUNS if key in kept]


def not_captured_line(keys: Sequence[str]) -> str | None:
    """The record's ONE absence line, or ``None`` when nothing is absent.

    ``not captured: model, cost, ordered tool calls, session identity`` —
    and, past ``NOT_CAPTURED_INLINE_MAX``, ``…, and 2 more``.

    An empty budget returns ``None`` and the surface prints NOTHING. It must
    never print a positive claim ("everything was captured"): the record knows
    what it failed to capture, not what there was to capture.
    """

    seen: set[str] = set()
    for key in keys or ():
        text = str(key or "").strip()
        if text in NOT_CAPTURED_NOUNS:
            seen.add(text)
    if not seen:
        return None
    # Declaration order, never the caller's order or a sort of the nouns.
    ordered = [noun for key, noun in NOT_CAPTURED_NOUNS.items() if key in seen]
    inline = ordered[:NOT_CAPTURED_INLINE_MAX]
    overflow = len(ordered) - len(inline)
    listed = ", ".join(inline)
    if overflow > 0:
        listed = f"{listed}, {NOT_CAPTURED_OVERFLOW.format(count=overflow)}"
    return f"{NOT_CAPTURED_PREFIX}: {listed}"


# --- the check row's own line -----------------------------------------------


def check_meta_line(
    result_label: Any = None,
    exit_code: Any = None,
    evidence_type: Any = None,
    source_label: Any = None,
) -> str:
    """One line for a check row: ``Failed · Exit 1 · test · Agent-reported``.

    Composed HERE, not on a surface, so the app prints ``check.meta_line`` and
    composes nothing. Every argument is optional because the reducer removes
    whatever it hoisted: when `evidence_type` and `source_label` are identical
    on every row of a record they belong on the section heading, and passing
    them again here is what printed ``Agent-reported`` six times on one page.
    """

    exit_text = None
    if exit_code is not None and not isinstance(exit_code, bool):
        try:
            exit_text = f"Exit {int(exit_code)}"
        except (TypeError, ValueError):
            exit_text = None
    return _joined_meta((result_label, exit_text, evidence_type, source_label))


def checks_heading_line(
    tally: Any = None,
    tier_label: Any = None,
    source_label: Any = None,
    evidence_type: Any = None,
) -> str:
    """The evidence section's heading line: the tally, then whichever of the
    tier, source and type is uniform across every row and was therefore hoisted
    off the rows. Stating the tier here is the record's ONLY print of it."""

    return _joined_meta((tally, tier_label, source_label, evidence_type))


# --- the check summary, clipped where a sentence ends -----------------------
# The measured failure: a one-line clamp cut check 0 of task_5f7dbea9 at
# ``raises decimal.InvalidOperation on the st…``. A preview that ends mid-clause
# is not a smaller version of the summary, so it is cut at a SENTENCE boundary:
# the first sentence is kept whole whatever its length, and the budget only
# decides how many further whole sentences ride along.

#: How many characters the record page's two-line summary slot carries at its
#: ~1150pt measure. A SOFT budget: it decides how many WHOLE sentences ride
#: along, never where a sentence is cut.
CHECK_SUMMARY_PREVIEW_BUDGET = 200

#: A sentence ends at ``.``/``!``/``?``, optionally closed by a quote or bracket,
#: and followed by whitespace. ``decimal.InvalidOperation`` and ``$1,234.56)``
#: are therefore NOT boundaries: no whitespace follows the stop.
_SENTENCE_BOUNDARY = _re.compile(r'(?<=[.!?])["\')\]]*\s+')


def split_sentences(text: Any) -> list[str]:
    """One string split into whole sentences, each keeping its own punctuation.

    Shared by the preview and its tests so the two cannot disagree about where a
    sentence ends.
    """

    body = str(text or "").strip()
    if not body:
        return []
    return [part.strip() for part in _SENTENCE_BOUNDARY.split(body) if part.strip()]


def check_summary_preview(
    summary: Any, budget: int = CHECK_SUMMARY_PREVIEW_BUDGET
) -> tuple[str | None, bool]:
    """``(preview, elided)`` for one check summary.

    The preview is always a run of WHOLE sentences from the start, so it can
    never end mid-clause, and it carries NO ellipsis: a full stop already
    terminates it, and ``elided`` is what tells a surface to offer the rest.
    The rules, in order:

    1. the first sentence is always kept, however long it is;
    2. further sentences ride along only while the run fits the budget.

    ``(None, False)`` for an absent summary: absence is the caller's to name.
    """

    sentences = split_sentences(summary)
    if not sentences:
        return None, False
    limit = budget if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0 else 0
    kept = sentences[:1]
    for sentence in sentences[1:]:
        candidate = " ".join([*kept, sentence])
        if len(candidate) > limit:
            break
        kept.append(sentence)
    return " ".join(kept), len(kept) < len(sentences)


# ---------------------------------------------------------------------------
# the Actions (tool calls) dimension
# ---------------------------------------------------------------------------

# Named absences of the tool-call count — one phrase per distinct state:
# nothing was instrumented; capture ran and recorded no calls; or the payload
# does not say whether capture ran at all.
ACTIONS_NOT_INSTRUMENTED = "not instrumented"
ACTIONS_NO_TOOL_CALLS = "no tool calls recorded"
ACTIONS_CAPTURE_UNKNOWN = "capture coverage unknown"
# A Task whose steps and checks recorded no related path.
RELATED_PATHS_NONE = "no related paths recorded"
ACTIONS_CAPTURE_BOUNDARY = (
    "No ordered action ledger; captured tool-call counts cannot be linked to results or timing."
)
# The tile qualifier for a capture the payload itself proves incomplete.
ACTIONS_CAPTURE_PARTIAL_QUALIFIER = "partial coverage"


def actions_capture_shortfall(coverage: Any, total: Any) -> str | None:
    """What a tool-call capture did NOT cover, in the payload's own words — or
    ``None`` when nothing in the payload proves a shortfall.

    The PROOF is the RECORD SHORTFALL (``record_shortfalls``): rows of
    ``{record_label, call_label, captured, recorded}`` where the ledger holds
    MORE records of a kind than the capture saw calls that could have written
    them. Every recorded section and check cost the agent at least one tool
    call, so ``captured < recorded`` is arithmetic, not inference.

    The CAPTURE WINDOW (``captured_first_at`` / ``captured_last_at``) against
    the Task's own ACTIVITY WINDOW (``activity_first_at`` /
    ``activity_last_at``) rides along as CONTEXT once a shortfall is proven —
    "17 minutes of a two-day task" is what makes the shortfall legible. It is
    deliberately NOT a trigger of its own: capture arrives in batches whose
    drain time bounds when a batch ENDED, not when its calls began, so a
    single-batch session looks like an instant even when it covered everything.
    Downgrading on that alone would trade one false statement for another.
    """

    coverage = coverage if isinstance(coverage, Mapping) else {}
    clauses: list[str] = []
    rows = coverage.get("record_shortfalls")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        captured = row.get("captured")
        recorded = row.get("recorded")
        if (
            isinstance(captured, bool)
            or isinstance(recorded, bool)
            or not isinstance(captured, int)
            or not isinstance(recorded, int)
            or captured < 0
            or recorded <= captured
        ):
            continue
        record_label = str(row.get("record_label") or "record").strip() or "record"
        call_label = str(row.get("call_label") or "recording").strip() or "recording"
        seen = (
            f"no {call_label} call"
            if captured == 0
            else _count_words(captured, f"{call_label} call")
        )
        clauses.append(
            f"the ledger holds {_count_words(recorded, record_label)} but capture saw {seen}"
        )
    if not clauses:
        return None
    captured_first = _finite(coverage.get("captured_first_at"))
    captured_last = _finite(coverage.get("captured_last_at"))
    activity_first = _finite(coverage.get("activity_first_at"))
    activity_last = _finite(coverage.get("activity_last_at"))
    if all(
        value is not None and value > 0
        for value in (captured_first, captured_last, activity_first, activity_last)
    ):
        count = total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None
        calls = _count_words(count, "call") if count is not None else "the captured calls"
        clauses.insert(
            0,
            f"captured {calls} covering {display_time_span(captured_first, captured_last)} "
            f"of a {display_time_span(activity_first, activity_last)} task",
        )
    return " · ".join(clauses)

# Tool category key → (label, detail). Labels describe only the observed
# category; they never imply success, effect, importance, or risk.
TOOL_CATEGORY_LABELS: dict[str, tuple[str, str]] = {
    "read": ("Read", "File or context read tool calls"),
    "edit": ("Edit", "Edit or write tool calls"),
    "execute": ("Execute", "Command or process tool calls"),
    "search": ("Search", "File or text search tool calls"),
    "network": ("Network", "Network access tool calls"),
    "agent": ("Agent", "Agent coordination tool calls"),
    "plan": ("Plan", "Planning tool calls"),
    "mcp": ("Connected tools", "Connected-tool calls"),
    "other": ("Other", "Tool calls outside named categories"),
}


_INT64_MAX = 2**63 - 1


def _count_words(count: int, singular: str, plural_form: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"


def related_paths_text(count: Any) -> str:
    """The related-path scope: a named absence for zero, else the count and
    what a related path is. A missing or invalid count reads empty."""

    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return ""
    if count == 0:
        return RELATED_PATHS_NONE
    return f"{count} related {'path' if count == 1 else 'paths'}"


# What a related path is (help text beside the count; never a modified-file claim).
RELATED_PATHS_DEFINITION = (
    "Related paths are unique paths from recorded work, machine checks, or captured edit tool calls — "
    "recorded associations, not modified files."
)


def actions_synopsis(
    counts: Any,
    stored_total: Any,
    *,
    capture_known: bool,
    source_text: str = "",
    coverage: Any = None,
) -> dict[str, Any]:
    """The tool-call synopsis every surface renders, from the stored counts.

    ``counts`` is the category → count mapping (``None`` when the payload has
    none); ``stored_total`` the stored total (``None`` when missing — never
    coerced to 0). ``capture_known`` says a capture basis recorded this Task's
    tool activity (so a zero is a recorded zero). ``coverage`` (see
    ``actions_capture_shortfall``) is what lets a count PROVE it covers the
    Task: when it shows a gap the state is ``partial`` and names the gap —
    ``exact`` is earned, never assumed. Returns ``{state, headline,
    integrity_detail, metrics, can_show_distribution, capture_boundary,
    tile}`` where ``tile`` is ``{value, absent, qualifier}``.
    """

    raw = counts if isinstance(counts, dict) else None
    total = stored_total if isinstance(stored_total, int) and not isinstance(stored_total, bool) else None
    familiar: list[dict[str, Any]] = []
    for key, (label, detail) in TOOL_CATEGORY_LABELS.items():
        value = (raw or {}).get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            familiar.append({"key": key, "label": label, "detail": detail, "count": value})
    invalid = 0
    unrecognized = 0
    unknown_total = 0
    for key, value in (raw or {}).items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or not str(key).strip():
            invalid += 1
            continue
        if value > 0 and key not in TOOL_CATEGORY_LABELS:
            unrecognized += 1
            unknown_total += value
    metrics = list(familiar)
    if unrecognized:
        metrics.append(
            {
                "key": "__unknown_types__",
                "label": "Unrecognized types",
                "detail": f"{unrecognized} unrecognized {'category' if unrecognized == 1 else 'categories'}",
                "count": unknown_total,
            }
        )
    categorized = sum(int(metric["count"]) for metric in metrics)
    # A sum no 64-bit client can represent is an integrity failure, not a count.
    overflowed = categorized > _INT64_MAX
    if overflowed:
        metrics = [metric for metric in familiar if int(metric["count"]) <= _INT64_MAX] if unrecognized else []
    basis = source_text.strip() or None

    def joined(*parts: str | None) -> str | None:
        present = [part for part in parts if part]
        return " · ".join(present) if present else None

    def result(state: str, headline: str, *, detail: str | None = None, tile: dict[str, Any],
               boundary: bool = True, distribution: bool = False, shown: list[dict[str, Any]] | None = None
               ) -> dict[str, Any]:
        return {
            "state": state,
            "headline": headline,
            "integrity_detail": detail,
            "metrics": metrics if shown is None else shown,
            "can_show_distribution": distribution,
            "capture_boundary": ACTIONS_CAPTURE_BOUNDARY if boundary else None,
            "stored_total": total,
            "categorized_total": categorized,
            "tile": tile,
        }

    def absent(text: str, qualifier: str | None = None) -> dict[str, Any]:
        return {"value": None, "absent": text, "qualifier": qualifier}

    if invalid or overflowed or (total is not None and total < 0):
        details: list[str] = []
        if invalid:
            details.append(f"{invalid} invalid {'category was' if invalid == 1 else 'categories were'} omitted")
        if total is not None and total < 0:
            details.append("the stored total is invalid")
        if overflowed:
            details.append("the categorized tool-call sum overflowed")
        elif categorized > 0:
            details.append(
                f"{categorized} valid {'tool call remains' if categorized == 1 else 'tool calls remain'} categorized"
            )
        if total is not None and total >= 0:
            details.append(f"stored total is {total}")
        shown = result(
            "invalid", "Tool-call data incomplete", detail=" · ".join(details),
            tile=absent("tool-call data incomplete"),
            boundary=categorized > 0 or (total or 0) > 0,
        )
        if overflowed:
            shown["categorized_total"] = None
        return shown

    if total is None:
        if categorized > 0:
            state = "unrecognized_categories" if unrecognized else "total_unavailable"
            detail = (
                f"{_count_words(unrecognized, 'unrecognized tool-call type')} · stored total unavailable"
                if unrecognized
                else "Stored tool-call total unavailable"
            )
            return result(
                state, f"{_count_words(categorized, 'categorized tool call')}", detail=detail,
                tile={"value": str(categorized),
                      "absent": None,
                      "qualifier": joined("categorized calls", basis, "types changed" if unrecognized else None)},
            )
        return result(
            "capture_unknown", ACTIONS_CAPTURE_UNKNOWN, tile=absent(ACTIONS_CAPTURE_UNKNOWN),
            boundary=False, shown=[],
        )

    if total == 0 and categorized == 0:
        if capture_known:
            return result("no_tool_calls", ACTIONS_NO_TOOL_CALLS, tile=absent(ACTIONS_NO_TOOL_CALLS),
                          boundary=False, shown=[])
        return result("not_instrumented", ACTIONS_NOT_INSTRUMENTED, tile=absent(ACTIONS_NOT_INSTRUMENTED),
                      boundary=False, shown=[])

    if total > 0 and raw is None:
        return result(
            "total_only", f"{_count_words(total, 'tool call')} in stored total",
            detail="Tool-call type breakdown unavailable",
            tile={"value": str(total), "absent": None, "qualifier": basis}, shown=[],
        )

    if total != categorized:
        detail = f"category counts sum to {categorized} · stored total is {total}"
        if unrecognized:
            detail += f" · {_count_words(unrecognized, 'unrecognized tool-call type')}"
        return result("mismatch", "Tool-call totals conflict", detail=detail,
                      tile=absent("tool-call totals conflict"))

    if unrecognized:
        return result(
            "unrecognized_categories", f"{_count_words(total, 'tool call')} captured",
            detail=(
                f"{unrecognized} unrecognized tool-call {'type was' if unrecognized == 1 else 'types were'} "
                "aggregated as Unrecognized types"
            ),
            tile={"value": str(total), "absent": None, "qualifier": joined(basis, "types changed")},
        )
    # ``exact`` is a claim about COVERAGE, not about arithmetic: the categories
    # can add up perfectly and still describe 17 minutes of a two-day Task. When
    # the payload can prove the capture missed calls or missed time, the state
    # is ``partial`` and says so in the same breath as the count.
    shortfall = actions_capture_shortfall(coverage, total)
    if shortfall:
        return result(
            "partial", f"{_count_words(total, 'tool call')} captured", detail=shortfall,
            tile={"value": str(total), "absent": None,
                  "qualifier": joined(basis, ACTIONS_CAPTURE_PARTIAL_QUALIFIER)},
            distribution=total > 0 and bool(metrics),
        )
    return result(
        "exact", f"{_count_words(total, 'tool call')} captured",
        tile={"value": str(total), "absent": None, "qualifier": basis},
        distribution=total > 0 and bool(metrics),
    )


# ---------------------------------------------------------------------------
# redaction sentences
# ---------------------------------------------------------------------------

# What a redacted check shows instead of its command: the store never keeps
# the command argument, and the title is the agent's own recorded name (which
# may itself read like a command).
COMMAND_NOT_SHOWN_TEXT = "The agent's command argument was not stored; the title is the name the agent recorded."
ARTIFACT_PATH_NOT_SHOWN_TEXT = "The artifact path was withheld by its source and is not shown."
ARTIFACT_URL_NOT_SHOWN_TEXT = "The artifact URL was withheld by its source and is not shown."

# Two DIFFERENT facts used to share one sentence, and one of them was false.
#
# ``agent_recorded`` — the agent volunteered a command alongside its check. The
# text IS stored, verbatim, on the event; the receipt prints the NAME the agent
# recorded instead (it is usually the same string) rather than repeating it. The
# honest word is therefore "recorded", never "not stored".
#
# ``digest_only`` — a hook-derived check, where only a sha256 digest of the
# command was ever kept. There is no text anywhere. This, and only this, is the
# case ``COMMAND_NOT_SHOWN_TEXT`` describes.
COMMAND_STATE_AGENT_RECORDED = "agent_recorded"
COMMAND_STATE_DIGEST_ONLY = "digest_only"
COMMAND_AGENT_RECORDED_TEXT = (
    "The agent recorded this check's command; the receipt shows the name it recorded, not the command text."
)


def command_state_text(state: Any) -> str | None:
    """The sentence for one check's command state, or ``None`` when the check
    carries no command at all (an absence with nothing to explain)."""

    key = str(state or "").strip()
    if key == COMMAND_STATE_AGENT_RECORDED:
        return COMMAND_AGENT_RECORDED_TEXT
    if key == COMMAND_STATE_DIGEST_ONLY:
        return COMMAND_NOT_SHOWN_TEXT
    return None


# ---------------------------------------------------------------------------
# the revision a check was stamped with
# ---------------------------------------------------------------------------

#: The named absence when no capture path stamped a revision.
REVISION_NOT_CAPTURED = "revision not captured"
#: HEAD was read by the hook, in the same process that ran the check.
REVISION_BASIS_HOOK = "host_hook"
#: HEAD was read on the server when the record ARRIVED — which is not when the
#: check ran, and for an agent that records before it commits names the commit
#: BEFORE the work.
REVISION_BASIS_SERVER_AT_RECORD = "server_captured_at_record"
#: The working tree had uncommitted changes at the moment HEAD was read.
REVISION_DIRTY_TEXT = "uncommitted changes"


def revision_label(revision: Any) -> str:
    """A check's git stamp, worded as WHAT IT IS rather than as provenance.

    ``at 8a4e024`` reads as "this is the revision that ran", and for the
    server-at-record basis that is false: HEAD is read when the MCP call
    arrives, so an agent that records a check before committing stamps the
    commit BEFORE its own work. The basis therefore leads the label:

    * ``host_hook`` → ``ran at 8a4e024 · main · uncommitted changes`` (the hook
      read HEAD in the same process as the check, so this IS what ran);
    * ``server_captured_at_record`` → ``HEAD when recorded: 8a4e024 · main ·
      uncommitted changes`` (a timestamped fact, not a provenance claim);
    * anything else → ``revision basis unknown: 8a4e024 · …``.
    """

    revision = revision if isinstance(revision, Mapping) else {}
    commit = str(revision.get("commit") or "").strip()
    if not commit:
        return REVISION_NOT_CAPTURED
    parts = [commit[:7]]
    branch = str(revision.get("branch") or "").strip()
    if branch:
        parts.append(branch)
    if revision.get("dirty") is True:
        parts.append(REVISION_DIRTY_TEXT)
    body = " · ".join(parts)
    basis = str(revision.get("basis") or "").strip()
    if basis == REVISION_BASIS_HOOK:
        return f"ran at {body}"
    if basis == REVISION_BASIS_SERVER_AT_RECORD:
        return f"HEAD when recorded: {body}"
    return f"revision basis unknown: {body}"


def revision_contradiction_text(commit: Any, paths: Any) -> str | None:
    """The named contradiction when a check declares files that do NOT exist at
    the revision it was stamped with — proof the stamp is not the revision the
    check ran against. ``None`` when nothing was contradicted."""

    commit = str(commit or "").strip()
    missing = [str(path).strip() for path in (paths if isinstance(paths, (list, tuple)) else []) if str(path).strip()]
    if not commit or not missing:
        return None
    shown = ", ".join(missing[:3])
    if len(missing) > 3:
        shown += f", and {len(missing) - 3} more"
    return (
        f"The stamped revision {commit[:7]} does not contain {shown}, "
        "so it is not the revision this check ran against."
    )


# ---------------------------------------------------------------------------
# gaps: what a reviewer cannot do, before what a ledger cannot prove
# ---------------------------------------------------------------------------

#: A gap that stops a reviewer from checking the work itself.
GAP_KIND_BLOCKS_REVIEW = "blocks_review"
#: A gap in agentacct's own bookkeeping about where a fact came from.
GAP_KIND_BOOKKEEPING = "provenance"
GAP_KIND_LABELS: dict[str, str] = {
    GAP_KIND_BLOCKS_REVIEW: "Blocks review",
    GAP_KIND_BOOKKEEPING: "Provenance bookkeeping",
}


def gap_kind_label(kind: Any) -> str:
    """The section heading for one gap rank."""

    return GAP_KIND_LABELS.get(str(kind or "").strip(), GAP_KIND_LABELS[GAP_KIND_BOOKKEEPING])


#: The opening words of the capture-coverage gap. It lives in the Actions
#: dimension but it stops a reviewer from seeing what the agent did, so it ranks
#: with the reviewer-facing gaps rather than with provenance bookkeeping.
GAP_CAPTURE_COVERAGE_PREFIX = "Tool-call capture did not cover this Task"

GAP_NO_COMMIT_RECORDED = (
    "No commit was recorded with this work, so it cannot be located in the repository."
)
GAP_NO_CHANGE_DESCRIPTION = (
    "No description of the change was recorded, so what was changed is not stated anywhere."
)
GAP_FILE_OPERATIONS_UNORDERED = (
    "File operations were not ordered, so the recorded paths cannot be read as a sequence of edits."
)


def gap_subagents_recorded_no_work(sessions: Any, tokens: Any) -> str | None:
    """``3 supporting sessions spent 2,396,651 tokens and recorded no work.``
    ``None`` unless supporting sessions really did spend tokens and record
    nothing."""

    count = sessions if isinstance(sessions, int) and not isinstance(sessions, bool) else 0
    spent = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else 0
    if count <= 0 or spent <= 0:
        return None
    return (
        f"{_count_words(count, 'supporting session')} spent {spent:,} tokens and recorded no work, "
        "so what they did is unreviewable."
    )


def gap_declared_paths_unobserved(declared: Any) -> str | None:
    """``Checks declared 2 file paths while the capture observed no file edit.``
    ``None`` when nothing was declared."""

    count = declared if isinstance(declared, int) and not isinstance(declared, bool) else 0
    if count <= 0:
        return None
    return (
        f"Checks declared {_count_words(count, 'file path')} while the capture observed no file edit, "
        "so the declared paths are unconfirmed."
    )


# --- gap ranking -------------------------------------------------------------
#
# A gap's KIND says whether it stops a reviewer at all; its CODE says how much
# it stops them. Without a code the list was ordered by which reducer happened
# to append first, so "file operations were not ordered" outranked "three
# supporting sessions spent 2.4 million tokens and recorded nothing". Rank is
# by WHAT THE GAP PREVENTS: an entire body of work being unreviewable outranks
# a missing ordering over work you can still read.

GAP_CODE_SUBAGENTS_SILENT = "subagents_recorded_no_work"
GAP_CODE_CAPTURE_COVERAGE = "capture_coverage_incomplete"
GAP_CODE_NO_CHANGE_DESCRIPTION = "no_change_description"
GAP_CODE_NO_COMMIT = "no_commit_recorded"
GAP_CODE_DECLARED_PATHS_UNOBSERVED = "declared_paths_unobserved"
GAP_CODE_FILE_OPERATIONS_UNORDERED = "file_operations_unordered"
GAP_CODE_WORK_NOT_TIED_TO_SESSION = "work_not_tied_to_a_session"
#: Free-text gaps a dimension reducer raised. They are real reviewer-facing
#: facts with no code of their own, so they rank in the middle rather than
#: being pushed under every coded gap or above them.
GAP_CODE_DIMENSION = "dimension_gap"

_GAP_RANK: dict[str, int] = {
    # A whole body of work nobody can read at all.
    GAP_CODE_SUBAGENTS_SILENT: 0,
    # What the agent did is only partly observable.
    GAP_CODE_CAPTURE_COVERAGE: 1,
    # Nothing anywhere says what changed.
    GAP_CODE_NO_CHANGE_DESCRIPTION: 2,
    GAP_CODE_DIMENSION: 3,
    # The work is described but cannot be located.
    GAP_CODE_NO_COMMIT: 4,
    # Attribution and corroboration of work you can already read.
    GAP_CODE_WORK_NOT_TIED_TO_SESSION: 5,
    GAP_CODE_DECLARED_PATHS_UNOBSERVED: 6,
    GAP_CODE_FILE_OPERATIONS_UNORDERED: 7,
}
#: Where an unrecognised code sorts: after every ranked gap, before nothing.
GAP_RANK_UNRANKED = max(_GAP_RANK.values()) + 1


def gap_rank(code: Any) -> int:
    """How far forward one gap sorts inside its kind. Lower is more urgent."""

    return _GAP_RANK.get(str(code or "").strip(), GAP_RANK_UNRANKED)


def hidden_in_subagents_text(hidden: Any, silent_sessions: Any, silent_tokens: Any) -> str:
    """Why ``hidden_in_subagents`` reads the number it reads.

    The count is steps RECORDED by a non-root session. A subagent that recorded
    nothing therefore drives it toward zero — the reading most likely to be
    taken as "nothing is hidden" is produced by the case where everything is.
    That contradiction is named here rather than left for a reader to notice.
    """

    steps = hidden if isinstance(hidden, int) and not isinstance(hidden, bool) else 0
    sessions = silent_sessions if isinstance(silent_sessions, int) and not isinstance(silent_sessions, bool) else 0
    tokens = silent_tokens if isinstance(silent_tokens, int) and not isinstance(silent_tokens, bool) else 0
    unreviewable = sessions > 0 and tokens > 0
    if steps > 0:
        text = f"{_count_words(steps, 'step')} ran in a supporting session."
        if unreviewable:
            text += (
                f" {_count_words(sessions, 'further supporting session')} spent {tokens:,} tokens and "
                "recorded no step at all, so more work is hidden than this count can show."
            )
        return text
    if unreviewable:
        return (
            f"No step is recorded as running in a supporting session — but {_count_words(sessions, 'supporting session')} "
            f"spent {tokens:,} tokens and recorded nothing, so this zero counts nothing rather than proving "
            "nothing was hidden."
        )
    return "No step ran in a supporting session."


# ---------------------------------------------------------------------------
# timeline events
# ---------------------------------------------------------------------------

TIMELINE_LANE_LABELS: dict[str, str] = {
    "primary": "Primary session",
    "supporting": "Supporting session",
    "evidence": "Check evidence",
    "control": "agentacct control",
}
SUPERSEDED_SUFFIX = " · superseded"
STEP_STATUS_NOT_RECORDED = "No status recorded"


def timeline_lane_label(key: Any) -> str:
    text = str(key or "").strip()
    return TIMELINE_LANE_LABELS.get(text, sentence_case(text.replace("_", " ")) or "Task evidence")


def step_status_label(status: Any) -> str:
    """A work step's recorded status, qualified as the agent's report:
    ``Reported completed`` / ``Reported handed off``. Never a bare
    ``Completed`` that could read as verified."""

    text = str(status or "").strip()
    if not text or text == "recorded":
        return STEP_STATUS_NOT_RECORDED
    return f"Reported {work_status_label(text).lower()}"


def check_event_status_label(result: Any, *, superseded: bool = False) -> str:
    """A check event's result words, with the named superseded state."""

    return check_result_label(result) + (SUPERSEDED_SUFFIX if superseded else "")


# ---------------------------------------------------------------------------
# progress beats — the narration the contract asks for
# ---------------------------------------------------------------------------
#
# The recording contract tells every agent to send `section_status=checkpoint`
# updates "rather than one giant section". The ledger keeps one record per
# section and the last summary wins, so the prose those updates carry was
# stored and then made unreachable: the most informative sentence in a Task
# could be in the store and in neither the receipt nor the timeline. A beat is
# one of those non-terminal summaries, restored as its own ordered record under
# the section that wrote it. The section's terminal summary stays its headline.

#: A beat whose prose reduces to nothing printable (never a substitute name for
#: prose that exists — it stands in only for a note with no usable label).
TIMELINE_BEAT_TITLE = "Progress note"
TIMELINE_BEAT_TIME_NOTE = "Recorded progress note; duration unavailable"
#: What a beat IS, for a surface that needs to say why the row is not a step.
TIMELINE_BEAT_DEFINITION = (
    "A progress note the agent recorded while the section was still open. It is narration, not a "
    "separate step: beats are never counted as steps, checks or coverage."
)


def timeline_beat_title(label: Any) -> str:
    """One beat's card title: the reduced prose, else the named beat noun."""

    return str(label or "").strip() or TIMELINE_BEAT_TITLE


# ---------------------------------------------------------------------------
# the viewing window — when there is nothing to narrow
# ---------------------------------------------------------------------------
#
# The time canvas's window has a FLOOR, and the floor is the axis's own finest
# tick step: the axis can label no interval shorter than one second, so a
# window below a few seconds is an unlabelled blank rather than a closer look.
# A Task whose WHOLE recorded span already sits at or under that floor
# therefore has no narrower view to move to — and the surfaces that offered
# one (the overview's two drag handles, its adjustable increment/decrement, its
# keyboard stop, its resize cursors, the canvas's own pinch / Option-scroll /
# plus-minus) were controls that could not act. This is the NAMED STATE that
# stands in their place.
#
# ``{span}`` is the recorded span, which only the rendering surface can
# measure; the words are all here. That is the same division of labour the
# freshness stamps use — Python owns the phrase, the surface owns the number.
TIMELINE_WINDOW_NOT_NARROWABLE = "Whole recorded span: {span} — nothing to narrow"
#: Why the control is gone, for the help text and the screen reader. It states
#: the axis limit rather than blaming the Task: a 0.3-second Task is not an
#: error, it is simply already shown whole.
TIMELINE_WINDOW_NOT_NARROWABLE_DETAIL = (
    "The whole recorded span is already in view, and it is shorter than the smallest window the "
    "time axis can label, so there is no narrower view to move to."
)


#: How to drive the canvas, in the order a reader needs it: the gesture that
#: changes the WINDOW first, then the one that MOVES through time.
#:
#: This exists because plain scroll deliberately passes through to the page --
#: the canvas must not hijack a reader's scrolling -- and a gesture that does
#: nothing is indistinguishable from a broken one. Google Maps solves the same
#: problem the same way ("Use ctrl + scroll to zoom the map"): say it at the
#: moment the plain gesture fails, not only in documentation nobody opens.
#:
#: Modifiers are named with their macOS glyphs because that is what is printed
#: on the reader's keyboard. Shift is named for the pan because a mouse has no
#: two-finger horizontal swipe, and without it a mouse user cannot pan at all.
TIMELINE_GESTURE_HINT = "⌥ scroll to zoom · ⇧ scroll or drag to move"
#: The same contract in full words, for the help popover and the screen reader,
#: where glyphs read poorly and there is room to say why plain scroll is free.
TIMELINE_GESTURE_HINT_DETAIL = (
    "Hold Option and scroll to change how much time is in view. Hold Shift and scroll, swipe "
    "sideways, or drag the canvas to move through time. Plain scrolling is left to the page, so "
    "the timeline never takes over your scrolling."
)


def timeline_span_text(seconds: Any) -> str:
    """A recorded span in words, down to hundredths of a second.

    The canvas's not-narrowable state reports spans BELOW the window floor —
    fractions of a second, where :func:`humanize_seconds` can only say
    ``<1m`` and a whole-seconds rounding would print the named-absence-breaking
    ``0s``. Hundredths below a second, tenths below ten, whole seconds below a
    minute, and :func:`humanize_seconds` above that, so a long span still reads
    in the words every other duration uses.

    ``window not recorded`` for a span that is missing, non-finite or not
    positive: an unmeasurable extent is a NAMED absence, never ``0s``.

    The app mirrors this rule in ``WorkTimeWindowScroller.spanText``
    (Swift) because the number can only be measured at render time; the table
    in ``tests/test_display_vocabulary.py`` and the one in
    ``WorkTimeCanvasInputTests.testSpanTextMirrorsThePythonSpanWords`` are the
    same table and must stay so.
    """

    value = _finite(seconds)
    if value is None or value <= 0:
        return TIME_SPAN_NOT_RECORDED
    if value < 1:
        return f"{value:.2f}s"
    if value < 10:
        return f"{value:.1f}s"
    if value < 60:
        # Half-away-from-zero, matching Swift's `rounded()`; Python's own
        # `round` is half-to-even and would disagree at exactly .5.
        return f"{int(value + 0.5)}s"
    return humanize_seconds(value)


def timeline_window_not_narrowable_text(seconds: Any) -> str:
    """The canvas's named state when no narrower window exists: the recorded
    span plus what a reader can do about it (nothing)."""

    return TIMELINE_WINDOW_NOT_NARROWABLE.format(span=timeline_span_text(seconds))


# ---------------------------------------------------------------------------
# timeline salience — which records a reviewer should be pulled toward
# ---------------------------------------------------------------------------
#
# Salience used to be `bool(blocker)` for work and a constant True for every
# check and control record. Across the installed ledger that is 5 salient
# sections in 1,309 and every single check loud, so a canvas of 48 records was
# 48 equally loud rows and the mark carried no information. These keys are
# derived from facts that VARY between records and that a reviewer would
# actually want pulled forward. Salience is a PAYLOAD FACT with a reason
# sentence: no surface re-derives it, and no surface has to guess why a row is
# marked.

SALIENCE_BLOCKER = "blocker_reported"
SALIENCE_OWNS_FAILED_CHECK = "owns_failed_check"
SALIENCE_COMPLETED_UNCHECKED = "completed_unchecked"
SALIENCE_LEFT_IN_PROGRESS = "left_in_progress"
SALIENCE_LARGEST_FILE_SET = "largest_file_set"
SALIENCE_CURRENT_FAILURE = "current_failure"
SALIENCE_CHECK_COULD_NOT_RUN = "check_could_not_run"
SALIENCE_RECOVERY_RUN = "recovery_run"
SALIENCE_CONTROL_RECORD = "control_record"

TIMELINE_SALIENCE_REASONS: dict[str, str] = {
    SALIENCE_BLOCKER: "The agent recorded a blocker on this section.",
    SALIENCE_OWNS_FAILED_CHECK: "A check recorded against this section did not pass.",
    SALIENCE_CURRENT_FAILURE: "A recorded failure that no later run has replaced.",
    SALIENCE_COMPLETED_UNCHECKED: "Reported complete with no check behind it.",
    SALIENCE_CHECK_COULD_NOT_RUN: "This check could not run, so it proves nothing either way.",
    SALIENCE_LEFT_IN_PROGRESS: "Left open: no terminal status was ever recorded for this section.",
    SALIENCE_RECOVERY_RUN: "This run replaces an earlier failed run of the same check.",
    SALIENCE_LARGEST_FILE_SET: "This section touched the largest recorded file set in the Task.",
    SALIENCE_CONTROL_RECORD: "agentacct ran this itself, so the record is owned rather than reported.",
}

#: Strongest first. A record can satisfy several; it is marked for the one a
#: reviewer would open it for.
_SALIENCE_RANK: tuple[str, ...] = (
    SALIENCE_BLOCKER,
    SALIENCE_CURRENT_FAILURE,
    SALIENCE_OWNS_FAILED_CHECK,
    SALIENCE_LEFT_IN_PROGRESS,
    SALIENCE_COMPLETED_UNCHECKED,
    SALIENCE_CHECK_COULD_NOT_RUN,
    SALIENCE_RECOVERY_RUN,
    SALIENCE_CONTROL_RECORD,
    SALIENCE_LARGEST_FILE_SET,
)


def timeline_salience_reason(key: Any) -> str | None:
    """The sentence for one salience key, or ``None`` for an unknown key."""

    return TIMELINE_SALIENCE_REASONS.get(str(key or "").strip())


def timeline_salience(keys: Any) -> dict[str, Any]:
    """``{salience, salience_reason, salience_keys, important}`` for one record.

    ``important`` stays in the payload for the readers that already switch on
    it, but it is now a DERIVED restatement of "this record has a salience
    key", never an independent flag a surface can disagree with.
    """

    supplied = [str(key or "").strip() for key in (keys if isinstance(keys, (list, tuple, set)) else ())]
    present = [key for key in _SALIENCE_RANK if key in supplied]
    strongest = present[0] if present else None
    return {
        "salience": strongest,
        "salience_reason": timeline_salience_reason(strongest),
        "salience_keys": present,
        "important": bool(present),
    }


# ---------------------------------------------------------------------------
# decision definitions, groups and the legend
# ---------------------------------------------------------------------------

# One short definition per decision key: the legend prints it, and the
# receipt / task-intelligence statements are built from it.
DECISION_DEFINITIONS: dict[str, str] = {
    "blocked": "The agent recorded a blocker for this Task.",
    "failed": "A step was recorded as failed.",
    "finding": "A recorded check found an issue in the work being reviewed.",
    "in_progress": "This Task is still in progress.",
    "verified": "Recorded machine evidence verifies the latest outcome.",
    "reported": "The agent reported completing work; no check verifies the completion claim itself.",
    "resolved": "A later passed check reports the exact blocker resolved; this is not a verified completion.",
    "mostly_done": "Recorded steps completed while one or more were left open; not a claim the Task is finished.",
    # Worded only as recorded: the agent's handed_off status is all agentacct
    # holds. No pickup is ever recorded by the handoff itself, so the absence is
    # named rather than claimed ("continued elsewhere" asserted a pickup).
    # The "not a completed or verified outcome" clause stays: a handoff must never
    # read as a completion (tests/test_task_outcome.py pins it).
    "handed_off": (
        "The agent handed this work off; a deliberate stop, not a completed or verified outcome. "
        "Picked up by: not recorded."
    ),
    "blocker_resolved_by_user": (
        "You marked the recorded blocker resolved; not a completion claim and not machine verification."
    ),
    "finding_resolved_by_user": (
        "You marked the recorded finding resolved; the failing check stays in history — "
        "this is not machine verification."
    ),
    "finding_superseded": SUPERSEDED_CHECK_DEFINITION,
    "ended_open": (
        "The session ended with a step still open; agentacct inferred the stop — "
        "not a recorded completion or a deliberate handoff."
    ),
    "inactive": (
        "Open steps, nothing recorded as finished, and work has since continued elsewhere — "
        "agentacct inferred it went quiet. Not a completion, and not a stated stop."
    ),
    "observed": "agentacct saw this Task but no work steps were recorded.",
    "unknown": "agentacct observed this Task but no outcome was recorded.",
}

# The review queue's one noun: the Work tab, the dashboard count and link, and
# every disposition effect name it the same way.
ATTENTION_QUEUE_NOUN = "Attention"

# Filter group key → (label, definition). Rows always wear their own decision
# word; a group is a filter only.
GROUP_DEFINITIONS: dict[str, tuple[str, str]] = {
    "attention": (
        ATTENTION_QUEUE_NOUN,
        "Tasks with an open failed check, failed step, blocker, or check that could not run that you have not reviewed.",
    ),
    "verified": ("Verified", DECISION_DEFINITIONS["verified"]),
    "reported": ("Reported", "The agent or you settled the work; no check verifies the completion itself."),
    "in_progress": ("In progress", DECISION_DEFINITIONS["in_progress"]),
    "observed": (DECISION_LABELS["observed"], DECISION_DEFINITIONS["observed"]),
    "stopped": ("Stopped", "Work that stopped without finishing: handed off, ended open, or inactive."),
    "other": ("Other", "Every Task outside the groups above, including reviewed items no longer in Attention."),
}

# Decision key → its filter group when attention is not open.
DECISION_GROUPS: dict[str, str] = {
    "finding": "attention",
    "failed": "attention",
    "blocked": "attention",
    "verified": "verified",
    "reported": "reported",
    "resolved": "reported",
    "mostly_done": "reported",
    "finding_superseded": "reported",
    "finding_resolved_by_user": "reported",
    "blocker_resolved_by_user": "reported",
    "in_progress": "in_progress",
    "started": "in_progress",
    "checkpoint": "in_progress",
    "observed": "observed",
    "handed_off": "stopped",
    "ended_open": "stopped",
    "inactive": "stopped",
}

# The legend's decision words, grouped by family (needs you, live, proven,
# claimed, inferred, ambient).
DECISION_LEGEND_ORDER: tuple[str, ...] = (
    "blocked",
    "failed",
    "finding",
    "in_progress",
    "verified",
    "reported",
    "resolved",
    "mostly_done",
    "handed_off",
    "blocker_resolved_by_user",
    "finding_resolved_by_user",
    "finding_superseded",
    "ended_open",
    "inactive",
    "observed",
)


def decision_group(key: Any, attention_open: Any = None) -> str:
    """The one filter group for a Task: ``attention`` exactly when the
    reducer's attention predicate is open; a danger word whose attention is no
    longer open leaves it for ``other``; otherwise the decision word's group."""

    group = DECISION_GROUPS.get(str(key or "").strip(), "other")
    if attention_open is True:
        return "attention"
    if attention_open is False and group == "attention":
        return "other"
    return group


def decision_legend() -> dict[str, Any]:
    """The status legend every surface renders: each decision word with its
    group and definition, and each group's definition."""

    return {
        "decisions": [
            {
                "key": key,
                "label": decision_label(key),
                "definition": DECISION_DEFINITIONS[key],
                "group_key": DECISION_GROUPS.get(key, "other"),
            }
            for key in DECISION_LEGEND_ORDER
        ],
        "groups": [
            {"key": key, "label": label, "definition": definition}
            for key, (label, definition) in GROUP_DEFINITIONS.items()
        ],
    }


# The attention sort rule, stated once (it mirrors the reducer's order classes).
ATTENTION_SORT_TEXT = (
    "failed checks and steps, then blockers, then checks that could not run, then most recent"
)
# The handoff lifecycle marker printed beside a different decision word.
LIFECYCLE_MARKER_TEXT = DECISION_LABELS["handed_off"]
# The handoff marker when a Task's handoff is not its frontier (it was resumed).
HANDOFF_NOT_FRONTIER_TEXT = "Not the current handoff frontier"
HANDOFF_STATEMENT_ABSENT_TEXT = "No handoff statement supplied"


def handoff_marker_line(handed_off: bool, statement: Any) -> str:
    """``Handed off · <statement>`` -- the one-line handoff marker exports print."""
    word = LIFECYCLE_MARKER_TEXT if handed_off else HANDOFF_NOT_FRONTIER_TEXT
    text = statement if isinstance(statement, str) and statement.strip() else HANDOFF_STATEMENT_ABSENT_TEXT
    return f"{word} · {text}"


def attention_count_text(total: Any) -> str:
    """``4 in Attention`` — the queue count every surface prints."""

    count = total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else 0
    return f"{count} in {ATTENTION_QUEUE_NOUN}"


ATTENTION_OPEN_ACTION = f"Open {ATTENTION_QUEUE_NOUN}"


def attention_queue_copy(total: Any) -> dict[str, str]:
    """The queue's words for one count: its noun, the count phrase, the link
    that opens it, and the attention sort rule."""

    return {
        "noun": ATTENTION_QUEUE_NOUN,
        "count_text": attention_count_text(total),
        "open_action": ATTENTION_OPEN_ACTION,
        "sort_text": ATTENTION_SORT_TEXT,
    }

# (attention kind, disposition action) → the effect of that action, stated
# before the user takes it: its queue consequence and what the badge becomes.
DISPOSITION_EFFECTS: dict[tuple[str, str], str] = {
    ("finding", "reviewed"): f"Leaves {ATTENTION_QUEUE_NOUN}; the badge stays Finding until resolved.",
    ("finding", "resolved"): (
        f"Leaves {ATTENTION_QUEUE_NOUN} and records your resolution; the badge becomes "
        f"{DECISION_LABELS['finding_resolved_by_user']}. The failing check stays in history."
    ),
    ("finding", "reopen"): f"Returns to {ATTENTION_QUEUE_NOUN} with its original badge.",
    ("blocked", "reviewed"): f"Leaves {ATTENTION_QUEUE_NOUN}; the badge stays Blocked until resolved.",
    ("blocked", "resolved"): (
        f"Leaves {ATTENTION_QUEUE_NOUN} and records your resolution; the badge becomes "
        f"{DECISION_LABELS['blocker_resolved_by_user']}."
    ),
    ("blocked", "reopen"): f"Returns to {ATTENTION_QUEUE_NOUN} with its original badge.",
    ("check_not_run", "reviewed"): (
        f"Leaves {ATTENTION_QUEUE_NOUN}; the check stays {CHECK_RESULT_LABELS['error']} until it runs or you resolve it."
    ),
    ("check_not_run", "resolved"): (
        f"Leaves {ATTENTION_QUEUE_NOUN} and records your resolution; nothing was checked, so the work stays unproven."
    ),
    ("check_not_run", "reopen"): f"Returns to {ATTENTION_QUEUE_NOUN} with its original badge.",
}

DISPOSITION_ACTIONS: tuple[str, ...] = ("reviewed", "resolved", "reopen")


def disposition_effects(kind: Any) -> dict[str, str | None]:
    """``{reviewed, resolved, reopen}`` effect sentences for one attention kind
    (``None`` for an action that kind does not offer)."""

    text = str(kind or "")
    return {action: DISPOSITION_EFFECTS.get((text, action)) for action in DISPOSITION_ACTIONS}


def window_label_for(kind: Any, window_minutes: Any = None) -> str:
    """A limit window's display name: ``5-hour limit`` / ``7-day limit`` for the
    canonical kinds, ``<N>m limit`` for another reported length, else ``limit
    window``."""

    text = str(kind or "")
    if text in WINDOW_LABELS:
        return WINDOW_LABELS[text]
    minutes = _finite(window_minutes)
    if minutes is not None:
        return f"{int(minutes)}m limit"
    return "limit window"


__all__ = [
    "ACTIONS_CAPTURE_PARTIAL_QUALIFIER",
    "COMMAND_AGENT_RECORDED_TEXT",
    "COMMAND_STATE_AGENT_RECORDED",
    "COMMAND_STATE_DIGEST_ONLY",
    "GAP_CAPTURE_COVERAGE_PREFIX",
    "GAP_CODE_CAPTURE_COVERAGE",
    "GAP_CODE_DECLARED_PATHS_UNOBSERVED",
    "GAP_CODE_DIMENSION",
    "GAP_CODE_FILE_OPERATIONS_UNORDERED",
    "GAP_CODE_NO_CHANGE_DESCRIPTION",
    "GAP_CODE_NO_COMMIT",
    "GAP_CODE_SUBAGENTS_SILENT",
    "GAP_CODE_WORK_NOT_TIED_TO_SESSION",
    "GAP_FILE_OPERATIONS_UNORDERED",
    "GAP_KIND_BLOCKS_REVIEW",
    "GAP_KIND_BOOKKEEPING",
    "GAP_KIND_LABELS",
    "GAP_NO_CHANGE_DESCRIPTION",
    "GAP_NO_COMMIT_RECORDED",
    "GAP_RANK_UNRANKED",
    "gap_rank",
    "hidden_in_subagents_text",
    "REVISION_BASIS_HOOK",
    "REVISION_BASIS_SERVER_AT_RECORD",
    "REVISION_DIRTY_TEXT",
    "REVISION_NOT_CAPTURED",
    "TIME_SPAN_NOT_RECORDED",
    "actions_capture_shortfall",
    "command_state_text",
    "display_time_span",
    "gap_declared_paths_unobserved",
    "gap_kind_label",
    "gap_subagents_recorded_no_work",
    "revision_contradiction_text",
    "revision_label",
    "BY_MODEL_SESSIONS_FOOTNOTE",
    "CAPACITY_CHECKED_LABEL",
    "COST_CHART_LEGEND",
    "COST_CHART_UNIT",
    "COST_HELD_TEXT",
    "FRESHNESS_JUST_NOW_SECONDS",
    "FRESHNESS_JUST_NOW_TEXT",
    "FRESHNESS_SEPARATOR",
    "LIMIT_PERCENT_NOT_REPORTED",
    "PLAN_SHARE_NOT_REPORTED",
    "PLAN_SHARE_STATES",
    "PERIOD_LABEL_UNDATED",
    "PERIOD_LABEL_WEEK_PREFIX",
    "PLAN_SHARE_TOKENS_SUFFIX",
    "RECORDED_USAGE_REFRESHED_LABEL",
    "RECORDED_USAGE_TITLE",
    "USAGE_RECORD_NOUN",
    "USAGE_SERIES",
    "USAGE_SERIES_DEFAULT_RULE",
    "WORK_STATUS_LABELS",
    "cost_total_label",
    "cost_unpriced_text",
    "data_age_text",
    "limit_used_text",
    "limit_value_text",
    "plan_share_fields",
    "plan_share_pct_text",
    "plan_share_state_text",
    "relative_age_text",
    "recorded_usage_sessions_text",
    "usage_records_text",
    "work_status_label",
    "ACTIONS_CAPTURE_BOUNDARY",
    "ACTIONS_CAPTURE_UNKNOWN",
    "ACTIONS_NOT_INSTRUMENTED",
    "ACTIONS_NO_TOOL_CALLS",
    "ARTIFACT_PATH_NOT_SHOWN_TEXT",
    "ARTIFACT_URL_NOT_SHOWN_TEXT",
    "ATTENTION_OPEN_ACTION",
    "ATTENTION_QUEUE_NOUN",
    "ATTENTION_SORT_TEXT",
    "COMMAND_NOT_SHOWN_TEXT",
    "DECISION_DEFINITIONS",
    "DECISION_GROUPS",
    "DECISION_LEGEND_ORDER",
    "DISPOSITION_ACTIONS",
    "GROUP_DEFINITIONS",
    "LIFECYCLE_MARKER_TEXT",
    "HANDOFF_NOT_FRONTIER_TEXT",
    "HANDOFF_STATEMENT_ABSENT_TEXT",
    "handoff_marker_line",
    "RELATED_PATHS_DEFINITION",
    "RELATED_PATHS_NONE",
    "SALIENCE_BLOCKER",
    "SALIENCE_CHECK_COULD_NOT_RUN",
    "SALIENCE_COMPLETED_UNCHECKED",
    "SALIENCE_CONTROL_RECORD",
    "SALIENCE_CURRENT_FAILURE",
    "SALIENCE_LARGEST_FILE_SET",
    "SALIENCE_LEFT_IN_PROGRESS",
    "SALIENCE_OWNS_FAILED_CHECK",
    "SALIENCE_RECOVERY_RUN",
    "STEP_STATUS_NOT_RECORDED",
    "SUPERSEDED_SUFFIX",
    "TIMELINE_BEAT_DEFINITION",
    "TIMELINE_BEAT_TIME_NOTE",
    "TIMELINE_BEAT_TITLE",
    "TIMELINE_LANE_LABELS",
    "TIMELINE_SALIENCE_REASONS",
    "TIMELINE_GESTURE_HINT",
    "TIMELINE_GESTURE_HINT_DETAIL",
    "TIMELINE_WINDOW_NOT_NARROWABLE",
    "TIMELINE_WINDOW_NOT_NARROWABLE_DETAIL",
    "timeline_beat_title",
    "timeline_salience",
    "timeline_salience_reason",
    "timeline_span_text",
    "timeline_window_not_narrowable_text",
    "TOOL_CATEGORY_LABELS",
    "actions_synopsis",
    "attention_count_text",
    "attention_queue_copy",
    "check_event_status_label",
    "decision_group",
    "decision_legend",
    "receipt_field_label",
    "related_paths_text",
    "step_status_label",
    "timeline_lane_label",
    "ASSERTED_BY_LABELS",
    "ASSERTED_BY_PHRASES",
    "GAP_LABEL_FALLBACK",
    "GAP_LABEL_NOT_YET_PROVEN",
    "NOT_CHECK_RELEVANT_DEFINITION",
    "NOT_CHECK_RELEVANT_WORDS",
    "NOT_GRADEABLE_NO_CHECKABLE_STEPS",
    "NOT_GRADEABLE_NO_FINISHED_STEPS",
    "NOT_GRADEABLE_NO_STEPS",
    "NOT_GRADEABLE_TEXT",
    "STILL_OPEN_WORDS",
    "STOP_LABELS",
    "UNCHECKED_STEP_WORDS",
    "UNLINKED_CHECK_WORDS",
    "asserted_by_label",
    "asserted_by_phrase",
    "sentence_case",
    "step_not_graded_reason",
    "ATTENTION_REASON_LABELS",
    "CHECK_NOT_RUN_WORDS",
    "CHECK_RESULT_LABELS",
    "CHECK_RESULT_TONES",
    "FAILED_CHECK_RESULTS",
    "NOT_RUN_CHECK_RESULTS",
    "check_result_key",
    "check_result_label",
    "check_result_note",
    "check_result_tone",
    "more_attention_text",
    "COST_ABSENT_NO_USAGE",
    "COST_ABSENT_UNPRICED",
    "COST_BASIS_LABELS",
    "COST_BASIS_NOT_REPORTED",
    "COST_LEGEND",
    "COST_PREFIX_ESTIMATE",
    "COST_PREFIX_PARTIAL",
    "COST_PREFIX_REPORTED",
    "DECISION_LABELS",
    "DISPOSITION_EFFECTS",
    "EVIDENCE_GRADE_LABELS",
    "EVIDENCE_GRADE_NOT_CHECK_RELEVANT",
    "EVIDENCE_GRADE_NOT_GRADED",
    "CHECK_SUMMARY_PREVIEW_BUDGET",
    "META_SEPARATOR",
    "NEXT_STEP_ABSENT",
    "NOT_CAPTURED_INLINE_MAX",
    "NOT_CAPTURED_NOUNS",
    "NOT_CAPTURED_OVERFLOW",
    "NOT_CAPTURED_PREFIX",
    "NOT_CAPTURED_SUBSUMED_BY",
    "TASK_GOAL_ABSENT",
    "check_meta_line",
    "check_summary_preview",
    "checks_heading_line",
    "collapse_not_captured_keys",
    "not_captured_line",
    "split_sentences",
    "ORIGIN_LABELS",
    "RECORD_SECTION_LABEL_KEYS",
    "RECEIPT_FIELD_LABELS",
    "TASK_LIST_FIELD_LABELS",
    "REPORTED_COST_CONFIDENCES",
    "RESET_NOT_REPORTED",
    "SOURCE_LABELS",
    "SUPERSEDED_CHECK_DEFINITION",
    "TIER_LABELS",
    "TIER_TABLE",
    "WINDOW_LABELS",
    "WINDOW_SHORT_LABELS",
    "cost_basis_label",
    "cost_confidence_display",
    "cost_display",
    "decision_label",
    "display_clock",
    "display_date",
    "period_label",
    "disposition_effects",
    "evidence_grade_label",
    "format_dollars",
    "humanize_seconds",
    "percent_share",
    "reset_text",
    "source_label",
    "source_tier_key",
    "window_label_for",
]
