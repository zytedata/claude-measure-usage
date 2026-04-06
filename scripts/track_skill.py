#!/usr/bin/env python3
"""Claude Code PostToolUse hook: track timing, token usage, and context window
for skill invocations.

Logs metrics to .skill-tracker/metrics.jsonl in the project's working directory.

Tracks ALL skills by default. Set SKILL_TRACKER_PREFIXES env var to filter
(comma-separated, e.g. "scrape-,deploy-").
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

METRICS_FILE = ".skill-tracker/metrics.jsonl"

TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def parse_transcript(transcript_path):
    """Parse a JSONL transcript, returning total usage and peak context per turn."""
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    peak_context = 0
    try:
        with open(transcript_path) as f:
            for line in f:
                obj = json.loads(line)
                usage = obj.get("message", {}).get("usage", {})
                if usage:
                    for key in TOKEN_KEYS:
                        totals[key] += usage.get(key, 0)
                    # Context = all input tokens sent to the model in this turn
                    context = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    peak_context = max(peak_context, context)
    except (OSError, json.JSONDecodeError):
        pass
    return totals, peak_context


def find_skill_timestamp(transcript_path, tool_use_id):
    """Find the timestamp when a Skill tool_use was recorded in the transcript."""
    try:
        with open(transcript_path) as f:
            for line in f:
                obj = json.loads(line)
                content = obj.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("id") == tool_use_id:
                            ts = obj.get("timestamp")
                            if ts:
                                return _parse_ts(ts)
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def find_subagent_transcripts(session_dir, start_ts, end_ts):
    """Find subagent transcripts that started within [start_ts, end_ts]."""
    subagents_dir = Path(session_dir) / "subagents"
    if not subagents_dir.exists():
        return []
    results = []
    for p in subagents_dir.glob("agent-*.jsonl"):
        if "compact" in p.name:
            continue
        try:
            with open(p) as f:
                for line in f:
                    obj = json.loads(line)
                    ts = obj.get("timestamp")
                    if ts:
                        first_ts = _parse_ts(ts)
                        if start_ts <= first_ts <= end_ts:
                            results.append(str(p))
                        break
        except (OSError, json.JSONDecodeError):
            pass
    return results


def sum_usage_between(transcript_path, start_ts, end_ts):
    """Sum token usage from the main transcript between two timestamps.

    Used for non-fork skills where tokens are in the parent transcript.
    """
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    peak_context = 0
    try:
        with open(transcript_path) as f:
            for line in f:
                obj = json.loads(line)
                ts_str = obj.get("timestamp")
                if not ts_str:
                    continue
                ts = _parse_ts(ts_str)
                if ts < start_ts:
                    continue
                if ts > end_ts:
                    break
                usage = obj.get("message", {}).get("usage", {})
                if usage:
                    for key in TOKEN_KEYS:
                        totals[key] += usage.get(key, 0)
                    context = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    peak_context = max(peak_context, context)
    except (OSError, json.JSONDecodeError):
        pass
    return totals, peak_context


def main():
    hook_input = json.load(sys.stdin)

    tool_input = hook_input.get("tool_input", {})
    skill_name = tool_input.get("skill", "")
    if not skill_name:
        return

    # Optional prefix filtering via env var
    prefixes = os.environ.get("SKILL_TRACKER_PREFIXES", "")
    if prefixes:
        prefix_list = [p.strip() for p in prefixes.split(",") if p.strip()]
        if not any(skill_name.startswith(p) for p in prefix_list):
            return

    transcript_path = hook_input.get("transcript_path", "")
    tool_use_id = hook_input.get("tool_use_id", "")
    tool_response = hook_input.get("tool_response", "")
    is_fork = "forked execution" in str(tool_response)

    now = time.time()

    # Find when the Skill tool_use was issued
    start_ts = find_skill_timestamp(transcript_path, tool_use_id)
    duration_s = round(now - start_ts, 1) if start_ts else None

    tokens = None
    peak_context = 0
    subagent_count = 0

    if start_ts:
        session_dir = str(Path(transcript_path).with_suffix(""))

        if is_fork:
            # Fork-context: tokens are in subagent transcripts
            subagent_paths = find_subagent_transcripts(
                session_dir, start_ts - 1, now
            )
            subagent_count = len(subagent_paths)
            tokens = dict.fromkeys(TOKEN_KEYS, 0)
            for path in subagent_paths:
                usage, ctx = parse_transcript(path)
                for key in TOKEN_KEYS:
                    tokens[key] += usage[key]
                peak_context = max(peak_context, ctx)
        else:
            # Non-fork: tokens are in the main transcript between start and now
            tokens, peak_context = sum_usage_between(
                transcript_path, start_ts - 0.1, now + 0.1
            )

            # Also check for subagents spawned during this skill
            subagent_paths = find_subagent_transcripts(
                session_dir, start_ts - 1, now
            )
            subagent_count = len(subagent_paths)
            for path in subagent_paths:
                usage, ctx = parse_transcript(path)
                for key in TOKEN_KEYS:
                    tokens[key] += usage[key]
                peak_context = max(peak_context, ctx)

    total_tokens = sum(tokens.values()) if tokens else None

    metric = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skill": skill_name,
        "args": tool_input.get("args", ""),
        "duration_s": duration_s,
        "is_fork": is_fork,
        "total_tokens": total_tokens,
        "peak_context_tokens": peak_context or None,
        "subagent_count": subagent_count,
    }
    if tokens:
        metric["tokens"] = tokens

    # Write to metrics log
    cwd = hook_input.get("cwd", ".")
    metrics_path = os.path.join(cwd, METRICS_FILE)
    os.makedirs(os.path.dirname(metrics_path), exist_ok=True)
    with open(metrics_path, "a") as f:
        f.write(json.dumps(metric) + "\n")

    # Print summary to stderr
    parts = [f"skill-tracker: {skill_name} {duration_s}s"]
    if total_tokens is not None:
        parts.append(
            f"{total_tokens:,} tok "
            f"({tokens['output_tokens']:,} out, "
            f"{tokens['cache_read_input_tokens']:,} cache-read)"
        )
    if peak_context:
        parts.append(f"peak ctx {peak_context:,}")
    if subagent_count:
        parts.append(f"{subagent_count} subagents")
    print(" | ".join(parts), file=sys.stderr)


if __name__ == "__main__":
    main()
