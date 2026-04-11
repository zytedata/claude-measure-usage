"""Textual application entry point for claude-usage-tui.

The UX contract this app implements lives in ``docs/tui-ux.md``.
v1 ships three screens (project picker → session picker → session
detail) plus a turn detail modal; this module currently only
launches the Textual runtime with a placeholder screen so the entry
point plumbing can be verified end-to-end before real screens land.
"""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Static


class ClaudeUsageTuiApp(App):
    """Top-level Textual application."""

    TITLE = "claude-usage-tui"
    SUB_TITLE = "interactive debugger for Claude Code token usage"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("?", "help", "Help", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(
            "claude-usage-tui scaffold.\n\n"
            "Screens will land incrementally:\n"
            "  1. project picker\n"
            "  2. session picker\n"
            "  3. session detail (turn table + modals)\n\n"
            "Press q to quit.",
            id="placeholder",
        )
        yield Footer()

    def action_help(self) -> None:
        # Placeholder — will open the help overlay once the screens
        # are wired up. Currently a no-op so the binding is visible.
        pass


def main() -> None:
    """Entry point for the ``claude-usage-tui`` console script."""
    ClaudeUsageTuiApp().run()


if __name__ == "__main__":
    main()
