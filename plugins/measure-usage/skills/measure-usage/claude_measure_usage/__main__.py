"""``python -m claude_measure_usage`` — launches the interactive TUI.

The ``claude-measure-usage`` console script from :mod:`pyproject.toml`
resolves to :func:`main` here too, so both invocation forms share
one entry point. The TUI implementation lives in
:mod:`claude_measure_usage.tui.app`.

Textual is imported lazily inside :func:`main` so that simply
importing :mod:`claude_measure_usage` — which the plain CLI and the
re-export facade both do — never pulls Textual into ``sys.modules``.
It ships as the ``tui`` extra, so it may not be installed at all.
"""


def main() -> None:
    try:
        from .tui.app import main as _tui_main
    except ImportError as exc:
        if (exc.name or "").split(".")[0] != "textual":
            raise
        raise SystemExit(
            "The interactive TUI needs Textual, which is not installed.\n"
            "Install it with: pip install claude-measure-usage[tui]"
        ) from exc

    _tui_main()


if __name__ == "__main__":
    main()
