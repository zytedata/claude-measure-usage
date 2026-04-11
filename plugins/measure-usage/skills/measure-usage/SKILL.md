---
name: measure-usage
description: Track token usage, cost breakdown, and tool stats during a Claude Code session. Use when the user asks about token usage, session cost, how expensive something was, how many tokens, what the context window looks like, or wants to measure/monitor resource consumption. Also trigger for "show usage", "how much did that cost", "what did I spend", "session stats", "show me the breakdown", "how many turns", "per-turn", "per turn breakdown", "turn-by-turn". No args = full session stats. "start" = begin tracking from this point. "stop" = stop tracking and save. "stats" = check tracked stats. "turns" = per-turn drill-down.
tools: Bash
---

# Measure Usage

Run one of these commands based on `$ARGUMENTS`:

- `$ARGUMENTS` contains "start" → begin tracking from this point:
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m claude_usage_tui.plain start "${CLAUDE_SESSION_ID}"
  ```

- `$ARGUMENTS` contains "stop" → stop tracking, show final stats, save to disk:
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m claude_usage_tui.plain stop "${CLAUDE_SESSION_ID}"
  ```

- `$ARGUMENTS` contains "stats" → check stats while tracking continues:
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m claude_usage_tui.plain stats "${CLAUDE_SESSION_ID}"
  ```

- `$ARGUMENTS` contains "turns", "per-turn", or "per turn" → per-turn drill-down table (main session + one table per subagent). If `$ARGUMENTS` also contains a session id (a UUID-like token), use it; otherwise default to the current session:
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m claude_usage_tui.plain turns "${CLAUDE_SESSION_ID}"
  ```

- Otherwise (default) → full session stats from the beginning:
  ```bash
  cd "${CLAUDE_SKILL_DIR}" && python3 -m claude_usage_tui.plain session "${CLAUDE_SESSION_ID}"
  ```

For `session`, `start`, `stop`, `stats`: don't repeat the full output. Summarize the key takeaways: total cost, top cost drivers, anything notable (e.g. a subagent using more than main, high context usage). Mention that full details are in the collapsed Bash output above.

For `turns`: the per-turn table IS the output — do not re-render it. Summarize by pointing at notable turns (the most expensive ones, turns that spawned subagents with large `sub` rollups, turns where context grew sharply) by their turn number, and tell the user to expand the Bash output to see the full table. If subagent tables are present, note which ones are most expensive and their short `↳id` anchors so the user can jump to them.
