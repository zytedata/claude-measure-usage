"""Textual screens for claude-usage-tui.

Each screen is a full-screen :class:`textual.screen.Screen` pushed
and popped on a stack — see ``docs/tui-ux.md`` for the interaction
contract.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    ProgressBar,
    Static,
)
from textual.widgets.option_list import Option

from ..metrics import compute_metrics_from_parsed, model_aware_cost_breakdown
# The summary modal deliberately reuses the plain text renderer's
# format_metrics helper so the TUI and the /measure-usage skill
# show byte-identical session summaries. This is a controlled
# cross-layer import: tui → plain is fine because plain itself
# touches no TUI code, so the subprocess isolation test
# ("plain mustn't load textual") still holds.
from ..plain.display import format_metrics
from ..turns_label import short_agent_id
from .detail_rows import (
    SORT_MODES,
    DetailRow,
    SortMode,
    TurnCostBreakdown,
    build_detail_rows,
    filter_rows,
    get_sort_mode,
    sort_mode_for_column,
    sort_rows,
    turn_cost_breakdown,
)
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
        Binding("r", "reload", "Reload"),
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help"),
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
        table = self.query_one(DataTable)
        table.add_column("Project", width=60)
        table.add_column("Sessions", width=10)
        table.add_column("Last", width=12)
        self._entries = discover_projects()
        self._populate_table(use_cwd_cursor=True)

    def action_reload(self) -> None:
        """Rescan ``~/.claude/projects`` and rebuild the table.

        Filesystem-only — no transcripts are parsed — so this is
        fast even for big trees. Preserves the cursor on the
        previously highlighted project if its directory still
        exists after the rescan; otherwise falls back to row 0.
        """
        table = self.query_one(DataTable)
        idx = table.cursor_row
        preserve: Path | None = None
        if idx is not None and 0 <= idx < len(self._entries):
            preserve = self._entries[idx].project_dir
        self._entries = discover_projects()
        self._populate_table(preserve_key=preserve)

    def _populate_table(
        self,
        use_cwd_cursor: bool = False,
        preserve_key: Path | None = None,
    ) -> None:
        """Rebuild DataTable rows from ``self._entries``.

        Cursor lands on ``preserve_key`` if given, else on the
        current cwd's project when ``use_cwd_cursor`` is set
        (initial mount) — so the common case of "I'm in a
        project, open its recent session" takes zero keystrokes
        of navigation. Falls back to row 0 if neither matches.
        """
        self.sub_title = f"{len(self._entries)} projects"
        table = self.query_one(DataTable)
        table.clear()
        if not self._entries:
            return
        target = preserve_key
        if target is None and use_cwd_cursor:
            target = project_for_cwd()
        target_idx = 0
        for i, entry in enumerate(self._entries):
            table.add_row(
                entry.cwd_display,
                str(entry.session_count),
                rel_time(entry.last_activity),
                key=str(entry.project_dir),
            )
            if target is not None and entry.project_dir == target:
                target_idx = i
        table.cursor_coordinate = Coordinate(target_idx, 0)
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
        self.app.push_screen(
            HelpModal(
                title="claude-usage-tui  —  Project picker",
                bindings=list(self.BINDINGS),
                intro=(
                    "Browse Claude Code projects under "
                    "~/.claude/projects. Enter drills into a "
                    "project's session list; r rescans the "
                    "projects directory.\n"
                ),
            )
        )


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
        Binding("i", "open_summary", "Summary"),
        Binding("r", "reload", "Reload"),
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help"),
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
        # Bumped on every load start so in-flight callbacks from
        # a cancelled worker can tell they're stale and drop. See
        # ``_start_loader`` for the full race description.
        self._load_epoch = 0
        self._pending_cursor_key: Path | None = None

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
        table = self.query_one(DataTable)
        table.add_column("Started", width=18)
        table.add_column("Turns", width=6)
        table.add_column("Token usage", width=12)
        table.add_column("Peak ctx", width=10)
        table.add_column("Model", width=8)
        table.add_column("Summary")
        self._start_loader()

    def action_reload(self) -> None:
        """Rescan the project directory and re-parse every transcript.

        Preserves the highlighted session's cursor position if it
        still exists after the reparse; new sessions appear at
        their natural sort position. Uses the same background
        worker as the initial mount so the UI stays responsive
        even when the project has many large transcripts.
        """
        table = self.query_one(DataTable)
        idx = table.cursor_row
        preserve: Path | None = None
        if idx is not None and 0 <= idx < len(self._entries):
            preserve = self._entries[idx].transcript_path
        self._start_loader(preserve_cursor=preserve)

    def _start_loader(self, preserve_cursor: Path | None = None) -> None:
        """Kick off (or restart) the background transcript parser.

        Shared by :meth:`on_mount` and :meth:`action_reload`.

        Bumps :attr:`_load_epoch` before starting the new worker
        and stamps the epoch into every ``call_from_thread``
        dispatch. ``run_worker(exclusive=True)`` cancels the
        previous worker but can't interrupt a mid-iteration
        Python thread — any already-dispatched callbacks can
        still fire on the main thread after the new worker has
        started and would otherwise append stale entries into
        the freshly-cleared list. The epoch check in each
        handler makes those late callbacks a no-op.
        """
        self._load_epoch += 1
        epoch = self._load_epoch
        self._entries = []
        self._pending_cursor_key = preserve_cursor

        paths = list_session_paths(self._project.project_dir)
        self.sub_title = (
            f"{self._project.cwd_display}  —  loading {len(paths)} sessions…"
        )
        table = self.query_one(DataTable)
        table.clear()
        loading = self.query_one("#loading")
        loading.remove_class("-hidden")
        try:
            self.query_one("#loading_label", Label).update(
                "Loading sessions…"
            )
        except Exception:
            pass

        if not paths:
            loading.add_class("-hidden")
            self._finish_loading(epoch)
            return
        progress = self.query_one("#loading_bar", ProgressBar)
        progress.update(total=len(paths), progress=0)
        self.run_worker(
            lambda: self._parse_all(paths, epoch),
            thread=True,
            exclusive=True,
            name="session-loader",
        )

    def on_unmount(self) -> None:
        # Kill any in-flight parser when the screen is popped so it
        # can't touch unmounted widgets via call_from_thread.
        self.workers.cancel_all()

    def _parse_all(self, paths: list[Path], epoch: int) -> None:
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
        - ``epoch`` is threaded through every dispatch so each
          handler can drop stale callbacks after a reload.
        """
        skipped: list[tuple[Path, Exception]] = []
        fatal: Exception | None = None
        try:
            for path in paths:
                try:
                    entry = load_session(path)
                except Exception as exc:
                    skipped.append((path, exc))
                    self._safe_call(self._on_session_skipped, epoch)
                    continue
                self._safe_call(self._on_session_parsed, epoch, entry)
        except Exception as exc:
            fatal = exc
        finally:
            self._safe_call(self._finish_loading, epoch, skipped, fatal)

    def _safe_call(self, fn, *args) -> None:
        """Dispatch ``fn`` to the app thread, swallowing teardown races."""
        try:
            self.app.call_from_thread(fn, *args)
        except Exception:
            pass

    def _on_session_parsed(self, epoch: int, entry: SessionEntry) -> None:
        if epoch != self._load_epoch:
            return
        self._entries.append(entry)
        self._advance_progress()

    def _on_session_skipped(self, epoch: int) -> None:
        if epoch != self._load_epoch:
            return
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
        epoch: int,
        skipped: list[tuple[Path, Exception]] | None = None,
        fatal: Exception | None = None,
    ) -> None:
        if epoch != self._load_epoch:
            return
        sort_sessions(self._entries)
        preserve = self._pending_cursor_key
        self._pending_cursor_key = None
        try:
            self.sub_title = self._build_sub_title(skipped or [], fatal)
            table = self.query_one(DataTable)
            target_idx = 0
            for i, entry in enumerate(self._entries):
                table.add_row(
                    short_datetime(entry.started_ts) or "—",
                    str(entry.turn_count),
                    short_tokens(entry.total_seq_tokens),
                    short_tokens(entry.peak_context_tokens),
                    tiny_model(entry.dominant_model),
                    (entry.first_user_message or "").replace("\n", " ")[:120],
                    key=str(entry.transcript_path),
                )
                if preserve is not None and entry.transcript_path == preserve:
                    target_idx = i
            if table.row_count:
                table.cursor_coordinate = Coordinate(target_idx, 0)
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
        self._open_session(entry)

    def _open_session(self, entry: SessionEntry) -> None:
        """Parse + build tree on the main thread, then push detail.

        A single-session parse tops out around ~50ms even on big
        sessions; doing it synchronously keeps the open path
        simple and avoids a second progress bar on a screen that
        will be visible for seconds anyway.
        """
        parsed, tree = self._parse_for(entry)
        self.app.push_screen(
            SessionDetailScreen(
                self._title_for(entry),
                parsed,
                tree,
                transcript_path=entry.transcript_path,
            )
        )

    def _parse_for(self, entry: SessionEntry) -> tuple[dict, list[dict]]:
        """Parse the transcript and build the subagent tree.

        Shared by ``_open_session`` (drill into detail) and
        ``action_open_summary`` (overlay summary modal) so both
        paths get identical data and nobody parses twice.
        """
        from ..parse import (
            build_agent_tree,
            find_subagent_transcripts,
            parse_transcript,
        )

        path = str(entry.transcript_path)
        parsed = parse_transcript(path)
        sub_infos = find_subagent_transcripts(path, 0)
        tree = build_agent_tree(path, parsed, sub_infos)
        return parsed, tree

    def _title_for(self, entry: SessionEntry) -> str:
        return "  —  ".join([
            self._project.cwd_display,
            short_datetime(entry.started_ts) or entry.session_id[:8],
        ])

    def action_open_summary(self) -> None:
        """Show the summary modal for the currently highlighted session.

        Same rendering as the SessionDetailScreen's ``i`` binding
        (compute_metrics_from_parsed + format_metrics) but opens
        straight from the picker — useful for triaging a long
        session list without having to drill into each candidate.
        """
        table = self.query_one(DataTable)
        idx = table.cursor_row
        if idx is None or idx >= len(self._entries):
            return
        entry = self._entries[idx]
        parsed, tree = self._parse_for(entry)
        self.app.push_screen(
            SummaryModal(
                title=self._title_for(entry),
                parsed=parsed,
                tree=tree,
                transcript_path=entry.transcript_path,
            )
        )

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()

    def action_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                title="claude-usage-tui  —  Session picker",
                bindings=list(self.BINDINGS),
                intro=(
                    "Sessions in the selected project, most "
                    "recent first. Enter drills into the full "
                    "turn table; i previews the summary "
                    "without drilling; r re-parses every "
                    "transcript from disk.\n"
                ),
            )
        )


