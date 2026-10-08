"""Ownership request transport works without loading a terminal backend."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

import pytest


@pytest.mark.platforms("linux")
def test_request_client_needs_only_stdlib_and_authenticates_the_peer(tmp_path):
    import tools

    from tools.environments.filesystem_authority import FilesystemAuthority
    from tools.environments.filesystem_authority_server import AuthorityServer
    from tools.environments.filesystem_claims import ClaimStore

    state, root = tmp_path / "authority", tmp_path / "maintained"
    ClaimStore.initialize(state)
    root.mkdir()
    authority = FilesystemAuthority(
        state,
        {
            os.getuid(): {
                "id": "controller",
                "execution_uid": os.getuid(),
                "roots": [str(root)],
            }
        },
    )
    message = {
        "version": 1,
        "authority": authority.store.authority_id,
        "principal": "controller",
        "op": "capabilities",
    }
    # A site-free interpreter cannot accidentally satisfy the client with the
    # gateway's terminal/config dependencies. The target protocol is stdlib-only.
    command = [
        sys.executable,
        "-S",
        "-m",
        "tools.environments.filesystem_authority_server",
        "request",
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="hfo-") as sockets:
            socket = str(Path(sockets) / "control")
            with AuthorityServer(socket, authority) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:

                    def request(payload):
                        result = subprocess.run(
                            command + ["--socket", socket],
                            input=json.dumps(payload) + "\n",
                            capture_output=True,
                            text=True,
                            cwd=Path(tools.__file__).resolve().parent.parent,
                            timeout=10,
                        )
                        assert result.returncode == 0, result.stderr
                        return json.loads(result.stdout)

                    accepted = request(message)
                    assert accepted == {
                        "ok": True,
                        "result": {
                            "version": 1,
                            "authority": authority.store.authority_id,
                        },
                    }
                    rejected = request({**message, "principal": "another-controller"})
                    assert rejected["ok"] is False and rejected["code"] == "rejected"
                finally:
                    server.shutdown()
                    thread.join(10)
    finally:
        authority.close()
