"""Live operator admission policy; draining never revokes admitted run ownership."""

import json
from pathlib import Path


def executor_admission(adapter, *, api):
    from hermes_cli.config import cfg_get, load_config_readonly

    # Resolve configuration under the request's owning profile, including probes.
    with adapter._profile_scope(api._api_request_profile.get()):
        try:
            path = cfg_get(load_config_readonly(), "gateway", "api_server", "admission_file", default=None)
            accepting = path is None
            if path is not None:
                if not isinstance(path, str) or not Path(path).is_absolute():
                    raise ValueError("admission_file requires an absolute path")
                policy = json.loads(Path(path).read_text(encoding="utf-8-sig"))
                accepting = (isinstance(policy, dict) and type(policy.get("version")) is int and policy["version"] == 1
                             and policy.get("accepting") is True)
        except (OSError, ValueError, TypeError):
            accepting = False
    limit = adapter._max_concurrent_runs
    count = adapter.active_agent_work_count()
    reservation = api._api_agent_request_reservation.get()
    if reservation and reservation["active"]:
        count -= 1
    return {"version": 1, "accepting": accepting,
            "available_slots": max(0, limit - count) if limit > 0 else None}


def executor_admission_response(adapter, request, *, api, idempotency_key,
                                idempotency_scope, idempotency_fingerprint, gateway_session_key):
    from gateway.platforms.api_server_runs import _lookup_replay_response
    from gateway.platforms.api_server_room_grants import _json_error

    if executor_admission(adapter, api=api)["accepting"]:
        return None
    replay = _lookup_replay_response(
        adapter, request, idempotency_key=idempotency_key, idempotency_scope=idempotency_scope,
        idempotency_fingerprint=idempotency_fingerprint, gateway_session_key=gateway_session_key,
        _openai_error=api._openai_error)
    if replay is not None:
        return replay
    return _json_error(api._openai_error, "Executor is draining; new runs are disabled",
                       code="executor_draining", status=503)
