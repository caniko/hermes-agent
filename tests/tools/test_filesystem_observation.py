"""Slow authority responses still provide one coherent process observation."""

import base64
import json
from types import SimpleNamespace
import subprocess

from tools.environments.filesystem_supervisor import FilesystemSupervisor
from tools.environments.job_supervision import JobReceipt, JobState


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
