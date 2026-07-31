"""Build DataTable rows for the session detail screen.

Deliberately not shared with :mod:`claude_measure_usage.plain.turns_table`.
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
# Cost breakdown
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TurnCostBreakdown:
    """Everything the modal needs to explain a turn's total cost.

    Splits the parent turn's ``own`` figure into its ``self`` and
    ``subagents`` components so the modal can render:

        Cost:     180.4K   (= own 176.2K + carry 4.2K + caused +1K)
          own decomposes as:
            self           2.9K
            subagents    173.3K

    ``cost = own + inherit`` matches the Cost cell in the table.
    ``own = self + subagents`` keeps the parent row's own figure
    honest for sort-by-cost without hiding the breakdown.
    ``caused`` is a forward projection — not part of ``cost``.
    """

    self_seq: float
    subagents_seq: float
    own: float
    inherit: float
    cost: float
    caused: float


def turn_cost_breakdown(
    turn: dict,
    children: list[dict],
    caused_seq: float,
) -> TurnCostBreakdown:
    """Compute the per-turn cost decomposition shown in the modal.

    Kept as a standalone helper so the table row builder and the
    modal agree on the math by construction — if Cost changes, it
    changes in one place.
    """
    self_seq = turn_own_seq(turn)
    subagents_seq = sum(_node_subtree_seq(c) for c in children)
    own = self_seq + subagents_seq
    inherit = turn_inherit_seq(turn)
    cost = own + inherit
    return TurnCostBreakdown(
        self_seq=self_seq,
        subagents_seq=subagents_seq,
        own=own,
        inherit=inherit,
        cost=cost,
        caused=caused_seq,
    )


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
        parsed: Result of :func:`claude_measure_usage.parse.parse_transcript`
            for the main session.
        tree: Result of :func:`claude_measure_usage.parse.build_agent_tree`
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
    # Parent turn's `own` / `cost` deliberately roll up the
    # subtree cost of every subagent this turn spawned (see
    # docs/tui-ux.md — "Accounting"). This keeps sort-by-cost
    # honest: an expensive-because-of-subagents turn bubbles up
    # to the top instead of hiding behind its cheap self-cost.
    # The subagent footnote rows below still surface each
    # subagent's individual contribution for visual breakdown.
    caused = caused_by_turn.get(t["turn_num"], 0.0)
    breakdown = turn_cost_breakdown(t, children, caused)
    own = breakdown.own
    cost = breakdown.cost
    inherit = breakdown.inherit
    took_secs = _turn_span_secs(t, prev_turn)

    return DetailRow(
        kind="turn",
        num=str(t["turn_num"]),
        when=_fmt_trel(t["ts"] - t0) if t.get("ts") is not None else "",
        took=_fmt_secs(took_secs),
        cost=short_tokens(round(cost)),
        own=short_tokens(round(own)),
        carry=short_tokens(round(inherit)),
        caused=_fmt_caused(caused),
        what=turn_label(t),
        ctx=short_tokens(t.get("ctx", 0)),
        model=tiny_model(t.get("model", "")),
        in_tokens=short_tokens(t.get("in_tokens", 0)),
        # "≈" marks output estimated from content length because the
        # transcript never recorded this turn's final usage.
        out=("≈" if t.get("out_estimated") else "")
        + short_tokens(t.get("out_tokens", 0)),
        cache_r=short_tokens(t.get("cache_r", 0)),
        cache_w=short_tokens(t.get("cache_w", 0)),
        # Stash the primitives the modal needs so it doesn't
        # have to re-walk the tree to compute the decomposition.
        # ``sort_keys`` lets the glued-sort logic rank turn blocks
        # by numeric values instead of re-parsing the formatted
        # cell strings. Every entry in :data:`SORT_MODES` with a
        # non-None sort key must be represented here.
        raw={
            "turn": t,
            "children": children,
            "breakdown": breakdown,
            "sort_keys": {
                "took": took_secs,
                "cost": cost,
                "own": own,
                "carry": inherit,
                "caused": caused,
            },
        },
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


# ---------------------------------------------------------------------------
# Glued sort
# ---------------------------------------------------------------------------

# Mode ids -> column keys used in DetailRow.raw["sort_keys"]. The
# UI surfaces these human-readable labels; the cycle binding walks
# through them in this order.
@dataclass(frozen=True)
class SortMode:
    """One entry in the sort cycle.

    - ``id`` — stable short name used in state and sub_title.
    - ``sort_key`` — key in :func:`DetailRow.raw["sort_keys"]`
      used to rank turn blocks. ``None`` for natural order
      (no reordering).
    - ``column_id`` — id of the DataTable column that represents
      this sort; used for the ▼ indicator that highlights the
      active column. "natural" highlights the ``#`` column
      because that's the visual anchor for transcript order.
    """

    id: str
    sort_key: str | None
    column_id: str


# Cycle ORDER follows the visual column order in the detail
# table: # → took → cost → own → carry → caused. Columns that
# don't have a meaningful sort (``when``, ``what``, ``ctx``,
# ``model``, raw token counts) are skipped with gaps. Pressing
# ``s`` walks these in order and wraps back to natural.
SORT_MODES: list[SortMode] = [
    SortMode("natural", None, "num"),
    SortMode("took", "took", "took"),
    SortMode("cost", "cost", "cost"),
    SortMode("own", "own", "own"),
    SortMode("carry", "carry", "carry"),
    SortMode("caused", "caused", "caused"),
]


def get_sort_mode(mode_id: str) -> SortMode:
    """Return the :class:`SortMode` for an id, or natural as fallback."""
    for m in SORT_MODES:
        if m.id == mode_id:
            return m
    return SORT_MODES[0]


def sort_mode_for_column(column_id: str) -> SortMode | None:
    """Return the :class:`SortMode` whose column_id matches, or None.

    Used by the header-click handler: clicks on a non-sortable
    column (``when``, ``what``, raw counts, ...) return ``None``
    and become no-ops.
    """
    for m in SORT_MODES:
        if m.column_id == column_id:
            return m
    return None


def sort_rows(
    rows: list[DetailRow],
    mode: str,
) -> list[DetailRow]:
    """Reorder a detail-row stream under the glued-sort rules.

    Non-``"natural"`` modes reorder turn rows by their numeric
    sort key while keeping the spawn relationship intact:

    - **Glue**: every subagent footnote row stays immediately
      after its spawning turn. A row block is one turn row plus
      its trailing subagents.
    - **Anchored non-turns**: a non-turn row that precedes a
      turn is treated as the turn's "lead-in" (user prompt,
      attachment, permission change) and travels with the turn
      when it moves. This preserves the "this event led to this
      turn" reading order.
    - **Orphan tail**: non-turn rows that follow the last turn
      with no further turn to attach to (compact boundaries,
      trailing slash commands) stay pinned at the end in natural
      order, regardless of sort mode — they're session-level
      markers, not per-turn data.

    ``mode == "natural"`` returns the input unchanged (no copy).
    Unknown modes also pass through unchanged so an accidental
    bad mode id never loses data.
    """
    if mode == "natural":
        return rows
    sort_mode = get_sort_mode(mode)
    if sort_mode.sort_key is None:
        return rows
    key_name = sort_mode.sort_key
    blocks, tail = _group_blocks(rows)

    def block_key(block: list[DetailRow]) -> float:
        turn = next(r for r in block if r.kind == "turn")
        sort_keys = turn.raw.get("sort_keys") or {}
        return -float(sort_keys.get(key_name, 0.0))

    blocks.sort(key=block_key)
    out: list[DetailRow] = []
    for block in blocks:
        out.extend(block)
    out.extend(tail)
    return out


def filter_rows(
    rows: list[DetailRow],
    text: str,
) -> list[DetailRow]:
    """Return the subset of ``rows`` whose blocks match ``text``.

    Substring match is case-insensitive (``str.casefold``) against
    each row's ``what`` and ``num`` fields. Match scope follows
    the glue rule from ``docs/tui-ux.md``:

    - A turn block (leading non-turns + turn + subagents) is
      shown in full when any of its rows matches — so a
      ``tool:bash`` filter still shows the user message that
      prompted the turn, plus every subagent the turn spawned,
      without any of them having to individually match.
    - Orphan tail rows match individually — they're not tied to
      any turn, so there's no block to drag along.

    Empty ``text`` returns the input unchanged.
    """
    if not text:
        return rows
    needle = text.casefold()
    blocks, tail = _group_blocks(rows)
    out: list[DetailRow] = []
    for block in blocks:
        if any(_row_matches(r, needle) for r in block):
            out.extend(block)
    for r in tail:
        if _row_matches(r, needle):
            out.append(r)
    return out


def _row_matches(row: DetailRow, needle: str) -> bool:
    """True if ``needle`` appears in the row's label or number column."""
    if needle in (row.what or "").casefold():
        return True
    if needle in (row.num or "").casefold():
        return True
    return False


