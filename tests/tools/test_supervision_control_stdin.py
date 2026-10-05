"""Supervisor control traffic cannot consume the gateway's command channel."""

import os
import signal
import subprocess
import sys

import pytest


@pytest.mark.platforms("posix")
def test_supervisor_supplies_eof_without_consuming_parent_stdin():
    code = '''
import subprocess
from tools.environments.systemd_jobs import SystemdJobSupervisor

def execute(script, stdin=None):
    return subprocess.run(["bash", "-c", script], input=stdin,
                          capture_output=True, text=True, timeout=10)

supervisor = SystemdJobSupervisor(execute, "/unused")
assert supervisor._run("cat; printf 'control-complete'") == "control-complete"
assert supervisor._run("cat", "explicit-input") == "explicit-input"
print("complete", flush=True)
'''
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    try:
        # Keep the upstream channel open, as it is in a VM backdoor or gateway.
        child.stdin.write("upstream-command\n")
        child.stdin.flush()
        child.wait(timeout=5)
        assert child.returncode == 0, child.stderr.read()
        assert child.stdout.read() == "complete\n"
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()
