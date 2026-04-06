"""CLI entry point for measure_usage."""

import json
import sys
import time
from datetime import datetime, timezone

from .display import format_metrics
from .metrics import compute_metrics
from .parse import find_transcript_path, parse_ts, _iter_transcript
from .state import (
    METRICS_FILE,
    load_state,
    save_state,
    remove_state,
    list_active_sessions,
    save_metrics_record,
)


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

    state = {
        "start_ts": time.time(),
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {
            "session_id": sid,
            "started_at": state["started_at"],
            "stopped_at": now_str,
            **metrics,
        }
        save_metrics_record(record)
        remove_state(sid)

        if len(active) > 1:
            print(f"Session: {sid}")
        print(format_metrics(metrics))

    print(f"\nSaved to {METRICS_FILE}")


def cmd_session(session_id):
    transcript_path = _resolve_transcript(session_id)

    start_ts = None
    started_at = None
    for entry in _iter_transcript(transcript_path):
        ts_str = entry.get("timestamp")
        if ts_str:
            start_ts = parse_ts(ts_str)
            started_at = ts_str
            break

    if start_ts is None:
        print("Empty or unreadable transcript.")
        return

    metrics = compute_metrics(transcript_path, start_ts)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    record = {
        "session_id": session_id,
        "started_at": started_at,
        "stopped_at": now_str,
        **metrics,
    }
    save_metrics_record(record)

    print(format_metrics(metrics))
    print(f"\nSaved to {METRICS_FILE}")


def main():
    if len(sys.argv) < 2:
        print("Usage: measure_usage <command> [session_id]")
        print("Commands: start, stats, stop, session")
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
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
