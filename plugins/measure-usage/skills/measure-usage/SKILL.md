---
name: measure-usage
description: Track token usage, cost breakdown, and tool stats during a Claude Code session. Use when the user asks about token usage, session cost, how expensive something was, or wants to measure/monitor resource consumption. No args = start/view tracking. "session" = full session stats. "stop" = stop and save.
tools: Bash
---

# Measure Usage

Run one of these commands based on `$ARGUMENTS`:

- `$ARGUMENTS` contains "session" → full session stats (no tracking needed):
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m measure_usage session "${CLAUDE_SESSION_ID}"
  ```

- `$ARGUMENTS` contains "stop" → stop tracking, show final stats, save to disk:
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m measure_usage stop "${CLAUDE_SESSION_ID}"
  ```

- Otherwise (empty or "start") → start tracking, or show stats if already tracking:
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m measure_usage start "${CLAUDE_SESSION_ID}"
  ```

Show the script output to the user as-is.
