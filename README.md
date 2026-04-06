# measure-usage

Claude Code plugin that tracks token usage, cost breakdown, and tool stats during sessions.

## Usage

Full session stats (no tracking needed):

```
/measure-usage session
```

Start tracking from this point:

```
/measure-usage
```

Check stats (while tracking continues):

```
/measure-usage
```

Stop tracking and save metrics:

```
/measure-usage stop
```

## What it tracks

- **Cost breakdown** — input-equivalent tokens with percentage by type
- **Token usage** — input, output, cache creation (5m/1h tiers), cache read
- **Per-model breakdown** — when multiple models are used (e.g. opus + haiku)
- **Peak context** — max tokens sent to the model in a single turn
- **Tool calls** — count by tool name
- **User messages** — number of user prompts
- **Subagents** — count and per-subagent token breakdown

## Install

**Marketplace** — install once, persists across sessions:

```bash
claude plugin marketplace add /path/to/measure-usage
claude plugin install measure-usage
```

**Direct** — load for a single session, ideal for development (changes take effect immediately):

```bash
claude --plugin-dir /path/to/measure-usage/plugins/measure-usage
```

## Output

When tracking is stopped, metrics are saved to `.measure-usage/metrics.jsonl`.
