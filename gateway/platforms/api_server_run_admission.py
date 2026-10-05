"""Stop an idempotent admission, including before its create request arrives."""

import time
import uuid

from aiohttp import web

from gateway.platforms.api_server_execution_context import validate_execution_context
from gateway.platforms.api_server_run_idempotency import request_identity


async def stop_admission(adapter, request, *, api) -> web.Response:
    from gateway.platforms.api_server_runs import _json_error, _replay_or_conflict, _stop_owned_run

    auth_error = adapter._check_auth(request)
    if auth_error is not None:
        return auth_error
    session_key, error = adapter._parse_session_key_header(request)
    if error is not None:
        return error
    try:
        body = await request.json()
        if not isinstance(body, dict) or "hosted_room_dispatch" in body:
            raise ValueError("Stopping an admission requires the original execution-context request")
        context = body.get("execution_context")
        validate_execution_context(context)
        key, fingerprint = request_identity(body, session_key, request.headers.get("Idempotency-Key", ""))
        if not key or context.get("lifetime") != "wait_for_jobs" or not adapter._run_idempotency_store.durable:
            raise ValueError("Stopping an admission requires wait_for_jobs, Idempotency-Key and durable storage")
    except (ValueError, TypeError) as exc:
        return _json_error(api._openai_error, str(exc), code="invalid_run_admission_stop", status=400)

    scope = adapter._run_idempotency_scope(request)
    now = time.time()
    run_id = f"run_{uuid.uuid4().hex}"
    cancelled = {"object": "hermes.run", "run_id": run_id, "status": "cancelled",
                 "created_at": now, "updated_at": now, "execution_context": context,
                 "last_event": "run.cancelled", "admission_cancelled": True}
    # Reserve uses the same unique key and transaction as create. A create that
    # was waiting on history/config I/O must observe this tombstone at reserve.
    outcome, record = adapter._run_idempotency_store.reserve(scope, key, fingerprint, run_id, cancelled)
    if outcome == "conflict":
        return _replay_or_conflict(adapter, request, outcome, record, session_key, api._openai_error)
    run_id = record["run_id"]
    status = adapter._durable_run_status(request, run_id)
    return _stop_owned_run(adapter, request, run_id, status,
                           adapter._active_run_agents.get(run_id), adapter._active_run_tasks.get(run_id),
                           _api_server=api)
