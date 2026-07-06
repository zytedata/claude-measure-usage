"""Regenerate docs/session-detail.svg from the checked-in transcript.

Usage, from the repo root:

    uv run python docs/screenshot/regenerate.py

The fixture under ``docs/screenshot/fixture/`` is a real Claude Code
session (produced by ``claude -p`` in a throwaway weather-CLI project,
with two parallel Explore subagents), scrubbed of the local username.
This script stages it into a throwaway HOME, drives the TUI to the
session-detail screen with Textual's test pilot, parks the cursor on
the first subagent row, and exports the SVG the README embeds.

The staging HOME must be a dash-free path: the TUI decodes a project
directory name back to a cwd by replacing ``-`` with ``/`` and checking
the result against the filesystem, so any dash in an ancestor directory
would break the decode and the header would show the raw encoded name.
Hence ``/private/tmp/muhome`` rather than a mktemp path.
"""
import asyncio
import os
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "session-detail.svg"
HOME = Path("/private/tmp/muhome")
PROJECT_DIR_NAME = "-private-tmp-muhome-code-forecast"
SIZE = (184, 34)
SUBAGENT_ROW = 6  # rows above the first ↳ row: user, 4 attachments, turn 1


def stage() -> None:
    shutil.rmtree(HOME, ignore_errors=True)
    # The decode target for PROJECT_DIR_NAME must exist on disk.
    (HOME / "code" / "forecast").mkdir(parents=True)
    project = HOME / ".claude" / "projects" / PROJECT_DIR_NAME
    project.parent.mkdir(parents=True)
    shutil.copytree(HERE / "fixture", project)
    os.environ["HOME"] = str(HOME)


async def shoot() -> None:
    from textual.widgets import DataTable

    from claude_usage_tui.tui.app import ClaudeUsageTuiApp
    from claude_usage_tui.tui.screens import SessionDetailScreen, SessionScreen

    app = ClaudeUsageTuiApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.screen.query_one(DataTable).row_count == 1, "project missing"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.1)
            if isinstance(app.screen, SessionScreen):
                if app.screen.query_one(DataTable).row_count > 0:
                    break
        else:
            raise RuntimeError("session picker never populated")
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.1)
            if isinstance(app.screen, SessionDetailScreen):
                break
        else:
            raise RuntimeError("session detail never opened")
        await pilot.pause(0.3)
        for _ in range(SUBAGENT_ROW):
            await pilot.press("down")
        await pilot.pause(0.2)
        OUT.write_text(app.export_screenshot(title="claude-usage-tui"))
        print(f"saved {OUT}")


def main() -> None:
    stage()
    asyncio.run(shoot())
    shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
