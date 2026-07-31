"""Render per-turn token usage tables for a session and its subagents.

The report is a text document with:

- A legend line explaining column units (raw tokens vs. Seq).
- A main session table.
- One table per subagent (depth-first), each anchored by a short ↳id
  that also appears in the "sub" column of the parent's table row that
  spawned it, so users can Ctrl-F between them.

All columns except ``Seq`` are raw transcript token counts. ``Seq``
is Sonnet-equivalent tokens and rolls up any descendant subagents
spawned by the turn — so a row whose ``what`` reads ``[Agent] …``
shows its own cost plus everything the subagent did.
"""

from .display import _fmt_k, _tiny_model
from ..metrics import (
    compute_caused_by_turn,
    model_aware_cost_breakdown,
    turn_inherit_seq,
    turn_own_seq,
    turn_seq,
)
from ..turns_label import short_agent_id, turn_label


# Column layout for the per-turn table.
#
# Left block — turn anchor + normalized cost + label:
#   #, t+, Seq, what
# Middle block — timing and context size:
#   time, ctx
# Separator (a literal │ column) visually marks the boundary.
# Right block — raw per-turn tokens by category plus the model family,
# which together explain HOW the Seq total was derived:
#   model, in, out, cache_r, cache_w
_COLUMNS = [
    ("#", "#", "r"),
    ("when", "when", "r"),
    ("took", "took", "r"),
    ("__sep1__", "│", "l"),
    ("Tokens", "tokens", "r"),
    ("own", "= own", "r"),
    ("inherit", "+ carry", "r"),
    ("__sep2__", "│", "l"),
    ("caused", "caused", "r"),
    ("what", "what", "l"),
    ("ctx", "ctx", "r"),
    ("__sep__", "│", "l"),
    ("model", "mdl", "l"),
    ("in", "in", "r"),
    ("out", "out", "r"),
    ("cache_r", "cache_r", "r"),
    ("cache_w", "cache_w", "r"),
]


def render_turns_report(main_parsed, tree):
    """Render the full per-turn report.

    Args:
        main_parsed: result of parse_transcript() for the main session.
        tree: result of build_agent_tree() — list of subagent node dicts.

    Returns:
        A multi-line string ready to print.
    """
    lines = [_legend(), ""]
    lines.extend(_render_main_section(main_parsed, tree))

    for node in _walk_tree(tree):
        lines.append("")
        lines.extend(_render_subagent_section(node))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section rendering
# ---------------------------------------------------------------------------

def _render_main_section(main_parsed, tree):
    """Render the main session header and table."""
    total_seq_own = _model_aware_total(main_parsed.get("tokens_by_model", {}))
    subtree_seq = total_seq_own + sum(_node_subtree_seq(n) for n in tree)
    header = _format_main_header(main_parsed, total_seq_own, subtree_seq)
    rows = _build_rows(
        main_parsed.get("rows") or main_parsed.get("turns", []),
        _children_by_call_turn(tree),
    )
    return [header, ""] + _render_table(rows)


def _render_subagent_section(node):
    """Render one subagent's header and table."""
    own_seq = _model_aware_total(node.get("tokens_by_model", {}))
    subtree_seq = own_seq + sum(
        _node_subtree_seq(c) for c in node.get("children", [])
    )
    header = _format_subagent_header(node, own_seq, subtree_seq)
    rows = _build_rows(
        node.get("rows") or node.get("turns", []),
        _children_by_call_turn(node.get("children", [])),
    )
    return [header, ""] + _render_table(rows)


def _format_main_header(main_parsed, own_seq, subtree_seq):
    turn_count = main_parsed.get("turn_count", 0)
    parts = [
        f"Main session  —  {turn_count} turns",
        _format_tokens_label(own_seq, subtree_seq),
    ]
    return "  —  ".join(parts)


def _format_subagent_header(node, own_seq, subtree_seq):
    cid = short_agent_id(node.get("path", ""))
    desc = node.get("call_description") or node.get("meta", {}).get("description", "") or ""
    parent = node.get("call_turn")
    turn_count = node.get("turn_count", 0)

    left = f"↳{cid}"
    if desc:
        left += f'  "{desc}"'

    right_parts = []
    if parent is not None:
        right_parts.append(f"parent turn {parent}")
    right_parts.append(f"{turn_count} turns")
    right_parts.append(_format_tokens_label(own_seq, subtree_seq))

    return f"{left}  —  " + "  —  ".join(right_parts)


