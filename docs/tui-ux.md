# claude-usage-tui — UX design

This document is the interaction contract for the interactive TUI
shipped by the `claude-usage-tui` PyPI package. It is an agreed
design, not an aspirational one — what's in v1 here is what v1
ships, and what's in Deferred is explicitly out of scope until it
earns its place.

The TUI is a read-only debugger over Claude Code session transcripts
stored in `~/.claude/projects/`. It is not a monitor, not a live
tracker, and not a report generator.

## Goals and non-goals

**Goals**

- Let a user find the expensive turn across projects, across sessions
  within a project, and within a specific session.
- Show the full, untruncated value of every data point the text CLI
  has to abbreviate — tool inputs, text previews, attachment payloads.
- Let a user drill from a parent session into any spawned subagent
  (recursively), with a one-keystroke back out.

**Non-goals (v1)**

- Live tailing of an in-progress session.
- Diffing two sessions or two turns.
- Writing anything: no state mutation, no exporting, no clipboard.
- Any kind of monitoring, alerting, or background polling.

## Architecture: three screens, one modal

```
project screen  ──↵──▶  session screen  ──↵──▶  session detail  ──↵──▶  turn detail modal
     ◀──esc/◀──                ◀──esc/◀──                       ◀──esc/◀──
                                                       │
                                                       ↵ on subagent row
                                                       ▼
                                                 session detail  (drill deeper, stack grows)
                                                       ◀──esc/◀──
```

Every screen is a full-screen Textual `Screen`. The detail-screen
stack grows arbitrarily deep when drilling into nested subagents;
`esc` / `←` always pops exactly one level.

### Screen 1 — project picker

Lists every directory under `~/.claude/projects/`, decoded back to
its original cwd, plus a user-typed "go to path" escape hatch for
directories outside that tree.

```
┌─ claude-usage-tui ─────────────────────────────────── 12 projects ─┐
│                                                                    │
│    Project                                Sessions  Tokens  Last   │
│  ▸ ~/svn/measure-usage                         18    2.4M  5m ago  │
│    ~/svn/scraping-agent-skills                 47   12.1M  1h ago  │
│    ~/svn/northstar2                            22    4.7M  3d ago  │
│    ~/svn/logic-ab-swift                         3     580K  2d ago │
│  ⋮                                                                  │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│  ↵ open  g goto path  s sort  / filter  r reload  q quit           │
└────────────────────────────────────────────────────────────────────┘
```

- `▸` marks the current working directory; cursor lands there by
  default. `Tokens` starts blank and fills in lazily per row as
  each project's sessions get parsed — scanning every transcript
  up front would be too slow.
- `g` opens a mini-prompt to type an arbitrary path.
- Default sort: `Last` (most recent activity first).

### Screen 2 — session picker

Lists `*.jsonl` files inside the selected project, with a preview
of each session's shape.

```
┌─ ~/svn/measure-usage ──────────────────── 18 sessions ─ sort: date ▼ ─┐
│                                                                       │
│    Started           Turns  Tokens  Peak ctx  Model   Summary         │
│  ▸ 2026-04-12 09:14      8   92.4K   28.9K    opus    Build claude-u…│
│    2026-04-11 22:30     42    1.1M   85.3K    opus    Add TUI plans…  │
│    2026-04-11 17:02      3     89K   12.1K    sonnet  Quick fix for…  │
│    2026-04-11 11:15  ⚠ 156    4.8M    174K    opus    Long debug sess…│
│  ⋮                                                                    │
│                                                                       │
├───────────────────────────────────────────────────────────────────────┤
│  ↵ open  esc back  s sort  / filter  r reload  q quit                 │
└───────────────────────────────────────────────────────────────────────┘
```

- `Summary` is the first user message, truncated. It's the
  "what was this session about?" anchor.
- `⚠` flags sessions whose peak context got close to the model's
  compaction threshold.

### Screen 3 — session detail

Every detail screen — main session or nested subagent — is
structurally identical. The only thing that changes with depth is
the header.

