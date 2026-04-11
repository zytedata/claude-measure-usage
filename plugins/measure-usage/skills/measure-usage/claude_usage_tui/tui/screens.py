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

from ..metrics import model_aware_cost_breakdown
from .detail_rows import DetailRow, build_detail_rows
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
    """

    # Tacked onto the sub_title so the normalization behind the
    # "Token usage" column is documented inline without costing a
    # dedicated legend row. Peak ctx stays raw; the column name
    # speaks for itself.
    USAGE_UNIT_NOTE = "usage is Sonnet input-equivalent"

    def __init__(self, project: ProjectEntry) -> None:
        super().__init__()
        self._project = project
        self._entries: list[SessionEntry] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="loading"):
            yield Label("Loading sessions…", id="loading_label")
            yield ProgressBar(id="loading_bar", show_eta=False)
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
        table.add_column("Token usage", width=12)
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

        Defensive structure:

        - Individual ``load_session`` failures are caught per-file
          so one malformed transcript can't abort the batch. The
          skipped path is recorded and surfaced in the loading
          label once everything else has loaded.
        - ``_finish_loading`` is dispatched in a ``finally`` so
          even an unexpected exception in the batch loop still
          leaves the UI in a consistent state — the table
          populates with whatever was parsed successfully, the
          loading indicator hides, and an error banner appears
          if anything went wrong.
        - ``call_from_thread`` itself is wrapped because the
          worker can race with screen teardown; dispatching into
          an unmounted screen raises, which would otherwise kill
          the worker silently.
        """
        skipped: list[tuple[Path, Exception]] = []
        fatal: Exception | None = None
        try:
            for path in paths:
                try:
                    entry = load_session(path)
                except Exception as exc:
                    skipped.append((path, exc))
                    self._safe_call(self._on_session_skipped)
                    continue
                self._safe_call(self._on_session_parsed, entry)
        except Exception as exc:
            fatal = exc
        finally:
            self._safe_call(self._finish_loading, skipped, fatal)

    def _safe_call(self, fn, *args) -> None:
        """Dispatch ``fn`` to the app thread, swallowing teardown races."""
        try:
            self.app.call_from_thread(fn, *args)
        except Exception:
            pass

    def _on_session_parsed(self, entry: SessionEntry) -> None:
        self._entries.append(entry)
        self._advance_progress()

    def _on_session_skipped(self) -> None:
        self._advance_progress()

    def _advance_progress(self) -> None:
        try:
            progress = self.query_one("#loading_bar", ProgressBar)
        except Exception:
            return
        try:
            progress.advance(1)
        except Exception:
            pass

    def _finish_loading(
        self,
        skipped: list[tuple[Path, Exception]] | None = None,
        fatal: Exception | None = None,
    ) -> None:
        sort_sessions(self._entries)
        try:
            self.sub_title = self._build_sub_title(skipped or [], fatal)
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
        except Exception:
            # Screen was popped mid-finish, or something else went
            # sideways. Nothing more to do — the user has already
            # navigated away.
            return
        self._update_loading_indicator(skipped or [], fatal)

    def _build_sub_title(
        self,
        skipped: list[tuple[Path, Exception]],
        fatal: Exception | None,
    ) -> str:
        parts = [
            self._project.cwd_display,
            f"{len(self._entries)} sessions",
            self.USAGE_UNIT_NOTE,
        ]
        if skipped:
            parts.append(f"{len(skipped)} skipped")
        if fatal is not None:
            parts.append(f"load error: {type(fatal).__name__}")
        return "  —  ".join(parts)

    def _update_loading_indicator(
        self,
        skipped: list[tuple[Path, Exception]],
        fatal: Exception | None,
    ) -> None:
        try:
            loading = self.query_one("#loading")
        except Exception:
            return
        if fatal is None and not skipped:
            loading.add_class("-hidden")
            return
        try:
            label = self.query_one("#loading_label", Label)
        except Exception:
            return
        msg_parts: list[str] = []
        if fatal is not None:
            msg_parts.append(f"error: {type(fatal).__name__}: {fatal}")
        if skipped:
            msg_parts.append(f"skipped {len(skipped)} unreadable")
        label.update(" · ".join(msg_parts))

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        row_idx = event.cursor_row
        if row_idx is None or row_idx >= len(self._entries):
            return
        entry = self._entries[row_idx]
        self.app.push_screen(SessionDetailScreen(self._project, entry))

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()

    def action_help(self) -> None:
        pass


