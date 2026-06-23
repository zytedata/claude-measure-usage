import json
import os
import sys
from pathlib import Path

import pytest

# Add scripts dir to path so we can import claude_usage_tui
PACKAGE_DIR = str(Path(__file__).parent.parent / "plugins" / "measure-usage" / "skills" / "measure-usage")
sys.path.insert(0, PACKAGE_DIR)

import claude_usage_tui as measure_usage  # noqa: E402
from claude_usage_tui.plain import commands as mu_commands  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# parse_ts
# ---------------------------------------------------------------------------

class TestParseTs:
    def test_z_suffix(self):
        ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        assert isinstance(ts, float)
        assert ts > 0

    def test_offset_suffix(self):
        ts = measure_usage.parse_ts("2026-04-07T10:00:00+00:00")
        assert ts == measure_usage.parse_ts("2026-04-07T10:00:00Z")

    def test_ordering(self):
        t1 = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        t2 = measure_usage.parse_ts("2026-04-07T10:00:01Z")
        assert t2 > t1


# ---------------------------------------------------------------------------
# parse_transcript
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty(self):
        assert measure_usage._estimate_tokens("") == 0
        assert measure_usage._estimate_tokens(None) == 0

    def test_short_text(self):
        # "hi" is 2 chars -> max(1, 2//4) = 1
        assert measure_usage._estimate_tokens("hi") == 1

    def test_longer_text(self):
        text = "a" * 400
        assert measure_usage._estimate_tokens(text) == 100

    def test_non_string(self):
        assert measure_usage._estimate_tokens(12345) == 1  # "12345" = 5 chars


# ---------------------------------------------------------------------------
# parse_transcript
# ---------------------------------------------------------------------------

