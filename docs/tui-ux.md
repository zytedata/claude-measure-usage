# claude-usage-tui — UX design

This document is the interaction contract for the interactive TUI
shipped by the `claude-usage-tui` package. It describes what actually
ships: code comments and tests cite it as the authority for the
non-obvious rules (glued sort, filter glue, subagent accounting).
Ideas that didn't make v1 live in Deferred, explicitly out of scope
until they earn their place.

The TUI is a read-only debugger over Claude Code session transcripts
stored in `~/.claude/projects/`. It is not a monitor, not a live
tracker, and not a report generator.

User-facing documentation — install, screenshots, column semantics,
the cost model — lives in the README. This doc records the
interaction rules and the reasoning behind them.

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

## Architecture: three screens, stacked modals

```
project screen  ──↵──▶  session screen  ──↵──▶  session detail  ──↵──▶  row modal
     ◀──esc                  ◀──esc                    ◀──esc       (turn / payload)
                                                       │
                                                       ↵ on a ↳subagent row
                                                       ▼
                                                 session detail  (drill deeper, stack grows)
```

Every screen is a full-screen Textual `Screen`. The detail-screen
stack grows arbitrarily deep when drilling into nested subagents;
`esc` always pops exactly one level (modal → table → session list →
project list).

`←` / `→` are **not** bound as back/forward. Textual's DataTable
in row-cursor mode uses those keys to scroll horizontally when the
table overflows — which happens whenever the terminal is narrower
than the column set. Giving the same key two different meanings
depending on terminal width would be worse than a single canonical
back key, so we stay on `esc` alone.

### Screen 1 — project picker

Lists every directory under `~/.claude/projects/`, decoded back to
its original cwd, with columns `Project`, `Sessions`, `Last`
(relative last-activity time). Rows are ordered by last activity;
the cursor starts on the project matching the current working
directory.

`r` rescans the tree — filesystem-only, no transcript parsing, so
it's fast even for big trees.

A `Tokens` column was designed but deferred: filling it requires
parsing every transcript in every project, which would block the
initial render on cold caches (see Deferred).

### Screen 2 — session picker

Lists `*.jsonl` transcripts in the selected project with columns
`Started`, `Turns`, `Token usage`, `Peak ctx`, `Model`, `Summary`.
`Summary` is the first user message, truncated — the "what was this
session about?" anchor. `Token usage` is Sonnet input-equivalent,
noted inline in the screen's sub_title rather than a dedicated
legend row.

Transcripts parse in a background worker with a per-file progress
bar. Mounting with a blocking parse would look like a freeze on
projects with many or large sessions; the worker lets the screen
paint immediately and keeps input responsive — `esc` cancels the
worker and pops.

`i` opens the summary overlay for the highlighted session straight
from the picker — triage a long session list without drilling into
each candidate. `r` re-parses the project, preserving the cursor.

### Screen 3 — session detail

Every detail screen — main session or nested subagent — is rendered
by the same class. Only the header changes with depth: subagent
drill-ins get a breadcrumb chain (`Main → ↳a3f2 → ↳b8c1`). The
header line above the table carries the session-level totals, and
the sub_title shows the active sort mode plus the "cost columns are
Sonnet input-equivalent" unit note.

Table columns, in order:

`#`, `when`, `took`, `cost`, `own`, `carry`, `caused`, `what`,
`ctx`, `model`, `in`, `out`, `cache_r`, `cache_w`

The last five are the raw transcript counts, unnormalized.

`r` re-parses the transcript from disk, preserving the current sort
mode and filter text. On subagent drill-in screens it's a no-op —
those don't own a file path; their data comes from the parent's
agent tree, so "reload" is expressed by popping back and reloading
there.

## Row semantics

The detail screen's table has three row kinds:

### Turn rows

One per logical assistant turn. Sort-sensitive. `↵` → opens the
**turn detail modal**.

### Subagent rows

Shown as dimmed, indented footnotes under the turn that spawned
them. **Each subagent = exactly one row**, regardless of how many
internal turns or nested subagents it had. The table never recurses
inline — deeper levels are visited by drilling.

Columns on a subagent row:

