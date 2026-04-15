# measure-usage

Token usage, cost breakdown, and tool stats for Claude Code sessions. Two ways
to use it:

- **Inspect past sessions** with `claude-usage-tui`, a standalone terminal app
  that walks `~/.claude/projects/` and drills down to any turn or subagent.
- **Inside a live session** with the `/measure-usage` slash command, installed
  as a Claude Code plugin.

## Interactive TUI

`claude-usage-tui` is a read-only debugger for Claude Code session transcripts.
It's not on PyPI yet — install from a local checkout:

```bash
uv tool install --editable /path/to/measure-usage
# or: pip install -e /path/to/measure-usage
```

Then run `claude-usage-tui` from anywhere. It walks `~/.claude/projects/` with
three stacked screens:

1. **Projects** — every project directory with session count and last activity.
2. **Sessions** — transcripts in the selected project with a one-line preview.
3. **Session detail** — the same per-turn table as `/measure-usage turns`;
   drilling into a subagent `↳id` row pushes a new detail screen for it.

Press `?` on any screen for the full keybinding list.

## Slash command

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

Per-turn drill-down (main session + one table per subagent):

```
/measure-usage turns
```

## What it tracks

- **Cost breakdown** — [Sonnet input-equivalent](#token-cost-sonnet-input-equivalent) tokens with percentage by type
- **Token usage** — input, output, cache creation (5m/1h tiers), cache read
- **Per-model breakdown** — when multiple models are used (e.g. opus + haiku)
- **Peak context** — max tokens sent to the model in a single turn
- **Tool calls** — count, estimated cost (invoke + carry), and wall time
- **User messages** — number of user prompts
- **Agent/skill tree** — hierarchical breakdown with per-agent cost and context
- **Per-turn tables** (`turns`) — every assistant turn with a cost
  decomposition (`tokens = own + carry`), downstream `caused` projection,
  wallclock timing, and a short label showing what happened that turn

## Example output

```
Duration: 89m 3s
Tokens: 5.6M (Sonnet input-equivalent)
  58%  Cache read: 3.2M
  34%  Cache write: 1.4M (5m) + 488.4K (1h)
   8%  Output: 447.2K
  <1%  Input: 14.6K
Peak context: 148.5K
Model turns: 523
User messages: 46
Tool calls (cost est.):
                    total  invoke   carry  count     wall
  Bash               1.1M  751.2K  383.8K    336   15m 2s
  Read             694.3K  275.0K  419.3K    160    46.1s
  Write            231.0K  227.9K    3.2K     17     3.1s
  Agent             99.7K   22.8K   77.0K     16  44m 35s
  Grep              55.6K   17.6K   38.0K     35     0.8s
  Skill             46.8K   30.4K   16.4K     28   48m 1s
  Edit              25.2K   17.3K    7.9K     17     0.4s
  Glob               2.5K    1.7K     800      6     0.3s
  AskUserQuestion    2.0K     883    1.1K      1    54.0s
  ToolSearch          458     133     325      1     0.0s
Breakdown:
                                            tokens  turns  context
Main session                               2374.2K    135   148.5K
  ├─ Agent codegen-analyze list-1            26.5K      2     9.9K
  │  └─ Skill scrape-codegen-analyze        251.0K     27    49.6K
  ├─ Agent codegen-analyze list-2            24.0K      2    10.0K
  │  └─ Skill scrape-codegen-analyze        328.0K     40    51.4K
  ├─ Agent codegen-analyze list-3            11.9K      2    10.1K
  │  └─ Skill scrape-codegen-analyze        358.9K     33    64.3K
  ├─ Skill scrape-explore-site              274.5K     43    40.0K
  ├─ Agent codegen-analyze front-1           25.8K      2    10.0K
  │  └─ Skill scrape-codegen-analyze        205.9K     26    43.1K
  └─ ...
```

> **Summary:** 5.6M Sonnet-equivalent tokens across 523 turns in ~89 minutes.
> Cache dominates (58% read + 34% write). Subagents did most of the work —
> the `scrape-codegen-analyze` skills together account for ~1.6M tokens.
> `codegen-analyze list-3` hit the highest subagent context at 64.3K,
> and the session peak context was 148.5K. Full details in the collapsed
> Bash output above.

### Per-turn table (`/measure-usage turns`)

The `turns` command renders one row per assistant turn for the main session
and one table per subagent, linked by short `↳id` anchors that are
searchable in the output. Columns decompose each turn's cost as
`tokens = own + carry`, show the downstream `caused` projection, and
bracket the raw transcript counts in a separate block on the right:

```
Tokens (and own/inherit/caused) are Sonnet input-equivalent, normalized across token type and model. in/out/cache_r/cache_w on the right are raw transcript counts.

Main session  —  135 turns  —  Tokens 5.6M (2.4M own + 3.2M subagents)

  #   when   took │ tokens  = own + carry │ caused  what                                                      ctx │ mdl    in   out cache_r cache_w
──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
      0:00        │                       │         [user] I want to get products from https://www.vinted.com/    │
      0:00        │                       │         [attachment:deferred_tools_delta] +21                         │
  1   0:03        │  13.8K  11.9K    1.9K │  +125K  (startup) [Skill] "scrape"                         ·   16.9K │ opus    3    35   11.3K    5.6K
  2   0:06     2s │  15.7K  13.8K    1.9K │  +140K  [Skill] "scrape-define"                            ·   17.7K │ opus    2    81   11.3K    6.3K
  3   0:09    11s │   6.9K   3.9K    2.9K │   +38K  Let me set up the workspace first.                 ·   19.4K │ opus    2    45   17.7K    1.7K
  4   0:21     8s │   4.7K   1.5K    3.2K │    +6K  Bash "mkdir -p .scrape/vinted-produ…"              ·   19.6K │ opus    1   115   19.4K     266
  5   0:30     3s │   4.1K    787    3.3K │    +3K  ToolSearch "select:AskUserQuestion"                ·   19.8K │ opus    1    61   19.6K     133
  6   0:35    58s │   7.6K   4.3K    3.3K │   +32K  AskUserQuestion                                    ·   21.1K │ opus    3   153   19.6K    1.5K
      1:31        │                       │         [user] https://www.vinted.com/items/…                         │
  7   1:35    17s │   6.5K   3.0K    3.5K │    +6K  Bash "uv run /Users/kmike/svn/scrap…"              ·   21.4K │ opus    3   284   21.1K     292
...
  9   1:52  1m40s │ 146.9K 143.4K    3.6K │   +15K  Page downloaded. Let me analyze it.  [↳a337]      ·   22.1K │ opus    1     1   21.4K     733
...
 17   6:00  4m48s │ 283.1K 278.3K    4.8K │   +33K  Exploring the site to download more pages. [↳a6ae] ·   30.7K │ opus    1    35   29.1K    1.7K
...
 20  10:57     2m │ 756.3K 751.0K    5.3K │    +7K  All 3 detail pages… [6 subagents]                  ·   32.2K │ opus    1     1   31.9K     352
──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
              47m │   5.4M   3.6M    1.7M │                                                                       │ total 168 17.1K   10.5M  146.5K

↳af29  "compare raw vs rendered"  —  parent turn 22  —  2 turns  —  Tokens 24.0K

#  when  took │ tokens = own + carry │ caused  what                                                             ctx │ mdl   in out cache_r cache_w
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
   0:00       │                      │         [user] Read all 6 analysis JSON files in /Users/kmike/svn/…         │
1  0:03    2s │   3.5K  2.3K    1.2K │         (startup) Let me read all 6 JSON files.                       8.5K │ opus   3   2    7.4K    1.1K
2  0:15    9s │  20.5K 19.1K    1.4K │         Here is the comparison report, limited to the 1…             15.9K │ opus   1 419    8.5K    7.5K
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
           12s │  24.0K 21.4K    2.6K │                                                                            │ total  4 421   15.8K    8.6K
```

Column semantics:

- `#` — turn number (model-invocation count). Non-turn rows like user
  prompts or attachments have no number and sit between turns.
- `when` — elapsed time since the session started.
- `took` — wallclock for this turn's own work plus the API round-trip
  latency it triggered (time between the previous turn's tools
  finishing and this turn starting).
- `tokens = own + carry` — Sonnet-equivalent cost decomposition.
  - `own`: what this turn *produced* — model output + tool results it
    wrote to the cache + subagent rollup. This is the part a developer
    can optimize by making the turn leaner.
  - `carry`: what this turn *paid to read* inherited context
    (`cache_r × 0.1 × model_scale`). Mostly determined by earlier
    turns — optimize by trimming upstream bloat.
- `caused` — projected downstream cost: what subsequent turns in the
  same compaction epoch will collectively pay to re-read this turn's
  `cache_w` contribution. Forward-attributed; not part of `tokens`.
  Always rendered with a leading `+` to mark it as a projection.
- `what` — first text line the assistant produced, or a collapsed tool
  list (`Read foo.py, Bash "pytest"`, `[3× Agent] e.g. "codegen-analyze detail-1"`).
  Turn rows get dot-leader padding so the eye can track across the row.
- `ctx` — context window size at this turn (raw `input + cache_r + cache_w`).
- `mdl`, `in`, `out`, `cache_r`, `cache_w` — raw transcript counts to
  the right of the `│` separator. These are what the API reported,
  without normalization.
- `(startup)` label on the first turn of each section signals that its
  `own` / `carry` / `caused` numbers include session bootstrap overhead
  (system prompt, tool schemas, CLAUDE.md, initial skill loads) that
  isn't purely developer-actionable.

Each section's header decomposes the total as
`Tokens N (A own + B subagents)` when subagents contributed, or just
`Tokens N` for a leaf. Subagent tables repeat the same column layout
scoped to that subagent's own turns, with a `parent turn N` cross-reference
back to the spawning row in the main table.

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
  `input_tokens * 0.1 * model_scale * remaining_turns_in_epoch`. This assumes
  the result stays in context as cached content for all subsequent assistant
  turns until either the session ends or a `/compact` boundary replaces it
  with a summary.

Both are approximations. The actual cost depends on caching behavior, context
window management, and prompt structure. Use them for relative comparisons
(which tools are expensive?) rather than absolute numbers. The per-turn
`caused` column uses the same flat-attribution formula at turn granularity.

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
