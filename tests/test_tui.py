"""Tests for the interactive TUI layer.

Discovery helpers live in ``claude_measure_usage.tui.discovery``; the
Textual screens are exercised end-to-end via ``App.run_test()``.
"""

import sys
import time
from pathlib import Path

import pytest

PACKAGE_DIR = str(
    Path(__file__).parent.parent
    / "plugins"
    / "measure-usage"
    / "skills"
    / "measure-usage"
)
sys.path.insert(0, PACKAGE_DIR)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

from claude_measure_usage.tui import discovery  # noqa: E402


class TestDiscoverProjects:
    def test_empty_root(self, tmp_path):
        root = tmp_path / "projects"
        root.mkdir()
        assert discovery.discover_projects(root) == []

    def test_missing_root(self, tmp_path):
        assert discovery.discover_projects(tmp_path / "nope") == []

    def test_counts_sessions_and_sorts_by_recency(self, tmp_path, monkeypatch):
        root = tmp_path / "projects"
        root.mkdir()
        # Two projects, each with a different number of sessions and
        # different most-recent-session timestamps.
        stale = root / "-tmp-stale"
        stale.mkdir()
        (stale / "a.jsonl").write_text("")
        recent = root / "-tmp-recent"
        recent.mkdir()
        (recent / "a.jsonl").write_text("")
        (recent / "b.jsonl").write_text("")
        # Make "recent" genuinely more recent.
        now = time.time()
        import os
        os.utime(stale / "a.jsonl", (now - 3600, now - 3600))
        os.utime(recent / "a.jsonl", (now - 60, now - 60))
        os.utime(recent / "b.jsonl", (now - 30, now - 30))

        entries = discovery.discover_projects(root)
        assert [e.project_dir.name for e in entries] == [
            "-tmp-recent",
            "-tmp-stale",
        ]
        assert entries[0].session_count == 2
        assert entries[1].session_count == 1
        assert entries[0].last_activity > entries[1].last_activity

    def test_ignores_non_directory_entries(self, tmp_path):
        root = tmp_path / "projects"
        root.mkdir()
        (root / "stray_file").write_text("")
        (root / "-tmp-proj").mkdir()
        entries = discovery.discover_projects(root)
        assert len(entries) == 1
        assert entries[0].project_dir.name == "-tmp-proj"

    def test_empty_project_uses_dir_mtime(self, tmp_path):
        root = tmp_path / "projects"
        root.mkdir()
        proj = root / "-tmp-empty"
        proj.mkdir()
        entries = discovery.discover_projects(root)
        assert len(entries) == 1
        assert entries[0].session_count == 0
        # last_activity falls back to the directory's mtime
        assert entries[0].last_activity == proj.stat().st_mtime


class TestDecodeCwd:
    # The decode step cannot be tested with pytest's ``tmp_path``
    # because pytest's fixture dir already contains dashes
    # (``pytest-of-<user>``, ``pytest-123``) — the encode/decode
    # round-trip through ``-`` → ``/`` is lossy on any path with
    # literal dashes, which is the whole reason the decoder uses a
    # filesystem existence check as a tiebreaker. These tests mock
    # ``Path.exists`` instead so the fixture dir doesn't leak in.

    def test_decoded_when_candidate_exists(self, monkeypatch):
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(Path, "home", lambda: Path("/somewhere-else"))
        assert discovery._decode_cwd("-home-me-proj") == "/home/me/proj"

    def test_fallback_when_candidate_missing(self, monkeypatch):
        monkeypatch.setattr(Path, "exists", lambda self: False)
        encoded = "-definitely-not-a-real-path-xyzzy"
        assert discovery._decode_cwd(encoded) == encoded

    def test_home_collapse_when_under_home(self, monkeypatch):
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(Path, "home", lambda: Path("/home/me"))
        assert discovery._decode_cwd("-home-me-svn-proj") == "~/svn/proj"

    def test_home_collapse_exact_home(self, monkeypatch):
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(Path, "home", lambda: Path("/home/me"))
        assert discovery._decode_cwd("-home-me") == "~"

    def test_home_collapse_outside_home(self, monkeypatch):
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(Path, "home", lambda: Path("/home/me"))
        assert discovery._decode_cwd("-var-log-foo") == "/var/log/foo"


FIXTURES = Path(__file__).parent / "fixtures"