def _format_tokens_label(own_seq, subtree_seq):
    """Render a section's Tokens figure.

    When the section spawned subagents, shows the total with an
    own/subagents decomposition: ``Tokens 5.6M (2.4M own + 3.2M
    subagents)``. For leaf sections (no descendants) shows just
    the single number so the decomposition isn't offered when
    there's nothing to decompose.
    """
    own_rounded = round(own_seq)
    subtree_rounded = round(subtree_seq)
    if subtree_rounded <= own_rounded:
        return f"Tokens {_fmt_k(own_rounded)}"
    sub_rounded = subtree_rounded - own_rounded
    return (
        f"Tokens {_fmt_k(subtree_rounded)} "
        f"({_fmt_k(own_rounded)} own + {_fmt_k(sub_rounded)} subagents)"
    )


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------

def _build_rows(source_rows, call_turn_to_children):
    """Turn parser rows into list-of-dict rows ready for the table renderer.

    ``source_rows`` is heterogeneous: items with ``kind == "turn"`` are
    model turns and contribute to the totals; any other kind is a
    non-turn timeline row (user message, system event, etc.) and gets
    rendered with blank token columns.

    The ``time`` column on each turn row is a single wallclock span:
    ``(this_turn.end_of_work) - (prev_turn.last_tool_result_ts)``.
    That bundles the gap — the API round-trip latency the previous
    row's tool_result triggered — with this row's own work. Reads
    naturally as "how long did I wait for this turn to arrive, and
    then how long did it take". When the previous turn had no tools
    (or there is no previous turn), ``time`` is just this row's own
    duration — there's no meaningful "wait" to attribute.

    The first turn of each section carries a ``(startup)`` marker
    in its label to warn that its own/inherit/caused numbers include
    session bootstrap overhead (system prompt, tool schemas,
    CLAUDE.md, initial skill/attachment loads) — none of which are
    visible in the transcript as their own entries. Turn 1's numbers
    should be read as "your work plus the session floor", not
    "optimizable work alone".
    """
    if not source_rows:
        return []

    t0 = _first_ts(source_rows)
    # Precompute caused_seq per turn — it depends on how many turns
    # remain in the same compaction epoch, so it needs a full pass.
    caused_by_turn = compute_caused_by_turn(source_rows)

    rows = []
    totals = {
        "in": 0, "out": 0, "cache_r": 0, "cache_w": 0, "seq": 0.0,
        "own": 0.0, "inherit": 0.0, "caused": 0.0, "took": 0.0,
    }

    prev_turn = None
    first_turn_seen = False
    for r in source_rows:
        if r.get("kind") == "turn":
            is_first = not first_turn_seen
            first_turn_seen = True
            rows.append(
                _format_turn_row(
                    r, t0, call_turn_to_children, totals, prev_turn,
                    caused_by_turn, is_first,
                )
            )
            prev_turn = r
        else:
            rows.append(_format_nonturn_row(r, t0))

    rows.append(_totals_row(totals))
    return rows


def _first_ts(source_rows):
    """Return the earliest timestamp in the row stream.

    Used as the anchor for the ``t+`` column. We pick the earliest
    timestamped row (not necessarily the first model turn), so
    pre-turn events like the initial user prompt show at ``0:00``
    instead of being squashed to empty with a negative delta.
    """
    for r in source_rows:
        if r.get("ts") is not None:
            return r["ts"]
    return 0


