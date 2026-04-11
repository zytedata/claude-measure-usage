"""Human-readable formatters used by the TUI screens.

Kept separate from ``claude_usage_tui.plain.display`` on purpose:
the plain CLI's formatters target a fixed-width text column layout
with its own padding and truncation concerns, while the TUI's
formatters feed into Textual widgets that handle layout themselves.
Sharing would force one consumer to accommodate the other's format
conventions, so each layer owns its own small helpers.
"""

from __future__ import annotations

import time


def rel_time(mtime: float, now: float | None = None) -> str:
    """Render a timestamp as a short "time ago" string.

    Resolution decays with age so recent activity stays precise while
    old activity collapses to whatever granularity is still readable:

        under 1 minute    →  "just now"
        under 1 hour      →  "Nm ago"
        under 1 day       →  "Nh ago"
        under 1 week      →  "Nd ago"
        under 1 year      →  "Nw ago"  or  absolute "YYYY-MM-DD"
        1 year or older   →  absolute "YYYY-MM-DD"

    Negative or zero delta renders as "just now" — a future mtime
    probably means clock skew, not a legitimately-future session.
    """
    if now is None:
        now = time.time()
    delta = now - mtime
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86_400:
        return f"{int(delta // 3600)}h ago"
    if delta < 7 * 86_400:
        return f"{int(delta // 86_400)}d ago"
    if delta < 30 * 86_400:
        return f"{int(delta // (7 * 86_400))}w ago"
    # Older than a month — an absolute date is more useful than
    # "14w ago", which requires mental arithmetic to place.
    return time.strftime("%Y-%m-%d", time.localtime(mtime))
