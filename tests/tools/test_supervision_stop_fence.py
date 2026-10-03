"""Stop fences every admitted job before waiting on the execution manager."""

import os
from pathlib import Path
import subprocess

import pytest

from tools.environments.job_supervision import JobState, SupervisionError
from tools.environments.systemd_jobs import SystemdJobSupervisor


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("reset_unavailable", [False, True])
@pytest.mark.parametrize("fence_unavailable", [False, True])
def test_stop_fences_all_jobs_and_requires_each_settlement(tmp_path, uncertain, reset_unavailable, fence_unavailable):
    root = tmp_path / "worker state"
    fence = root / "fence"
    fence.mkdir(parents=True)
    (fence / "boot").write_bytes(Path("/proc/sys/kernel/random/boot_id").read_bytes())
    ids = [f"{value:032x}" for value in range(1, 36)]
    for job in ids:
        (root / f"job-{job}").mkdir()
    if uncertain:
        (root / "uncertain").write_text(ids[-1])
    if fence_unavailable:
        # Already empty units cannot substitute for the durable launch fence.
        for job in ids:
            (root / f"{job}.stopped").touch()
    manager = tmp_path / "bin"
    manager.mkdir()
    systemctl = manager / "systemctl"
    systemctl.write_text('''#!/usr/bin/env bash
set -eu
shift # --user
operation=$1; shift
case "$operation" in
  stop)
    # A manager may wait for a slow job. No other admission may remain unfenced
    # by the time that first potentially blocking control operation starts.
    for job in "$FIXTURE_ROOT"/job-*; do
      test -f "$FIXTURE_ROOT/fence/${job##*/job-}.stopped" || exit 1
    done
    for unit in "$@"; do
      id=${unit##*-}; id=${id%.service}
      touch "$FIXTURE_ROOT/$id.stopped"
    done
    ;;
  show)
    id=${1##*-}; id=${id%.service}
    printf 'LoadState=loaded\nControlGroup=\n'
    if test -f "$FIXTURE_ROOT/$id.stopped" &&
       { test ! -f "$FIXTURE_ROOT/uncertain" || test "$(cat "$FIXTURE_ROOT/uncertain")" != "$id"; }; then
      printf 'ActiveState=inactive\nSubState=dead\n'
    else
      printf 'ActiveState=deactivating\nSubState=stop-sigterm\n'
    fi
    ;;
  reset-failed) test "$FIXTURE_RESET_UNAVAILABLE" = 0 ;;
  *) exit 1 ;;
esac
''')
    systemctl.chmod(0o700)
    environment = {**os.environ, "FIXTURE_ROOT": str(root),
                   "FIXTURE_RESET_UNAVAILABLE": "1" if reset_unavailable else "0",
                   "PATH": str(manager) + os.pathsep + os.environ["PATH"]}

    control_calls = []
    def execute(script, stdin=None):
        control_calls.append(script)
        if fence_unavailable and "flock -x" in script and " stop " in script:
            return subprocess.CompletedProcess([], 1, "", "fence write unavailable")
        return subprocess.run(["bash", "--noprofile", "--norc", "-c", script],
                              input=stdin, env=environment, capture_output=True, text=True, timeout=10)

    supervisor = SystemdJobSupervisor(execute, str(root))
    if uncertain or fence_unavailable:
        message = "command failed" if fence_unavailable else "has not settled"
        with pytest.raises(SupervisionError, match=message):
            supervisor.stop()
        assert supervisor.settled() == (not uncertain)
        expected = JobState.UNKNOWN if uncertain else JobState.SETTLED
        assert supervisor.inspect(supervisor.jobs()[-1]) is expected
    else:
        supervisor.stop()
        # A run may have many short staging jobs. Its Stop proof must not pay
        # a new SSH/control-channel round trip for every retained receipt.
        assert len(control_calls) <= 6
        assert supervisor.settled()
    assert (fence / "sealed").exists()
    assert all((fence / f"{job}.stopped").exists() for job in ids) == (not fence_unavailable)
