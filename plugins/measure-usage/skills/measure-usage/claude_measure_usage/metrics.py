"""Cost calculations, model normalization, and metrics computation."""

from .parse import (
    TOKEN_KEYS,
    merge_tokens_by_model,
    total_from_by_model,
    find_subagent_transcripts,
    parse_transcript,
    build_agent_tree,
    flatten_tree,
)

# Model input price relative to Sonnet ($3/M input tokens).
# Within-model ratios (output=5x, cache_read=0.1x, etc.) are consistent
# across models; only the base input price differs.
# Checked in order — more specific patterns first.
_MODEL_SCALES = [
    # Fable tier ($10/MTok input)
    ("fable", 10 / 3),  # claude-fable-5
    ("mythos", 10 / 3),  # claude-mythos-5 — same model/pricing as Fable
    # Legacy Opus ($15/MTok input)
    ("opus-4-1", 5.0),  # claude-opus-4-1-20250414
    ("3-opus", 5.0),  # claude-3-opus-20240229
    # Current / future Opus ($5/MTok input)
    ("opus", 5 / 3),
    # Sonnet — all versions ($3/MTok input)
    ("sonnet", 1.0),
    # Legacy Haiku ($0.80/MTok input)
    ("3-5-haiku", 0.267),  # claude-3-5-haiku-20241022
    ("3-haiku", 0.267),  # claude-3-haiku-20240307
    # Current / future Haiku ($1/MTok input)
    ("haiku", 1 / 3),
]


def _model_cost_scale(model_name):
    """Cost scale relative to Sonnet input price."""
    name = model_name.lower()
    for pattern, scale in _MODEL_SCALES:
        if pattern in name:
            return scale
    return 1.0


# ---------------------------------------------------------------------------
# Cost breakdown
# ---------------------------------------------------------------------------

COST_LABELS = {
    "cache_read_input_tokens": "Cache read",
    "input_tokens": "Input",
    "cache_5m": "Cache write (5m)",
    "cache_1h": "Cache write (1h)",
    "output_tokens": "Output",
}


def cost_breakdown(tokens):
    """Compute cost in input-equivalent tokens with percentage breakdown.

    Ratios (consistent across Claude models):
        cache read = 0.1x, input = 1x,
        cache write 5m = 1.25x, cache write 1h = 2.0x,
        output = 5x

    Uses per-tier cache costs when available, falls back to 1.25x
    for the aggregate cache_creation_input_tokens.
    """
    cache_5m = tokens.get("ephemeral_5m_input_tokens", 0)
    cache_1h = tokens.get("ephemeral_1h_input_tokens", 0)
    cache_total = tokens.get("cache_creation_input_tokens", 0)

    if cache_5m or cache_1h:
        cache_cost_5m = cache_5m * 1.25
        cache_cost_1h = cache_1h * 2.0
    else:
        cache_cost_5m = 0
        cache_cost_1h = 0
        if cache_total:
            cache_cost_5m = cache_total * 1.25

    costs = {
        "cache_read_input_tokens": tokens.get("cache_read_input_tokens", 0) * 0.1,
        "input_tokens": tokens.get("input_tokens", 0) * 1.0,
        "cache_5m": cache_cost_5m,
        "cache_1h": cache_cost_1h,
        "output_tokens": tokens.get("output_tokens", 0) * 5.0,
    }
    total = sum(costs.values())
    percentages = []
    for key in sorted(costs, key=lambda k: -costs[k]):
        pct = (costs[key] / total * 100) if total > 0 else 0
        if pct >= 1:
            percentages.append((COST_LABELS[key], pct))
    return {"total": total, "costs": costs, "percentages": percentages}


def model_aware_cost_breakdown(tokens_by_model):
    """Compute cost in Sonnet input-equivalent tokens, normalized across models.

    Returns per-category costs and total, all scaled to Sonnet pricing.
    """
    categories = {
        "cache_read": 0.0,
        "cache_write_5m": 0.0,
        "cache_write_1h": 0.0,
        "output": 0.0,
        "input": 0.0,
    }
    for model, tokens in tokens_by_model.items():
        scale = _model_cost_scale(model)
        mc = cost_breakdown(tokens)
        categories["cache_read"] += mc["costs"]["cache_read_input_tokens"] * scale
        categories["cache_write_5m"] += mc["costs"]["cache_5m"] * scale
        categories["cache_write_1h"] += mc["costs"]["cache_1h"] * scale
        categories["output"] += mc["costs"]["output_tokens"] * scale
        categories["input"] += mc["costs"]["input_tokens"] * scale

    total = sum(categories.values())
    return {"total": total, "categories": categories}


