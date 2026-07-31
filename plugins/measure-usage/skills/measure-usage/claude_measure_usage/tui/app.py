"""Textual application entry point for claude-measure-usage.

The UX contract this app implements lives in ``docs/tui-ux.md``.
The app itself is a thin container — all real behavior lives in
the pushed screens.
"""

from textual.app import App

from .screens import ProjectScreen


class MeasureUsageApp(App):
    """Top-level Textual application."""

    TITLE = "claude-measure-usage"

    def on_mount(self) -> None:
        self.push_screen(ProjectScreen())


def main() -> None:
    """Entry point for the ``claude-measure-usage`` console script."""
    MeasureUsageApp().run()


if __name__ == "__main__":
    main()
