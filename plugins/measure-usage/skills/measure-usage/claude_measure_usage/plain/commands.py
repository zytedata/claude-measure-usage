"""Text-mode CLI commands for the /measure-usage skill."""

import sys
import time
from datetime import datetime, timezone

from .display import format_metrics
from ..metrics import compute_metrics
from ..parse import (
    find_transcript_path,
    find_subagent_transcripts,
    build_agent_tree,
    parse_transcript,
    parse_ts,
    _iter_transcript,
)
from .state import (
    load_state,
    save_state,
    remove_state,
    list_active_sessions,
)
from .turns_table import render_turns_report


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _resolve_transcript(session_id):
    transcript = find_transcript_path(session_id)
    if transcript is None:
        print(f"Error: could not find transcript for session {session_id}")
        sys.exit(1)
    return transcript


def cmd_start(session_id):
    transcript_path = _resolve_transcript(session_id)
    state = load_state(session_id)
    if state is not None:
        metrics = compute_metrics(state["transcript_path"], state["start_ts"])
        print(f"Tracking since {state['started_at']}:\n")
        print(format_metrics(metrics))
        return

    # Anchor the tracking window to the transcript's current tail rather
    # than wallclock: tracking must be reproducible from transcript data
    # alone, and user intent is "from this point forward". A 1ms epsilon
    # places start_ts strictly after the latest observed entry so the
    # turn the user was just looking at is excluded from the window.
    # An empty transcript falls back to wallclock since there's nothing
    # else to anchor on.
    latest = _latest_transcript_ts(transcript_path)
    if latest is not None:
        start_ts = latest + 0.001
        started_at = datetime.fromtimestamp(start_ts, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    else:
        start_ts = time.time()
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    state = {
        "start_ts": start_ts,
        "started_at": started_at,
        "transcript_path": transcript_path,
    }
    save_state(session_id, state)
    print("Usage tracking started.")


def cmd_stats(session_id=None):
    if session_id:
        state = load_state(session_id)
        if state is None:
            print("Not tracking this session. Run /measure-usage to start.")
            return
        metrics = compute_metrics(state["transcript_path"], state["start_ts"])
        print(format_metrics(metrics))
        return

    sessions = list_active_sessions()
    if not sessions:
        print("Not currently tracking any sessions.")
        return
    for sid, state in sessions:
        print(f"Session: {sid}")
        print(f"Started: {state['started_at']}")
        metrics = compute_metrics(state["transcript_path"], state["start_ts"])
        print(format_metrics(metrics))
        print()


def cmd_stop(session_id=None):
    if session_id:
        state = load_state(session_id)
        if state is None:
            print("Not tracking this session. Nothing to stop.")
            return
        active = [(session_id, state)]
    else:
        active = list_active_sessions()
        if not active:
            print("Not currently tracking any sessions. Nothing to stop.")
            return

    for sid, state in active:
        metrics = compute_metrics(state["transcript_path"], state["start_ts"])
        remove_state(sid)

        if len(active) > 1:
            print(f"Session: {sid}")
        print(format_metrics(metrics))


def cmd_turns(session_id):
    """Render a per-turn token usage table for a session.

    Respects an active tracking window (``start``/``stop``) when one
    exists for the session — in that case the table starts from the
    tracked ``start_ts`` instead of the first transcript entry. Without
    tracking state, renders the whole session.
    """
    transcript_path = _resolve_transcript(session_id)

    state = load_state(session_id)
    if state is not None:
        start_ts = state["start_ts"]
    else:
        start_ts = _first_entry_ts(transcript_path)
    if start_ts is None:
        print("Empty or unreadable transcript.")
        return

    main_parsed = parse_transcript(transcript_path, start_ts)
    subagent_infos = find_subagent_transcripts(transcript_path, start_ts)
    tree = build_agent_tree(transcript_path, main_parsed, subagent_infos)

    print(render_turns_report(main_parsed, tree))


def _first_entry_ts(transcript_path):
    """Return the timestamp of the first entry in a transcript, or None."""
    for entry in _iter_transcript(transcript_path):
        ts_str = entry.get("timestamp")
        if ts_str:
            return parse_ts(ts_str)
    return None


def _latest_transcript_ts(transcript_path):
    """Return the max timestamp across all transcript entries, or None
    if the transcript is empty or missing."""
    max_ts = None
    for entry in _iter_transcript(transcript_path):
        ts_str = entry.get("timestamp")
        if not ts_str:
            continue
        ts = parse_ts(ts_str)
        if max_ts is None or ts > max_ts:
            max_ts = ts
    return max_ts


def cmd_session(session_id):
    transcript_path = _resolve_transcript(session_id)

    start_ts = _first_entry_ts(transcript_path)
    if start_ts is None:
        print("Empty or unreadable transcript.")
        return

    metrics = compute_metrics(transcript_path, start_ts)
    print(format_metrics(metrics))


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m claude_measure_usage.plain <command> [session_id]")
        print("Commands: start, stats, stop, session, turns")
        sys.exit(1)

    command = sys.argv[1]
    if command == "start":
        if len(sys.argv) < 3:
            print("Error: session_id required for start")
            sys.exit(1)
        cmd_start(sys.argv[2])
    elif command == "stats":
        cmd_stats(sys.argv[2] if len(sys.argv) > 2 else None)
    elif command == "stop":
        cmd_stop(sys.argv[2] if len(sys.argv) > 2 else None)
    elif command == "session":
        if len(sys.argv) < 3:
            print("Error: session_id required for session")
            sys.exit(1)
        cmd_session(sys.argv[2])
    elif command == "turns":
        if len(sys.argv) < 3:
            print("Error: session_id required for turns")
            sys.exit(1)
        cmd_turns(sys.argv[2])
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
