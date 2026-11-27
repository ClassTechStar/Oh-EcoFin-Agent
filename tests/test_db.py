"""DB 层：DDL/WAL/参数化查询/事件落库与重放。"""

from __future__ import annotations

import json


def test_create_and_query(tmp_db):
    tmp_db.insert_message("s1", "user", "你好", tokens=10)
    rows = tmp_db.query("SELECT role, content, tokens FROM messages WHERE session_id=?", ("s1",))
    assert len(rows) == 1
    assert rows[0]["role"] == "user"
    assert rows[0]["tokens"] == 10


def test_wal_mode(tmp_db):
    row = tmp_db.query_one("PRAGMA journal_mode")
    assert row["journal_mode"].lower() == "wal"


def test_tool_call_roundtrip(tmp_db):
    tmp_db.insert_tool_call("s1", "get_quote", {"symbols": ["600519"]},
                            {"ok": True, "quotes": []}, 12, True, False)
    row = tmp_db.query_one("SELECT name, args_json, result_json, ok FROM tool_calls")
    assert row["name"] == "get_quote"
    assert json.loads(row["args_json"])["symbols"] == ["600519"]
    assert row["ok"] == 1


def test_llm_call_and_cost(tmp_db):
    tmp_db.insert_llm_call("s1", "glm-5.3-flash", "primary", "react",
                           100, 50, 20, 3, 800)
    assert tmp_db.cost_today_cents() == 3


def test_events_replay(tmp_db):
    for i in range(1, 6):
        tmp_db.append_event("s1", i, "message_delta", {"text": str(i)})
    tmp_db.append_event("s1", 3, "message_delta", {"text": "替换"})
    rows = tmp_db.load_events_after("s1", 2)
    assert [r["id"] for r in rows] == [3, 4, 5]
    assert rows[0]["payload"]["text"] == "替换"


def test_sessions_and_messages_order(tmp_db):
    tmp_db.insert_message("s2", "user", "a")
    tmp_db.insert_message("s2", "assistant", "b")
    rows = tmp_db.query("SELECT content FROM messages WHERE session_id='s2' ORDER BY id")
    assert [r["content"] for r in rows] == ["a", "b"]