# ---------------------------------------------------------------------------
# Per-turn Seq
# ---------------------------------------------------------------------------


def turn_seq(row):
    """Sonnet-equivalent token count for one per-turn row.

    Uses the same multipliers as cost_breakdown() (cache_r×0.1, in×1,
    cache_w×1.25, out×5) scaled by the model's input-price ratio. Per-turn
    rows don't carry cache-tier splits, so cache_w is treated as 5m.

    Decomposition: ``turn_seq = turn_own_seq(row) + turn_inherit_seq(row)``
    (excluding subagent rollup, which is added separately by the
    renderer from the build_agent_tree result).
    """
    return turn_own_seq(row) + turn_inherit_seq(row)


def turn_own_seq(row):
    """Developer-actionable cost of this turn.

    Sonnet-equivalent tokens for what *this turn* put into motion:
    generated output, tool-call inputs (input_tokens), and content
    written into the cache (cache_w). Does NOT include cache_r — that
    represents inherited context the turn didn't choose to carry.

    Shrinking own_seq requires changing the turn itself: pick a cheaper
    model, write less, call smaller tools, trim tool results.

    When the row carries per-tier cache_w split (``cache_w_5m`` /
    ``cache_w_1h``), prices each tier at its own rate (1.25x and 2.0x).
    Falls back to a flat 1.25x on aggregate ``cache_w`` when tier data
    isn't available so callers that hand-build rows without tier
    splits still get a sensible number.
    """
    scale = _model_cost_scale(row.get("model", "unknown"))
    cache_w_5m = row.get("cache_w_5m", 0)
    cache_w_1h = row.get("cache_w_1h", 0)
    if cache_w_5m or cache_w_1h:
        cache_w_cost = cache_w_5m * 1.25 + cache_w_1h * 2.0
    else:
        cache_w_cost = row.get("cache_w", 0) * 1.25
    seq = row.get("in_tokens", 0) * 1.0 + cache_w_cost + row.get("out_tokens", 0) * 5.0
    return seq * scale


def turn_inherit_seq(row):
    """Cost this turn paid to read inherited context.

    Sonnet-equivalent tokens for ``cache_r``. Shrinking this requires
    changing *earlier* turns that put bloat into the cache — the
    current turn is mostly a passive reader.
    """
    scale = _model_cost_scale(row.get("model", "unknown"))
    return row.get("cache_r", 0) * 0.1 * scale


def projected_future_reads_seq(write_tokens, model, turns_remaining):
    """Flat projection of future cache_read cost caused by a write.

    Models the assumption that every one of ``turns_remaining`` subsequent
    turns will re-read ``write_tokens`` tokens at the cache-read rate
    (``0.1x``) scaled by the writer's model.

    Used by both ``turn_caused_seq`` (per-turn cache_w attribution) and
    ``compute_tool_costs`` (per-tool-invocation result attribution)
    to keep a single formula for downstream cost projection. ``turns_remaining``
    should respect compaction epoch boundaries when the caller has that
    information — tools that fired before a compaction don't get billed
    for turns after it, since the summary replaced their content.
    """
    if turns_remaining <= 0 or not write_tokens:
        return 0.0
    scale = _model_cost_scale(model)
    return write_tokens * 0.1 * scale * turns_remaining


def turn_caused_seq(row, turns_remaining_in_epoch):
    """Projected downstream cost caused by this turn's cache_w.

    Each subsequent turn in the same compaction epoch is assumed to
    pay ``cache_w × 0.1 × model_scale`` to read this turn's
    contribution. Compaction boundaries end the epoch: turns after
    the next boundary do not inherit this turn's cache_w (the
    summary replaced it).
    """
    return projected_future_reads_seq(
        row.get("cache_w", 0),
        row.get("model", "unknown"),
        turns_remaining_in_epoch,
    )


def compute_caused_by_turn(rows):
    """Return {turn_num: caused_seq} for all turn rows in ``rows``.

    Walks the row list in order, counting how many turns follow each
    one within the same compaction epoch (bounded by
    ``system:compact_boundary`` rows). Then applies ``turn_caused_seq``
    per turn. ``rows`` may be the ``parse_transcript`` output's row
    stream — non-turn rows are skipped for counting but do bound the
    epoch when their kind is ``system:compact_boundary``.
    """
    # Split row stream into epochs at compact_boundary markers.
    epochs = [[]]
    for r in rows:
        if r.get("kind") == "system:compact_boundary":
            epochs.append([])
            continue
        if r.get("kind") == "turn":
            epochs[-1].append(r)

    caused = {}
    for epoch in epochs:
        n = len(epoch)
        for i, turn in enumerate(epoch):
            turns_remaining = n - i - 1
            caused[turn["turn_num"]] = turn_caused_seq(turn, turns_remaining)
    return caused


