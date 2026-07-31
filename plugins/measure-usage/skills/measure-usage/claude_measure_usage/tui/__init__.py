"""Interactive Textual UI for claude-measure-usage.

This subpackage must not be imported by :mod:`claude_measure_usage.plain`
or by the top-level :mod:`claude_measure_usage` ``__init__`` — Textual
is only pulled in when the user actually launches the TUI, so the
``/measure-usage`` Claude Code skill (which invokes
``python -m claude_measure_usage.plain``) stays dependency-free.
"""
