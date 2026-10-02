"""Target supervision includes daemonized descendants and fences late launches."""

import shlex
import subprocess
import time

import pytest


def wait_for(predicate):
    deadline = time.monotonic() + 15
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("target did not reach the expected state")
        time.sleep(0.05)


@pytest.fixture
def target(tmp_path, monkeypatch, backend):
    from tools.environments.local import build_subprocess_env
    from tools.process_registry import systemd_user_bus_env

    env = systemd_user_bus_env(build_subprocess_env())
    probe = subprocess.run(["systemctl", "--user", "show", "--property=Version"],
                           env=env, capture_output=True, text=True)
    if probe.returncode:
        pytest.skip("a running systemd user manager is required")
    if backend == "local":
        def execute(script, stdin=None):
            return subprocess.run(["bash", "--noprofile", "--norc", "-c", script],
                                  input=stdin, env=env, capture_output=True, text=True, timeout=20)
        execute.ssh_config = {}
        yield execute
        return
    from tests.tools.ssh_worker_transport import openssh_transport
    from tools.environments.ssh import SSHEnvironment

    home = tmp_path / "login"
    home.mkdir()
    with openssh_transport(tmp_path / "sshd", home, monkeypatch) as config:
        ssh = SSHEnvironment(host=config["host"], user=config["user"], port=config["port"],
                             key_path=config["key"], cwd=str(tmp_path), probe_only=True)
        try:
            def execute(script, stdin=None):
                return subprocess.run(ssh._build_ssh_command() + ["bash --noprofile --norc -c " + shlex.quote(script)],
                                      input=stdin, env=env, capture_output=True, text=True, timeout=20)
            execute.ssh_config = config
            yield execute
        finally:
            ssh.cleanup()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local", "ssh"])
def test_supervisor_waits_for_daemon_and_recovers_stop_fence(tmp_path, target):
    from tools.environments.job_supervision import JobState, SupervisionError
    from tools.environments.systemd_jobs import SystemdJobSupervisor

    root = str(tmp_path / "worker %t state")
    supervisor = SystemdJobSupervisor(target, root)
    supervisor.prepare()
    release = tmp_path / "release"
    started = tmp_path / "started"
    parent_done = tmp_path / "parent-done"
    # setsid + closed stdio removes both the process-tree and pipe-lifetime clues.
    child = f"echo ready > {shlex.quote(str(started))}; while test ! -f {shlex.quote(str(release))}; do sleep .05; done"
    command = (f"setsid bash -c {shlex.quote(child)} </dev/null >/dev/null 2>&1 & "
               f"echo done > {shlex.quote(str(parent_done))}")
    try:
        job = supervisor.start(command, cwd=str(tmp_path), environment_names=("PATH", "HOME"))
        wait_for(lambda: started.exists() and parent_done.exists())
        assert supervisor.inspect(job) is JobState.RUNNING
        # Reconstruct using only the target and receipt directory, as on reconnect.
        recovered = SystemdJobSupervisor(target, root)
        assert recovered.jobs() == [job]
        assert recovered.inspect(job) is JobState.RUNNING
        release.touch()
        wait_for(lambda: recovered.inspect(job) is JobState.SETTLED)
        second = recovered.start("sleep 300", cwd=str(tmp_path), environment_names=("PATH",))
        recovered.stop()
        assert recovered.inspect(second) is JobState.SETTLED
        assert recovered.settled()
        with pytest.raises(SupervisionError, match="sealed"):
            supervisor.start("touch should-not-exist", cwd=str(tmp_path), environment_names=("PATH",))
        assert not (tmp_path / "should-not-exist").exists()
        # A launch queued in a disconnected transport must consult the fence on
        # arrival, rather than relying on the controller's old admission state.
        import threading
        entered, resume = threading.Event(), threading.Event()
        def delayed(script, stdin=None):
            entered.set()
            assert resume.wait(10)
            return target(script, stdin)
        from concurrent.futures import ThreadPoolExecutor
        late = SystemdJobSupervisor(delayed, root)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(late.start, "touch late-launch", cwd=str(tmp_path), environment_names=("PATH",))
            assert entered.wait(10)
            recovered.seal()
            resume.set()
            with pytest.raises(SupervisionError, match="sealed"):
                pending.result(timeout=15)
        assert not (tmp_path / "late-launch").exists()
    finally:
        supervisor.stop()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local", "ssh"])
