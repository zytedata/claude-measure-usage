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

- **Cost breakdown** — [Sonnet input-equivalent](#token-cost-sonnet-input-equivalent) tokens with percentage by type
- **Token usage** — input, output, cache creation (5m/1h tiers), cache read
- **Per-model breakdown** — when multiple models are used (e.g. opus + haiku)
- **Peak context** — max tokens sent to the model in a single turn
- **Tool calls** — count, estimated cost (invoke + carry), and wall time
- **User messages** — number of user prompts
- **Agent/skill tree** — hierarchical breakdown with per-agent cost and context

## Example output

```
Duration: 89m 3s
Tokens: 11.0M (Sonnet input-equivalent)
  52%  Cache read: 5.7M
  33%  Cache write: 2.8M (5m) + 914.9K (1h)
  15%  Output: 1.6M
  <1%  Input: 36.3K
Peak context: 148.5K
Model turns: 962
User messages: 46
Tool calls (cost est.):
                    total  invoke   carry  count     wall
  Bash               1.4M  751.2K  650.4K    336   15m 2s
  Read               1.0M  275.0K  730.8K    160    46.1s
  Write            233.1K  227.9K    5.3K     17     3.1s
  Agent            152.2K   22.8K  129.4K     16  44m 35s
  Grep              82.0K   17.6K   64.4K     35     0.8s
  Skill             56.7K   30.4K   26.3K     28   48m 1s
  Edit              30.6K   17.3K   13.3K     17     0.4s
  Glob               3.5K    1.7K    1.7K      6     0.3s
Breakdown:
                                            tokens  turns  context
Main session                               4209.0K    231   148.5K
  ├─ Agent codegen-analyze list-1            26.5K      2     9.9K
  │  └─ Skill scrape-codegen-analyze        514.8K     50    49.6K
  ├─ Agent codegen-analyze list-2            24.0K      2    10.0K
  │  └─ Skill scrape-codegen-analyze        670.1K     71    51.4K
  ├─ Skill scrape-explore-site              587.0K     80    40.0K
  ├─ Agent codegen-analyze list-3            11.9K      2    10.1K
  │  └─ Skill scrape-codegen-analyze        795.8K     64    64.3K
  ├─ Agent codegen-analyze detail-1          30.0K      2    10.5K
  │  └─ Skill scrape-codegen-analyze        427.6K     45    39.5K
  └─ ...
```

> **Summary:** 11.0M Sonnet-equivalent tokens across 962 turns in ~89 minutes.
> Cache dominates (52% read + 33% write). Subagents used more than the main
> session — the three `scrape-codegen-analyze` list skills alone account for ~2M
> tokens. `codegen-analyze list-3` has the highest context at 64.3K.
> Full details in the collapsed Bash output above.

## How costs are calculated

### Token cost (Sonnet input-equivalent)

Raw token counts are hard to reason about — 100K cache-read tokens cost 10x less
than 100K output tokens, and the same tokens on Opus cost 5x more than on Sonnet.
**Sonnet input-equivalent** is a single normalized unit that accounts for both
token type and model, so you can compare and sum everything directly.

The conversion works in two steps:

**1. Token type multipliers** (consistent across all Claude models, based on
the ratio of per-type prices to the input price):

| Type | Multiplier | Why |
|------|-----------|-----|
| Cache read | 0.1x | Cheapest — reusing cached context |
| Input | 1x | Baseline |
| Cache write (5m) | 1.25x | Writing to short-lived cache |
| Cache write (1h) | 2x | Writing to longer-lived cache |
| Output | 5x | Most expensive — model generation |

**2. Model scaling** (when multiple models are used in a session):

| Model | Scale | Input price |
|-------|-------|-------------|
| Haiku | 0.33x | $1/M |
| Sonnet | 1x | $3/M |
| Opus | 1.67x | $5/M |

So 1000 output tokens on Opus = 1000 × 5 (output) × 1.67 (opus) = 8,350
Sonnet input-equivalent tokens. This makes it easy to see where money is actually
going. Legacy models (Opus 4.1 and earlier, Haiku 3.5 and earlier) are also
supported at their original prices.

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
