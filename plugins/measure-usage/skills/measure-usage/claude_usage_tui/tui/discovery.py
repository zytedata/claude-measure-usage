"""Filesystem discovery of Claude Code projects and sessions.

The Claude Code CLI stores each cwd's sessions in a directory under
``~/.claude/projects/`` whose name is the cwd with ``/`` replaced by
``-``. Inside each such directory live one ``.jsonl`` file per
session. This module walks that tree and exposes structured records
for the TUI to render.

No transcript parsing happens here — token totals and turn counts
live a level above, in the parser. Discovery is cheap filesystem
work only, so the project screen can render immediately without
waiting for any transcripts to be read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    # .jsonl files. The name is the cwd with / replaced by -.
    project_dir: Path

    # Best-effort decoded cwd for display. Falls back to the raw
    # directory name when decoding to an existing filesystem path
    # fails (because the encoding is ambiguous when the original
    # path contained literal dashes).
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
        cwd_display=_decode_cwd(project_dir.name),
        session_count=len(sessions),
        last_activity=last_activity,
    )


def _decode_cwd(encoded: str) -> str:
    """Decode a project-dir name back to a displayable cwd.

    Claude Code encodes the cwd by replacing ``/`` with ``-`` — a
    lossy encoding because literal dashes in path components become
    indistinguishable from separators. We take a best-effort approach:
    replace every ``-`` with ``/`` and check the result against the
    filesystem; if it exists, use it. Otherwise fall back to the raw
    encoded string so users still see *something* meaningful.

    Also collapses ``$HOME`` to ``~`` for compactness.
    """
    candidate = "/" + encoded.lstrip("-").replace("-", "/")
    if Path(candidate).exists():
        return _collapse_home(candidate)
    return encoded


def _collapse_home(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


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
    encoded = cwd.replace("/", "-")
    candidate = root / encoded
    if candidate.is_dir():
        return candidate
    return None
