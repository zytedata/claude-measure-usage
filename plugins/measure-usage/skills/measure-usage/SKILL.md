---
name: measure-usage
description: Track token usage, cost breakdown, and tool stats during a Claude Code session. Use when the user asks about token usage, session cost, how expensive something was, or wants to measure/monitor resource consumption. No args = full session stats. "start" = begin tracking from this point. "stop" = stop tracking and save. "stats" = check tracked stats.
tools: Bash
---

# Measure Usage

Run one of these commands based on `$ARGUMENTS`:

- `$ARGUMENTS` contains "start" → begin tracking from this point:
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m measure_usage start "${CLAUDE_SESSION_ID}"
  ```

- `$ARGUMENTS` contains "stop" → stop tracking, show final stats, save to disk:
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m measure_usage stop "${CLAUDE_SESSION_ID}"
  ```

- `$ARGUMENTS` contains "stats" → check stats while tracking continues:
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m measure_usage stats "${CLAUDE_SESSION_ID}"
  ```

- Otherwise (default) → full session stats from the beginning:
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m measure_usage session "${CLAUDE_SESSION_ID}"
  ```

Show the script output to the user as-is (the Bash result is collapsed by default).
