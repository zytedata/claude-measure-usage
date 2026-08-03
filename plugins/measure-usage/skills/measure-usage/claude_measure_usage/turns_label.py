"""Build human-readable labels for per-turn rows."""

_LABEL_MAX = 48
_SHORT_AGENT_ID_LEN = 4


def short_agent_id(path_or_id):
    """Extract a short id from an agent filename or path."""
    name = path_or_id
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.startswith("agent-"):
        name = name[len("agent-") :]
    if name.endswith(".jsonl"):
        name = name[: -len(".jsonl")]
    return name[:_SHORT_AGENT_ID_LEN]


def turn_label(row, max_len=_LABEL_MAX):
    """Build the 'what' column label for a per-turn row.

    Preference order:
      1. First text block's first line (what the user saw scroll past).
      2. A tool-calls list with Agent/Skill descriptions inlined and
         repeated tool names collapsed with an ×N suffix.
      3. Empty string.
    """
    text = (row.get("text_preview") or "").strip()
    if text:
        return _truncate(text, max_len)

    tool_calls = row.get("tool_calls") or []
    if not tool_calls:
        return ""

    return _format_tool_calls(tool_calls, max_len)


def _format_tool_calls(tool_calls, max_len):
    """Render a tool-call list with special cases for Agent/Skill fan-out."""
    agent_count = sum(1 for c in tool_calls if c["name"] == "Agent")
    skill_count = sum(1 for c in tool_calls if c["name"] == "Skill")

    # Parallel Agent fan-out: show count + first description as exemplar.
    if agent_count > 1 and agent_count == len(tool_calls):
        first_desc = tool_calls[0].get("input", {}).get("description", "")
        label = f"[{agent_count}× Agent]"
        if first_desc:
            label += f' e.g. "{first_desc}"'
        return _truncate(label, max_len)

    # Single Agent/Skill turn — render as its own bracketed form.
    if len(tool_calls) == 1 and agent_count == 1:
        desc = tool_calls[0].get("input", {}).get("description", "")
        return _truncate(f'[Agent] "{desc}"' if desc else "[Agent]", max_len)
    if len(tool_calls) == 1 and skill_count == 1:
        sk = tool_calls[0].get("input", {}).get("skill", "")
        return _truncate(f'[Skill] "{sk}"' if sk else "[Skill]", max_len)

    # Generic list: collapse repeats, keep first-seen order.
    fragments = []
    counts = {}
    order = []
    for call in tool_calls:
        frag = _tool_fragment(call)
        if frag not in counts:
            order.append(frag)
        counts[frag] = counts.get(frag, 0) + 1
    for frag in order:
        n = counts[frag]
        fragments.append(f"{frag} ×{n}" if n > 1 else frag)

    return _truncate(", ".join(fragments), max_len)


# ---------------------------------------------------------------------------
# Per-tool input extraction
# ---------------------------------------------------------------------------


def _tool_fragment(call):
    """Render a single tool call as a short label fragment."""
    name = call.get("name", "unknown")
    inp = call.get("input") or {}
    extractor = _TOOL_EXTRACTORS.get(name)
    if extractor is not None:
        return extractor(inp)
    return name


def _file_path_fragment(label):
    def _build(inp):
        path = inp.get("file_path") or ""
        return f"{label} {_basename(path)}" if path else label

    return _build


def _bash_fragment(inp):
    cmd = (inp.get("command") or "").strip().split("\n", 1)[0]
    cmd = _truncate(cmd, 30)
    return f'Bash "{cmd}"' if cmd else "Bash"


def _grep_fragment(inp):
    pat = _truncate(inp.get("pattern") or "", 20)
    return f'Grep "{pat}"' if pat else "Grep"


def _glob_fragment(inp):
    pat = inp.get("pattern") or ""
    return f'Glob "{pat}"' if pat else "Glob"


def _agent_fragment(inp):
    desc = inp.get("description", "")
    return f'Agent "{desc}"' if desc else "Agent"


def _skill_fragment(inp):
    sk = inp.get("skill", "")
    return f'Skill "{sk}"' if sk else "Skill"


def _web_fetch_fragment(inp):
    url = inp.get("url", "")
    return f"WebFetch {_domain(url)}" if url else "WebFetch"


def _web_search_fragment(inp):
    q = _truncate(inp.get("query") or "", 30)
    return f'WebSearch "{q}"' if q else "WebSearch"


def _tool_search_fragment(inp):
    q = _truncate(inp.get("query") or "", 30)
    return f'ToolSearch "{q}"' if q else "ToolSearch"


def _task_create_fragment(inp):
    subj = _truncate(inp.get("subject") or "", 40)
    return f'TaskCreate "{subj}"' if subj else "TaskCreate"


def _task_update_fragment(inp):
    tid = inp.get("taskId") or ""
    status = inp.get("status") or ""
    if tid and status:
        return f"TaskUpdate #{tid} → {status}"
    if tid:
        return f"TaskUpdate #{tid}"
    return "TaskUpdate"


def _task_simple_fragment(label):
    def _build(inp):
        tid = inp.get("taskId") or ""
        return f"{label} #{tid}" if tid else label

    return _build


def _send_message_fragment(inp):
    to = inp.get("to") or ""
    return f"SendMessage → {to}" if to else "SendMessage"


_TOOL_EXTRACTORS = {
    "Read": _file_path_fragment("Read"),
    "Edit": _file_path_fragment("Edit"),
    "Write": _file_path_fragment("Write"),
    "NotebookEdit": _file_path_fragment("NotebookEdit"),
    "Bash": _bash_fragment,
    "Grep": _grep_fragment,
    "Glob": _glob_fragment,
    "Agent": _agent_fragment,
    "Skill": _skill_fragment,
    "WebFetch": _web_fetch_fragment,
    "WebSearch": _web_search_fragment,
    "ToolSearch": _tool_search_fragment,
    "TaskCreate": _task_create_fragment,
    "TaskUpdate": _task_update_fragment,
    "TaskGet": _task_simple_fragment("TaskGet"),
    "TaskStop": _task_simple_fragment("TaskStop"),
    "TaskOutput": _task_simple_fragment("TaskOutput"),
    "SendMessage": _send_message_fragment,
}


def _domain(url):
    """Extract a short hostname for WebFetch labels."""
    if "://" in url:
        url = url.split("://", 1)[1]
    return url.split("/", 1)[0][:40]


def _basename(path):
    if not path:
        return ""
    return path.rsplit("/", 1)[-1]


def _truncate(s, max_len):
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"
