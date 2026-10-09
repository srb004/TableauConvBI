"""
chat_history.py

Local persistence for saved conversations. Each saved conversation is one
JSON file under `saved_chats/` (gitignored -- this is local, per-user data,
not project source). `app.py` auto-saves after every turn (see its
`current_session_id`), so one conversation stays one continuously-updated
file rather than accumulating a new file per turn. This module never
touches `streamlit`, Tableau, or the LLM.

A saved file's shape:
    {
        "saved_at": "2026-10-09T14:32:05",   # local time, isoformat -- last
                                              # write, not first ("saved"
                                              # really means "last synced")
        "datasource": "FENIX_SEARCH ...",     # selected_datasource as of
                                               # the most recent turn
        "messages": [...]                     # exact st.session_state["messages"]
    }
"""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

SAVED_CHATS_DIR = Path(__file__).resolve().parent / "saved_chats"


def _slugify(text, max_len=40):
    text = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return text[:max_len] or "chat"


def _first_user_question(messages):
    for m in messages:
        if m.get("role") == "user" and m.get("content"):
            return m["content"]
    return ""


def save_session(messages, datasource, session_id=None):
    """
    Save the current conversation to a local JSON file, auto-called after
    every turn so a conversation is never lost without an explicit save
    step.

    Args:
        messages: the exact st.session_state["messages"] list (plain
            dicts/lists/str/float/None -- the same shape already used to
            re-render history, so it round-trips through JSON as-is).
        datasource: the data source name selected when this conversation
            happened (for display only -- each message already carries
            its own "datasource").
        session_id: None for a conversation's first save (a new file is
            created and its id returned); the id returned by a previous
            call for every later turn of the *same* conversation (that
            file is overwritten in place, so one conversation stays one
            file instead of growing a new file per turn).

    Returns:
        str | None: the session's id (its filename stem) -- `session_id`
        unchanged if it was given, or a newly-minted one otherwise. None
        if `messages` is empty (nothing worth saving; `session_id` is
        returned as-is in that case too).
    """
    if not messages:
        return session_id

    SAVED_CHATS_DIR.mkdir(parents=True, exist_ok=True)

    saved_at = datetime.now()
    if session_id is None:
        session_id = f"{saved_at.strftime('%Y%m%d-%H%M%S')}_{_slugify(_first_user_question(messages))}_{uuid.uuid4().hex[:6]}"
    path = SAVED_CHATS_DIR / f"{session_id}.json"

    payload = {
        "saved_at": saved_at.isoformat(timespec="seconds"),
        "datasource": datasource,
        "messages": messages,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return session_id


def list_sessions():
    """
    List saved conversations, newest first.

    Returns:
        list[dict]: [{"id": str, "saved_at": str, "datasource": str,
        "message_count": int, "preview": str}, ...]. A file that can't be
        read/parsed is skipped rather than raised -- one corrupt save
        shouldn't break the whole history view.
    """
    if not SAVED_CHATS_DIR.exists():
        return []

    sessions = []
    for path in SAVED_CHATS_DIR.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            messages = payload.get("messages") or []
            sessions.append({
                "id": path.stem,
                "saved_at": payload.get("saved_at") or "",
                "datasource": payload.get("datasource") or "",
                "message_count": len(messages),
                "preview": _first_user_question(messages),
            })
        except (OSError, ValueError):
            continue

    sessions.sort(key=lambda s: s["saved_at"], reverse=True)
    return sessions


def load_session(session_id):
    """
    Load one saved conversation.

    Returns:
        dict: {"saved_at": str, "datasource": str, "messages": list[dict]},
        or None if `session_id` doesn't exist / can't be parsed.
    """
    path = SAVED_CHATS_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def delete_session(session_id):
    """Delete one saved conversation. No-op if it doesn't exist."""
    path = SAVED_CHATS_DIR / f"{session_id}.json"
    path.unlink(missing_ok=True)
