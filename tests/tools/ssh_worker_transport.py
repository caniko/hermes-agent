"""Disposable OpenSSH transport for the remote worker-home contract test."""

import contextlib
import os
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
import time

import pytest


@contextlib.contextmanager
def openssh_transport(root, login_home, monkeypatch):
    sshd = shutil.which("sshd")
    if not sshd:
        pytest.skip("OpenSSH server is not installed")
    bash = shutil.which("bash")
    ssh = shutil.which("ssh")
    keygen = shutil.which("ssh-keygen")
    assert bash and ssh and keygen
    root.mkdir()
    for name in ("host", "client"):
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(root / name)], check=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    user = pwd.getpwuid(os.getuid()).pw_name
    fixture_path = os.path.dirname(sys.executable) + ':' + os.environ['PATH']
    (login_home / ".bash_profile").write_text(f"export PATH={shlex.quote(fixture_path)}\n")
    command = root / "command"
    command.write_text(
        f"#!{bash}\nexport HOME={shlex.quote(str(login_home))}\n"
        f"export PATH={shlex.quote(fixture_path)}\n"
        "export __ETC_PROFILE_SOURCED=1\n"
        f"exec {shlex.quote(bash)} --noprofile --norc -c \"$SSH_ORIGINAL_COMMAND\"\n")
    command.chmod(0o700)
    config = root / "sshd_config"
    config.write_text(
        f"ListenAddress 127.0.0.1\nPort {port}\nHostKey {root / 'host'}\n"
        f"PidFile {root / 'pid'}\nAuthorizedKeysFile {root / 'client.pub'}\n"
        f"AllowUsers {user}\nStrictModes no\nUsePAM no\nPasswordAuthentication no\n"
        f"KbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n"
        f"AllowAgentForwarding no\nAllowTcpForwarding no\nX11Forwarding no\n"
        f"ForceCommand {command}\n")
    known = root / "known_hosts"
    known.write_text(f"[127.0.0.1]:{port} " + (root / "host.pub").read_text())
    client_config = root / "ssh_config"
    client_config.write_text(
        f"Host *\n  UserKnownHostsFile {known}\n  GlobalKnownHostsFile /dev/null\n"
        f"  IdentitiesOnly yes\n  IdentityAgent none\n  StrictHostKeyChecking yes\n")
    from tools.environments.ssh import SSHEnvironment

    build = SSHEnvironment._build_ssh_command
    monkeypatch.setattr(SSHEnvironment, "_build_ssh_command", lambda self, *a, **k: [
        ssh, "-F", str(client_config), *build(self, *a, **k)[1:]])
    # Production creates/uses real masters and performs its normal cleanup.
    with (root / "sshd.log").open("w+") as log:
        server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while True:
                if server.poll() is not None:
                    log.seek(0)
                    pytest.fail(f"fixture sshd exited: {log.read()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        pytest.fail("fixture sshd did not listen within 10 seconds")
                    time.sleep(0.05)
            yield {"host": "127.0.0.1", "user": user, "port": port, "key": str(root / "client")}
        finally:
            server.terminate()
            server.wait(timeout=10)
