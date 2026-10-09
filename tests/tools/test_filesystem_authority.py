"""Real target jobs retain shared ownership across independent control clients."""

import os
import shlex
import subprocess
import time
import uuid

import pytest

from tools.environments.filesystem_authority import FilesystemAuthority
from tools.environments.filesystem_claims import ClaimStore
from tools.environments.systemd_jobs import SystemdJobSupervisor
from tools.environments.job_supervision import SupervisionError

from tests.tools.test_target_job_supervision import target
from tests.tools.test_target_job_supervision import wait_for


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local"])
def test_systemd_mount_properties_reach_manager_with_exact_paths(tmp_path, target):
    import json
    import sys
    import sysconfig
    from pathlib import Path
    from tools.environments.job_supervision import JobReceipt

    base = tmp_path / 'worker %t "state" \\ '
    base.mkdir()
    state = base / "authority"
    ClaimStore.initialize(state)
    root = tmp_path / 'shared %t "data" \\ '
    root.mkdir()
    maintained = root / "project"
    maintained.mkdir()

    def factory(row, uid):
        # The real parser can be qualified without root. User namespaces cannot
        # access the host's proc-FD pins; the system-provider test covers launch.
        supervisor = authority._system_supervisor(row, uid)
        supervisor.execute = target
        supervisor._manager = "--user"
        return supervisor

    authority = FilesystemAuthority(state, {os.getuid(): {"id": "controller", "execution_uid": os.getuid(),
        "roots": [str(root)], "environment": {"PATH": os.environ["PATH"]}}},
        supervisor_factory=factory)

    def call(op, **fields):
        return authority.dispatch(os.getuid(), {"version": 1, "authority": authority.store.authority_id,
            "principal": "controller", "request": "attempt", "fingerprint": "fp", "op": op, **fields})

    try:
        first = call("reserve", roots=[str(maintained)])
        assert first["state"] == "active"
        assert first["roots"] == [str(maintained)]
        job = JobReceipt(uuid.uuid4().hex)
        try:
            call("start", job=job.id, command=":", cwd=str(maintained), environment={})
        except SupervisionError:
            pass  # A retained failed unit still exposes the manager's parsed policy.
        row = authority.store.get("controller", "attempt")
        supervisor = authority._supervisor(row)
        _, unit = supervisor._job(job)
        address = target("busctl --user --json=short call org.freedesktop.systemd1 /org/freedesktop/systemd1 "
                         "org.freedesktop.systemd1.Manager GetUnit s " + shlex.quote(unit))
        assert address.returncode == 0, address.stderr
        unit_path = json.loads(address.stdout)["data"][0]

        def property_value(name):
            result = target("busctl --user --json=short get-property org.freedesktop.systemd1 "
                            + shlex.quote(unit_path) + " org.freedesktop.systemd1.Service " + name)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)["data"]

        binds = property_value("BindPaths")
        assert any(source == first["runtime_dir"] and destination == source for source, destination, *_ in binds)
        pinned, = [source for source, destination, *_ in binds if destination == str(maintained)]
        assert os.stat(pinned) == maintained.stat()
        assert str(maintained) in property_value("ReadWritePaths")
        assert str(root) not in property_value("ReadWritePaths")
        assert first["runtime_dir"] in property_value("ReadWritePaths")
        assert property_value("RootDirectory") == str(state / row["id"] / "rootfs")
        assert property_value("ProtectHome") == "tmpfs"
        assert any(source == str(state / row["id"] / "fence") and destination == "/run/hermes-job-fence"
                   for source, destination, *_ in property_value("BindReadOnlyPaths"))
        readonly = property_value("BindReadOnlyPaths")
        executable = str(Path(sys._base_executable).resolve(strict=True))
        base_vars = {"base": sys.base_prefix, "platbase": sys.base_exec_prefix}
        for path in (executable, sysconfig.get_path("stdlib", vars=base_vars),
                     sysconfig.get_path("platstdlib", vars=base_vars)):
            assert any(source == str(Path(path).resolve(strict=True)) and destination == path
                       for source, destination, *_ in readonly)
    finally:
        call("stop")
        assert call("release")["state"] == "settled"
        authority.close()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local", "ssh"])
