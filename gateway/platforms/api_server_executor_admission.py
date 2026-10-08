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
                policy = json.loads(Path(path).read_text(encoding="utf-8"))
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