# ---------------------------------------------------------------------------
# Tool costs and wall times
# ---------------------------------------------------------------------------


def compute_tool_costs(tool_invocations, total_turns, rows=None):
    """Compute per-tool marginal and accumulated costs from invocations.

    Marginal: ``(output_est × 5 + input_est) × model_scale`` — the
    one-time cost of the tool call: output tokens come from the
    tool_use arguments text-length estimate, input tokens from the
    tool_result content estimate.

    Accumulated: projected future cache-read cost using
    ``projected_future_reads_seq``. When ``rows`` is supplied, the
    turn count for each invocation's epoch is bounded by
    ``system:compact_boundary`` markers — tools that fired before a
    compaction don't get billed for turns after it, since the summary
    replaced their content. Without ``rows``, falls back to
    ``total_turns`` as a single epoch.
    """
    turn_nums_per_epoch = _epoch_turn_nums(rows) if rows else None

    costs = {}
    for inv in tool_invocations:
        name = inv["name"]
        scale = _model_cost_scale(inv["model"])
        marginal = (inv["output_est"] * 5 + inv["input_est"]) * scale

        rt = inv.get("result_turn")
        if rt is not None:
            acc_turns = _tool_turns_remaining(rt, total_turns, turn_nums_per_epoch)
            accumulated = projected_future_reads_seq(
                inv["input_est"],
                inv["model"],
                acc_turns,
            )
        else:
            accumulated = 0

        if name not in costs:
            costs[name] = {"marginal": 0.0, "accumulated": 0.0}
        costs[name]["marginal"] += marginal
        costs[name]["accumulated"] += accumulated

    for name in costs:
        costs[name]["total"] = costs[name]["marginal"] + costs[name]["accumulated"]

    return costs


def _epoch_turn_nums(rows):
    """Return a list of sets, one per compaction epoch, each
    containing the ``turn_num`` values of turns in that epoch."""
    epochs = [set()]
    for r in rows:
        if r.get("kind") == "system:compact_boundary":
            epochs.append(set())
            continue
        if r.get("kind") == "turn":
            epochs[-1].add(r["turn_num"])
    return epochs


def _tool_turns_remaining(result_turn, total_turns, turn_nums_per_epoch):
    """How many future turns will re-read this tool's result.

    When epoch data is available, counts only turns in the same
    compaction epoch as ``result_turn`` that are strictly after it.
    Otherwise treats the whole session as a single epoch.
    """
    if turn_nums_per_epoch is None:
        return max(0, total_turns - result_turn - 1)
    for epoch in turn_nums_per_epoch:
        if result_turn in epoch:
            return sum(1 for tn in epoch if tn > result_turn)
    return max(0, total_turns - result_turn - 1)


def merge_tool_costs(a, b):
    """Merge two tool_costs dicts, summing values."""
    merged = {}
    for name in set(list(a) + list(b)):
        merged[name] = {
            "marginal": a.get(name, {}).get("marginal", 0)
            + b.get(name, {}).get("marginal", 0),
            "accumulated": a.get(name, {}).get("accumulated", 0)
            + b.get(name, {}).get("accumulated", 0),
        }
        merged[name]["total"] = merged[name]["marginal"] + merged[name]["accumulated"]
    return merged


def compute_wall_times(tool_invocations):
    """Compute total wall time per tool from invocations."""
    wall_times = {}
    for inv in tool_invocations:
        call_ts = inv.get("call_ts")
        result_ts = inv.get("result_ts")
        if call_ts and result_ts:
            name = inv["name"]
            wall_times[name] = wall_times.get(name, 0) + (result_ts - call_ts)
    return wall_times


# ---------------------------------------------------------------------------
# Main metrics computation
# ---------------------------------------------------------------------------


def _latest_ts_in_parsed(parsed):
    """Return the latest entry timestamp from a parse_transcript
    result, or None if nothing in the window carries a timestamp.

    Walks ``rows`` (which includes non-turn entries) from the end so
    the first timestamped row found is the latest one in the window.
    """
    rows = parsed.get("rows") or parsed.get("turns") or []
    for r in reversed(rows):
        ts = r.get("ts")
        if ts is not None:
            return ts
    return None


