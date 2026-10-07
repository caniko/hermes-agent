"""Tests for tui_gateway.event_replay — per-session event seq + replay ring."""

import threading

import pytest

from tui_gateway import event_replay
from tui_gateway.event_replay import (
    latest_seq,
    reset_replay_state,
    events_since,
    replay_stats,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_replay_state()
    yield
    reset_replay_state()


def _frame(sid, etype="message.delta"):
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": etype, "session_id": sid, "payload": {}},
    }


def test_stamp_adds_monotonic_seq_per_session():
    f1 = _frame("s1")
    f2 = _frame("s1")
    other = _frame("s2")

    event_replay._stamp_event(f1)
    event_replay._stamp_event(other)
    event_replay._stamp_event(f2)

    assert f1["params"]["seq"] == 1
    assert f2["params"]["seq"] == 2  # per-session counter, unaffected by s2
    assert other["params"]["seq"] == 1


def test_stamp_ignores_non_event_and_sessionless_frames():
    rpc = {"jsonrpc": "2.0", "id": 1, "result": {}}
    no_sid = {"jsonrpc": "2.0", "method": "event", "params": {"type": "skin.changed"}}

    event_replay._stamp_event(rpc)
    event_replay._stamp_event(no_sid)

    assert "seq" not in rpc
    assert "seq" not in no_sid["params"]
    assert replay_stats()["events"] == 0


def test_events_since_returns_only_newer_frames_in_order():
    frames = [_frame("s1") for _ in range(5)]
    for f in frames:
        event_replay._stamp_event(f)

    got = events_since("s1", 3)
    assert [e["seq"] for e in got] == [4, 5]
    assert events_since("s1", 0) == [f["params"] for f in frames]
    assert events_since("s1", 99) == []
    assert latest_seq("s1") == 5


def test_events_since_returns_client_dispatchable_event_objects():
    """Cross-language contract: the client's replay loop dispatches an element
    only when it has a TOP-LEVEL ``type`` (json-rpc-gateway.ts fetchReplay:
    ``if (!event?.type) continue``). Returning full JSON-RPC envelopes here
    makes every replayed event silently droppable — the original #94219 bug.
    """
    event_replay._stamp_event(_frame("s1"))
    (event,) = events_since("s1", 0)

    # Bare event object, not an envelope.
    assert event["type"] == "message.delta"
    assert event["session_id"] == "s1"
    assert event["seq"] == 1
    assert "jsonrpc" not in event
    assert "method" not in event
    assert "params" not in event


def test_unknown_session_returns_empty():
    assert events_since("nope", 0) == []
    assert latest_seq("nope") == 0


def test_ring_buffer_is_bounded():
    for i in range(event_replay._REPLAY_BUFFER_MAX + 50):
        event_replay._stamp_event(_frame("s1"))

    stats = replay_stats()
    assert stats["events"] == event_replay._REPLAY_BUFFER_MAX
    # Oldest evicted: last_seen=0 must report truncation via the RPC contract.
    buf = event_replay._replay_buffers["s1"]
    assert buf[0][0] > 1


def test_session_count_bounded_with_fifo_eviction():
    for i in range(event_replay._REPLAY_SESSIONS_MAX + 10):
        event_replay._stamp_event(_frame(f"s{i}"))

    stats = replay_stats()
    assert stats["sessions"] == event_replay._REPLAY_SESSIONS_MAX
    assert events_since("s0", 0) == []  # oldest session fully evicted
    assert latest_seq(f"s{event_replay._REPLAY_SESSIONS_MAX + 9}") == 1


def test_concurrent_stamping_never_drops_or_duplicates_seq():
    errors = []

    def worker(sid):
        try:
            seen = set()
            for _ in range(200):
                f = _frame(sid)
                event_replay._stamp_event(f)
                seq = f["params"]["seq"]
                assert seq not in seen
                seen.add(seq)
        except AssertionError as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert replay_stats()["events"] == 8 * 200


def test_concurrent_publish_keeps_transport_and_replay_in_the_same_order(monkeypatch):
    from tui_gateway import server

    first_at_write = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    delivered = []
    errors = []

    class PausedTransport:
        def write(self, frame):
            seq = frame["params"]["seq"]
            if seq == 1:
                first_at_write.set()
                if not release_first.wait(5):
                    raise AssertionError("first writer was not released")
            delivered.append(seq)
            return True

    monkeypatch.setitem(server._sessions, "ordered", {"transport": PausedTransport()})

    def publish(done=None):
        try:
            assert server.write_json(_frame("ordered"))
        except BaseException as exc:
            errors.append(exc)
        finally:
            if done is not None:
                done.set()

    first = threading.Thread(target=publish)
    second = threading.Thread(target=publish, args=(second_done,))
    first.start()
    try:
        assert first_at_write.wait(5)
        second.start()
        # On the broken path seq 2 overtakes the paused seq 1. A serialized
        # publisher instead waits for the explicit release below.
        second_done.wait(2)
    finally:
        release_first.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert delivered == [event["seq"] for event in events_since("ordered", 0)] == [1, 2]


