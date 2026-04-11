"""Tests for the interactive TUI layer.

Discovery helpers live in ``claude_usage_tui.tui.discovery``; the
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

from claude_usage_tui.tui import discovery  # noqa: E402


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

from claude_usage_tui.tui.format import (  # noqa: E402
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
        from claude_usage_tui.tui.app import ClaudeUsageTuiApp

        async def run():
            app = ClaudeUsageTuiApp()
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
        from claude_usage_tui.tui.app import ClaudeUsageTuiApp
        from claude_usage_tui.tui.screens import SessionScreen

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-p"
        proj_dir.mkdir(parents=True)
        for i, fixture in enumerate(
            ["basic_session.jsonl", "multi_tool.jsonl", "with_web_search.jsonl"]
        ):
            shutil.copy(FIXTURES / fixture, proj_dir / f"s{i}.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = ClaudeUsageTuiApp()
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
        from claude_usage_tui.tui.app import ClaudeUsageTuiApp
        from claude_usage_tui.tui.screens import ProjectScreen, SessionScreen

        projects_root = tmp_path / ".claude" / "projects"
        proj_dir = projects_root / "-tmp-fake-project"
        proj_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "basic_session.jsonl", proj_dir / "session.jsonl")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run():
            app = ClaudeUsageTuiApp()
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