class TestParseTranscript:
    def test_basic_session(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "basic_session.jsonl"))

        assert result["turn_count"] == 4
        assert "claude-opus-4-6" in result["tokens_by_model"]

        tokens = result["tokens_by_model"]["claude-opus-4-6"]
        assert tokens["input_tokens"] == 100 + 100 + 150 + 200
        assert tokens["output_tokens"] == 20 + 20 + 30 + 40
        assert tokens["cache_creation_input_tokens"] == 50 + 50 + 0 + 0
        assert tokens["cache_read_input_tokens"] == 200 + 200 + 300 + 400

        # Peak context = max of (input + cache_creation + cache_read) per turn
        # Turn 4: 200 + 0 + 400 = 600
        assert result["peak_context_tokens"] == 600

    def test_tool_uses(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "basic_session.jsonl"))
        assert result["tool_uses"] == {"Read": 1}

    def test_user_message_count(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "basic_session.jsonl"))
        # 2 user text messages, 1 tool_result (not counted)
        assert result["user_message_count"] == 2

    def test_agent_calls_extracted(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "with_subagents.jsonl"))
        assert len(result["agent_calls"]) == 1
        call = result["agent_calls"][0]
        assert call["name"] == "Agent"
        assert call["description"] == "research task"
        assert isinstance(call["ts"], float)

    def test_agent_calls_with_skill(self):
        result = measure_usage.parse_transcript(
            str(FIXTURES / "with_nested_subagents" / "subagents" / "agent-parent1.jsonl")
        )
        assert len(result["agent_calls"]) == 1
        call = result["agent_calls"][0]
        assert call["name"] == "Skill"
        assert call["description"] == "scrape-analyze-page"

    def test_agent_calls_none_for_basic(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "basic_session.jsonl"))
        assert result["agent_calls"] == []

    def test_tool_invocations_basic(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "basic_session.jsonl"))
        invocs = result["tool_invocations"]
        assert len(invocs) == 1
        assert invocs[0]["name"] == "Read"
        assert invocs[0]["model"] == "claude-opus-4-6"
        assert invocs[0]["output_est"] > 0
        assert invocs[0]["input_est"] > 0
        assert invocs[0]["result_turn"] == 3  # after 3 assistant turns

    def test_tool_invocations_multi(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "multi_tool.jsonl"))
        invocs = result["tool_invocations"]
        assert len(invocs) == 5  # Read, Grep, Edit, Bash, Read
        names = [i["name"] for i in invocs]
        assert names == ["Read", "Grep", "Edit", "Bash", "Read"]
        # All have output and input estimates
        for inv in invocs:
            assert inv["output_est"] > 0
            assert inv["input_est"] > 0

    def test_multi_tool(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "multi_tool.jsonl"))

        assert result["turn_count"] == 6
        assert result["tool_uses"] == {
            "Read": 2,
            "Grep": 1,
            "Edit": 1,
            "Bash": 1,
        }

    def test_multi_tool_user_messages(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "multi_tool.jsonl"))
        # 1 user text message, 5 tool_results
        assert result["user_message_count"] == 1

    def test_with_start_ts(self):
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:05Z")
        result = measure_usage.parse_transcript(
            str(FIXTURES / "multi_tool.jsonl"), start_ts=start_ts
        )
        assert result["turn_count"] == 4
        assert result["tool_uses"] == {"Edit": 1, "Bash": 1, "Read": 1}

    def test_server_tool_use(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "with_web_search.jsonl"))
        assert result["server_tool_use"]["web_search_requests"] == 1
        assert result["server_tool_use"]["web_fetch_requests"] == 1
        assert result["tool_uses"] == {"WebSearch": 1, "WebFetch": 1}

    def test_no_server_tool_use(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "basic_session.jsonl"))
        assert result["server_tool_use"] == {}

    def test_nonexistent_file(self):
        result = measure_usage.parse_transcript("/nonexistent/path.jsonl")
        assert result["turn_count"] == 0
        assert result["tokens_by_model"] == {}

    def test_empty_file(self, tmp_path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        result = measure_usage.parse_transcript(str(empty))
        assert result["turn_count"] == 0
        assert result["tokens_by_model"] == {}


# ---------------------------------------------------------------------------
# find_subagent_transcripts
# ---------------------------------------------------------------------------

class TestReadSubagentMeta:
    def test_reads_meta(self):
        meta = measure_usage.read_subagent_meta(
            str(FIXTURES / "with_nested_subagents" / "subagents" / "agent-parent1.jsonl")
        )
        assert meta["agentType"] == "general-purpose"
        assert meta["description"] == "analyze pages"

    def test_missing_meta(self, tmp_path):
        jsonl = tmp_path / "agent-nope.jsonl"
        jsonl.write_text("")
        meta = measure_usage.read_subagent_meta(str(jsonl))
        assert meta == {}


class TestFindSubagentTranscripts:
    def test_finds_subagent(self):
        transcript = str(FIXTURES / "with_subagents.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        results = measure_usage.find_subagent_transcripts(transcript, start_ts)
        assert len(results) == 1
        assert "agent-abc123.jsonl" in results[0]["path"]
        assert isinstance(results[0]["start_ts"], float)
        assert isinstance(results[0]["meta"], dict)

    def test_filters_by_start_ts(self):
        transcript = str(FIXTURES / "with_subagents.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:01:00Z")
        results = measure_usage.find_subagent_transcripts(transcript, start_ts)
        assert len(results) == 0

    def test_no_subagents_dir(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        transcript.write_text("")
        results = measure_usage.find_subagent_transcripts(str(transcript), 0)
        assert results == []

    def test_nested_subagents_found(self):
        transcript = str(FIXTURES / "with_nested_subagents.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        results = measure_usage.find_subagent_transcripts(transcript, start_ts)
        assert len(results) == 2
        paths = [r["path"] for r in results]
        assert any("agent-child1.jsonl" in p for p in paths)
        assert any("agent-parent1.jsonl" in p for p in paths)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

class TestModelCostScale:
    def test_opus_current(self):
        assert measure_usage._model_cost_scale("claude-opus-4-6") == pytest.approx(5 / 3)
        assert measure_usage._model_cost_scale("claude-opus-4-5-20250301") == pytest.approx(5 / 3)

    def test_opus_future(self):
        assert measure_usage._model_cost_scale("claude-opus-5-0") == pytest.approx(5 / 3)

    def test_opus_legacy(self):
        assert measure_usage._model_cost_scale("claude-opus-4-1-20250414") == 5.0
        assert measure_usage._model_cost_scale("claude-3-opus-20240229") == 5.0

    def test_sonnet(self):
        assert measure_usage._model_cost_scale("claude-sonnet-4-6") == 1.0

    def test_haiku_current(self):
        assert measure_usage._model_cost_scale("claude-haiku-4-5-20251001") == pytest.approx(1 / 3)

    def test_haiku_future(self):
        assert measure_usage._model_cost_scale("claude-haiku-5-0") == pytest.approx(1 / 3)

    def test_haiku_legacy(self):
        assert measure_usage._model_cost_scale("claude-3-5-haiku-20241022") == pytest.approx(0.267)
        assert measure_usage._model_cost_scale("claude-3-haiku-20240307") == pytest.approx(0.267)

    def test_unknown_defaults_to_sonnet(self):
        assert measure_usage._model_cost_scale("unknown") == 1.0


class TestBuildAgentTree:
    def test_single_level(self):
        transcript = str(FIXTURES / "with_subagents.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        main_parsed = measure_usage.parse_transcript(transcript, start_ts)
        subagent_infos = measure_usage.find_subagent_transcripts(transcript, start_ts)

        tree = measure_usage.build_agent_tree(transcript, main_parsed, subagent_infos)
        assert len(tree) == 1
        assert tree[0]["call_tool"] == "Agent"
        assert tree[0]["children"] == []

    def test_nested(self):
        transcript = str(FIXTURES / "with_nested_subagents.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        main_parsed = measure_usage.parse_transcript(transcript, start_ts)
        subagent_infos = measure_usage.find_subagent_transcripts(transcript, start_ts)

        tree = measure_usage.build_agent_tree(transcript, main_parsed, subagent_infos)
        assert len(tree) == 1
        parent = tree[0]
        assert parent["call_tool"] == "Agent"
        assert parent["call_description"] == "analyze pages"
        assert len(parent["children"]) == 1

        child = parent["children"][0]
        assert child["call_tool"] == "Skill"
        assert child["call_description"] == "scrape-analyze-page"
        assert child["children"] == []
        assert child["total_tokens"] > 0

    def test_empty(self):
        transcript = str(FIXTURES / "basic_session.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        main_parsed = measure_usage.parse_transcript(transcript, start_ts)

        tree = measure_usage.build_agent_tree(transcript, main_parsed, [])
        assert tree == []

    def test_matched_by_agent_id_when_startup_exceeds_tolerance(self, tmp_path):
        """Subagent with >100 ms startup delay still matches its parent.

        Claude Code writes ``toolUseResult.agentId`` on the parent's
        tool_result; that id appears in the subagent's filename
        (``subagents/agent-<id>.jsonl``). The matcher must prefer
        this deterministic link over timestamp proximity, otherwise
        the subagent falls back to ``call_turn=None`` and gets
        dropped from the detail timeline.
        """
        transcript, agent_id = _write_agent_id_fixture(
            tmp_path,
            sub_start="2026-04-07T10:00:03.500Z",  # 2.5 s after the call
        )

        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        main_parsed = measure_usage.parse_transcript(transcript, start_ts)
        sub_infos = measure_usage.find_subagent_transcripts(transcript, start_ts)

        tree = measure_usage.build_agent_tree(transcript, main_parsed, sub_infos)

        assert len(tree) == 1
        node = tree[0]
        assert node["call_tool"] == "Skill"
        assert node["call_description"] == "scrape-explore-site"
        # call_turn must resolve so the detail view can attach the
        # subagent as a footnote under the spawning turn.
        assert node["call_turn"] == 1
        assert f"agent-{agent_id}.jsonl" in node["path"]

    def test_parser_captures_agent_id_to_tool_use_link(self, tmp_path):
        """Parser exposes toolUseResult.agentId → tool_use_id mapping."""
        transcript, agent_id = _write_agent_id_fixture(
            tmp_path,
            sub_start="2026-04-07T10:00:03.500Z",
        )
        parsed = measure_usage.parse_transcript(transcript)
        assert parsed["agent_id_to_tool_use"] == {agent_id: "tuse_skill_1"}

    def test_timestamp_fallback_still_works_without_agent_id(self):
        """Old transcripts without toolUseResult.agentId keep matching.

        The legacy timestamp tolerance (100 ms) has to stay wired up
        for transcripts recorded before Claude Code started emitting
        the deterministic agentId link.
        """
        transcript = str(FIXTURES / "with_subagents.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        main_parsed = measure_usage.parse_transcript(transcript, start_ts)
        sub_infos = measure_usage.find_subagent_transcripts(transcript, start_ts)

        # Sanity: fixture indeed has no agentId links, so the test
        # exercises the fallback path rather than duplicating the
        # agentId-matching case above.
        assert main_parsed["agent_id_to_tool_use"] == {}

        tree = measure_usage.build_agent_tree(transcript, main_parsed, sub_infos)
        assert len(tree) == 1
        assert tree[0]["call_tool"] == "Agent"
        assert tree[0]["call_turn"] == 1


def _write_agent_id_fixture(tmp_path, sub_start):
    """Build a minimal forked-Skill transcript with an agentId link.

    Main session has one Skill tool_use (``tuse_skill_1``) whose
    tool_result carries ``toolUseResult.agentId`` pointing at a
    subagent transcript in ``subagents/agent-<id>.jsonl``. The
    subagent's first entry timestamp is controlled by ``sub_start``
    so callers can pin a delay that exceeds the 100 ms tolerance.
    """
    agent_id = "a8c2fcd336f0e15e2"
    transcript = tmp_path / "session.jsonl"
    session_dir = tmp_path / "session"
    subagents_dir = session_dir / "subagents"
    subagents_dir.mkdir(parents=True)

    entries = [
        {
            "type": "user",
            "message": {"role": "user", "content": "kick off"},
            "uuid": "u1",
            "timestamp": "2026-04-07T10:00:00Z",
            "sessionId": "s",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{
                    "type": "tool_use",
                    "id": "tuse_skill_1",
                    "name": "Skill",
                    "input": {"skill": "scrape-explore-site"},
                }],
                "usage": {
                    "input_tokens": 10, "output_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 100,
                },
            },
            "uuid": "a1",
            "timestamp": "2026-04-07T10:00:01.000Z",
            "sessionId": "s",
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tuse_skill_1",
                    "content": "done",
                }],
            },
            "uuid": "u2",
            "timestamp": "2026-04-07T10:00:35Z",
            "sessionId": "s",
            "toolUseResult": {
                "success": True,
                "commandName": "scrape-explore-site",
                "status": "forked",
                "agentId": agent_id,
            },
        },
    ]
    with transcript.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    sub_path = subagents_dir / f"agent-{agent_id}.jsonl"
    sub_entries = [
        {
            "type": "user",
            "message": {"role": "user", "content": "prompt"},
            "uuid": "su1",
            "timestamp": sub_start,
            "sessionId": "sub",
            "isSidechain": True,
            "agentId": agent_id,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 20, "output_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 50,
                },
            },
            "uuid": "sa1",
            "timestamp": "2026-04-07T10:00:30Z",
            "sessionId": "sub",
            "isSidechain": True,
        },
    ]
    with sub_path.open("w") as f:
        for e in sub_entries:
            f.write(json.dumps(e) + "\n")
    sub_meta = subagents_dir / f"agent-{agent_id}.meta.json"
    sub_meta.write_text(json.dumps({"agentType": "general-purpose"}))
    return str(transcript), agent_id


class TestFormatTree:
    def test_has_header(self):
        nodes = [{
            "path": "agent-abc.jsonl", "call_tool": "Agent",
            "call_description": "research task", "meta": {},
            "total_tokens": 500, "turn_count": 3,
            "tokens_by_model": {"claude-sonnet-4-6": dict.fromkeys(measure_usage.TOKEN_KEYS, 0)},
            "children": [],
        }]
        lines = measure_usage._format_tree(nodes)
        assert lines[0] == "Breakdown:"
        assert "tokens" in lines[1]
        assert "turns" in lines[1]
        assert "context" in lines[1]

    def test_single_node(self):
        nodes = [{
            "path": "agent-abc.jsonl", "call_tool": "Agent",
            "call_description": "research task", "meta": {},
            "total_tokens": 500, "turn_count": 3,
            "tokens_by_model": {"claude-sonnet-4-6": dict.fromkeys(measure_usage.TOKEN_KEYS, 0)},
            "children": [],
        }]
        lines = measure_usage._format_tree(nodes)
        # header + column headers + 1 data row
        assert len(lines) == 3
        assert "Agent research task" in lines[2]
        assert "\u2514\u2500" in lines[2]

    def test_with_main(self):
        nodes = [{
            "path": "agent-abc.jsonl", "call_tool": "Agent",
            "call_description": "task", "meta": {},
            "total_tokens": 500, "turn_count": 3,
            "tokens_by_model": {"claude-sonnet-4-6": dict.fromkeys(measure_usage.TOKEN_KEYS, 0)},
            "children": [],
        }]
        main = {"tokens_by_model": {"claude-sonnet-4-6": {
            "input_tokens": 100, "output_tokens": 50,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }}, "turn_count": 5, "peak_context_tokens": 1000}
        lines = measure_usage._format_tree(nodes, main)
        # header + column headers + main row + 1 data row
        assert len(lines) == 4
        assert "Main session" in lines[2]
        assert "Agent task" in lines[3]

    def test_nested(self):
        nodes = [{
            "path": "agent-abc.jsonl", "call_tool": "Agent",
            "call_description": "analyze pages", "meta": {},
            "total_tokens": 500, "turn_count": 3,
            "tokens_by_model": {"claude-sonnet-4-6": dict.fromkeys(measure_usage.TOKEN_KEYS, 0)},
            "children": [{
                "path": "agent-def.jsonl", "call_tool": "Skill",
                "call_description": "scrape-page", "meta": {},
                "total_tokens": 200, "turn_count": 2,
                "tokens_by_model": {"claude-sonnet-4-6": dict.fromkeys(measure_usage.TOKEN_KEYS, 0)},
                "children": [],
            }],
        }]
        lines = measure_usage._format_tree(nodes)
        text = "\n".join(lines)
        assert "Agent analyze pages" in text
        assert "Skill scrape-page" in text

    def test_multiple_siblings(self):
        node = lambda desc, tool="Agent": {
            "path": "agent.jsonl", "call_tool": tool,
            "call_description": desc, "meta": {},
            "total_tokens": 100, "turn_count": 1,
            "tokens_by_model": {"claude-sonnet-4-6": dict.fromkeys(measure_usage.TOKEN_KEYS, 0)},
            "children": [],
        }
        nodes = [node("first"), node("second"), node("third", "Skill")]
        lines = measure_usage._format_tree(nodes)
        text = "\n".join(lines)
        # First two use ├─, last uses └─
        assert "\u251c\u2500" in text
        assert "\u2514\u2500" in text


class TestComputeToolCosts:
    def test_basic(self):
        invocations = [
            {"name": "Read", "model": "claude-sonnet-4-6",
             "output_est": 10, "input_est": 100, "result_turn": 1},
        ]
        costs = measure_usage.compute_tool_costs(invocations, total_turns=3)
        # marginal: (10*5 + 100) * 1.0 = 150
        assert costs["Read"]["marginal"] == 150
        # accumulated: 100 * 0.1 * 1.0 * (3 - 1 - 1) = 10
        assert costs["Read"]["accumulated"] == 10
        assert costs["Read"]["total"] == 160

    def test_opus_scaling(self):
        invocations = [
            {"name": "Read", "model": "claude-opus-4-6",
             "output_est": 10, "input_est": 100, "result_turn": 1},
        ]
        costs = measure_usage.compute_tool_costs(invocations, total_turns=3)
        scale = 5 / 3  # Opus 4.6: $5 / $3
        # marginal: (10*5 + 100) * scale = 250
        assert costs["Read"]["marginal"] == pytest.approx(150 * scale)
        # accumulated: 100 * 0.1 * scale * 1
        assert costs["Read"]["accumulated"] == pytest.approx(10 * scale)

    def test_no_accumulated_for_last_turn(self):
        invocations = [
            {"name": "Read", "model": "claude-sonnet-4-6",
             "output_est": 10, "input_est": 100, "result_turn": 4},
        ]
        costs = measure_usage.compute_tool_costs(invocations, total_turns=5)
        # accumulated turns = max(0, 5 - 4 - 1) = 0
        assert costs["Read"]["accumulated"] == 0

    def test_multiple_invocations_same_tool(self):
        invocations = [
            {"name": "Read", "model": "claude-sonnet-4-6",
             "output_est": 5, "input_est": 100, "result_turn": 0},
            {"name": "Read", "model": "claude-sonnet-4-6",
             "output_est": 5, "input_est": 200, "result_turn": 3},
        ]
        costs = measure_usage.compute_tool_costs(invocations, total_turns=5)
        # First: marginal=(25+100)=125, accum=100*0.1*4=40
        # Second: marginal=(25+200)=225, accum=200*0.1*1=20
        assert costs["Read"]["marginal"] == 350
        assert costs["Read"]["accumulated"] == 60

    def test_merge_tool_costs(self):
        a = {"Read": {"marginal": 100, "accumulated": 50, "total": 150}}
        b = {"Read": {"marginal": 200, "accumulated": 30, "total": 230},
             "Grep": {"marginal": 80, "accumulated": 10, "total": 90}}
        merged = measure_usage.merge_tool_costs(a, b)
        assert merged["Read"]["marginal"] == 300
        assert merged["Read"]["accumulated"] == 80
        assert merged["Read"]["total"] == 380
        assert merged["Grep"]["total"] == 90


class TestTokenHelpers:
    def test_merge_tokens_by_model(self):
        a = {"opus": {"input_tokens": 100, "output_tokens": 50,
                       "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        b = {"opus": {"input_tokens": 200, "output_tokens": 30,
                       "cache_creation_input_tokens": 10, "cache_read_input_tokens": 0},
             "haiku": {"input_tokens": 50, "output_tokens": 20,
                        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        merged = measure_usage.merge_tokens_by_model(a, b)
        assert merged["opus"]["input_tokens"] == 300
        assert merged["opus"]["output_tokens"] == 80
        assert merged["haiku"]["input_tokens"] == 50

    def test_merge_disjoint_models(self):
        a = {"opus": {"input_tokens": 100, "output_tokens": 0,
                       "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        b = {"haiku": {"input_tokens": 50, "output_tokens": 0,
                        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        merged = measure_usage.merge_tokens_by_model(a, b)
        assert "opus" in merged
        assert "haiku" in merged

    def test_total_from_by_model(self):
        by_model = {
            "opus": {"input_tokens": 100, "output_tokens": 50,
                      "cache_creation_input_tokens": 10, "cache_read_input_tokens": 20},
            "haiku": {"input_tokens": 30, "output_tokens": 10,
                       "cache_creation_input_tokens": 0, "cache_read_input_tokens": 5},
        }
        totals = measure_usage.total_from_by_model(by_model)
        assert totals["input_tokens"] == 130
        assert totals["output_tokens"] == 60

    def test_total_from_empty(self):
        totals = measure_usage.total_from_by_model({})
        assert totals == dict.fromkeys(measure_usage.ALL_TOKEN_KEYS, 0)


# ---------------------------------------------------------------------------
# cost_breakdown
# ---------------------------------------------------------------------------

class TestCostBreakdown:
    def test_with_tiers(self):
        tokens = {
            "input_tokens": 100,
            "output_tokens": 100,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 100,
            "ephemeral_5m_input_tokens": 40,
            "ephemeral_1h_input_tokens": 60,
        }
        cost = measure_usage.cost_breakdown(tokens)
        # 100*1 + 100*5 + 40*1.25 + 60*2.0 + 100*0.1 = 100+500+50+120+10 = 780
        assert cost["total"] == pytest.approx(780.0)

    def test_fallback_without_tiers(self):
        tokens = {
            "input_tokens": 100,
            "output_tokens": 100,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 100,
        }
        cost = measure_usage.cost_breakdown(tokens)
        # Fallback: 100*1.25 for cache creation
        # 100*1 + 100*5 + 100*1.25 + 100*0.1 = 735
        assert cost["total"] == pytest.approx(735.0)

    def test_output_dominates(self):
        tokens = {
            "input_tokens": 10,
            "output_tokens": 1000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        cost = measure_usage.cost_breakdown(tokens)
        pct_map = dict(cost["percentages"])
        assert pct_map["Output"] > 99

    def test_cache_read_cheap(self):
        tokens = {
            "input_tokens": 10,
            "output_tokens": 100,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 100000,
        }
        cost = measure_usage.cost_breakdown(tokens)
        pct_map = dict(cost["percentages"])
        assert pct_map["Cache read"] > 90

    def test_zero_tokens(self):
        tokens = dict.fromkeys(measure_usage.TOKEN_KEYS, 0)
        cost = measure_usage.cost_breakdown(tokens)
        assert cost["total"] == 0

    def test_percentages_sorted_descending(self):
        tokens = {
            "input_tokens": 100,
            "output_tokens": 200,
            "cache_creation_input_tokens": 50,
            "cache_read_input_tokens": 1000,
        }
        cost = measure_usage.cost_breakdown(tokens)
        pcts = [pct for _, pct in cost["percentages"]]
        assert pcts == sorted(pcts, reverse=True)

    def test_small_percentages_filtered(self):
        tokens = {
            "input_tokens": 1,
            "output_tokens": 10000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        cost = measure_usage.cost_breakdown(tokens)
        # Input is ~0.002%, should be filtered out
        labels = [label for label, _ in cost["percentages"]]
        assert "Input" not in labels


# ---------------------------------------------------------------------------
# compute_metrics
# ---------------------------------------------------------------------------

class TestComputeMetrics:
    def test_basic(self):
        transcript = str(FIXTURES / "basic_session.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        now = measure_usage.parse_ts("2026-04-07T10:01:00Z")

        metrics = measure_usage.compute_metrics(transcript, start_ts, now=now)

        assert metrics["duration_s"] == 60.0
        assert metrics["turn_count"] == 4
        assert metrics["subagent_count"] == 0
        assert metrics["total_tokens"] == sum(metrics["tokens"].values())
        assert metrics["user_message_count"] == 2
        assert "claude-opus-4-6" in metrics["tokens_by_model"]

    def test_with_subagents(self):
        transcript = str(FIXTURES / "with_subagents.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        now = measure_usage.parse_ts("2026-04-07T10:01:00Z")

        metrics = measure_usage.compute_metrics(transcript, start_ts, now=now)

        assert metrics["subagent_count"] == 1
        assert metrics["turn_count"] == 5  # 2 main + 3 subagent
        assert metrics["tool_uses"]["Agent"] == 1
        assert metrics["tool_uses"]["Grep"] == 1

        # Tree has 1 child
        assert len(metrics["tree"]) == 1
        sub = metrics["tree"][0]
        assert sub["turn_count"] == 3
        assert sub["total_tokens"] > 0
        assert "agent-abc123.jsonl" in sub["path"]

        # Main session breakdown
        assert metrics["main"]["turn_count"] == 2
        assert metrics["main"]["tool_uses"] == {"Agent": 1}

    def test_hierarchical_main_vs_subagents(self):
        transcript = str(FIXTURES / "with_subagents.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        now = measure_usage.parse_ts("2026-04-07T10:01:00Z")

        metrics = measure_usage.compute_metrics(transcript, start_ts, now=now)

        main_tokens = metrics["main"]["total_tokens"]
        sub_tokens = sum(s["total_tokens"] for s in metrics["tree"])
        assert main_tokens + sub_tokens == metrics["total_tokens"]

    def test_nested_subagents(self):
        transcript = str(FIXTURES / "with_nested_subagents.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        now = measure_usage.parse_ts("2026-04-07T10:01:00Z")

        metrics = measure_usage.compute_metrics(transcript, start_ts, now=now)

        # 2 subagents total (parent + child)
        assert metrics["subagent_count"] == 2
        # Tree: 1 root child (Agent "analyze pages"), which has 1 child (Skill scrape-analyze-page)
        assert len(metrics["tree"]) == 1
        parent = metrics["tree"][0]
        assert parent["call_tool"] == "Agent"
        assert parent["call_description"] == "analyze pages"
        assert len(parent["children"]) == 1

        child = parent["children"][0]
        assert child["call_tool"] == "Skill"
        assert child["call_description"] == "scrape-analyze-page"
        assert child["children"] == []

        # Token totals add up
        main_tokens = metrics["main"]["total_tokens"]
        parent_tokens = parent["total_tokens"]
        child_tokens = child["total_tokens"]
        assert main_tokens + parent_tokens + child_tokens == metrics["total_tokens"]

    def test_tokens_by_model(self):
        transcript = str(FIXTURES / "basic_session.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        now = measure_usage.parse_ts("2026-04-07T10:01:00Z")

        metrics = measure_usage.compute_metrics(transcript, start_ts, now=now)

        assert len(metrics["tokens_by_model"]) == 1
        assert "claude-opus-4-6" in metrics["tokens_by_model"]
        model_total = sum(metrics["tokens_by_model"]["claude-opus-4-6"].values())
        assert model_total == metrics["total_tokens"]

    def test_tool_uses_included(self):
        transcript = str(FIXTURES / "multi_tool.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        now = measure_usage.parse_ts("2026-04-07T10:01:00Z")

        metrics = measure_usage.compute_metrics(transcript, start_ts, now=now)
        assert metrics["tool_uses"]["Read"] == 2
        assert metrics["tool_uses"]["Bash"] == 1

    def test_tool_costs(self):
        transcript = str(FIXTURES / "multi_tool.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        now = measure_usage.parse_ts("2026-04-07T10:01:00Z")

        metrics = measure_usage.compute_metrics(transcript, start_ts, now=now)
        assert "tool_costs" in metrics
        assert set(metrics["tool_costs"].keys()) == {"Read", "Grep", "Edit", "Bash"}
        # Each tool should have marginal, accumulated, total
        for tc in metrics["tool_costs"].values():
            assert tc["marginal"] > 0
            assert tc["accumulated"] >= 0
            assert tc["total"] == tc["marginal"] + tc["accumulated"]
        # First Read called early, should have accumulated context cost
        assert metrics["tool_costs"]["Read"]["accumulated"] > 0


# ---------------------------------------------------------------------------
# format_metrics
# ---------------------------------------------------------------------------

class TestFormatMetrics:
    def _make_metrics(self, **overrides):
        defaults = {
            "duration_s": 65.3,
            "total_tokens": 1000,
            "tokens": {
                "input_tokens": 400,
                "output_tokens": 200,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 300,
            },
            "tokens_by_model": {
                "claude-sonnet-4-6": {
                    "input_tokens": 400, "output_tokens": 200,
                    "cache_creation_input_tokens": 100, "cache_read_input_tokens": 300,
                }
            },
            "peak_context_tokens": 800,
            "turn_count": 5,
            "user_message_count": 2,
            "subagent_count": 0,
            "tool_uses": {"Read": 3, "Edit": 1},
            "tool_costs": {},
            "server_tool_use": {},
            "tree": [],
            "main": {},
        }
        defaults.update(overrides)
        return defaults

    def test_basic_format(self):
        text = measure_usage.format_metrics(self._make_metrics())

        assert "1m 5s" in text
        assert "Tokens:" in text
        assert "Sonnet input-equivalent" in text
        assert "Output:" in text
        assert "Input:" in text
        assert "Read" in text
        assert "Edit" in text
        assert "User messages: 2" in text
        assert "Subagents" not in text  # 0 subagents
        assert "By model" not in text  # single model

    def test_tool_costs_displayed(self):
        text = measure_usage.format_metrics(self._make_metrics(
            tool_costs={
                "Read": {"marginal": 45200, "accumulated": 3000, "total": 48200},
                "Edit": {"marginal": 2100, "accumulated": 0, "total": 2100},
            },
        ))
        assert "45.2K" in text
        assert "3.0K" in text
        assert "2.1K" in text
        assert "est." in text
        # Semi-table with header
        assert "invoke" in text
        assert "carry" in text

    def test_tool_costs_small(self):
        text = measure_usage.format_metrics(self._make_metrics(
            tool_costs={
                "Read": {"marginal": 150, "accumulated": 0, "total": 150},
            },
        ))
        assert "150" in text

    def test_multi_model(self):
        text = measure_usage.format_metrics(self._make_metrics(
            tokens_by_model={
                "claude-opus-4-6": {
                    "input_tokens": 300, "output_tokens": 150,
                    "cache_creation_input_tokens": 50, "cache_read_input_tokens": 200,
                },
                "claude-haiku-4-5-20251001": {
                    "input_tokens": 100, "output_tokens": 50,
                    "cache_creation_input_tokens": 50, "cache_read_input_tokens": 100,
                },
            },
        ))
        assert "By model:" in text
        assert "opus:" in text
        assert "haiku:" in text

    def test_short_duration(self):
        text = measure_usage.format_metrics(self._make_metrics(
            duration_s=3.2,
            user_message_count=0,
        ))
        assert "3.2s" in text

    def test_with_tree(self):
        text = measure_usage.format_metrics(self._make_metrics(
            subagent_count=2,
            main={"tokens_by_model": {"claude-sonnet-4-6": {
                "input_tokens": 200, "output_tokens": 100,
                "cache_creation_input_tokens": 50, "cache_read_input_tokens": 150,
            }}, "turn_count": 3},
            tree=[
                {"path": "agent-abc.jsonl", "call_tool": "Agent",
                 "call_description": "research task", "meta": {},
                 "total_tokens": 500, "turn_count": 3,
                 "tokens_by_model": {"claude-haiku-4-5-20251001": dict.fromkeys(measure_usage.TOKEN_KEYS, 0)},
                 "children": [
                     {"path": "agent-ghi.jsonl", "call_tool": "Skill",
                      "call_description": "analyze-page", "meta": {},
                      "total_tokens": 200, "turn_count": 2,
                      "tokens_by_model": {"claude-haiku-4-5-20251001": dict.fromkeys(measure_usage.TOKEN_KEYS, 0)},
                      "children": []},
                 ]},
                {"path": "agent-def.jsonl", "call_tool": "Skill",
                 "call_description": "scrape-data", "meta": {},
                 "total_tokens": 300, "turn_count": 2,
                 "tokens_by_model": {"claude-opus-4-6": dict.fromkeys(measure_usage.TOKEN_KEYS, 0)},
                 "children": []},
            ],
        ))
        assert "Breakdown:" in text
        assert "Main session" in text
        assert "Agent research task" in text
        assert "Skill analyze-page" in text
        assert "Skill scrape-data" in text
        # Tree chars and column headers
        assert "\u251c\u2500" in text or "\u2514\u2500" in text
        assert "tokens" in text
        assert "context" in text

    def test_server_tool_use(self):
        text = measure_usage.format_metrics(self._make_metrics(
            server_tool_use={"web_search_requests": 3, "web_fetch_requests": 2},
        ))
        assert "Server tool use:" in text
        assert "web search: 3" in text
        assert "web fetch: 2" in text

    def test_server_tool_use_zeros_hidden(self):
        text = measure_usage.format_metrics(self._make_metrics(
            server_tool_use={"web_search_requests": 0, "web_fetch_requests": 0},
        ))
        assert "Server tool use" not in text


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

class TestStateManagement:
    def test_save_load_remove(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        sid = "abc-123"
        state = {
            "start_ts": 1000.0,
            "started_at": "2026-04-07T10:00:00Z",
            "transcript_path": "/path/to/transcript.jsonl",
        }

        assert measure_usage.load_state(sid) is None

        measure_usage.save_state(sid, state)
        loaded = measure_usage.load_state(sid)
        assert loaded == state

        # State file uses session ID as filename
        assert os.path.exists(
            os.path.join(measure_usage.SESSIONS_DIR, "abc-123.json")
        )

        measure_usage.remove_state(sid)
        assert measure_usage.load_state(sid) is None

    def test_list_active_sessions(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        state1 = {
            "start_ts": 1000.0,
            "started_at": "2026-04-07T10:00:00Z",
            "transcript_path": "/path/to/session1.jsonl",
        }
        state2 = {
            "start_ts": 2000.0,
            "started_at": "2026-04-07T10:30:00Z",
            "transcript_path": "/path/to/session2.jsonl",
        }
        measure_usage.save_state("sess-1", state1)
        measure_usage.save_state("sess-2", state2)

        sessions = measure_usage.list_active_sessions()
        assert len(sessions) == 2
        ids = {sid for sid, _ in sessions}
        assert ids == {"sess-1", "sess-2"}

# ---------------------------------------------------------------------------
# Commands (integration)
# ---------------------------------------------------------------------------

class TestFindTranscriptPath:
    def test_finds_by_cwd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Set up: ~/.claude/projects/-tmp-myproject/session-123.jsonl
        project_dir = tmp_path / ".claude" / "projects" / "-tmp-myproject"
        project_dir.mkdir(parents=True)
        transcript = project_dir / "session-123.jsonl"
        transcript.write_text("")

        result = measure_usage.find_transcript_path(
            "session-123", cwd="/tmp/myproject"
        )
        assert result == str(transcript)

    def test_fallback_search(self, tmp_path, monkeypatch):
        # Set up transcript in a project dir that doesn't match cwd
        project_dir = tmp_path / ".claude" / "projects" / "-other-project"
        project_dir.mkdir(parents=True)
        transcript = project_dir / "session-456.jsonl"
        transcript.write_text("")

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = measure_usage.find_transcript_path(
            "session-456", cwd="/wrong/path"
        )
        assert result == str(transcript)

    def test_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".claude" / "projects").mkdir(parents=True)
        result = measure_usage.find_transcript_path("nonexistent", cwd="/tmp")
        assert result is None


class TestCommands:
    TRANSCRIPTS = {
        "sess-basic": str(FIXTURES / "basic_session.jsonl"),
        "sess-multi": str(FIXTURES / "multi_tool.jsonl"),
        "sess-none": "/nonexistent.jsonl",
    }

    @pytest.fixture(autouse=True)
    def _patch_resolve(self, monkeypatch):
        """Patch _resolve_transcript to map session IDs to fixture paths."""
        monkeypatch.setattr(
            mu_commands, "_resolve_transcript",
            lambda sid: self.TRANSCRIPTS.get(sid, sid),
        )

    def test_start_stats_stop(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)

        # Copy the base fixture to a writable location so we can
        # append new entries mid-test. cmd_start anchors the tracking
        # window to the transcript's current tail, so an entry that
        # exists only AFTER cmd_start falls inside the window.
        transcript = tmp_path / "session.jsonl"
        transcript.write_bytes((FIXTURES / "basic_session.jsonl").read_bytes())
        monkeypatch.setattr(
            mu_commands, "_resolve_transcript", lambda sid: str(transcript),
        )

        mu_commands.cmd_start("sess-dyn")
        out = capsys.readouterr().out
        assert "started" in out.lower()

        # Simulate a turn that happens after /start: append an
        # assistant entry with a later timestamp than any pre-start
        # entry. This turn should show up inside the tracked window.
        new_turn = {
            "type": "assistant",
            "uuid": "post1",
            "timestamp": "2026-04-07T11:00:00Z",
            "sessionId": "sess-dyn",
            "message": {
                "role": "assistant",
                "id": "msg_post",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "response"}],
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 100,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
        with open(transcript, "a") as f:
            f.write(json.dumps(new_turn) + "\n")

        mu_commands.cmd_stats("sess-dyn")
        out = capsys.readouterr().out
        assert "Tokens" in out

        mu_commands.cmd_stop("sess-dyn")
        out = capsys.readouterr().out
        assert "Tokens" in out

        # State cleaned up
        assert measure_usage.load_state("sess-dyn") is None

    def test_start_when_already_tracking(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)

        mu_commands.cmd_start("sess-basic")
        capsys.readouterr()

        mu_commands.cmd_start("sess-basic")
        out = capsys.readouterr().out
        assert "Tracking since" in out

    def test_stats_not_tracking(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)

        mu_commands.cmd_stats("sess-none")
        out = capsys.readouterr().out
        assert "Not tracking" in out

    def test_stop_not_tracking(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)

        mu_commands.cmd_stop("sess-none")
        out = capsys.readouterr().out
        assert "Not tracking" in out or "Nothing to stop" in out

    def test_multiple_sessions(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)

        mu_commands.cmd_start("sess-basic")
        mu_commands.cmd_start("sess-multi")
        capsys.readouterr()

        sessions = measure_usage.list_active_sessions()
        assert len(sessions) == 2

        mu_commands.cmd_stop()
        capsys.readouterr()

        assert measure_usage.list_active_sessions() == []

    def test_session(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)

        mu_commands.cmd_session("sess-basic")
        out = capsys.readouterr().out
        assert "Tokens:" in out
        assert "Read" in out


# ---------------------------------------------------------------------------
# Per-turn rows (parse_transcript output)
# ---------------------------------------------------------------------------

class TestPerTurnRows:
    def test_basic_session_turn_rows(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "basic_session.jsonl"))
        turns = result["turns"]
        assert len(turns) == 4
        assert [t["turn_num"] for t in turns] == [1, 2, 3, 4]
        # Turn 1: thinking only, no text preview, no tool calls
        assert turns[0]["text_preview"] == ""
        assert turns[0]["tool_calls"] == []
        # Turn 2: has a text block
        assert turns[1]["text_preview"] == "Hello!"
        # Turn 3: invokes Read
        assert len(turns[2]["tool_calls"]) == 1
        assert turns[2]["tool_calls"][0]["name"] == "Read"
        assert turns[2]["tool_calls"][0]["input"]["file_path"] == "/tmp/test.txt"
        # Turn 4: text block after tool result
        assert turns[3]["text_preview"] == "Here is the file."

    def test_turn_row_token_fields(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "basic_session.jsonl"))
        t = result["turns"][0]
        assert t["in_tokens"] == 100
        assert t["out_tokens"] == 20
        assert t["cache_r"] == 200
        assert t["cache_w"] == 50
        assert t["ctx"] == 350
        assert t["model"] == "claude-opus-4-6"

    def test_agent_call_records_turn_num(self):
        result = measure_usage.parse_transcript(str(FIXTURES / "with_subagents.jsonl"))
        assert len(result["agent_calls"]) == 1
        assert result["agent_calls"][0]["turn_num"] == 1


# ---------------------------------------------------------------------------
# turns_label
# ---------------------------------------------------------------------------

class TestTurnLabel:
    def test_prefers_text_preview(self):
        row = {
            "text_preview": "Hello world",
            "tool_calls": [{"name": "Read", "input": {"file_path": "/a"}}],
        }
        assert measure_usage.turn_label(row) == "Hello world"

    def test_single_read(self):
        row = {
            "text_preview": "",
            "tool_calls": [{"name": "Read", "input": {"file_path": "/tmp/foo.py"}}],
        }
        assert measure_usage.turn_label(row) == "Read foo.py"

    def test_bash_with_command(self):
        row = {
            "text_preview": "",
            "tool_calls": [{"name": "Bash", "input": {"command": "pytest -q"}}],
        }
        assert measure_usage.turn_label(row) == 'Bash "pytest -q"'

    def test_single_agent_with_description(self):
        row = {
            "text_preview": "",
            "tool_calls": [{"name": "Agent", "input": {"description": "do a thing"}}],
        }
        assert measure_usage.turn_label(row) == '[Agent] "do a thing"'

    def test_parallel_agent_fan_out(self):
        row = {
            "text_preview": "",
            "tool_calls": [
                {"name": "Agent", "input": {"description": "task A"}},
                {"name": "Agent", "input": {"description": "task B"}},
                {"name": "Agent", "input": {"description": "task C"}},
            ],
        }
        label = measure_usage.turn_label(row)
        assert label.startswith("[3× Agent]")
        assert "task A" in label

    def test_repeated_tool_collapsed(self):
        row = {
            "text_preview": "",
            "tool_calls": [
                {"name": "Read", "input": {"file_path": "/a.py"}},
                {"name": "Read", "input": {"file_path": "/a.py"}},
                {"name": "Grep", "input": {"pattern": "foo"}},
            ],
        }
        label = measure_usage.turn_label(row)
        assert "Read a.py ×2" in label
        assert 'Grep "foo"' in label

    def test_empty(self):
        assert measure_usage.turn_label({"text_preview": "", "tool_calls": []}) == ""

    def test_task_create_with_subject(self):
        row = {
            "text_preview": "",
            "tool_calls": [
                {"name": "TaskCreate", "input": {"subject": "Build parser"}}
            ],
        }
        assert measure_usage.turn_label(row) == 'TaskCreate "Build parser"'

    def test_task_update_status(self):
        row = {
            "text_preview": "",
            "tool_calls": [
                {"name": "TaskUpdate", "input": {"taskId": "3", "status": "completed"}}
            ],
        }
        assert measure_usage.turn_label(row) == "TaskUpdate #3 → completed"

    def test_web_fetch_domain(self):
        row = {
            "text_preview": "",
            "tool_calls": [
                {"name": "WebFetch", "input": {"url": "https://example.com/docs/x"}}
            ],
        }
        assert measure_usage.turn_label(row) == "WebFetch example.com"

    def test_tool_search_query(self):
        row = {
            "text_preview": "",
            "tool_calls": [
                {"name": "ToolSearch", "input": {"query": "select:Read,Edit"}}
            ],
        }
        assert measure_usage.turn_label(row) == 'ToolSearch "select:Read,Edit"'


class TestMsgIdDedupe:
    """Claude Code emits one JSONL entry per content block; a single logical
    turn can span multiple entries sharing the same message id (all carrying
    the same usage payload). The parser must count it as one turn."""

    def _write(self, tmp_path, entries):
        path = tmp_path / "split_turn.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        return str(path)

    def _assistant(self, uuid, msg_id, ts, content, usage):
        return {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": ts,
            "sessionId": "s1",
            "message": {
                "role": "assistant",
                "id": msg_id,
                "model": "claude-sonnet-4-6",
                "content": content,
                "usage": usage,
            },
        }

    def test_split_thinking_plus_tool_counts_as_one_turn(self, tmp_path):
        usage = {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 100,
        }
        entries = [
            self._assistant("u1", "msg_A", "2026-04-07T10:00:00Z",
                            [{"type": "thinking", "thinking": "..."}], usage),
            self._assistant("u2", "msg_A", "2026-04-07T10:00:00Z",
                            [{"type": "tool_use", "id": "t1", "name": "Read",
                              "input": {"file_path": "/a.py"}}], usage),
            self._assistant("u3", "msg_B", "2026-04-07T10:00:05Z",
                            [{"type": "text", "text": "done"}], usage),
        ]
        path = self._write(tmp_path, entries)

        result = measure_usage.parse_transcript(path)
        # Two logical turns, not three.
        assert result["turn_count"] == 2
        assert len(result["turns"]) == 2
        # Tokens accumulated only twice, not three times.
        tokens = result["tokens_by_model"]["claude-sonnet-4-6"]
        assert tokens["input_tokens"] == 20
        assert tokens["cache_read_input_tokens"] == 200
        # Content from both entries of the first logical turn must be on
        # the same row: we have the Read tool call there.
        first = result["turns"][0]
        assert len(first["tool_calls"]) == 1
        assert first["tool_calls"][0]["name"] == "Read"
        # Second turn has the text preview.
        assert result["turns"][1]["text_preview"] == "done"

    def test_split_turn_takes_max_output_tokens(self, tmp_path):
        """Within a split turn, output_tokens is written incrementally:
        early content-block entries carry a partial count and only the
        final entry carries the complete total, while input/cache are
        identical across the split. The turn must be booked with the
        complete (max) output, not the first (partial) entry — otherwise
        summed output under-reports against the billed total while
        input/cache stay exact."""
        base = {
            "input_tokens": 1961,
            "cache_creation_input_tokens": 6320,
            "cache_read_input_tokens": 0,
        }
        entries = [
            self._assistant("u1", "msg_A", "2026-04-07T10:00:00Z",
                            [{"type": "text", "text": "working"}],
                            {**base, "output_tokens": 4}),
            self._assistant("u2", "msg_A", "2026-04-07T10:00:01Z",
                            [{"type": "tool_use", "id": "t1", "name": "Bash",
                              "input": {"command": "ls"}}],
                            {**base, "output_tokens": 4}),
            self._assistant("u3", "msg_A", "2026-04-07T10:00:02Z",
                            [{"type": "tool_use", "id": "t2", "name": "Bash",
                              "input": {"command": "pwd"}}],
                            {**base, "output_tokens": 175}),
        ]
        path = self._write(tmp_path, entries)

        result = measure_usage.parse_transcript(path)
        # Still one logical turn.
        assert result["turn_count"] == 1
        assert len(result["turns"]) == 1
        tokens = result["tokens_by_model"]["claude-sonnet-4-6"]
        # Complete output (175), not the first partial entry (4) nor the
        # sum of the repeated entries (183).
        assert tokens["output_tokens"] == 175
        # Input/cache are booked once and unchanged by the merge.
        assert tokens["input_tokens"] == 1961
        assert tokens["cache_creation_input_tokens"] == 6320
        assert tokens["cache_read_input_tokens"] == 0
        # The turn row reflects the complete output too.
        assert result["turns"][0]["out_tokens"] == 175

    def test_system_entry_between_split_halves_still_dedupes(self, tmp_path):
        """A ``system``-type entry interleaved between the two halves of a
        split turn must not break msg-id dedupe."""
        usage = {
            "input_tokens": 1,
            "output_tokens": 1500,
            "cache_creation_input_tokens": 900,
            "cache_read_input_tokens": 64000,
        }
        entries = [
            self._assistant("u1", "msg_Z", "2026-04-10T21:40:56Z",
                            [{"type": "thinking", "thinking": "..."}], usage),
            {
                "type": "system",
                "uuid": "sys1",
                "timestamp": "2026-04-10T21:41:10Z",
                "sessionId": "s1",
                "message": {},
            },
            self._assistant("u2", "msg_Z", "2026-04-10T21:41:10Z",
                            [{"type": "text", "text": "## How it works"}], usage),
        ]
        path = self._write(tmp_path, entries)

        result = measure_usage.parse_transcript(path)
        assert result["turn_count"] == 1
        assert len(result["turns"]) == 1
        assert result["turns"][0]["text_preview"] == "## How it works"

    def test_parallel_tool_calls_same_msg_id_one_turn(self, tmp_path):
        """Seven parallel TaskCreate calls in one logical turn should be
        one row in the per-turn output, not seven."""
        usage = {
            "input_tokens": 5,
            "output_tokens": 500,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 1000,
        }
        entries = [
            self._assistant(f"u{i}", "msg_X", "2026-04-07T10:00:00Z",
                            [{"type": "tool_use", "id": f"t{i}",
                              "name": "TaskCreate",
                              "input": {"subject": f"task {i}"}}], usage)
            for i in range(7)
        ]
        path = self._write(tmp_path, entries)

        result = measure_usage.parse_transcript(path)
        assert result["turn_count"] == 1
        assert len(result["turns"]) == 1
        # All 7 tool calls land on the single row.
        assert len(result["turns"][0]["tool_calls"]) == 7
        # Tool invocations list also has 7 entries (one per tool_use block).
        assert len(result["tool_invocations"]) == 7
        # Usage counted exactly once.
        tokens = result["tokens_by_model"]["claude-sonnet-4-6"]
        assert tokens["output_tokens"] == 500


class TestNonturnRows:
    """The per-turn report surfaces non-assistant transcript entries as
    timeline rows. We emit one row per entry kind and deliberately don't
    filter so unexpected types stay visible."""

    def _write(self, tmp_path, entries):
        path = tmp_path / "mixed.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        return str(path)

    def test_user_text_builds_user_row(self, tmp_path):
        path = self._write(tmp_path, [
            {"type": "user", "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "user", "content": "hello world"}},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "user"
        assert "hello world" in rows[0]["what"]

    def test_interrupt_detected(self, tmp_path):
        path = self._write(tmp_path, [
            {"type": "user", "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "[Request interrupted by user for tool use]"},
             ]}},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "interrupt"
        assert "interrupted" in rows[0]["what"].lower()

    def test_permission_mode_row(self, tmp_path):
        path = self._write(tmp_path, [
            {"type": "permission-mode", "permissionMode": "acceptEdits",
             "timestamp": "2026-04-07T10:00:00Z", "sessionId": "s1"},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "permission-mode"
        assert "acceptEdits" in rows[0]["what"]

    def test_tool_result_only_user_entry_is_dropped(self, tmp_path):
        """User entries containing only tool_result blocks are
        filtered out — they're noisy (one per tool call) and their
        timestamps are preserved via last_tool_result_ts on the
        preceding turn for the renderer's gap column."""
        path = self._write(tmp_path, [
            {"type": "user", "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_01Y5P3f8KrVaz", "content": "ok"},
             ]}},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert rows == []

    def test_attachment_deferred_tools_delta(self, tmp_path):
        path = self._write(tmp_path, [
            {"type": "attachment", "timestamp": "2026-04-07T10:00:00Z",
             "sessionId": "s1",
             "attachment": {"type": "deferred_tools_delta",
                            "addedNames": ["A", "B", "C"], "removedNames": []}},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "attachment:deferred_tools_delta"
        assert "+3" in rows[0]["what"]

    def test_compact_boundary_splits_caused_epoch(self, tmp_path):
        """Per-turn caused_seq attribution must stop at
        ``system:compact_boundary``: turns before the boundary cannot
        inherit their cache_w into turns after it."""
        from claude_usage_tui.metrics import compute_caused_by_turn

        def mk_turn(n, cache_w=0):
            return {
                "kind": "turn",
                "turn_num": n,
                "model": "claude-sonnet-4-6",
                "cache_w": cache_w,
            }

        rows = [
            mk_turn(1, cache_w=1000),   # before boundary, 2 turns after it in epoch
            mk_turn(2),
            mk_turn(3),
            {"kind": "system:compact_boundary"},
            mk_turn(4, cache_w=1000),   # after boundary, 2 turns after it in epoch
            mk_turn(5),
            mk_turn(6),
        ]
        caused = compute_caused_by_turn(rows)
        # Turn 1 has 2 turns remaining in its epoch (turns 2, 3).
        # Without epoch awareness this would be 5 turns remaining.
        assert caused[1] == 1000 * 0.1 * 1.0 * 2  # 200
        # Turn 4 has 2 turns remaining in its own epoch (turns 5, 6).
        assert caused[4] == 1000 * 0.1 * 1.0 * 2  # 200
        # Last turn in each epoch has 0 remaining.
        assert caused[3] == 0.0
        assert caused[6] == 0.0

    def test_compact_boundary_epoch_aware_tool_costs(self, tmp_path):
        """compute_tool_costs should respect compact_boundary when
        ``rows`` is supplied — a tool result from before a
        compaction is not billed against turns after it."""
        from claude_usage_tui.metrics import compute_tool_costs

        invocations = [
            {
                "name": "Read",
                "model": "claude-sonnet-4-6",
                "output_est": 0,
                "input_est": 1000,
                "result_turn": 1,  # result lands at turn 1 (before boundary)
                "call_ts": 1.0,
                "result_ts": 2.0,
            },
        ]
        rows = [
            {"kind": "turn", "turn_num": 1, "model": "claude-sonnet-4-6"},
            {"kind": "turn", "turn_num": 2, "model": "claude-sonnet-4-6"},
            {"kind": "turn", "turn_num": 3, "model": "claude-sonnet-4-6"},
            {"kind": "system:compact_boundary"},
            {"kind": "turn", "turn_num": 4, "model": "claude-sonnet-4-6"},
            {"kind": "turn", "turn_num": 5, "model": "claude-sonnet-4-6"},
        ]

        # Without epoch awareness — accumulates over all 3 turns after
        # result (turns 2, 3, 4 would all read it, so acc_turns=3).
        costs_no_epoch = compute_tool_costs(invocations, total_turns=5)
        assert costs_no_epoch["Read"]["accumulated"] == 1000 * 0.1 * 1.0 * 3

        # With epoch awareness — only turns 2 and 3 remain in turn 1's
        # epoch; the boundary cuts off further reads.
        costs_epoch = compute_tool_costs(invocations, total_turns=5, rows=rows)
        assert costs_epoch["Read"]["accumulated"] == 1000 * 0.1 * 1.0 * 2

    def test_unknown_type_still_renders(self, tmp_path):
        """Unknown types fall through to a generic builder so nothing is
        silently dropped."""
        path = self._write(tmp_path, [
            {"type": "some-new-type", "timestamp": "2026-04-07T10:00:00Z",
             "sessionId": "s1"},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "some-new-type"
        assert "some-new-type" in rows[0]["what"]

    def test_end_ts_tracks_split_turn_last_entry(self, tmp_path):
        """A logical turn split across thinking/text/tool_use entries
        should have end_ts = timestamp of the last entry, so the
        renderer can compute model-generation duration."""
        usage = {"input_tokens": 1, "output_tokens": 1,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        path = self._write(tmp_path, [
            {"type": "assistant", "uuid": "a1",
             "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "assistant", "id": "m1",
                         "model": "claude-sonnet-4-6",
                         "content": [{"type": "thinking", "thinking": "..."}],
                         "usage": usage}},
            {"type": "assistant", "uuid": "a2",
             "timestamp": "2026-04-07T10:00:14Z",
             "message": {"role": "assistant", "id": "m1",
                         "model": "claude-sonnet-4-6",
                         "content": [{"type": "text", "text": "done"}],
                         "usage": usage}},
        ])
        result = measure_usage.parse_transcript(path)
        assert len(result["turns"]) == 1
        t = result["turns"][0]
        assert round(t["end_ts"] - t["ts"]) == 14

    def test_last_tool_result_ts_latched_on_prev_turn(self, tmp_path):
        """tool_result timestamps should land on the turn that fired
        the tool, not on a new row, so the renderer can compute the
        'gap' between tools finishing and the next turn starting."""
        usage = {"input_tokens": 1, "output_tokens": 1,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        path = self._write(tmp_path, [
            {"type": "assistant", "uuid": "a1",
             "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "assistant", "id": "m1",
                         "model": "claude-sonnet-4-6",
                         "content": [{"type": "tool_use", "id": "t1",
                                      "name": "Bash",
                                      "input": {"command": "sleep 5"}}],
                         "usage": usage}},
            {"type": "user", "timestamp": "2026-04-07T10:00:05Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
             ]}},
            {"type": "assistant", "uuid": "a2",
             "timestamp": "2026-04-07T10:00:08Z",
             "message": {"role": "assistant", "id": "m2",
                         "model": "claude-sonnet-4-6",
                         "content": [{"type": "text", "text": "done"}],
                         "usage": usage}},
        ])
        result = measure_usage.parse_transcript(path)
        # tool_result row dropped; two turn rows remain.
        assert len(result["rows"]) == 2
        # Previous turn records when its tool finished.
        t1, t2 = result["turns"]
        assert t1["last_tool_result_ts"] is not None
        t1_done = t1["last_tool_result_ts"]
        # Second turn started 3s after the tool finished.
        assert round(t2["ts"] - t1_done) == 3

    def test_skill_base_dir_shim_dropped(self, tmp_path):
        """The 'Base directory for this skill: …' preamble Claude Code
        feeds back when a Skill tool fires is a client-side wrapper,
        not a real user message — drop it."""
        path = self._write(tmp_path, [
            {"type": "user", "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "user", "content":
                 "Base directory for this skill: /path/to/skill\n\n# Skill\n..."}},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert rows == []

    def test_task_notification_shim_dropped(self, tmp_path):
        path = self._write(tmp_path, [
            {"type": "user", "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "user", "content":
                 "<task-notification><task-id>x</task-id><status>complete</status></task-notification>"}},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert rows == []

    def test_local_command_shim_dropped(self, tmp_path):
        path = self._write(tmp_path, [
            {"type": "user", "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "user", "content":
                 "<local-command-stdout>installed ok</local-command-stdout>"}},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert rows == []

    def test_bash_input_shim_dropped(self, tmp_path):
        path = self._write(tmp_path, [
            {"type": "user", "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "user", "content":
                 "<bash-input>git diff</bash-input>"}},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert rows == []

    def test_slash_command_extracted(self, tmp_path):
        """Slash command wrappers carry real intent — render them
        compactly instead of dropping."""
        path = self._write(tmp_path, [
            {"type": "user", "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "user", "content":
                 "<command-message>simplify</command-message>\n"
                 "<command-name>/simplify</command-name>\n"
                 "<command-args>--no-tests</command-args>"}},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "slash-command"
        assert "/simplify" in rows[0]["what"]
        assert "--no-tests" in rows[0]["what"]

    def test_slash_command_no_args(self, tmp_path):
        path = self._write(tmp_path, [
            {"type": "user", "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "user", "content":
                 "<command-message>compact</command-message>\n"
                 "<command-name>/compact</command-name>"}},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "slash-command"
        assert rows[0]["what"] == "[slash] /compact"

    def test_genuine_user_text_kept(self, tmp_path):
        """Real user prompts must NOT be filtered by accident — only
        client-side shim wrappers should be dropped."""
        path = self._write(tmp_path, [
            {"type": "user", "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "user", "content": "rewrite the function"}},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "user"
        assert "rewrite the function" in rows[0]["what"]

    def test_mixed_chronological_order(self, tmp_path):
        """A mixed transcript produces rows in chronological order with
        turns and non-turn events interleaved."""
        path = self._write(tmp_path, [
            {"type": "user", "timestamp": "2026-04-07T10:00:00Z",
             "message": {"role": "user", "content": "first question"}},
            {"type": "assistant", "uuid": "a1",
             "timestamp": "2026-04-07T10:00:01Z",
             "message": {"role": "assistant", "id": "msg_A",
                         "model": "claude-sonnet-4-6",
                         "content": [{"type": "text", "text": "Answer."}],
                         "usage": {"input_tokens": 10, "output_tokens": 5,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 100}}},
            {"type": "permission-mode", "permissionMode": "acceptEdits",
             "timestamp": "2026-04-07T10:00:02Z", "sessionId": "s1"},
        ])
        rows = measure_usage.parse_transcript(path)["rows"]
        kinds = [r["kind"] for r in rows]
        assert kinds == ["user", "turn", "permission-mode"]


class TestShortAgentId:
    def test_path(self):
        assert measure_usage.short_agent_id(
            "/x/agent-a048f2eedd306ffdc.jsonl"
        ) == "a048"

    def test_bare(self):
        assert measure_usage.short_agent_id("agent-abcdef.jsonl") == "abcd"


# ---------------------------------------------------------------------------
# turns_table render_turns_report
# ---------------------------------------------------------------------------

class TestRenderTurnsReport:
    def test_basic_session(self):
        main_parsed = measure_usage.parse_transcript(str(FIXTURES / "basic_session.jsonl"))
        out = measure_usage.render_turns_report(main_parsed, [])
        assert "Main session" in out
        assert "4 turns" in out
        assert "Hello!" in out
        assert "Read test.txt" in out
        assert "in" in out and "cache_r" in out and "Tokens" in out

    def test_with_subagents(self):
        transcript = str(FIXTURES / "with_subagents.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        main_parsed = measure_usage.parse_transcript(transcript, start_ts)
        subagent_infos = measure_usage.find_subagent_transcripts(transcript, start_ts)
        tree = measure_usage.build_agent_tree(transcript, main_parsed, subagent_infos)

        out = measure_usage.render_turns_report(main_parsed, tree)
        # Main section present
        assert "Main session" in out
        # Subagent section present
        assert "parent turn 1" in out
        # sub column populated on spawning turn
        assert "↳" in out

    def test_nested_subagents(self):
        transcript = str(FIXTURES / "with_nested_subagents.jsonl")
        start_ts = measure_usage.parse_ts("2026-04-07T10:00:00Z")
        main_parsed = measure_usage.parse_transcript(transcript, start_ts)
        subagent_infos = measure_usage.find_subagent_transcripts(transcript, start_ts)
        tree = measure_usage.build_agent_tree(transcript, main_parsed, subagent_infos)

        out = measure_usage.render_turns_report(main_parsed, tree)
        # Both the parent and the nested child should get their own tables.
        assert out.count("parent turn") >= 2
        # Main session's header should decompose Tokens into own + subagents
        # when nested subagents contribute (i.e. the total > own alone).
        assert "own + " in out
        assert "subagents)" in out


# ---------------------------------------------------------------------------
# turn_seq
# ---------------------------------------------------------------------------

class TestTurnSeq:
    def test_sonnet_cache_read_dominated(self):
        row = {
            "model": "claude-sonnet-4-6",
            "in_tokens": 100,
            "out_tokens": 20,
            "cache_r": 10000,
            "cache_w": 0,
        }
        # 10000*0.1 + 100*1 + 0 + 20*5 = 1200
        assert measure_usage.turn_seq(row) == pytest.approx(1200)

    def test_opus_scaling(self):
        row = {
            "model": "claude-opus-4-6",
            "in_tokens": 100,
            "out_tokens": 0,
            "cache_r": 0,
            "cache_w": 0,
        }
        # 100 * 5/3 = 166.666...
        assert measure_usage.turn_seq(row) == pytest.approx(100 * 5 / 3)


# ---------------------------------------------------------------------------
# Layering: the plain CLI must never pull in Textual
# ---------------------------------------------------------------------------

class TestPlainDoesNotImportTextual:
    """The /measure-usage skill runs `python -m claude_usage_tui.plain`
    and must not load Textual as a side effect — the skill environment
    doesn't need (and shouldn't require) a GUI dependency.

    Runs in a fresh subprocess so earlier tests that may have imported
    Textual directly can't pollute sys.modules and mask a regression.
    """

    def test_plain_import_does_not_load_textual(self):
        import subprocess

        code = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import claude_usage_tui.plain  # noqa: F401\n"
            "from claude_usage_tui.plain import commands  # noqa: F401\n"
            "bad = sorted(m for m in sys.modules if m == 'textual' or m.startswith('textual.'))\n"
            "assert not bad, 'textual leaked into plain: ' + repr(bad)\n"
            % PACKAGE_DIR
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
