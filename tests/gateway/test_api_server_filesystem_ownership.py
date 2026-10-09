"""Early ownership parks a second controller before model or file-tool execution."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock

import pytest
import hermes_yaml as yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tests.gateway.test_api_server_execution_context import multiplex, profile_scope
from tests.tools.test_target_job_supervision import target
from tools.environments.filesystem_authority import FilesystemAuthority
from tools.environments.filesystem_authority_server import AuthorityServer
from tools.environments.filesystem_claims import ClaimStore
from tools.environments.systemd_jobs import SystemdJobSupervisor


@pytest.mark.asyncio
@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local", "ssh"])
async def test_two_gateways_park_before_tools_and_keep_key_rotation_identity(tmp_path, monkeypatch, target, backend):
    from gateway.platforms.api_server_execution_context import capture_execution_context, ExecutionContextError
    from gateway.platforms.api_server_filesystem_ownership import ownership_supervisor
    from gateway.platforms.api_server_job_lifetime import create_job_lifetime, supervision_record
    from hermes_constants import hermes_home_key
    from model_tools import handle_function_call
    from tools.environments.job_supervision import SupervisionError

    state, root = tmp_path / "authority", tmp_path / "maintained"
    root.mkdir()
    ClaimStore.initialize(state)
    # The hosted runner invokes Python by absolute store path. Its ambient PATH
    # does not enroll an interpreter for jobs, which deliberately use the target
    # policy instead of inheriting a controller or SSH login environment.
    enrolled_path = os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"]
    authority = FilesystemAuthority(state, {os.getuid(): {
        "id": "controller", "execution_uid": os.getuid(), "roots": [str(root)],
        "environment": {"PATH": enrolled_path},
    }}, supervisor_factory=lambda row, uid: SystemdJobSupervisor(target, str(state / row["id"])))
    called = []
    adapters = []
    with tempfile.TemporaryDirectory(prefix="hfo-") as sockets:
        socket_path = sockets + "/control"
        with AuthorityServer(socket_path, authority) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                clients, contexts, headers = [], [], []
                for name in ("a", "b"):
                    home = tmp_path / name
                    home.mkdir()
                    command = [sys.executable, "-m", "tools.environments.filesystem_authority_server"]
                    # SSH fixture logs into this source checkout; explicit Python
                    # path keeps the transport independent of login PATH setup.
                    if backend == "ssh":
                        command = [sys.executable, str(__import__("pathlib").Path(__file__).resolve().parents[2] /
                                                      "tests/gateway/filesystem_authority_client.py")]
                    terminal = {"backend": backend, "cwd": str(root), "filesystem_authority": {
                        "authority": authority.store.authority_id, "principal": "controller",
                        "socket": socket_path, "command": command,
                    }}
                    context = {"version": 1, "backend": backend, "cwd": str(root), "lifetime": "wait_for_jobs",
                               "ownership": {"authority": authority.store.authority_id, "principal": "controller",
                                              "request": "shared-wire-request", "fingerprint": "shared-wire-digest",
                                              "roots": [str(root)]}}
                    if backend == "ssh":
                        ssh = target.ssh_config
                        terminal.update(ssh_host=ssh["host"], ssh_port=ssh["port"], ssh_user=ssh["user"],
                                        ssh_key=ssh["key"], ssh_hermes_home=str(home / "remote"))
                        context["ssh"] = {k: ssh[k] for k in ("host", "port", "user")}
                    (home / "config.yaml").write_text(yaml.safe_dump({"terminal": terminal,
                                                                     "approvals": {"unattended_mode": "approve"}}))
                    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": name}))
                    adapters.append(adapter)
                    def create_agent(_name=name, **kwargs):
                        agent = MagicMock()
                        agent.session_id = kwargs["session_id"]
                        def run(task_id, **_):
                            called.append(_name)
                            result = json.loads(handle_function_call("write_file", {"path": str(root / _name), "content": _name}, task_id=task_id))
                            assert "error" not in result, result
                            result = json.loads(handle_function_call("terminal", {"command": "printf '%s' \"$HOME\""}, task_id=task_id))
                            assert result["exit_code"] == 0, result
                            assert "/jobs/" in result["output"], result
                            interpreter = json.loads(handle_function_call("terminal", {"command": "command -v python3"}, task_id=task_id))
                            assert interpreter["exit_code"] == 0, interpreter
                            assert interpreter["output"].strip() == os.path.join(os.path.dirname(sys.executable), "python3"), interpreter
                            for code in ("owned_value = 40; print(owned_value)", "owned_value += 2; print(owned_value)"):
                                cell = json.loads(handle_function_call("execute_code", {"code": code}, task_id=task_id))
                                assert cell["status"] == "success", cell
                                assert cell.get("kernel"), cell
                            assert cell["output"].strip() == "42", cell
                            return {"completed": True, "final_response": _name}
                        agent.run_conversation.side_effect = run
                        return agent
                    monkeypatch.setattr(adapter, "_create_agent", create_agent)
                    @web.middleware
                    async def scoped(request, handler, _home=home):
                        with profile_scope(_home):
                            return await handler(request)
                    app = web.Application(middlewares=[scoped])
                    app.router.add_post("/v1/filesystem-ownership", adapter._handle_filesystem_ownership)
                    app.router.add_post("/v1/runs", adapter._handle_runs)
                    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
                    client = TestClient(TestServer(app))
                    await client.start_server()
                    clients.append(client)
                    contexts.append(context)
                    headers.append({"Authorization": f"Bearer {name}", "Idempotency-Key": name})
                outside = tmp_path / "outside"
                outside.mkdir()
                denied_context = {**contexts[0], "ownership": {
                    **contexts[0]["ownership"], "request": "denied", "roots": [str(outside)],
                }}
                denied = await clients[0].post("/v1/filesystem-ownership", headers=headers[0],
                    json={"operation": "reserve", "execution_context": denied_context})
                assert denied.status == 409, await denied.text()
                assert (await denied.json())["code"] == "rejected"
                assert not authority.store.live()
                # Refusal still supports a durable stop tombstone; the control
                # plane must settle the original request rather than drop it.
                stopped = await clients[0].post("/v1/filesystem-ownership", headers=headers[0],
                    json={"operation": "stop", "execution_context": denied_context})
                assert stopped.status == 200 and (await stopped.json())["state"] == "settled"
                reserved = await clients[0].post("/v1/filesystem-ownership", headers=headers[0],
                                                 json={"operation": "reserve", "execution_context": contexts[0]})
                assert reserved.status == 200, await reserved.text()
                first = await reserved.json()
                assert first["state"] == "active"
                # A -> B -> A with identical client ownership identities and
                # the same Unix controller enrollment must never replay A's grant.
                other = await clients[1].post("/v1/filesystem-ownership", headers=headers[1],
                    json={"operation": "reserve", "execution_context": contexts[1]})
                assert other.status == 200, await other.text()
                assert (await other.json())["state"] == "pending"
                for operation in ("status", "stop", "release"):
                    other = await clients[1].post("/v1/filesystem-ownership", headers=headers[1],
                        json={"operation": operation, "execution_context": contexts[1]})
                    assert other.status == 200, await other.text()
                    assert (await other.json())["claim"] != first["claim"]
                resumed = await clients[0].post("/v1/filesystem-ownership", headers=headers[0],
                    json={"operation": "status", "execution_context": contexts[0]})
                assert resumed.status == 200, await resumed.text()
                assert await resumed.json() == first
                with profile_scope(tmp_path / "a"):
                    admitted_context = capture_execution_context(contexts[0])
                    record = supervision_record(admitted_context, "recovery-contract")
                    for recorded_home in (None, hermes_home_key(tmp_path / "b")):
                        with pytest.raises(SupervisionError, match="recovery identity mismatch"):
                            create_job_lifetime(admitted_context, {**record, "profile_home": recorded_home})
                with profile_scope(tmp_path / "b"):
                    with pytest.raises(ExecutionContextError, match="admitted profile scope"):
                        ownership_supervisor(admitted_context)
                contexts[1]["ownership"]["request"] = "b-after-cancel"
                b = await clients[1].post("/v1/runs", headers=headers[1], json={"input": "work", "execution_context": contexts[1]})
                assert b.status == 202, await b.text()
                bid = (await b.json())["run_id"]
                deadline = time.monotonic() + 30
                while adapters[1]._run_statuses[bid].get("last_event") != "run.waiting_for_ownership":
                    assert time.monotonic() < deadline
                    await asyncio.sleep(.05)
                assert not called
                a = await clients[0].post("/v1/runs", headers=headers[0], json={"input": "work", "execution_context": contexts[0]})
                assert a.status == 202, await a.text()
                aid = (await a.json())["run_id"]
                await asyncio.wait_for(asyncio.gather(*adapters[0]._active_run_tasks.values()), 60)
                assert called == ["a"]
                assert adapters[0]._run_statuses[aid]["supervision"]["profile_home"] == hermes_home_key(tmp_path / "a")
                still_waiting = await clients[1].post("/v1/filesystem-ownership", headers=headers[1],
                                                     json={"operation": "reserve", "execution_context": contexts[1]})
                assert (await still_waiting.json())["state"] == "pending"
                released = await clients[0].post("/v1/filesystem-ownership", headers=headers[0],
                                                json={"operation": "release", "execution_context": contexts[0]})
                assert released.status == 200, await released.text()
                assert (await released.json())["state"] == "settled"
                await asyncio.wait_for(asyncio.gather(*(task for adapter in adapters for task in adapter._active_run_tasks.values())), 60)
                assert called == ["a", "b"]
                assert [adapter._run_statuses[rid]["status"] for adapter, rid in zip(adapters, (aid, bid))] == ["completed", "completed"]
                assert (root / "a").read_text() == "a" and (root / "b").read_text() == "b"
                adapters[0]._api_key = "rotated"
                replay = await clients[0].post("/v1/runs", headers={**headers[0], "Authorization": "Bearer rotated"},
                                               json={"input": "work", "execution_context": contexts[0]})
                assert replay.status == 202 and (await replay.json())["run_id"] == aid
                assert called == ["a", "b"]
                same_claim = await clients[0].post("/v1/filesystem-ownership",
                    headers={**headers[0], "Authorization": "Bearer rotated"},
                    json={"operation": "status", "execution_context": contexts[0]})
                assert same_claim.status == 200, await same_claim.text()
                assert (await same_claim.json())["claim"] == first["claim"]
            finally:
                for adapter in adapters:
                    adapter._stopping_run_ids.update(adapter._active_run_tasks)
                await asyncio.wait_for(asyncio.gather(*(task for adapter in adapters for task in adapter._active_run_tasks.values()),
                                                      return_exceptions=True), 30)
                for client in locals().get("clients", []):
                    await client.close()
                server.shutdown()
                thread.join()
                authority.close()
