"""Formatting metrics for human-readable output."""

from .metrics import (
    _model_cost_scale,
    cost_breakdown,
    model_aware_cost_breakdown,
)


def format_metrics(metrics):
    """Format metrics dict as a human-readable summary."""
    lines = []

    lines.append(f"Duration: {_fmt_duration(metrics['duration_s'])}")

    # Token breakdown (Sonnet input-equivalent, normalized across models)
    tokens_by_model = metrics.get("tokens_by_model", {})
    mac = model_aware_cost_breakdown(tokens_by_model)
    if mac["total"] > 0:
        lines.append(f"Tokens: {_fmt_k(round(mac['total']))} (Sonnet input-equivalent)")
        cats = mac["categories"]

        cost_items = []
        if cats["cache_read"] > 0:
            cost_items.append((cats["cache_read"], "Cache read", None))

        cw5 = cats["cache_write_5m"]
        cw1 = cats["cache_write_1h"]
        cw_total = cw5 + cw1
        if cw_total > 0:
            if cw5 > 0 and cw1 > 0:
                label = f"Cache write: {_fmt_k(round(cw5))} (5m) + {_fmt_k(round(cw1))} (1h)"
                cost_items.append((cw_total, label, True))
            elif cw5 > 0:
                cost_items.append((cw5, "Cache write (5m)", None))
            else:
                cost_items.append((cw1, "Cache write (1h)", None))

        if cats["output"] > 0:
            cost_items.append((cats["output"], "Output", None))
        if cats["input"] > 0:
            cost_items.append((cats["input"], "Input", None))

        cost_items.sort(key=lambda x: -x[0])
        for val, label, is_composite in cost_items:
            if is_composite:
                lines.append(f"  {label}")
            else:
                lines.append(f"  {label}: {_fmt_k(round(val))}")

    # Per-model breakdown (only if multiple models)
    if len(tokens_by_model) > 1:
        lines.append("By model:")
        for model, mtokens in sorted(tokens_by_model.items()):
            scale = _model_cost_scale(model)
            mc = cost_breakdown(mtokens)
            scaled = round(mc["total"] * scale)
            lines.append(f"  {_short_model(model)}: {_fmt_k(scaled)}")

    # Context
    if metrics.get("peak_context_tokens"):
        lines.append(f"Peak context: {_fmt_k(metrics['peak_context_tokens'])}")

    # Turns, user messages, subagents
    lines.append(f"Model turns: {metrics['turn_count']}")
    if metrics.get("user_message_count"):
        lines.append(f"User messages: {metrics['user_message_count']}")
    if metrics.get("subagent_count"):
        lines.append(f"Subagents: {metrics['subagent_count']}")

    # Tool calls table
    _format_tool_table(lines, metrics)

    # Server-side tool use
    stu = metrics.get("server_tool_use", {})
    stu_nonzero = {k: v for k, v in stu.items() if v}
    if stu_nonzero:
        lines.append("Server tool use:")
        for key, val in sorted(stu_nonzero.items()):
            label = key.replace("_", " ").replace("requests", "").strip()
            lines.append(f"  {label}: {val}")

    # Hierarchical agent tree
    tree = metrics.get("tree", [])
    if tree:
        main = metrics.get("main", {})
        main_mac = model_aware_cost_breakdown(main.get("tokens_by_model", {}))
        main_cost = _fmt_k(round(main_mac["total"])) if main_mac["total"] > 0 else "0"
        main_peak = main.get("peak_context_tokens", 0)
        main_peak_str = f", peak {_fmt_k(main_peak)} ctx" if main_peak else ""
        lines.append(
            f"Main session: {main_cost}, "
            f"{main.get('turn_count', 0)} turns{main_peak_str}"
        )
        lines.extend(_format_tree(tree))

    return "\n".join(lines)


