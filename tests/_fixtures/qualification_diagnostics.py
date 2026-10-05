"""Retain assertion and bounded control-latency diagnostics from the real suite."""

from functools import wraps
import json
import threading
import time

_lock = threading.Lock()
_timings = {}
_case_timings = {}
_originals = []
_OPERATIONS = frozenset({"capabilities", "reserve", "stop", "seal", "release", "drain",
                         "status", "jobs", "start", "observe", "stop_job"})


def _record(label, seconds):
    with _lock:
        for timings in (_timings, _case_timings):
            entry = timings.setdefault(label, {"count": 0, "totalSeconds": 0.0, "maxSeconds": 0.0})
            entry["count"] += 1
            entry["totalSeconds"] += seconds
            entry["maxSeconds"] = max(entry["maxSeconds"], seconds)


def _measure(cls, name, label, *, operations=False):
    original = getattr(cls, name)

    @wraps(original)
    def measured(self, *args, **kwargs):
        started = time.monotonic()
        try:
            return original(self, *args, **kwargs)
        finally:
            elapsed = time.monotonic() - started
            _record(label, elapsed)
            if operations:
                op = args[0] if args else kwargs.get("op")
                # Never render arbitrary request values into diagnostic labels.
                op = op if isinstance(op, str) and op in _OPERATIONS else "other"
                _record(f"{label}:{op}", elapsed)

    setattr(cls, name, measured)
    _originals.append((cls, name, original))


def pytest_sessionstart(session):
    from tools.environments.filesystem_authority import FilesystemAuthority
    from tools.environments.filesystem_supervisor import FilesystemSupervisor
    from tools.environments.systemd_jobs import SystemdJobSupervisor

    # Every call still reaches the original control transport and authority.
    # Labels contain no commands, paths, payloads, credentials or claim IDs.
    _measure(SystemdJobSupervisor, "_run", "systemd-control")
    _measure(FilesystemSupervisor, "_request", "authority-transport", operations=True)
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
    if report.when == "call" and report.nodeid.startswith("tests/gateway/test_api_server_filesystem_ownership.py::"):
        with _lock:
            snapshot = json.dumps(_case_timings, sort_keys=True)
        print(f"\nQUALIFICATION_OWNERSHIP_TIMINGS {report.nodeid} {snapshot}", flush=True)
    if report.failed:
        print(f"\nQUALIFICATION_FAILURE {report.nodeid} ({report.when})\n{report.longrepr}", flush=True)


def pytest_runtest_setup(item):
    with _lock:
        _case_timings.clear()