def test_launch_checks_mounted_identity_before_workload_code(tmp_path, target):
    root = tmp_path / "maintained"
    root.mkdir()
    identity = root.stat()
    supervisor = SystemdJobSupervisor(target, str(tmp_path / "control"),
        root_identities=((str(root), identity.st_dev, identity.st_ino),))
    supervisor.prepare()
    root.rename(tmp_path / "original")
    root.mkdir()
    job = supervisor.start("printf redirected > writer", cwd=str(root), environment_names=())
    try:
        wait_for(lambda: supervisor.main_exit_code(job) is not None)
        assert supervisor.main_exit_code(job) == 78
        assert not (root / "writer").exists()
        assert not (tmp_path / "original/writer").exists()
    finally:
        supervisor.stop()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local"])
def test_completed_job_observations_use_durable_receipts_without_reprobing_the_manager(tmp_path, target):
    state, root = tmp_path / "authority", tmp_path / "data"
    ClaimStore.initialize(state)
    root.mkdir()
    calls = []
    def execute(script, stdin=None):
        calls.append(time.monotonic())
        return target(script, stdin)
    policies = {os.getuid(): {"id": "controller", "execution_uid": os.getuid(), "roots": [str(root)]}}
    authority = FilesystemAuthority(state, policies,
        supervisor_factory=lambda row, uid: SystemdJobSupervisor(execute, str(state / row["id"])))
    message = {"version": 1, "authority": authority.store.authority_id, "principal": "controller",
               "request": "attempt", "fingerprint": "fp"}
    job = uuid.uuid4().hex
    def call(op, **fields):
        return authority.dispatch(os.getuid(), {**message, "op": op, **fields})
    try:
        call("reserve", roots=[str(root)])
        call("start", job=job, command="printf receipt", cwd=str(root), environment={})
        wait_for(lambda: call("observe", job=job)["state"] == "settled")
        before = len(calls)
        for _ in range(5):
            observation = call("observe", job=job)
            assert observation["state"] == "settled" and observation["exit_code"] == 0
        assert len(calls) - before <= 5  # At most the requested output transfer.
        assert call("status")["state"] == "active"
        assert call("release")["state"] == "settled"
    finally:
        call("stop")
        call("release")
        authority.close()


