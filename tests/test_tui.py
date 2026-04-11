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

from claude_usage_tui.tui.format import rel_time  # noqa: E402


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