def compute_metrics(transcript_path, start_ts, now=None):
    """Compute usage metrics from transcript since start_ts.

    Reads the transcript file, then delegates to
    :func:`compute_metrics_from_parsed` so the actual computation
    is shared with callers that already have a parsed result in
    hand (the TUI's summary modal).
    """
    main = parse_transcript(transcript_path, start_ts)
    subagent_infos = find_subagent_transcripts(transcript_path, start_ts)
    tree = build_agent_tree(transcript_path, main, subagent_infos)
    return compute_metrics_from_parsed(main, tree, start_ts, now=now)


def compute_metrics_from_parsed(parsed, tree, start_ts, now=None):
    """Compute usage metrics from already-parsed transcript data.

    Same return shape as :func:`compute_metrics` but accepts
    ``parsed`` (the ``parse_transcript`` result) and ``tree`` (the
    ``build_agent_tree`` result) directly. Lets the TUI reuse the
    metrics math on a session whose transcript is already in
    memory — no second read from disk.

    ``now`` defaults to the timestamp of the latest entry inside
    the window; an empty window reports zero duration.
    """
    main = parsed
    if now is None:
        now = _latest_ts_in_parsed(main) or start_ts
    duration_s = round(now - start_ts, 1)

    all_subagents = flatten_tree(tree)

    # Merge totals across main + all subagents
    all_tokens_by_model = dict(main["tokens_by_model"])
    all_tool_costs = compute_tool_costs(
        main["tool_invocations"],
        main["turn_count"],
        rows=main.get("rows"),
    )
    all_wall_times = compute_wall_times(main["tool_invocations"])
    merged_tool_uses = dict(main["tool_uses"])
    merged_peak = main["peak_context_tokens"]
    merged_turns = main["turn_count"]
    merged_user_messages = main["user_message_count"]
    merged_server_tool_use = dict(main["server_tool_use"])
    # Turns whose final usage never reached the transcript, and the
    # output tokens the parser estimated for them (see parse.py's
    # _finalize). Surfaced so renderers can mark totals approximate.
    merged_output_estimated = dict.fromkeys(("turn_count", "added_tokens"), 0)
    for key in merged_output_estimated:
        merged_output_estimated[key] += (main.get("output_estimated") or {}).get(key, 0)

    for sub in all_subagents:
        all_tokens_by_model = merge_tokens_by_model(
            all_tokens_by_model, sub["tokens_by_model"]
        )
        sub_tool_costs = compute_tool_costs(
            sub["tool_invocations"],
            sub["turn_count"],
            rows=sub.get("rows"),
        )
        all_tool_costs = merge_tool_costs(all_tool_costs, sub_tool_costs)
        sub_wall = compute_wall_times(sub["tool_invocations"])
        for name, wt in sub_wall.items():
            all_wall_times[name] = all_wall_times.get(name, 0) + wt
        for tool, count in sub["tool_uses"].items():
            merged_tool_uses[tool] = merged_tool_uses.get(tool, 0) + count
        merged_peak = max(merged_peak, sub["peak_context_tokens"])
        merged_turns += sub["turn_count"]
        merged_user_messages += sub["user_message_count"]
        for key, val in sub["server_tool_use"].items():
            merged_server_tool_use[key] = merged_server_tool_use.get(key, 0) + val
        for key in merged_output_estimated:
            merged_output_estimated[key] += (sub.get("output_estimated") or {}).get(
                key, 0
            )

    merged_tokens = total_from_by_model(all_tokens_by_model)
    total_tokens = sum(merged_tokens[k] for k in TOKEN_KEYS)
    main_totals = total_from_by_model(main["tokens_by_model"])

    return {
        "duration_s": duration_s,
        "tokens": merged_tokens,
        "tokens_by_model": all_tokens_by_model,
        "total_tokens": total_tokens,
        "peak_context_tokens": merged_peak,
        "turn_count": merged_turns,
        "user_message_count": merged_user_messages,
        "tool_uses": merged_tool_uses,
        "tool_costs": all_tool_costs,
        "tool_wall_times": all_wall_times,
        "server_tool_use": merged_server_tool_use,
        "subagent_count": len(all_subagents),
        "output_estimated": merged_output_estimated,
        "tree": tree,
        "main": {
            "tokens_by_model": main["tokens_by_model"],
            "total_tokens": sum(main_totals[k] for k in TOKEN_KEYS),
            "peak_context_tokens": main["peak_context_tokens"],
            "turn_count": main["turn_count"],
            "tool_uses": main["tool_uses"],
            "user_message_count": main["user_message_count"],
            "server_tool_use": main["server_tool_use"],
        },
    }
