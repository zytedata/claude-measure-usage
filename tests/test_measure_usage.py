import json
import os
import sys
from pathlib import Path

import pytest

# Add scripts dir to path so we can import measure_usage
PACKAGE_DIR = str(Path(__file__).parent.parent / "plugins" / "measure-usage" / "skills" / "measure-usage")
sys.path.insert(0, PACKAGE_DIR)

import measure_usage
from measure_usage import __main__ as mu_commands

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
    def test_opus(self):
        assert measure_usage._model_cost_scale("claude-opus-4-6") == 5.0

    def test_sonnet(self):
        assert measure_usage._model_cost_scale("claude-sonnet-4-6") == 1.0

    def test_haiku(self):
        assert measure_usage._model_cost_scale("claude-haiku-4-5-20251001") == 0.267

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


class TestFormatTree:
    def test_single_node(self):
        nodes = [{
            "path": "agent-abc.jsonl", "call_tool": "Agent",
            "call_description": "research task", "meta": {},
            "total_tokens": 500, "turn_count": 3,
            "tokens_by_model": {"claude-sonnet-4-6": dict.fromkeys(measure_usage.TOKEN_KEYS, 0)},
            "children": [],
        }]
        lines = measure_usage._format_tree(nodes)
        assert len(lines) == 1
        assert 'Agent "research task"' in lines[0]
        assert "3 turns" in lines[0]
        assert "\u2514\u2500" in lines[0]  # last (only) child

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
        assert len(lines) == 2
        assert 'Agent "analyze pages"' in lines[0]
        assert "Skill scrape-page" in lines[1]
        assert "\u2514\u2500" in lines[1]

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
        assert len(lines) == 3
        # First two use ├─, last uses └─
        assert "\u251c\u2500" in lines[0]
        assert "\u251c\u2500" in lines[1]
        assert "\u2514\u2500" in lines[2]


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
        # marginal: (10*5 + 100) * 5.0 = 750
        assert costs["Read"]["marginal"] == 750
        # accumulated: 100 * 0.1 * 5.0 * 1 = 50
        assert costs["Read"]["accumulated"] == 50

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
        assert "call" in text
        assert "ctx" in text

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
        assert "Subagents: 2" in text
        assert "Main session:" in text
        assert 'Agent "research task"' in text
        assert "Skill analyze-page" in text
        assert "Skill scrape-data" in text
        # Tree chars
        assert "\u251c\u2500" in text or "\u2514\u2500" in text

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

    def test_save_metrics_record(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        record = {"timestamp": "2026-04-07T10:00:00Z", "total_tokens": 1000}
        measure_usage.save_metrics_record(record)
        measure_usage.save_metrics_record(record)

        lines = Path(measure_usage.METRICS_FILE).read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["total_tokens"] == 1000


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

        mu_commands.cmd_start("sess-basic")
        out = capsys.readouterr().out
        assert "started" in out.lower()

        mu_commands.cmd_stats("sess-basic")
        out = capsys.readouterr().out
        assert "Tokens" in out

        mu_commands.cmd_stop("sess-basic")
        out = capsys.readouterr().out
        assert "Saved to" in out

        # Metrics file written
        assert os.path.exists(measure_usage.METRICS_FILE)
        record = json.loads(Path(measure_usage.METRICS_FILE).read_text().strip())
        assert record["total_tokens"] > 0
        assert "tool_uses" in record
        assert "tokens_by_model" in record
        assert "tree" in record
        assert record["session_id"] == "sess-basic"

        # State cleaned up
        assert measure_usage.load_state("sess-basic") is None

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
        out = capsys.readouterr().out
        assert "Saved to" in out

        assert measure_usage.list_active_sessions() == []

    def test_session(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)

        mu_commands.cmd_session("sess-basic")
        out = capsys.readouterr().out
        assert "Tokens:" in out
        assert "Read" in out
        assert "Saved to" in out

        # Record saved with timestamps
        record = json.loads(Path(measure_usage.METRICS_FILE).read_text().strip())
        assert record["session_id"] == "sess-basic"
        assert "started_at" in record
        assert "stopped_at" in record
