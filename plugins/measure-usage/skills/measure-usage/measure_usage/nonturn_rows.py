"""Label extraction for non-assistant transcript entries.

The per-turn table surfaces non-assistant entries Claude Code writes to a
session JSONL — user prompts, interrupts, permission mode flips, hook
progress, attachments, and so on — as timeline rows alongside model
turns. Each kind of entry has a short builder that produces a labeled
row; unknown types fall through to a generic builder so anything new
that appears stays visible.

Some user entries are pure client-side wrappers (slash command
invocation tags, skill preambles, bash input/output shims, background
task notifications). The matching builder returns ``None`` for those
and the parser drops the row, keeping the timeline focused on real
events.
"""

import re

_PREVIEW_LEN = 70


def build_nonturn_label(entry, msg):
    """Return ``(kind, label)`` for a non-assistant transcript entry.

    ``kind`` is a short machine-friendly discriminator (``user``,
    ``permission-mode``, ``attachment``, etc.). ``label`` is the
    human-readable line to show in the ``what`` column. Returns
    ``None`` to indicate the entry should be filtered (Claude Code
    client-side shim with no UX value). Falls back to a bare type
    name for unknown shapes so unrecognized types still render.
    """
    etype = entry.get("type", "unknown")
    builder = _BUILDERS.get(etype, _build_generic)
    return builder(entry, msg)


# ---------------------------------------------------------------------------
# Per-type builders
# ---------------------------------------------------------------------------

def _build_user(entry, msg):
    """User messages: genuine text, shims, interrupts, or tool_result wrappers.

    Returns ``None`` for client-side shim wrappers that have no UX
    value in a usage timeline:

      - skill invocation preambles (``Base directory for this skill:``),
      - background task notifications (``<task-notification>``),
      - bash input/output shims (``<bash-input>`` etc.),
      - local-command output (``<local-command-stdout>``).

    Slash command invocations get a special compact rendering rather
    than being dropped — the user typing ``/foo`` is a real action
    worth seeing in the timeline.
    """
    content = msg.get("content", "")
    text = _extract_user_text(content)

    if text:
        if text.startswith("[Request interrupted"):
            return ("interrupt", f"[interrupt] {text}")

        slash = _try_slash_command(text)
        if slash is not None:
            return slash

        if _is_shim_text(text):
            return None

        return ("user", f"[user] {_preview(text)}")

    if isinstance(content, list):
        # All-tool_result entries are dropped earlier in the parser
        # via _is_tool_result_only_user_entry; this branch handles
        # malformed mixes where there's no text block at all.
        return None

    return ("user", "[user]")