@pytest.mark.platforms("linux")
def test_authority_fences_lost_controller_and_waits_for_detached_writer(tmp_path):
    from tools.environments.local import build_subprocess_env
    from tools.process_registry import systemd_user_bus_env

    env = systemd_user_bus_env(build_subprocess_env())
    probe = subprocess.run(["systemctl", "--user", "show", "--property=Version"], env=env, capture_output=True)
    if probe.returncode:
        pytest.skip("a running systemd user manager is required")
    state = tmp_path / "authority"
    ClaimStore.initialize(state)
    root = tmp_path / "shared data "
    root.mkdir()
    def execute(script, stdin=None):
        return subprocess.run(["bash", "--noprofile", "--norc", "-c", script], input=stdin,
                              env=env, capture_output=True, text=True, timeout=30)
    def factory(row, uid):
        return SystemdJobSupervisor(execute, str(state / row["id"]))
    policies = {10001: {"id": "a", "execution_uid": os.getuid(), "roots": [str(root)]},
                10002: {"id": "b", "execution_uid": os.getuid(), "roots": [str(root)]}}
    authority = FilesystemAuthority(state, policies, supervisor_factory=factory)
    def call(uid, op, **fields):
        return authority.dispatch(uid, {"version": 1, "authority": authority.store.authority_id,
                                       "principal": policies[uid]["id"],
                                       "op": op, "request": "attempt", "fingerprint": "fp", **fields})
    try:
        assert call(10001, "reserve", roots=[str(root)])["state"] == "active"
        assert call(10002, "reserve", roots=[str(root)])["state"] == "pending"
        child = "echo started > started; while test ! -e release; do sleep .05; done; echo done > done"
        job = {"job": uuid.uuid4().hex, "cwd": str(root), "environment": {"PATH": env["PATH"]},
               "command": f"setsid bash -c {shlex.quote(child)} </dev/null >/dev/null 2>&1 &"}
        call(10001, "start", **job)
        deadline = time.monotonic() + 15
        while not (root / "started").exists():
            assert time.monotonic() < deadline
            time.sleep(.05)
        assert call(10001, "release")["state"] == "sealed"
        assert call(10002, "reserve", roots=[str(root)])["state"] == "pending"
        authority.close()
        authority = FilesystemAuthority(state, policies, supervisor_factory=factory)
        authority.reconcile()
        assert call(10001, "status")["state"] == "sealed"
        with pytest.raises(Exception, match="sealed"):
            call(10001, "start", **{**job, "job": uuid.uuid4().hex, "command": "touch late"})
        (root / "release").touch()
        while call(10001, "release")["state"] != "settled":
            assert time.monotonic() < deadline
            time.sleep(.05)
        assert (root / "done").read_text() == "done\n"
        assert not (root / "late").exists()
        assert call(10002, "reserve", roots=[str(root)])["state"] == "active"
        call(10002, "start", **{**job, "job": uuid.uuid4().hex, "command": "sleep 300"})
        authority.close()
        authority = FilesystemAuthority(state, policies, supervisor_factory=factory)
        assert call(10002, "status")["state"] == "sealed"
        authority.reconcile()
        assert call(10002, "status")["state"] == "sealed"
        call(10002, "stop")
        assert call(10002, "status")["state"] == "stopping"
        call(10002, "release")
        assert call(10002, "status")["state"] == "settled"
        assert call(10002, "reserve", roots=[str(root)])["state"] == "settled"
    finally:
        call(10001, "stop")
        call(10002, "stop")
        authority.close()


def test_authority_authenticates_scope_and_never_reinitializes_lost_ledger(tmp_path):
    state = tmp_path / "authority"
    ClaimStore.initialize(state)
    root = tmp_path / "allowed"
    root.mkdir()
    other = tmp_path / "outside"
    other.mkdir()
    policies = {10001: {"id": "controller", "execution_uid": 1001, "roots": [str(root)]}}
    authority = FilesystemAuthority(state, policies)
    message = {"version": 1, "authority": authority.store.authority_id, "op": "reserve",
               "principal": "controller",
               "request": "attempt", "fingerprint": "fp", "roots": [str(other)]}
    try:
        with pytest.raises(ValueError, match="distinct principal"):
            FilesystemAuthority(state, {**policies, 10002: policies[10001]})
        with pytest.raises(PermissionError, match="unenrolled"):
            authority.dispatch(10002, message)
        with pytest.raises(PermissionError, match="scope"):
            authority.dispatch(10001, message)
        with pytest.raises(ValueError, match="identity"):
            authority.dispatch(10001, {**message, "authority": "other-host"})
        assert not authority.store.live()
        with pytest.raises(BlockingIOError):
            FilesystemAuthority(state, policies)
        assert authority.dispatch(10001, {**message, "op": "stop"})["state"] == "settled"
        assert authority.dispatch(10001, message)["state"] == "settled"
    finally:
        authority.close()
    (state / "claims.sqlite").unlink()
    with pytest.raises(Exception, match="unable to open"):
        ClaimStore(state)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local"])