```
┌─ 2026-04-12 09:14  ~/svn/measure-usage ────── 8 turns · 92.4K tokens ──┐
│ Duration 3m11s  Peak ctx 28.9K  Model opus                              │
│ 14% own · 36% carry · 50% caused   Wallclock 1m57s   Tools: Bash·Skill  │
├─────────────────────────────────────────────────────────────────────────┤
│ #      when   took    tokens  own    carry  what                   ctx │
│ ─────  ────   ────    ──────  ───    ─────  ──────────────────  ────── │
│ 1      0:04          20.9K   18.4K   2.5K   (startup) [Skill] …  23K  │
│ 2      0:08   4s      7.8K    4.0K   3.9K   Starting Stage 1: d… 24K  │
│ 3      0:14   6s     12.0K    8.0K   4.0K   Bash "BASE=barnesan… 26K  │
│ 4      0:18   3s      5.8K    1.4K   4.4K   Folders ready. Now … 27K  │
│        0:27                                 [user] https://www… │
│ 5      0:30  1m34s  180.4K  176.2K   4.2K   Bash "uv run scrape" 27K  │
│  ↳a3f2 0:30  1m10s  180.0K                    [Agent] "investig… 52K  │
│  ↳b8c1 0:32    25s   45.2K                    [Skill] "scrape"   15K  │
│ 6      2:08   3s      7.0K    2.5K   4.5K   The site blocked b…  28K  │
│ ...                                                                    │
├─────────────────────────────────────────────────────────────────────────┤
│ ↵ details  s sort  / filter  r reload  esc back  q quit  ? help         │
└─────────────────────────────────────────────────────────────────────────┘
```

Drilling into a subagent (↵ on one of the `↳` rows) pushes a new
detail screen with the subagent's breadcrumb in its header:

```
┌─ ↳a3f2 "investigate blocker" · parent: ~/svn/measure-usage #5 · 7 turns · 180K ─┐
│ Main  →  ↳a3f2                                                                   │
│ Duration 1m10s  Peak ctx 52.1K  Model opus  spawned by: [Agent] at parent #5     │
├──────────────────────────────────────────────────────────────────────────────────┤
│ #   when  took  tokens  own   carry  what                                  ctx   │
│ ...                                                                              │
```

Depth 2, 3, 4+ all look the same. The breadcrumb grows
(`Main → ↳a3f2 → ↳b8c1 → ↳c7d5`).

## Row semantics

The detail screen's table has three row kinds:

### Turn rows

One per logical assistant turn. Sort-sensitive. Columns match the
text CLI's per-turn output exactly:

- `#`, `when`, `took`, `tokens`, `own`, `carry`, `caused`, `what`,
  `ctx`, `model`, `in`, `out`, `cache_r`, `cache_w`

`↵` → opens the **turn detail modal**.

### Subagent rows

Shown as dimmed, indented footnotes under the turn that spawned
them. **Each subagent = exactly one row**, regardless of how many
internal turns or nested subagents it had. The table never recurses
inline — deeper levels are visited by drilling.

Columns on a subagent row:

- `#` shows `↳{short_id}` instead of a number.
- `when` / `took` show the invocation time and the subagent's
  wallclock span.