class SessionDetailScreen(Screen):
    """Per-session drill-in: turn table with subagent footnotes.

    Reusable for nested subagent drill-ins — the same class
    renders a main session's timeline and a subagent's internal
    timeline, because a subagent tree node has essentially the
    same shape as a :func:`parse_transcript` result. The caller
    passes in the already-parsed data and the direct children so
    no work is duplicated.

    ``Enter`` on a turn row opens :class:`TurnDetailModal`;
    ``Enter`` on a subagent row drills directly into a new
    :class:`SessionDetailScreen` for that subagent (stack grows);
    ``Enter`` on a non-turn row opens :class:`NonturnDetailModal`.
    """

    BINDINGS = [
        Binding("s", "cycle_sort", "Sort"),
        Binding("slash", "open_filter", "Filter"),
        Binding("i", "open_summary", "Summary"),
        Binding("r", "reload", "Reload"),
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help"),
    ]

    DEFAULT_CSS = """
    SessionDetailScreen #detail_header {
        height: auto;
        padding: 0 2 1 2;
        color: $text-muted;
    }
    SessionDetailScreen #filter_input {
        display: none;
        height: 3;
        margin: 0 2;
    }
    SessionDetailScreen #filter_input.-active {
        display: block;
    }
    SessionDetailScreen DataTable {
        height: 1fr;
    }
    """

    USAGE_UNIT_NOTE = "cost columns are Sonnet input-equivalent"

    def __init__(
        self,
        title: str,
        parsed: dict,
        tree: list[dict],
        transcript_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._parsed = parsed
        self._tree = tree
        # Only the top-level session detail screen carries a
        # transcript path; subagent drill-ins pass None because
        # their data comes from the parent session's agent tree,
        # not from a file path we can re-read.
        self._transcript_path = transcript_path
        self._rows: list[DetailRow] = []
        self._sort_mode = "natural"
        self._filter_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("", id="detail_header")
        yield _FilterInput(
            placeholder="filter — substring on what",
            id="filter_input",
        )
        table: DataTable[str] = DataTable(id="detail_table", zebra_stripes=True)
        table.cursor_type = "row"
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self._rows = build_detail_rows(self._parsed, self._tree)
        self.query_one("#detail_header", Label).update(self._build_header_text())

        table = self.query_one(DataTable)
        # Each column gets an explicit key so the sort-highlight
        # helper can look up its Column object to rewrite the
        # label (e.g. ``cost ▼``) when sort state changes, and
        # so HeaderSelected handling can map clicks back to sort
        # modes via ``sort_mode_for_column``.
        for label, key, width in self._column_spec():
            table.add_column(label, key=key, width=width)

        self._repopulate_table()
        table.focus()

    @staticmethod
    def _column_spec() -> list[tuple[str, str, int]]:
        """Ordered (label, key, width) for every DataTable column.

        Column keys double as ids for the sort-highlight helper
        and the header-click handler. Keep these stable; sort
        mode entries in :data:`SORT_MODES` reference them by id.

        Each sortable column's width has to accommodate the
        header label plus a trailing ``" ▼"`` (2 chars) so the
        active-sort indicator doesn't get truncated when the
        column becomes the active sort — ``caused`` at width 7
        used to clip the arrow to ``caused ▼`` → ``caused ▼``.
        """
        return [
            ("#", "num", 6),
            ("when", "when", 8),
            ("took", "took", 7),
            ("cost", "cost", 9),
            ("own", "own", 9),
            ("carry", "carry", 9),
            ("caused", "caused", 9),
            ("what", "what", 50),
            ("ctx", "ctx", 8),
            ("model", "model", 7),
            ("in", "in_tokens", 7),
            ("out", "out", 7),
            ("cache_r", "cache_r", 9),
            ("cache_w", "cache_w", 9),
        ]

    def _refresh_column_labels(self) -> None:
        """Rewrite column header labels to mark the active sort.

        The column whose ``column_id`` matches the current sort
        mode gets its label rendered bold with a ``▼`` suffix;
        every other column resets to a plain label. Works by
        mutating ``DataTable.columns[key].label`` directly —
        Textual refreshes the header on the next render pass.
        """
        from rich.text import Text

        active = get_sort_mode(self._sort_mode).column_id
        table = self.query_one(DataTable)
        for label, key, _ in self._column_spec():
            column = table.columns.get(key)
            if column is None:
                continue
            if key == active:
                column.label = Text(f"{label} ▼", style="bold")
            else:
                column.label = Text(label)
        table.refresh()

    def _repopulate_table(self) -> None:
        """Rebuild the DataTable contents for the current sort/filter.

        Clears the table and re-adds rows from ``sort_rows`` →
        ``filter_rows``. Both operations work at block level, so
        they compose cleanly: sort reorders blocks, filter keeps
        only matching blocks, the glue relationships are
        preserved either way.

        DataTable.clear() is O(rows) but row counts stay in the
        hundreds even for big sessions — still cheap relative to
        parsing the transcript, so we don't bother diffing.
        """
        table = self.query_one(DataTable)
        table.clear()
        visible = sort_rows(self._rows, self._sort_mode)
        if self._filter_text:
            visible = filter_rows(visible, self._filter_text)
        self._visible_rows = visible
        for row in visible:
            table.add_row(*_cells_for(row))
        if table.row_count:
            table.cursor_coordinate = Coordinate(0, 0)
        self.sub_title = self._build_sub_title()
        self._refresh_column_labels()

    def _build_sub_title(self) -> str:
        """Sub_title shows breadcrumb + unit note + sort/filter state."""
        parts = [self._title, self.USAGE_UNIT_NOTE]
        if self._sort_mode != "natural":
            parts.append(f"sort: {self._sort_mode} ▼")
        if self._filter_text:
            parts.append(f'filter: "{self._filter_text}"')
        return "  —  ".join(parts)

    def _build_header_text(self) -> str:
        """One-line header above the table with session-level totals."""
        parsed = self._parsed
        tbm = parsed.get("tokens_by_model") or {}
        total_seq = 0.0
        if tbm:
            total_seq = model_aware_cost_breakdown(tbm)["total"]
        # Dominant model — picked by raw token volume, not by Seq,
        # so a Haiku-heavy session isn't labelled "opus" just
        # because Opus is more expensive per-token.
        dominant = _dominant_model(tbm)
        parts = [
            f"{parsed.get('turn_count', 0)} turns",
            f"{short_tokens(round(total_seq))} cost",
            f"peak ctx {short_tokens(parsed.get('peak_context_tokens', 0))}",
            f"model {tiny_model(dominant) or '—'}",
        ]
        return "  ·  ".join(parts)

    def action_cycle_sort(self) -> None:
        """Advance to the next sort mode in :data:`SORT_MODES`.

        Cycle order follows the visual column order in the detail
        table: natural (``#``) → took → cost → own → carry →
        caused → back to natural. Columns that don't carry a
        meaningful sort (``when``, ``what``, ``ctx``, model, raw
        token counts) are skipped — the cycle has gaps.
        """
        mode_ids = [m.id for m in SORT_MODES]
        idx = mode_ids.index(self._sort_mode) if self._sort_mode in mode_ids else 0
        self._sort_mode = mode_ids[(idx + 1) % len(mode_ids)]
        self._repopulate_table()

    def on_data_table_header_selected(
        self, event: DataTable.HeaderSelected
    ) -> None:
        """Click-to-sort: mouse-click on a column header.

        Maps the clicked column key to a sort mode via
        :func:`sort_mode_for_column`. Clicking a non-sortable
        header (``when``, ``what``, model, raw counts) returns
        ``None`` and the click is ignored. Clicking the already-
        active column toggles back to natural order so the same
        click can "undo" the sort.
        """
        key = event.column_key.value if event.column_key else None
        if key is None:
            return
        target = sort_mode_for_column(key)
        if target is None:
            return
        if self._sort_mode == target.id and target.id != "natural":
            self._sort_mode = "natural"
        else:
            self._sort_mode = target.id
        self._repopulate_table()

    def action_open_summary(self) -> None:
        """Show the full session-level summary.

        Runs the same metrics computation the /measure-usage
        skill produces (duration, token breakdown by type,
        tool cost table, subagent rollups) and renders it via
        the plain CLI's ``format_metrics`` so the two UIs stay
        byte-identical. No file re-read — reuses the already-
        parsed data the screen already has in memory.
        """
        self.app.push_screen(
            SummaryModal(
                title=self._title,
                parsed=self._parsed,
                tree=self._tree,
                transcript_path=self._transcript_path,
            )
        )

    def action_reload(self) -> None:
        """Re-parse the transcript from disk and rebuild the rows.

        Preserves the current sort mode and filter text so a
        user who's set up a filter doesn't have to re-type it
        on reload.

        No-op on subagent drill-in screens — those don't own a
        file path; they'd need to reload the parent session
        instead, which is better expressed by popping back and
        reloading there.
        """
        if self._transcript_path is None:
            return
        from ..parse import (
            build_agent_tree,
            find_subagent_transcripts,
            parse_transcript,
        )

        path = str(self._transcript_path)
        self._parsed = parse_transcript(path)
        sub_infos = find_subagent_transcripts(path, 0)
        self._tree = build_agent_tree(path, self._parsed, sub_infos)
        self._rows = build_detail_rows(self._parsed, self._tree)
        # Refresh the top-of-table header too — turn count and
        # totals may have changed since the screen first loaded.
        self.query_one("#detail_header", Label).update(
            self._build_header_text()
        )
        self._repopulate_table()

    def action_open_filter(self) -> None:
        """Show the filter input and give it focus.

        If the filter is already active, re-focusing it lets the
        user edit the current query in place.
        """
        inp = self.query_one("#filter_input", _FilterInput)
        inp.add_class("-active")
        inp.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "filter_input":
            return
        self._filter_text = event.value
        self._repopulate_table()

    def close_filter(self) -> None:
        """Dismiss the filter input and restore the full listing.

        Called by the filter Input's own Esc binding so that the
        screen's Esc = back binding doesn't fire while the input
        has focus.
        """
        inp = self.query_one("#filter_input", _FilterInput)
        inp.value = ""
        inp.remove_class("-active")
        self._filter_text = ""
        self._repopulate_table()
        self.query_one(DataTable).focus()

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        idx = event.cursor_row
        visible = getattr(self, "_visible_rows", self._rows)
        if idx is None or idx >= len(visible):
            return
        row = visible[idx]
        if row.kind == "turn":
            self.app.push_screen(
                TurnDetailModal(row),
                self._handle_modal_result,
            )
        elif row.kind == "subagent":
            self._drill_into_subagent(row.raw.get("subagent") or {})
        elif row.kind == "nonturn":
            self.app.push_screen(NonturnDetailModal(row))

    def _handle_modal_result(self, result) -> None:
        """Callback for ``TurnDetailModal`` dismissal.

        The modal returns a dict ``{"drill": <subagent_node>}``
        when the user picked a subagent from its list, or ``None``
        when they just closed it. Anything else is a bug.
        """
        if not result:
            return
        child = result.get("drill")
        if child:
            self._drill_into_subagent(child)

    def _drill_into_subagent(self, child: dict) -> None:
        """Push a new ``SessionDetailScreen`` for a subagent node.

        A tree node already carries ``rows``, ``turns``,
        ``tokens_by_model``, ``peak_context_tokens`` — exactly the
        fields ``build_detail_rows`` and the header need — so we
        pass the node straight through as the "parsed" argument
        and its ``children`` as the new "tree". The title grows
        a breadcrumb so the screen header reads as a chain back
        to the root session.
        """
        if not child:
            return
        sub_title = f"{self._title}  →  ↳{short_agent_id(child.get('path', ''))}"
        self.app.push_screen(
            SessionDetailScreen(
                sub_title,
                child,
                child.get("children") or [],
            )
        )

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()

    def action_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                title="claude-usage-tui  —  Session detail",
                bindings=list(self.BINDINGS),
                intro=(
                    "Per-turn timeline for one session. Enter "
                    "on a turn row opens the detail modal; on "
                    "a subagent footnote row drills into its "
                    "own detail screen. s cycles sort, / "
                    "filters, i shows the session summary, r "
                    "reloads from disk.\n"
                ),
            )
        )