@pytest.mark.parametrize("failure", ["before-submit", "unavailable-exit"])
def test_stop_fences_admitted_intent_even_when_no_exit_receipt_exists(tmp_path, target, monkeypatch, failure):
    state = tmp_path / "authority"
    ClaimStore.initialize(state)
    root = tmp_path / "data"
    root.mkdir()
    authority = FilesystemAuthority(state, {os.getuid(): {
        "id": "controller", "execution_uid": os.getuid(), "roots": [str(root)],
    }}, supervisor_factory=lambda row, uid: SystemdJobSupervisor(target, str(state / row["id"])))
    message = {"version": 1, "authority": authority.store.authority_id, "principal": "controller",
               "request": "attempt", "fingerprint": "fp"}
    job = uuid.uuid4().hex
    command = {"job": job, "cwd": str(root), "environment": {}, "command": "touch entered; sleep 300; touch late"}
    def call(op, **fields):
        return authority.dispatch(os.getuid(), {**message, "op": op, **fields})
    try:
        call("reserve", roots=[str(root)])
        provider = authority._supervisor(authority.store.get("controller", "attempt"))
        with monkeypatch.context() as patch:
            if failure == "before-submit":
                def fail_start(*args, **kwargs):
                    raise SupervisionError("transport failed before target submission")
                patch.setattr(provider, "start", fail_start)
                with pytest.raises(SupervisionError):
                    call("start", **command)
            else:
                call("start", **command)
                # Admission acknowledgement may precede the workload's launch gate.
                wait_for(lambda: (root / "entered").exists())
            def fail_exit(*args, **kwargs):
                raise SupervisionError("exit observation was lost")
            patch.setattr(provider, "main_exit_code", fail_exit)
            with monkeypatch.context() as lost_stop:
                def fail_stop(*args, **kwargs):
                    raise SupervisionError("stop acknowledgement was lost")
                lost_stop.setattr(provider, "stop_jobs", fail_stop)
                with pytest.raises(SupervisionError, match="stop acknowledgement"):
                    call("stop")
                receipt = authority.store.db.execute("SELECT exit_code,settled FROM jobs WHERE id=?", (job,)).fetchone()
                assert tuple(receipt) == (None, 0)  # An unsent Stop supplies no foreground exit evidence.
                if failure == "unavailable-exit":
                    observation = call("observe", job=job)
                    assert observation["state"] == "running" and observation["exit_code"] is None
                assert call("reserve", request="waiter", roots=[str(root)])["state"] == "pending"
            stopped = call("stop")
            assert stopped["drained"] and stopped["state"] == "stopping"
        receipt = authority.store.db.execute("SELECT exit_code,settled FROM jobs WHERE id=?", (job,)).fetchone()
        assert tuple(receipt) == (-15, 1)
        assert call("release")["state"] == "settled"
        with pytest.raises(SupervisionError, match="sealed"):
            call("start", **command)
        assert not (root / "late").exists()
    finally:
        call("stop")
        call("release")
        authority.close()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local", "ssh"])
