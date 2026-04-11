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

# Non-turn entry types dropped from the per-turn timeline.
# All pure client-side bookkeeping with no UX or debugging value:
#   - file-history-snapshot: /undo feature state
#   - queue-operation: input queue enqueue/dequeue (user pastes,
#     task notifications) — noisy and opaque
_NONTURN_SKIP_TYPES = frozenset({
    "file-history-snapshot",
    "queue-operation",
})

# (type, subtype) pairs dropped from the per-turn timeline:
#   - system/turn_duration: per-turn wallclock marker, redundant
#     with the t+ column.
#   - system/local_command: shell escape (`!cmd`) output, mirrors
#     the user-side <local-command-*> shims that are also dropped.
_NONTURN_SKIP_SUBTYPES = frozenset({
    ("system", "turn_duration"),
    ("system", "local_command"),
})


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
    """Accumulates metrics from transcript entries.

    A single logical assistant turn is often split across multiple JSONL
    entries — Claude Code emits one entry per content block (thinking,
    text, tool_use), all sharing the same message id. Each entry carries
    the same ``usage`` payload. We dedupe by message id so turn_count
    and token totals reflect logical turns, not raw entry counts.
    """

    def __init__(self, start_ts=None):
        self.start_ts = start_ts
        self.tokens_by_model = {}
        self.peak_context = 0
        self.turn_count = 0
        self.tool_uses = {}
        self.user_message_count = 0
        self.first_entry_ts = None
        self.server_tool_use = {}
        self.model = "unknown"
        self.entry_ts = None
        self.tool_invocations = []
        self.agent_calls = []
        self.turns = []
        self.rows = []
        self._tool_use_id_to_invoc = {}
        self._cur_turn = None
        self._cur_msg_id = None

    def result(self):
        return {
            "tokens_by_model": self.tokens_by_model,
            "peak_context_tokens": self.peak_context,
            "turn_count": self.turn_count,
            "tool_uses": self.tool_uses,
            "user_message_count": self.user_message_count,
            "first_entry_ts": self.first_entry_ts,
            "server_tool_use": self.server_tool_use,
            "tool_invocations": self.tool_invocations,
            "agent_calls": self.agent_calls,
            "turns": self.turns,
            "rows": self.rows,
        }

    def process_entry(self, entry):
        ts_str = entry.get("timestamp")
        if ts_str:
            self.entry_ts = parse_ts(ts_str)
            if self.first_entry_ts is None:
                self.first_entry_ts = self.entry_ts

        if self.start_ts is not None:
            if ts_str and self.entry_ts < self.start_ts:
                return

        msg = entry.get("message", {}) or {}

        if self._is_user_text_message(entry, msg):
            self.user_message_count += 1

        usage = msg.get("usage")
        is_assistant = entry.get("type") == "assistant"
        if usage:
            # Multiple JSONL entries can share a message id — one per
            # content block of a single logical turn, potentially with
            # unrelated system entries interleaved between them. Only
            # the first entry for a given id counts as a new turn.
            msg_id = msg.get("id")
            if msg_id and msg_id == self._cur_msg_id:
                # Continuation of the current turn: keep _cur_turn,
                # skip usage accumulation and turn_count increment.
                # Extend end_ts so the renderer can measure the
                # model's generation duration across split entries.
                if self._cur_turn is not None and self.entry_ts is not None:
                    prior = self._cur_turn.get("end_ts") or 0
                    self._cur_turn["end_ts"] = max(prior, self.entry_ts)
            else:
                self.turn_count += 1
                self.model = msg.get("model", "unknown")
                self._accumulate_usage(usage)
                self._cur_turn = self._start_turn_row(usage)
                self.turns.append(self._cur_turn)
                self.rows.append(self._cur_turn)
                self._cur_msg_id = msg_id
        elif not is_assistant:
            # Non-assistant transcript entries (user messages, system
            # events, permission mode changes, attachments, etc.) do
            # not carry usage but are surfaced as timeline rows so
            # users can see UX interactions inline with model turns.
            self._append_nonturn_row(entry, msg)

        # Only assistant-side blocks contribute to the current turn row;
        # tool_result blocks are processed regardless (they carry input
        # estimates for invocation cost accounting).
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use" and is_assistant:
                    self._handle_tool_use(block)
                elif btype == "tool_result":
                    self._handle_tool_result(block)
                elif btype == "text" and is_assistant:
                    self._collect_turn_text(block)

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

    def _start_turn_row(self, usage):
        """Create a new per-turn row populated from the usage block.

        Raw token counts only; Sonnet-equivalent ("Seq") is computed
        lazily by the cost layer.
        """
        in_tokens = usage.get("input_tokens", 0)
        out_tokens = usage.get("output_tokens", 0)
        cache_r = usage.get("cache_read_input_tokens", 0)
        cache_w = usage.get("cache_creation_input_tokens", 0)
        return {
            "kind": "turn",
            "turn_num": self.turn_count,
            "ts": self.entry_ts,
            # Timestamp of the last JSONL entry sharing this turn's
            # message id. A single logical turn can be split across
            # multiple entries (thinking, text, tool_use) whose
            # timestamps are seconds apart, so tracking the max gives
            # us the model's generation end time. Updated in
            # process_entry on msg_id continuation.
            "end_ts": self.entry_ts,
            "model": self.model,
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
            "cache_r": cache_r,
            "cache_w": cache_w,
            "ctx": in_tokens + cache_r + cache_w,
            "text_preview": "",
            "tool_calls": [],
            # Max tool_result ts among tool results that came in AFTER
            # this turn fired its tool_use blocks and BEFORE the next
            # turn started. Filled in by _handle_tool_result.
            "last_tool_result_ts": None,
        }

    def _append_nonturn_row(self, entry, msg):
        """Append a non-turn timeline row for a transcript entry.

        Pure bookkeeping entries with no UX or debugging value are
        dropped here (not in the renderer) so downstream consumers
        of ``rows`` don't have to re-filter.
        """
        etype = entry.get("type")
        if etype in _NONTURN_SKIP_TYPES:
            return
        if (etype, entry.get("subtype")) in _NONTURN_SKIP_SUBTYPES:
            return
        # Drop user entries whose content is entirely tool_result blocks.
        # The only useful datapoint — when the last tool finished — is
        # latched onto the spawning turn's ``last_tool_result_ts`` via
        # ``_handle_tool_result``, and surfaced as a "gap" column later.
        if self._is_tool_result_only_user_entry(entry, msg):
            return
        from .nonturn_rows import build_nonturn_label
        result = build_nonturn_label(entry, msg)
        if result is None:
            return
        kind, label = result
        row = {
            "kind": kind,
            "ts": self.entry_ts,
            "what": label,
        }
        # Preserve compact_boundary metadata so the metrics layer can
        # detect epoch boundaries when attributing cache_w caused cost.
        if kind == "system:compact_boundary":
            row["compact_meta"] = entry.get("compactMetadata") or {}
        self.rows.append(row)

    @staticmethod
    def _is_tool_result_only_user_entry(entry, msg):
        """True if entry is a ``user`` message whose content is nothing
        but ``tool_result`` blocks (the synthetic wrappers Claude Code
        writes after tool calls return)."""
        if entry.get("type") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, list) or not content:
            return False
        return all(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
            if isinstance(b, dict)
        )

    def _collect_turn_text(self, block):
        """Capture the first text block's first line for the current turn."""
        if self._cur_turn is None or self._cur_turn["text_preview"]:
            return
        text = block.get("text", "") or ""
        first_line = text.strip().split("\n", 1)[0].strip()
        self._cur_turn["text_preview"] = first_line

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
        inp = block.get("input", {}) or {}

        # Track Agent/Skill calls for tree building
        if name in ("Agent", "Skill") and self.entry_ts is not None:
            desc = inp.get("description", "") if name == "Agent" else inp.get("skill", "")
            self.agent_calls.append({
                "ts": self.entry_ts,
                "name": name,
                "description": desc,
                "turn_num": self.turn_count,
            })

        # Attach tool call to the current turn for label rendering.
        if self._cur_turn is not None:
            self._cur_turn["tool_calls"].append({
                "name": name,
                "input": inp,
                "id": block.get("id", ""),
            })

        invoc = {
            "name": name,
            "model": self.model,
            "output_est": _estimate_tokens(json.dumps(inp)),
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

        # Latch the latest tool_result timestamp onto the preceding
        # turn so the renderer can show the "gap" between tools
        # finishing and the next turn starting. ``_cur_turn`` still
        # points at that turn here because new turns are only started
        # on assistant entries with usage, and tool_result user
        # entries come between turns.
        if self._cur_turn is not None and self.entry_ts is not None:
            prior = self._cur_turn.get("last_tool_result_ts") or 0
            self._cur_turn["last_tool_result_ts"] = max(prior, self.entry_ts)

        if invoc is None:
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
        for info, call_tool, call_desc, call_turn in children_map.get(parent_path, []):
            parsed = sub_parsed[info["path"]]
            sub_totals = total_from_by_model(parsed["tokens_by_model"])
            node = {
                "path": os.path.basename(info["path"]),
                "call_tool": call_tool,
                "call_description": call_desc,
                "call_turn": call_turn,
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
                "turns": parsed["turns"],
                "rows": parsed.get("rows", []),
                "children": _build_children(info["path"]),
            }
            children.append(node)
        return children

    return _build_children(main_path)


def _collect_agent_calls(main_path, main_parsed, subagent_infos, sub_parsed):
    """Collect all Agent/Skill calls from main + subagent transcripts.

    Returns list of (caller_path, call_ts, tool_name, description, call_turn).
    """
    all_calls = []
    for call in main_parsed["agent_calls"]:
        all_calls.append((
            main_path, call["ts"], call["name"], call["description"], call.get("turn_num"),
        ))
    for info in subagent_infos:
        for call in sub_parsed[info["path"]]["agent_calls"]:
            all_calls.append((
                info["path"], call["ts"], call["name"], call["description"], call.get("turn_num"),
            ))
    return all_calls


def _match_subagents_to_parents(main_path, subagent_infos, all_calls):
    """Match each subagent to a parent by closest timestamp.

    Returns dict: parent_path -> list of (info, call_tool, call_description, call_turn).
    """
    children_map = {}
    for info in subagent_infos:
        best_match = None
        best_delta = float("inf")
        for caller_path, call_ts, tool_name, desc, call_turn in all_calls:
            delta = info["start_ts"] - call_ts
            if 0 <= delta <= SUBAGENT_MATCH_TOLERANCE_S and delta < best_delta:
                best_delta = delta
                best_match = (caller_path, tool_name, desc, call_turn)

        if best_match:
            parent_path, call_tool, call_desc, call_turn = best_match
        else:
            parent_path = main_path
            call_tool = None
            call_desc = info["meta"].get("description", "")
            call_turn = None

        children_map.setdefault(parent_path, []).append(
            (info, call_tool, call_desc, call_turn)
        )
    return children_map


def flatten_tree(nodes):
    """Flatten tree nodes into a list (depth-first)."""
    result = []
    for node in nodes:
        result.append(node)
        result.extend(flatten_tree(node["children"]))
    return result
