"""SSE 事件协议：id 严格递增 / 环形缓冲 / 重放合并。"""

from __future__ import annotations

from backend.engine.events import SessionStream, StreamHub


def test_ids_strictly_increasing():
    s = SessionStream("t1", buffer_size=10)
    events = [s.publish("message_delta", {"text": "x"}) for _ in range(5)]
    ids = [e.id for e in events]
    assert ids == sorted(ids)
    assert len(set(ids)) == 5


def test_ring_buffer_eviction():
    s = SessionStream("t2", buffer_size=5)
    for i in range(10):
        s.publish("m", {"i": i})
    assert len(s._buffer) == 5
    assert s._buffer[-1].payload["i"] == 9


def test_replay_from_memory():
    s = SessionStream("t3", buffer_size=100)
    for i in range(8):
        s.publish("m", {"i": i})
    replayed = s.replay_from(5, [])
    assert [e.id for e in replayed] == [6, 7, 8]


def test_replay_merges_db_rows():
    s = SessionStream("t4", buffer_size=3)  # 缓冲小 → SQLite 兜底
    for i in range(6):
        s.publish("m", {"i": i})
    # 环形缓冲只剩 4,5,6；SQLite 兜底补上 0-3
    db_rows = [{"id": i, "event": "m", "payload": {"i": i}} for i in range(4)]
    replayed = s.replay_from(-1, db_rows)
    assert [e.id for e in replayed] == [0, 1, 2, 3, 4, 5, 6]
    assert replayed[0].payload["i"] == 0


def test_subscribe_broadcast_and_unsubscribe():
    s = SessionStream("t5")
    q = s.subscribe()
    ev = s.publish("m", {"x": 1})
    got = q.get_nowait()
    assert got.id == ev.id
    s.unsubscribe(q)
    s.publish("m", {"x": 2})
    assert q.qsize() == 0


def test_hub_session_isolation():
    hub = StreamHub()
    s1 = hub.get("a")
    s2 = hub.get("b")
    assert s1 is not s2
    assert hub.get("a") is s1


def test_sse_format():
    s = SessionStream("t6")
    ev = s.publish("chart", {"kind": "kline"})
    frame = ev.sse_format()
    assert frame.startswith(f"id: {ev.id}\nevent: chart\ndata: ")
    assert frame.endswith("\n\n")
