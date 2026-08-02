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

import time
from pathlib import Path

from rich.markup import escape as _escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from .service import SentinelService
from .usage_snapshot import (
    LiveSnapshot,
    build_live_snapshot,
    cost_text,
    format_tokens,
    humanize_seconds,
    usage_bar,
)

# The window tokens the `w` key cycles through (same vocabulary as `now --window`).
_WINDOW_CYCLE: tuple[str, ...] = ("today", "7d", "30d", "all")

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
    #windows { height: auto; }
    #breakdowns { height: auto; }
    #byclient, #bymodel { height: auto; width: 1fr; }
    #bymodel { margin: 0 0 0 2; }
    #limits-body { padding: 0 0 1 0; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("w", "cycle_window", "Cycle window"),
        Binding("s", "open_sessions", "Sessions"),
    ]

    def __init__(
        self,
        *,
        store_dir: Path,
        client: str | None = None,
        window_token: str = "7d",
        refresh_seconds: float = 5.0,
    ) -> None:
        super().__init__()
        self.store_dir = Path(store_dir)
        self.client = client
        self.window_token = window_token
        self.refresh_seconds = max(1.0, float(refresh_seconds))
        self._snapshot: LiveSnapshot | None = None
        self._last_fingerprint: int | None = None
        self._last_refresh_at: float | None = None
        self._error: str | None = None
        # Work-ledger cache shared across the sessions/detail screens. The ledger
        # is EXPENSIVE (O(all events), no caching upstream — ~5s on ~8k events),
        # so it is built once on demand in a worker thread and reused until the
        # event log changes (fingerprint) or the user forces a refresh.
        self._work_ledger: dict | None = None
        self._work_ledger_fp: int | None = None
        # Last composed text for each dynamic panel — a stable hook for headless
        # tests (avoids depending on Rich renderable internals).
        self._status_text: str = ""
        self._limits_text: str = ""

    # -- lifecycle ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="status")
        with Vertical(id="body"):
            yield Static("Usage windows", classes="section-title")
            yield DataTable(id="windows", cursor_type="none", zebra_stripes=True)
            with Horizontal(id="breakdowns"):
                with Vertical():
                    yield Static("", id="byclient-title", classes="section-title")
                    yield DataTable(id="byclient", cursor_type="none")
                with Vertical():
                    yield Static("", id="bymodel-title", classes="section-title")
                    yield DataTable(id="bymodel", cursor_type="none")
            yield Static("Provider limits", classes="section-title")
            yield Static("", id="limits-body")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "agentacct"
        self.sub_title = "live usage · cost · limits"
        self.query_one("#windows", DataTable).add_columns("window", "tokens", "est. cost", "sessions")
        self.query_one("#byclient", DataTable).add_columns("client", "tokens", "est. cost", "sessions")
        self.query_one("#bymodel", DataTable).add_columns("model", "client", "tokens", "est. cost")
        self.refresh_data(force=True)
        self.set_interval(self.refresh_seconds, self.refresh_data)
        self.set_interval(1.0, self._tick_countdowns)

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
                parts.append(f"refreshed {time.strftime('%H:%M:%S', time.localtime(self._last_refresh_at))}")
            if usage.as_of is not None:
                parts.append(f"as of {humanize_seconds(now - usage.as_of)} ago")
            parts.append(f"{usage.usage_record_count} usage records")
            parts.append(f"window: {usage.breakdown_window}")
            if self.client:
                parts.append(f"client: {_escape(self.client)}")
            parts.append(f"store: {store}")
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
        limits = self._snapshot.limits
        if not limits:
            self._limits_text = (
                "No provider limit data recorded yet — use your agents, or run "
                "`agentacct usage watch`."
            )
            body.update(self._limits_text)
            return
        now = time.time()
        lines: list[str] = []
        for limit in limits:
            # Every value below is provider-derived and rendered as Rich markup,
            # so escape it — a stray '[/]' in e.g. plan_type would otherwise raise
            # a MarkupError and take down the whole live view.
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
                    line += (
                        f"  · resets in {humanize_seconds(delta)}" if delta > 0 else "  · resets now"
                    )
                lines.append(line)
            if isinstance(limit.credits, dict) and limit.credits.get("has_credits"):
                lines.append(f"  credits: {_escape(str(limit.credits.get('balance')))}")
            if limit.reached_type:
                lines.append(f"  [red]⚠ limit reached: {_escape(str(limit.reached_type))}[/]")
            lines.append("")
        self._limits_text = "\n".join(lines).rstrip("\n")
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
        self.refresh_data(force=True)

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


