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
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
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
        self._error: str | None = None
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


__all__ = ["AgentAcctTUI"]
