"""Recovered approvals keep stop authority until their executor workers settle (#113639)."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.platforms.api_server as api_server
from gateway.config import PlatformConfig
from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
from gateway.platforms.api_server_runs import _schedule_recovery_run
from hermes_state import SessionDB


@pytest.fixture
def recovery(tmp_path, request, monkeypatch):
    bound = getattr(request, "param", None) == "bound"
    if bound:
        (tmp_path / "config.yaml").write_text(f"terminal:\n  backend: local\n  cwd: {tmp_path}\n")
        monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda profile: tmp_path)
    adapter = api_server.APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "recovery-test-key"})
    )
    adapter._run_idempotency_store.close()
    path = tmp_path / "runs.db"
    store = adapter._run_idempotency_store = RunIdempotencyStore(str(path))
    headers = {"Authorization": "Bearer recovery-test-key"}
    scope = adapter._run_idempotency_scope(SimpleNamespace(headers=headers))
    parent, run_id, session_id = "run-parent", "run-successor", "session-recovery"
    db = adapter._session_db = SessionDB(tmp_path / "sessions.db")
    db.create_session(session_id, source="api_server")
    db.append_message(session_id, "user", "Run the approved tool.")
    db.append_message(session_id, "assistant", tool_calls=[{
        "id": "call-a", "type": "function",
        "function": {"name": "terminal", "arguments": '{"command":"probe"}'},
    }])
    store.reserve(scope, "probe", "fingerprint", parent,
                  {"run_id": parent, "status": "waiting_for_approval"})
    store.save_run_launch(parent, {
        "session_id": session_id, "agent_kwargs": {}, "request_profile": "default",
        **({"execution_context": {"version": 1, "backend": "local", "cwd": str(tmp_path)}} if bound else {}),
    })
    store.record_approval_request(
        parent, {"run_id": parent, "status": "waiting_for_approval"},
        {"event": "approval.request", "request_id": "approval-a"},
        tool={"tool_call_id": "call-a", "tool_name": "terminal", "tool_args": {"command": "probe"}},
    )
    store.resolve_approval(scope, parent, "approval-a", "once", applied=False, resolved=0)
    plan = store.reserve_recovery_successor(
        scope, parent, successor_run_id=run_id,
        owner_pid=adapter._run_owner_pid, owner_started=adapter._run_owner_started,
    )
    agents = [MagicMock(session_id=session_id), MagicMock(session_id=session_id)]
    for agent in agents:
        agent.interrupted = threading.Event()
        agent.interrupt.side_effect = lambda *args, _agent=agent, **kwargs: _agent.interrupted.set()
        for name in ("session_prompt_tokens", "session_completion_tokens", "session_total_tokens"):
            setattr(agent, name, 0)
    adapter._create_agent = MagicMock(side_effect=agents)
    app = web.Application()
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    yield SimpleNamespace(
        adapter=adapter, store=store, db=db, path=path, scope=scope, plan=plan,
        parent=parent, run_id=run_id, session_id=session_id, agents=agents, app=app, headers=headers,
        cwd=str(tmp_path),
    )
    store.close()
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_dispatch", "during_tool", "after_tool", "during_final"])
@pytest.mark.parametrize("shutdown", [False, True], ids=["stop", "shutdown"])
async def test_stop_fences_recovery_and_survives_reopening(recovery, tmp_path, monkeypatch, phase, shutdown):
    case = recovery
    loop = asyncio.get_running_loop()
    reached = asyncio.Event()
    release = threading.Event()
    effect = tmp_path / "effect"

    def pause():
        loop.call_soon_threadsafe(reached.set)
        assert release.wait(10)

    get_database = case.adapter._ensure_session_db_async

    async def database():
        if phase == "before_dispatch":
            reached.set()
            assert await asyncio.to_thread(release.wait, 10)
        return await get_database()

    def invoke(agent, *args, **kwargs):
        if phase == "during_tool":
            pause()
        if not agent.interrupted.is_set():
            effect.write_text("one execution")
        return "tool settled"

    append_message = case.db.append_message

    def append(*args, **kwargs):
        result = append_message(*args, **kwargs)
        if phase == "after_tool":
            pause()
        return result

    def final(**kwargs):
        if phase == "during_final":
            pause()
        # Stop authority must win even when a provider returns an ordinary result.
        return {"final_response": "done"}

    monkeypatch.setattr(case.adapter, "_ensure_session_db_async", database)
    monkeypatch.setattr(case.db, "append_message", append)
    monkeypatch.setattr("agent.agent_runtime_helpers.invoke_tool", invoke)
    case.agents[1].run_conversation.side_effect = final
    task = None
    try:
        async with TestClient(TestServer(case.app)) as client:
            assert _schedule_recovery_run(case.adapter, case.plan, _api_server=api_server)
            task = case.adapter._active_run_tasks[case.run_id]
            await asyncio.wait_for(reached.wait(), 10)
            denied = await client.post(f"/v1/runs/{case.run_id}/stop")
            assert denied.status == 401
            assert case.run_id not in case.adapter._stopping_run_ids
            if shutdown:
                case.adapter.interrupt_active_runs("Gateway shutdown")
            else:
                stopped = await client.post(f"/v1/runs/{case.run_id}/stop", headers=case.headers)
                assert stopped.status == 200
                assert (await stopped.json())["status"] == "stopping"
            assert case.adapter.active_agent_work_count() == 1
            assert not task.done()
            assert not _schedule_recovery_run(case.adapter, case.plan, _api_server=api_server)
            release.set()
            await asyncio.wait_for(asyncio.shield(task), 10)
            status = await client.get(f"/v1/runs/{case.run_id}", headers=case.headers)
            expected = "interrupted" if shutdown else "cancelled"
            assert (await status.json())["status"] == expected
        assert effect.exists() == (phase in {"after_tool", "during_final"})
        if phase in {"during_tool", "during_final"}:
            assert case.agents[phase == "during_final"].interrupted.is_set()
        if phase != "during_final":
            case.agents[1].run_conversation.assert_not_called()
        assert case.adapter.active_agent_work_count() == 0
        assert case.run_id not in case.adapter._active_run_agents
        case.store.close()
        reopened = RunIdempotencyStore(str(case.path))
        try:
            assert reopened.status_for_run(case.scope, case.run_id)["status"]["status"] == expected
            events = reopened.events_after(case.scope, case.run_id, 0)
            assert events[-1]["event"] == f"run.{expected}"
            assert not any(event["event"] == "run.completed" for event in events)
        finally:
            reopened.close()
    finally:
        release.set()
        if task is not None:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_start", "tool_result", "tool_error", "reap_error", "final"])
async def test_task_cancellation_retains_worker_and_dispatch_evidence(recovery, monkeypatch, phase):
    case = recovery
    loop = asyncio.get_running_loop()
    reached, exited = asyncio.Event(), asyncio.Event()
    release = threading.Event()

    def worker():
        loop.call_soon_threadsafe(reached.set)
        try:
            assert release.wait(10)
            if phase == "tool_error":
                raise RuntimeError("worker exited without a tool receipt")
            return "tool settled"
        finally:
            loop.call_soon_threadsafe(exited.set)

    monkeypatch.setattr("agent.agent_runtime_helpers.invoke_tool",
                        lambda *args, **kwargs: "tool settled" if phase == "final" else worker())
    case.agents[1].run_conversation.side_effect = lambda **kwargs: {"final_response": worker()}
    if phase == "reap_error":
        monkeypatch.setattr(api_server, "_reap_disconnected_agent_processes",
                            MagicMock(side_effect=RuntimeError("reaper unavailable")))
    assert _schedule_recovery_run(case.adapter, case.plan, _api_server=api_server)
    task = case.adapter._active_run_tasks[case.run_id]
    try:
        if phase != "before_start":
            await asyncio.wait_for(reached.wait(), 10)
        task.cancel()
        if phase != "before_start":
            agent = case.agents[phase == "final"]
            assert await asyncio.to_thread(agent.interrupted.wait, 5)
            task.cancel()  # Repeated shutdown cancellation must not release a live worker.
            async with TestClient(TestServer(case.app)) as client:
                status = await client.get(f"/v1/runs/{case.run_id}", headers=case.headers)
                assert (await status.json())["status"] == "stopping"
            assert not task.done()
            assert case.adapter.active_agent_work_count() == 1
            assert case.adapter._active_run_agents[case.run_id] is agent
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
        expected = "unrecoverable" if phase == "tool_error" else "cancelled"
        status = case.store.status_for_run(case.scope, case.run_id)["status"]
        assert status["status"] == expected
        if phase == "tool_error":
            assert status["intervention_reason"] == "tool_effect_uncertain"
        if phase != "before_start":
            assert exited.is_set()
        if phase in {"tool_result", "reap_error", "final"}:
            snapshot = case.store.recovery_snapshot(case.scope, case.parent)
            assert snapshot["tool_result"] == {"output": "tool settled"}
        assert case.run_id not in case.adapter._active_run_tasks
        assert case.adapter.active_agent_work_count() == 0
        assert not _schedule_recovery_run(case.adapter, case.plan, _api_server=api_server)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
        if phase != "before_start" and reached.is_set():
            await asyncio.wait_for(exited.wait(), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["bound"], indirect=True)
@pytest.mark.parametrize("drift", [False, True], ids=["retained-context", "changed-worker"])
async def test_recovery_preserves_admitted_execution_context(recovery, monkeypatch, tmp_path, drift):
    from tools.terminal_scope import get_terminal_scope

    case = recovery
    if drift:
        changed = tmp_path / "different-workspace"
        changed.mkdir()
        (tmp_path / "config.yaml").write_text(f"terminal:\n  backend: local\n  cwd: {changed}\n")
    seen = []

    def invoke(*args, **kwargs):
        seen.append(get_terminal_scope()["TERMINAL_CWD"])
        return "pinned tool"

    def final(**kwargs):
        seen.append(get_terminal_scope()["TERMINAL_CWD"])
        return {"final_response": "pinned final"}

    monkeypatch.setattr("agent.agent_runtime_helpers.invoke_tool", invoke)
    case.agents[1].run_conversation.side_effect = final
    assert _schedule_recovery_run(case.adapter, case.plan, _api_server=api_server)
    task = case.adapter._active_run_tasks[case.run_id]
    await asyncio.wait_for(asyncio.shield(task), 10)
    status = case.store.status_for_run(case.scope, case.run_id)["status"]
    assert status["status"] == ("failed" if drift else "completed")
    assert seen == ([] if drift else [case.cwd, case.cwd])
    if drift:
        case.adapter._create_agent.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_parent_stop_is_durable_and_rejects_delayed_approval(recovery):
    case = recovery
    async with TestClient(TestServer(case.app)) as client:
        stopped = await client.post(f"/v1/runs/{case.parent}/stop", headers=case.headers)
        assert stopped.status == 200
        assert case.store.stop_requested(case.parent)
        assert case.store.stop_requested(case.run_id)
        case.store.close()
        case.store = case.adapter._run_idempotency_store = RunIdempotencyStore(str(case.path))
        approval = await client.post(f"/v1/runs/{case.parent}/approval", headers=case.headers,
                                     json={"request_id": "approval-a", "choice": "once"})
        assert approval.status == 409
        assert (await approval.json())["error"]["code"] == "run_stopped"
        assert not _schedule_recovery_run(case.adapter, case.plan, _api_server=api_server)
        case.adapter._create_agent.assert_not_called()


@pytest.mark.asyncio
async def test_live_successor_observes_stop_from_independent_store(recovery, monkeypatch):
    case = recovery
    reached = asyncio.Event()
    loop = asyncio.get_running_loop()

    def invoke(agent, *args, **kwargs):
        loop.call_soon_threadsafe(reached.set)
        assert agent.interrupted.wait(10)
        return "settled independent stop"

    monkeypatch.setattr("agent.agent_runtime_helpers.invoke_tool", invoke)
    assert _schedule_recovery_run(case.adapter, case.plan, _api_server=api_server)
    task = case.adapter._active_run_tasks[case.run_id]
    await asyncio.wait_for(reached.wait(), 10)
    other = RunIdempotencyStore(str(case.path))
    try:
        assert case.run_id in other.stop_lineage(case.scope, case.parent)
        await asyncio.wait_for(asyncio.shield(task), 10)
        assert other.status_for_run(case.scope, case.run_id)["status"]["status"] == "cancelled"
        case.agents[1].run_conversation.assert_not_called()
    finally:
        case.agents[0].interrupted.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
        other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["bound"], indirect=True)
@pytest.mark.parametrize("cancel", [False, True], ids=["http-stop", "task-cancel"])
async def test_stop_during_recovery_ownership_preparation_retains_settlement(recovery, monkeypatch, cancel):
    from gateway.platforms.api_server_job_lifetime import RunJobLifetime
    from tools.environments.filesystem_supervisor import OwnershipPending
    from tools.environments.supervised_execution import SupervisionBinding

    case = recovery
    case.plan["launch"]["execution_context"]["lifetime"] = "wait_for_jobs"
    entered, collecting, collected = asyncio.Event(), asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    supervisor = MagicMock()

    def prepare():
        loop.call_soon_threadsafe(entered.set)
        raise OwnershipPending("another claim holds the selected directory")

    def settled():
        loop.call_soon_threadsafe(collecting.set)
        return collected.is_set()

    supervisor.prepare.side_effect = prepare
    supervisor.settled.side_effect = settled
    lifetime = RunJobLifetime(SupervisionBinding(case.cwd), supervisor)
    monkeypatch.setattr("gateway.platforms.api_server_recovery_execution_context.create_job_lifetime",
                        lambda *args: lifetime)
    assert _schedule_recovery_run(case.adapter, case.plan, _api_server=api_server)
    task = case.adapter._active_run_tasks[case.run_id]
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with TestClient(TestServer(case.app)) as client:
            if cancel:
                task.cancel()
            else:
                receipt = await (await client.post(f"/v1/runs/{case.parent}/stop", headers=case.headers)).json()
                assert receipt["lineage_settled"] is False
            await asyncio.wait_for(collecting.wait(), 10)
            assert not task.done()
            case.adapter._create_agent.assert_not_called()
            assert lifetime.prepared
            task.cancel()
            collected.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
            receipt = await (await client.post(f"/v1/runs/{case.parent}/stop", headers=case.headers)).json()
            assert receipt["lineage_settled"] is True
            assert case.store.stop_requested(case.run_id)
            assert case.store.status_for_run(case.scope, case.run_id)["status"]["status"] == "cancelled"
    finally:
        collected.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("during_tool", [False, True], ids=["before-dispatch", "live-tool"])
async def test_stop_terminal_parent_joins_live_successor(recovery, monkeypatch, during_tool):
    case = recovery
    reached, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = case.adapter._ensure_session_db_async

    async def database():
        if not during_tool:
            reached.set()
            assert await asyncio.to_thread(release.wait, 10)
        return await original()

    def invoke(agent, *args, **kwargs):
        loop.call_soon_threadsafe(reached.set)
        assert release.wait(10)
        assert agent.interrupted.is_set()
        return "stopped tool"

    monkeypatch.setattr(case.adapter, "_ensure_session_db_async", database)
    monkeypatch.setattr("agent.agent_runtime_helpers.invoke_tool", invoke)
    assert _schedule_recovery_run(case.adapter, case.plan, _api_server=api_server)
    task = case.adapter._active_run_tasks[case.run_id]
    try:
        await asyncio.wait_for(reached.wait(), 10)
        async with TestClient(TestServer(case.app)) as client:
            stopped = await client.post(f"/v1/runs/{case.parent}/stop", headers=case.headers)
            receipt = await stopped.json()
            assert stopped.status == 200
            assert receipt["lineage_settled"] is False
            assert not task.done()
            release.set()
            await asyncio.wait_for(asyncio.shield(task), 10)
            stopped = await client.post(f"/v1/runs/{case.parent}/stop", headers=case.headers)
            assert (await stopped.json())["lineage_settled"] is True
        assert case.store.status_for_run(case.scope, case.run_id)["status"]["status"] == "cancelled"
        case.agents[1].run_conversation.assert_not_called()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
