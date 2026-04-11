"""Interactive Textual UI for claude-usage-tui.

This subpackage must not be imported by :mod:`claude_usage_tui.plain`
or by the top-level :mod:`claude_usage_tui` ``__init__`` — Textual
is only pulled in when the user actually launches the TUI, so the
``/measure-usage`` Claude Code skill (which invokes
``python -m claude_usage_tui.plain``) stays dependency-free.
"""
