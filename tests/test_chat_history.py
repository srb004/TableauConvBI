"""Tests for chat_history.py's local save/list/load/delete behavior."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat_history


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_history, "SAVED_CHATS_DIR", tmp_path / "saved_chats")
    yield


def _messages():
    return [
        {"role": "user", "content": "What's the total Sales?"},
        {"role": "assistant", "content": "The total Sales is 42.", "dataframe": [{"SUM(Sales)": 42}]},
    ]


def test_save_returns_none_for_empty_conversation():
    assert chat_history.save_session([], "Superstore Datasource") is None
    assert chat_history.list_sessions() == []


def test_save_then_list_then_load_round_trips():
    session_id = chat_history.save_session(_messages(), "Superstore Datasource")
    assert session_id is not None

    sessions = chat_history.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["id"] == session_id
    assert sessions[0]["datasource"] == "Superstore Datasource"
    assert sessions[0]["message_count"] == 2
    assert sessions[0]["preview"] == "What's the total Sales?"

    loaded = chat_history.load_session(session_id)
    assert loaded["datasource"] == "Superstore Datasource"
    assert loaded["messages"] == _messages()


def test_list_sessions_sorted_newest_first():
    first = chat_history.save_session(_messages(), "A")
    # Force a distinguishable, later timestamp without relying on real
    # wall-clock delay between the two saves.
    path = chat_history.SAVED_CHATS_DIR / f"{first}.json"
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["saved_at"] = "2020-01-01T00:00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")

    second = chat_history.save_session(_messages(), "B")

    sessions = chat_history.list_sessions()
    assert [s["id"] for s in sessions] == [second, first]


def test_save_with_session_id_overwrites_the_same_file():
    first_turn = [_messages()[0], _messages()[1]]
    session_id = chat_history.save_session(first_turn, "Superstore Datasource")

    second_turn = first_turn + [
        {"role": "user", "content": "And by Category?"},
        {"role": "assistant", "content": "Here's the breakdown.", "dataframe": [{"Category": "Tech", "SUM(Sales)": 1}]},
    ]
    returned_id = chat_history.save_session(second_turn, "Superstore Datasource", session_id)

    assert returned_id == session_id
    sessions = chat_history.list_sessions()
    assert len(sessions) == 1  # still one file, not two
    assert sessions[0]["message_count"] == 4
    assert chat_history.load_session(session_id)["messages"] == second_turn


def test_save_with_session_id_and_no_messages_keeps_id_unchanged():
    assert chat_history.save_session([], "Superstore Datasource", "some-existing-id") == "some-existing-id"


def test_delete_session_removes_it():
    session_id = chat_history.save_session(_messages(), "Superstore Datasource")
    chat_history.delete_session(session_id)
    assert chat_history.list_sessions() == []
    assert chat_history.load_session(session_id) is None


def test_delete_nonexistent_session_is_a_noop():
    chat_history.delete_session("does-not-exist")  # must not raise


def test_load_nonexistent_session_returns_none():
    assert chat_history.load_session("does-not-exist") is None