def _format_turn_row(t, t0, call_turn_to_children, totals, prev_turn, caused_by_turn, is_first):
    """Render a model turn, accumulating into ``totals`` in place.

    Cost decomposition:

    - ``Seq`` — marginal cost of making this turn. Everything the
      turn consumed: ``own + inherit + subagent rollup``.
    - ``own`` — developer-actionable part: what the turn produced
      (out + cache_w + in) plus any subagents it spawned. Shrink
      this by making the turn more efficient.
    - ``inherit`` — what the turn paid to read inherited context
      (cache_r). Shrink this by making *earlier* turns leaner.
    - ``caused`` — projected downstream cost: what subsequent turns
      in the same compaction epoch will pay to read this turn's
      cache_w contribution. Forward-attributed; not part of Seq.
    """
    own_self_seq = turn_own_seq(t)
    inherit_seq = turn_inherit_seq(t)

    children = call_turn_to_children.get(t["turn_num"], [])
    sub_seq = sum(c_seq for _, c_seq, _ in children)

    own_seq = own_self_seq + sub_seq
    merged_seq = own_seq + inherit_seq
    caused_seq = caused_by_turn.get(t["turn_num"], 0.0)

    label = turn_label(t)
    if children:
        label = _append_child_ids(label, children)
    if is_first:
        # First turn bakes in the session's startup overhead —
        # system prompt, tool schemas, CLAUDE.md, initial skill
        # loads — which have no other representation in the
        # transcript. Mark the row with a prefix so readers know
        # its own/carry/caused numbers aren't purely developer-
        # actionable. Prefix (not suffix) so the marker is never
        # truncated when the label is long.
        label = f"(startup) {label}" if label else "(startup)"

    totals["in"] += t["in_tokens"]
    totals["out"] += t["out_tokens"]
    totals["cache_r"] += t["cache_r"]
    totals["cache_w"] += t["cache_w"]
    totals["seq"] += merged_seq
    totals["own"] += own_seq
    totals["inherit"] += inherit_seq
    totals["caused"] += caused_seq
    # Time total = sum of each turn's span from max(prev tool end, ts)
    # through this turn's own work end. Same interval _fmt_time renders.
    end = _turn_end(t)
    start = _turn_effective_start(t, prev_turn)
    totals["took"] += max(0.0, end - start)

    return {
        "_is_turn": True,
        "#": str(t["turn_num"]),
        "when": _fmt_trel(t["ts"] - t0) if t.get("ts") is not None else "",
        "took": _fmt_time(t, prev_turn),
        "model": _tiny_model(t.get("model", "")),
        "in": _fmt_raw(t["in_tokens"]),
        "out": _fmt_raw(t["out_tokens"]),
        "cache_r": _fmt_raw(t["cache_r"], dash_zero=True),
        "cache_w": _fmt_raw(t["cache_w"], dash_zero=True),
        "ctx": _fmt_raw(t["ctx"]),
        "Tokens": _fmt_raw(round(merged_seq)),
        "own": _fmt_raw(round(own_seq)),
        "inherit": _fmt_raw(round(inherit_seq)),
        "caused": _fmt_caused(caused_seq),
        "what": label,
    }


def _format_nonturn_row(r, t0):
    """Render a non-turn timeline row with blank token columns."""
    ts = r.get("ts")
    return {
        "#": "",
        "when": _fmt_trel(ts - t0) if ts is not None else "",
        "took": "",
        "model": "",
        "in": "",
        "out": "",
        "cache_r": "",
        "cache_w": "",
        "ctx": "",
        "Tokens": "",
        "own": "",
        "inherit": "",
        "caused": "",
        "what": r.get("what") or "",
    }


def _totals_row(totals):
    # caused is a forward projection, not a direct cost, so summing
    # it across rows isn't meaningful — it would double-count tokens
    # against the same future reads. Leave it blank in the totals row.
    return {
        "#": "",
        "when": "",
        "took": _fmt_secs(totals["took"]),
        "model": "total",
        "in": _fmt_raw(totals["in"]),
        "out": _fmt_raw(totals["out"]),
        "cache_r": _fmt_raw(totals["cache_r"]),
        "cache_w": _fmt_raw(totals["cache_w"]),
        "ctx": "",
        "Tokens": _fmt_raw(round(totals["seq"])),
        "own": _fmt_raw(round(totals["own"])),
        "inherit": _fmt_raw(round(totals["inherit"])),
        "caused": "",
        "what": "",
    }


def _append_child_ids(label, children):
    """Append short ↳ids for spawned subagents to a turn's label."""
    if len(children) <= 3:
        ids = " ".join(f"↳{cid}" for cid, _, _ in children)
    else:
        ids = f"{len(children)} subagents"
    if label:
        return f"{label}  [{ids}]"
    return f"[{ids}]"


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------

def _walk_tree(nodes):
    """Yield every node in the tree, depth-first."""
    for node in nodes:
        yield node
        yield from _walk_tree(node.get("children", []))