def _dominant_model(tokens_by_model: dict) -> str:
    """Pick the model with the most raw tokens across all categories."""
    best = ""
    best_total = -1
    for model, counts in tokens_by_model.items():
        total = sum(counts.get(k, 0) for k in counts)
        if total > best_total:
            best = model
            best_total = total
    return best


class _FilterInput(Input):
    """Input that swallows Esc to dismiss the filter bar.

    Without this override, pressing Esc while the input has focus
    would bubble to the screen's ``escape = back`` binding and
    pop the whole detail screen — surprising when the user just
    wants to cancel a filter.
    """

    BINDINGS = [
        Binding("escape", "cancel_filter", "Cancel", show=False),
    ]

    def action_cancel_filter(self) -> None:
        screen = self.screen
        if isinstance(screen, SessionDetailScreen):
            screen.close_filter()


class TurnDetailModal(ModalScreen):
    """Full-screen overlay showing everything about one turn.

    Built from a :class:`DetailRow` whose ``raw`` dict already
    contains the underlying turn payload, the direct subagent
    children, and the pre-computed cost breakdown — nothing gets
    re-parsed. Dismisses with ``None`` on plain close, or with
    ``{"drill": <child_node>}`` when the user picks a subagent
    to drill into.

    ``enter`` is also bound to close so a user who opened the
    modal to read its content can dismiss it with the same key
    they used to open it. The OptionList that holds spawned
    subagents carries its own priority ``enter`` binding for
    row selection, so when it has focus that wins and Enter
    drills instead — consistent with every other list widget
    in the app.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("enter", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    DEFAULT_CSS = """
    TurnDetailModal {
        align: center middle;
    }
    TurnDetailModal > Vertical {
        width: 90%;
        max-width: 130;
        height: 85%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    TurnDetailModal #modal_header {
        height: auto;
        color: $accent;
        text-style: bold;
    }
    TurnDetailModal #modal_body {
        height: 1fr;
        padding-top: 1;
    }
    TurnDetailModal #modal_body Static {
        height: auto;
        margin-bottom: 1;
    }
    TurnDetailModal #modal_body Label {
        height: auto;
        color: $text-muted;
        margin-top: 1;
    }
    TurnDetailModal #modal_footer {
        height: 1;
        color: $text-muted;
        dock: bottom;
        padding-top: 1;
    }
    """

    def __init__(self, row: DetailRow) -> None:
        super().__init__()
        self._row = row
        raw = row.raw
        self._turn = raw.get("turn") or {}
        self._children: list[dict] = raw.get("children") or []
        breakdown = raw.get("breakdown")
        if breakdown is None:
            # Shouldn't happen in practice — detail_rows always
            # populates breakdown — but be defensive so a malformed
            # row doesn't throw on modal open.
            breakdown = turn_cost_breakdown(self._turn, self._children, 0.0)
        self._breakdown: TurnCostBreakdown = breakdown

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._header_text(), id="modal_header")
            with VerticalScroll(id="modal_body"):
                yield Static(self._cost_text(), markup=False)
                yield Static(self._raw_counts_text(), markup=False)
                text_preview = (self._turn.get("text_preview") or "").strip()
                if text_preview:
                    yield Label("Text preview")
                    yield Static(text_preview, markup=False)
                tool_calls = self._turn.get("tool_calls") or []
                if tool_calls:
                    yield Label(f"Tool calls ({len(tool_calls)})")
                    yield Static(self._tool_calls_text(tool_calls), markup=False)
                if self._children:
                    yield Label("Spawned subagents")
                    yield OptionList(
                        *[
                            Option(self._subagent_option_text(c), id=str(i))
                            for i, c in enumerate(self._children)
                        ],
                        id="sub_list",
                    )
            yield Static(self._footer_hint(), id="modal_footer")

    def on_mount(self) -> None:
        # When the turn spawned subagents, focus the OptionList so
        # its priority enter binding wins over the screen-level
        # enter = close — Enter drills into the subagent instead
        # of dismissing the modal. For turns without children
        # there's nothing to focus and Enter falls through to
        # close, which is what a user expects from an
        # informational popup.
        if self._children:
            try:
                self.query_one("#sub_list", OptionList).focus()
            except Exception:
                pass

    def _header_text(self) -> str:
        t = self._turn
        num = self._row.num or str(t.get("turn_num", "?"))
        when = self._row.when or ""
        took = self._row.took or ""
        bits = [f"Turn {num}"]
        if when:
            bits.append(when)
        if took:
            bits.append(f"took {took}")
        bits.append(f"{short_tokens(round(self._breakdown.cost))} cost")
        return "  ·  ".join(bits)

    def _cost_text(self) -> str:
        b = self._breakdown
        lines = [
            f"Model:    {t_or_dash(self._turn.get('model'))}",
            (
                f"Cost:     {short_tokens(round(b.cost))} "
                f"(= own {short_tokens(round(b.own))} "
                f"+ carry {short_tokens(round(b.inherit))}"
                + (
                    f" · caused +{short_tokens(round(b.caused))}"
                    if b.caused else ""
                )
                + ")"
            ),
        ]
        if b.subagents_seq > 0:
            lines.append("            own decomposes as:")
            lines.append(
                f"              self           {short_tokens(round(b.self_seq))}"
            )
            lines.append(
                f"              subagents    {short_tokens(round(b.subagents_seq))}"
            )
        return "\n".join(lines)

    def _raw_counts_text(self) -> str:
        t = self._turn
        return (
            f"Raw:      in {short_tokens(t.get('in_tokens', 0))}   "
            f"out {short_tokens(t.get('out_tokens', 0))}   "
            f"cache_r {short_tokens(t.get('cache_r', 0))}   "
            f"cache_w {short_tokens(t.get('cache_w', 0))}   "
            f"ctx {short_tokens(t.get('ctx', 0))}"
        )

    def _tool_calls_text(self, tool_calls: list[dict]) -> str:
        import json

        lines = []
        for i, tc in enumerate(tool_calls, 1):
            name = tc.get("name", "unknown")
            lines.append(f"[{i}] {name}")
            inp = tc.get("input") or {}
            if inp:
                try:
                    # Pretty-print so long Bash commands / large
                    # inputs wrap naturally in the modal body.
                    pretty = json.dumps(inp, indent=2, ensure_ascii=False)
                except Exception:
                    pretty = repr(inp)
                # Indent every line so the body reads as a block
                # under the tool-name header.
                for line in pretty.splitlines():
                    lines.append(f"    {line}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def _subagent_option_text(self, child: dict) -> str:
        cid = short_agent_id(child.get("path", ""))
        tool = child.get("call_tool") or "↳"
        desc = (
            child.get("call_description")
            or (child.get("meta") or {}).get("description", "")
            or ""
        )
        turns = child.get("turn_count", 0)
        from .detail_rows import _node_subtree_seq

        subtree = _node_subtree_seq(child)
        bits = [f"↳{cid}", f"[{tool}]"]
        if desc:
            bits.append(f'"{desc}"')
        bits.append(f"{turns} turn{'s' if turns != 1 else ''}")
        bits.append(short_tokens(round(subtree)))
        return "   ".join(bits)

    def _footer_hint(self) -> str:
        if self._children:
            return "↵ drill into subagent   ·   esc close"
        return "esc close"

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        idx = int(event.option.id or "-1")
        if 0 <= idx < len(self._children):
            self.dismiss({"drill": self._children[idx]})

    def action_close(self) -> None:
        self.dismiss(None)


def t_or_dash(value) -> str:
    return str(value) if value else "—"


class HelpModal(ModalScreen):
    """Keybinding reference overlay.

    Renders whatever bindings the caller passes in — each
    screen's ``action_help`` hands its own :data:`BINDINGS` list
    through, so the help always matches the current screen. The
    same class serves every screen; no per-screen subclass.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("enter", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }
    HelpModal > Vertical {
        width: 80%;
        max-width: 90;
        height: auto;
        max-height: 85%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    HelpModal #modal_header {
        height: auto;
        color: $accent;
        text-style: bold;
    }
    HelpModal #modal_body {
        height: auto;
        padding-top: 1;
    }
    HelpModal #modal_body Static {
        height: auto;
    }
    HelpModal #modal_footer {
        height: 1;
        color: $text-muted;
        padding-top: 1;
    }
    """

    # Keys whose internal binding name differs from what the
    # user actually presses. Extend as new bindings appear.
    _KEY_DISPLAY = {
        "question_mark": "?",
        "slash": "/",
        "escape": "Esc",
        "enter": "↵",
        "left": "←",
        "right": "→",
        "up": "↑",
        "down": "↓",
        "pageup": "PgUp",
        "pagedown": "PgDn",
        "home": "Home",
        "end": "End",
    }

    def __init__(
        self,
        title: str,
        bindings: list[Binding],
        intro: str = "",
    ) -> None:
        super().__init__()
        self._title = title
        # NOTE: cannot be named ``self._bindings`` — Textual's
        # DOMNode.__init__ already owns that attribute (it's the
        # instance-level BindingsMap) and overwriting it with a
        # plain list blows up the binding chain during key
        # dispatch with "AttributeError: 'list' object has no
        # attribute 'key_to_bindings'".
        self._display_bindings = bindings
        self._intro = intro

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, id="modal_header")
            with VerticalScroll(id="modal_body"):
                if self._intro:
                    yield Static(self._intro)
                yield Static(self._render_keys())
                yield Static(self._NAVIGATION_HINT)
            yield Static("esc / ↵ close", id="modal_footer")

    _NAVIGATION_HINT = (
        "\nTable navigation:\n"
        "  ↑ / ↓         Move cursor one row\n"
        "  PgUp / PgDn   Move cursor one page\n"
        "  Home / End    Scroll to start / end\n"
        "  ← / →         Horizontal scroll on narrow terminals"
    )

    def _render_keys(self) -> str:
        """Format the binding list as an aligned two-column table.

        Skips bindings with ``show=False`` (they're
        intentionally hidden — e.g. the filter Input's private
        ``escape = cancel_filter``) and bindings without a
        description.
        """
        visible = [
            b for b in self._display_bindings
            if b.show and b.description
        ]
        if not visible:
            return "No keybindings."
        keys = [self._KEY_DISPLAY.get(b.key, b.key) for b in visible]
        width = max(len(k) for k in keys)
        lines = ["Keys:"]
        for key_str, b in zip(keys, visible):
            lines.append(f"  {key_str:<{width}}   {b.description}")
        return "\n".join(lines)

    def action_close(self) -> None:
        self.dismiss(None)


class SummaryModal(ModalScreen):
    """Session-level summary overlay.

    Shows what the ``/measure-usage`` skill shows in its text
    output: duration, token breakdown by type, peak context,
    turn + user-message counts, per-tool cost table, subagent
    tree rollup. Reuses :func:`compute_metrics_from_parsed` and
    :func:`format_metrics` directly so the TUI and the plain
    CLI produce identical summaries — single source of truth.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("enter", "close", "Close"),
        Binding("i", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    DEFAULT_CSS = """
    SummaryModal {
        align: center middle;
    }
    SummaryModal > Vertical {
        width: 90%;
        max-width: 110;
        height: 85%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    SummaryModal #modal_header {
        height: auto;
        color: $accent;
        text-style: bold;
    }
    SummaryModal #modal_path {
        height: auto;
        color: $text-muted;
        margin-top: 1;
    }
    SummaryModal #modal_body {
        height: 1fr;
        padding-top: 1;
    }
    SummaryModal #modal_body Static {
        height: auto;
    }
    SummaryModal #modal_footer {
        height: 1;
        color: $text-muted;
        dock: bottom;
        padding-top: 1;
    }
    """

    def __init__(
        self,
        title: str,
        parsed: dict,
        tree: list[dict],
        transcript_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._parsed = parsed
        self._tree = tree
        # Absolute path to the session's .jsonl transcript, shown so
        # the user can copy it and hand it to an agent to debug the
        # session directly (see issue #10). None on subagent drill-in
        # summaries, whose data comes from the parent's agent tree
        # rather than a standalone file — nothing to point at there.
        self._transcript_path = transcript_path

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Summary  ·  {self._title}", id="modal_header")
            if self._transcript_path is not None:
                yield Static(
                    f"Transcript: {self._transcript_path}",
                    id="modal_path",
                    markup=False,
                )
            with VerticalScroll(id="modal_body"):
                yield Static(self._summary_text())
            yield Static("esc close", id="modal_footer")

    def _summary_text(self) -> str:
        start_ts = self._parsed.get("first_entry_ts") or 0.0
        metrics = compute_metrics_from_parsed(
            self._parsed, self._tree, start_ts
        )
        return format_metrics(metrics)

    def action_close(self) -> None:
        self.dismiss(None)


class NonturnDetailModal(ModalScreen):
    """Full-payload modal for non-turn timeline rows.

    Renders whichever fields are meaningful for the row's
    ``kind`` — the ``allowedTools`` list for a
    ``command_permissions`` attachment, the full user message
    text, compaction metadata, etc. Unknown kinds fall back to
    a pretty-printed JSON dump so nothing is ever invisible.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("enter", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    DEFAULT_CSS = """
    NonturnDetailModal {
        align: center middle;
    }
    NonturnDetailModal > Vertical {
        width: 90%;
        max-width: 120;
        height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    NonturnDetailModal #modal_header {
        height: auto;
        color: $accent;
        text-style: bold;
    }
    NonturnDetailModal #modal_body {
        height: 1fr;
        padding-top: 1;
    }
    NonturnDetailModal #modal_body Static {
        height: auto;
        margin-bottom: 1;
    }
    NonturnDetailModal #modal_footer {
        height: 1;
        color: $text-muted;
        dock: bottom;
        padding-top: 1;
    }
    """

    def __init__(self, row: DetailRow) -> None:
        super().__init__()
        self._row = row
        self._entry = (row.raw.get("nonturn") or {}).get("entry") or {}
        self._kind = (row.raw.get("nonturn") or {}).get("kind") or ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._header_text(), id="modal_header")
            with VerticalScroll(id="modal_body"):
                yield Static(self._body_text(), markup=False)
            yield Static("esc close", id="modal_footer")

    def _header_text(self) -> str:
        bits = [self._kind or "event"]
        if self._row.when:
            bits.append(self._row.when)
        return "  ·  ".join(bits)

    def _body_text(self) -> str:
        """Dispatch on ``kind`` for structured per-type rendering.

        Each branch pulls whichever fields are meaningful for
        that entry type. The ultimate fallback is a pretty JSON
        dump so fields we haven't classified still display.
        """
        kind = self._kind
        entry = self._entry
        if kind == "user":
            return self._render_user(entry)
        if kind == "slash-command":
            return self._render_slash(entry)
        if kind.startswith("attachment:"):
            return self._render_attachment(kind, entry)
        if kind == "permission-mode":
            return self._render_permission_mode(entry)
        if kind == "system:compact_boundary":
            return self._render_compact(entry)
        if kind.startswith("system:"):
            return self._render_system(entry)
        if kind == "interrupt":
            return self._render_user(entry)
        return self._render_json_fallback(entry)

    # --- per-kind renderers ---

    def _render_user(self, entry: dict) -> str:
        msg = entry.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(block.get("text") or "")
            return "\n\n".join(p for p in parts if p)
        return self._render_json_fallback(entry)

    def _render_slash(self, entry: dict) -> str:
        import re

        msg = entry.get("message") or {}
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += block.get("text") or ""
        name_match = re.search(
            r"<command-name>(.*?)</command-name>", text, re.DOTALL
        )
        args_match = re.search(
            r"<command-args>(.*?)</command-args>", text, re.DOTALL
        )
        msg_match = re.search(
            r"<command-message>(.*?)</command-message>", text, re.DOTALL
        )
        lines = []
        if name_match:
            lines.append(f"command: {name_match.group(1).strip()}")
        if args_match and args_match.group(1).strip():
            lines.append(f"args:    {args_match.group(1).strip()}")
        if msg_match and msg_match.group(1).strip():
            lines.append("")
            lines.append(msg_match.group(1).strip())
        return "\n".join(lines) if lines else text

    def _render_attachment(self, kind: str, entry: dict) -> str:
        import json

        att = entry.get("attachment") or {}
        atype = att.get("type", "")
        lines = [f"type: {atype}" if atype else "attachment"]
        if atype == "command_permissions":
            allowed = att.get("allowedTools") or []
            lines.append("")
            lines.append(f"allowedTools ({len(allowed)}):")
            for tool in allowed:
                lines.append(f"  · {tool}")
        elif atype == "deferred_tools_delta":
            added = att.get("addedNames") or []
            removed = att.get("removedNames") or []
            if added:
                lines.append("")
                lines.append(f"added ({len(added)}):")
                for name in added:
                    lines.append(f"  + {name}")
            if removed:
                lines.append("")
                lines.append(f"removed ({len(removed)}):")
                for name in removed:
                    lines.append(f"  - {name}")
        else:
            # Unknown attachment type — dump the raw payload so the
            # user can still see what was attached.
            try:
                pretty = json.dumps(att, indent=2, ensure_ascii=False)
            except Exception:
                pretty = repr(att)
            lines.append("")
            lines.append(pretty)
        return "\n".join(lines)

    def _render_permission_mode(self, entry: dict) -> str:
        mode = entry.get("permissionMode", "?")
        return f"new mode: {mode}"

    def _render_compact(self, entry: dict) -> str:
        meta = entry.get("compactMetadata") or {}
        lines = []
        trig = meta.get("trigger")
        if trig:
            lines.append(f"trigger:   {trig}")
        pre = meta.get("preTokens")
        if pre is not None:
            lines.append(f"preTokens: {pre}")
        post = meta.get("postTokens")
        if post is not None:
            lines.append(f"postTokens: {post}")
        if not lines:
            return self._render_json_fallback(entry)
        return "\n".join(lines)

    def _render_system(self, entry: dict) -> str:
        content = entry.get("content")
        if isinstance(content, str) and content:
            return content
        return self._render_json_fallback(entry)

    def _render_json_fallback(self, entry: dict) -> str:
        """Last-resort pretty JSON dump for unclassified payloads.

        A few bookkeeping fields that are never interesting to a
        human reader get filtered out so the dump stays readable.
        """
        import json

        filtered = {
            k: v for k, v in entry.items()
            if k not in {"uuid", "parentUuid", "sessionId", "userType",
                         "entrypoint", "isSidechain", "version", "gitBranch"}
        }
        try:
            return json.dumps(filtered, indent=2, ensure_ascii=False)
        except Exception:
            return repr(filtered)

    def action_close(self) -> None:
        self.dismiss(None)


def _cells_for(row: DetailRow):
    """Turn a :class:`DetailRow` into styled cells for DataTable.

    Row styling differentiates three kinds of content by role:

    - **Turn rows** keep default styling (primary content). Only
      the model cell is colored by family so opus / sonnet /
      haiku are distinguishable at a glance across a long
      session.
    - **Subagent rows** render in cyan so they visually pop as
      actionable footnotes — Enter drills into them, they're
      real work, and they shouldn't look identical to the inert
      annotation rows below.
    - **Non-turn rows** (user messages, attachments, compact
      boundaries, permission changes) render dim. They're
      structural anchors, not data the user is scanning for
      hotspots.
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
        out: list = list(cells_raw)
        model_color = _model_color(row.model)
        if model_color:
            # Column index 9 is the model cell. Swap in a Rich
            # Text object so only that cell is colored; the
            # rest of the row stays at default style.
            out[9] = Text(row.model or "", style=model_color)
        return out
    if row.kind == "subagent":
        model_color = _model_color(row.model)
        styled: list = []
        for i, c in enumerate(cells_raw):
            if i == 9 and model_color:
                styled.append(Text(c or "", style=f"{model_color} bold"))
            else:
                styled.append(Text(c or "", style="cyan"))
        return styled
    # nonturn
    return [Text(c or "", style="dim") for c in cells_raw]


def _model_color(model: str) -> str | None:
    """Map a model family to a Rich color name.

    Uses ANSI-named colors so the result respects whatever
    terminal theme the user runs — cyan / red / green all adapt
    to both dark and light backgrounds. Fable = magenta (above
    Opus pricing), Opus = red (expensive), Sonnet = no color
    (baseline), Haiku = green (cheapest). Unknown models return
    ``None`` → no coloring.
    """
    if not model:
        return None
    if "fable" in model or "mythos" in model:
        return "magenta"
    if "opus" in model:
        return "red"
    if "haiku" in model:
        return "green"
    return None
