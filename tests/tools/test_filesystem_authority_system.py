"""Disposable system-provider qualification; requires an operator-authorized root runner.

No accounts or persistent services are created. Jobs only write inside a fresh
temporary tree, and the test drains every retained unit before removing it.
"""

import os
import pwd
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import venv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tools.environments.filesystem_authority import FilesystemAuthority
from tools.environments.filesystem_authority_server import AuthorityServer
from tools.environments.filesystem_claims import ClaimStore
from tools.environments.filesystem_supervisor import AuthorityRequestRejected, FilesystemSupervisor, OwnershipPending
from tools.environments.job_supervision import JobState


def wait_for(predicate):
    deadline = time.monotonic() + 30
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("system-provider receipt did not settle")
        time.sleep(.1)


@pytest.mark.platforms("linux")
@pytest.mark.skipif(os.geteuid() != 0, reason="requires explicit operator authorization for the system provider")
def test_system_provider_launches_guardian_from_copied_virtualenv(tmp_path):
    runtime = tmp_path / "private-runtime"
    venv.EnvBuilder(symlinks=False).create(runtime)
    interpreter = runtime / "bin/python"
    assert not interpreter.is_symlink()
    assert interpreter.resolve().is_relative_to(tmp_path)
    # Keep imports tied to the built candidate, not the editable source. The
    # authority actually runs from a copied, non-standard interpreter path.
    script = (f"import sys; sys.path[:] = {sys.path!r}; "
              "from tests.tools.test_filesystem_authority_system import "
              "test_system_provider_preserves_uid_and_confines_same_uid_workers as check; check()")
    result = subprocess.run([str(interpreter), "-c", script], capture_output=True, text=True,
                            timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.platforms("linux")
@pytest.mark.skipif(os.geteuid() != 0, reason="requires explicit operator authorization for the system provider")
def test_authority_client_rejects_foreign_server_before_sending_payload(tmp_path):
    from tools.environments.filesystem_authority_server import request

    tmp_path.chmod(0o711)
    socket_path = tmp_path / "foreign-control"
    # A root-owned socket inode does not prove the listener's identity. Bind as
    # root, then listen as the workload UID, leaving the pathname protected.
    script = ("import os,socket,sys; s=socket.socket(socket.AF_UNIX); "
              "s.bind(sys.argv[1]); os.chmod(sys.argv[1], 0o666); "
              "os.setuid(int(sys.argv[2])); s.listen(1); print('ready', flush=True); "
              "c,_=s.accept(); payload=c.recv(4096); "
              "print('leaked' if payload else 'protected', flush=True); "
              "c.sendall(b'{\"ok\":true,\"result\":{}}\\n') if payload else None")
    process = subprocess.Popen([sys.executable, "-c", script, str(socket_path),
                                str(pwd.getpwnam("nobody").pw_uid)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(PermissionError):
            request(str(socket_path), {"command": "private payload"})
        output, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
        assert output.strip() == "protected"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


@pytest.mark.platforms("linux")
@pytest.mark.skipif(os.geteuid() != 0, reason="requires explicit operator authorization for the system provider")
def test_system_provider_preserves_uid_and_confines_same_uid_workers():
    from tools.environments.local import build_subprocess_env

    workload = pwd.getpwnam("nobody")
    env = build_subprocess_env()
    with tempfile.TemporaryDirectory(prefix="hermes-system-provider-") as directory:
        base = Path(directory)
        base.chmod(0o711)
        roots = [base / "shared data ", base / "separate"]
        for root in roots:
            root.mkdir(mode=0o700)
            os.chown(root, workload.pw_uid, workload.pw_gid)
        maintained = roots[0] / "project"
        maintained.mkdir(mode=0o700)
        os.chown(maintained, workload.pw_uid, workload.pw_gid)
        credentials = base / "worker-credentials"
        credentials.write_text("disposable credential fixture")
        credentials.chmod(0o600)
        os.chown(credentials, workload.pw_uid, workload.pw_gid)
        sibling = roots[0] / "unrequested-private"
        sibling.write_text("unrequested sibling")
        sibling.chmod(0o600)
        os.chown(sibling, workload.pw_uid, workload.pw_gid)
        state = base / "authority"
        ClaimStore.initialize(state)
        policies = {uid: {"id": name, "execution_uid": workload.pw_uid,
            "roots": [str(root) for root in roots], "environment": {"PATH": env["PATH"]}}
            for uid, name in ((60001, "controller-a"), (60002, "controller-b"))}
        authority = FilesystemAuthority(state, policies)
        socket_path = str(base / "control")
        script = (
            "import json,socket,sys; s=socket.socket(socket.AF_UNIX); "
            "s.connect(sys.argv[1]); s.sendall(sys.stdin.buffer.read()); "
            "sys.stdout.buffer.write(s.makefile('rb').readline())"
        )
        def client(uid):
            def call(payload):
                # Controllers need a traversable interpreter independently of
                # the authority's private virtualenv.
                return subprocess.run([sys._base_executable, "-c", script, socket_path], input=payload,
                    env=env, text=True, capture_output=True, timeout=40,
                    user=uid, group=workload.pw_gid, extra_groups=[])
            return call
        def supervisor(uid, request, root):
            return FilesystemSupervisor(client(uid), {"authority": authority.store.authority_id,
                "principal": policies[uid]["id"], "request": request, "fingerprint": request,
                "roots": [str(root)]}, {})
        with AuthorityServer(socket_path, authority) as server:
            os.chown(socket_path, 0, workload.pw_gid)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            a, b = supervisor(60001, "a", maintained), supervisor(60002, "b", maintained)
            other = supervisor(60002, "other", roots[1])
            try:
                a.prepare()
                with pytest.raises(OwnershipPending):
                    b.prepare()
                job = a.start('printf "%s" "$HOME"; printf original > maintained; '
                    'printf private > "$HOME/private"; mkdir -p "$CARGO_TARGET_DIR"; '
                    'printf cached > "$CARGO_TARGET_DIR/artifact"', cwd=str(maintained), environment_names=())
                wait_for(lambda: a.main_exit_code(job) is not None)
                assert a.exit_code(job) == 0, a.output(job)
                # The foreground receipt can precede PID1's final orphan reap
                # and manager cgroup settlement. Neither substitutes for the other.
                wait_for(lambda: a.inspect(job) is JobState.SETTLED)
                assert a.inspect(job) is JobState.SETTLED
                assert (maintained / "maintained").stat().st_uid == workload.pw_uid
                assert (maintained / "maintained").stat().st_gid == workload.pw_gid
                assert a.output(job) == a.runtime_dir + "/home"
                assert not (maintained / ".hermes").exists()
                probe = a.start(f"! cat -- /proc/1/fd/2 && ! cat -- {shlex.quote(str(sibling))} "
                    f"&& ! cat -- {shlex.quote(str(credentials))} && printf scoped",
                    cwd=str(maintained), environment_names=())
                wait_for(lambda: a.main_exit_code(probe) is not None)
                assert a.exit_code(probe) == 0, a.output(probe)
                other.prepare()
                forbidden = [a.runtime_dir + "/home/private", str(state / "claims.sqlite"),
                             str(maintained / "maintained"), str(credentials)]
                command = " && ".join(f"! cat -- {shlex.quote(path)}" for path in forbidden)
                command += f" && ! touch -- {shlex.quote(str(roots[0] / 'ungranted'))} && printf isolated"
                isolated = other.start(command, cwd=str(roots[1]), environment_names=())
                wait_for(lambda: other.main_exit_code(isolated) is not None)
                assert other.exit_code(isolated) == 0, other.output(isolated)
                assert other.output(isolated).endswith("isolated")
                assert not (roots[0] / "ungranted").exists()
                # Retain the detached writer after its PID-namespace leader exits.
                ready, release, finished = (maintained / name for name in
                                            ("daemon-ready", "daemon-release", "daemon-finished"))
                child = (f"touch {shlex.quote(str(ready))}; "
                         f"while test ! -e {shlex.quote(str(release))}; do sleep .05; done; "
                         f"printf finished > {shlex.quote(str(finished))}")
                daemon = a.start(
                    f"setsid bash -c {shlex.quote(child)} </dev/null >/dev/null 2>&1 & "
                    f"while test ! -e {shlex.quote(str(ready))}; do sleep .05; done",
                    cwd=str(maintained), environment_names=())
                wait_for(lambda: a.main_exit_code(daemon) is not None)
                assert a.main_exit_code(daemon) == 0  # Foreground exit precedes descendant settlement.
                assert ready.exists()
                assert a.inspect(daemon) is JobState.RUNNING
                assert not finished.exists()
                release.touch()
                wait_for(lambda: a.inspect(daemon) is JobState.SETTLED)
                assert finished.read_text() == "finished"
                a.seal()
                assert a.settled()
                with pytest.raises(OwnershipPending):
                    b.prepare()
                assert a.release()["state"] == "settled"
                b.prepare()
                job = b.start("printf next > maintained", cwd=str(maintained), environment_names=())
                wait_for(lambda: b.main_exit_code(job) is not None)
                assert b.exit_code(job) == 0, b.output(job)
                assert (maintained / "maintained").read_text() == "next"
                assert (maintained / "maintained").stat().st_uid == workload.pw_uid
                # Replace the host ancestor after validation but before the
                # manager receives the launch. Only the originally granted inode
                # may be writable inside the namespace; its replacement stays idle.
                row = authority.store.get("controller-b", "b")
                provider = authority._supervisor(row)
                execute = provider.execute
                entered, resume = threading.Event(), threading.Event()
                def delayed(script, stdin=None):
                    if "--unit=" in script:
                        entered.set()
                        assert resume.wait(15)
                    return execute(script, stdin)
                provider.execute = delayed
                moved = base / "relocated-domain"
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(b.start, "printf pinned > after-replacement",
                        cwd=str(maintained), environment_names=())
                    try:
                        assert entered.wait(15)
                        roots[0].rename(moved)
                        roots[0].mkdir(mode=0o700)
                        os.chown(roots[0], workload.pw_uid, workload.pw_gid)
                        maintained.mkdir(mode=0o700)
                        os.chown(maintained, workload.pw_uid, workload.pw_gid)
                    finally:
                        resume.set()
                    pinned = pending.result(timeout=40)
                provider.execute = execute
                wait_for(lambda: b.main_exit_code(pinned) is not None)
                assert b.exit_code(pinned) == 0, b.output(pinned)
                assert (moved / "project/after-replacement").read_text() == "pinned"
                assert not (maintained / "after-replacement").exists()
                with pytest.raises(AuthorityRequestRejected):
                    b.start("touch late", cwd=str(maintained), environment_names=())
                assert not (maintained / "late").exists()
            finally:
                try:
                    for owned in (a, b, other):
                        owned.stop()
                        assert owned.settled()
                        assert owned.release()["state"] == "settled"
                finally:
                    server.shutdown()
                    thread.join()
                    authority.close()
