"""Build DataTable rows for the session detail screen.

Deliberately not shared with :mod:`claude_usage_tui.plain.turns_table`.
The text CLI and the TUI render the same underlying data — turn
rows, non-turn rows, subagent rollups — but have different column
sets, sort semantics, and future column ambitions. A shared row
builder would end up serving neither; see ``docs/tui-ux.md`` for
the rationale.

Everything here imports only from the shared data layer
(``parse``, ``metrics``, ``turns_label``) and stays independent
of both the ``plain`` and the ``tui`` presentation modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..metrics import (
    compute_caused_by_turn,
    model_aware_cost_breakdown,
    turn_inherit_seq,
    turn_own_seq,
)
from ..turns_label import short_agent_id, turn_label
from .format import short_tokens, tiny_model


# ---------------------------------------------------------------------------
# Row dataclass
# ---------------------------------------------------------------------------

@dataclass
class DetailRow:
    """A single row in the session detail DataTable.

    ``kind`` determines how the TUI styles and interacts with the
    row:

    - ``"turn"``      — a model turn. Primary content; every
                        column is populated.
    - ``"subagent"``  — dimmed footnote line attached to the turn
                        that spawned the subagent. Carries the
                        subtree's rollup cost in its ``cost`` cell
                        so the user can see how each parent turn
                        decomposes without drilling in.
    - ``"nonturn"``   — an interleaved timeline entry: user
                        message, slash command, compaction, etc.
                        Token columns are blank.
    """

    kind: str
    num: str = ""
    when: str = ""
    took: str = ""
    cost: str = ""
    own: str = ""
    carry: str = ""
    caused: str = ""
    what: str = ""
    ctx: str = ""
    model: str = ""
    in_tokens: str = ""
    out: str = ""
    cache_r: str = ""
    cache_w: str = ""
    # Retained so future sort/filter/modal work can key off the
    # structured data without re-parsing.
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------

def build_detail_rows(parsed: dict, tree: list[dict]) -> list[DetailRow]:
    """Produce the ordered row stream for one session's detail screen.

    Walks the parser's ``rows`` list in natural transcript order
    (turns and non-turn timeline events interleaved) and, after
    each turn row that spawned direct subagents, emits one dimmed
    subagent footnote row per child.

    Args:
        parsed: Result of :func:`claude_usage_tui.parse.parse_transcript`
            for the main session.
        tree: Result of :func:`claude_usage_tui.parse.build_agent_tree`
            — list of direct subagent nodes. Grandchildren are
            reachable by drilling into their parent's detail
            screen, not by recursing inline (see ``docs/tui-ux.md``).

    Returns:
        Ordered list of :class:`DetailRow` ready to feed into a
        Textual ``DataTable``.
    """
    source_rows = parsed.get("rows") or parsed.get("turns", [])
    if not source_rows:
        return []

    t0 = _first_ts(source_rows)
    caused_by_turn = compute_caused_by_turn(source_rows)
    children_by_turn = _children_by_call_turn(tree)

    out: list[DetailRow] = []
    prev_turn: dict | None = None
    for r in source_rows:
        if r.get("kind") == "turn":
            children = children_by_turn.get(r["turn_num"], [])
            out.append(_turn_row(r, t0, caused_by_turn, prev_turn, children))
            for child in children:
                out.append(_subagent_row(child, t0))
            prev_turn = r
        else:
            out.append(_nonturn_row(r, t0))
    return out


# ---------------------------------------------------------------------------
# Row construction — turns
# ---------------------------------------------------------------------------

def _turn_row(
    t: dict,
    t0: float,
    caused_by_turn: dict[int, float],
    prev_turn: dict | None,
    children: list[dict],
) -> DetailRow:
    own_self = turn_own_seq(t)
    inherit = turn_inherit_seq(t)
    # Parent turn's `own` / `cost` deliberately roll up the
    # subtree cost of every subagent this turn spawned (see
    # docs/tui-ux.md — "Accounting"). This keeps sort-by-cost
    # honest: an expensive-because-of-subagents turn bubbles up
    # to the top instead of hiding behind its cheap self-cost.
    # The subagent footnote rows below still surface each
    # subagent's individual contribution for visual breakdown.
    sub_seq = sum(_node_subtree_seq(c) for c in children)
    own = own_self + sub_seq
    cost = own + inherit
    caused = caused_by_turn.get(t["turn_num"], 0.0)

    return DetailRow(
        kind="turn",
        num=str(t["turn_num"]),
        when=_fmt_trel(t["ts"] - t0) if t.get("ts") is not None else "",
        took=_fmt_took(t, prev_turn),
        cost=short_tokens(round(cost)),
        own=short_tokens(round(own)),
        carry=short_tokens(round(inherit)),
        caused=_fmt_caused(caused),
        what=turn_label(t),
        ctx=short_tokens(t.get("ctx", 0)),
        model=tiny_model(t.get("model", "")),
        in_tokens=short_tokens(t.get("in_tokens", 0)),
        out=short_tokens(t.get("out_tokens", 0)),
        cache_r=short_tokens(t.get("cache_r", 0)),
        cache_w=short_tokens(t.get("cache_w", 0)),
        raw={"turn": t},
    )


def _subagent_row(child: dict, t0: float) -> DetailRow:
    """Render one direct subagent as a dimmed footnote under its parent.

    ``t0`` is the parent session's first-event timestamp, so the
    ``when`` column lines up with the surrounding turn rows — a
    subagent spawned at ``0:42`` reads as "0:42" in its footnote,
    not the absolute epoch.
    """
    cid = short_agent_id(child.get("path", ""))
    subtree_seq = _node_subtree_seq(child)
    desc = (
        child.get("call_description")
        or (child.get("meta") or {}).get("description", "")
        or ""
    )
    turn_count = child.get("turn_count", 0)
    tool_name = child.get("call_tool") or "↳"
    label_parts = [f"[{tool_name}]"]
    if desc:
        label_parts.append(f'"{desc}"')
    if turn_count:
        label_parts.append(f"— {turn_count} turn{'s' if turn_count != 1 else ''}")
    label = " ".join(label_parts)

    sub_rows = child.get("rows") or child.get("turns") or []
    took_str = _fmt_subagent_took(sub_rows)
    when_str = _fmt_subagent_when(sub_rows, t0)

    dominant = _dominant_model_tokens(child.get("tokens_by_model") or {})

    return DetailRow(
        kind="subagent",
        num=f"↳{cid}",
        when=when_str,
        took=took_str,
        cost=short_tokens(round(subtree_seq)),
        what=label,
        ctx=short_tokens(child.get("peak_context_tokens", 0)),
        model=tiny_model(dominant),
        raw={"subagent": child, "subtree_seq": subtree_seq},
    )


def _nonturn_row(r: dict, t0: float) -> DetailRow:
    ts = r.get("ts")
    return DetailRow(
        kind="nonturn",
        when=_fmt_trel(ts - t0) if ts is not None else "",
        what=r.get("what") or "",
        raw={"nonturn": r},
    )


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------

def _children_by_call_turn(tree: list[dict]) -> dict[int, list[dict]]:
    """Group direct-child subagents by the parent turn that spawned them.

    Subagents without a matched ``call_turn`` are dropped from the
    inline rendering — they'll still be discoverable by future
    drill-in mechanics once navigation to an un-attributed
    subagent is worth adding.
    """
    by_turn: dict[int, list[dict]] = {}
    for node in tree:
        call_turn = node.get("call_turn")
        if call_turn is None:
            continue
        by_turn.setdefault(call_turn, []).append(node)
    return by_turn


def _node_subtree_seq(node: dict) -> float:
    """Recursive Sonnet-equivalent total for a subagent and its descendants."""
    total = 0.0
    tbm = node.get("tokens_by_model") or {}
    if tbm:
        total += model_aware_cost_breakdown(tbm)["total"]
    for child in node.get("children", []):
        total += _node_subtree_seq(child)
    return total


def _dominant_model_tokens(tokens_by_model: dict) -> str:
    best = ""
    best_total = -1
    for model, counts in tokens_by_model.items():
        total = sum(counts.get(k, 0) for k in counts)
        if total > best_total:
            best = model
            best_total = total
    return best


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------

def _first_ts(source_rows: list[dict]) -> float:
    for r in source_rows:
        if r.get("ts") is not None:
            return r["ts"]
    return 0.0


def _fmt_trel(secs: float | None) -> str:
    if secs is None or secs < 0:
        return ""
    secs = int(secs)
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fmt_secs(delta: float | None) -> str:
    if delta is None or delta < 1:
        return ""
    secs = int(delta)
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 10:
        return f"{m}m{s:02d}s" if s else f"{m}m"
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _fmt_took(turn: dict, prev_turn: dict | None) -> str:
    """Turn wallclock: pre-turn wait + this turn's work.

    Mirrors the text CLI's ``took`` column — see
    :func:`claude_usage_tui.plain.turns_table._fmt_time` for the
    original treatment and rationale.
    """
    ts = turn.get("ts")
    if ts is None:
        return ""
    end = turn.get("end_ts") or ts
    tool_end = turn.get("last_tool_result_ts") or 0
    end = max(end, tool_end)
    if prev_turn is not None:
        prev_done = prev_turn.get("last_tool_result_ts")
        if prev_done is not None:
            ts = min(ts, prev_done)
    return _fmt_secs(end - ts)


def _fmt_subagent_took(sub_rows: list[dict]) -> str:
    timestamps = [
        r.get("ts") for r in sub_rows
        if r.get("ts") is not None and r.get("kind") == "turn"
    ]
    if len(timestamps) < 2:
        return ""
    return _fmt_secs(max(timestamps) - min(timestamps))


def _fmt_subagent_when(sub_rows: list[dict], t0: float) -> str:
    """When the subagent fired its first turn, relative to the
    parent session's ``t0``.

    Reads as a continuation of the parent's timeline — a subagent
    that started 0:42 into the main session shows "0:42" in its
    footnote, same format as the turn rows around it.
    """
    for r in sub_rows:
        ts = r.get("ts")
        if ts is not None:
            return _fmt_trel(ts - t0)
    return ""


def _fmt_caused(seq: float) -> str:
    if not seq:
        return ""
    return f"+{round(seq / 1000):.0f}K"
