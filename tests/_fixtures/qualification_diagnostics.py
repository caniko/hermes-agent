"""Retain assertion and bounded control-latency diagnostics from the real suite."""

from functools import wraps
import json
import threading
import time

_lock = threading.Lock()
_timings = {}
_originals = []


def _record(label, seconds):
    with _lock:
        entry = _timings.setdefault(label, {"count": 0, "totalSeconds": 0.0, "maxSeconds": 0.0})
        entry["count"] += 1
        entry["totalSeconds"] += seconds
        entry["maxSeconds"] = max(entry["maxSeconds"], seconds)


def _measure(cls, name, label):
    original = getattr(cls, name)

    @wraps(original)
    def measured(self, *args, **kwargs):
        started = time.monotonic()
        try:
            return original(self, *args, **kwargs)
        finally:
            _record(label, time.monotonic() - started)

    setattr(cls, name, measured)
    _originals.append((cls, name, original))


def pytest_sessionstart(session):
    from tools.environments.filesystem_authority import FilesystemAuthority
    from tools.environments.filesystem_supervisor import FilesystemSupervisor
    from tools.environments.systemd_jobs import SystemdJobSupervisor

    # Every call still reaches the original control transport and authority.
    # Labels contain no commands, paths, payloads, credentials or claim IDs.
    _measure(SystemdJobSupervisor, "_run", "systemd-control")
    _measure(FilesystemSupervisor, "_request", "authority-transport")
    original = FilesystemAuthority.dispatch

    @wraps(original)
    def measured_dispatch(self, *args, **kwargs):
        started = time.monotonic()
        with self._lock:
            acquired = time.monotonic()
            _record("authority-lock-wait", acquired - started)
            try:
                return original(self, *args, **kwargs)
            finally:
                _record("authority-locked-dispatch", time.monotonic() - acquired)

    FilesystemAuthority.dispatch = measured_dispatch
    _originals.append((FilesystemAuthority, "dispatch", original))


def pytest_sessionfinish(session, exitstatus):
    with _lock:
        print("\nQUALIFICATION_CONTROL_TIMINGS " + json.dumps(_timings, sort_keys=True), flush=True)
    while _originals:
        cls, name, original = _originals.pop()
        setattr(cls, name, original)


def pytest_runtest_logreport(report):
    if report.failed:
        print(f"\nQUALIFICATION_FAILURE {report.nodeid} ({report.when})\n{report.longrepr}", flush=True)
