"""Trusted PID1 for a confined job; run with isolated Python, before workload env."""

import ctypes
import os
import signal
import subprocess
import sys


def main():
    if os.getpid() != 1:
        raise RuntimeError("confined job guardian must be PID1")
    # The workload shares our UID, but must not ptrace us or reopen the private
    # exit-receipt FD through /proc/1/fd. Children receive only stdout/input.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    if libc.prctl(4, 0, 0, 0, 0):  # PR_SET_DUMPABLE=0
        raise OSError(ctypes.get_errno(), "cannot protect job guardian")
    pidfd = None

    def forward(signum, _frame):
        if pidfd is not None:
            try:
                signal.pidfd_send_signal(pidfd, signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    child = subprocess.Popen(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout,
                             stderr=sys.stdout, close_fds=True)
    try:
        try:
            pidfd = os.pidfd_open(child.pid)
        except ProcessLookupError:
            pass  # The foreground already exited; wait still owns its receipt.
        code = child.wait()
        # systemd alone opens FD2 in the authority-owned control directory.
        # Never pass it to workload code. Persist foreground evidence without
        # confusing it with the lifetime of the complete namespace/cgroup.
        os.write(2, f"{code}\n".encode("ascii"))
        os.fsync(2)
        while True:
            try:
                os.waitpid(-1, 0)  # PID1 adopts and reaps detached descendants.
            except ChildProcessError:
                break
        return code if code >= 0 else 128 - code
    finally:
        if pidfd is not None:
            os.close(pidfd)


if __name__ == "__main__":
    sys.exit(main())