def test_systemd_submission_delayed_past_seal_cannot_execute_work(tmp_path, target):
    from tools.environments.job_supervision import JobState, SupervisionError
    from tools.environments.systemd_jobs import SystemdJobSupervisor

    supervisor = SystemdJobSupervisor(target, str(tmp_path / "state"))
    supervisor.prepare()
    release, submitted = tmp_path / "release", tmp_path / "submitted"
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    real = target("command -v systemd-run").stdout.strip()
    bash = target("command -v bash").stdout.strip()
    shim = shim_dir / "systemd-run"
    # Model a request queued in the manager after its submitting connection
    # disappeared. Close the caller's gate FD so seal can win before arrival.
    shim.write_text(f"#!{bash}\n"
        "( exec 9>&-; "
        f"while test ! -e {shlex.quote(str(release))}; do sleep .05; done; "
        f"{shlex.quote(real)} \"$@\"; touch {shlex.quote(str(submitted))} "
        ") </dev/null >/dev/null 2>&1 &\nexit 1\n")
    shim.chmod(0o700)
    def delayed(script, stdin=None):
        return target(f"export PATH={shlex.quote(str(shim_dir))}:$PATH; " + script, stdin)
    uncertain = SystemdJobSupervisor(delayed, supervisor.state_dir)
    try:
        with pytest.raises(SupervisionError):
            uncertain.start("touch late-write", cwd=str(tmp_path), environment_names=("PATH",))
        job, = supervisor.jobs()
        supervisor.seal()
        assert supervisor.inspect(job) is JobState.SETTLED
        release.touch()
        wait_for(submitted.exists)
        wait_for(lambda: supervisor.main_exit_code(job) is not None)
        assert not (tmp_path / "late-write").exists()
        assert supervisor.inspect(job) is JobState.SETTLED
    finally:
        release.touch()
        wait_for(submitted.exists)
        supervisor.stop()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local", "ssh"])
def test_terminal_dispatch_keeps_supervised_background_descendants_owned(tmp_path, target, backend, monkeypatch):
    import json
    import hermes_yaml as yaml
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.environments.supervised_execution import SupervisionBinding, bind_job_supervision
    from tools.environments.systemd_jobs import SystemdJobSupervisor
    from tools.process_registry import process_registry
    from tools.terminal_scope import install_and_reset_profile_terminal_scope
    from tools.terminal_tool import terminal_tool
    from tools.terminal_tool_lifecycle import cleanup_vm

    home = tmp_path / "worker"
    home.mkdir()
    data = tmp_path / "personal-data"
    data.mkdir()
    terminal = {"backend": backend, "cwd": str(data)}
    ssh = target.ssh_config
    if ssh:
        terminal.update(ssh_host=ssh["host"], ssh_user=ssh["user"], ssh_port=ssh["port"],
                        ssh_key=ssh["key"], ssh_hermes_home=str(tmp_path / "remote-worker"))
    (home / "config.yaml").write_text(yaml.safe_dump({"terminal": terminal}))
    binding = SupervisionBinding(str(tmp_path / "supervision"))
    supervisor = SystemdJobSupervisor(target, binding.state_dir)
    supervisor.prepare()
    token = set_hermes_home_override(home)
    task = "supervised-terminal-test"
    monkeypatch.setenv("__ETC_PROFILE_SOURCED", "1")
    try:
        with install_and_reset_profile_terminal_scope(home), bind_job_supervision(binding):
            foreground = json.loads(terminal_tool(command="printf '%s' exact", task_id=task))
            assert foreground.get("exit_code") == 0, foreground
            assert foreground["output"] == "exact"
            release, started = data / "release", data / "started"
            child = f"echo ready > {shlex.quote(str(started))}; while test ! -e {shlex.quote(str(release))}; do sleep .05; done"
            command = f"setsid bash -c {shlex.quote(child)} </dev/null >/dev/null 2>&1 &"
            result = json.loads(terminal_tool(command=command, background=True, task_id=task))
            assert result.get("exit_code") == 0, result
            wait_for(started.exists)
            assert not process_registry.get(result["session_id"]).exited
            supervisor.seal()
            assert not supervisor.settled()
            release.touch()
            wait_for(supervisor.settled)
            wait_for(lambda: process_registry.get(result["session_id"]).exited)
    finally:
        supervisor.stop()
        with install_and_reset_profile_terminal_scope(home):
            cleanup_vm(task)
        reset_hermes_home_override(token)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local", "ssh"])
