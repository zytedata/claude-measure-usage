"""Filesystem discovery of Claude Code projects and sessions.

The Claude Code CLI stores each cwd's sessions in a directory under
``~/.claude/projects/`` whose name is the cwd with every
non-alphanumeric character replaced by ``-``. Inside each such
directory live one ``.jsonl`` file per session. This module walks that tree and exposes structured records
for the TUI to render.

No transcript parsing happens here — token totals and turn counts
live a level above, in the parser. Discovery is cheap filesystem
work only, so the project screen can render immediately without
waiting for any transcripts to be read.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..metrics import model_aware_cost_breakdown
from ..parse import _iter_transcript, parse_transcript


def default_projects_root() -> Path:
    """Return ``~/.claude/projects``, resolved at call time.

    Resolving lazily (rather than caching at import) means tests
    that monkeypatch ``Path.home`` still redirect discovery — the
    module's consumers never hold onto a pre-computed path.
    """
    return Path.home() / ".claude" / "projects"


@dataclass(frozen=True)
class ProjectEntry:
    """A discovered Claude Code project directory."""

    # Directory under ~/.claude/projects/ that holds this project's
    # .jsonl files.
    project_dir: Path

    # The cwd recorded in the project's transcripts, with the home
    # directory collapsed to ~. Falls back to the raw directory name
    # when the project has no transcript that records a cwd.
    cwd_display: str

    # Number of .jsonl session files in the project directory.
    session_count: int

    # Most recent mtime across .jsonl files, or the directory mtime
    # when the project has no sessions yet. Epoch seconds.
    last_activity: float


def discover_projects(
    projects_root: Path | None = None,
) -> list[ProjectEntry]:
    """Return every project directory under ``projects_root``.

    ``projects_root`` defaults to ``~/.claude/projects``. Results are
    sorted by ``last_activity`` descending so the most recently
    touched project is first — matches the TUI's default sort.
    """
    root = projects_root if projects_root is not None else default_projects_root()
    if not root.exists():
        return []
    entries: list[ProjectEntry] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        entries.append(_project_entry(child))
    entries.sort(key=lambda e: e.last_activity, reverse=True)
    return entries


def _project_entry(project_dir: Path) -> ProjectEntry:
    sessions = sorted(project_dir.glob("*.jsonl"))
    if sessions:
        last_activity = max(p.stat().st_mtime for p in sessions)
    else:
        last_activity = project_dir.stat().st_mtime
    return ProjectEntry(
        project_dir=project_dir,
        cwd_display=_project_cwd(project_dir, sessions),
        session_count=len(sessions),
        last_activity=last_activity,
    )


def _project_cwd(project_dir: Path, sessions: list[Path]) -> str:
    """Return the cwd recorded in the project's transcripts.

    The directory name cannot be decoded back into the cwd, since
    every non-alphanumeric character in the cwd is encoded as ``-``,
    so the cwd is taken from the first transcript record that
    carries one.
    """
    for path in sessions:
        for entry in _iter_transcript(path):
            cwd = entry.get("cwd")
            if cwd:
                return _collapse_home(cwd)
    return project_dir.name


def _collapse_home(path: str) -> str:
    try:
        relative = Path(path).relative_to(Path.home())
    except ValueError:
        return path
    return "~" if relative == Path() else "~/" + relative.as_posix()


@dataclass(frozen=True)
class SessionEntry:
    """A parsed Claude Code session transcript."""

    transcript_path: Path
    session_id: str
    started_ts: float | None
    turn_count: int
    total_seq_tokens: float
    peak_context_tokens: int
    dominant_model: str
    first_user_message: str | None


def list_session_paths(project_dir: Path) -> list[Path]:
    """Return ``*.jsonl`` transcript paths in a project directory.

    Cheap filesystem enumeration — no parsing. Lets the session
    screen mount immediately and kick off transcript parsing as a
    background worker so the UI stays responsive on large projects.
    """
    return [
        p
        for p in sorted(project_dir.glob("*.jsonl"))
        if not p.name.endswith(".meta.json")
    ]


def discover_sessions(project_dir: Path) -> list[SessionEntry]:
    """Return every session transcript inside ``project_dir``.

    Parses each ``*.jsonl`` file with the shared
    :func:`claude_measure_usage.parse.parse_transcript` so the TUI's
    session screen can sort and display real token totals. Empty
    or malformed files are still included as ``SessionEntry``
    records with zeroed fields — the user should see them rather
    than have them silently disappear.

    Results are sorted by ``started_ts`` descending (most recent
    first); sessions without a parsable start timestamp fall to
    the bottom at natural filesystem order.
    """
    entries = [load_session(p) for p in list_session_paths(project_dir)]
    sort_sessions(entries)
    return entries


def sort_sessions(entries: list[SessionEntry]) -> None:
    """Sort session entries in place by ``started_ts`` desc.

    Sessions without a parsable timestamp fall to the bottom in
    natural filesystem order.
    """
    entries.sort(
        key=lambda e: (e.started_ts is None, -(e.started_ts or 0.0)),
    )


def load_session(path: Path) -> SessionEntry:
    parsed = parse_transcript(str(path))
    tokens_by_model = parsed.get("tokens_by_model") or {}
    if tokens_by_model:
        total_seq = model_aware_cost_breakdown(tokens_by_model)["total"]
        dominant = _dominant_model(tokens_by_model)
    else:
        total_seq = 0.0
        dominant = ""
    return SessionEntry(
        transcript_path=path,
        session_id=path.stem,
        started_ts=parsed.get("first_entry_ts"),
        turn_count=parsed.get("turn_count", 0),
        total_seq_tokens=total_seq,
        peak_context_tokens=parsed.get("peak_context_tokens", 0),
        dominant_model=dominant,
        first_user_message=_pick_summary(parsed.get("rows") or []),
    )


def _pick_summary(rows: list[dict]) -> str | None:
    """Return a display-friendly first-user-intent summary.

    Walks the parsed rows stream in order and returns the first row
    that represents a meaningful user intent — either a plain user
    message or a content slash command. Navigation-only slash
    commands (``/clear``, ``/compact``, ``/exit``, etc.) are treated
    as transparent: they mark session boundaries or tooling state
    rather than describing what the session is about, so the picker
    looks past them to the next real intent. Client-side shims
    (task notifications, bash input/output wrappers, ...) are
    already dropped upstream by the nonturn-row pipeline.

    The returned string has the ``[user] `` / ``[slash] `` prefix
    stripped; the session screen has only one column to render
    this in, and the kind marker adds noise without adding
    information the user can't infer from context.
    """
    for row in rows:
        kind = row.get("kind", "")
        what = row.get("what") or ""
        if kind == "slash-command":
            stripped = _strip_prefix(what, "[slash] ")
            cmd = stripped.split()[0] if stripped else ""
            if cmd in _NAVIGATION_SLASH_COMMANDS:
                continue
            return stripped
        if kind == "user":
            return _strip_prefix(what, "[user] ")
    return None


# Slash commands that only adjust Claude Code's session/tooling
# state and say nothing about what the user is working on. Sessions
# that *begin* with one of these (common: ``/clear`` to start a
# fresh context) should be identified by whatever comes next, not
# by the boundary marker itself.
_NAVIGATION_SLASH_COMMANDS = frozenset(
    {
        "/clear",
        "/compact",
        "/exit",
        "/quit",
        "/reset",
        "/init",
        "/login",
        "/logout",
        "/model",
        "/config",
        "/help",
        "/status",
        "/cost",
    }
)


def _strip_prefix(text: str, prefix: str) -> str:
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text


def _dominant_model(tokens_by_model: dict) -> str:
    """Pick the model that accounts for the most raw tokens.

    Uses total raw tokens (in + out + cache_r + cache_w) as the
    ranking key rather than Sonnet-equivalent, because this column
    is informational — we want "the model you used most", not "the
    model that cost the most". A session that used Haiku for
    thousands of turns and Opus for one shouldn't be labeled as
    "opus".
    """
    best_model = ""
    best_total = -1
    for model, counts in tokens_by_model.items():
        total = sum(counts.get(k, 0) for k in counts)
        if total > best_total:
            best_model = model
            best_total = total
    return best_model


def project_for_cwd(
    cwd: str | None = None,
    projects_root: Path | None = None,
) -> Path | None:
    """Return the project directory that corresponds to a given cwd.

    Used by the TUI to land the initial cursor on the current
    directory's project. ``cwd`` defaults to :func:`os.getcwd`.
    """
    if cwd is None:
        cwd = os.getcwd()
    root = projects_root if projects_root is not None else default_projects_root()
    encoded = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    candidate = root / encoded
    if candidate.is_dir():
        return candidate
    return None
