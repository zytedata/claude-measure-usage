"""``python -m claude_measure_usage`` — launches the interactive TUI.

The ``claude-measure-usage`` console script from :mod:`pyproject.toml`
resolves to :func:`main` here too, so both invocation forms share
one entry point. The TUI implementation lives in
:mod:`claude_measure_usage.tui.app`.

Textual is imported lazily inside :func:`main` so that simply
importing :mod:`claude_measure_usage` — which the plain CLI and the
re-export facade both do — never pulls Textual into ``sys.modules``.
"""


def main() -> None:
    from .tui.app import main as _tui_main

    _tui_main()


if __name__ == "__main__":
    main()