class SessionsScreen(Screen):
    """A list of sessions (from the work ledger); select one to drill in."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Rebuild"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._by_key: dict[str, dict] = {}
        self._total_sessions = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="sessions-status")
        yield DataTable(id="sessions", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "agentacct"
        self.sub_title = "sessions"
        table = self.query_one("#sessions", DataTable)
        table.add_columns("session", "client", "project", "activity", "tokens", "est. cost", "steps")
        self.query_one("#sessions-status", Static).update(
            "Building the work ledger (one-time, a few seconds)…"
        )
        self._load()

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
        app.call_from_thread(self._populate, ledger)

    def _show_error(self, message: str) -> None:
        if not self.is_mounted:  # a late callback must not touch a dismissed screen
            return
        self.query_one("#sessions-status", Static).update(
            f"[red]could not build the work ledger:[/] {_escape(message)}"
        )

    def _populate(self, ledger: dict) -> None:
        if not self.is_mounted:  # screen was dismissed before the build finished
            return
        sessions = list((ledger.get("session_rollup") or {}).get("sessions") or [])
        # Most-recent first; unknown activity sorts last.
        sessions.sort(key=lambda s: (s.get("last_activity_at") or 0.0), reverse=True)
        self._total_sessions = len(sessions)
        shown = sessions[:_SESSIONS_LIMIT]

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
            active = counts.get("active", 0)
            blocked = counts.get("blocked", 0)
            if active:
                steps += f" ▶{active}"
            if blocked:
                steps += f" ⚠{blocked}"
            table.add_row(
                _escape(str(title))[:48],
                _escape(str(entry.get("client") or "")),
                _escape(str(entry.get("project") or "—"))[:24],
                _humanize_ago(entry.get("last_activity_at"), now),
                format_tokens(usage.get("total_tokens")),
                _session_cost_text(usage),
                steps,
                key=key,
            )
        cap = f" (showing most recent {_SESSIONS_LIMIT})" if self._total_sessions > _SESSIONS_LIMIT else ""
        self.query_one("#sessions-status", Static).update(
            f"{self._total_sessions} sessions{cap} · Enter to open · r to rebuild · Esc back"
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value if event.row_key is not None else None
        entry = self._by_key.get(str(key)) if key is not None else None
        if entry is not None:
            self.app.push_screen(SessionDetailScreen(entry))

    def action_refresh(self) -> None:
        self.query_one("#sessions-status", Static).update("Rebuilding the work ledger…")
        self._load(force=True)

    def action_back(self) -> None:
        self.app.pop_screen()


class SessionDetailScreen(Screen):
    """One session's steps (work items) and their machine-check results."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("backspace", "back", "Back"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, entry: dict) -> None:
        super().__init__()
        self._entry = entry
        # Composed text kept for headless tests (Rich renderable internals are
        # not a stable assertion target).
        self._header_text = ""
        self._body_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="detail-header")
        with VerticalScroll():
            yield Static("", id="detail-body")
        yield Footer()

    def on_mount(self) -> None:
        entry = self._entry
        self.title = "agentacct"
        self.sub_title = "session detail"
        client = entry.get("client")
        session_id = entry.get("client_session_id")
        title = entry.get("client_session_title") or entry.get("client_session_id_short") or "(untitled)"
        usage = entry.get("usage") or {}
        now = time.time()

        header = f"[bold]{_escape(str(title))}[/]"
        header += f"\n{_escape(str(client or ''))} · {_escape(str(entry.get('project') or '—'))}"
        first, last = entry.get("first_activity_at"), entry.get("last_activity_at")
        if isinstance(first, (int, float)) and isinstance(last, (int, float)) and first and last:
            header += f" · {_humanize_ago(last, now)}"
        header += (
            f" · {format_tokens(usage.get('total_tokens'))} tokens"
            f" · {_session_cost_text(usage)}"
        )
        self._header_text = header
        self.query_one("#detail-header", Static).update(header)

        ledger = getattr(self.app, "_work_ledger", None) or {}
        work_items = [
            wi for wi in (ledger.get("work_items") or []) if _session_matches(wi, client, session_id)
        ]
        self._body_text = self._render_steps(work_items, now)
        self.query_one("#detail-body", Static).update(self._body_text)

    def _render_steps(self, work_items: list[dict], now: float) -> str:
        if not work_items:
            return (
                "No recorded work steps for this session.\n"
                "[dim]Steps come from MCP section/check events; a usage-only session has none.[/]"
            )
        lines: list[str] = []
        for wi in work_items:
            status = str(wi.get("latest_status") or "?")
            color = _work_status_color(status)
            title = wi.get("title") or wi.get("section_id") or "(untitled step)"
            kind = str(wi.get("kind") or "")
            meta = " · ".join(p for p in (_escape(kind) if kind else "", status, _humanize_ago(wi.get("updated_at"), now)) if p)
            lines.append(f"[{color}]●[/] [bold]{_escape(str(title))}[/]  [dim]{meta}[/]")
            summary = wi.get("summary")
            if summary:
                lines.append(f"    [dim]{_escape(str(summary))}[/]")
            for ev in wi.get("evidence_events") or []:
                if not isinstance(ev, dict):
                    continue
                mark, mcolor = _evidence_mark(str(ev.get("result") or ""))
                etype = _escape(str(ev.get("evidence_type") or "check"))
                summ = _escape(str(ev.get("summary") or ev.get("result") or ""))
                cline = f"    [{mcolor}]{mark}[/] {etype}: {summ}"
                exit_code = ev.get("exit_code")
                if isinstance(exit_code, int):
                    cline += f" [dim](exit {exit_code})[/]"
                lines.append(cline)
            blocker = wi.get("blocker")
            if blocker:
                lines.append(f"    [red]⚠ blocked:[/] {_escape(str(blocker))}")
            lines.append("")
        return "\n".join(lines).rstrip("\n")

    def action_back(self) -> None:
        self.app.pop_screen()


__all__ = ["AgentAcctTUI", "SessionsScreen", "SessionDetailScreen"]
