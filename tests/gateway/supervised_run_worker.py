"""Disposable API process for crash/reconnect lifecycle qualification."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

from aiohttp import web

from agent.secret_scope import set_multiplex_active
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import _profile_runtime_scope
from tools.terminal_tool import terminal_tool


async def main(home: Path, port_file: Path):
    set_multiplex_active(True)
    with _profile_runtime_scope(home):
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
        def create_agent(**kwargs):
            agent = MagicMock()
            agent.session_id = kwargs["session_id"]
            def run(task_id, **_):
                result = json.loads(terminal_tool(command="sleep 300", background=True, task_id=task_id))
                assert result.get("exit_code") == 0, result
                return {"final_response": "model finished", "completed": True}
            agent.run_conversation.side_effect = run
            return agent
        adapter._create_agent = create_agent
        app = web.Application()
        app.router.add_post("/v1/runs", adapter._handle_runs)
        app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port_file.write_text(str(site._server.sockets[0].getsockname()[1]))
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
