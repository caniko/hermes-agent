"""Kernel setup keeps payloads private while paying one target admission."""

import io
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

from tools.code_kernel_supervised import spawn_supervised_kernel
from tools.environments.supervised_execution import SupervisionBinding


@pytest.mark.parametrize("backend", ["local", "ssh"])
def test_owned_kernel_stays_in_the_granted_runtime(tmp_path, monkeypatch, backend):
    from tools import code_execution_tool

    monkeypatch.setattr(code_execution_tool, "_load_config", lambda: {"mode": "strict"})
    runtime = tmp_path / "granted runtime"
    runtime.mkdir()
    launches = []
    staging_commands = []

    class TargetEnvironment:
        def execute(self, command, *, cwd, timeout, stdin_data=None):
            # Refuse the former root-level staging before it can touch the host.
            assert str(runtime) in command, "kernel files escaped the granted runtime"
            staging_commands.append(command)
            result = subprocess.run(["bash", "--noprofile", "--norc", "-c", command],
                                    input=stdin_data, cwd=cwd, timeout=timeout,
                                    capture_output=True, text=True)
            return {"returncode": result.returncode, "output": result.stdout + result.stderr}

        def _run_bash(self, command):
            launches.append(command)
            return SimpleNamespace(stdout=io.BytesIO())

    binding = SupervisionBinding("", supervisor=SimpleNamespace(runtime_dir=str(runtime)))
    kernel = spawn_supervised_kernel(binding, TargetEnvironment(), backend, "fixture", "fixture",
                                     frozenset(), 10)
    directory = Path(kernel.kernel_dir)
    assert directory.parent == runtime
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("kernel_runner.py", "hermes_tools.py", "kernel.env"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert len(launches) == 1
    assert len(staging_commands) == 1
    assert kernel.rpc_token not in staging_commands[0]
    assert kernel.rpc_token not in launches[0]
    assert binding.kernels == [kernel]


class Shell:
    def __init__(self):
        self.commands = []

    def execute(self, command, *, cwd, timeout, stdin_data):
        self.commands.append(command)
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", command],
                                cwd=cwd, timeout=timeout, input=stdin_data,
                                capture_output=True, text=True)
        return {"returncode": result.returncode, "output": result.stdout + result.stderr}


def test_stage_kernel_in_one_admission_with_private_exact_payloads(tmp_path):
    from tools.code_kernel_supervised import _stage_kernel_files

    env = Shell()
    directory = tmp_path / "kernel with 'quotes' ; spaces"
    contents = ("print('café')\n", "# tool stubs\n", "export TOKEN='secret-not-in-argv'\n")
    _stage_kernel_files(env, str(directory), *contents)
    assert len(env.commands) == 1
    assert all(content.strip() not in env.commands[0] for content in contents)
    for name, content in zip(("kernel_runner.py", "hermes_tools.py", "kernel.env"), contents):
        path = directory / name
        assert path.read_text() == content
        assert path.stat().st_mode & 0o777 == 0o600
    for path in (directory, directory / "cells", directory / "rpc"):
        assert path.stat().st_mode & 0o777 == 0o700
    assert not (tmp_path / "spaces").exists()


def test_stage_kernel_refuses_incomplete_stdin(tmp_path):
    from tools.code_kernel_supervised import _stage_kernel_files

    class TruncatedShell(Shell):
        def execute(self, command, **kwargs):
            kwargs["stdin_data"] = kwargs["stdin_data"].splitlines()[0] + "\n"
            return super().execute(command, **kwargs)

    env = TruncatedShell()
    with pytest.raises(RuntimeError, match="supervised kernel staging failed"):
        _stage_kernel_files(env, str(tmp_path / "kernel"), "runner", "tools", "secret")
    assert not (tmp_path / "kernel/kernel.env").exists()


def test_stage_kernel_refuses_directory_setup_failure(tmp_path):
    from tools.code_kernel_supervised import _stage_kernel_files

    directory = tmp_path / "not-a-directory"
    directory.write_text("existing file")
    with pytest.raises(RuntimeError, match="supervised kernel staging failed"):
        _stage_kernel_files(Shell(), str(directory), "runner", "tools", "secret")
    assert directory.read_text() == "existing file"
