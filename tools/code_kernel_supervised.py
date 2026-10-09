"""Run-scoped kernels using the existing file-cell protocol and target supervisor."""

import base64
import shlex
import uuid

from tools.environments.job_supervision import SupervisionError
from tools.environments.supervised_execution import SupervisedProcessHandle, bind_job_supervision, local_supervisor


def _stage_kernel_files(env, directory, runner, tools_module, env_content):
    """Stage private kernel files in one admitted job; payloads travel on stdin."""
    from tools.code_execution_rpc import _execute_checked, _private_dirs_cmd

    command = _private_dirs_cmd(directory, f"{directory}/cells", f"{directory}/rpc")
    payloads = []
    for name, content in (("kernel_runner.py", runner), ("hermes_tools.py", tools_module),
                          ("kernel.env", env_content)):
        path = shlex.quote(f"{directory}/{name}")
        command += (f' && IFS= read -r payload && printf "%s" "$payload" | base64 -d > {path}'
                    f" && chmod 600 -- {path}")
        payloads.append(base64.b64encode(content.encode("utf-8")).decode("ascii"))
    _execute_checked(env, command, "supervised kernel staging", timeout=15,
                     stdin_data="\n".join(payloads) + "\n")


def spawn_supervised_kernel(binding, env, env_type, owner, task_env_id, sandbox_tools, idle_exit):
    from tools.code_execution_env import _build_child_env, _resolve_child_cwd, _resolve_child_python
    from tools.code_execution_tool import (
        MAX_STDOUT_BYTES, _get_execution_mode, _remote_env_content,
        generate_hermes_tools_module,
    )
    from tools.code_kernel import RUNNER_CELL_SOURCE
    from tools.code_kernel_remote import REMOTE_KERNEL_RUNNER_SOURCE, RemoteKernel
    import secrets

    # Authority control state is not exposed to workloads. An owned binding
    # stages files in the separately granted execution-host runtime instead.
    staging = getattr(binding.supervisor, "runtime_dir", None) or binding.state_dir
    directory = f"{staging}/kernel-{uuid.uuid4().hex}"
    token = secrets.token_urlsafe(32)
    _stage_kernel_files(env, directory, REMOTE_KERNEL_RUNNER_SOURCE.format(
        cell_source=RUNNER_CELL_SOURCE, capture_limit=MAX_STDOUT_BYTES, idle_exit=idle_exit),
        generate_hermes_tools_module(list(sandbox_tools), transport="file"),
        _remote_env_content(f"{directory}/rpc", token, HERMES_KERNEL_DIR=directory, PYTHONPATH=directory))
    mode = _get_execution_mode()
    if env_type == "local" and binding.supervisor is None:
        python = _resolve_child_python(mode)
        cwd = _resolve_child_cwd(mode, directory, task_id=task_env_id) or directory
        values = _build_child_env(rpc_endpoint="", rpc_token=token, tmpdir=directory, child_python=python)
        values.update(HERMES_RPC_DIR=f"{directory}/rpc", HERMES_KERNEL_DIR=directory)
        supervisor = local_supervisor(binding, values)
        job = supervisor.start(shlex.join([python, f"{directory}/kernel_runner.py"]),
                               cwd=cwd, environment_names=tuple(values))
        process = SupervisedProcessHandle(supervisor, job)
    else:
        from agent.runtime_cwd import scope_terminal_cwd
        from tools.terminal_tool import get_session_cwd
        # File-cell staging probes use cwd="/". They must not choose the
        # script's project cwd, and a remote path must not be checked locally.
        cwd = directory if mode == "strict" else (get_session_cwd(task_env_id) or scope_terminal_cwd())
        command = (f"cd {shlex.quote(directory)} && ( set -a && . ./kernel.env && set +a && "
                   f"rm -f ./kernel.env && cd -- {shlex.quote(cwd)} && "
                   f"exec python3 {shlex.quote(directory + '/kernel_runner.py')} )")
        process = env._run_bash(command)
    # Cells return through files. Drain diagnostics without an unconsumed pipe
    # blocking settlement; the supervisor retains the original output on target.
    process.stdout.close()
    kernel = RemoteKernel(env=env, env_type=env_type, kernel_dir=directory, pid="", rpc_token=token,
                          owner=owner, supervised_process=process)
    binding.kernels.append(kernel)
    return kernel


def finish_supervised_kernel(kernel):
    """Exit the idle interpreter, leaving its descendants in the owned cgroup."""
    from tools.code_execution_rpc import _execute_checked
    from tools.code_kernel_remote import _REGISTRY

    process = kernel.supervised_process
    if process.supervisor.main_exit_code(process.job) is None:
        # Trusted lifecycle control must work even after tool admission is sealed.
        with bind_job_supervision(None):
            try:
                _execute_checked(kernel.env, "touch -- " + shlex.quote(kernel.kernel_dir + "/stop"),
                                 "supervised kernel close", timeout=15)
            except (RuntimeError, OSError) as exc:
                raise SupervisionError("kernel shutdown acknowledgement is unavailable") from exc
    with _REGISTRY.lock:
        for key, value in list(_REGISTRY.kernels.items()):
            if value is kernel:
                _REGISTRY.kernels.pop(key)
