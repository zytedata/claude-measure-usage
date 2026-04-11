"""``python -m claude_usage_tui`` — launches the interactive TUI.

The TUI is not implemented yet. For the text report used by the
``/measure-usage`` skill, run ``python -m claude_usage_tui.plain``.
"""

import sys


def main():
    print(
        "The claude-usage-tui interactive UI is not implemented yet.\n"
        "For the text report, run: python -m claude_usage_tui.plain",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