- `#` shows `↳{short_id}` instead of a number.
- `when` / `took` show the invocation time (relative to the parent
  session's start, so it lines up with surrounding turn rows) and
  the subagent's wallclock span.
- `cost` shows the subtree total (already included in the parent
  turn's `own`/`cost` — see Accounting below).
- `own` / `carry` / `caused` are blank; those are per-turn concepts.
- `what` shows `[{tool}] "{description}" — {N} turns`.
- `ctx` shows the subagent's peak context.
- `model` shows the subagent's dominant model.

Row cells are rendered in a dimmed style so nobody mistakes a
footnote for an independent line item.

`↵` → pushes a new session detail screen for that subagent.

### Non-turn rows

Attachments, compact boundaries, user messages, slash-command
invocations, permission-mode changes, progress events. These come
from the transcript's non-turn timeline (`nonturn_rows.py`) and are
shown inline at their natural position so UX events read in order
with model turns.

`↵` → opens a **payload modal** showing the full contents
(full `allowedTools` list, full user message, full attachment body).
These are the things the text CLI truncates or collapses.

## Accounting (important)

**Parent turn's `own` and `cost` include the subagent subtree
rollup.** This is unchanged from the text CLI. Rationale: we want
sorting by `cost desc` to bubble up turns that were expensive
*because of* their subagents, not hide them behind self-cost.

Subagent footnote rows show each subagent's individual contribution
for visual accounting, but that contribution is already counted in
the parent — summing all rows naively would double-count.

The session-level totals shown in the detail header are computed
from the parse, not by summing table rows, so they stay reconciled
with the session total regardless of what rows are visible.

The **turn detail modal** shows the rigorous breakdown for any
parent turn: `cost = own + carry`, with `own` decomposed into
`self` (this turn's own work) and `subagents` (rolled up from
spawned children, itemized per child).

## Sort

`s` cycles through a fixed rotation; the active sort column gets a
`▼` indicator in its header label:

```
natural  →  took ▼  →  cost ▼  →  own ▼  →  carry ▼  →  caused ▼  →  natural
```

The cycle order follows the visual column order in the table.
Columns without a meaningful sort (`when`, `what`, `ctx`, `model`,
the raw counts) are skipped. Clicking a column header also sorts —
clicks on non-sortable columns are no-ops.

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

A scrollable overlay showing everything about one turn:

- the cost decomposition (`cost = own + carry`, `caused`
  projection, `own = self + subagents` itemized per child),
- the raw transcript counts,
- the full text preview,
- tool calls with their inputs,
- a **Spawned subagents** list when the turn has children.

`↵` on an entry in the subagent list drills into that subagent's
detail screen directly. The original design kept subagent
navigation exclusively in the main table, but drilling from the
modal saves the hop back out, and Enter-selects-row is consistent
with every other list widget in the app. Elsewhere in the modal,
`↵` closes it — the same key that opened it — as do `esc` and `q`.

## Non-turn payload modals

One modal renders all non-turn row types, dispatching on the row's
kind for structured per-type content:

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

Unknown kinds fall back to a pretty-printed JSON dump so nothing is
ever invisible. All dismissible with `esc`.

## Summary overlay

`i` — available on the session picker and every session detail
screen — shows the session-level summary: duration, token breakdown
by type, peak context, turn and user-message counts, per-tool cost
table, subagent tree rollup. It reuses the same
`compute_metrics_from_parsed` + `format_metrics` pipeline as the
`/measure-usage` skill's text output, so the TUI and the plain CLI
produce identical summaries — single source of truth.

The overlay also prints the absolute path to the session's `.jsonl`
transcript (`Transcript: …`) so a user can read it. It's shown for real
sessions but omitted on subagent drill-in summaries, whose data
comes from the parent's agent tree rather than a standalone file.

## Keybindings

| Key | Project | Sessions | Session detail |
|---|---|---|---|
| `↑` / `↓` | move cursor | move cursor | move cursor |
| `↵` | open project | open session | detail modal / drill into `↳` row |
| `esc` | — | back | back one level |
| `s` | — | — | cycle sort |
| `/` | — | — | filter |
| `i` | — | summary overlay | summary overlay |
| `r` | rescan tree | re-parse project | re-parse transcript |
| `q` | quit | quit | quit |
| `?` | help | help | help |

`?` opens a help overlay listing the current screen's bindings.

"Open" on the pickers is handled via `DataTable.RowSelected` rather
than a screen-level `enter` binding: DataTable installs its own
priority enter binding that fires that message, which would shadow
anything the screen defines.

No `d` (toggleable detail sidebar) — dropped because the modal
covers the same ground with less surface area. No `→` direct-drill
shortcut — `↵` handles it since the cursor can land on subagent
rows directly.

## Deferred (v2+)

- Project picker `Tokens` column, filled lazily per row.
- `g` goto-path prompt on the project picker, for directories
  outside `~/.claude/projects/`.
- Sort and filter on the project and session pickers.
- `⚠` flag on sessions whose peak context neared the compaction
  threshold.
- Filter mini-DSL (`tool:`, `>5k`, `model:opus`).
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
