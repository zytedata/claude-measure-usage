"""Text-mode CLI for claude_usage_tui.

This subpackage holds the non-interactive renderer and session-state
helpers used by the `/measure-usage` Claude Code skill. It must not
import from :mod:`claude_usage_tui.tui` so that running the skill
never pulls in Textual.
"""
