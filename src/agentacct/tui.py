"""The ``agentacct tui`` live terminal dashboard (Textual).

A thin UI over :mod:`agentacct.usage_snapshot`: it polls the authoritative local
event log and renders the same numbers ``agentacct now`` / ``agentacct limits``
print — calendar-window usage & cost, a by-client / top-model breakdown, and
provider rate-limit bars with a live reset countdown. No credentials, no API
calls; the data path is identical to the CLI so the surfaces can never disagree.

Refresh model (two timers):

* every ``refresh_seconds`` — re-read the event log and, only when the event
  count changed (the log is append-only, so count is a sound change key) or a
  refresh was forced, rebuild the snapshot and repaint the tables. An unchanged
  tick is cheap: no cube rebuild.
* every second — recompute just the reset countdowns and the "as of" age from the
  cached snapshot, so those tick smoothly without re-scanning the log.

The whole thing is headless-testable with Textual's ``App.run_test()`` — see
``tests/test_tui.py``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from rich.markup import escape as _escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Collapsible, DataTable, Footer, Header, LoadingIndicator, Static

from .service import SentinelService
from .subagent_roles import read_roles_for_children
from .usage_snapshot import (
    ClientLimit,
    LiveSnapshot,
    UsagePage,
    build_client_limits,
    build_live_snapshot,
    build_usage_page,
    cost_text,
    finite,
    format_tokens,
    humanize_seconds,
    limit_is_stale,
    ratio_bar,
    usage_bar,
)

# The window tokens the `w` key cycles through (same vocabulary as `now --window`).
_WINDOW_CYCLE: tuple[str, ...] = ("today", "7d", "30d", "all")

# The TUI freshens the store from the client session files on launch/refresh (like
# the HTML dashboard's Refresh) so the in-flight session shows real usage. Pinned
# off in tests so they never scan the developer's real ~/.claude / ~/.codex.
_AUTO_IMPORT_ENV = "AGENTACCT_TUI_AUTO_IMPORT"


def _auto_import_enabled() -> bool:
    value = os.environ.get(_AUTO_IMPORT_ENV)
    if value is None:
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")

# The sessions drill-down shows the most-recent slice; the full count is always
# reported so the cap is never silent.
_SESSIONS_LIMIT = 500


def _work_status_color(status: str) -> str:
    """Green (done) / yellow (in-flight) / red (blocked) / dim (unknown)."""

    if status in ("completed", "resolved", "handed_off"):
        return "green"
    if status in ("started", "checkpoint"):
        return "yellow"
    if status == "blocked":
        return "red"
    return "dim"


def _evidence_mark(result: str) -> tuple[str, str]:
    """(glyph, color) for a machine-check result."""

    if result == "passed":
        return "✓", "green"
    if result in ("failed", "error"):
        return "✗", "red"
    if result == "skipped":
        return "»", "yellow"
    return "•", "dim"


def _events_fingerprint(events: list) -> int:
    """A content-sensitive change key for the loaded event list.

    The event COUNT is NOT a sound change key: the usage importer supersedes rows
    IN PLACE (``service.replace_events`` drops a re-observed row and records a
    fresh one), so a growing single-model session keeps the count fixed while its
    tokens/cost change — the whole point of a live view. Hashing each event's
    identity + observation time makes any append, removal, or in-place supersede
    (which records a new event id) change the key and trigger a rebuild.

    The values are stringified so the key is TOTAL: a corrupted/hand-injected
    ledger row can round-trip ``event_id`` / ``created_at`` as a JSON list or
    object (unhashable), and this function runs on the unguarded refresh path —
    it must never raise, per refresh_data's fail-soft contract.
    """

    return hash(tuple((str(event.get("event_id")), str(event.get("created_at"))) for event in events))


def _limit_color(used_percent: float) -> str:
    """Green / amber / red by utilization — the at-a-glance headroom signal."""

    if used_percent < 70:
        return "green"
    if used_percent < 90:
        return "yellow"
    return "red"


def _format_limit_lines(limits: list[ClientLimit], now: float) -> tuple[str, int]:
    """(Rich-markup text, hidden-stale count) for a set of provider-limit readings.

    Shared by the home Provider-limits panel and the usage screen so they render
    identically. Stale streams (a signed-out / cancelled account frozen older than
    its longest window) are dropped so the panel isn't cluttered with a meaningless
    frozen bar — the count is returned so the hiding is disclosed, never silent.
    Every provider-derived value is escaped: a stray ``[/]`` in e.g. ``plan_type``
    would otherwise raise a MarkupError and take down the whole live view.
    """

    fresh = [limit for limit in limits if not limit_is_stale(limit, now)]
    hidden = len(limits) - len(fresh)
    lines: list[str] = []
    for limit in fresh:
        header = f"[bold]{_escape(limit.client)}[/]"
        if limit.origin_label:
            header += f" [dim]({_escape(limit.origin_label)})[/]"
        if limit.plan_type:
            header += f"  plan: {_escape(str(limit.plan_type))}"
        if limit.org:
            header += f"  org: {_escape(str(limit.org)[:8])}"
        if limit.captured_at is not None:
            header += f"  [dim](as of {humanize_seconds(now - limit.captured_at)} ago)[/]"
        lines.append(header)
        for window in limit.windows:
            used = window.used_percent
            bar_value = used if used is not None else 0.0
            color = _limit_color(bar_value)
            bar = f"[{color}]{usage_bar(bar_value)}[/]"
            pct = f"{used:5.1f}%" if used is not None else "   — "
            line = f"  {window.label:>7}  {bar}  {pct}"
            resets_at = window.resets_at
            if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool) and resets_at > 0:
                delta = resets_at - now
                line += f"  · resets in {humanize_seconds(delta)}" if delta > 0 else "  · resets now"
            lines.append(line)
        if isinstance(limit.credits, dict) and limit.credits.get("has_credits"):
            lines.append(f"  credits: {_escape(str(limit.credits.get('balance')))}")
        if limit.reached_type:
            lines.append(f"  [red]⚠ limit reached: {_escape(str(limit.reached_type))}[/]")
        lines.append("")
    return "\n".join(lines).rstrip("\n"), hidden


def _limits_panel_text(limits: list[ClientLimit], now: float) -> str:
    """The full Provider-limits panel text (fresh readings + a disclosed stale
    count), or an explicit empty/all-stale note. One implementation for the home
    panel and the usage screen."""

    if not limits:
        return (
            "No provider limit data recorded yet — use your agents, or run "
            "`agentacct usage watch`."
        )
    text, hidden = _format_limit_lines(limits, now)
    stale_note = (
        f"[dim]{hidden} stale reading(s) hidden (e.g. a signed-out account).[/]"
        if hidden
        else ""
    )
    if not text:  # everything was stale
        return f"[dim]No active provider limit readings.[/]  {stale_note}".rstrip()
    return f"{text}\n\n{stale_note}" if stale_note else text


def _session_status(entry: dict) -> tuple[str, str, str]:
    """(glyph, label, color) for a session's overall state.

    Derived from the session's work items' ``latest_status`` — NOT
    ``work['counts']``, because that bucket folds ``handed_off`` (and
    ``started`` / ``checkpoint``) into ``active`` (see work_ledger
    ``_session_work_summary``), so a cleanly handed-off session would otherwise
    misread as still in progress.

    Precedence: blocked > handed-off > in-progress > done. A hand-off is a
    *session-level* clean stop (the agent handed the work to another session), so
    it deliberately outranks a still-open step: a real session commonly leaves one
    stray section ``started`` that was never closed, and that must not make an
    already-handed-off run read as "in progress". (The rollup's work items carry
    no timestamps, so we can't order by "latest step"; this precedence encodes the
    same intent — a hand-off closes the in-progress gap.) A usage-only session
    with no recorded steps shows no badge.
    """

    work = entry.get("work") or {}
    items = work.get("items") or []
    statuses = [str(item.get("latest_status") or "") for item in items if isinstance(item, dict)]
    if any(s == "blocked" for s in statuses):
        return ("⚠", "blocked", "red")
    if any(s == "handed_off" for s in statuses):
        return ("⏸", "handed off", "cyan")
    if any(s in ("started", "checkpoint") for s in statuses):
        return ("▶", "in progress", "yellow")
    if any(s in ("completed", "resolved") for s in statuses):
        return ("✓", "done", "green")
    return ("·", "—", "dim")


def _session_badge(entry: dict) -> str:
    """A short colored status badge cell for a session (safe fixed-vocab markup)."""

    glyph, label, color = _session_status(entry)
    return f"[{color}]{glyph} {label}[/]"


# Clients that have a weekly subscription plan agentacct can estimate against.
_PLAN_CLIENTS = ("claude-code", "codex")


def _plan_pct_cell(entry: dict, pcts: dict) -> str:
    """A compact ``≈X%`` weekly-plan cell for a session row, or ``—``.

    A value appears ONLY for a plan-bearing client (see ``_PLAN_CLIENTS``) whose
    estimate is CALIBRATED from the user's own recorded limit history: the
    pre-computed ``pcts`` map contains only calibrated clients' sessions, so a
    session absent from it — an uncalibrated client, a non-plan client, or a
    zero-token session — shows ``—`` rather than a number from a shipped equation.
    The nuance (calibrating vs. no plan) is spelled out in the session detail."""

    if str(entry.get("client") or "") not in _PLAN_CLIENTS:
        return "—"
    pct = (pcts or {}).get(str(entry.get("client_session_id")))
    if not (isinstance(pct, (int, float)) and not isinstance(pct, bool) and pct > 0):
        return "—"
    return f"≈{pct:.1f}%" if pct >= 0.1 else "≈<0.1%"


def _fold_sessions(sessions: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Fold every subagent DESCENDANT under its top-most root session.

    Returns ``(top_level_sessions_newest_first, children_by_parent_session_key)``.
    A run and all its (and its children's) subagents collapse to one top-level
    row. Grouping follows the ledger's own ``rollup_group_key`` ("{client}::{parent}"
    for a child, else the session's own key), which already applies the parent
    source-namespace compatibility gate — the TUI never contradicts the ledger.
    The walk carries a visited-set so a parent cycle resolves to ``None`` (every
    session on it stays top-level, nothing can vanish) and an orphan chain (parent
    absent) stops at the top-most present ancestor.
    """

    by_session_key = {str(s.get("session_key")): s for s in sessions}

    def _resolve_top(start: str) -> str | None:
        seen: set[str] = set()
        cur = start
        while True:
            node = by_session_key.get(cur)
            if node is None:
                return cur
            group = str(node.get("rollup_group_key") or cur)
            if group == cur or group not in by_session_key:
                return cur  # a root, or the top-most present ancestor
            if group in seen:
                return None  # cycle → keep this session top-level
            seen.add(cur)
            cur = group

    children_by_parent: dict[str, list[dict]] = {}
    top: list[dict] = []
    for entry in sessions:
        sk = str(entry.get("session_key"))
        root = _resolve_top(sk)
        if root is not None and root != sk:
            children_by_parent.setdefault(root, []).append(entry)
        else:
            top.append(entry)
    top.sort(key=lambda s: (s.get("last_activity_at") or 0.0), reverse=True)
    return top, children_by_parent


