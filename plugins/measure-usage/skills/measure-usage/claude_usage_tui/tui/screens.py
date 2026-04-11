"""Textual screens for claude-usage-tui.

Each screen is a full-screen :class:`textual.screen.Screen` pushed
and popped on a stack — see ``docs/tui-ux.md`` for the interaction
contract. Currently only the project picker is implemented; session
picker and session detail follow in subsequent commits.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from .discovery import (
    ProjectEntry,
    discover_projects,
    project_for_cwd,
)
from .format import rel_time


class ProjectScreen(Screen):
    """Top-level picker listing every project in ``~/.claude/projects``.

    Shows each project as a row with session count and last-activity
    relative time. The ``Tokens`` column shown in the design doc is
    deferred — computing it requires parsing every transcript and
    would block the initial render on cold caches.
    """

    BINDINGS = [
        Binding("enter", "open", "Open", priority=False),
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

    def action_open(self) -> None:
        # Will push SessionScreen in the next commit; for now just a
        # no-op so the binding stays visible in the footer.
        pass

    def action_quit(self) -> None:
        self.app.exit()

    def action_help(self) -> None:
        # Placeholder until the help overlay lands.
        pass