def test_supervisor_preserves_input_and_treats_lost_control_as_unknown(tmp_path, target):
    from tools.environments.job_supervision import JobState
    from tools.environments.systemd_jobs import SystemdJobSupervisor

    supervisor = SystemdJobSupervisor(target, str(tmp_path / "state"))
    supervisor.prepare()
    try:
        job = supervisor.start("cat; exit 7", cwd=str(tmp_path), environment_names=("PATH",), stdin="exact\ninput\n")
        wait_for(lambda: supervisor.inspect(job) is JobState.SETTLED)
        assert supervisor.output(job) == "exact\ninput\n"
        assert supervisor.exit_code(job) == 7
        def disconnected(*_):
            raise OSError("SSH disconnected")
        offline = SystemdJobSupervisor(disconnected, supervisor.state_dir)
        assert offline.inspect(job) is JobState.UNKNOWN
        assert not offline.settled()
        # Submission took effect but its reply was lost: reconnect discovers
        # and controls the original job, without replaying the command.
        from tools.environments.job_supervision import SupervisionError
        def lose_ack(script, stdin=None):
            target(script, stdin)
            raise OSError("lost acknowledgement")
        uncertain = SystemdJobSupervisor(lose_ack, supervisor.state_dir)
        with pytest.raises(SupervisionError):
            uncertain.start("sleep 300", cwd=str(tmp_path), environment_names=("PATH",))
        recovered_jobs = supervisor.jobs()
        assert len(recovered_jobs) == 2
        assert any(supervisor.inspect(receipt) is JobState.RUNNING for receipt in recovered_jobs)
    finally:
        supervisor.stop()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local", "ssh"])
@pytest.mark.parametrize("cancel", [False, True])
def test_run_waits_for_delegation_worker_before_sealing_launches(tmp_path, target, cancel):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tools.async_delegation import dispatch_async_delegation
    from tools.environments.supervised_execution import SupervisionBinding, bind_job_supervision
    from tools.environments.systemd_jobs import SystemdJobSupervisor
    from gateway.platforms.api_server_job_lifetime import RunJobLifetime
    from tools.environments.job_supervision import SupervisionError

    binding = SupervisionBinding(str(tmp_path / "state"))
    supervisor = SystemdJobSupervisor(target, binding.state_dir)
    supervisor.prepare()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    interrupted = threading.Event()
    output = tmp_path / "delegated-output"

    def child():
        entered.set()
        assert release.wait(15)
        try:
            if cancel:
                with pytest.raises(SupervisionError, match="sealed"):
                    supervisor.start("touch " + shlex.quote(str(output)), cwd=str(tmp_path), environment_names=("PATH",))
            else:
                supervisor.start("echo child > " + shlex.quote(str(output)), cwd=str(tmp_path), environment_names=("PATH",))
            return {"status": "completed"}
        finally:
            finished.set()

    try:
        with bind_job_supervision(binding), ThreadPoolExecutor(max_workers=1) as pool:
            dispatched = dispatch_async_delegation(goal="write after parent returns", context=None, toolsets=None,
                role="leaf", model=None, session_key="supervised-delegation", runner=child, interrupt_fn=interrupted.set)
            assert dispatched["status"] == "dispatched", dispatched
            assert entered.wait(10)
            settling = pool.submit(RunJobLifetime(binding, supervisor).settle, lambda: cancel)
            try:
                # A child is deliberately parked while the model has returned.
                # Completion must remain pending until that actual worker exits.
                from concurrent.futures import TimeoutError
                with pytest.raises(TimeoutError):
                    settling.result(timeout=2)
                if cancel:
                    assert interrupted.wait(10)
            finally:
                release.set()
            assert finished.wait(10)
            settling.result(timeout=15)
            if cancel:
                assert not output.exists()
            else:
                assert output.read_text() == "child\n"
    finally:
        release.set()
        supervisor.stop()
