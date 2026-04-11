"""Textual screens for claude-usage-tui.

Each screen is a full-screen :class:`textual.screen.Screen` pushed
and popped on a stack — see ``docs/tui-ux.md`` for the interaction
contract.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from .discovery import (
    ProjectEntry,
    SessionEntry,
    discover_projects,
    discover_sessions,
    project_for_cwd,
)
from .format import rel_time, short_datetime, short_tokens, tiny_model


class ProjectScreen(Screen):
    """Top-level picker listing every project in ``~/.claude/projects``.

    Shows each project as a row with session count and last-activity
    relative time. The ``Tokens`` column shown in the design doc is
    deferred — computing it requires parsing every transcript and
    would block the initial render on cold caches.

    "Open" is handled via :class:`DataTable.RowSelected` rather than
    a screen-level ``enter`` binding: DataTable installs its own
    ``priority=True`` enter binding to fire that message, which
    would shadow anything this screen defines.
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[ProjectEntry] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        table: DataTable[str] = DataTable(id="projects", zebra_stripes=True)
        table.cursor_type = "row"
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self._entries = discover_projects()
        self.sub_title = f"{len(self._entries)} projects"
        table = self.query_one(DataTable)
        table.add_column("Project", width=60)
        table.add_column("Sessions", width=10)
        table.add_column("Last", width=12)
        if not self._entries:
            return
        cwd_dir = project_for_cwd()
        cwd_row = 0
        for i, entry in enumerate(self._entries):
            table.add_row(
                entry.cwd_display,
                str(entry.session_count),
                rel_time(entry.last_activity),
                key=str(entry.project_dir),
            )
            if cwd_dir is not None and entry.project_dir == cwd_dir:
                cwd_row = i
        # Land the cursor on the current cwd's project so the common
        # case — "I'm in a project, open my recent session here" —
        # takes zero keystrokes of navigation.
        table.cursor_coordinate = Coordinate(cwd_row, 0)
        table.focus()

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        row_idx = event.cursor_row
        if row_idx is None or row_idx >= len(self._entries):
            return
        entry = self._entries[row_idx]
        self.app.push_screen(SessionScreen(entry))

    def action_quit(self) -> None:
        self.app.exit()

    def action_help(self) -> None:
        # Placeholder until the help overlay lands.
        pass


class SessionScreen(Screen):
    """Session picker: lists every transcript in a project.

    Parses each ``.jsonl`` on mount via
    :func:`claude_usage_tui.tui.discovery.discover_sessions`. Real
    measurements show ~4ms per session, so a 200-session project
    still mounts in well under a second — no background worker
    needed for v1.
    """

    # Both Esc and ← bind to back-one-level per docs/tui-ux.md. The
    # DataTable row cursor mode ignores ← (no horizontal cursor),
    # so the browser-style "back" shortcut has no conflict. Only
    # one of the two is shown in the footer to avoid noise.
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("left", "back", "Back", show=False),
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
    ]

    def __init__(self, project: ProjectEntry) -> None:
        super().__init__()
        self._project = project
        self._entries: list[SessionEntry] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        table: DataTable[str] = DataTable(id="sessions", zebra_stripes=True)
        table.cursor_type = "row"
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self._entries = discover_sessions(self._project.project_dir)
        self.sub_title = (
            f"{self._project.cwd_display}  —  {len(self._entries)} sessions"
        )
        table = self.query_one(DataTable)
        table.add_column("Started", width=18)
        table.add_column("Turns", width=6)
        table.add_column("Tokens", width=10)
        table.add_column("Peak ctx", width=10)
        table.add_column("Model", width=8)
        table.add_column("Summary")
        for entry in self._entries:
            table.add_row(
                short_datetime(entry.started_ts) or "—",
                str(entry.turn_count),
                short_tokens(entry.total_seq_tokens),
                short_tokens(entry.peak_context_tokens),
                tiny_model(entry.dominant_model),
                (entry.first_user_message or "").replace("\n", " ")[:120],
                key=str(entry.transcript_path),
            )
        table.cursor_coordinate = Coordinate(0, 0)
        table.focus()

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        # Session detail screen lands in the next commit; for now
        # the row-select is a no-op so the other bindings still work.
        pass

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()

    def action_help(self) -> None:
        pass
