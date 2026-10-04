"""A run's terminal target is a precondition, never an untrusted config override."""

import asyncio
import json
import os
import shlex
import time
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.terminal_scope import install_and_reset_profile_terminal_scope
from tools.terminal_tool import _get_env_config


@pytest.fixture(autouse=True)
def multiplex():
    from agent.secret_scope import is_multiplex_active, set_multiplex_active

    previous = is_multiplex_active()
    set_multiplex_active(True)
    yield
    set_multiplex_active(previous)


@contextmanager
def profile_scope(home):
    token = set_hermes_home_override(home)
    try:
        with install_and_reset_profile_terminal_scope(home):
            yield
    finally:
        reset_hermes_home_override(token)


@pytest.fixture
def ssh_target(tmp_path, monkeypatch, backend):
    if backend == "local":
        yield {"host": "example.test", "user": "worker", "port": 2222, "key": ""}
        return
    from tests.tools.ssh_worker_transport import openssh_transport

    login_home = tmp_path / "login"
    login_home.mkdir()
    with openssh_transport(tmp_path / "sshd", login_home, monkeypatch) as target:
        yield target


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
@pytest.mark.parametrize("backend", ["local", "ssh"])
async def test_runs_check_and_pin_the_served_target(tmp_path, monkeypatch, backend, ssh_target):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
    observed = []

    def create_agent(**kwargs):
        initial = _get_env_config()
        agent = MagicMock()
        agent.session_id = kwargs["session_id"]

        def run(task_id, **_):
            from gateway.session_context import session_history_delivery_supported
            from tools.terminal_tool import terminal_tool
            from tools.terminal_tool_lifecycle import cleanup_vm

            actual = _get_env_config()
            observed.append((initial["cwd"], actual["cwd"], actual["env_type"], actual["ssh_user"]))
            assert not session_history_delivery_supported()
            try:
                result = json.loads(terminal_tool(command="pwd", task_id=task_id))
                assert result.get("exit_code") == 0, result
                assert result["output"].strip() == initial["cwd"]
            finally:
                cleanup_vm(task_id)
            return {"final_response": "done"}

        agent.run_conversation.side_effect = run
        return agent

    monkeypatch.setattr(adapter, "_create_agent", create_agent)
    homes = {}

    @web.middleware
    async def routed_profile(request, handler):
        home = homes[request.headers["Test-Profile"]]
        with profile_scope(home):
            response = await handler(request)
            if response.status == 202 and request.headers.get("Test-Change-Config"):
                (home / "config.yaml").write_text(f"terminal:\n  backend: local\n  cwd: {tmp_path}\n")
            return response

    app = web.Application(middlewares=[routed_profile])
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    headers = {"Authorization": "Bearer fixture-key"}
    async with TestClient(TestServer(app)) as client:
        for name in ("a", "b"):
            home = tmp_path / name
            home.mkdir()
            workspace = home / "data"
            workspace.mkdir()
            (home / "config.yaml").write_text(
                f"terminal:\n  backend: {backend}\n  cwd: {workspace}\n"
                f"  ssh_host: {ssh_target['host']}\n  ssh_port: {ssh_target['port']}\n  ssh_user: {ssh_target['user']}\n"
                f"  ssh_key: {ssh_target['key']}\n  ssh_hermes_home: {home / 'remote-state'}\n")
            homes[name] = home
        for name in ("a", "b", "a"):
            headers["Test-Profile"] = name
            home = homes[name]
            context = {"version": 1, "backend": backend, "cwd": str(home / "data")}
            if backend == "ssh":
                context["ssh"] = {"host": ssh_target["host"], "port": ssh_target["port"], "user": ssh_target["user"]}
            with profile_scope(home):
                # Every mismatched component is rejected before an agent or run exists.
                mismatches = [{**context, "cwd": str(tmp_path)}, {**context, "version": 2}]
                if backend == "ssh":
                    mismatches += [{**context, "ssh": {**context["ssh"], field: value}}
                                   for field, value in (("host", "other.test"), ("user", "other"), ("port", 22))]
                for wrong in mismatches:
                    response = await client.post("/v1/runs", headers=headers,
                                                 json={"input": "work", "execution_context": wrong})
                    assert response.status in (400, 409), await response.text()
                before = len(observed)
                payload = {"input": "work", "execution_context": context}
                original_config = (home / "config.yaml").read_text()
                run_headers = {**headers, "Test-Change-Config": "1", "Idempotency-Key": f"{name}-{before}"}
                unauthorized = await client.post("/v1/runs", headers={"Test-Profile": name}, json=payload)
                assert unauthorized.status == 401
                response = await client.post("/v1/runs", headers=run_headers, json=payload)
                assert response.status == 202, await response.text()
                run_id = (await response.json())["run_id"]
                await asyncio.wait_for(asyncio.gather(*adapter._active_run_tasks.values()), 10)
                assert adapter._run_statuses[run_id]["status"] == "completed"
                assert adapter._run_statuses[run_id]["execution_context"] == context
                assert observed[before:] == [(context["cwd"], context["cwd"], backend, ssh_target["user"])]
                # A changed worker config must not re-execute a lost-acceptance replay.
                replay = await client.post("/v1/runs", headers=run_headers, json=payload)
                assert replay.status == 202
                assert (await replay.json())["run_id"] == run_id
                assert len(observed) == before + 1
                (home / "config.yaml").write_text(original_config)
                capabilities = await (await client.get("/v1/capabilities", headers=headers)).json()
                assert capabilities["features"]["runs_execution_context"]["version"] == context["version"]
        # An SSH path can only be checked on its target host. It fails before
        # agent construction, and never falls back to the login home.
        if backend == "ssh":
            (home / "data").rmdir()
            response = await client.post("/v1/runs", headers=headers, json=payload)
            assert response.status == 202
            run_id = (await response.json())["run_id"]
            await asyncio.wait_for(asyncio.gather(*adapter._active_run_tasks.values()), 10)
            assert adapter._run_statuses[run_id]["status"] == "failed"
            assert "directory" in adapter._run_statuses[run_id]["error"]
    assert len(observed) == 3


