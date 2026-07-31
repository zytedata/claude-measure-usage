"""Session state management."""

import json
import os
from pathlib import Path

STATE_DIR = ".measure-usage"
SESSIONS_DIR = os.path.join(STATE_DIR, "sessions")


def state_path(session_id):
    return os.path.join(SESSIONS_DIR, f"{session_id}.json")


def load_state(session_id):
    path = state_path(session_id)
    if not os.path.exists(path):
        return None
    return json.loads(Path(path).read_text())


def save_state(session_id, state):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    Path(state_path(session_id)).write_text(json.dumps(state))


def remove_state(session_id):
    path = state_path(session_id)
    if os.path.exists(path):
        os.remove(path)


def list_active_sessions():
    if not os.path.exists(SESSIONS_DIR):
        return []
    sessions = []
    for p in Path(SESSIONS_DIR).glob("*.json"):
        try:
            state = json.loads(p.read_text())
            sessions.append((p.stem, state))
        except (OSError, json.JSONDecodeError):
            continue
    return sessions