class AgentAcctTUI(App):
    """Live usage / cost / limits dashboard."""

    CSS = """
    .section-title {
        text-style: bold;
        color: $accent;
        padding: 1 0 0 0;
    }
    #status {
        color: $text-muted;
        padding: 0 0 1 0;
    }
    /* The home body scrolls: on a short terminal the limits + recent-sessions
       panels below the fold would otherwise be clipped and unreachable. */
    #body { height: 1fr; }
    #windows { height: auto; }
    #breakdowns { height: auto; padding: 1 0 0 0; }
    #byclient, #bymodel { height: auto; width: 1fr; }
    #bymodel { margin: 0 0 0 2; }
    #limits-body { padding: 0 0 1 0; }
    #recent-status { color: $text-muted; padding: 0 0 1 0; }
    #recent { height: auto; }
    #usage-status { color: $text-muted; padding: 0 0 1 0; }
    #usage-scroll { padding: 0 1; }
    #usage-limits { padding: 0 0 1 0; }
    #usage-periods, #usage-models { height: auto; }
    #sessions-status { color: $text-muted; padding: 0 0 1 0; }
    #sessions-loading { height: auto; }
    #detail-header { padding: 0 1 1 1; }
    #detail-scroll { padding: 0 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("w", "cycle_window", "Cycle window"),
        Binding("s", "open_sessions", "Sessions"),
        Binding("u", "open_usage", "Usage"),
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
        # Where to read Claude subagent transcripts for child-session roles; None
        # → the reader's default (~/.claude, gated by AGENTACCT_SCAN_SUBAGENT_ROLES).
        # Tests point this at a tmp dir.
        self.subagent_projects_root = subagent_projects_root
        self._snapshot: LiveSnapshot | None = None
        self._last_fingerprint: int | None = None
        self._last_refresh_at: float | None = None
        # A manual `r` flashes the refreshed-stamp until this time, so a fast
        # (often no-op) main-page refresh gives visible feedback.
        self._flash_until: float = 0.0
        self._importing: bool = False
        # Recent-sessions panel (home) build guard — mutated on the main thread
        # only (kicker + populate/error callbacks), so it needs no lock. Prevents
        # re-kicking the home ledger build while one is already in flight.
        self._recent_loading: bool = False
        self._error: str | None = None
        # Work-ledger cache shared across the sessions/detail screens. The ledger
        # is EXPENSIVE (O(all events), no caching upstream — ~5s on ~8k events),
        # so it is built once on demand in a worker thread and reused until the
        # event log changes (fingerprint) or the user forces a refresh.
        self._work_ledger: dict | None = None
        self._work_ledger_fp: int | None = None
        # The home recent-sessions panel keeps its OWN ledger cache (see
        # _load_recent), deliberately separate from _work_ledger above: its worker
        # runs on a different node+group, so sharing one cache across the two
        # non-coalescing lineages would let them stale-clobber / tear each other.
        self._recent_ledger: dict | None = None
        self._recent_ledger_fp: int | None = None
        # Per-account weekly-plan estimate cache as a SINGLE tuple
        # (fingerprint, weights, records, per-session pcts) so the recent-panel worker
        # thread and the main-thread detail can never tear a multi-field write — one
        # atomic assignment, keyed on the event-log content so it self-heals on change
        # and never goes stale after an import.
        self._plan_cache: Any = None
        # Folded child (subagent) sessions, keyed by the parent's session_key;
        # written ONLY by the sessions screen (its sole owner), read by the detail
        # screen. The home panel does not publish here (its table is non-interactive).
        self._children_by_parent: dict[str, list[dict]] = {}
        # Last composed text for each dynamic panel — a stable hook for headless
        # tests (avoids depending on Rich renderable internals).
        self._status_text: str = ""
        self._limits_text: str = ""
        self._recent_text: str = ""

    # -- lifecycle ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="status")
        # Scrollable so nothing below the fold is unreachable; the two panels the
        # user most wants at a glance — provider limits ("how much left") and the
        # recent sessions — sit above the by-client / by-model detail breakdown.
        with VerticalScroll(id="body"):
            yield Static("Usage windows", classes="section-title")
            yield DataTable(id="windows", cursor_type="none", zebra_stripes=True)
            yield Static("Provider limits", classes="section-title")
            yield Static("", id="limits-body")
            yield Static("Recent sessions", classes="section-title")
            yield Static("", id="recent-status")
            yield DataTable(id="recent", cursor_type="none")
            with Horizontal(id="breakdowns"):
                with Vertical():
                    yield Static("", id="byclient-title", classes="section-title")
                    yield DataTable(id="byclient", cursor_type="none")
                with Vertical():
                    yield Static("", id="bymodel-title", classes="section-title")
                    yield DataTable(id="bymodel", cursor_type="none")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "agentacct"
        self.sub_title = "live usage · cost · limits"
        self.query_one("#windows", DataTable).add_columns("window", "tokens", "est. cost", "sessions")
        self.query_one("#byclient", DataTable).add_columns("client", "tokens", "est. cost", "sessions")
        self.query_one("#bymodel", DataTable).add_columns("model", "client", "tokens", "est. cost")
        self.query_one("#recent", DataTable).add_columns(
            "session", "client", "status", "tokens", "plan", "activity"
        )
        self.refresh_data(force=True)  # show the stored view instantly
        self._start_import()  # then freshen it from the client logs in the background
        self._start_recent()  # and fill the recent-sessions panel from the ledger
        self.set_interval(self.refresh_seconds, self.refresh_data)
        self.set_interval(1.0, self._tick_countdowns)

    # -- usage import (store freshness) -------------------------------------

    def _start_import(self) -> None:
        """Kick a background usage import so the in-flight session shows real usage.

        Off (env-gated) → no-op, so the auto-refresh timer never triggers a scan
        and tests never read the developer's real client logs.

        Never launches a second import while one is in flight: the importer mutates
        a PROCESS-GLOBAL pricing catalog (env + cache) and restores it on exit, so
        two overlapping imports would corrupt each other's pricing and persist
        mis-priced rows. `exclusive=True` on the worker cannot prevent this (a
        running thread can't be cancelled mid-call), so we gate here. ``_importing``
        is only ever mutated on the main thread (here and in ``_on_import_done``),
        so the guard is race-free without a lock.
        """

        if self._importing or not _auto_import_enabled():
            return
        self._importing = True
        self._render_status()
        self._import_usage()

    @work(thread=True, exclusive=True, group="import")
    def _import_usage(self) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()
        try:
            from .cli import _local_usage_import_payload

            _local_usage_import_payload(store_dir=self.store_dir, client="all", estimate_costs=True)
        except Exception:  # noqa: BLE001 - freshness is best-effort; never crash the UI.
            pass
        if worker.is_cancelled:
            return
        self.call_from_thread(self._on_import_done)

    def _on_import_done(self) -> None:
        self._importing = False
        self.refresh_data(force=True)
        # Fresh usage just landed → rebuild the recent-sessions panel from it
        # (force supersedes any still-running pre-import build via the recent
        # lineage's exclusive group; the cancelled build's is_cancelled guard
        # drops it before it can write the panel's private cache).
        self._start_recent(force=True)

    # -- recent sessions (home panel) ---------------------------------------

    def _start_recent(self, force: bool = False) -> None:
        """Kick the background ledger build that fills the home recent-sessions
        panel. Coalesced: no second build starts while one is in flight unless
        ``force`` supersedes it (a manual refresh or fresh post-import usage)."""

        if self._recent_loading and not force:
            return
        self._recent_loading = True
        try:
            self.query_one("#recent-status", Static).update("loading recent sessions…")
        except Exception:  # noqa: BLE001 - cosmetic only.
            pass
        self._load_recent(force=force)

    @work(thread=True, exclusive=True, group="recent")
    def _load_recent(self, force: bool = False) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()
        try:
            service = SentinelService(self.store_dir, create=False)
            events = service.list_all_events()
            fingerprint = _events_fingerprint(events)
            # The home panel keeps its OWN ledger cache, deliberately separate from
            # the SessionsScreen `_work_ledger` shared cache. That screen's worker
            # runs on a different node+group, so `exclusive=` cannot coalesce or
            # cancel across the two — sharing one cache would let this lineage
            # stale-clobber the other's (and tear the two-field pair). Decoupling
            # keeps SessionsScreen the SOLE writer of the shared cache (its prior,
            # reviewed design); the only cost is building the ledger once for the
            # panel and again if the user opens `s` (both off the refresh timer).
            if not force and self._recent_ledger is not None and fingerprint == self._recent_ledger_fp:
                ledger = self._recent_ledger
            else:
                from .work_ledger import build_work_ledger

                ledger = build_work_ledger(events)
                # A build cancelled by a newer `r`/import can't be interrupted
                # mid-flight; bail before any side effect so a stale result never
                # overwrites the fresher rebuild's cache. Only this (exclusive)
                # lineage writes these fields, so at most one writer is ever live.
                if worker.is_cancelled:
                    return
                self._recent_ledger = ledger
                self._recent_ledger_fp = fingerprint
        except Exception as exc:  # noqa: BLE001 - the panel degrades, never crashes the app.
            if not worker.is_cancelled:
                self.call_from_thread(self._recent_error, str(exc))
            return
        if worker.is_cancelled:
            return
        sessions = list((ledger.get("session_rollup") or {}).get("sessions") or [])
        # Only the top-level list is needed here; the fold's children index is NOT
        # published — the recent table is non-interactive, and overwriting the
        # shared `_children_by_parent` would let a background home refresh disagree
        # with what SessionsScreen (its sole writer) is showing.
        top, _children = _fold_sessions(sessions)
        # Estimate each session's share of the weekly plan (claude-code) — computed
        # here on the worker thread (off the UI loop) and cached by fingerprint.
        try:
            _confidence, pcts = self._ensure_plan_cache(events)
        except Exception:  # noqa: BLE001 - the plan column is best-effort.
            pcts = {}
        if worker.is_cancelled:
            return
        self.call_from_thread(self._populate_recent, top, pcts)

    def _ensure_plan_cache(self, events: list) -> tuple:
        """Compute + cache per-client plan calibration, keyed on the event fingerprint.

        Returns ``(confidence_by_client, pcts)``. ``pcts`` maps ``session_id -> % of
        the weekly plan`` and contains ONLY sessions of clients whose estimate is
        CALIBRATED from the user's own recorded limit history — a baseline
        (uncalibrated) client is deliberately excluded so the UI shows an honest
        'calibrating' note instead of a number derived from a shipped equation.
        ``confidence_by_client`` carries every plan-bearing client's status so the
        detail view can distinguish 'calibrating' from 'no plan'.

        Shared by the recent panel, the sessions list, and the session detail so the
        expensive usage view + calibration run at most once per event change and a
        stale scale can't survive an import. Stored as one tuple so a concurrent
        writer can never observe a torn value; the last writer wins."""

        fingerprint = _events_fingerprint(events)
        cache = self._plan_cache
        if cache is None or cache[0] != fingerprint:
            from .plan_cost import calibrate_plan_weights, session_plan_pcts
            from .usage_snapshot import usage_records

            confidence: dict[str, str] = {}
            pcts: dict[str, float] = {}
            for client in _PLAN_CLIENTS:
                records = usage_records(events, client=client)
                weights = calibrate_plan_weights(events, client=client, records=records)
                confidence[client] = weights.confidence
                # Only surface a number when it is grounded in THIS account's own
                # limit history. Providers change how many tokens a plan grants, so a
                # shipped universal equation would be a guess — we withhold it.
                if weights.confidence == "calibrated":
                    pcts.update(session_plan_pcts(records, weights, client=client))
            cache = (fingerprint, confidence, pcts)
            self._plan_cache = cache  # single atomic assignment
        return cache[1], cache[2]

    def _recent_error(self, message: str) -> None:
        self._recent_loading = False
        self._recent_text = f"could not load sessions: {message}"
        try:
            self.query_one("#recent-status", Static).update(
                f"[red]could not load sessions:[/] {_escape(message)}"
            )
        except Exception:  # noqa: BLE001 - last resort.
            pass

    def _populate_recent(self, top: list[dict], pcts: dict[str, float] | None = None) -> None:
        self._recent_loading = False
        pcts = pcts or {}
        try:
            table = self.query_one("#recent", DataTable)
        except Exception:  # noqa: BLE001 - app tearing down.
            return
        table.clear()
        now = time.time()
        for entry in top[:5]:
            usage = entry.get("usage") or {}
            title = (
                entry.get("client_session_title")
                or entry.get("client_session_id_short")
                or "(untitled)"
            )
            table.add_row(
                _escape(str(title))[:40],
                _escape(str(entry.get("client") or "")),
                _session_badge(entry),  # fixed-vocab colored badge (safe markup)
                format_tokens(usage.get("total_tokens")),
                _plan_pct_cell(entry, pcts),
                _humanize_ago(entry.get("last_activity_at"), now),
            )
        note = f"{len(top)} sessions · press s for all" if top else "no sessions yet"
        self._recent_text = note
        try:
            self.query_one("#recent-status", Static).update(note)
        except Exception:  # noqa: BLE001 - cosmetic only.
            pass

    # -- data ---------------------------------------------------------------

    def refresh_data(self, force: bool = False) -> None:
        """Re-read the event log; rebuild the snapshot when it changed (or forced).

        Fail-soft: a read or build error is shown in the status line and retried on
        the next tick — it never crashes the UI and never poisons the change key
        (the fingerprint is advanced only after a successful rebuild).
        """

        try:
            service = SentinelService(self.store_dir, create=False)
            events = service.list_all_events()
        except Exception as exc:  # noqa: BLE001 - a live view must survive a bad read.
            self._error = f"error reading store: {exc}"
            self._render_status()
            return

        fingerprint = _events_fingerprint(events)
        if (
            not force
            and self._snapshot is not None
            and self._error is None
            and fingerprint == self._last_fingerprint
        ):
            return  # content unchanged → skip the (expensive) cube rebuild.

        try:
            snapshot = build_live_snapshot(
                events, client=self.client, breakdown_window=self.window_token
            )
        except Exception as exc:  # noqa: BLE001 - never let a build error kill the UI.
            # Leave the fingerprint unadvanced so the next tick retries.
            self._error = f"error building snapshot: {exc}"
            self._render_status()
            return

        self._error = None
        self._last_fingerprint = fingerprint
        self._snapshot = snapshot
        # Stamp the actual rebuild time so the status line gives visible feedback:
        # `r` forces a rebuild, so the timestamp advances on every manual refresh
        # even when the underlying numbers are unchanged.
        self._last_refresh_at = time.time()
        self._render_all()

    # -- rendering ----------------------------------------------------------

    def _render_all(self) -> None:
        # Defense in depth: data-derived text is escaped before it reaches Rich
        # markup (below), but a render helper must never be able to propagate an
        # exception into Textual's event loop and kill the app.
        try:
            self._render_status()
            self._render_windows()
            self._render_breakdowns()
            self._render_limits()
        except Exception as exc:  # noqa: BLE001 - degrade to a plain note, never crash.
            try:
                self.query_one("#status", Static).update(f"render error: {_escape(str(exc))}")
            except Exception:  # noqa: BLE001 - last resort; nothing more we can do.
                pass

    def _render_status(self) -> None:
        store = _escape(str(self.store_dir))
        if self._error is not None:
            text = f"[red]{_escape(self._error)}[/]  ·  store: {store}"
        elif self._snapshot is None:
            text = f"loading…  ·  store: {store}"
        else:
            usage = self._snapshot.usage
            now = time.time()
            parts: list[str] = []
            if self._last_refresh_at is not None:
                stamp = time.strftime("%H:%M:%S", time.localtime(self._last_refresh_at))
                if now < self._flash_until:  # highlighted badge for ~1.2s after `r`
                    parts.append(f"[black on green] ⟳ refreshed {stamp} [/]")
                else:
                    parts.append(f"refreshed {stamp}")
            if usage.as_of is not None:
                parts.append(f"as of {humanize_seconds(now - usage.as_of)} ago")
            parts.append(f"{usage.usage_record_count} usage records")
            parts.append(f"window: {usage.breakdown_window}")
            if self.client:
                parts.append(f"client: {_escape(self.client)}")
            parts.append(f"store: {store}")
            if self._importing:
                parts.append("[yellow]⟳ importing usage…[/]")
            text = "  ·  ".join(parts)
        self._status_text = text
        self.query_one("#status", Static).update(text)

    def _render_windows(self) -> None:
        table = self.query_one("#windows", DataTable)
        table.clear()
        if self._snapshot is None:
            return
        for window in self._snapshot.usage.windows:
            totals = window.totals
            table.add_row(
                window.label,
                format_tokens(totals.get("total_tokens_including_cached")),
                cost_text(totals),
                format_tokens(totals.get("sessions")),
            )

    def _render_breakdowns(self) -> None:
        if self._snapshot is None:
            return
        usage = self._snapshot.usage
        self.query_one("#byclient-title", Static).update(f"by client · {usage.breakdown_window}")
        self.query_one("#bymodel-title", Static).update(f"top models · {usage.breakdown_window}")

        by_client = self.query_one("#byclient", DataTable)
        by_client.clear()
        for row in sorted(usage.by_client, key=lambda r: -(r.get("total_tokens_including_cached") or 0)):
            by_client.add_row(
                # Escape data-derived names: DataTable re-parses str cells as Rich
                # markup, so an unescaped '[' pattern would raise mid-render.
                _escape(str(row.get("client"))),
                format_tokens(row.get("total_tokens_including_cached")),
                cost_text(row),
                format_tokens(row.get("sessions")),
            )

        by_model = self.query_one("#bymodel", DataTable)
        by_model.clear()
        top_models = sorted(
            usage.by_model, key=lambda r: -(r.get("total_tokens_including_cached") or 0)
        )[:8]
        for row in top_models:
            by_model.add_row(
                _escape(str(row.get("model") or "—")),
                _escape(str(row.get("client") or "")),
                format_tokens(row.get("total_tokens_including_cached")),
                cost_text(row),
            )

    def _render_limits(self) -> None:
        body = self.query_one("#limits-body", Static)
        if self._snapshot is None:
            self._limits_text = ""
            body.update("")
            return
        # Shared with the usage screen; hides stale (signed-out/cancelled) streams
        # and discloses the count.
        self._limits_text = _limits_panel_text(self._snapshot.limits, time.time())
        body.update(self._limits_text)

    def _tick_countdowns(self) -> None:
        """1-second tick: refresh only the time-derived text (countdowns + age)."""

        if self._snapshot is None:
            return
        try:
            self._render_status()
            self._render_limits()
        except Exception:  # noqa: BLE001 - a countdown repaint must never crash the app.
            pass

    # -- actions ------------------------------------------------------------

    def action_refresh(self) -> None:
        # Flash the refreshed-stamp so a fast (often unchanged) refresh is visible.
        self._flash_until = time.time() + 1.2
        self.refresh_data(force=True)
        self._start_import()  # also re-scan the client logs for fresh usage
        # Rebuild the recent panel now. When an import is enabled it also rebuilds
        # on completion (fresh usage), so only force a build here when there is no
        # import to trigger it — avoids a redundant double build on every `r`.
        if not _auto_import_enabled():
            self._start_recent(force=True)

    def action_cycle_window(self) -> None:
        try:
            index = _WINDOW_CYCLE.index(self.window_token)
        except ValueError:
            index = _WINDOW_CYCLE.index("7d")
        self.window_token = _WINDOW_CYCLE[(index + 1) % len(_WINDOW_CYCLE)]
        self.refresh_data(force=True)

    def action_open_sessions(self) -> None:
        # Only open the sessions drill-down from the base dashboard. The app-level
        # `s` binding stays active while a (non-modal) sub-screen is on top, so
        # without this guard a second `s` would stack a duplicate screen and spawn
        # a second expensive ledger build.
        if len(self.screen_stack) > 1:
            return
        self.push_screen(SessionsScreen())

    def action_open_usage(self) -> None:
        # Same guard as the sessions drill-down: the app-level `u` binding stays
        # active while a (non-modal) sub-screen is on top, so without this a second
        # `u` would stack a duplicate usage screen.
        if len(self.screen_stack) > 1:
            return
        self.push_screen(UsageScreen())


def _humanize_ago(ts: Any, now: float) -> str:
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0 and ts <= now:
        return f"{humanize_seconds(now - ts)} ago"
    return "—"


def _session_matches(work_item: dict, client: Any, session_id: Any) -> bool:
    return work_item.get("client") == client and work_item.get("client_session_id") == session_id


def _session_cost_text(usage: dict) -> str:
    """Cost cell for a session's usage summary, using the shared honesty rule.

    The session summary carries no ``cost_complete`` flag (unlike the cube), so
    derive it: complete only when some row is priced AND none is unpriced or held.
    Otherwise :func:`cost_text` shows the priced subtotal as partial (``~$``) or
    an em-dash. Equivalent to the cube's own completeness test in practice.
    """

    complete = bool(usage.get("priced_rows")) and not usage.get("unpriced_rows") and not usage.get(
        "excluded_non_additive_rows"
    )
    return cost_text(
        {
            "cost_complete": complete,
            "estimated_cost_usd": usage.get("estimated_cost_usd"),
            "known_additive_cost_usd": usage.get("estimated_cost_usd"),
        }
    )


def _status_glyph(status: str) -> str:
    if status in ("completed", "resolved", "handed_off"):
        return "✓"
    if status == "blocked":
        return "⚠"
    if status in ("started", "checkpoint"):
        return "▶"
    return "●"


def _first_line(text: str, limit: int = 72) -> str:
    line = str(text).strip().splitlines()[0] if str(text).strip() else ""
    return line[:limit] + ("…" if len(line) > limit else "")


class SessionsScreen(Screen):
    """A list of TOP-LEVEL sessions (child/subagent sessions are folded into their
    parent's detail); select one to drill in."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Rebuild"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._by_key: dict[str, dict] = {}
        self._total_top = 0
        self._total_sessions = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="sessions-status")
        yield LoadingIndicator(id="sessions-loading")
        yield DataTable(id="sessions", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "agentacct"
        self.sub_title = "sessions"
        table = self.query_one("#sessions", DataTable)
        table.add_columns(
            "session", "client", "project", "activity", "status", "tokens", "est. cost", "plan", "steps", "⋔ sub"
        )
        self._set_loading(True, "Building the work ledger (one-time, a few seconds)…")
        self._load()

    def _set_loading(self, loading: bool, message: str | None = None) -> None:
        try:
            self.query_one("#sessions-loading", LoadingIndicator).display = loading
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
        if message is not None:
            self.query_one("#sessions-status", Static).update(message)

    @work(thread=True, exclusive=True)
    def _load(self, force: bool = False) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()
        app = self.app
        try:
            service = SentinelService(app.store_dir, create=False)
            events = service.list_all_events()
            fingerprint = _events_fingerprint(events)
            if not force and app._work_ledger is not None and fingerprint == app._work_ledger_fp:
                ledger = app._work_ledger
            else:
                from .work_ledger import build_work_ledger

                ledger = build_work_ledger(events)
                # A thread worker cancelled by `exclusive=True` (a newer `s`/`r`)
                # cannot be interrupted mid-build, so bail before any side effect —
                # otherwise this stale result would overwrite the fresher rebuild's
                # cache and repaint the screen with older data.
                if worker.is_cancelled:
                    return
                app._work_ledger = ledger
                app._work_ledger_fp = fingerprint
        except Exception as exc:  # noqa: BLE001 - surface, never crash the screen.
            if not worker.is_cancelled:
                app.call_from_thread(self._show_error, str(exc))
            return
        if worker.is_cancelled:
            return
        # Per-session weekly-plan % (calibrated clients only) — computed on the worker
        # thread and cached on the app, shared with the home panel / detail view.
        try:
            _confidence, pcts = app._ensure_plan_cache(events)
        except Exception:  # noqa: BLE001 - the plan column is best-effort.
            pcts = {}
        app.call_from_thread(self._populate, ledger, pcts)

    def _show_error(self, message: str) -> None:
        if not self.is_mounted:  # a late callback must not touch a dismissed screen
            return
        self._set_loading(False)
        self.query_one("#sessions-status", Static).update(
            f"[red]could not build the work ledger:[/] {_escape(message)}"
        )

    def _populate(self, ledger: dict, pcts: dict[str, float] | None = None) -> None:
        if not self.is_mounted:  # screen was dismissed before the build finished
            return
        pcts = pcts or {}
        sessions = list((ledger.get("session_rollup") or {}).get("sessions") or [])
        self._total_sessions = len(sessions)

        # Fold every subagent DESCENDANT under its top-most root session, so a run
        # and all its (and its children's) subagents collapse to one row (shared
        # with the home recent-sessions panel via _fold_sessions).
        top, children_by_parent = _fold_sessions(sessions)
        self.app._children_by_parent = children_by_parent
        self._total_top = len(top)
        shown = top[:_SESSIONS_LIMIT]

        table = self.query_one("#sessions", DataTable)
        table.clear()
        self._by_key.clear()
        now = time.time()
        for index, entry in enumerate(shown):
            key = str(entry.get("session_key") or index)
            self._by_key[key] = entry
            usage = entry.get("usage") or {}
            counts = (entry.get("work") or {}).get("counts") or {}
            title = entry.get("client_session_title") or entry.get("client_session_id_short") or "(untitled)"
            steps = f"{counts.get('total', 0)}✓{counts.get('completed', 0)}"
            if counts.get("active", 0):
                steps += f" ▶{counts.get('active', 0)}"
            if counts.get("blocked", 0):
                steps += f" ⚠{counts.get('blocked', 0)}"
            n_children = len(children_by_parent.get(str(entry.get("session_key")), []))
            table.add_row(
                _escape(str(title))[:48],
                _escape(str(entry.get("client") or "")),
                _escape(str(entry.get("project") or "—"))[:24],
                _humanize_ago(entry.get("last_activity_at"), now),
                _session_badge(entry),
                format_tokens(usage.get("total_tokens")),
                _session_cost_text(usage),
                _plan_pct_cell(entry, pcts),
                steps,
                str(n_children) if n_children else "",
                key=key,
            )
        self._set_loading(False)
        folded = self._total_sessions - self._total_top
        cap = f" (showing most recent {_SESSIONS_LIMIT})" if self._total_top > _SESSIONS_LIMIT else ""
        foldnote = f" · {folded} subagent sessions folded" if folded else ""
        self.query_one("#sessions-status", Static).update(
            f"{self._total_top} sessions{cap}{foldnote} · Enter to open · r rebuild · Esc back"
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value if event.row_key is not None else None
        entry = self._by_key.get(str(key)) if key is not None else None
        if entry is not None:
            self.app.push_screen(SessionDetailScreen(entry))

    def action_refresh(self) -> None:
        self._set_loading(True, "Rebuilding the work ledger…")
        self._load(force=True)

    def action_back(self) -> None:
        self.app.pop_screen()


# How many child sessions to render (with role lookups) in a parent's detail.
_DETAIL_CHILDREN_LIMIT = 25


class SessionDetailScreen(Screen):
    """One session's steps (work items) + check results + its subagents."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("backspace", "back", "Back"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, entry: dict) -> None:
        super().__init__()
        self._entry = entry
        # Composed text kept for headless tests (Rich renderable internals are not
        # a stable assertion target).
        self._header_text = ""
        self._body_text = ""
        self._subagents_text = ""

    def compose(self) -> ComposeResult:
        entry = self._entry
        now = time.time()
        yield Header(show_clock=True)
        yield Static(self._render_header(entry, now), id="detail-header")
        with VerticalScroll(id="detail-scroll"):
            steps = self._steps()
            if not steps:
                yield Static(
                    "[dim]No recorded work steps — steps come from MCP section/check "
                    "events; a usage-only session has none.[/]",
                    id="detail-nosteps",
                )
            for wi in steps:
                title_line, content = self._step_render(wi, now)
                self._body_text += title_line + "\n" + content + "\n"
                yield Collapsible(Static(content), title=title_line, collapsed=True)
            children = self._children()
            if children:
                subtitle, content = self._subagents_render(
                    entry, children, now, getattr(self.app, "subagent_projects_root", None)
                )
                self._subagents_text = content
                yield Collapsible(Static(content), title=subtitle, collapsed=True, id="subagents")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "agentacct"
        self.sub_title = "session detail"

    # -- data ---------------------------------------------------------------

    def _steps(self) -> list[dict]:
        ledger = getattr(self.app, "_work_ledger", None) or {}
        client = self._entry.get("client")
        sid = self._entry.get("client_session_id")
        return [wi for wi in (ledger.get("work_items") or []) if _session_matches(wi, client, sid)]

    def _children(self) -> list[dict]:
        index = getattr(self.app, "_children_by_parent", {}) or {}
        kids = list(index.get(str(self._entry.get("session_key")), []))
        kids.sort(key=lambda s: ((s.get("usage") or {}).get("total_tokens") or 0), reverse=True)
        return kids

    # -- rendering ----------------------------------------------------------

    def _render_header(self, entry: dict, now: float) -> str:
        title = entry.get("client_session_title") or entry.get("client_session_id_short") or "(untitled)"
        usage = entry.get("usage") or {}
        header = f"[bold]{_escape(str(title))}[/]"
        glyph, label, color = _session_status(entry)
        if label != "—":  # a usage-only session (no recorded steps) shows no badge
            header += f"  [{color}]{glyph} {label}[/]"
        header += f"\n{_escape(str(entry.get('client') or ''))} · {_escape(str(entry.get('project') or '—'))}"
        last = entry.get("last_activity_at")
        if isinstance(last, (int, float)) and last:
            header += f" · {_humanize_ago(last, now)}"
        header += f" · {format_tokens(usage.get('total_tokens'))} tokens · {_session_cost_text(usage)}"
        plan_line = self._plan_estimate(entry)
        if plan_line:
            header += f"\n[dim]{plan_line}[/]"
        self._header_text = header
        return header

    def _plan_estimate(self, entry: dict) -> str | None:
        """A one-line weekly-plan share for this session — a number only when honest.

        For a plan-bearing client (claude-code, codex) the number is shown ONLY when
        the estimate is CALIBRATED from the user's own recorded limit history; while
        that history is still too thin (or too much of the account's usage happens
        outside the tracked client, e.g. the Claude desktop app / web), we show an
        honest 'calibrating' note rather than a figure from a shipped equation.
        Non-plan clients get no line. Fail-soft: any read/build problem yields no line
        rather than crashing the detail.
        """

        client = str(entry.get("client") or "")
        if client not in _PLAN_CLIENTS:
            return None
        try:
            app = self.app
            events = SentinelService(app.store_dir, create=False).list_all_events()
            # Shared, fingerprint-cached confidence + per-session % (built once per
            # event change, off the UI thread by the recent-panel worker when it ran
            # first). pcts contains only calibrated clients' sessions.
            confidence, pcts = app._ensure_plan_cache(events)
            if confidence.get(client) == "calibrated":
                pct = pcts.get(str(entry.get("client_session_id")), 0.0)
                shown = f"{pct:.1f}%" if pct >= 0.1 else "<0.1%"
                return f"≈ {shown} of your weekly plan · estimate, calibrated to your usage"
            # Uncalibrated: no fabricated number — say what agentacct is doing instead.
            return "weekly plan: calibrating from your own usage — not enough limit history yet"
        except Exception:  # noqa: BLE001 - the estimate is best-effort; never crash the detail.
            return None

    def _step_render(self, wi: dict, now: float) -> tuple[str, str]:
        status = str(wi.get("latest_status") or "?")
        title = wi.get("title") or wi.get("section_id") or "(untitled step)"
        kind = str(wi.get("kind") or "")
        checks = [e for e in (wi.get("evidence_events") or []) if isinstance(e, dict)]
        # Collapsible title is plain (escaped) — no color, so it can't break markup.
        parts = [status, kind, _humanize_ago(wi.get("updated_at"), now)]
        if checks:
            parts.append(f"{len(checks)} checks")
        title_line = f"{_status_glyph(status)} {_escape(str(title))}  — {_escape(' · '.join(p for p in parts if p))}"

        lines: list[str] = []
        summary = wi.get("summary")
        if summary:
            lines.append(f"[dim]{_escape(str(summary))}[/]")
        for ev in checks:
            mark, mcolor = _evidence_mark(str(ev.get("result") or ""))
            etype = _escape(str(ev.get("evidence_type") or "check"))
            summ = _escape(str(ev.get("summary") or ev.get("result") or ""))
            cline = f"[{mcolor}]{mark}[/] {etype}: {summ}"
            exit_code = ev.get("exit_code")
            if isinstance(exit_code, int):
                cline += f" [dim](exit {exit_code})[/]"
            lines.append(cline)
        blocker = wi.get("blocker")
        if blocker:
            lines.append(f"[red]⚠ blocked:[/] {_escape(str(blocker))}")
        return title_line, ("\n".join(lines) if lines else "[dim](no summary or checks)[/]")

    def _subagents_render(
        self, entry: dict, children: list[dict], now: float, projects_root=None
    ) -> tuple[str, str]:
        # Aggregate over the folded set actually listed (all descendants), so the
        # subtitle count and totals are self-consistent — the ledger's
        # related.children_usage counts only DIRECT children and would disagree.
        total = len(children)
        agg_tokens = sum(int((c.get("usage") or {}).get("total_tokens") or 0) for c in children)
        agg_cost = 0.0
        any_cost = False
        for c in children:
            value = finite((c.get("usage") or {}).get("estimated_cost_usd"))
            if value is not None:
                agg_cost += value
                any_cost = True
        subtitle = f"⋔ Subagents ({total}) — {format_tokens(agg_tokens)} tok"
        if any_cost:
            subtitle += f" · ~${agg_cost:.2f}"

        shown = children[:_DETAIL_CHILDREN_LIMIT]
        child_ids = [str(c.get("client_session_id")) for c in shown]
        roles = read_roles_for_children(
            str(entry.get("client_session_id")), child_ids, projects_root=projects_root
        )

        lines: list[str] = []
        for child in shown:
            cid = str(child.get("client_session_id"))
            usage = child.get("usage") or {}
            counts = (child.get("work") or {}).get("counts") or {}
            role = roles.get(cid)
            role_type = (role.agent_type if role and role.agent_type else child.get("session_kind") or "subagent")
            models = child.get("observed_models") or []
            model = _escape(str(models[0])) if models else ""
            head = f"[bold]{_escape(str(role_type))}[/]"
            if role and role.task:
                head += f" [dim]{_escape(_first_line(role.task))}[/]"
            meta = " · ".join(
                p for p in (
                    f"{format_tokens(usage.get('total_tokens'))} tok",
                    _session_cost_text(usage),
                    model,
                    (f"{counts.get('total', 0)} steps" if counts.get("total") else ""),
                ) if p
            )
            lines.append(f"{head}\n    [dim]{meta}[/]")
        if total > len(shown):
            lines.append(f"[dim]… and {total - len(shown)} more (by token volume)[/]")
        return subtitle, "\n".join(lines)

    def action_back(self) -> None:
        self.app.pop_screen()


# The trailing ranges the usage screen's `d` key cycles: (days, label). None =
# all time. Granularity is auto (daily for short ranges, weekly for 90/all).
_USAGE_RANGE_CYCLE: tuple[tuple[int | None, str], ...] = (
    (7, "7d"),
    (30, "30d"),
    (90, "90d"),
    (None, "all"),
)
# How many (client, model) rows the by-model detail lists.
_USAGE_MODELS_LIMIT = 20


class UsageScreen(Screen):
    """A dedicated usage view: a per-period token/cost time series (with text
    bars) plus a by-model detail table, over the same cube the overview uses."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("backspace", "back", "Back"),
        Binding("d", "cycle_range", "Range"),
        Binding("c", "cycle_client", "Client"),
        Binding("m", "cycle_model", "Model"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, *, range_index: int = 1) -> None:  # default → 30d
        super().__init__()
        self._range_index = range_index % len(_USAGE_RANGE_CYCLE)
        self._page: UsagePage | None = None
        # Filters (None = all). Client scopes the whole page + limits; model scopes
        # the time series + by-model table. The cycle vocabularies are recomputed on
        # each rebuild from the unfiltered / client-scoped views.
        self._client_filter: str | None = None
        self._model_filter: str | None = None
        self._available_clients: list[str] = []
        self._available_models: list[str] = []
        # Composed text kept for headless tests.
        self._status_text = ""
        self._periods_text = ""
        self._limits_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="usage-status")
        with VerticalScroll(id="usage-scroll"):
            yield Static("Plan limits", classes="section-title")
            yield Static("", id="usage-limits")
            yield Static("Usage over time", classes="section-title")
            yield DataTable(id="usage-periods", cursor_type="none", zebra_stripes=True)
            yield Static("By model", classes="section-title")
            yield DataTable(id="usage-models", cursor_type="none")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "agentacct"
        self.sub_title = "usage"
        # Inherit the app-level --client filter as the starting scope.
        self._client_filter = getattr(self.app, "client", None)
        self.query_one("#usage-periods", DataTable).add_columns(
            "period", "tokens", "est. cost", "sessions", ""
        )
        self.query_one("#usage-models", DataTable).add_columns(
            "model", "client", "tokens", "est. cost", "sessions"
        )
        self._rebuild()

    def _rebuild(self) -> None:
        days, label = _USAGE_RANGE_CYCLE[self._range_index]
        cf, mf = self._client_filter, self._model_filter
        try:
            events = SentinelService(self.app.store_dir, create=False).list_all_events()
            # Client vocabulary from the unfiltered view; model vocabulary from the
            # current client scope (model-unfiltered) — so cycling never collapses
            # the option list to the single value currently selected. Reuse builds
            # where the filters make them identical (the screen is on-demand, not on
            # a timer, so a couple of cube builds are fine).
            unfiltered = build_usage_page(events, days=days)
            self._available_clients = sorted(
                {str(m.get("client")) for m in unfiltered.by_model if m.get("client")}
            )
            scoped = unfiltered if cf is None else build_usage_page(events, client=cf, days=days)
            self._available_models = sorted(
                {str(m.get("model")) for m in scoped.by_model if m.get("model")}
            )
            # A range (or client) change can strand the model filter on a model with
            # no usage in the new scope; drop it so the view never renders an empty
            # page while the status line still claims a now-absent model filter.
            if mf is not None and mf not in self._available_models:
                mf = None
                self._model_filter = None
            page = scoped if mf is None else build_usage_page(events, client=cf, model=mf, days=days)
            limits = build_client_limits(events, client=cf)
        except Exception as exc:  # noqa: BLE001 - surface, never crash the screen.
            self._status_text = f"error: {exc}"
            try:
                self.query_one("#usage-status", Static).update(
                    f"[red]could not build usage:[/] {_escape(str(exc))}"
                )
            except Exception:  # noqa: BLE001 - last resort.
                pass
            return
        self._page = page
        try:
            self._render_page(page, label, limits)
        except Exception as exc:  # noqa: BLE001 - a render error degrades to a note.
            try:
                self.query_one("#usage-status", Static).update(f"render error: {_escape(str(exc))}")
            except Exception:  # noqa: BLE001
                pass

    def _render_page(self, page: UsagePage, label: str, limits: list[ClientLimit]) -> None:
        now = time.time()
        parts = [f"range: {label}", f"{page.granularity}"]
        if page.as_of is not None and page.as_of <= now:
            parts.append(f"as of {humanize_seconds(now - page.as_of)} ago")
        parts.append(f"{page.usage_record_count} usage records")
        parts.append(f"client: {_escape(str(page.client_filter)) if page.client_filter else 'all'}")
        if page.model_filter:
            parts.append(f"model: {_escape(str(page.model_filter))}")
        parts.append("c client · m model · d range · r · Esc")
        self._status_text = "  ·  ".join(parts)
        self.query_one("#usage-status", Static).update(self._status_text)

        # Plan limits (shared renderer with the home panel; stale streams hidden).
        self._limits_text = _limits_panel_text(limits, now)
        self.query_one("#usage-limits", Static).update(self._limits_text)

        # Periods newest-first for a monitor (latest on top, no scroll needed); the
        # "unknown" bucket (bad-timestamp rows, all-time only) always sorts last so
        # it never masquerades as the most recent period. Bars scale to the busiest
        # period in view — the share-of-peak signal a TUI can draw without a chart.
        periods = list(page.by_period)
        dated = [p for p in periods if p.get("period") != "unknown"]
        unknown = [p for p in periods if p.get("period") == "unknown"]
        ordered = list(reversed(dated)) + unknown
        peak = max((int(p.get("total_tokens_including_cached") or 0) for p in ordered), default=0)

        table = self.query_one("#usage-periods", DataTable)
        table.clear()
        text_rows: list[str] = []
        for period in ordered:
            tokens = int(period.get("total_tokens_including_cached") or 0)
            bar = ratio_bar(tokens, peak)
            table.add_row(
                _escape(str(period.get("period"))),
                format_tokens(tokens),
                cost_text(period),
                format_tokens(period.get("sessions")),
                bar,
            )
            text_rows.append(f"{period.get('period')} {tokens} {bar}")
        self._periods_text = "\n".join(text_rows)

        models = self.query_one("#usage-models", DataTable)
        models.clear()
        for model in page.by_model[:_USAGE_MODELS_LIMIT]:
            models.add_row(
                _escape(str(model.get("model") or "—")),
                _escape(str(model.get("client") or "")),
                format_tokens(model.get("total_tokens_including_cached")),
                cost_text(model),
                format_tokens(model.get("sessions")),
            )

    def action_cycle_range(self) -> None:
        self._range_index = (self._range_index + 1) % len(_USAGE_RANGE_CYCLE)
        self._rebuild()

    def action_cycle_client(self) -> None:
        options: list[str | None] = [None, *self._available_clients]
        try:
            index = options.index(self._client_filter)
        except ValueError:
            index = 0
        self._client_filter = options[(index + 1) % len(options)]
        # Models are client-scoped, so a client change invalidates the model filter.
        self._model_filter = None
        self._rebuild()

    def action_cycle_model(self) -> None:
        options: list[str | None] = [None, *self._available_models]
        try:
            index = options.index(self._model_filter)
        except ValueError:
            index = 0
        self._model_filter = options[(index + 1) % len(options)]
        self._rebuild()

    def action_refresh(self) -> None:
        self._rebuild()

    def action_back(self) -> None:
        self.app.pop_screen()


__all__ = ["AgentAcctTUI", "SessionsScreen", "SessionDetailScreen", "UsageScreen"]