def test_restart_fences_delayed_submission_before_exposing_sealed_ledger(tmp_path, target):
    state = tmp_path / "authority"
    ClaimStore.initialize(state)
    root = tmp_path / "data"
    root.mkdir()
    policies = {os.getuid(): {"id": "controller", "execution_uid": os.getuid(), "roots": [str(root)]}}
    factory = lambda row, uid: SystemdJobSupervisor(target, str(state / row["id"]))
    authority = FilesystemAuthority(state, policies, supervisor_factory=factory)
    message = {"version": 1, "authority": authority.store.authority_id, "principal": "controller",
               "request": "attempt", "fingerprint": "fp"}
    release, submitted = tmp_path / "release", tmp_path / "submitted"
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    real = target("command -v systemd-run").stdout.strip()
    bash = target("command -v bash").stdout.strip()
    shim = shim_dir / "systemd-run"
    shim.write_text(f"#!{bash}\n( exec 9>&-; "
        f"while test ! -e {shlex.quote(str(release))}; do sleep .05; done; "
        f"{shlex.quote(real)} \"$@\"; touch {shlex.quote(str(submitted))} "
        ") </dev/null >/dev/null 2>&1 &\nexit 1\n")
    shim.chmod(0o700)
    try:
        receipt = authority.dispatch(os.getuid(), {**message, "op": "reserve", "roots": [str(root)]})
        supervisor = authority._supervisor(authority.store.get("controller", "attempt"))
        supervisor.execute = lambda script, stdin=None: target(f"export PATH={shlex.quote(str(shim_dir))}:$PATH; " + script, stdin)
        with pytest.raises(SupervisionError):
            authority.dispatch(os.getuid(), {**message, "op": "start", "job": uuid.uuid4().hex,
                "command": "printf late > late-write", "cwd": str(root), "environment": {}})
        authority.close()
        authority = FilesystemAuthority(state, policies, supervisor_factory=factory)
        assert authority.dispatch(os.getuid(), {**message, "op": "status"})["state"] == "sealed"
        release.touch()
        wait_for(submitted.exists)
        recovered = authority._supervisor(authority.store.get("controller", "attempt"))
        job, = recovered.jobs()
        wait_for(lambda: recovered.main_exit_code(job) is not None)
        assert not (root / "late-write").exists()
        assert (state / receipt["claim"] / "fence/sealed").exists()
    finally:
        release.touch()
        wait_for(submitted.exists)
        authority.dispatch(os.getuid(), {**message, "op": "stop"})
        assert authority.dispatch(os.getuid(), {**message, "op": "release"})["state"] == "settled"
        authority.close()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local", "ssh"])
def test_observation_reuses_exit_evidence_but_retains_ownership_until_stop(tmp_path, target, monkeypatch):
    from tools.environments.job_supervision import JobReceipt, JobState

    state, root = tmp_path / "authority", tmp_path / "data"
    ClaimStore.initialize(state)
    root.mkdir()
    authority = FilesystemAuthority(state, {os.getuid(): {
        "id": "controller", "execution_uid": os.getuid(), "roots": [str(root)],
    }}, supervisor_factory=lambda row, uid: SystemdJobSupervisor(target, str(state / row["id"])))
    message = {"version": 1, "authority": authority.store.authority_id, "principal": "controller",
               "request": "attempt", "fingerprint": "fp"}

    def call(op, **fields):
        return authority.dispatch(os.getuid(), {**message, "op": op, **fields})

    try:
        call("reserve", roots=[str(root)])
        provider = authority._supervisor(authority.store.get("controller", "attempt"))
        job = uuid.uuid4().hex
        call("start", job=job, cwd=str(root), environment={}, command="echo natural; exit 7")
        wait_for(lambda: provider.inspect(JobReceipt(job)) is JobState.SETTLED)
        with monkeypatch.context() as patch:
            def duplicate_exit(*args):
                raise AssertionError("the observation already carries the target exit evidence")
            patch.setattr(provider, "main_exit_code", duplicate_exit)
            with monkeypatch.context() as lost_stop:
                def unavailable_stop(*args):
                    raise SupervisionError("stop acknowledgement was lost")
                lost_stop.setattr(provider, "stop_jobs", unavailable_stop)
                with pytest.raises(SupervisionError, match="stop acknowledgement"):
                    call("observe", job=job, offset=0)
                recorded = authority.store.db.execute("SELECT exit_code,settled FROM jobs WHERE id=?", (job,)).fetchone()
                assert tuple(recorded) == (7, 0)
                assert call("reserve", request="waiter", roots=[str(root)])["state"] == "pending"
            observation = call("observe", job=job, offset=0)
        assert observation["state"] == "settled" and observation["exit_code"] == 7
        assert call("reserve", request="waiter", roots=[str(root)])["state"] == "pending"
        assert call("release")["state"] == "settled"
        assert call("reserve", request="waiter", roots=[str(root)])["state"] == "active"
    finally:
        call("stop")
        call("release")
        call("stop", request="waiter")
        call("release", request="waiter")
        authority.close()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local"])
