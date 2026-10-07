"""Cancellation owns an idempotency key before a delayed create can start work."""

import asyncio
import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server_runs import _http_routes
from gateway.platforms.api_server import APIServerAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before_create", "during_admission"])
@pytest.mark.parametrize("supervised", [False, True], ids=["ordinary", "supervised"])
async def test_stop_admission_fences_delayed_requests_after_restart(tmp_path, monkeypatch, when, supervised):
    import hermes_yaml as yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "terminal": {"backend": "local", "cwd": str(tmp_path)}}))
    body = {"input": "maintain directory", "execution_context": {
        "version": 1, "backend": "local", "cwd": str(tmp_path), "lifetime": "wait_for_jobs"}}
    if not supervised:
        body.pop("execution_context")
    headers = {"Idempotency-Key": "stop-before-admission", "X-Hermes-Session-Key": "owned-directory",
               "Authorization": "Bearer admission-fixture-key"}
    config = PlatformConfig(enabled=True, extra={"key": "admission-fixture-key"})
    adapter = APIServerAdapter(config)
    entered, release = asyncio.Event(), asyncio.Event()

    async def history(*args):
        entered.set()
        await release.wait()
        return []

    app = web.Application()
    for method, path, handler in _http_routes(adapter):
        app.router.add_route(method, path, handler)
    agent = MagicMock()
    with patch.object(adapter, "_create_agent", return_value=agent), patch.object(
        adapter, "_declared_conversation_session", return_value="existing-session"
    ), patch.object(adapter, "_conversation_history_for_session", side_effect=history), patch.object(
        adapter, "_admit_to_live_bot_chat", return_value=None
    ):
        async with TestClient(TestServer(app)) as client:
            pending = None
            try:
                if when == "during_admission":
                    pending = asyncio.create_task(client.post("/v1/runs", json=body, headers=headers))
                    await asyncio.wait_for(entered.wait(), 10)
                stopped = await client.post("/v1/runs/stop", json=body, headers=headers)
                assert stopped.status == 200
                receipt = await stopped.json()
                assert receipt["status"] == "cancelled"
                assert receipt.get("admission") == {
                    "version": 1, "root_run_id": receipt["run_id"],
                    "key_sha256": hashlib.sha256(headers["Idempotency-Key"].encode()).hexdigest(),
                    "body_sha256": hashlib.sha256(json.dumps(body).encode()).hexdigest(),
                }
                assert receipt["stop_requested"] is True
                assert receipt["lineage_settled"] is True
                assert receipt["lineage"] == [{"run_id": receipt["run_id"], "status": "cancelled"}]
                release.set()
                if pending:
                    assert (await (await pending).json())["run_id"] == receipt["run_id"]
                assert (await client.post("/v1/runs/stop", json={**body, "input": "different"}, headers=headers)).status == 409
                assert (await client.post("/v1/runs/stop", json=body, headers={"Authorization": headers["Authorization"]})).status == 400
                assert (await client.post("/v1/runs/stop", json=body)).status == 401
            finally:
                release.set()
                if pending:
                    await pending
                await asyncio.gather(*adapter._active_run_tasks.values(), return_exceptions=True)
                adapter._run_idempotency_store.close()
            agent.run_conversation.assert_not_called()

    # No target config dependency for a replay or cancellation. The original
    # directory can be unavailable and the cancelled admission remains fenced.
    (tmp_path / "config.yaml").write_text("terminal:\n  backend: local\n  cwd: /missing-target\n")
    restarted = APIServerAdapter(config)
    app = web.Application()
    for method, path, handler in _http_routes(restarted):
        app.router.add_route(method, path, handler)
    try:
        async with TestClient(TestServer(app)) as client:
            replay = await client.post("/v1/runs", json=body, headers=headers)
            assert replay.status == 202
            assert (await replay.json())["run_id"] == receipt["run_id"]
            stopped = await client.post("/v1/runs/stop", json=body, headers=headers)
            resumed = await stopped.json()
            assert resumed["status"] == "cancelled"
            assert resumed["admission"] == receipt["admission"]
            wire = json.dumps(body, indent=2, ensure_ascii=False)
            stopped = await client.post("/v1/runs/stop", data=wire,
                                        headers={**headers, "Content-Type": "application/json"})
            rebound = await stopped.json()
            assert rebound["run_id"] == receipt["run_id"]
            assert rebound["admission"]["body_sha256"] == hashlib.sha256(wire.encode()).hexdigest()
            conflict = await client.post("/v1/runs/stop", json={**body, "input": "different request"}, headers=headers)
            assert conflict.status == 409
            assert "admission" not in await conflict.json()
            unauthenticated = await client.post("/v1/runs/stop", json=body,
                                                headers={**headers, "Authorization": "Bearer wrong-fixture-key"})
            assert unauthenticated.status == 401
            assert "admission" not in await unauthenticated.json()
    finally:
        restarted._run_idempotency_store.close()
