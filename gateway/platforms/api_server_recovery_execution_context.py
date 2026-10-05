"""Recover only within the original worker's execution and job authority."""

import asyncio
from contextlib import contextmanager, suppress

from gateway.platforms.api_server_execution_context import (
    bind_execution_context, capture_execution_context, verify_execution_directory,
)
from gateway.platforms.api_server_job_lifetime import create_job_lifetime, supervision_record, waits_for_jobs
from tools.environments.supervised_execution import bind_job_supervision


class RecoveryExecutionContext:
    def __init__(self, adapter, plan):
        self.adapter = adapter
        self.run_id = str(plan["successor_run_id"])
        self.profile = (plan.get("launch") or {}).get("request_profile")
        parent = adapter._run_idempotency_store.status_for_run(plan["scope"], plan["parent_run_id"])
        requested = (plan.get("launch") or {}).get("execution_context") or (parent or {}).get("status", {}).get("execution_context")
        with adapter._profile_scope(self.profile):
            self.context = capture_execution_context(requested) if requested is not None else None
        self.lifetime = None

    @contextmanager
    def scope(self):
        with self.adapter._profile_scope(self.profile), bind_execution_context(self.context), bind_job_supervision(
                self.lifetime.binding if self.lifetime is not None else None):
            yield

    async def prepare(self):
        if self.context is None:
            return
        with self.scope():
            await asyncio.to_thread(verify_execution_directory, self.context)
            fields = {"execution_context": self.context.requested}
            if waits_for_jobs(self.context):
                record = supervision_record(self.context, self.run_id)
                self.lifetime = await asyncio.to_thread(create_job_lifetime, self.context, record)
                self.lifetime.prepared = True
                self.adapter._run_job_lifetimes[self.run_id] = self.lifetime
                fields.update(supervision=record, supervision_ready=True)
                current = self.adapter._set_run_status(self.run_id, "recovery_pending", **fields)
                self.adapter._run_idempotency_store.update_status(self.run_id, current)
                from tools.environments.filesystem_supervisor import OwnershipPending

                while not (self.run_id in self.adapter._stopping_run_ids
                           or self.run_id in self.adapter._shutdown_interrupted_run_ids
                           or self.adapter._run_idempotency_store.stop_requested(self.run_id)):
                    try:
                        await asyncio.to_thread(self.lifetime.supervisor.prepare)
                        if "ownership" in self.context.requested:
                            self.lifetime.binding.state_dir = self.lifetime.supervisor.runtime_dir
                        break
                    except OwnershipPending:
                        await asyncio.sleep(0.1)
            current = self.adapter._set_run_status(self.run_id, "recovery_pending", **fields)
            self.adapter._run_idempotency_store.update_status(self.run_id, current)

    def settle(self, failed=False):
        if self.lifetime is not None and self.lifetime.prepared:
            with self.scope():
                self.lifetime.settle(lambda: failed or self.run_id in self.adapter._stopping_run_ids
                                     or self.adapter._run_idempotency_store.stop_requested(self.run_id))

    def close(self):
        self.adapter._run_job_lifetimes.pop(self.run_id, None)
        if self.lifetime is not None:
            self.lifetime.close()


def invoke_recovered_tool(adapter, agent, plan, history, on_dispatch, api):
    from agent.agent_runtime_helpers import invoke_tool
    from gateway.session_context import clear_session_vars

    launch, tool = plan["launch"], plan["tool"]
    session_id = str(launch.get("session_id") or plan["parent_run_id"])
    tokens = None
    try:
        tokens = adapter._bind_api_server_session(
            chat_id=session_id, session_key=plan["successor_run_id"], session_id=session_id,
            profile=launch.get("request_profile") or "", browser_control_principal=None,
            browser_control_transport_family=None, session_history_delivery="",
        )
        api._publish_turn_process_ownership(agent, session_id)
        on_dispatch()
        return invoke_tool(
            agent, str(tool["tool_name"]), dict(tool["tool_args"]), session_id,
            tool_call_id=str(tool["tool_call_id"]), messages=history,
            pre_tool_block_checked=True, skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True, approved_recovery=True,
        )
    finally:
        api._clear_turn_process_ownership(agent)
        if tokens:
            with suppress(Exception):
                clear_session_vars(tokens)
