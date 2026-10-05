"""Retain executor ownership while joining Stop and cancellation."""

import asyncio
from contextlib import suppress


async def await_run_executor(adapter, run, agent, execution, *, api):
    from gateway.platforms.api_server_runs import _observe_durable_stop, _unregister_approval_notify

    cancelled = False
    while True:
        try:
            done, _ = await asyncio.wait([execution], timeout=0.1)
            if done:
                result = execution.result()
                break
            _observe_durable_stop(adapter, run.run_id, _api_server=api)
        except asyncio.CancelledError:
            if execution.done():
                execution.result()
                raise
            cancelled = True
            adapter._run_idempotency_store.stop_lineage(adapter._run_owners[run.run_id], run.run_id)
            if run.run_id not in adapter._stopping_run_ids:
                adapter._stopping_run_ids.add(run.run_id)
                adapter._set_run_status(run.run_id, "stopping", last_event="run.stopping")
                with suppress(Exception):
                    api.request_hard_interrupt(agent, "Run task cancelled")
            # Wrapper cancellation cannot stop its worker. Release approval
            # waits and retain ownership until the executor settles.
            _unregister_approval_notify(run.approval_session_key)
    if cancelled:
        raise asyncio.CancelledError
    return result
