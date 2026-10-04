"""An owned kernel stages private files inside its execution-host grant."""

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

    class TargetEnvironment:
        def execute(self, command, *, cwd, timeout, stdin_data=None):
            # Refuse the former root-level staging before it can touch the host.
            assert str(runtime) in command, "kernel files escaped the granted runtime"
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
    assert kernel.rpc_token not in launches[0]
    assert binding.kernels == [kernel]