def test_failed_slow_writer_isolated_from_other_sessions_and_releases_publication(
    monkeypatch,
):
    from tui_gateway import server

    started = threading.Event()
    release = threading.Event()
    other_done = threading.Event()
    failures = []
    delivered = []

    class FailingTransport:
        def write(self, frame):
            started.set()
            assert release.wait(5)
            raise OSError("disconnected")

    class RecordingTransport:
        def write(self, frame):
            delivered.append((frame["params"]["session_id"], frame["params"]["seq"]))
            return True

    monkeypatch.setitem(server._sessions, "slow", {"transport": FailingTransport()})
    monkeypatch.setitem(server._sessions, "other", {"transport": RecordingTransport()})

    def publish(sid, done=None):
        try:
            server.write_json(_frame(sid))
        except BaseException as exc:
            failures.append(exc)
        finally:
            if done is not None:
                done.set()

    slow = threading.Thread(target=publish, args=("slow",))
    other = threading.Thread(target=publish, args=("other", other_done))
    slow.start()
    try:
        assert started.wait(5)
        other.start()
        assert other_done.wait(5), (
            "a slow session blocked another session's publication"
        )
    finally:
        release.set()
        slow.join(5)
        if other.ident is not None:
            other.join(5)

    assert not slow.is_alive() and not other.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], OSError)
    monkeypatch.setitem(server._sessions, "slow", {"transport": RecordingTransport()})
    assert server.write_json(_frame("slow"))
    assert delivered == [("other", 1), ("slow", 2)]


def test_byte_budget_evicts_payloads_and_preserves_gap_semantics(monkeypatch):
    monkeypatch.setattr(event_replay, "_REPLAY_BUFFER_BYTES_MAX", 700)
    monkeypatch.setattr(event_replay, "_REPLAY_PROCESS_BYTES_MAX", 5_000)

    first = _frame("s1", "tool.complete")
    first["params"]["payload"] = {"result": "x" * 400}
    second = _frame("s1", "tool.complete")
    second["params"]["payload"] = {"result": "y" * 400}
    event_replay._stamp_event(first)
    event_replay._stamp_event(second)

    stats = replay_stats()
    assert stats["bytes"] <= stats["max_bytes_per_session"]
    assert [event["seq"] for event in events_since("s1", 0)] == [2]
    assert event_replay.is_truncated("s1", 0)

    reset_replay_state()
    monkeypatch.setattr(event_replay, "_REPLAY_BUFFER_BYTES_MAX", 1_000)
    monkeypatch.setattr(event_replay, "_REPLAY_PROCESS_BYTES_MAX", 700)
    first = _frame("s1", "tool.complete")
    first["params"]["payload"] = {"result": "x" * 400}
    event_replay._stamp_event(first)
    other = _frame("s2", "tool.complete")
    other["params"]["payload"] = {"result": "y" * 400}
    event_replay._stamp_event(other)

    stats = replay_stats()
    assert stats["bytes"] <= stats["max_bytes_process"]
    assert events_since("s1", 0) == []
    assert event_replay.is_truncated("s1", 0)
    assert [event["seq"] for event in events_since("s2", 0)] == [1]


def test_oversized_event_marks_gap_even_with_empty_buffer(monkeypatch):
    monkeypatch.setattr(event_replay, "_REPLAY_BUFFER_BYTES_MAX", 300)
    monkeypatch.setattr(event_replay, "_REPLAY_PROCESS_BYTES_MAX", 300)

    oversized = _frame("s1", "tool.complete")
    oversized["params"]["payload"] = {"result": "x" * 1000}
    event_replay._stamp_event(oversized)

    assert events_since("s1", 0) == []
    assert event_replay.is_truncated("s1", 0)
    assert not event_replay.is_truncated("s1", 1)

    event_replay._stamp_event(_frame("s1"))
    assert [event["seq"] for event in events_since("s1", 0)] == [2]
    assert event_replay.is_truncated("s1", 0)
    assert not event_replay.is_truncated("s1", 1)
    assert not event_replay.is_truncated("s1", 2)

    # The gap watermark never moves backwards: a small frame after an oversized one must
    # not hide the hole the oversized frame left.
    reset_replay_state()
    monkeypatch.setattr(event_replay, "_REPLAY_BUFFER_MAX", 1)
    monkeypatch.setattr(event_replay, "_REPLAY_BUFFER_BYTES_MAX", 1000)
    monkeypatch.setattr(event_replay, "_REPLAY_PROCESS_BYTES_MAX", 1000)
    event_replay._stamp_event(_frame("s"))
    large = _frame("s")
    large["params"]["payload"] = {"data": "x" * 2000}
    event_replay._stamp_event(large)
    event_replay._stamp_event(_frame("s"))
    assert event_replay.is_truncated("s", 1)


def test_truncation_detection_semantics():
    """The RPC handler's truncated flag: gap between last_seen and buffer start."""
    # Overflow the ring so the oldest events are genuinely evicted.
    for _ in range(event_replay._REPLAY_BUFFER_MAX + 10):
        event_replay._stamp_event(_frame("s1"))

    with event_replay._replay_lock:
        oldest = event_replay._replay_buffers["s1"][0][0]

    assert oldest > 1  # eviction happened

    # Client saw everything up to just before the buffer → NOT truncated.
    assert not event_replay.is_truncated("s1", oldest - 1)
    # Client saw seq 5, buffer starts later → truncated.
    assert event_replay.is_truncated("s1", 5)
    # Unknown session: nothing evicted, nothing truncated.
    assert not event_replay.is_truncated("nope", 0)
