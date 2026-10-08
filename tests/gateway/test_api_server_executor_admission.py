"""Profile draining refuses new work without abandoning an existing admission."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.api_server_runs import _http_routes


@pytest.mark.asyncio
async def test_live_detach_retains_stop_and_replay(tmp_path, monkeypatch):
    import hermes_yaml as yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    marker = tmp_path / "admission.json"
    marker.write_text(json.dumps({"version": 1, "accepting": True}))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "gateway": {"api_server": {"admission_file": str(marker)}}}))
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
    headers = {"Authorization": "Bearer fixture-key", "Idempotency-Key": "old-intent"}
    body = {"input": "old work"}
    app = web.Application()
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    for method, path, handler in _http_routes(adapter):
        app.router.add_route(method, path, handler)
    agent = MagicMock()
    with patch.object(adapter, "_create_agent", return_value=agent):
        try:
            async with TestClient(TestServer(app)) as client:
                # Stop-before-create creates a durable, owned admission tombstone.
                stopped = await client.post("/v1/runs/stop", json=body, headers=headers)
                assert stopped.status == 200
                receipt = await stopped.json()
                marker.write_text(json.dumps({"version": 1, "accepting": False}))
                capabilities = await (await client.get("/v1/capabilities", headers=headers)).json()
                assert capabilities["features"]["runs_executor_admission"]["accepting"] is False
                replay = await client.post("/v1/runs", json=body, headers=headers)
                assert replay.status == 202
                assert (await replay.json())["run_id"] == receipt["run_id"]
                denied = await client.post("/v1/runs", json=body, headers={**headers, "Idempotency-Key": "new-intent"})
                assert denied.status == 503
                assert (await denied.json())["error"]["code"] == "executor_draining"
                stopped_again = await client.post("/v1/runs/stop", json=body, headers=headers)
                assert stopped_again.status == 200
                assert (await stopped_again.json())["admission"] == receipt["admission"]
                agent.run_conversation.assert_not_called()
        finally:
            adapter._run_idempotency_store.close()


@pytest.mark.parametrize("contents", [None, "invalid", '{"version":1,"accepting":"true"}', '{"version":2,"accepting":true}', '{"version":true,"accepting":true}'])
@pytest.mark.asyncio
async def test_configured_gate_fails_closed_and_reports_capacity(tmp_path, monkeypatch, contents):
    import hermes_yaml as yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    marker = tmp_path / "admission.json"
    if contents is not None:
        marker.write_text(contents)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "gateway": {"api_server": {"admission_file": str(marker), "max_concurrent_runs": 1}}}))
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
    try:
        response = await adapter._handle_capabilities(MagicMock())
        admission = json.loads(response.text)["features"]["runs_executor_admission"]
        assert admission == {"version": 1, "accepting": False, "available_slots": 1}
        marker.write_text(json.dumps({"version": 1, "accepting": True}))
        with patch.object(adapter, "active_agent_work_count", return_value=1):
            response = await adapter._handle_capabilities(MagicMock())
        assert json.loads(response.text)["features"]["runs_executor_admission"] == {
            "version": 1, "accepting": True, "available_slots": 0}
    finally:
        adapter._run_idempotency_store.close()


@pytest.mark.asyncio
async def test_detach_during_history_preparation_prevents_reservation(tmp_path, monkeypatch):
    import hermes_yaml as yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    marker = tmp_path / "admission.json"
    marker.write_text(json.dumps({"version": 1, "accepting": True}))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "gateway": {"api_server": {"admission_file": str(marker)}}}))
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
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
    ), patch.object(adapter, "_conversation_history_for_session", side_effect=history):
        try:
            async with TestClient(TestServer(app)) as client:
                pending = asyncio.create_task(client.post("/v1/runs", json={"input": "new work"}, headers={
                    "Authorization": "Bearer fixture-key", "Idempotency-Key": "delayed-intent",
                    "X-Hermes-Session-Key": "conversation"}))
                try:
                    await asyncio.wait_for(entered.wait(), 10)
                    marker.write_text(json.dumps({"version": 1, "accepting": False}))
                    release.set()
                    response = await pending
                    assert response.status == 503
                    assert (await response.json())["error"]["code"] == "executor_draining"
                    assert adapter._run_statuses == {}
                    agent.run_conversation.assert_not_called()
                finally:
                    release.set()
                    await pending
        finally:
            adapter._run_idempotency_store.close()