def test_failed_scope_preparation_fences_retries_until_explicit_release(tmp_path, monkeypatch, target):
    from pathlib import Path

    state = tmp_path / "authority"
    ClaimStore.initialize(state)
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
    authority = FilesystemAuthority(state, {os.getuid(): {
        "id": "controller", "execution_uid": os.getuid(), "roots": [str(root) for root in roots],
    }}, supervisor_factory=lambda row, uid: SystemdJobSupervisor(target, str(state / row["id"])))
    message = {"version": 1, "authority": authority.store.authority_id, "principal": "controller",
               "request": "attempt", "fingerprint": "fp", "roots": [str(root) for root in roots]}
    mkdir = Path.mkdir
    def fail_partial_runtime(path, *args, **kwargs):
        if path.name == "cache" and path.parent.parent == state.parent / "jobs":
            raise OSError("runtime preparation failed")
        return mkdir(path, *args, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "mkdir", fail_partial_runtime)
            with pytest.raises(OSError, match="preparation failed"):
                authority.dispatch(os.getuid(), {**message, "op": "reserve"})
        retry = authority.dispatch(os.getuid(), {**message, "op": "reserve"})
        assert retry["state"] == "sealed"
        with pytest.raises(SupervisionError, match="sealed"):
            authority.dispatch(os.getuid(), {**message, "op": "start", "job": uuid.uuid4().hex,
                "cwd": str(roots[0]), "command": "touch unexpected", "environment": {}})
        next_request = {**message, "request": "next"}
        assert authority.dispatch(os.getuid(), {**next_request, "op": "reserve"})["state"] == "pending"
        assert authority.dispatch(os.getuid(), {**message, "op": "release"})["state"] == "settled"
        assert authority.dispatch(os.getuid(), {**next_request, "op": "reserve"})["state"] == "active"
        assert not (roots[0] / "unexpected").exists()
    finally:
        authority.close()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("backend", ["local", "ssh"])
