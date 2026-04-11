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
    Label,
    OptionList,
    ProgressBar,
    Static,
)
from textual.widgets.option_list import Option

from ..metrics import model_aware_cost_breakdown
from ..turns_label import short_agent_id
from .detail_rows import (
    SORT_MODES,
    DetailRow,
    TurnCostBreakdown,
    build_detail_rows,
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
        self._open_session(entry)

    def _open_session(self, entry: SessionEntry) -> None:
        """Parse + build tree on the main thread, then push detail.

        A single-session parse tops out around ~50ms even on big
        sessions; doing it synchronously keeps the open path
        simple and avoids a second progress bar on a screen that
        will be visible for seconds anyway.
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
        title = "  —  ".join([
            self._project.cwd_display,
            short_datetime(entry.started_ts) or entry.session_id[:8],
        ])
        self.app.push_screen(SessionDetailScreen(title, parsed, tree))

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()

    def action_help(self) -> None:
        pass


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
    :class:`SessionDetailScreen` for that subagent (stack grows).
    Non-turn rows are inert for now (a payload modal is a later
    commit).
    """

    BINDINGS = [
        Binding("s", "cycle_sort", "Sort"),
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

    USAGE_UNIT_NOTE = "cost columns are Sonnet input-equivalent"

    def __init__(
        self,
        title: str,
        parsed: dict,
        tree: list[dict],
    ) -> None:
        super().__init__()
        self._title = title
        self._parsed = parsed
        self._tree = tree
        self._rows: list[DetailRow] = []
        self._sort_mode = "natural"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("", id="detail_header")
        table: DataTable[str] = DataTable(id="detail_table", zebra_stripes=True)
        table.cursor_type = "row"
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self._rows = build_detail_rows(self._parsed, self._tree)
        self.query_one("#detail_header", Label).update(self._build_header_text())

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

        self._repopulate_table()
        table.focus()

    def _repopulate_table(self) -> None:
        """Rebuild the DataTable contents for the current sort mode.

        Clears the table and re-adds rows from ``sort_rows``.
        DataTable.clear() is O(rows) but the row count is small
        (hundreds) so this is cheap relative to parsing the
        transcript — we don't bother diffing.
        """
        table = self.query_one(DataTable)
        table.clear()
        visible = sort_rows(self._rows, self._sort_mode)
        self._visible_rows = visible
        for row in visible:
            table.add_row(*_cells_for(row))
        if table.row_count:
            table.cursor_coordinate = Coordinate(0, 0)
        self.sub_title = self._build_sub_title()

    def _build_sub_title(self) -> str:
        """Sub_title shows breadcrumb + unit note + sort indicator."""
        parts = [self._title, self.USAGE_UNIT_NOTE]
        if self._sort_mode != "natural":
            parts.append(f"sort: {self._sort_mode} ▼")
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
        """Advance to the next sort mode in :data:`SORT_MODES`."""
        mode_ids = [m[0] for m in SORT_MODES]
        idx = mode_ids.index(self._sort_mode) if self._sort_mode in mode_ids else 0
        self._sort_mode = mode_ids[(idx + 1) % len(mode_ids)]
        self._repopulate_table()

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
        pass


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


class TurnDetailModal(ModalScreen):
    """Full-screen overlay showing everything about one turn.

    Built from a :class:`DetailRow` whose ``raw`` dict already
    contains the underlying turn payload, the direct subagent
    children, and the pre-computed cost breakdown — nothing gets
    re-parsed. Dismisses with ``None`` on plain close, or with
    ``{"drill": <child_node>}`` when the user picks a subagent
    to drill into.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
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
                yield Static(self._cost_text())
                yield Static(self._raw_counts_text())
                text_preview = (self._turn.get("text_preview") or "").strip()
                if text_preview:
                    yield Label("Text preview")
                    yield Static(text_preview)
                tool_calls = self._turn.get("tool_calls") or []
                if tool_calls:
                    yield Label(f"Tool calls ({len(tool_calls)})")
                    yield Static(self._tool_calls_text(tool_calls))
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
