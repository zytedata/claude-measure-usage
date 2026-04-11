"""Textual application entry point for claude-usage-tui.

The UX contract this app implements lives in ``docs/tui-ux.md``.
The app itself is a thin container — all real behavior lives in
the pushed screens.
"""

from textual.app import App

from .screens import ProjectScreen


class ClaudeUsageTuiApp(App):
    """Top-level Textual application."""

    TITLE = "claude-usage-tui"

    def on_mount(self) -> None:
        self.push_screen(ProjectScreen())


def main() -> None:
    """Entry point for the ``claude-usage-tui`` console script."""
    ClaudeUsageTuiApp().run()


if __name__ == "__main__":
    main()
