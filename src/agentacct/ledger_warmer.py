"""Rebuild the recorder's derived work projections soon after the store changes.

The derived work ledger is change-keyed: any write makes the next reader pay a
full rebuild (seconds on a large store) before its request is answered. This
moves that rebuild off the reader's request: while someone has been reading
work data recently, a store change is followed by a background rebuild, so the
next click usually finds the projections already built.

Freshness is untouched. The warmer only fills the same change-keyed caches a
request would fill; every request still derives its own key from the store as
it is at that moment, so a projection is reused only when it matches the
current store exactly. A reader that arrives mid-rebuild waits on the same
single-flight lock it always has.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


class LedgerWarmer:
    """Decides when a background rebuild is due, and runs it.

    ``read_change_token`` returns a cheap value that differs whenever the
    projections' inputs changed. ``rebuild`` fills the caches for the store as
    it is now. Both are injected so the timing rules can be tested with a fake
    clock and no threads; ``start`` runs the same rules on a daemon thread.

    The rules, in order:

    * Nobody read work data within ``reader_window_seconds``: stay idle. A
      closed app costs nothing beyond this thread's own wake-ups.
    * The store changed: wait until it has been quiet for ``settle_seconds``
      (writes arrive in bursts), but never longer than ``max_wait_seconds``
      after the first unbuilt change.
    * After a rebuild that took ``d`` seconds, rest ``d * rest_factor`` before
      the next one, so continuous writes cannot turn the warmer into a
      permanent load: its share of one core stays under 1 / (1 + rest_factor).
    """

    def __init__(
        self,
        *,
        read_change_token: Callable[[], Any],
        rebuild: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        settle_seconds: float = 0.4,
        max_wait_seconds: float = 2.0,
        reader_window_seconds: float = 180.0,
        rest_factor: float = 2.0,
        check_interval_seconds: float = 0.25,
    ) -> None:
        self._read_change_token = read_change_token
        self._rebuild = rebuild
        self._clock = clock
        self._settle_seconds = settle_seconds
        self._max_wait_seconds = max_wait_seconds
        self._reader_window_seconds = reader_window_seconds
        self._rest_factor = rest_factor
        self._check_interval_seconds = check_interval_seconds

        self._last_reader_at: float | None = None
        self._built_token: Any = None
        self._has_built = False
        self._unbuilt_token: Any = None
        self._first_unbuilt_change_at: float | None = None
        self._last_change_at: float | None = None
        self._resting_until = 0.0

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def note_reader(self) -> None:
        """Record that a request just read work data (called on request threads)."""

        self._last_reader_at = self._clock()

    def is_watching(self) -> bool:
        """True while a reader was seen within the reader window."""

        last = self._last_reader_at
        return last is not None and self._clock() - last <= self._reader_window_seconds

    def rebuild_now(self) -> None:
        """Rebuild for the store as it is now, regardless of readers or timing."""

        try:
            token = self._read_change_token()
        except Exception:
            return
        self._run_rebuild(token)

    def check_once(self) -> bool:
        """Apply the rules once. Returns True when this call ran a rebuild."""

        now = self._clock()
        if not self.is_watching():
            self._forget_unbuilt_change()
            return False
        try:
            token = self._read_change_token()
        except Exception:
            # An unreadable store is a reader's problem to report, not ours.
            return False
        if self._has_built and token == self._built_token:
            self._forget_unbuilt_change()
            return False
        if self._first_unbuilt_change_at is None or token != self._unbuilt_token:
            if self._first_unbuilt_change_at is None:
                self._first_unbuilt_change_at = now
            self._unbuilt_token = token
            self._last_change_at = now
        assert self._last_change_at is not None
        settled = now - self._last_change_at >= self._settle_seconds
        overdue = now - self._first_unbuilt_change_at >= self._max_wait_seconds
        if not (settled or overdue) or now < self._resting_until:
            return False
        self._run_rebuild(token)
        return True

    def start(self) -> None:
        """Build once now (the startup warm), then keep watching on a thread."""

        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="agentacct-ledger-warm", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self.rebuild_now()
        while not self._stop.wait(self._check_interval_seconds):
            self.check_once()

    def _run_rebuild(self, token: Any) -> None:
        started = self._clock()
        try:
            self._rebuild()
        except Exception:
            # Best-effort: a reader's own request rebuilds and reports. The
            # token is still recorded so a persistent failure cannot spin.
            pass
        finished = self._clock()
        # The token was read BEFORE the rebuild. A write that landed during it
        # leaves the store's token different from this one, so the next check
        # sees a change and rebuilds again instead of trusting this build.
        self._built_token = token
        self._has_built = True
        self._forget_unbuilt_change()
        self._resting_until = finished + max(0.0, finished - started) * self._rest_factor

    def _forget_unbuilt_change(self) -> None:
        self._unbuilt_token = None
        self._first_unbuilt_change_at = None
        self._last_change_at = None