def _group_blocks(
    rows: list[DetailRow],
) -> tuple[list[list[DetailRow]], list[DetailRow]]:
    """Split a row stream into (turn blocks, orphan tail).

    Each turn block is a list of ``DetailRow`` objects consisting
    of: zero or more leading non-turn rows + one turn row + zero
    or more trailing subagent footnote rows. The orphan tail
    holds any non-turn rows that trail after the last turn.
    """
    blocks: list[list[DetailRow]] = []
    lead_buffer: list[DetailRow] = []
    current: list[DetailRow] | None = None

    for r in rows:
        if r.kind == "turn":
            block = lead_buffer + [r]
            blocks.append(block)
            current = block
            lead_buffer = []
        elif r.kind == "subagent" and current is not None:
            current.append(r)
        else:  # nonturn (or unclassified) — buffer for the next turn
            lead_buffer.append(r)
            current = None
    # Anything left in the buffer is a tail orphan. These stay
    # pinned at the end because they have no turn to attach to.
    return blocks, lead_buffer


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


def _turn_span_secs(turn: dict, prev_turn: dict | None) -> float:
    """Wallclock span for a turn, in seconds.

    Spans from the end of the previous turn's tool execution to
    the end of this turn's own work, so the interval bundles
    pre-turn API wait with this turn's generation + tools. See
    :func:`claude_measure_usage.plain.turns_table._fmt_time` for the
    original treatment and rationale.

    Returns 0 when there's no meaningful span (first turn, no
    tools on the previous turn, missing timestamps).
    """
    ts = turn.get("ts")
    if ts is None:
        return 0.0
    end = turn.get("end_ts") or ts
    tool_end = turn.get("last_tool_result_ts") or 0
    end = max(end, tool_end)
    start = ts
    if prev_turn is not None:
        prev_done = prev_turn.get("last_tool_result_ts")
        if prev_done is not None:
            start = min(ts, prev_done)
    return max(0.0, end - start)


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
