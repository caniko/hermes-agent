"""Bind a run's target supervisor to the existing terminal ProcessHandle seam."""

import os
import logging
import shlex
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from tools.environments.job_supervision import JobReceipt, JobState, SupervisionError, TargetJobSupervisor
from tools.environments.systemd_jobs import SystemdJobSupervisor


@dataclass
class SupervisionBinding:
    state_dir: str
    supervisor: TargetJobSupervisor | None = field(default=None, repr=False)
    handles: list = field(default_factory=list, compare=False, repr=False)
    kernels: list = field(default_factory=list, compare=False, repr=False)
    _workers: dict = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _sealed: bool = field(default=False, init=False)

    def submit(self, executor, fn, *args, interrupt=None):
        # Submission and registration share the settlement lock. A fast worker
        # (or its child submission) cannot disappear between the two.
        with self._lock:
            if self._sealed:
                raise SupervisionError("run worker admission is sealed")
            future = executor.submit(fn, *args)
            self._workers[future] = interrupt
            return future

    def workers_settled(self) -> bool:
        with self._lock:
            self._workers = {f: stop for f, stop in self._workers.items() if not f.done()}
            if self._workers:
                return False
            self._sealed = True
            return True

    def stop_workers(self) -> None:
        with self._lock:
            self._sealed = True
            callbacks = [stop for f, stop in self._workers.items() if not f.done() and stop is not None]
            self._workers = {f: None for f in self._workers}
        for stop in callbacks:
            try:
                stop()
            except Exception:
                logging.getLogger(__name__).exception("Could not interrupt an owned delegation worker")


_binding: ContextVar[SupervisionBinding | None] = ContextVar("target_job_supervision", default=None)


@contextmanager
def bind_job_supervision(binding: SupervisionBinding | None):
    token = _binding.set(binding)
    try:
        yield
    finally:
        _binding.reset(token)


def current_job_supervision() -> SupervisionBinding | None:
    return _binding.get()


def submit_owned_worker(executor, fn, *args, interrupt=None):
    binding = current_job_supervision()
    if binding is None:
        return executor.submit(fn, *args)
    return binding.submit(executor, fn, *args, interrupt=interrupt)


def local_supervisor(binding: SupervisionBinding, env: dict) -> TargetJobSupervisor:
    from tools.environments.local import _find_bash

    bash = _find_bash()
    def execute(script, stdin=None):
        return subprocess.run([bash, "--noprofile", "--norc", "-c", script], input=stdin,
                              env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    return SystemdJobSupervisor(execute, binding.state_dir)


def ssh_supervisor(binding: SupervisionBinding, env, values: dict) -> TargetJobSupervisor:
    from tools.environments.remote_common import client_env_with

    def execute(script, stdin=None):
        return subprocess.run(env._build_ssh_command(send_env=values) + [
            "bash --noprofile --norc -c " + shlex.quote(script)], input=stdin,
            env=client_env_with(values), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30)
    return SystemdJobSupervisor(execute, binding.state_dir)


class SupervisedProcessHandle:
    """Foreground completion follows the shell; run ownership follows its cgroup.

Background jobs set wait_for_descendants, so process-tool completion also waits
for their cgroup. Transport loss keeps the handle live and the receipt intact.
"""

    pid = None  # A target PID must never be mistaken for a controller PID.
    stdin = None
    stderr = None

    def __init__(self, supervisor: TargetJobSupervisor, job: JobReceipt, *, wait_for_descendants: bool = False):
        from agent.memory_provider import spawn_context_thread

        self.supervisor, self.job = supervisor, job
        self.wait_for_descendants = wait_for_descendants
        self.returncode = None
        self._done = threading.Event()
        read_fd, self._write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "r", encoding="utf-8-sig", errors="replace")
        if binding := current_job_supervision():
            binding.handles.append(self)
        spawn_context_thread(target=self._drain, daemon=True,
                             name=f"supervised-{job.id[:8]}").start()

    def _drain(self) -> None:
        offset = 0
        output_open = True
        try:
            while True:
                try:
                    state, code, chunk = self.supervisor.observe(self.job, offset)
                    settled = state is JobState.SETTLED
                    if chunk:
                        offset += len(chunk)
                        if output_open:
                            try:
                                view = memoryview(chunk)
                                while view:
                                    view = view[os.write(self._write_fd, view):]
                            except BrokenPipeError:
                                output_open = False
                    if len(chunk) == 65536:
                        # A full bounded frame may have a tail. Query it before
                        # publishing completion, including after the main exits.
                        continue
                    if code is not None and (not self.wait_for_descendants or settled):
                        self.returncode = code
                        return
                    if settled and self._done.is_set():
                        return  # stop() already collected proof; systemd may unload the unit.
                except SupervisionError:
                    if self._done.is_set():
                        return
                time.sleep(0.1)
        finally:
            os.close(self._write_fd)
            self._done.set()

    def poll(self) -> int | None:
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("supervised job", timeout)
        return self.returncode

    def kill(self) -> None:
        self.supervisor.stop_job(self.job)
        self.returncode = -15
        self._done.set()