def _format_tool_table(lines, metrics):
    """Append a formatted tool calls table to lines."""
    tool_uses = metrics.get("tool_uses", {})
    tool_costs = metrics.get("tool_costs", {})
    wall_times = metrics.get("tool_wall_times", {})
    if not tool_uses:
        return

    has_costs = bool(tool_costs)
    has_wall = bool(wall_times)

    sorted_tools = sorted(
        tool_uses.items(),
        key=lambda x: -(tool_costs.get(x[0], {}).get("total", 0) or 0),
    )

    # Build column data
    rows = []
    for tool, count in sorted_tools:
        tc = tool_costs.get(tool, {})
        row = {
            "name": tool,
            "total": _fmt_k(round(tc.get("total", 0))) if tc else "",
            "call": _fmt_k(round(tc.get("marginal", 0))) if tc else "",
            "ctx": _fmt_k(round(tc.get("accumulated", 0))) if tc else "",
            "calls": str(count),
            "wall": _fmt_duration(wall_times[tool]) if tool in wall_times else "",
        }
        rows.append(row)

    # Determine which columns to show
    cols = [("name", "")]
    if has_costs:
        cols.append(("total", "total"))
        cols.append(("call", "call"))
        cols.append(("ctx", "ctx"))
    cols.append(("calls", "calls"))
    if has_wall:
        cols.append(("wall", "wall"))

    # Compute column widths
    widths = {}
    for key, header in cols:
        w = len(header)
        for row in rows:
            w = max(w, len(row[key]))
        widths[key] = w

    # Format header and rows
    def fmt_row(row_data):
        parts = []
        for key, _ in cols:
            val = row_data[key]
            if key == "name":
                parts.append(f"{val:<{widths[key]}}")
            else:
                parts.append(f"{val:>{widths[key]}}")
        return "  " + "  ".join(parts)

    header_label = "Tool calls (cost est.):" if has_costs else "Tool calls:"
    lines.append(header_label)

    # Header row
    header_data = {key: header for key, header in cols}
    lines.append(fmt_row(header_data))

    for row in rows:
        lines.append(fmt_row(row))


def _fmt_k(tokens):
    """Format a token count compactly: 150, 1.5K, 2.3M."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1000:
        return f"{tokens / 1000:.1f}K"
    return str(tokens)


def _short_model(model_name):
    """Shorten a model ID to its family name: opus, sonnet, haiku."""
    name = model_name.lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in name:
            return family
    return model_name


def _fmt_duration(duration_s):
    minutes = int(duration_s // 60)
    seconds = duration_s % 60
    if minutes > 0:
        return f"{minutes}m {seconds:.0f}s"
    return f"{seconds:.1f}s"


def _format_tree(nodes, prefix="  "):
    """Format a tree of agent nodes with box-drawing characters.

    Returns a list of formatted lines.
    """
    lines = []
    for i, node in enumerate(nodes):
        is_last = i == len(nodes) - 1
        connector = "\u2514\u2500 " if is_last else "\u251c\u2500 "
        child_prefix = prefix + ("   " if is_last else "\u2502  ")

        # Label
        call_tool = node.get("call_tool")
        call_desc = node.get("call_description", "")
        if call_tool == "Agent":
            label = f'Agent "{call_desc}"' if call_desc else "Agent"
        elif call_tool == "Skill":
            label = f"Skill {call_desc}" if call_desc else "Skill"
        else:
            label = node.get("path", "unknown")

        # Cost
        sub_mac = model_aware_cost_breakdown(node.get("tokens_by_model", {}))
        cost_str = _fmt_k(round(sub_mac["total"])) if sub_mac["total"] > 0 else "0"

        turns = node.get("turn_count", 0)
        peak = node.get("peak_context_tokens", 0)
        peak_str = f", peak {_fmt_k(peak)} ctx" if peak else ""
        lines.append(f"{prefix}{connector}{label}: {cost_str}, {turns} turns{peak_str}")

        # Recurse into children
        children = node.get("children", [])
        if children:
            lines.extend(_format_tree(children, child_prefix))

    return lines
