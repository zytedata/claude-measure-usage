# measure-usage

Claude Code plugin that tracks token usage, cost breakdown, and tool stats during sessions.

## Usage

Full session stats (default, no tracking needed):

```
/measure-usage
```

Start tracking from this point:

```
/measure-usage start
```

Check stats (while tracking continues):

```
/measure-usage stats
```

Stop tracking and save metrics:

```
/measure-usage stop
```

## What it tracks

- **Cost breakdown** — Sonnet input-equivalent tokens with percentage by type
- **Token usage** — input, output, cache creation (5m/1h tiers), cache read
- **Per-model breakdown** — when multiple models are used (e.g. opus + haiku)
- **Peak context** — max tokens sent to the model in a single turn
- **Tool calls** — count, estimated cost (invoke + carry), and wall time
- **User messages** — number of user prompts
- **Agent/skill tree** — hierarchical breakdown with per-agent cost and context

## Example output

```
Duration: 146m 50s
Tokens: 33.1M (Sonnet input-equivalent)
  52%  Cache read: 17.1M
  33%  Cache write: 8.3M (5m) + 2.7M (1h)
  15%  Output: 4.9M
  <1%  Input: 108.9K
Peak context: 148.5K
Model turns: 962
User messages: 46
Tool calls (cost est.):
                    total  invoke   carry  count     wall
  Bash               4.2M    2.3M    2.0M    336   15m 2s
  Read               3.0M  824.9K    2.2M    160    46.1s
  Write            699.4K  683.7K   15.8K     17     3.1s
  Agent            456.5K   68.3K  388.2K     16  44m 35s
  Grep             246.0K   52.9K  193.2K     35     0.8s
  Skill            170.0K   91.3K   78.8K     28   48m 1s
  Edit              91.8K   51.8K   40.0K     17     0.4s
  Glob              10.4K    5.2K    5.2K      6     0.3s
Breakdown:
                                             tokens  turns  context
Main session                               12627.1K    231   148.5K
  ├─ Agent codegen-analyze list-1             79.6K      2     9.9K
  │  └─ Skill scrape-codegen-analyze        1544.4K     50    49.6K
  ├─ Agent codegen-analyze list-2             71.9K      2    10.0K
  │  └─ Skill scrape-codegen-analyze        2010.2K     71    51.4K
  ├─ Skill scrape-explore-site              1761.0K     80    40.0K
  ├─ Agent codegen-analyze list-3             35.7K      2    10.1K
  │  └─ Skill scrape-codegen-analyze        2387.4K     64    64.3K
  ├─ Agent codegen-analyze detail-1           90.1K      2    10.5K
  │  └─ Skill scrape-codegen-analyze        1282.7K     45    39.5K
  └─ ...
```

> **Summary:** 33.1M Sonnet-equivalent tokens across 962 turns. Cache dominates
> (52% read + 33% write). Subagents used more than the main session — the three
> `scrape-codegen-analyze` list skills alone account for ~6M tokens.
> `codegen-analyze list-3` has the highest context at 64.3K.
> Full details in the collapsed Bash output above.

## How costs are calculated

### Token cost (Sonnet input-equivalent)

All costs are normalized to **Sonnet input token equivalents**, making it easy to compare
across models and token types. The ratios within a model are consistent:

| Type | Multiplier |
|------|-----------|
| Cache read | 0.1x |
| Input | 1x |
| Cache write (5m) | 1.25x |
| Cache write (1h) | 2x |
| Output | 5x |

When multiple models are used, costs are further scaled by each model's relative
input price (Opus = 5x Sonnet, Haiku = 0.267x Sonnet).

### Tool cost estimates

Tool costs are rough estimates, not exact measurements. They are computed from
the transcript by estimating token counts from text length (~4 chars per token):

- **invoke** — one-time cost of the tool call: `(output_tokens * 5 + input_tokens) * model_scale`.
  Output tokens are estimated from the tool call arguments, input tokens from
  the tool result content.
- **carry** — ongoing cost of the result sitting in context for subsequent turns:
  `input_tokens * 0.1 * model_scale * remaining_turns`. This assumes the result
  stays in context as cached content for all subsequent assistant turns.

Both are approximations. The actual cost depends on caching behavior, context
window management, and prompt structure. Use them for relative comparisons
(which tools are expensive?) rather than absolute numbers.

### Agent tree

The breakdown section shows a hierarchical view of main session and subagents.
Parent-child relationships are inferred by matching timestamps: when an Agent or
Skill tool call in a transcript is followed within 100ms by a new subagent transcript
starting, they are linked as parent and child. The **context** column shows the
peak context window per agent — each agent has its own independent context.

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