def _children_by_call_turn(direct_children):
    """Group direct subagent children by the turn that spawned them.

    Returns dict: call_turn -> list of (short_id, subtree_seq, description).
    Children without a matched call_turn are skipped (they'll still render
    their own table, they just can't be attributed to a main-table row).
    """
    result = {}
    for child in direct_children:
        call_turn = child.get("call_turn")
        if call_turn is None:
            continue
        cid = short_agent_id(child.get("path", ""))
        seq = _node_subtree_seq(child)
        desc = child.get("call_description") or child.get("meta", {}).get("description", "")
        result.setdefault(call_turn, []).append((cid, seq, desc))
    return result


def _node_subtree_seq(node):
    """Sonnet-equivalent Seq for a node and all of its descendants."""
    total = _model_aware_total(node.get("tokens_by_model", {}))
    for child in node.get("children", []):
        total += _node_subtree_seq(child)
    return total


def _model_aware_total(tokens_by_model):
    if not tokens_by_model:
        return 0.0
    return model_aware_cost_breakdown(tokens_by_model)["total"]


# ---------------------------------------------------------------------------
# Table layout
# ---------------------------------------------------------------------------

def _render_table(rows):
    """Format data rows with aligned columns.

    Drops the ``time`` column when no row carries a value for it.
    """
    if not rows:
        return ["  (no turns)"]

    cols = [c for c in _COLUMNS if _column_has_data(c[0], rows)]
    widths = _column_widths(cols, rows)

    lines = []
    header_row = {k: h for k, h, _ in cols}
    lines.append(_format_row(cols, widths, header_row))
    lines.append(_divider(cols, widths))
    data_rows = rows[:-1]
    for row in data_rows:
        lines.append(_format_row(cols, widths, row))
    lines.append(_divider(cols, widths))
    lines.append(_format_row(cols, widths, rows[-1]))
    return lines


def _column_has_data(key, rows):
    if key in ("took", "own", "inherit", "caused"):
        return any((row.get(key) or "") for row in rows)
    return True


def _column_widths(cols, rows):
    widths = {}
    for key, header, _ in cols:
        w = len(header)
        for row in rows:
            w = max(w, len(row.get(key, "")))
        widths[key] = w
    return widths


def _format_row(cols, widths, row, is_header=False):
    """Assemble a row with tighter spacing in selected blocks.

    Columns are joined with a 2-space gap by default. Tight
    single-space gaps are used inside two blocks:

    - The cost-decomposition block ``tokens = own + carry``, so the
      header reads as a formula rather than three disjoint labels.
    - The raw-tokens block ``mdl in out cache_r cache_w``, so narrow
      numeric columns don't feel wastefully padded on common rows.

    Pillar separators (``__sep*__``) always have 1-space gaps on
    both sides.
    """
    tight_keys = {
        # cost decomposition
        "Tokens", "own", "inherit",
        # raw-tokens block
        "model", "in", "out", "cache_r", "cache_w",
    }

    pieces = []
    for idx, (key, header, align) in enumerate(cols):
        is_sep = key.startswith("__sep")
        if is_sep:
            cell = "│"
        else:
            val = row.get(key, "")
            if key == "what" and row.get("_is_turn"):
                cell = _pad_with_leaders(val, widths[key])
            elif align == "l":
                cell = f"{val:<{widths[key]}}"
            else:
                cell = f"{val:>{widths[key]}}"

        prev_is_sep = idx > 0 and cols[idx - 1][0].startswith("__sep")
        if idx == 0:
            gap = ""
        elif is_sep or prev_is_sep or key in tight_keys:
            gap = " "
        else:
            gap = "  "
        pieces.append(gap + cell)

    return "".join(pieces).rstrip()


def _pad_with_leaders(content, width):
    """Pad a left-aligned cell with dot leaders that end at a fixed
    column regardless of content length.

    Shorter labels → longer dot runs; longer labels → shorter dot
    runs. The last dot always sits at ``width - 1`` so the eye has a
    consistent landing point — the 2-space column separator provides
    breathing room before the next column.

    Structure: ``<content><spacer><dots>``. Spacer is 2 characters
    of breathing room after the content. Dots are placed at every
    even position from the right end so the last one lands flush
    against the column boundary.

    Shorter cells that can't fit at least one dot fall back to plain
    space padding.
    """
    if not content:
        return " " * width
    pad = width - len(content)
    if pad < 4:
        return content + " " * pad
    leader_len = pad - 2  # 2-space spacer before the dots
    strip_chars = []
    for i in range(leader_len):
        from_right = leader_len - 1 - i
        strip_chars.append("·" if from_right % 2 == 0 else " ")
    return content + "  " + "".join(strip_chars)


