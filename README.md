# skill-tracker

Claude Code plugin that tracks timing, token usage, and context window for skill invocations.

## What it tracks

For every skill invocation, logs to `.skill-tracker/metrics.jsonl`:

- **Wall time** — duration in seconds
- **Token usage** — input, output, cache creation, cache read (broken down)
- **Peak context** — max tokens sent to the model in a single turn
- **Subagent count** — how many subagents were spawned

Works for both `context:fork` skills (parses subagent transcripts) and non-fork skills (parses the main transcript between tool_use and tool_result).

## Install

```bash
claude plugin add /path/to/skill-tracker
```

## Configuration

By default, tracks all skills. To filter by prefix, set:

```bash
export SKILL_TRACKER_PREFIXES="scrape-,deploy-"
```

## Output

Each skill invocation appends one JSON line to `.skill-tracker/metrics.jsonl`:

```json
{
  "timestamp": "2026-04-07T10:30:00Z",
  "skill": "scrape-explore-site",
  "args": "https://example.com",
  "duration_s": 160.5,
  "is_fork": true,
  "total_tokens": 145000,
  "peak_context_tokens": 91000,
  "subagent_count": 3,
  "tokens": {
    "input_tokens": 50000,
    "output_tokens": 30000,
    "cache_creation_input_tokens": 5000,
    "cache_read_input_tokens": 10000
  }
}
```

A summary line is also printed to stderr after each skill run.
