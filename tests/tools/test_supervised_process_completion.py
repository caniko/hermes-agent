"""A collected unit completes its stream only with durable target Stop proof."""

import os
from pathlib import Path
import subprocess

import pytest

from tools.environments.job_supervision import SupervisionError
from tools.environments.supervised_execution import SupervisedProcessHandle
from tools.environments.systemd_jobs import SystemdJobSupervisor


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("stopped", [False, True])
def test_collected_unit_requires_stop_proof_to_complete_its_handle(tmp_path, stopped):
    state = tmp_path / "state"
    fence = state / "fence"
    fence.mkdir(parents=True)
    (fence / "boot").write_bytes(Path("/proc/sys/kernel/random/boot_id").read_bytes())
    job_id = "a" * 32
    folder = state / f"job-{job_id}"
    folder.mkdir()
    (folder / "output").write_text("retained job output\n")
    if stopped:
        (fence / f"{job_id}.stopped").touch()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    manager = binaries / "systemctl"
    # systemctl show succeeds for an unloaded unit, with unset exit properties.
    # That is different from a transport failure and is not a running process.
    manager.write_text("#!/usr/bin/env bash\nprintf 'LoadState=not-found\\nActiveState=inactive\\nSubState=dead\\nControlGroup=\\nExecMainCode=0\\nExecMainStatus=0\\n'\n")
    manager.chmod(0o700)
    environment = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}

    def execute(script, stdin=None):
        return subprocess.run(["bash", "--noprofile", "--norc", "-c", script],
                              input=stdin, env=environment, capture_output=True, text=True, timeout=10)

    supervisor = SystemdJobSupervisor(execute, str(state))
    job = supervisor.jobs()[0]
    if not stopped:
        with pytest.raises(SupervisionError):
            supervisor.main_exit_code(job)
        return
    assert supervisor.main_exit_code(job) == -15
    handle = SupervisedProcessHandle(supervisor, job)
    assert handle.wait(timeout=5) == -15
    assert handle.stdout.read() == "retained job output\n"
    handle.stdout.close()