class SessionDetailScreen(Screen):
    """Per-session drill-in: turn table with subagent footnotes.

    Shows the full timeline of a single transcript — model turns,
    non-turn timeline entries (user messages, slash commands,
    attachments), and subagent footnote rows dimmed beneath the
    turns that spawned them. Parse happens synchronously in
    ``on_mount`` because we've already paid once at the session
    picker (loads are cached in the session loader in a future
    commit); a second parse of a single file is cheap.

    Sort, filter, and the turn detail modal are deferred to later
    commits per docs/tui-ux.md.
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
    ]

    DEFAULT_CSS = """
    SessionDetailScreen #detail_header {
        height: auto;
        padding: 0 2 1 2;
        color: $text-muted;
    }
    SessionDetailScreen DataTable {
        height: 1fr;
    }
    """

    # Appended to sub_title so readers remember the cost columns
    # are derived, not raw. Same convention as SessionScreen.
    USAGE_UNIT_NOTE = "cost columns are Sonnet input-equivalent"

    def __init__(
        self,
        project: ProjectEntry,
        session: SessionEntry,
    ) -> None:
        super().__init__()
        self._project = project
        self._session = session
        self._rows: list[DetailRow] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("", id="detail_header")
        table: DataTable[str] = DataTable(id="detail_table", zebra_stripes=True)
        table.cursor_type = "row"
        yield table
        yield Footer()

    def on_mount(self) -> None:
        from ..parse import (
            build_agent_tree,
            find_subagent_transcripts,
            parse_transcript,
        )

        path = str(self._session.transcript_path)
        parsed = parse_transcript(path)
        sub_infos = find_subagent_transcripts(path, 0)
        tree = build_agent_tree(path, parsed, sub_infos)
        self._rows = build_detail_rows(parsed, tree)

        self.sub_title = self._build_sub_title()
        self.query_one("#detail_header", Label).update(
            self._build_header_text(parsed)
        )

        table = self.query_one(DataTable)
        table.add_column("#", width=6)
        table.add_column("when", width=8)
        table.add_column("took", width=7)
        table.add_column("cost", width=9)
        table.add_column("own", width=9)
        table.add_column("carry", width=9)
        table.add_column("caused", width=7)
        table.add_column("what", width=50)
        table.add_column("ctx", width=8)
        table.add_column("model", width=7)
        table.add_column("in", width=7)
        table.add_column("out", width=7)
        table.add_column("cache_r", width=9)
        table.add_column("cache_w", width=9)

        for row in self._rows:
            table.add_row(*_cells_for(row))

        if table.row_count:
            table.cursor_coordinate = Coordinate(0, 0)
        table.focus()

    def _build_sub_title(self) -> str:
        parts = [
            self._project.cwd_display,
            short_datetime(self._session.started_ts) or self._session.session_id[:8],
            self.USAGE_UNIT_NOTE,
        ]
        return "  —  ".join(parts)

    def _build_header_text(self, parsed: dict) -> str:
        """One-line header above the table with session-level totals."""
        tbm = parsed.get("tokens_by_model") or {}
        total_seq = 0.0
        if tbm:
            total_seq = model_aware_cost_breakdown(tbm)["total"]
        parts = [
            f"{parsed.get('turn_count', 0)} turns",
            f"{short_tokens(round(total_seq))} cost",
            f"peak ctx {short_tokens(parsed.get('peak_context_tokens', 0))}",
            f"model {tiny_model(self._session.dominant_model) or '—'}",
        ]
        return "  ·  ".join(parts)

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        # Turn detail modal and subagent drill-in land in future
        # commits. No-op for now.
        pass

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()

    def action_help(self) -> None:
        pass


def _cells_for(row: DetailRow):
    """Turn a :class:`DetailRow` into styled cells for DataTable.

    Subagent footnote rows are rendered dimmed so they read as
    sub-items of the turn they attach to, not as independent
    entries. Non-turn rows also get the dim treatment — they're
    timeline annotations, not primary content. Turn rows stay at
    default style.
    """
    from rich.text import Text

    cells_raw = [
        row.num,
        row.when,
        row.took,
        row.cost,
        row.own,
        row.carry,
        row.caused,
        row.what,
        row.ctx,
        row.model,
        row.in_tokens,
        row.out,
        row.cache_r,
        row.cache_w,
    ]
    if row.kind == "turn":
        return cells_raw
    style = "dim"
    return [Text(c or "", style=style) for c in cells_raw]