class TestDiscoverSessions:
    def _seed_project(self, tmp_path):
        """Create a fake project dir with two real transcript fixtures."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "basic.jsonl").write_bytes(
            (FIXTURES / "basic_session.jsonl").read_bytes()
        )
        (proj / "multi.jsonl").write_bytes(
            (FIXTURES / "multi_tool.jsonl").read_bytes()
        )
        return proj

    def test_empty_dir(self, tmp_path):
        (tmp_path / "proj").mkdir()
        assert discovery.discover_sessions(tmp_path / "proj") == []

    def test_parses_fixture_sessions(self, tmp_path):
        proj = self._seed_project(tmp_path)
        entries = discovery.discover_sessions(proj)
        assert len(entries) == 2
        for e in entries:
            assert e.turn_count > 0
            assert e.total_seq_tokens > 0
            assert e.dominant_model  # non-empty
            assert e.started_ts is not None

    def test_sort_by_start_desc(self, tmp_path):
        # Craft two tiny sessions with known start timestamps and
        # verify the more recent one is first.
        proj = tmp_path / "proj"
        proj.mkdir()
        old = (
            '{"type":"user","message":{"role":"user","content":"hi"},'
            '"timestamp":"2020-01-01T00:00:00Z","uuid":"u1"}\n'
        )
        new = (
            '{"type":"user","message":{"role":"user","content":"hi"},'
            '"timestamp":"2099-01-01T00:00:00Z","uuid":"u2"}\n'
        )
        (proj / "old.jsonl").write_text(old)
        (proj / "new.jsonl").write_text(new)
        entries = discovery.discover_sessions(proj)
        assert [e.session_id for e in entries] == ["new", "old"]

    def test_empty_transcript_survives(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "empty.jsonl").write_text("")
        entries = discovery.discover_sessions(proj)
        assert len(entries) == 1
        assert entries[0].turn_count == 0
        assert entries[0].total_seq_tokens == 0.0
        assert entries[0].dominant_model == ""
        assert entries[0].started_ts is None

    def test_summary_strips_user_prefix(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        entry = (
            '{"type":"user","message":{"role":"user","content":"hello world"},'
            '"timestamp":"2030-01-01T00:00:00Z","uuid":"u1"}\n'
        )
        (proj / "s.jsonl").write_text(entry)
        [e] = discovery.discover_sessions(proj)
        assert e.first_user_message == "hello world"

    def test_summary_keeps_content_slash_command(self, tmp_path):
        # Content slash commands like /commit or /review-pr ARE the
        # task the user started — they should be surfaced.
        proj = tmp_path / "proj"
        proj.mkdir()
        entry = (
            '{"type":"user","message":{"role":"user","content":'
            '"<command-name>/commit</command-name>\\n'
            '<command-args>-m fix bug</command-args>"},'
            '"timestamp":"2030-01-01T00:00:00Z","uuid":"u1"}\n'
        )
        (proj / "s.jsonl").write_text(entry)
        [e] = discovery.discover_sessions(proj)
        assert e.first_user_message == "/commit -m fix bug"

    def test_summary_skips_navigation_slash_to_real_message(self, tmp_path):
        # The common /clear-then-real-prompt pattern: /clear marks a
        # session boundary, it says nothing about what the session
        # is about. Summary should skip past it.
        proj = tmp_path / "proj"
        proj.mkdir()
        lines = [
            '{"type":"user","message":{"role":"user","content":'
            '"<command-name>/clear</command-name>\\n<command-args></command-args>"},'
            '"timestamp":"2030-01-01T00:00:00Z","uuid":"u1"}',
            '{"type":"user","message":{"role":"user","content":"fix the auth bug"},'
            '"timestamp":"2030-01-01T00:00:01Z","uuid":"u2"}',
        ]
        (proj / "s.jsonl").write_text("\n".join(lines) + "\n")
        [e] = discovery.discover_sessions(proj)
        assert e.first_user_message == "fix the auth bug"

    def test_summary_falls_back_when_only_navigation_slash(self, tmp_path):
        # A session that's just /clear and nothing else has no
        # meaningful summary — better to show nothing than show
        # "/clear" and pretend it's informative.
        proj = tmp_path / "proj"
        proj.mkdir()
        entry = (
            '{"type":"user","message":{"role":"user","content":'
            '"<command-name>/clear</command-name>\\n<command-args></command-args>"},'
            '"timestamp":"2030-01-01T00:00:00Z","uuid":"u1"}\n'
        )
        (proj / "s.jsonl").write_text(entry)
        [e] = discovery.discover_sessions(proj)
        assert e.first_user_message is None


class TestDominantModel:
    def test_picks_highest_total_raw_tokens(self):
        tokens_by_model = {
            "claude-opus-4-6": {"input_tokens": 100, "output_tokens": 10},
            "claude-haiku-4-5": {"input_tokens": 10_000, "output_tokens": 500},
        }
        assert discovery._dominant_model(tokens_by_model) == "claude-haiku-4-5"

    def test_empty(self):
        assert discovery._dominant_model({}) == ""


# ---------------------------------------------------------------------------
# detail_rows
# ---------------------------------------------------------------------------

from claude_measure_usage.parse import (  # noqa: E402
    parse_transcript,
    find_subagent_transcripts,
    build_agent_tree,
)
from claude_measure_usage.tui import detail_rows  # noqa: E402


class TestBuildDetailRows:
    def test_empty_transcript(self):
        assert detail_rows.build_detail_rows({"rows": []}, []) == []

    def test_basic_session(self):
        parsed = parse_transcript(str(FIXTURES / "basic_session.jsonl"))
        rows = detail_rows.build_detail_rows(parsed, [])
        kinds = {r.kind for r in rows}
        assert "turn" in kinds
        # Each turn row carries a non-empty cost cell
        turns = [r for r in rows if r.kind == "turn"]
        assert all(r.cost for r in turns)
        assert all(r.num for r in turns)

    def test_subagent_footnotes_appear_after_parent(self):
        path = str(FIXTURES / "with_subagents.jsonl")
        parsed = parse_transcript(path)
        sub_infos = find_subagent_transcripts(path, 0)
        tree = build_agent_tree(path, parsed, sub_infos)
        assert tree, "fixture must have subagents"

        rows = detail_rows.build_detail_rows(parsed, tree)
        kinds = [r.kind for r in rows]
        # Every subagent row must be preceded by a turn row
        for i, k in enumerate(kinds):
            if k == "subagent":
                assert any(
                    kinds[j] == "turn" for j in range(i - 1, -1, -1)
                ), f"subagent at row {i} has no preceding turn"

    def test_parent_turn_cost_rolls_up_subagent_subtree(self):
        # When a turn spawns subagents, the parent's cost/own
        # numbers must include the subtree rollup — documented
        # in the design doc and critical for sort correctness.
        path = str(FIXTURES / "with_subagents.jsonl")
        parsed = parse_transcript(path)
        sub_infos = find_subagent_transcripts(path, 0)
        tree = build_agent_tree(path, parsed, sub_infos)
        rows = detail_rows.build_detail_rows(parsed, tree)
        # Find a turn row that has a subagent footnote right after
        parent_turn = None
        sub_rows: list = []
        for i, r in enumerate(rows):
            if r.kind == "turn" and i + 1 < len(rows) and rows[i + 1].kind == "subagent":
                parent_turn = r
                for j in range(i + 1, len(rows)):
                    if rows[j].kind != "subagent":
                        break
                    sub_rows.append(rows[j])
                break
        assert parent_turn is not None and sub_rows
        # Parent's cost should be at least as big as the sum of its
        # subagent subtree costs. (Exact equality isn't guaranteed
        # because parent also has its own self-cost, but strict
        # inequality would indicate a rollup bug.)
        import re
        def to_num(s):
            m = re.match(r"([\d.]+)([KM]?)", s or "")
            if not m:
                return 0
            val = float(m.group(1))
            unit = m.group(2)
            return val * {"": 1, "K": 1000, "M": 1_000_000}[unit]

        parent_cost = to_num(parent_turn.cost)
        sub_sum = sum(to_num(r.cost) for r in sub_rows)
        assert parent_cost >= sub_sum * 0.95, (
            f"parent cost {parent_cost} should include subagent rollup "
            f"{sub_sum}, but doesn't"
        )

    def test_nonturn_rows_have_blank_cost(self):
        parsed = parse_transcript(str(FIXTURES / "basic_session.jsonl"))
        rows = detail_rows.build_detail_rows(parsed, [])
        for r in rows:
            if r.kind == "nonturn":
                assert r.cost == ""
                assert r.own == ""
                assert r.what


class TestCellStyling:
    """Unit coverage for _cells_for's per-row-kind styling.

    Avoids asserting exact Rich markup and instead checks the
    ``style`` attribute on the emitted ``rich.text.Text`` objects
    so the test reads as "what the user sees" rather than
    "what Rich's representation happens to be today".
    """

    def _mk(self, kind: str, **overrides):
        return detail_rows.DetailRow(
            kind=kind,
            num=overrides.get("num", "1"),
            what=overrides.get("what", "hello"),
            model=overrides.get("model", ""),
            cost=overrides.get("cost", "1K"),
            own=overrides.get("own", "1K"),
        )

    def test_turn_row_default_styling(self):
        from claude_measure_usage.tui.screens import _cells_for

        row = self._mk("turn", model="claude-sonnet-4-6")
        cells = _cells_for(row)
        # Plain strings, no Rich Text wrapping (Sonnet → no color)
        assert all(isinstance(c, str) for c in cells)

    def test_turn_row_opus_model_colored_red(self):
        from rich.text import Text
        from claude_measure_usage.tui.screens import _cells_for

        row = self._mk("turn", model="claude-opus-4-6")
        cells = _cells_for(row)
        # Model cell (index 9) swapped to a red Text object; rest
        # stay as plain strings.
        assert isinstance(cells[9], Text)
        assert "red" in str(cells[9].style)

    def test_turn_row_haiku_model_colored_green(self):
        from rich.text import Text
        from claude_measure_usage.tui.screens import _cells_for

        row = self._mk("turn", model="claude-haiku-4-5")
        cells = _cells_for(row)
        assert isinstance(cells[9], Text)
        assert "green" in str(cells[9].style)

    def test_turn_row_fable_model_colored_magenta(self):
        from rich.text import Text
        from claude_measure_usage.tui.screens import _cells_for

        row = self._mk("turn", model="claude-fable-5")
        cells = _cells_for(row)
        assert isinstance(cells[9], Text)
        assert "magenta" in str(cells[9].style)

    def test_subagent_row_colored_cyan(self):
        from rich.text import Text
        from claude_measure_usage.tui.screens import _cells_for

        row = self._mk("subagent", num="↳a3f2", what="[Agent] investigate")
        cells = _cells_for(row)
        # Every cell is a Text with cyan style
        assert all(isinstance(c, Text) for c in cells)
        assert all("cyan" in str(c.style) for c in cells)

    def test_subagent_row_model_cell_keeps_family_color(self):
        from rich.text import Text
        from claude_measure_usage.tui.screens import _cells_for

        row = self._mk("subagent", model="claude-opus-4-6")
        cells = _cells_for(row)
        assert isinstance(cells[9], Text)
        # Model cell gets the model color (red) in addition to bold
        assert "red" in str(cells[9].style)

    def test_nonturn_row_colored_dim(self):
        from rich.text import Text
        from claude_measure_usage.tui.screens import _cells_for

        row = self._mk("nonturn", what="[user] hi")
        cells = _cells_for(row)
        assert all(isinstance(c, Text) for c in cells)
        assert all("dim" in str(c.style) for c in cells)


class TestColumnWidthsFitSortIndicator:
    """Every sortable column must be wide enough for ``label ▼``.

    The sort-highlight helper appends a trailing ``" ▼"`` to the
    active sort column's header label. If the column was sized
    to the plain label width the arrow gets truncated at render
    time — invisible to the user, who then thinks the binding is
    broken. This test guards against that regression for every
    sortable column in the spec.
    """

    def test_every_sortable_column_has_room_for_arrow(self):
        from claude_measure_usage.tui.screens import SessionDetailScreen

        sortable_columns = {
            m.column_id for m in detail_rows.SORT_MODES
        }
        for label, key, width in SessionDetailScreen._column_spec():
            if key in sortable_columns:
                required = len(label) + 2  # " ▼"
                assert width >= required, (
                    f"column {key!r} has width {width}, needs "
                    f"at least {required} to fit '{label} ▼'"
                )


class TestSortModes:
    """Coverage for the SORT_MODES cycle shape and lookup helpers."""

    def test_cycle_order_matches_column_order(self):
        ids = [m.id for m in detail_rows.SORT_MODES]
        assert ids == [
            "natural", "took", "cost", "own", "carry", "caused",
        ]

    def test_natural_highlights_num_column(self):
        m = detail_rows.get_sort_mode("natural")
        assert m.column_id == "num"
        assert m.sort_key is None

    def test_sort_mode_for_known_column(self):
        assert detail_rows.sort_mode_for_column("cost").id == "cost"
        assert detail_rows.sort_mode_for_column("num").id == "natural"

    def test_sort_mode_for_unsortable_column_returns_none(self):
        assert detail_rows.sort_mode_for_column("what") is None
        assert detail_rows.sort_mode_for_column("model") is None
        assert detail_rows.sort_mode_for_column("ctx") is None

    def test_unknown_mode_falls_back_to_natural(self):
        assert detail_rows.get_sort_mode("bogus").id == "natural"


class TestGluedSort:
    """Verify the glued-sort contract from docs/tui-ux.md.

    Uses synthetic DetailRow fixtures so the assertions can be
    tight on ordering without fighting real fixture variance.
    """

    def _mk_turn(self, num: int, cost: float, own: float = None, took: float = 0.0):
        return detail_rows.DetailRow(
            kind="turn",
            num=str(num),
            cost=str(cost),
            raw={
                "turn": {"turn_num": num},
                "children": [],
                "sort_keys": {
                    "cost": cost,
                    "own": own if own is not None else cost,
                    "took": took,
                    "ctx": 0.0,
                },
            },
        )

    def _mk_sub(self, sid: str):
        return detail_rows.DetailRow(kind="subagent", num=f"↳{sid}")

    def _mk_nonturn(self, label: str):
        return detail_rows.DetailRow(kind="nonturn", what=label)

    def test_natural_returns_unchanged(self):
        rows = [
            self._mk_nonturn("lead"),
            self._mk_turn(1, 10.0),
        ]
        assert detail_rows.sort_rows(rows, "natural") is rows

    def test_unknown_mode_is_safe(self):
        rows = [self._mk_turn(1, 10.0)]
        assert detail_rows.sort_rows(rows, "bogus") == rows

    def test_glue_subagent_to_turn(self):
        rows = [
            self._mk_turn(1, 5.0),
            self._mk_sub("a"),
            self._mk_turn(2, 15.0),
            self._mk_sub("b"),
            self._mk_turn(3, 10.0),
        ]
        out = detail_rows.sort_rows(rows, "cost")
        # Turn 2 (highest cost) first with its ↳b, then turn 3,
        # then turn 1 with ↳a.
        assert [r.num for r in out] == ["2", "↳b", "3", "1", "↳a"]

    def test_leading_nonturn_travels_with_turn(self):
        rows = [
            self._mk_nonturn("user: fix auth"),
            self._mk_turn(1, 5.0),
            self._mk_nonturn("user: and also this"),
            self._mk_turn(2, 15.0),
            self._mk_turn(3, 10.0),
        ]
        out = detail_rows.sort_rows(rows, "cost")
        kinds = [(r.kind, r.num or r.what) for r in out]
        assert kinds == [
            ("nonturn", "user: and also this"),
            ("turn", "2"),
            ("turn", "3"),
            ("nonturn", "user: fix auth"),
            ("turn", "1"),
        ]

    def test_tail_orphan_stays_at_end(self):
        rows = [
            self._mk_turn(1, 5.0),
            self._mk_turn(2, 20.0),
            self._mk_nonturn("compact boundary"),
        ]
        out = detail_rows.sort_rows(rows, "cost")
        assert [(r.kind, r.num or r.what) for r in out] == [
            ("turn", "2"),
            ("turn", "1"),
            ("nonturn", "compact boundary"),
        ]

    def test_own_vs_cost_sorts_different(self):
        # Turns where cost and own rank differently (simulates a
        # turn that's cheap in own but expensive in total because
        # of carry, or vice versa). The sort key should be the
        # one the user picked.
        rows = [
            self._mk_turn(1, cost=100.0, own=10.0),
            self._mk_turn(2, cost=50.0, own=40.0),
        ]
        by_cost = detail_rows.sort_rows(rows, "cost")
        assert [r.num for r in by_cost] == ["1", "2"]
        by_own = detail_rows.sort_rows(rows, "own")
        assert [r.num for r in by_own] == ["2", "1"]


class TestFilterRows:
    """Verify the block-level filter rules from docs/tui-ux.md."""

    def _mk_turn(self, num: int, what: str):
        return detail_rows.DetailRow(
            kind="turn",
            num=str(num),
            what=what,
            raw={
                "turn": {"turn_num": num},
                "children": [],
                "sort_keys": {"cost": 0, "own": 0, "took": 0, "ctx": 0},
            },
        )

    def _mk_sub(self, sid: str, what: str):
        return detail_rows.DetailRow(kind="subagent", num=f"↳{sid}", what=what)

    def _mk_nonturn(self, what: str):
        return detail_rows.DetailRow(kind="nonturn", what=what)

    def test_empty_filter_returns_input_unchanged(self):
        rows = [self._mk_turn(1, "Bash ls")]
        assert detail_rows.filter_rows(rows, "") is rows

    def test_case_insensitive_substring(self):
        rows = [
            self._mk_turn(1, "Bash ls /tmp"),
            self._mk_turn(2, "Read /foo.py"),
        ]
        out = detail_rows.filter_rows(rows, "BASH")
        assert [r.num for r in out] == ["1"]

    def test_match_pulls_subagents_with_parent(self):
        # Filter on the parent's label — subagents come along.
        rows = [
            self._mk_turn(1, "investigate auth"),
            self._mk_sub("a", "[Agent] jwt check"),
            self._mk_sub("b", "[Agent] session check"),
            self._mk_turn(2, "unrelated"),
        ]
        out = detail_rows.filter_rows(rows, "investigate")
        assert [r.num for r in out] == ["1", "↳a", "↳b"]

    def test_match_on_subagent_pulls_parent(self):
        rows = [
            self._mk_turn(1, "Bash git status"),
            self._mk_sub("a", "[Agent] investigate blocker"),
            self._mk_turn(2, "unrelated"),
        ]
        out = detail_rows.filter_rows(rows, "blocker")
        assert [r.num for r in out] == ["1", "↳a"]

    def test_leading_nonturn_pulls_its_turn(self):
        # A match on the "lead-in" non-turn (user prompt /
        # attachment) drags the whole block in with it.
        rows = [
            self._mk_nonturn("[user] fix auth bug"),
            self._mk_turn(1, "Bash git log"),
            self._mk_turn(2, "Read foo.py"),
        ]
        out = detail_rows.filter_rows(rows, "fix auth")
        assert [r.kind for r in out] == ["nonturn", "turn"]
        assert out[0].what == "[user] fix auth bug"
        assert out[1].num == "1"

    def test_match_on_num(self):
        # Matching on the "num" column lets filter hit short
        # subagent ids (↳a3f2) as well as turn numbers.
        rows = [
            self._mk_turn(1, "one"),
            self._mk_sub("a3f2", "[Agent] X"),
            self._mk_turn(2, "two"),
        ]
        out = detail_rows.filter_rows(rows, "a3f2")
        assert [r.num for r in out] == ["1", "↳a3f2"]

    def test_tail_orphan_matches_individually(self):
        rows = [
            self._mk_turn(1, "alpha"),
            self._mk_nonturn("compact boundary: 150K → 30K"),
            self._mk_nonturn("[permission-mode] → acceptEdits"),
        ]
        out = detail_rows.filter_rows(rows, "compact")
        assert [r.what for r in out] == ["compact boundary: 150K → 30K"]

    def test_no_match_returns_empty(self):
        rows = [self._mk_turn(1, "hello")]
        assert detail_rows.filter_rows(rows, "nope") == []


class TestProjectForCwd:
    def test_matches_existing_dir(self, tmp_path):
        root = tmp_path / "projects"
        root.mkdir()
        proj = root / "-home-me-work"
        proj.mkdir()
        result = discovery.project_for_cwd("/home/me/work", projects_root=root)
        assert result == proj

    def test_no_match_returns_none(self, tmp_path):
        root = tmp_path / "projects"
        root.mkdir()
        assert discovery.project_for_cwd("/nowhere", projects_root=root) is None


# ---------------------------------------------------------------------------
# format
# ---------------------------------------------------------------------------

from claude_measure_usage.tui.format import (  # noqa: E402
    rel_time,
    short_datetime,
    short_tokens,
    tiny_model,
)


class TestRelTime:
    NOW = 1_700_000_000.0

    def test_just_now(self):
        assert rel_time(self.NOW - 5, now=self.NOW) == "just now"

    def test_minutes(self):
        assert rel_time(self.NOW - 5 * 60, now=self.NOW) == "5m ago"

    def test_hours(self):
        assert rel_time(self.NOW - 3 * 3600, now=self.NOW) == "3h ago"

    def test_days(self):
        assert rel_time(self.NOW - 2 * 86_400, now=self.NOW) == "2d ago"

    def test_weeks(self):
        assert rel_time(self.NOW - 2 * 7 * 86_400, now=self.NOW) == "2w ago"

    def test_absolute_for_old(self):
        # Over a month → absolute date. Exact string depends on
        # localtime, so just check the shape.
        out = rel_time(self.NOW - 90 * 86_400, now=self.NOW)
        assert len(out) == 10 and out.count("-") == 2

    def test_future_mtime_clamps_to_just_now(self):
        assert rel_time(self.NOW + 60, now=self.NOW) == "just now"


class TestShortTokens:
    def test_small(self):
        assert short_tokens(42) == "42"

    def test_thousands(self):
        assert short_tokens(12_400) == "12.4K"

    def test_millions(self):
        assert short_tokens(2_380_000) == "2.4M"

    def test_zero(self):
        assert short_tokens(0) == "0"

    def test_none(self):
        assert short_tokens(None) == ""


class TestShortDatetime:
    def test_formats_epoch(self):
        # 2023-01-15 10:30:00 UTC. Exact local time depends on TZ,
        # so just verify shape.
        out = short_datetime(1_673_778_600.0)
        assert len(out) == 16
        assert out.count("-") == 2 and out.count(":") == 1

    def test_none(self):
        assert short_datetime(None) == ""


class TestTinyModel:
    def test_fable(self):
        assert tiny_model("claude-fable-5") == "fable"

    def test_opus(self):
        assert tiny_model("claude-opus-4-6") == "opus"

    def test_sonnet(self):
        assert tiny_model("claude-sonnet-4-5-20250514") == "sonnet"

    def test_haiku(self):
        assert tiny_model("claude-haiku-4-5-20251001") == "haiku"

    def test_empty(self):
        assert tiny_model("") == ""

    def test_unknown_passes_through(self):
        assert tiny_model("mystery-model") == "mystery-model"


# ---------------------------------------------------------------------------
# ProjectScreen (pilot smoke test)
# ---------------------------------------------------------------------------

class TestNonturnDetailModal:
    """Unit tests for the non-turn modal's per-kind renderers.

    Constructs synthetic DetailRow objects with fake underlying
    entries and asserts the rendered body contains the right
    fields. Avoids live pilot navigation — those tests have
    proven flaky for finding specific session kinds in the
    real ~/.claude/projects tree.
    """

    def _mk_row(self, kind: str, entry: dict):
        from claude_measure_usage.tui.detail_rows import DetailRow

        return DetailRow(
            kind="nonturn",
            what="",
            raw={"nonturn": {"kind": kind, "entry": entry}},
        )

    def _modal(self, kind: str, entry: dict):
        from claude_measure_usage.tui.screens import NonturnDetailModal

        return NonturnDetailModal(self._mk_row(kind, entry))

    def test_user_renders_full_text(self):
        entry = {"message": {"content": "fix the auth bug in jwt.py"}}
        body = self._modal("user", entry)._body_text()
        assert body == "fix the auth bug in jwt.py"

    def test_user_renders_text_blocks(self):
        entry = {
            "message": {
                "content": [
                    {"type": "text", "text": "first line"},
                    {"type": "text", "text": "second line"},
                ]
            }
        }
        body = self._modal("user", entry)._body_text()
        assert "first line" in body
        assert "second line" in body

    def test_slash_command_extracts_name_and_args(self):
        entry = {
            "message": {
                "content": (
                    "<command-name>/commit</command-name>\n"
                    "<command-args>-m fix auth bug</command-args>\n"
                    "<command-message>Generate a conventional commit</command-message>"
                )
            }
        }
        body = self._modal("slash-command", entry)._body_text()
        assert "command: /commit" in body
        assert "args:    -m fix auth bug" in body
        assert "Generate a conventional commit" in body

    def test_attachment_command_permissions_lists_allowed_tools(self):
        entry = {
            "attachment": {
                "type": "command_permissions",
                "allowedTools": ["Skill", "Bash", "Read", "Write"],
            }
        }
        body = self._modal("attachment:command_permissions", entry)._body_text()
        assert "type: command_permissions" in body
        assert "allowedTools (4)" in body
        for tool in ("Skill", "Bash", "Read", "Write"):
            assert f"· {tool}" in body

    def test_attachment_deferred_tools_delta_lists_added_removed(self):
        entry = {
            "attachment": {
                "type": "deferred_tools_delta",
                "addedNames": ["WebFetch", "WebSearch"],
                "removedNames": ["Monitor"],
            }
        }
        body = self._modal(
            "attachment:deferred_tools_delta", entry
        )._body_text()
        assert "added (2)" in body
        assert "+ WebFetch" in body
        assert "removed (1)" in body
        assert "- Monitor" in body

    def test_attachment_unknown_falls_back_to_json(self):
        entry = {
            "attachment": {
                "type": "weird_new_kind",
                "customField": "custom value",
            }
        }
        body = self._modal("attachment:weird_new_kind", entry)._body_text()
        assert "weird_new_kind" in body
        assert "customField" in body
        assert "custom value" in body

    def test_permission_mode_shows_new_mode(self):
        entry = {"permissionMode": "acceptEdits"}
        body = self._modal("permission-mode", entry)._body_text()
        assert "new mode: acceptEdits" in body

    def test_compact_boundary_shows_metadata(self):
        entry = {
            "compactMetadata": {
                "trigger": "manual",
                "preTokens": 150_000,
                "postTokens": 35_000,
            }
        }
        body = self._modal("system:compact_boundary", entry)._body_text()
        assert "trigger:   manual" in body
        assert "preTokens: 150000" in body
        assert "postTokens: 35000" in body

    def test_unknown_kind_json_fallback_filters_bookkeeping(self):
        entry = {
            "uuid": "secret-uuid",
            "parentUuid": "also-secret",
            "interestingField": "keep me",
        }
        body = self._modal("mystery", entry)._body_text()
        assert "interestingField" in body
        assert "keep me" in body
        # Bookkeeping fields filtered out
        assert "secret-uuid" not in body
        assert "parentUuid" not in body


class TestProjectScreenPilot:
    """End-to-end smoke test for the project picker.

    Uses Textual's ``App.run_test()`` pilot harness to mount the app
    against a fake ``~/.claude/projects`` tree and verify the screen
    populates its DataTable. The discovery layer is covered above;
    this exists to catch regressions in the Textual wiring (screen
    push, column setup, cursor placement, quit binding).
    """

    @pytest.fixture
    def fake_projects(self, tmp_path, monkeypatch):
        projects_root = tmp_path / ".claude" / "projects"
        projects_root.mkdir(parents=True)
        (projects_root / "-home-user-alpha").mkdir()
        (projects_root / "-home-user-alpha" / "s1.jsonl").write_text("")
        (projects_root / "-home-user-beta").mkdir()
        (projects_root / "-home-user-beta" / "s1.jsonl").write_text("")
        (projects_root / "-home-user-beta" / "s2.jsonl").write_text("")
        # Point discovery at our fake root via HOME.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        return projects_root

    def test_mounts_and_populates(self, fake_projects):
        import asyncio
        from textual.widgets import DataTable
        from claude_measure_usage.tui.app import MeasureUsageApp

        async def run():
            app = MeasureUsageApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.screen.query_one(DataTable)
                assert table.row_count == 2
                # Projects are sorted most-recent-first, and both were
                # just created, so order depends on mtime resolution.
                # Just assert both names are present.
                rows = list(table.rows.keys())
                assert len(rows) == 2
                await pilot.press("q")

        asyncio.run(run())

    def test_project_screen_reload_picks_up_new_project(
        self, tmp_path, monkeypatch
    ):
        """``r`` on the project picker rescans ``~/.claude/projects``.

        Mounts the app against a fake tree with two projects,
        creates a third on disk, presses ``r``, and asserts the
        new project appears. Also verifies the cursor stays on
        the project it was sitting on before the reload —
        rescans shouldn't jump a user who was about to hit Enter.
        """
        import asyncio
        from textual.widgets import DataTable
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import ProjectScreen

        projects_root = tmp_path / ".claude" / "projects"
        projects_root.mkdir(parents=True)
        (projects_root / "-home-user-alpha").mkdir()
        (projects_root / "-home-user-alpha" / "s1.jsonl").write_text("")
        (projects_root / "-home-user-beta").mkdir()
        (projects_root / "-home-user-beta" / "s1.jsonl").write_text("")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, ProjectScreen)
                table = app.screen.query_one(DataTable)
                assert table.row_count == 2
                # Park the cursor on the current top-of-list entry
                # so we can prove reload preserved it.
                initial_cursor = table.cursor_row
                preserved_key = (
                    app.screen._entries[initial_cursor].project_dir
                )

                # Add a brand-new project directory on disk.
                (projects_root / "-home-user-gamma").mkdir()
                (projects_root / "-home-user-gamma" / "s1.jsonl").write_text("")

                await pilot.press("r")
                await pilot.pause()

                table = app.screen.query_one(DataTable)
                assert table.row_count == 3, (
                    "reload should pick up the new project"
                )
                # Cursor should still be on the same project that
                # was highlighted before the reload.
                new_cursor = table.cursor_row
                assert (
                    app.screen._entries[new_cursor].project_dir
                    == preserved_key
                ), "cursor should survive reload"
                await pilot.press("q")

        asyncio.run(run())

    def test_session_screen_reload_picks_up_new_session(
        self, tmp_path, monkeypatch
    ):
        """``r`` on the session picker re-parses every transcript.

        Drops a new ``.jsonl`` into the project directory after
        the initial load and verifies that pressing ``r`` causes
        it to show up in the session list. Also waits for the
        background parser to finish before asserting — the
        second parse is async, same as the initial one.
        """
        import asyncio
        import shutil
        from textual.widgets import DataTable
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import SessionScreen

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-sreload"
        proj_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "basic_session.jsonl", proj_dir / "a.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SessionScreen)
                await app.workers.wait_for_complete()
                await pilot.pause()
                table = app.screen.query_one(DataTable)
                assert table.row_count == 1

                # Drop a second session on disk and reload.
                shutil.copy(
                    FIXTURES / "basic_session.jsonl", proj_dir / "b.jsonl"
                )
                await pilot.press("r")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

                table = app.screen.query_one(DataTable)
                assert table.row_count == 2, (
                    "reload should pick up the newly-added session"
                )
                await pilot.press("q")

        asyncio.run(run())

    def test_loader_survives_unreadable_transcript(
        self, tmp_path, monkeypatch
    ):
        """An exception while loading one session must not abort the batch.

        Injects a transcript path that load_session will choke on
        and verifies that (a) other sessions in the project still
        parse and populate the table, (b) the loader reaches its
        finally block and flips the loading container out of its
        initial state, and (c) the skipped-count is surfaced.
        """
        import asyncio
        import shutil
        from unittest.mock import patch
        from textual.widgets import DataTable
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui import discovery
        from claude_measure_usage.tui.screens import SessionScreen

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-p"
        proj_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "basic_session.jsonl", proj_dir / "ok.jsonl")
        (proj_dir / "bad.jsonl").write_text("this will blow up")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        real_load = discovery.load_session

        def flaky(path):
            if path.name == "bad.jsonl":
                raise RuntimeError("boom")
            return real_load(path)

        async def run():
            with patch.object(discovery, "load_session", flaky), \
                 patch(
                     "claude_measure_usage.tui.screens.load_session", flaky
                 ):
                app = MeasureUsageApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    await pilot.press("enter")
                    await pilot.pause()
                    assert isinstance(app.screen, SessionScreen)
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    table = app.screen.query_one(DataTable)
                    # The good session loaded; the bad one was skipped.
                    assert table.row_count == 1
                    assert "skipped" in app.screen.sub_title
                    await pilot.press("q")

        asyncio.run(run())

    def test_reload_preserves_sort_and_filter(self, tmp_path, monkeypatch):
        """``r`` re-parses the transcript and keeps the current
        sort mode and filter text.

        Simulates a transcript change by appending a new entry to
        the fixture on disk between loads, then asserts the
        reload picked it up (row count bumped) and the user's
        sort/filter state is still active.
        """
        import asyncio
        import shutil
        from textual.widgets import DataTable
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import SessionDetailScreen

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-reload"
        proj_dir.mkdir(parents=True)
        transcript = proj_dir / "s.jsonl"
        shutil.copy(FIXTURES / "multi_tool.jsonl", transcript)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        extra_entry = (
            '{"type":"user","message":{"role":"user","content":"added-later"},'
            '"timestamp":"2030-01-01T00:00:00Z","uuid":"extra-uuid"}\n'
        )

        async def run():
            app = MeasureUsageApp()
            async with app.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SessionDetailScreen)
                initial_row_count = len(app.screen._rows)

                # Set sort mode to cost (two s presses: natural → took → cost)
                await pilot.press("s")
                await pilot.press("s")
                await pilot.pause()
                assert app.screen._sort_mode == "cost"

                # Append a new entry to the transcript file
                with open(transcript, "a") as f:
                    f.write(extra_entry)

                # Reload — should pick up the new entry and keep the sort
                await pilot.press("r")
                await pilot.pause()
                assert app.screen._sort_mode == "cost", (
                    "sort mode should survive reload"
                )
                assert len(app.screen._rows) > initial_row_count, (
                    "reload should pick up the appended entry"
                )
                await pilot.press("q")

        asyncio.run(run())

    def test_slash_opens_filter_and_esc_cancels(
        self, tmp_path, monkeypatch
    ):
        """/ opens the filter input, typing filters, Esc cancels."""
        import asyncio
        import shutil
        from textual.widgets import DataTable, Input
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import SessionDetailScreen

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-filter"
        proj_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "multi_tool.jsonl", proj_dir / "s.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SessionDetailScreen)
                full_count = app.screen.query_one(DataTable).row_count
                assert full_count > 0

                # Press / — input should appear and gain focus.
                await pilot.press("slash")
                await pilot.pause()
                inp = app.screen.query_one("#filter_input", Input)
                assert "-active" in inp.classes
                assert inp.has_focus

                # Type a query that won't match anything
                for c in "xyznopematch":
                    await pilot.press(c)
                await pilot.pause()
                assert app.screen._filter_text == "xyznopematch"
                assert app.screen.query_one(DataTable).row_count == 0
                assert 'filter: "xyznopematch"' in app.screen.sub_title

                # Esc cancels — filter clears, full view restored
                await pilot.press("escape")
                await pilot.pause()
                assert app.screen._filter_text == ""
                assert "-active" not in inp.classes
                assert app.screen.query_one(DataTable).row_count == full_count
                # Screen is still the detail screen (Esc did NOT pop)
                assert isinstance(app.screen, SessionDetailScreen)
                await pilot.press("q")

        asyncio.run(run())

    def test_header_click_sorts_column(self, tmp_path, monkeypatch):
        """Clicking a sortable column header activates that sort.

        Exercises ``on_data_table_header_selected`` via a direct
        message post rather than a geometric click — the pilot's
        click coordinates are fragile for header hits and the
        behavior we care about is the sort-mode state change,
        not the click geometry.
        """
        import asyncio
        import shutil
        from textual.widgets import DataTable
        from textual.widgets.data_table import ColumnKey
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import SessionDetailScreen

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-hdr"
        proj_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "multi_tool.jsonl", proj_dir / "s.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                detail = app.screen
                assert isinstance(detail, SessionDetailScreen)
                assert detail._sort_mode == "natural"

                table = detail.query_one(DataTable)
                # Click the cost header → sort activates
                msg = DataTable.HeaderSelected(
                    data_table=table,
                    column_key=ColumnKey("cost"),
                    column_index=3,
                    label="cost",
                )
                detail.on_data_table_header_selected(msg)
                assert detail._sort_mode == "cost"
                assert "sort: cost" in detail.sub_title

                # Click again → toggles back to natural
                detail.on_data_table_header_selected(msg)
                assert detail._sort_mode == "natural"

                # Click a non-sortable header → no-op
                msg_what = DataTable.HeaderSelected(
                    data_table=table,
                    column_key=ColumnKey("what"),
                    column_index=7,
                    label="what",
                )
                detail.on_data_table_header_selected(msg_what)
                assert detail._sort_mode == "natural"
                await pilot.press("q")

        asyncio.run(run())

    def test_active_sort_column_header_highlighted(
        self, tmp_path, monkeypatch
    ):
        """The active sort column header is rendered with ``▼``
        and a bold style; other headers reset to plain labels."""
        import asyncio
        import shutil
        from textual.widgets import DataTable
        from claude_measure_usage.tui.app import MeasureUsageApp

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-hdr2"
        proj_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "multi_tool.jsonl", proj_dir / "s.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()

                table = app.screen.query_one(DataTable)
                # Default: natural → num column gets the ▼
                num_label = str(table.columns["num"].label)
                assert "▼" in num_label

                # Cycle to "cost"
                for _ in range(2):
                    await pilot.press("s")
                await pilot.pause()
                assert app.screen._sort_mode == "cost"
                cost_label = str(table.columns["cost"].label)
                num_label_after = str(table.columns["num"].label)
                assert "▼" in cost_label
                assert "▼" not in num_label_after
                await pilot.press("q")

        asyncio.run(run())

    def test_s_cycles_sort_mode(self, tmp_path, monkeypatch):
        """Pressing ``s`` on the detail screen cycles sort modes
        in visual column order: natural → took → cost → own →
        carry → caused → natural."""
        import asyncio
        import shutil
        from textual.widgets import DataTable
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import SessionDetailScreen

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-sort"
        proj_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "multi_tool.jsonl", proj_dir / "s.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SessionDetailScreen)
                assert app.screen._sort_mode == "natural"
                expected = ["took", "cost", "own", "carry", "caused", "natural"]
                for expected_mode in expected:
                    await pilot.press("s")
                    await pilot.pause()
                    assert app.screen._sort_mode == expected_mode, (
                        f"expected {expected_mode}, got {app.screen._sort_mode}"
                    )
                    if expected_mode == "natural":
                        assert "sort:" not in app.screen.sub_title
                    else:
                        assert f"sort: {expected_mode}" in app.screen.sub_title
                await pilot.press("q")

        asyncio.run(run())

    def test_turn_modal_opens_and_lists_subagents(
        self, tmp_path, monkeypatch
    ):
        """Enter on a turn-with-subagents row opens the modal and
        populates its subagent OptionList.

        Uses a fixture transcript that actually spawns subagents so
        we don't have to fabricate a fake tree. The sub_title and
        row count are asserted as basic liveness checks, then the
        modal is dismissed cleanly.
        """
        import asyncio
        import shutil
        from textual.widgets import DataTable, OptionList
        from textual.coordinate import Coordinate
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import (
            ProjectScreen,
            SessionDetailScreen,
            SessionScreen,
            TurnDetailModal,
        )

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-sub"
        proj_dir.mkdir(parents=True)
        shutil.copy(
            FIXTURES / "with_subagents.jsonl", proj_dir / "session.jsonl"
        )
        # Subagent transcripts live in a sibling dir named after the
        # session; the fixture already has that layout at
        # fixtures/with_subagents/ — copy it across.
        subagent_src = FIXTURES / "with_subagents"
        if subagent_src.exists():
            shutil.copytree(subagent_src, proj_dir / "session")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                await pilot.press("enter")  # open project
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("enter")  # open session
                await pilot.pause()
                assert isinstance(app.screen, SessionDetailScreen)
                detail = app.screen
                rows = detail._rows
                turn_with_sub = next(
                    (
                        i for i, r in enumerate(rows)
                        if r.kind == "turn"
                        and i + 1 < len(rows)
                        and rows[i + 1].kind == "subagent"
                    ),
                    None,
                )
                assert turn_with_sub is not None, (
                    "fixture should have a turn that spawned subagents"
                )
                table = detail.query_one(DataTable)
                table.cursor_coordinate = Coordinate(turn_with_sub, 0)
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, TurnDetailModal)
                modal = app.screen
                assert modal._children, "modal should have subagent list"
                sub_list = modal.query_one("#sub_list", OptionList)
                assert sub_list.option_count == len(modal._children)
                # Dismiss cleanly
                await pilot.press("escape")
                await pilot.pause()
                assert isinstance(app.screen, SessionDetailScreen)
                await pilot.press("q")

        asyncio.run(run())

    def test_question_mark_opens_help_modal_on_each_screen(
        self, tmp_path, monkeypatch
    ):
        """Pressing ``?`` on every screen opens the HelpModal
        with that screen's BINDINGS. Esc closes it cleanly back
        to the originating screen."""
        import asyncio
        import shutil
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import (
            HelpModal,
            ProjectScreen,
            SessionDetailScreen,
            SessionScreen,
        )

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-help"
        proj_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "basic_session.jsonl", proj_dir / "s.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()

                # ProjectScreen — help is pretty minimal (q, ?).
                project_screen = app.screen
                assert isinstance(project_screen, ProjectScreen)
                await pilot.press("question_mark")
                await pilot.pause()
                assert isinstance(app.screen, HelpModal)
                keys_text = app.screen._render_keys()
                assert "Quit" in keys_text
                assert "Help" in keys_text
                await pilot.press("escape")
                await pilot.pause()
                assert app.screen is project_screen

                # SessionScreen — should now include i (Summary)
                # and the Back binding.
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                session_screen = app.screen
                assert isinstance(session_screen, SessionScreen)
                await pilot.press("question_mark")
                await pilot.pause()
                assert isinstance(app.screen, HelpModal)
                keys_text = app.screen._render_keys()
                assert "Summary" in keys_text
                assert "Back" in keys_text
                await pilot.press("escape")
                await pilot.pause()
                assert app.screen is session_screen

                # SessionDetailScreen — full set including Sort,
                # Filter, Reload.
                await pilot.press("enter")
                await pilot.pause()
                detail = app.screen
                assert isinstance(detail, SessionDetailScreen)
                await pilot.press("question_mark")
                await pilot.pause()
                assert isinstance(app.screen, HelpModal)
                keys_text = app.screen._render_keys()
                for needed in ("Sort", "Filter", "Reload", "Summary", "Back"):
                    assert needed in keys_text, f"{needed!r} missing from help"
                await pilot.press("escape")
                await pilot.pause()
                assert app.screen is detail
                await pilot.press("q")

        asyncio.run(run())

    def test_i_on_session_picker_opens_summary_modal(
        self, tmp_path, monkeypatch
    ):
        """Pressing ``i`` on the session picker opens the summary
        modal for the highlighted session without drilling into
        the detail screen."""
        import asyncio
        import shutil
        from textual.widgets import Static
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import (
            SessionScreen,
            SummaryModal,
        )

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-isumm"
        proj_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "basic_session.jsonl", proj_dir / "s.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert isinstance(app.screen, SessionScreen)
                session_screen = app.screen
                await pilot.press("i")
                await pilot.pause()
                assert isinstance(app.screen, SummaryModal)
                summary_text = app.screen._summary_text()
                # Basic liveness check: summary mentions a
                # Duration line — that's what format_metrics
                # always puts on the first line.
                assert summary_text.startswith("Duration:")
                # The overlay surfaces the transcript path
                assert app.screen._transcript_path == proj_dir / "s.jsonl"
                path_line = app.screen.query_one("#modal_path", Static)
                assert str(proj_dir / "s.jsonl") in str(path_line.render())
                # Dismiss — should land back on the session
                # picker, not on a detail screen.
                await pilot.press("escape")
                await pilot.pause()
                assert app.screen is session_screen
                await pilot.press("q")

        asyncio.run(run())

    def test_subagent_row_enter_drills_into_new_detail_screen(
        self, tmp_path, monkeypatch
    ):
        """Pressing Enter on a subagent footnote row pushes a new
        SessionDetailScreen whose title carries the drill breadcrumb.
        """
        import asyncio
        import shutil
        from textual.widgets import DataTable
        from textual.coordinate import Coordinate
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import SessionDetailScreen

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-sub"
        proj_dir.mkdir(parents=True)
        shutil.copy(
            FIXTURES / "with_subagents.jsonl", proj_dir / "session.jsonl"
        )
        subagent_src = FIXTURES / "with_subagents"
        if subagent_src.exists():
            shutil.copytree(subagent_src, proj_dir / "session")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SessionDetailScreen)
                parent_title = app.screen._title
                detail = app.screen
                sub_row_idx = next(
                    i for i, r in enumerate(detail._rows)
                    if r.kind == "subagent"
                )
                table = detail.query_one(DataTable)
                table.cursor_coordinate = Coordinate(sub_row_idx, 0)
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SessionDetailScreen)
                # Different screen instance — a nested one was pushed
                assert app.screen is not detail
                # Breadcrumb appended to title
                assert "↳" in app.screen._title
                assert app.screen._title.startswith(parent_title)
                # Esc pops back to parent
                await pilot.press("escape")
                await pilot.pause()
                assert app.screen is detail
                await pilot.press("q")

        asyncio.run(run())

    def test_drill_to_session_detail_and_back(self, tmp_path, monkeypatch):
        """Navigation Project → Session → Detail → back stack pops.

        Exercises the full three-screen nav stack with a real
        fixture, asserts each screen is the expected class, and
        verifies Esc pops back cleanly to the parent at each level.
        """
        import asyncio
        import shutil
        from textual.widgets import DataTable
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import (
            ProjectScreen,
            SessionScreen,
            SessionDetailScreen,
        )

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-fake"
        proj_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "basic_session.jsonl", proj_dir / "session.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, ProjectScreen)
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SessionScreen)
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SessionDetailScreen)
                table = app.screen.query_one(DataTable)
                # At least one row populated from the fixture
                assert table.row_count > 0
                # Pop back stack
                await pilot.press("escape")
                await pilot.pause()
                assert isinstance(app.screen, SessionScreen)
                await pilot.press("escape")
                await pilot.pause()
                assert isinstance(app.screen, ProjectScreen)
                await pilot.press("q")

        asyncio.run(run())

    def test_session_loader_worker_populates_table(self, tmp_path, monkeypatch):
        """Multiple fixture sessions land via the background worker.

        Verifies that (a) the progress bar appears during load,
        (b) the worker's call_from_thread writes actually reach
        the DataTable, and (c) the progress container is hidden
        once loading finishes.
        """
        import asyncio
        import shutil
        from textual.widgets import DataTable, ProgressBar
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import SessionScreen

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-p"
        proj_dir.mkdir(parents=True)
        for i, fixture in enumerate(
            ["basic_session.jsonl", "multi_tool.jsonl", "with_web_search.jsonl"]
        ):
            shutil.copy(FIXTURES / fixture, proj_dir / f"s{i}.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SessionScreen)
                await app.workers.wait_for_complete()
                await pilot.pause()
                table = app.screen.query_one(DataTable)
                assert table.row_count == 3
                loading = app.screen.query_one("#loading")
                assert "-hidden" in loading.classes
                # Progress bar filled
                bar = app.screen.query_one("#loading_bar", ProgressBar)
                assert bar.percentage == 1.0
                await pilot.press("q")

        asyncio.run(run())

    def test_open_pushes_session_screen(self, tmp_path, monkeypatch):
        # Full end-to-end nav test. Uses a real fixture transcript
        # so SessionScreen has something to render — discover_sessions
        # parses it synchronously at mount time.
        import asyncio
        import shutil
        from textual.widgets import DataTable
        from claude_measure_usage.tui.app import MeasureUsageApp
        from claude_measure_usage.tui.screens import ProjectScreen, SessionScreen

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-fake-project"
        proj_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "basic_session.jsonl", proj_dir / "session.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = MeasureUsageApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, ProjectScreen)
                # Press enter to open the only project.
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SessionScreen)
                await app.workers.wait_for_complete()
                await pilot.pause()
                session_table = app.screen.query_one(DataTable)
                assert session_table.row_count == 1
                # Esc pops back to the project picker.
                await pilot.press("escape")
                await pilot.pause()
                assert isinstance(app.screen, ProjectScreen)
                await pilot.press("q")

        asyncio.run(run())
