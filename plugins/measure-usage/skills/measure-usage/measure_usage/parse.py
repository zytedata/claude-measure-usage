"""Transcript parsing, token helpers, and agent tree building."""

import json
import os
from datetime import datetime
from pathlib import Path

TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

CACHE_TIER_KEYS = (
    "ephemeral_5m_input_tokens",
    "ephemeral_1h_input_tokens",
)

ALL_TOKEN_KEYS = TOKEN_KEYS + CACHE_TIER_KEYS

_CHARS_PER_TOKEN = 4
SUBAGENT_MATCH_TOLERANCE_S = 0.1  # 100ms


def _estimate_tokens(text):
    """Rough token estimate from text length (~4 chars per token)."""
    if not text:
        return 0
    return max(1, len(str(text)) // _CHARS_PER_TOKEN)


def parse_ts(ts_str):
    """Parse an ISO timestamp string to a Unix timestamp."""
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def merge_tokens_by_model(a, b):
    """Merge two tokens_by_model dicts, summing values."""
    merged = {}
    for model in set(list(a) + list(b)):
        merged[model] = {}
        for key in ALL_TOKEN_KEYS:
            val = a.get(model, {}).get(key, 0) + b.get(model, {}).get(key, 0)
            if val or key in TOKEN_KEYS:
                merged[model][key] = val
    return merged


def total_from_by_model(tokens_by_model):
    """Sum all models into a flat token dict."""
    totals = dict.fromkeys(ALL_TOKEN_KEYS, 0)
    for model_tokens in tokens_by_model.values():
        for key in ALL_TOKEN_KEYS:
            totals[key] += model_tokens.get(key, 0)
    return totals


# ---------------------------------------------------------------------------
# Transcript path resolution
# ---------------------------------------------------------------------------

def find_transcript_path(session_id, cwd=None):
    """Resolve transcript path from session ID and working directory.

    Claude Code stores transcripts at:
        ~/.claude/projects/{encoded-cwd}/{session_id}.jsonl

    where encoded-cwd replaces '/' with '-'.
    """
    if cwd is None:
        cwd = os.getcwd()
    claude_dir = Path.home() / ".claude" / "projects"
    project_key = cwd.replace("/", "-")
    transcript = claude_dir / project_key / f"{session_id}.jsonl"
    if transcript.exists():
        return str(transcript)
    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return str(candidate)
    return None


# ---------------------------------------------------------------------------
# Subagent discovery
# ---------------------------------------------------------------------------

def read_subagent_meta(jsonl_path):
    """Read .meta.json alongside a subagent JSONL file.

    Returns dict with agentType, description, etc. or {} if missing.
    """
    meta_path = Path(jsonl_path).with_suffix(".meta.json")
    try:
        return json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def find_subagent_transcripts(transcript_path, start_ts):
    """Find subagent transcripts that started after start_ts.

    Returns list of dicts with path, start_ts, and meta.
    """
    session_dir = Path(transcript_path).with_suffix("")
    subagents_dir = session_dir / "subagents"
    if not subagents_dir.exists():
        return []
    results = []
    for p in sorted(subagents_dir.glob("agent-*.jsonl")):
        if "compact" in p.name:
            continue
        for entry in _iter_transcript(str(p)):
            ts = entry.get("timestamp")
            if ts:
                ts_float = parse_ts(ts)
                if ts_float >= start_ts:
                    results.append({
                        "path": str(p),
                        "start_ts": ts_float,
                        "meta": read_subagent_meta(str(p)),
                    })
                break
    return results


# ---------------------------------------------------------------------------
# JSONL iteration
# ---------------------------------------------------------------------------

def _iter_transcript(path):
    """Yield parsed JSON objects from a JSONL file, skipping bad lines."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

def parse_transcript(transcript_path, start_ts=None):
    """Parse a JSONL transcript, optionally from start_ts onward.

    Returns a dict with:
        tokens_by_model, peak_context_tokens, turn_count, tool_uses,
        user_message_count, server_tool_use, tool_invocations, agent_calls
    """
    parser = _TranscriptParser(start_ts)
    for entry in _iter_transcript(transcript_path):
        parser.process_entry(entry)
    return parser.result()


class _TranscriptParser:
    """Accumulates metrics from transcript entries."""

    def __init__(self, start_ts=None):
        self.start_ts = start_ts
        self.tokens_by_model = {}
        self.peak_context = 0
        self.turn_count = 0
        self.tool_uses = {}
        self.user_message_count = 0
        self.server_tool_use = {}
        self.model = "unknown"
        self.entry_ts = None
        self.tool_invocations = []
        self.agent_calls = []
        self._tool_use_id_to_invoc = {}

    def result(self):
        return {
            "tokens_by_model": self.tokens_by_model,
            "peak_context_tokens": self.peak_context,
            "turn_count": self.turn_count,
            "tool_uses": self.tool_uses,
            "user_message_count": self.user_message_count,
            "server_tool_use": self.server_tool_use,
            "tool_invocations": self.tool_invocations,
            "agent_calls": self.agent_calls,
        }

    def process_entry(self, entry):
        ts_str = entry.get("timestamp")
        if ts_str:
            self.entry_ts = parse_ts(ts_str)

        if self.start_ts is not None:
            if ts_str and self.entry_ts < self.start_ts:
                return

        msg = entry.get("message", {})

        if self._is_user_text_message(entry, msg):
            self.user_message_count += 1

        usage = msg.get("usage")
        if usage:
            self.turn_count += 1
            self.model = msg.get("model", "unknown")
            self._accumulate_usage(usage)

        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    self._handle_tool_use(block)
                elif block.get("type") == "tool_result":
                    self._handle_tool_result(block)

    @staticmethod
    def _is_user_text_message(entry, msg):
        """Check if entry is a genuine user text message (not tool result)."""
        if entry.get("type") != "user" or entry.get("isMeta"):
            return False
        content = msg.get("content", "")
        if isinstance(content, list):
            return not all(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
                if isinstance(b, dict)
            )
        return True

    def _accumulate_usage(self, usage):
        """Accumulate token usage from an assistant turn."""
        model = self.model
        if model not in self.tokens_by_model:
            self.tokens_by_model[model] = dict.fromkeys(TOKEN_KEYS, 0)
        for key in TOKEN_KEYS:
            self.tokens_by_model[model][key] += usage.get(key, 0)

        context = (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        self.peak_context = max(self.peak_context, context)

        # Cache tiers
        cache_creation = usage.get("cache_creation", {})
        for tier_key in CACHE_TIER_KEYS:
            val = cache_creation.get(tier_key, 0)
            if val:
                self.tokens_by_model[model][tier_key] = (
                    self.tokens_by_model[model].get(tier_key, 0) + val
                )

        # Server-side tool use
        for key, val in usage.get("server_tool_use", {}).items():
            if isinstance(val, (int, float)):
                self.server_tool_use[key] = self.server_tool_use.get(key, 0) + val

    def _handle_tool_use(self, block):
        """Handle a tool_use content block."""
        name = block.get("name", "unknown")
        self.tool_uses[name] = self.tool_uses.get(name, 0) + 1

        # Track Agent/Skill calls for tree building
        if name in ("Agent", "Skill") and self.entry_ts is not None:
            inp = block.get("input", {})
            desc = inp.get("description", "") if name == "Agent" else inp.get("skill", "")
            self.agent_calls.append({"ts": self.entry_ts, "name": name, "description": desc})

        invoc = {
            "name": name,
            "model": self.model,
            "output_est": _estimate_tokens(json.dumps(block.get("input", {}))),
            "input_est": 0,
            "result_turn": None,
            "call_ts": self.entry_ts,
            "result_ts": None,
        }
        self.tool_invocations.append(invoc)
        tool_id = block.get("id", "")
        if tool_id:
            self._tool_use_id_to_invoc[tool_id] = invoc

    def _handle_tool_result(self, block):
        """Handle a tool_result content block."""
        tool_id = block.get("tool_use_id", "")
        invoc = self._tool_use_id_to_invoc.get(tool_id)

        result_content = block.get("content", "")
        if isinstance(result_content, list):
            result_content = json.dumps(result_content)
        elif not isinstance(result_content, str):
            result_content = str(result_content)

        input_est = _estimate_tokens(result_content)
        if invoc is not None:
            invoc["input_est"] = input_est
            invoc["result_turn"] = self.turn_count
            invoc["result_ts"] = self.entry_ts
        else:
            self.tool_invocations.append({
                "name": "unknown",
                "model": self.model,
                "output_est": 0,
                "input_est": input_est,
                "result_turn": self.turn_count,
                "call_ts": None,
                "result_ts": self.entry_ts,
            })


# ---------------------------------------------------------------------------
# Agent tree building
# ---------------------------------------------------------------------------

def build_agent_tree(main_path, main_parsed, subagent_infos):
    """Build a tree of agent relationships from timestamp matching.

    Matches each subagent's start timestamp to the closest Agent/Skill
    tool_use call (within 100ms tolerance) to determine parent-child
    relationships. Subagents with no match become children of main.

    Args:
        main_path: Path to the main session JSONL
        main_parsed: Result of parse_transcript() for the main session
        subagent_infos: List of dicts from find_subagent_transcripts()

    Returns list of tree nodes (children of main).
    """
    if not subagent_infos:
        return []

    # Parse each subagent
    sub_parsed = {}
    for info in subagent_infos:
        sub_parsed[info["path"]] = parse_transcript(info["path"])

    # Collect all Agent/Skill calls tagged by caller path
    all_calls = _collect_agent_calls(main_path, main_parsed, subagent_infos, sub_parsed)

    # Match each subagent to a parent
    children_map = _match_subagents_to_parents(
        main_path, subagent_infos, all_calls,
    )

    # Build tree recursively
    def _build_children(parent_path):
        children = []
        for info, call_tool, call_desc in children_map.get(parent_path, []):
            parsed = sub_parsed[info["path"]]
            sub_totals = total_from_by_model(parsed["tokens_by_model"])
            node = {
                "path": os.path.basename(info["path"]),
                "call_tool": call_tool,
                "call_description": call_desc,
                "meta": info["meta"],
                "total_tokens": sum(sub_totals[k] for k in TOKEN_KEYS),
                "turn_count": parsed["turn_count"],
                "tokens_by_model": parsed["tokens_by_model"],
                "tool_uses": parsed["tool_uses"],
                "peak_context_tokens": parsed["peak_context_tokens"],
                "user_message_count": parsed["user_message_count"],
                "server_tool_use": parsed["server_tool_use"],
                "tool_invocations": parsed["tool_invocations"],
                "agent_calls": parsed["agent_calls"],
                "children": _build_children(info["path"]),
            }
            children.append(node)
        return children

    return _build_children(main_path)


def _collect_agent_calls(main_path, main_parsed, subagent_infos, sub_parsed):
    """Collect all Agent/Skill calls from main + subagent transcripts.

    Returns list of (caller_path, call_ts, tool_name, description).
    """
    all_calls = []
    for call in main_parsed["agent_calls"]:
        all_calls.append((main_path, call["ts"], call["name"], call["description"]))
    for info in subagent_infos:
        for call in sub_parsed[info["path"]]["agent_calls"]:
            all_calls.append((info["path"], call["ts"], call["name"], call["description"]))
    return all_calls


def _match_subagents_to_parents(main_path, subagent_infos, all_calls):
    """Match each subagent to a parent by closest timestamp.

    Returns dict: parent_path -> list of (info, call_tool, call_description).
    """
    children_map = {}
    for info in subagent_infos:
        best_match = None
        best_delta = float("inf")
        for caller_path, call_ts, tool_name, desc in all_calls:
            delta = info["start_ts"] - call_ts
            if 0 <= delta <= SUBAGENT_MATCH_TOLERANCE_S and delta < best_delta:
                best_delta = delta
                best_match = (caller_path, tool_name, desc)

        if best_match:
            parent_path, call_tool, call_desc = best_match
        else:
            parent_path = main_path
            call_tool = None
            call_desc = info["meta"].get("description", "")

        children_map.setdefault(parent_path, []).append((info, call_tool, call_desc))
    return children_map


def flatten_tree(nodes):
    """Flatten tree nodes into a list (depth-first)."""
    result = []
    for node in nodes:
        result.append(node)
        result.extend(flatten_tree(node["children"]))
    return result
