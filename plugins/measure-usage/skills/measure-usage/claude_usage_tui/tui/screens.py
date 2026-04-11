"""Textual screens for claude-usage-tui.

Each screen is a full-screen :class:`textual.screen.Screen` pushed
and popped on a stack — see ``docs/tui-ux.md`` for the interaction
contract.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label, ProgressBar

from .discovery import (
    ProjectEntry,
    SessionEntry,
    discover_projects,
    list_session_paths,
    load_session,
    project_for_cwd,
    sort_sessions,
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

    Parses each ``.jsonl`` in a background thread worker and
    advances a :class:`ProgressBar` per file. On projects with many
    or large sessions the parse can run into seconds, and mounting
    the screen with a blocking parse would look like the app had
    frozen. The worker approach lets the screen paint immediately
    with its progress indicator and keeps keyboard input responsive
    (``Esc`` to go back cancels the worker and pops the screen).
    """

    # ``←`` / ``→`` are intentionally NOT bound for navigation: on
    # narrow terminals the table overflows horizontally and the
    # DataTable uses those keys to scroll its content into view.
    # Binding them at the screen level would give the same key two
    # different meanings depending on terminal width — worse than
    # just requiring ``Esc`` for back.
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
    ]

    DEFAULT_CSS = """
    SessionScreen #loading {
        height: 1;
        padding: 0 2;
        background: $surface;
    }
    SessionScreen #loading Label {
        width: auto;
        margin-right: 1;
        color: $text-muted;
    }
    SessionScreen #loading ProgressBar {
        width: 1fr;
    }
    SessionScreen #loading.-hidden {
        display: none;
    }
    SessionScreen #legend {
        height: 1;
        padding: 0 2;
        color: $text-muted;
    }
    """

    # Spelled out so readers don't confuse the Tokens column
    # (Sonnet input-equivalent, normalized across token type and
    # model) with the raw peak-context count shown alongside.
    # Matches the legend line the plain text CLI prints.
    LEGEND = (
        "Tokens: Sonnet input-equivalent, normalized across models "
        "and token types. Peak ctx: raw."
    )

    def __init__(self, project: ProjectEntry) -> None:
        super().__init__()
        self._project = project
        self._entries: list[SessionEntry] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="loading"):
            yield Label("Loading sessions…", id="loading_label")
            yield ProgressBar(id="loading_bar", show_eta=False)
        yield Label(self.LEGEND, id="legend")
        table: DataTable[str] = DataTable(id="sessions", zebra_stripes=True)
        table.cursor_type = "row"
        yield table
        yield Footer()

    def on_mount(self) -> None:
        paths = list_session_paths(self._project.project_dir)
        self.sub_title = (
            f"{self._project.cwd_display}  —  loading {len(paths)} sessions…"
        )
        table = self.query_one(DataTable)
        table.add_column("Started", width=18)
        table.add_column("Turns", width=6)
        table.add_column("Tokens", width=10)
        table.add_column("Peak ctx", width=10)
        table.add_column("Model", width=8)
        table.add_column("Summary")

        loading = self.query_one("#loading")
        if not paths:
            loading.add_class("-hidden")
            self._finish_loading()
            return
        progress = self.query_one("#loading_bar", ProgressBar)
        progress.update(total=len(paths), progress=0)
        # exclusive=True cancels any prior loader on this screen so
        # pressing r to reload doesn't leave two parsers racing.
        self.run_worker(
            lambda: self._parse_all(paths),
            thread=True,
            exclusive=True,
            name="session-loader",
        )

    def on_unmount(self) -> None:
        # Kill any in-flight parser when the screen is popped so it
        # can't touch unmounted widgets via call_from_thread.
        self.workers.cancel_all()

    def _parse_all(self, paths: list[Path]) -> None:
        """Worker body: parses each session and streams progress.

        Runs on a background thread. All UI touches go through
        ``call_from_thread`` so the main event loop stays
        single-threaded.
        """
        for path in paths:
            if not self.is_mounted:
                return
            entry = load_session(path)
            self.app.call_from_thread(self._on_session_parsed, entry)
        self.app.call_from_thread(self._finish_loading)

    def _on_session_parsed(self, entry: SessionEntry) -> None:
        self._entries.append(entry)
        try:
            progress = self.query_one("#loading_bar", ProgressBar)
        except Exception:
            return
        progress.advance(1)

    def _finish_loading(self) -> None:
        sort_sessions(self._entries)
        self.sub_title = (
            f"{self._project.cwd_display}  —  {len(self._entries)} sessions"
        )
        table = self.query_one(DataTable)
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
        if table.row_count:
            table.cursor_coordinate = Coordinate(0, 0)
        table.focus()
        try:
            self.query_one("#loading").add_class("-hidden")
        except Exception:
            pass

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
