"""Cost calculations, model normalization, and metrics computation."""

import time

from .parse import (
    TOKEN_KEYS,
    CACHE_TIER_KEYS,
    ALL_TOKEN_KEYS,
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
    # Legacy Opus ($15/MTok input)
    ("opus-4-1", 5.0),       # claude-opus-4-1-20250414
    ("3-opus", 5.0),          # claude-3-opus-20240229
    # Current / future Opus ($5/MTok input)
    ("opus", 5 / 3),
    # Sonnet — all versions ($3/MTok input)
    ("sonnet", 1.0),
    # Legacy Haiku ($0.80/MTok input)
    ("3-5-haiku", 0.267),    # claude-3-5-haiku-20241022
    ("3-haiku", 0.267),       # claude-3-haiku-20240307
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
# Tool costs and wall times
# ---------------------------------------------------------------------------

def compute_tool_costs(tool_invocations, total_turns):
    """Compute per-tool marginal and accumulated costs from invocations.

    Marginal: (output_est × 5 + input_est) × model_scale
    Accumulated: input_est × 0.1 × model_scale × subsequent_turns
        (assumes cache reads for subsequent turns)
    """
    costs = {}
    for inv in tool_invocations:
        name = inv["name"]
        scale = _model_cost_scale(inv["model"])
        marginal = (inv["output_est"] * 5 + inv["input_est"]) * scale

        rt = inv.get("result_turn")
        if rt is not None:
            acc_turns = max(0, total_turns - rt - 1)
            accumulated = inv["input_est"] * 0.1 * scale * acc_turns
        else:
            accumulated = 0

        if name not in costs:
            costs[name] = {"marginal": 0.0, "accumulated": 0.0}
        costs[name]["marginal"] += marginal
        costs[name]["accumulated"] += accumulated

    for name in costs:
        costs[name]["total"] = costs[name]["marginal"] + costs[name]["accumulated"]

    return costs


def merge_tool_costs(a, b):
    """Merge two tool_costs dicts, summing values."""
    merged = {}
    for name in set(list(a) + list(b)):
        merged[name] = {
            "marginal": a.get(name, {}).get("marginal", 0) + b.get(name, {}).get("marginal", 0),
            "accumulated": a.get(name, {}).get("accumulated", 0) + b.get(name, {}).get("accumulated", 0),
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

def compute_metrics(transcript_path, start_ts, now=None):
    """Compute usage metrics from transcript since start_ts.

    Returns a dict with merged totals, main session breakdown, and a tree
    of subagent nodes (each with children).
    """
    if now is None:
        now = time.time()
    duration_s = round(now - start_ts, 1)

    main = parse_transcript(transcript_path, start_ts)

    subagent_infos = find_subagent_transcripts(transcript_path, start_ts)
    tree = build_agent_tree(transcript_path, main, subagent_infos)
    all_subagents = flatten_tree(tree)

    # Merge totals across main + all subagents
    all_tokens_by_model = dict(main["tokens_by_model"])
    all_tool_costs = compute_tool_costs(main["tool_invocations"], main["turn_count"])
    all_wall_times = compute_wall_times(main["tool_invocations"])
    merged_tool_uses = dict(main["tool_uses"])
    merged_peak = main["peak_context_tokens"]
    merged_turns = main["turn_count"]
    merged_user_messages = main["user_message_count"]
    merged_server_tool_use = dict(main["server_tool_use"])

    for sub in all_subagents:
        all_tokens_by_model = merge_tokens_by_model(all_tokens_by_model, sub["tokens_by_model"])
        sub_tool_costs = compute_tool_costs(sub["tool_invocations"], sub["turn_count"])
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
