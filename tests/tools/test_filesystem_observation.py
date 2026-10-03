"""Slow authority responses still provide one coherent process observation."""

import base64
import json
from types import SimpleNamespace
import subprocess
import threading

import pytest

from tools.environments.filesystem_supervisor import FilesystemSupervisor
from tools.environments.job_supervision import JobReceipt, JobState
from tools.environments.supervised_execution import SupervisedProcessHandle


def test_slow_authority_response_keeps_exit_state_and_output_coherent(monkeypatch):
    from tools.environments import filesystem_supervisor

    clock = [0.0]
    monkeypatch.setattr(filesystem_supervisor, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    observations = iter([
        {"state": "running", "exit_code": None, "output": base64.b64encode(b"before\n").decode()},
        {"state": "settled", "exit_code": 7, "output": base64.b64encode(b"after\n").decode()},
    ])

    def call(payload):
        clock[0] += 2  # A response can take longer than the reuse window.
        return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "result": next(observations)}))

    supervisor = FilesystemSupervisor(call, {
        "authority": "fixture", "principal": "controller", "request": "run", "fingerprint": "bound",
    }, {})
    job = JobReceipt("a" * 32)
    assert supervisor.inspect(job) is JobState.RUNNING
    assert supervisor.main_exit_code(job) is None
    assert supervisor.read_output(job, 0) == b"before\n"
    clock[0] += 1
    assert supervisor.inspect(job) is JobState.SETTLED
    assert supervisor.main_exit_code(job) == 7
    assert supervisor.read_output(job, 0) == b"after\n"


@pytest.mark.parametrize("size", [7, 65536, 65543])
def test_handle_drains_bounded_output_without_replaying_offset_zero(tmp_path, size):
    content = (b"exact output\n" * (size // 13 + 1))[:size]
    output = tmp_path / "output"
    output.write_bytes(content)
    offsets = []

    def call(payload):
        request = json.loads(payload)
        assert request["op"] == "observe"
        offset = request["offset"]
        offsets.append(offset)
        with output.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read(65536)
        return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "result": {
            "state": "settled", "exit_code": 7, "output": base64.b64encode(chunk).decode(),
        }}))

    supervisor = FilesystemSupervisor(call, {
        "authority": "fixture", "principal": "controller", "request": "run", "fingerprint": "bound",
    }, {})
    handle = SupervisedProcessHandle(supervisor, JobReceipt("a" * 32))
    # Drain concurrently: payloads can exceed the OS pipe capacity.
    received = handle.stdout.read()
    assert handle.wait(timeout=5) == 7
    handle.stdout.close()
    assert received.encode() == content
    assert offsets == ([0] if size < 65536 else [0, 65536])


def test_descendant_wait_holds_through_unknown_and_main_exit():
    unknown_observed = threading.Event()
    allow_settlement = threading.Event()

    def call(payload):
        request = json.loads(payload)
        if allow_settlement.is_set():
            state, code = "settled", 9
        elif not unknown_observed.is_set():
            state, code = "unknown", None
            unknown_observed.set()
        else:
            state, code = "running", 9  # Main shell exited; its descendant still owns the cgroup.
        return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "result": {
            "state": state, "exit_code": code, "output": "",
        }}))

    supervisor = FilesystemSupervisor(call, {
        "authority": "fixture", "principal": "controller", "request": "run", "fingerprint": "bound",
    }, {})
    handle = SupervisedProcessHandle(supervisor, JobReceipt("b" * 32), wait_for_descendants=True)
    try:
        assert unknown_observed.wait(5)
        with pytest.raises(subprocess.TimeoutExpired):
            handle.wait(timeout=.3)
        assert handle.poll() is None
    finally:
        allow_settlement.set()
        handle.wait(timeout=5)
        handle.stdout.close()
    assert handle.returncode == 9


def test_streaming_job_is_not_blocked_by_another_jobs_cached_probe():
    probe_started = threading.Event()
    release_probe = threading.Event()
    first_job = JobReceipt("a" * 32)
    second_job = JobReceipt("b" * 32)

    def call(payload):
        request = json.loads(payload)
        if request["job"] == first_job.id:
            probe_started.set()
            assert release_probe.wait(5)
        return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "result": {
            "state": "settled", "exit_code": 0, "output": "",
        }}))

    supervisor = FilesystemSupervisor(call, {
        "authority": "fixture", "principal": "controller", "request": "run", "fingerprint": "bound",
    }, {})
    probe = threading.Thread(target=supervisor.main_exit_code, args=(first_job,))
    probe.start()
    handle = None
    try:
        assert probe_started.wait(5)
        handle = SupervisedProcessHandle(supervisor, second_job)
        assert handle.wait(timeout=1) == 0
        assert not release_probe.is_set()
    finally:
        release_probe.set()
        probe.join(5)
        if handle is not None:
            handle.wait(timeout=5)
            handle.stdout.close()
