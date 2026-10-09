"""One target response binds cgroup state, main exit and a bounded output slice."""

import os
from pathlib import Path
import subprocess

import pytest

from tools.environments.job_supervision import JobReceipt, JobState, SupervisionError
from tools.environments.systemd_jobs import SystemdJobSupervisor


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("failure", [None, "lost-control", "missing-boot", "manager-error"])
@pytest.mark.parametrize("load,active,stopped,state,exit_code", [
    ("loaded", "inactive", False, JobState.SETTLED, 7),
    ("not-found", "inactive", True, JobState.SETTLED, -15),
    ("not-found", "inactive", False, JobState.UNKNOWN, None),
    ("loaded", "active", False, JobState.UNKNOWN, None),
])
def test_observation_uses_one_transport_and_keeps_unknown_owned(
    tmp_path, load, active, stopped, state, exit_code, failure,
):
    root = tmp_path / "worker state"
    fence = root / "fence"
    fence.mkdir(parents=True)
    (fence / "boot").write_bytes(Path("/proc/sys/kernel/random/boot_id").read_bytes())
    if failure == "missing-boot":
        (fence / "boot").unlink()
    job = JobReceipt("a" * 32)
    folder = root / f"job-{job.id}"
    folder.mkdir()
    content = b"prefix" + bytes(range(256)) * 300
    (folder / "output").write_bytes(content)
    if stopped:
        (fence / f"{job.id}.stopped").touch()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    manager = binaries / "systemctl"
    manager.write_text("#!/usr/bin/env bash\nprintf '%s\\n' "
                       + f"LoadState={load} ActiveState={active} SubState=dead ControlGroup= "
                       + "ExecMainCode=1 ExecMainStatus=7\n")
    if failure == "manager-error":
        manager.write_text("#!/usr/bin/env bash\nexit 1\n")
    manager.chmod(0o700)
    environment = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
    calls = []

    def execute(script, stdin=None):
        calls.append(script)
        if failure == "lost-control":
            return subprocess.CompletedProcess([], 1, "", "control disconnected")
        return subprocess.run(["bash", "--noprofile", "--norc", "-c", script],
                              input=stdin, env=environment, capture_output=True, text=True, timeout=10)

    supervisor = SystemdJobSupervisor(execute, str(root))
    if failure:
        with pytest.raises(SupervisionError):
            supervisor.observe(job, 6)
    else:
        observed_state, observed_code, output = supervisor.observe(job, 6)
        assert (observed_state, observed_code) == (state, exit_code)
        assert output == content[6:6 + 65536]
    assert len(calls) == 1