@pytest.mark.asyncio
@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local", "ssh"])
@pytest.mark.parametrize("tool", ["terminal", "execute_code", "execute_code_active"])
async def test_run_remains_owned_until_jobs_settle_and_stop_fences_them(tmp_path, monkeypatch, backend, ssh_target, tool):
    import subprocess
    import hermes_yaml as yaml
    from tools.environments.local import build_subprocess_env
    from tools.process_registry import systemd_user_bus_env
    from tools.terminal_tool import terminal_tool
    from tools.terminal_tool_lifecycle import cleanup_vm

    probe = subprocess.run(["systemctl", "--user", "show", "--property=Version"], capture_output=True,
                           env=systemd_user_bus_env(build_subprocess_env()))
    if probe.returncode:
        pytest.skip("a systemd user manager is required")
    home, data = tmp_path / "worker", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    terminal = {"backend": backend, "cwd": str(data)}
    context = {"version": 1, "backend": backend, "cwd": str(data), "lifetime": "wait_for_jobs"}
    if backend == "ssh":
        terminal.update(ssh_host=ssh_target["host"], ssh_user=ssh_target["user"], ssh_port=ssh_target["port"],
                        ssh_key=ssh_target["key"], ssh_hermes_home=str(tmp_path / "remote-worker"))
        context["ssh"] = {k: ssh_target[k] for k in ("host", "user", "port")}
    (home / "config.yaml").write_text(yaml.safe_dump({
        "terminal": terminal, "approvals": {"unattended_mode": "approve"}}))
    monkeypatch.setenv("__ETC_PROFILE_SOURCED", "1")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
    tasks = []
    completed_models = set()

    def create_agent(**kwargs):
        agent = MagicMock()
        agent.session_id = kwargs["session_id"]
        def run(task_id, **_):
            from hermes_cli.config import get_config_path, load_config_readonly
            assert get_config_path() == home / "config.yaml"
            assert load_config_readonly()["approvals"]["unattended_mode"] == "approve"
            tasks.append(task_id)
            child = (f"echo ready > {shlex.quote(str(data / task_id))}; "
                     f"while test ! -e {shlex.quote(str(data / ('release-' + task_id)))}; do sleep .05; done")
            if tool == "terminal":
                result = json.loads(terminal_tool(command=f"setsid bash -c {shlex.quote(child)} </dev/null >/dev/null 2>&1 &",
                                                  background=True, task_id=task_id))
                assert result.get("exit_code") == 0, result
            else:
                from tools.code_execution_tool import execute_code
                code = ("import subprocess, os\nvalue = 41\n"
                        f"assert os.getcwd() == {str(data)!r}\n"
                        f"subprocess.Popen(['bash', '-c', {child!r}], start_new_session=True, "
                        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n")
                if tool == "execute_code_active":
                    code += ("import time\n"
                             f"while not os.path.exists({str(data / ('release-' + task_id))!r}): time.sleep(.05)\n")
                result = json.loads(execute_code(code, task_id=task_id))
                if tool == "execute_code_active" and run_is_cancelled(task_id):
                    assert result["status"] == "interrupted", result
                    return {"interrupted": True, "completed": False}
                assert result["status"] == "success", result
                result = json.loads(execute_code("print(value + 1)", task_id=task_id))
                assert result["status"] == "success" and result["output"].strip() == "42", result
            completed_models.add(task_id)
            return {"final_response": "model finished", "completed": True}
        agent.run_conversation.side_effect = run
        return agent
    monkeypatch.setattr(adapter, "_create_agent", create_agent)
    def run_is_cancelled(run_id):
        return run_id in adapter._stopping_run_ids
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    app.router.add_post("/v1/runs/stop", adapter._handle_stop_admission)
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    headers = {"Authorization": "Bearer fixture-key"}
    with profile_scope(home):
        try:
            async with TestClient(TestServer(app)) as client:
                capability = (await (await client.get("/v1/capabilities", headers=headers)).json())["features"]["runs_execution_context"]
                assert context["lifetime"] in capability["lifetimes"]
                for cancel in (False, True):
                    response = await client.post("/v1/runs", headers={**headers, "Idempotency-Key": str(cancel)},
                                                 json={"input": "work", "execution_context": context})
                    assert response.status == 202, await response.text()
                    run_id = (await response.json())["run_id"]
                    deadline = time.monotonic() + 45
                    while not (data / run_id).exists() or (tool != "execute_code_active" and run_id not in completed_models):
                        assert adapter._run_statuses[run_id]["status"] not in {"failed", "cancelled"}, adapter._run_statuses[run_id]
                        assert time.monotonic() < deadline, adapter._run_statuses[run_id]
                        await asyncio.sleep(.05)
                    status = await (await client.get(f"/v1/runs/{run_id}", headers=headers)).json()
                    assert status["status"] == "running", status
                    assert run_id in adapter._active_run_tasks
                    if tool == "execute_code":
                        with pytest.raises(asyncio.TimeoutError):
                            await asyncio.wait_for(asyncio.shield(adapter._active_run_tasks[run_id]), 2)
                    if cancel and tool == "terminal":
                        stopped = await client.post("/v1/runs/stop", headers={**headers, "Idempotency-Key": str(cancel)},
                                                    json={"input": "work", "execution_context": context})
                        assert stopped.status == 200, await stopped.text()
                        assert (await stopped.json())["status"] == "stopping"
                    elif cancel:
                        stopped = await client.post(f"/v1/runs/{run_id}/stop", headers=headers)
                        assert (await stopped.json())["status"] == "stopping"
                    else:
                        (data / ("release-" + run_id)).touch()
                    await asyncio.wait_for(asyncio.gather(*adapter._active_run_tasks.values()), 20)
                    assert adapter._run_statuses[run_id]["status"] == (
                        "cancelled" if cancel else "completed"
                    ), adapter._run_statuses[run_id]
                    assert run_id not in adapter._active_run_agents
                    if tool != "terminal":
                        from tools.code_kernel_remote import _REMOTE_KERNELS
                        assert not any(run_id in str(part) for key in _REMOTE_KERNELS for part in key)
        finally:
            for task in tasks:
                (data / ("release-" + task)).touch()
            for run_id in list(adapter._active_run_tasks):
                adapter._stopping_run_ids.add(run_id)
            if adapter._active_run_tasks:
                await asyncio.wait_for(asyncio.gather(*adapter._active_run_tasks.values()), 20)
            for task in tasks:
                cleanup_vm(task)


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
@pytest.mark.parametrize("backend", ["local", "ssh"])
async def test_bound_agent_edits_the_selected_directory_with_real_tools(tmp_path, monkeypatch, backend, ssh_target):
    from gateway.run import _profile_runtime_scope
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
    homes = {}

    @web.middleware
    async def routed_profile(request, handler):
        with _profile_runtime_scope(homes[request.headers["Test-Profile"]], hydrate_secrets=False):
            return await handler(request)

    app = web.Application(middlewares=[routed_profile])
    app.router.add_post("/v1/runs", adapter._handle_runs)
    with FakeLLMServer(api_key="sk-fake-e2e") as provider:
        for name in ("a", "b"):
            home = tmp_path / name
            workspace = tmp_path / f"data-{name}"
            workspace.mkdir()
            (workspace / "personal.txt").write_text("keep me")
            homes[name] = write_hermes_home(home, provider.base_url, extra_config=(
                "tools:\n  api_server:\n    enabled: [terminal, file]\n"
                "terminal:\n"
                f"  backend: {backend}\n  cwd: {workspace}\n"
                f"  ssh_host: {ssh_target['host']}\n  ssh_port: {ssh_target['port']}\n"
                f"  ssh_user: {ssh_target['user']}\n  ssh_key: {ssh_target['key']}\n"
                f"  ssh_hermes_home: {home / 'remote-state'}\n"
            ))
            # Pair this fixture credential with its loopback endpoint. An unbound
            # OPENAI_API_KEY must never be forwarded to an arbitrary custom host.
            with (home / ".env").open("a") as secrets:
                secrets.write(f"OPENAI_BASE_URL={provider.base_url}\n")
        async with TestClient(TestServer(app)) as client:
            for turn, name in enumerate(("a", "b", "a")):
                workspace = tmp_path / f"data-{name}"
                context = {"version": 1, "backend": backend, "cwd": str(workspace)}
                if backend == "ssh":
                    context["ssh"] = {"host": ssh_target["host"], "port": ssh_target["port"], "user": ssh_target["user"]}
                # File first: this must select the pinned backend even without an
                # existing terminal environment. Terminal then sees that same file.
                if turn == 2:
                    provider.push(
                        ToolCall("read_file", {"path": "result.txt"}),
                        ToolCall("patch", {"mode": "replace", "path": "result.txt", "old_string": "a-0", "new_string": "a-2"}),
                    )
                else:
                    provider.push(ToolCall("write_file", {"path": "result.txt", "content": f"{name}-{turn}"}))
                provider.push(
                    ToolCall("terminal", {"command": "pwd; cat result.txt; cd .."}),
                    Text("done"),
                )
                response = await client.post("/v1/runs", headers={
                    "Authorization": "Bearer fixture-key", "Test-Profile": name,
                }, json={"input": "Update result.txt", "session_id": f"maintenance-{name}", "execution_context": context})
                assert response.status == 202, await response.text()
                run_id = (await response.json())["run_id"]
                await asyncio.wait_for(asyncio.gather(*adapter._active_run_tasks.values()), 30)
                status = adapter._run_statuses[run_id]
                assert status["status"] == "completed", status
                assert (workspace / "result.txt").read_text() == f"{name}-{turn}"
                assert (workspace / "personal.txt").read_text() == "keep me"
                assert (workspace / "result.txt").stat().st_uid == os.getuid()
                tools = [m for m in provider.main_requests()[-1]["messages"] if m["role"] == "tool"]
                assert str(workspace) in tools[-1]["content"]
                assert f"{name}-{turn}" in tools[-1]["content"]
        assert (tmp_path / "data-b" / "result.txt").read_text() == "b-1"
    adapter._run_idempotency_store.close()


@pytest.mark.asyncio
async def test_bound_run_cannot_be_handed_to_a_live_owner(tmp_path, monkeypatch):
    from tools import bot_live_delivery

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
    (tmp_path / "config.yaml").write_text(f"terminal:\n  backend: local\n  cwd: {tmp_path}\n")
    delivered = MagicMock()
    monkeypatch.setattr(bot_live_delivery, "find_canonical_live_owner", lambda home: {"session_id": "held"})
    monkeypatch.setattr(bot_live_delivery, "deliver_to_live_owner", delivered)
    with profile_scope(tmp_path):
        db = await adapter._ensure_session_db_async()
        db.create_session("held", source="api_server")
        app = web.Application()
        app.router.add_post("/v1/runs", adapter._handle_runs)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/v1/runs", headers={"Authorization": "Bearer fixture-key"}, json={
                "input": "work", "session_id": "held",
                "execution_context": {"version": 1, "backend": "local", "cwd": str(tmp_path)},
            })
            assert response.status == 409, await response.text()
            assert not adapter._active_run_tasks
            delivered.assert_not_called()
