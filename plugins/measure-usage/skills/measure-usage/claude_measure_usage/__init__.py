"""Measure token usage during Claude Code sessions.

Two presentation layers sit on top of a shared data layer:

- :mod:`claude_measure_usage.plain` — non-interactive text renderer used
  by the ``/measure-usage`` Claude Code skill.
- :mod:`claude_measure_usage.tui` — interactive Textual UI (not imported
  here, so ``import claude_measure_usage`` never pulls Textual in).
"""

# Re-export public API for convenience and test compatibility
from .parse import (
    TOKEN_KEYS,
    CACHE_TIER_KEYS,
    ALL_TOKEN_KEYS,
    _estimate_tokens,
    parse_ts,
    find_transcript_path,
    read_subagent_meta,
    find_subagent_transcripts,
    parse_transcript,
    merge_tokens_by_model,
    total_from_by_model,
    build_agent_tree,
    flatten_tree,
)

from .metrics import (
    _model_cost_scale,
    cost_breakdown,
    model_aware_cost_breakdown,
    compute_tool_costs,
    merge_tool_costs,
    compute_wall_times,
    compute_metrics,
    turn_seq,
)

from .turns_label import short_agent_id, turn_label
from .nonturn_rows import build_nonturn_label

from .plain.turns_table import render_turns_report
from .plain.display import (
    format_metrics,
    _format_tree,
    _fmt_k,
    _fmt_duration,
)
from .plain.state import (
    STATE_DIR,
    SESSIONS_DIR,
    state_path,
    load_state,
    save_state,
    remove_state,
    list_active_sessions,
)
