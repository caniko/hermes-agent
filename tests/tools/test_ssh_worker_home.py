"""SSH worker state must follow the served profile, independently of the login home."""

import os
import shlex
import shutil
import stat
from contextlib import nullcontext

import pytest

from tools.environments.ssh import SSHEnvironment
from tools.terminal_scope import install_and_reset_profile_terminal_scope
from tools.terminal_tool import _get_env_config
from tools.terminal_tool_backends import _build_ssh_env, _ssh_config_from_config


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("transport", ["shell", "openssh"])
def test_profile_worker_homes_sync_and_execute_independently(tmp_path, monkeypatch, transport):
    """Exercise YAML -> scope -> backend -> real tar/shell I/O for A -> B -> A.

    Both transports use a disposable login home; the OpenSSH variant starts its
    own loopback sshd. Synchronization and command I/O are real; no model is used.
    """
    login_home = tmp_path / "login"
    personal = login_home / ".hermes"
    personal.mkdir(parents=True)
    sentinel = personal / "personal.txt"
    sentinel.write_text("personal")
    workspace = tmp_path / "data"
    workspace.mkdir()
    bash = shutil.which("bash")
    assert bash
    if transport == "shell":
        monkeypatch.setattr(SSHEnvironment, "_build_ssh_command", lambda self, *a, **k: [
            "env", f"HOME={login_home}", bash, "--noprofile", "--norc", "-c", 'eval "$*"', "ssh-fixture"])
        monkeypatch.setattr(SSHEnvironment, "_control_sockets", lambda self: [])
        connection = nullcontext({"host": "fixture", "user": "worker", "port": 22, "key": ""})
    else:
        from tests.tools.ssh_worker_transport import openssh_transport
        connection = openssh_transport(tmp_path / "sshd", login_home, monkeypatch)
    # Avoid sourcing host login scripts in the transport fixture.
    monkeypatch.setattr(SSHEnvironment, "init_session", lambda self: None)

    with connection as target:
        _exercise_profiles(tmp_path, workspace, target, sentinel, personal)


def _exercise_profiles(tmp_path, workspace, target, sentinel, personal):

    profiles = {}
    for name in ("a", "b"):
        profile = tmp_path / f"profile-{name}"
        profile.mkdir()
        skill = profile / "skills" / "example" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"skill-{name}: café", encoding="utf-8")
        remote = tmp_path / f"worker '{name}'"
        remote.mkdir(mode=0o750)
        (profile / "config.yaml").write_text(
            f"terminal:\n  backend: ssh\n  ssh_host: {target['host']}\n  ssh_user: {target['user']}\n"
            f"  ssh_port: {target['port']}\n  ssh_key: {target['key']}\n"
            f"  ssh_hermes_home: {remote}\n  cwd: {workspace}\n")
        profiles[name] = (profile, remote, skill)

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    sockets = {}
    for name in ("a", "b", "a"):
        profile, remote, skill = profiles[name]
        token = set_hermes_home_override(profile)
        try:
            with install_and_reset_profile_terminal_scope(profile):
                cfg = _get_env_config()
                env = _build_ssh_env(cwd=cfg["cwd"], timeout=10,
                                     ssh_config=_ssh_config_from_config(cfg))
                try:
                    assert (remote / "skills/example/SKILL.md").read_text(encoding="utf-8") == skill.read_text(encoding="utf-8")
                    assert stat.S_IMODE(remote.stat().st_mode) == 0o750
                    assert stat.S_IMODE((remote / "skills/example").stat().st_mode) == 0o700
                    proc = env._run_bash(
                        'printf "%s\\n" "$HERMES_HOME"; '
                        f'printf updated-{name} > "$HERMES_HOME/skills/example/SKILL.md"; '
                        f'touch {shlex.quote(str(workspace / name))}')
                    output, _ = proc.communicate(timeout=10)
                    assert proc.returncode == 0
                    assert output.strip() == str(remote)
                    sockets[name] = env.control_socket
                finally:
                    env.cleanup()
                    env._sync_manager = None  # __del__ must not retry after the transport fixture is gone.
                assert skill.read_text() == f"updated-{name}"
        finally:
            reset_hermes_home_override(token)
    assert sockets["a"] != sockets["b"]
    assert sentinel.read_text() == "personal"
    assert sorted(p.name for p in personal.iterdir()) == ["personal.txt"]
    assert all((workspace / name).stat().st_uid == os.getuid() for name in ("a", "b"))


@pytest.mark.parametrize("home", ["relative", "~/worker", "/", "/data/../", "/bad\x00path"])
def test_invalid_worker_home_is_rejected_before_connecting(home, monkeypatch):
    def connect(self):
        pytest.fail("invalid worker home reached SSH")

    monkeypatch.setattr(SSHEnvironment, "_establish_connection", connect)
    with pytest.raises(ValueError, match="ssh_hermes_home"):
        SSHEnvironment(host="fixture", user="worker", hermes_home=home)