def _extract_user_text(content):
    """Pull a unified text string from a user message's content payload.

    Handles both the string form and the list-of-blocks form. Joins
    multiple text blocks with newlines so shim detectors that look at
    a leading prefix still work when text is wrapped in a list.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", "") or "")
        return "\n".join(parts).strip()
    return ""


# Prefixes that mark a user message as a Claude Code client shim with
# no UX value. Matched against the message's leading characters after
# stripping whitespace.
_USER_SHIM_PREFIXES = (
    "Base directory for this skill:",
    "<task-notification>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)


def _is_shim_text(text):
    return any(text.startswith(prefix) for prefix in _USER_SHIM_PREFIXES)


_SLASH_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_SLASH_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def _try_slash_command(text):
    """Detect ``<command-name>…</command-name>`` shims and render them
    as a compact ``[slash] /name args`` row.

    Returns ``None`` when the text isn't a slash-command wrapper, so
    the caller can fall through to other handling.
    """
    name_match = _SLASH_NAME_RE.search(text)
    if name_match is None:
        return None
    name = name_match.group(1).strip()
    args_match = _SLASH_ARGS_RE.search(text)
    args = args_match.group(1).strip() if args_match else ""
    label = f"[slash] {name}"
    if args:
        label += f" {args}"
    return ("slash-command", _truncate_label(label))


def _build_system(entry, msg):
    subtype = entry.get("subtype") or "?"
    kind = f"system:{subtype}"
    content = entry.get("content")
    if subtype == "turn_duration":
        return (kind, f"[system:turn_duration]")
    if subtype == "compact_boundary":
        meta = entry.get("compactMetadata", {}) or {}
        trig = meta.get("trigger", "")
        pre = meta.get("preTokens")
        parts = ["[system:compact_boundary]"]
        if trig:
            parts.append(trig)
        if pre:
            parts.append(f"preTokens {_fmt_short_k(pre)}")
        return (kind, " ".join(parts))
    if subtype == "api_error":
        err = entry.get("error") or ""
        return (kind, f"[system:api_error] {_preview(str(err), 50)}")
    if isinstance(content, str) and content:
        return (kind, f"[{kind}] {_preview(content)}")
    return (kind, f"[{kind}]")


def _build_permission_mode(entry, msg):
    mode = entry.get("permissionMode", "?")
    return ("permission-mode", f"[permission-mode] → {mode}")


def _build_attachment(entry, msg):
    att = entry.get("attachment") or {}
    atype = att.get("type", "")
    if atype == "deferred_tools_delta":
        added = att.get("addedNames") or []
        removed = att.get("removedNames") or []
        parts = []
        if added:
            parts.append(f"+{len(added)}")
        if removed:
            parts.append(f"-{len(removed)}")
        summary = " ".join(parts) or "noop"
        return (
            "attachment:deferred_tools_delta",
            f"[attachment:deferred_tools_delta] {summary}",
        )
    return (f"attachment:{atype}" if atype else "attachment",
            f"[attachment{':' + atype if atype else ''}]")


def _build_progress(entry, msg):
    data = entry.get("data") or {}
    dtype = data.get("type", "")
    hook_ev = data.get("hookEvent", "")
    hook_name = data.get("hookName", "")
    tool_id = (entry.get("toolUseID") or "")[:8]
    parts = ["[progress]"]
    if dtype:
        parts.append(dtype)
    if hook_name:
        parts.append(hook_name)
    elif hook_ev:
        parts.append(hook_ev)
    if tool_id:
        parts.append(f"({tool_id})")
    return ("progress", " ".join(parts))


def _build_queue_operation(entry, msg):
    op = entry.get("operation", "?")
    content = entry.get("content") or ""
    return (
        f"queue-operation:{op}",
        f"[queue-operation:{op}] {_preview(content)}".rstrip(),
    )


def _build_file_history_snapshot(entry, msg):
    is_update = entry.get("isSnapshotUpdate")
    backups = ((entry.get("snapshot") or {}).get("trackedFileBackups") or {})
    count = len(backups)
    tag = "update" if is_update else "base"
    suffix = f" ({count} tracked)" if count else ""
    return ("file-history-snapshot", f"[file-history-snapshot] {tag}{suffix}")


def _build_last_prompt(entry, msg):
    return ("last-prompt", f"[last-prompt] {_preview(entry.get('lastPrompt') or '')}")


def _build_custom_title(entry, msg):
    return ("custom-title", f"[custom-title] {entry.get('customTitle', '')}")


def _build_agent_name(entry, msg):
    return ("agent-name", f"[agent-name] {entry.get('agentName', '')}")


def _build_pr_link(entry, msg):
    num = entry.get("prNumber", "")
    repo = entry.get("prRepository", "")
    return ("pr-link", f"[pr-link] {repo}#{num}".rstrip())


def _build_worktree_state(entry, msg):
    ws = entry.get("worktreeSession") or {}
    name = ws.get("worktreeName", "")
    return ("worktree-state", f"[worktree-state] {name}".rstrip())


def _build_generic(entry, msg):
    """Fallback for unrecognized entry types — still emit a row so
    unknown types are visible rather than silently dropped."""
    etype = entry.get("type", "unknown")
    return (etype, f"[{etype}]")


_BUILDERS = {
    "user": _build_user,
    "system": _build_system,
    "permission-mode": _build_permission_mode,
    "attachment": _build_attachment,
    "progress": _build_progress,
    "queue-operation": _build_queue_operation,
    "file-history-snapshot": _build_file_history_snapshot,
    "last-prompt": _build_last_prompt,
    "custom-title": _build_custom_title,
    "agent-name": _build_agent_name,
    "pr-link": _build_pr_link,
    "worktree-state": _build_worktree_state,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _preview(text, max_len=_PREVIEW_LEN):
    """Collapse whitespace and truncate a free-form text preview."""
    if not text:
        return ""
    one_line = " ".join(str(text).split())
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 1] + "…"


def _truncate_label(label, max_len=_PREVIEW_LEN + 10):
    """Truncate a fully-formed label without collapsing whitespace.

    Used by handlers (like the slash-command builder) that have
    already produced their final string and just need a length cap.
    """
    if len(label) <= max_len:
        return label
    return label[: max_len - 1] + "…"


def _fmt_short_k(n):
    """Format an integer compactly for labels: 1.5K, 170K, 2.3M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _short_tool_id(tool_use_id):
    """Return a short, distinctive suffix of a tool_use_id.

    Anthropic tool_use_ids look like ``toolu_01<hash>``. The ``toolu_01``
    prefix is constant across all ids so we strip it and return the
    first 8 chars of the distinctive part.
    """
    if not tool_use_id:
        return ""
    stripped = tool_use_id
    for prefix in ("toolu_01", "toolu_"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    return stripped[:8] or tool_use_id[:10]