- `tokens` shows the subtree total (already included in the parent
  turn's `own`/`tokens` — see accounting below).
- `own` / `carry` / `caused` are blank; those are per-turn concepts.
- `what` shows the invocation label + `{N}t` turn-count annotation.
- `ctx` shows the subagent's peak context.
- `model` shows the subagent's dominant model.

Visually: row cells are rendered in a dimmed style and the `tokens`
value is prefixed with `↳` so nobody mistakes it for an independent
line item.

`↵` → pushes a new session detail screen for that subagent.

### Non-turn rows

Attachments, compact boundaries, user messages, slash-command
invocations, permission-mode changes, progress events. These come
from the transcript's non-turn timeline (`nonturn_rows.py`) and are
shown inline at their natural position so UX events read in order
with model turns.

`↵` → opens a small **payload modal** showing the full contents
(full `allowedTools` list, full user message, full attachment body).
These are the things the text CLI truncates or collapses.

## Accounting (important)

**Parent turn's `own` and `tokens` include the subagent subtree
rollup.** This is unchanged from the text CLI. Rationale: we want
sorting by `tokens desc` to bubble up turns that were expensive
*because of* their subagents, not hide them behind self-cost.

Subagent footnote rows show each subagent's individual contribution
for visual accounting, but that contribution is already counted in
the parent — summing all rows naively would double-count.

The **totals row** at the bottom of the table sums turn rows only.
Subagent footnote rows do not contribute. This keeps the table
total reconciled with the session total.

The **turn detail modal** shows the rigorous breakdown for any
parent turn:

```
Tokens:   180.4K   (= own 176.2K + carry 4.2K · caused +1K)
            own decomposes as:
              self          2.9K   (this turn's own work)
              subagents   173.3K   (rolled up from spawned children)
                ↳a3f2    180.0K    [Agent] "investigate blocker"
                ↳b8c1     45.2K    [Skill] "scrape"
```

## Sort

`s` cycles through a fixed rotation shown in the header:

```
natural  →  tokens ▼  →  own ▼  →  took ▼  →  ctx ▼  →  natural
```

Sort is **glued**: it operates on turn rows; each subagent footnote
stays immediately after its spawning turn. Sort never reorders
subagents relative to their parent — the spawn relationship is a
debugging affordance and breaking it to hoist a cheap parent's
expensive subagent above unrelated turns makes the list unreadable.

Non-turn rows stay in natural transcript order regardless of sort
mode — they're structural anchors, not comparable data.

## Filter

`/` opens an input bar. As the user types, rows are filtered by
substring match on the `what` column. Match scope:

- **Turn row matches** → turn row and its subagent footnotes are
  both shown.
- **Subagent row matches** → subagent row and its parent turn are
  both shown (glue).
- **Non-turn row matches** → non-turn row and its immediate
  neighboring turn are both shown.

This preserves the "matched context is never orphaned" invariant,
with no tree expansion state to maintain.

v1 is substring-only. A mini-DSL (`tool:Bash`, `>5k`, `model:opus`)
is deferred to v2 — adds a tokenizer without proportional UX value.

## Turn detail modal

```
┌─ Turn 5  ·  0:30 +1m34s  ·  180.4K tokens ─────────────── [esc] ─┐
│                                                                   │
│ Model:    claude-opus-4-6                                         │
│ Tokens:   180.4K (= own 176.2K + carry 4.2K · caused +1K)         │
│            own decomposes as:                                     │
│              self          2.9K                                   │
│              subagents   173.3K                                   │
│ Raw:      in 6   out 312   cache_r 26.9K   cache_w 115   ctx 27K  │
│                                                                   │
│ Text preview ───────────────────────────────────────────────────  │
│ Running the scraper to see how it handles a known-blocked site    │
│ before we try to work around the anti-bot measures.               │
│                                                                   │
│ Tool calls ─────────────────────────────────────────────────────  │
│ ▸ [1] Bash                                                        │
│     command: uv run /Users/kmike/svn/scraping-agent-skills/…      │
│     description: Run scraper against blocked page                 │
│                                                                   │
│                                                esc close          │
└───────────────────────────────────────────────────────────────────┘
```

Scrollable. Tool calls with long inputs have an expand toggle.
Contains no "Spawned subagents" section — that lives in the main
table as footnote rows.

## Non-turn detail modals

Each non-turn row type gets a targeted payload modal:

| Row type | Modal content |
|---|---|
| `attachment:command_permissions` | `allowedTools` list |
| `attachment:deferred_tools_delta` | Added + removed tool names |
| `attachment:skill_listing` | Full skill list (scrollable) |
| `user` message | Full user text |
| `slash` command | Command + arguments + payload |
| `system:compact_boundary` | Pre/post token counts, dropped entries |
| `permission-mode` | Old → new mode |
| `progress` | Hook name, tool id, full data dict |

All dismissible with `esc`.

## Keybindings (session detail)

| Key | Action |
|---|---|
| `↑` / `↓` | Move cursor |
| `↵` | Open detail modal for focused row; ↵ on a subagent row drills into its screen |
| `esc` / `←` | Back one level (modal → table → session list → project list) |
| `s` | Cycle sort |
| `/` | Filter |
| `r` | Reload transcript from disk |
| `q` | Quit |
| `?` | Help overlay |

No `d` (toggleable detail sidebar) — dropped because the modal
covers the same ground with less surface area. No `→` direct-drill
shortcut — `↵` handles it since the cursor can land on subagent
rows directly.

## v1 scope

- Three screens with navigation, back, reload.
- Project screen: `Sessions`, `Last`, lazy `Tokens`.
- Session screen: all columns.
- Session detail: turn rows + subagent footnote rows + non-turn
  rows; turn detail modal; non-turn payload modals.
- Glued sort cycle.
- Substring filter with glue rule.
- Recursive subagent drill-in via screen stack.

## Deferred (v2+)

- Filter mini-DSL (`tool:`, `>5k`, `model:opus`).
- Column-header click to sort (mouse).
- Live tailing of active sessions.
- Shift-cursor to skip between turn rows past subagent footnotes.
- Clipboard copy actions.
- Color theming / light mode.

## Code layout

The TUI lives in `claude_usage_tui/tui/` and imports shared data
from the top-level modules (`parse`, `metrics`, `turns_label`,
`nonturn_rows`). It must not import from `claude_usage_tui.plain`
and vice versa — the layering is enforced by a subprocess test
(`TestPlainDoesNotImportTextual`).

Row assembly is not shared with the plain text renderer. The two
views want different column sets and data shapes; `tui/` builds its
own row structures directly from the parser's output, calling the
shared cost primitives (`metrics.turn_own_seq`, `turn_inherit_seq`,
`compute_caused_by_turn`, `model_aware_cost_breakdown`) as needed.