def test_supervisor_uses_authenticated_socket_over_local_and_ssh(tmp_path, target):
    import sys
    import tempfile
    import threading
    from tools.environments.filesystem_authority_server import AuthorityServer
    from tools.environments.filesystem_supervisor import FilesystemSupervisor, OwnershipPending

    state = tmp_path / "authority"
    ClaimStore.initialize(state)
    root = tmp_path / "data"
    root.mkdir()
    policy = {os.getuid(): {"id": "test-controller", "execution_uid": os.getuid(), "roots": [str(root)]}}
    authority = FilesystemAuthority(state, policy, supervisor_factory=lambda row, uid:
                                    SystemdJobSupervisor(target, str(state / row["id"])))
    with tempfile.TemporaryDirectory(prefix="hfa-") as socket_dir:
        socket_path = socket_dir + "/control"
        with AuthorityServer(socket_path, authority) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            command = shlex.join([sys.executable, "-m", "tools.environments.filesystem_authority_server",
                                  "request", "--socket", socket_path])
            repo = str(__import__("pathlib").Path(__file__).resolve().parents[2])
            lost_reply = False
            def call(payload):
                nonlocal lost_reply
                result = target(f"cd -- {shlex.quote(repo)}; {command}", payload)
                if __import__("json").loads(payload)["op"] == "start" and not lost_reply:
                    lost_reply = True
                    raise OSError("start acknowledgement was lost")
                return result
            descriptor = {"authority": authority.store.authority_id, "principal": "test-controller", "request": "one", "fingerprint": "one",
                          "roots": [str(root)]}
            a = FilesystemSupervisor(call, descriptor, {"PATH": os.environ["PATH"]})
            b = FilesystemSupervisor(call, {**descriptor, "request": "two", "fingerprint": "two"},
                                    {"PATH": os.environ["PATH"]})
            try:
                a.prepare()
                with pytest.raises(OwnershipPending):
                    b.prepare()
                job = a.start("echo once >> executions; cat; exit 7", cwd=str(root), environment_names=("PATH",), stdin="exact\ninput\n")
                deadline = time.monotonic() + 15
                while a.main_exit_code(job) is None:
                    assert time.monotonic() < deadline
                    time.sleep(.1)
                assert a.exit_code(job) == 7
                assert a.output(job) == "exact\ninput\n"
                assert (root / "executions").read_text() == "once\n"
                assert a.jobs() == [job]
                with pytest.raises(PermissionError, match="scope"):
                    authority.dispatch(os.getuid(), {"version": 1, **descriptor, "op": "start",
                        "job": uuid.uuid4().hex, "cwd": str(tmp_path), "environment": {}, "command": "touch escaped"})
                assert a.jobs() == [job]
                a.seal()
                assert a.settled()
                with pytest.raises(OwnershipPending):
                    b.prepare()
                row = authority.store.get("test-controller", "one")
                provider = authority._supervisor(row, prepare=False)
                control = __import__("pathlib").Path(provider.state_dir)
                payloads = [control / ("job-" + job.id) / name
                            for name in ("input", "command", "environment", "output")]
                assert all(path.exists() for path in payloads)
                cleanup = provider.cleanup_payloads
                def unavailable_cleanup():
                    raise SupervisionError("payload cleanup transport unavailable")
                provider.cleanup_payloads = unavailable_cleanup
                with pytest.raises(SupervisionError):
                    a.release()
                assert all(path.exists() for path in payloads)
                provider.cleanup_payloads = cleanup
                # Restart after the durable release decision but before its
                # cleanup/reply. Replay must finish cleanup without replaying work.
                authority.close()
                authority = FilesystemAuthority(state, policy, supervisor_factory=lambda row, uid:
                    SystemdJobSupervisor(target, str(state / row["id"])))
                server.authority = authority
                assert a.release()["state"] == "settled"
                assert not any(path.exists() for path in payloads)
                assert (control / "fence/sealed").is_file()
                assert (control / ("job-" + job.id)).is_dir()
                assert a.release()["state"] == "settled"
                assert (root / "executions").read_text() == "once\n"
                b.prepare()
                # Exercise the authority/client boundary, not just the provider:
                # serial five-second stops exceed this transport's existing bound.
                ready = [root / f"slow-ready-{index}" for index in range(6)]
                slow = [b.start(f'trap "" TERM; touch {shlex.quote(str(path))}; '
                                'while :; do sleep .1; done',
                                cwd=str(root), environment_names=("PATH",)) for path in ready]
                wait_for(lambda: all(path.exists() for path in ready))
                b.stop()
                assert b.settled()
                from tools.environments.job_supervision import JobState
                assert all(b.inspect(owned) is JobState.SETTLED for owned in slow)
                with pytest.raises(SupervisionError, match="sealed"):
                    b.start("touch late-after-stop", cwd=str(root), environment_names=("PATH",))
                assert not (root / "late-after-stop").exists()
            finally:
                a.stop()
                b.stop()
                server.shutdown()
                thread.join()
                authority.close()


@pytest.mark.platforms("linux")
def test_authority_rejects_writable_socket_parent(tmp_path):
    from tools.environments.filesystem_authority_server import AuthorityServer

    for mode in (0o770, 0o777, 0o1777):
        parent = tmp_path / f"socket-{mode:o}"
        parent.mkdir()
        parent.chmod(mode)
        with pytest.raises(PermissionError):
            with AuthorityServer(str(parent / "control"), None):
                pass
        assert not (parent / "control").exists()
        private = parent / "private"
        private.mkdir(mode=0o700)
        if mode == 0o1777 and os.geteuid() == 0:
            # A protected service-owned child below root's sticky /tmp is safe;
            # a socket directly in the shared sticky directory is not.
            with AuthorityServer(str(private / "control"), None):
                assert (private / "control").is_socket()
        else:
            with pytest.raises(PermissionError):
                with AuthorityServer(str(private / "control"), None):
                    pass
            assert not (private / "control").exists()
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(protected, target_is_directory=True)
    with pytest.raises(OSError):
        with AuthorityServer(str(alias / "control"), None):
            pass
    assert not (protected / "control").exists()
