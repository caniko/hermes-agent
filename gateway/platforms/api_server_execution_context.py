"""Optional /v1/runs precondition for a worker's configured terminal target.

This pins existing policy; clients cannot supply credentials or reconfigure a
worker. It describes the initial terminal/file backend, not filesystem confinement.
"""

import os
import posixpath
import shlex
from contextlib import contextmanager
from dataclasses import dataclass

from tools.terminal_scope import get_terminal_scope, reset_terminal_scope, set_terminal_scope


class ExecutionContextError(ValueError):
    def __init__(self, message: str, *, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ExecutionContext:
    requested: dict
    terminal_policy: dict[str, str]


def capture_execution_context(raw) -> ExecutionContext:
    from tools.terminal_tool import _get_env_config

    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ExecutionContextError("execution_context requires version 1", status=400)
    backend, cwd = raw.get("backend"), raw.get("cwd")
    keys = {"version", "backend", "cwd"} | ({"ssh"} if backend == "ssh" else set())
    if backend not in ("local", "ssh") or set(raw) != keys:
        raise ExecutionContextError("execution_context requires backend local or ssh and its target fields", status=400)
    path_module = posixpath if backend == "ssh" else os.path
    if not isinstance(cwd, str) or not path_module.isabs(cwd) or "\0" in cwd:
        raise ExecutionContextError("execution_context.cwd must be an absolute target path", status=400)
    if backend == "ssh":
        ssh = raw.get("ssh")
        if (not isinstance(ssh, dict) or set(ssh) != {"host", "port", "user"}
                or any(not isinstance(ssh[k], str) or not ssh[k].strip() for k in ("host", "user"))
                or type(ssh["port"]) is not int or not 1 <= ssh["port"] <= 65535):
            raise ExecutionContextError("execution_context.ssh requires host, user and port", status=400)

    # Read through the same resolver as terminal/file tools (including the CLI bridge).
    config = _get_env_config()
    actual = {"version": 1, "backend": config["env_type"], "cwd": config["cwd"]}
    if actual["backend"] == "ssh":
        actual["ssh"] = {"host": config["ssh_host"], "user": config["ssh_user"], "port": config["ssh_port"]}
    # Exact spelling preserves symlink/.. semantics and never resolves a remote path locally.
    if actual != raw:
        raise ExecutionContextError("execution_context does not match the worker's configured terminal target")
    if backend == "local" and not os.path.isdir(cwd):
        raise ExecutionContextError("execution_context.cwd is not an existing directory")
    scope = get_terminal_scope()
    policy = dict(scope) if scope is not None else {k: v for k, v in os.environ.items() if k.startswith("TERMINAL_")}
    policy.update(TERMINAL_ENV=backend, TERMINAL_CWD=cwd)
    return ExecutionContext(actual, policy)


def verify_execution_directory(context: ExecutionContext) -> None:
    """Check the pinned target before constructing an agent, on its own host."""
    cwd = context.requested["cwd"]
    if context.requested["backend"] == "local":
        if not os.path.isdir(cwd):
            raise ExecutionContextError("execution_context.cwd is not an existing directory")
        return
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_backends import _build_ssh_env, _ssh_config_from_config

    env = _build_ssh_env(cwd=cwd, timeout=10, probe_only=True,
                         ssh_config=_ssh_config_from_config(_get_env_config()))
    try:
        result = env._run_ssh(f"test -d {shlex.quote(cwd)}", timeout=10)
        if result.returncode != 0:
            raise ExecutionContextError("execution_context.cwd is not an accessible SSH directory")
    finally:
        env.cleanup()


@contextmanager
def bind_execution_context(context: ExecutionContext | None):
    if context is None:
        yield
        return
    token = set_terminal_scope(context.terminal_policy)
    try:
        # Scope re-entry and executor handoff retain the admitted policy rather
        # than re-reading a file that may have changed while the run was queued.
        yield
    finally:
        reset_terminal_scope(token)