def _divider(cols, widths):
    """Divider matches the gap pattern used by ``_format_row``."""
    tight_keys = {
        "Tokens", "own", "inherit",
        "model", "in", "out", "cache_r", "cache_w",
    }
    total = 0
    for idx, (key, _, _) in enumerate(cols):
        total += widths[key]
        if idx == 0:
            continue
        is_sep = key.startswith("__sep")
        prev_is_sep = cols[idx - 1][0].startswith("__sep")
        if is_sep or prev_is_sep or key in tight_keys:
            total += 1
        else:
            total += 2
    return "─" * total


# ---------------------------------------------------------------------------
# Formatting primitives
# ---------------------------------------------------------------------------

def _legend():
    return (
        "Tokens (and own/inherit/caused) are Sonnet input-equivalent, "
        "normalized across token type and model. "
        "in/out/cache_r/cache_w on the right are raw transcript counts."
    )


def _fmt_raw(tokens, dash_zero=False):
    """Render a raw token count compactly."""
    if tokens == 0:
        return "—" if dash_zero else "0"
    return _fmt_k(tokens)


def _fmt_caused(seq):
    """Render a caused-cost figure with a leading ``+`` prefix.

    Always rendered in K units for consistency — caused is a
    projection, not a measurement, so visual uniformity matters
    more than precision at the high end. The ``+`` marks it as a
    forward-attributed projection (downstream cost this turn will
    trigger, not part of the turn's own Seq total).
    """
    if not seq:
        return ""
    return f"+{round(seq / 1000):.0f}K"


def _fmt_trel(secs):
    """Elapsed time relative to a session's first turn: m:ss or h:mm:ss."""
    if secs is None or secs < 0:
        return ""
    secs = int(secs)
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fmt_time(turn, prev_turn):
    """Render a single wallclock figure for how long this turn took.

    Spans from the end of the previous turn's tool execution to the
    end of this turn's own work. That bundles two things:

    - **Pre-turn wait** — API round-trip latency triggered by the
      previous turn's tool_result. When the previous turn had no
      tools (or there is no previous turn), this interval is not
      included — there's no meaningful "wait" to attribute to the
      transition, since any delay is usually idle time between
      user prompts.
    - **This turn's work** — model generation + tool execution,
      from the first JSONL entry for this turn to the last
      tool_result (or the last content block if no tools fired).

    The resulting single number reads as "wallclock cost attributable
    to this turn". Summing across turn rows approximates the total
    agentic wallclock, excluding idle between user prompts.
    """
    ts = turn.get("ts")
    if ts is None:
        return ""
    end = _turn_end(turn)
    start = _turn_effective_start(turn, prev_turn)
    return _fmt_secs(end - start)


def _turn_end(turn):
    """End-of-work timestamp for a turn: max of its last content block
    and its last tool_result."""
    ts = turn["ts"]
    end_ts = turn.get("end_ts") or ts
    tool_end = turn.get("last_tool_result_ts") or 0
    return max(end_ts, tool_end)


def _turn_effective_start(turn, prev_turn):
    """Effective start of this turn's wallclock span.

    Normally the turn's own ``ts``. When the previous turn had
    tool_result(s), we extend the span backwards to the moment those
    tools finished, so the API round-trip latency that followed them
    is charged to *this* row instead of the previous one.
    """
    ts = turn["ts"]
    if prev_turn is None:
        return ts
    prev_done = prev_turn.get("last_tool_result_ts")
    if prev_done is None:
        return ts
    return min(ts, prev_done)


def _fmt_secs(delta):
    """Compact duration. Empty under 1s, precise under 1m, then
    progressively coarser:

      ``3s``      — under a minute: second precision.
      ``1m20s``   — 1–9 minutes: minute + second precision.
      ``12m``     — 10 minutes and up: minute precision only.
      ``2h05m``   — 1 hour and up: hour + minute precision.

    Keeps the widest plausible value to 7 chars (``59m``, ``23h59m``).
    """
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
