"""Diagnostic wrappers preserve real calls and reject payload-derived labels."""

from types import SimpleNamespace

import pytest

from tests._fixtures import qualification_diagnostics as diagnostics


def test_operation_metrics_preserve_return_exception_and_private_values(monkeypatch):
    monkeypatch.setattr(diagnostics, "_timings", {})
    monkeypatch.setattr(diagnostics, "_case_timings", {})
    monkeypatch.setattr(diagnostics, "_originals", [])
    ticks = iter([0.0, 2.0, 3.0, 5.0])
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    calls = []
    result = object()
    error = RuntimeError("original failure")

    class Transport:
        def request(self, op, **kwargs):
            calls.append((op, kwargs))
            if op != "observe":
                raise error
            return result

    original = Transport.request
    diagnostics._measure(Transport, "request", "authority-transport", operations=True)
    transport = Transport()
    assert transport.request("observe", payload="private") is result
    with pytest.raises(RuntimeError) as caught:
        transport.request("secret-not-a-label", payload="private")
    assert caught.value is error
    assert calls == [("observe", {"payload": "private"}), ("secret-not-a-label", {"payload": "private"})]
    assert set(diagnostics._timings) == {"authority-transport", "authority-transport:observe", "authority-transport:other"}
    assert diagnostics._timings["authority-transport"]["count"] == 2
    assert diagnostics._timings["authority-transport"]["totalSeconds"] == 4.0
    diagnostics.pytest_runtest_setup(None)
    assert not diagnostics._case_timings and diagnostics._timings
    diagnostics.pytest_sessionfinish(None, 0)
    assert Transport.request is original
