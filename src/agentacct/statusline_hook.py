"""Lightweight Claude Code statusLine hook for agentacct (terminal-CLI users).

Installed as the Claude Code ``statusLine`` command via
``python -m agentacct.statusline_hook``. Claude invokes it (at most ~every 300ms)
with the session JSON on stdin. This module is deliberately tiny — it imports
only the stdlib and (lazily) ``agentacct.rate_limits``; agentacct's package
``__init__`` is empty, so it starts in tens of milliseconds rather than the ~0.4s
of the full CLI. That matters: a heavy process on every status render would lag
the bar and burn CPU.

It does two things and never fails:

1. Persist the latest provider rate-limit reading to a spool file that
   ``agentacct usage import-local`` / ``watch`` ingest into ``rate_limit_observed``
   events — the heavy recording stays OUT of this hot path.
2. Print a one-line status string (model · context% · 5h/7d limits · cost).

Any error is swallowed and a minimal line is printed, so a slow or broken hook
can never break or delay Claude Code's status bar.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Mapping


def _limit_pct(window: Any) -> str:
    if isinstance(window, Mapping):
        used = window.get("used_percentage")
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            return f"{int(round(used))}%"
    return "—"


def render_status_line(payload: Mapping[str, Any]) -> str:
    """Compact one-line status: model · ctx% · 5h/7d · $cost (fields present only)."""

    parts: list[str] = []
    model = payload.get("model")
    if isinstance(model, Mapping):
        name = model.get("display_name") or model.get("id")
        if name:
            parts.append(str(name))
    ctx = payload.get("context_window")
    if isinstance(ctx, Mapping):
        used = ctx.get("used_percentage")
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            parts.append(f"ctx {int(round(used))}%")
    rate_limits = payload.get("rate_limits")
    if isinstance(rate_limits, Mapping):
        parts.append(f"5h {_limit_pct(rate_limits.get('five_hour'))}")
        parts.append(f"7d {_limit_pct(rate_limits.get('seven_day'))}")
    cost = payload.get("cost")
    if isinstance(cost, Mapping):
        total = cost.get("total_cost_usd")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            parts.append(f"${float(total):.2f}")
    return "  ".join(parts)


def _safe_print(text: str) -> None:
    # Even stdout can fail (e.g. BrokenPipe if Claude closed the pipe); swallow it
    # so nothing escapes main() and the process still exits 0.
    try:
        print(text)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001 - never let a read error break the status bar.
        _safe_print("")
        return 0

    payload: Any = {}
    try:
        payload = json.loads(raw) if raw and raw.strip() else {}
    except Exception:  # noqa: BLE001 - malformed stdin → empty payload, still print.
        payload = {}

    # Persist the latest reading for agentacct to ingest (best-effort, off the
    # hot path's critical output).
    if isinstance(payload, Mapping) and isinstance(payload.get("rate_limits"), Mapping):
        try:
            from agentacct import rate_limits as rl

            rl.write_claude_statusline_spool(payload["rate_limits"], captured_at=time.time())
        except Exception:  # noqa: BLE001 - spooling must never break the bar.
            pass

    try:
        line = render_status_line(payload) if isinstance(payload, Mapping) else ""
    except Exception:  # noqa: BLE001 - rendering must never break the bar.
        line = ""
    _safe_print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
