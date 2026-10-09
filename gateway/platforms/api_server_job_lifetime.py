"""Run lifetime follows execution-host jobs, including after controller loss."""

import asyncio
import logging
import posixpath
import time
from contextlib import suppress
from dataclasses import dataclass

from gateway.platforms.api_server_execution_context import bind_execution_context, capture_execution_context
from tools.environments.job_supervision import SupervisionError, TargetJobSupervisor
from tools.environments.supervised_execution import SupervisionBinding, local_supervisor, ssh_supervisor


def waits_for_jobs(context) -> bool:
    return bool(context and context.requested.get("lifetime") == "wait_for_jobs")


def supervision_record(context, run_id: str) -> dict:
    from hermes_constants import get_hermes_home
    from tools.terminal_tool import _get_env_config

    if "ownership" in context.requested:
        return {"provider": "filesystem_authority", "ownership": context.requested["ownership"],
                "profile_home": context.profile_home}
    home = (str(get_hermes_home()) if context.requested["backend"] == "local"
            else _get_env_config()["ssh_hermes_home"])
    return {"provider": "systemd", "state_dir": posixpath.join(home, "run-jobs", run_id)}


@dataclass
class RunJobLifetime:
    binding: SupervisionBinding
    supervisor: TargetJobSupervisor
    transport: object = None
    prepared: bool = False

    def settle(self, should_stop) -> None:
        # Communication loss leaves the run live. Every retry is a control/query
        # operation against the same receipts; user commands are never replayed.
        while True:
            try:
                if should_stop():
                    self.binding.stop_workers()
                    self.supervisor.stop()
                if not self.binding.workers_settled():
                    time.sleep(0.25)
                    continue
                if not should_stop():
                    from tools.code_kernel_supervised import finish_supervised_kernel
                    for kernel in self.binding.kernels:
                        finish_supervised_kernel(kernel)
                self.supervisor.seal()
                if self.supervisor.settled():
                    for handle in self.binding.handles:
                        if should_stop() and handle.poll() is None:
                            handle.kill()
                        handle.wait()
                    self.supervisor.stop()  # retire retained, now empty units
                    return
            except SupervisionError:
                pass
            time.sleep(0.25)

    def close(self):
        if self.transport is not None:
            self.transport.cleanup()


def create_job_lifetime(context, record: dict) -> RunJobLifetime:
    from tools.environments.local import _make_run_env
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_backends import _build_ssh_env, _ssh_config_from_config

    if record["provider"] == "filesystem_authority":
        from gateway.platforms.api_server_filesystem_ownership import ownership_supervisor

        if (record["ownership"] != context.requested.get("ownership")
                or not record.get("profile_home") or record["profile_home"] != context.profile_home):
            raise SupervisionError("filesystem ownership recovery identity mismatch")
        supervisor, transport = ownership_supervisor(context)
        return RunJobLifetime(SupervisionBinding("", supervisor=supervisor), supervisor, transport)
    if record["provider"] != "systemd":
        raise SupervisionError("run supervisor provider is unavailable")
    binding = SupervisionBinding(record["state_dir"])
    if context.requested["backend"] == "local":
        return RunJobLifetime(binding, local_supervisor(binding, _make_run_env({})))
    env = _build_ssh_env(cwd=context.requested["cwd"], timeout=10, probe_only=True,
                         ssh_config=_ssh_config_from_config(_get_env_config()))
    return RunJobLifetime(binding, ssh_supervisor(binding, env, {}), env)


def request_job_stop(self, run_id: str) -> None:
    lifetime = self._run_job_lifetimes.get(run_id)
    if lifetime is None:
        return
    async def stop():
        lifetime.binding.stop_workers()
        with suppress(SupervisionError):
            await asyncio.to_thread(lifetime.supervisor.stop)
    task = asyncio.create_task(stop())
    self._background_tasks.add(task)
    task.add_done_callback(self._background_tasks.discard)


async def settle_failed_run(run) -> None:
    if run.job_lifetime is None or not run.job_lifetime.prepared:
        return
    settlement = asyncio.create_task(asyncio.to_thread(run.job_lifetime.settle, lambda: True))
    while True:
        try:
            await asyncio.shield(settlement)
            return
        except asyncio.CancelledError:
            if settlement.done():
                settlement.result()
                return


async def cleanup_job_payloads(self, run_id, lifetime) -> None:
    from tools.environments.systemd_jobs import SystemdJobSupervisor

    if not isinstance(lifetime.supervisor, SystemdJobSupervisor):
        return  # Shared filesystem authorities own their separate receipt ledger.
    status = self._run_statuses.get(run_id, {})
    if status.get("status") not in {"completed", "failed", "cancelled", "interrupted"}:
        return
    try:
        # _set_run_status logs persistence errors; deletion must instead fail
        # closed and verify the authenticated durable terminal record first.
        if not self._run_idempotency_store.durable:
            raise RuntimeError("terminal run persistence is not durable")
        self._run_idempotency_store.update_status(run_id, status)
        record = self._run_idempotency_store.status_for_run(self._run_owners[run_id], run_id)
        if record is None or record["status"] != status:
            raise RuntimeError("terminal run persistence is unavailable")
        await asyncio.to_thread(lifetime.supervisor.cleanup_payloads)
    except Exception:
        logging.getLogger("gateway.platforms.api_server").exception("Retaining supervised payloads for run %s", run_id)


def ensure_job_recovery(self, run_id: str, profile) -> None:
    status = self._run_statuses.get(run_id, {})
    if (not status.get("supervision_recovery_required") or run_id in self._active_run_tasks
            or status.get("status") in {"completed", "failed", "cancelled", "interrupted"}):
        return

    async def recover():
        lifetime = None
        try:
            while lifetime is None:
                try:
                    with self._profile_scope(profile):
                        context = capture_execution_context(status["execution_context"])
                        with bind_execution_context(context):
                            lifetime = await asyncio.to_thread(create_job_lifetime, context, status["supervision"])
                except (ValueError, RuntimeError, OSError):
                    self._set_run_status(run_id, "stopping", last_event="run.supervision_unavailable")
                    await asyncio.sleep(1)
            self._run_job_lifetimes[run_id] = lifetime
            # The model worker was lost; stop its existing jobs and fence any
            # delayed submissions before publishing interruption.
            settlement = asyncio.create_task(asyncio.to_thread(lifetime.settle, lambda: True))
            while True:
                try:
                    await asyncio.shield(settlement)
                    break
                except asyncio.CancelledError:
                    if settlement.done():
                        settlement.result()
                        break
            self._set_run_status(run_id, "interrupted", last_event="run.interrupted",
                                 error="Gateway restarted; owned jobs have been stopped.")
            await cleanup_job_payloads(self, run_id, lifetime)
        finally:
            self._active_run_tasks.pop(run_id, None)
            self._run_job_lifetimes.pop(run_id, None)
            if lifetime is not None:
                lifetime.close()

    task = asyncio.create_task(recover())
    self._active_run_tasks[run_id] = task
    self._background_tasks.add(task)
    task.add_done_callback(self._background_tasks.discard)
