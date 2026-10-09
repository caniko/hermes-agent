"""A killed API worker cannot turn target-side jobs into a terminal receipt."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import hermes_yaml as yaml
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import _profile_runtime_scope
from tools.environments.supervised_execution import SupervisionBinding, local_supervisor
from tools.environments.local import build_subprocess_env
from tools.environments.job_supervision import JobState


@pytest.mark.asyncio
@pytest.mark.platforms("linux")
async def test_restarted_gateway_stops_orphan_jobs_before_terminal_status(tmp_path, monkeypatch):
    from tools.process_registry import systemd_user_bus_env

    env = systemd_user_bus_env(build_subprocess_env())
    if subprocess.run(["systemctl", "--user", "show", "--property=Version"],
                      env=env, capture_output=True).returncode:
        pytest.skip("a systemd user manager is required")
    home = tmp_path / "worker"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"terminal": {"backend": "local", "cwd": str(tmp_path)}}))
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["__ETC_PROFILE_SOURCED"] = "1"
    port_file = tmp_path / "port"
    headers = {"Authorization": "Bearer fixture-key"}
    child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("supervised_run_worker.py")),
                              str(home), str(port_file)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    supervisor = None
    try:
        deadline = time.monotonic() + 30
        while not port_file.exists():
            assert child.poll() is None, child.stderr.read().decode()
            assert time.monotonic() < deadline
            await asyncio.sleep(.05)
        async with ClientSession(headers=headers) as client:
            url = f"http://127.0.0.1:{port_file.read_text()}"
            response = await client.post(url + "/v1/runs", headers={"Idempotency-Key": "crash"}, json={
                "input": "start owned job", "execution_context": {
                    "version": 1, "backend": "local", "cwd": str(tmp_path), "lifetime": "wait_for_jobs"}})
            assert response.status == 202, await response.text()
            run_id = (await response.json())["run_id"]
            supervisor = local_supervisor(SupervisionBinding(str(home / "run-jobs" / run_id)), env)
            while True:
                status = await (await client.get(url + f"/v1/runs/{run_id}")).json()
                assert status["status"] not in {"failed", "completed"}, status
                if status.get("last_event") == "run.waiting_for_jobs":
                    break
                assert time.monotonic() < deadline
                await asyncio.sleep(.05)
        assert any(supervisor.inspect(job) is JobState.RUNNING for job in supervisor.jobs())
        child.kill()
        child.wait(timeout=10)
        with _profile_runtime_scope(home):
            adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
            app = web.Application()
            app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
            async with TestClient(TestServer(app)) as client:
                initial = await (await client.get(f"/v1/runs/{run_id}", headers=headers)).json()
                assert initial["status"] == "stopping", initial
                await asyncio.wait_for(asyncio.gather(*adapter._active_run_tasks.values()), 20)
                final = await (await client.get(f"/v1/runs/{run_id}", headers=headers)).json()
                assert final["status"] == "interrupted", final
                assert supervisor.settled()
            adapter._run_idempotency_store.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stderr.close()
        if supervisor is not None:
            supervisor.stop()
