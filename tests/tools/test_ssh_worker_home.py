"""SSH worker state must follow the served profile, independently of the login home."""

import os
import shlex
import shutil

import pytest

from tools.environments.ssh import SSHEnvironment
from tools.terminal_scope import install_and_reset_profile_terminal_scope
from tools.terminal_tool import _get_env_config
from tools.terminal_tool_backends import _build_ssh_env, _ssh_config_from_config


@pytest.mark.platforms("posix")
def test_profile_worker_homes_sync_and_execute_independently(tmp_path, monkeypatch):
    """Exercise YAML -> scope -> backend -> real tar/shell I/O for A -> B -> A.

    The transport executes locally in a disposable login home; everything above
    transport (including sync-back) is real. No SSH service or model is needed.
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
    monkeypatch.setattr(SSHEnvironment, "_build_ssh_command", lambda self, *a, **k: [
        "env", f"HOME={login_home}", bash, "--noprofile", "--norc", "-c", 'eval "$*"', "ssh-fixture"])
    # Session snapshots are separately covered; do not source host login scripts.
    monkeypatch.setattr(SSHEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(SSHEnvironment, "_control_sockets", lambda self: [])

    profiles = {}
    for name in ("a", "b"):
        profile = tmp_path / f"profile-{name}"
        profile.mkdir()
        skill = profile / "skills" / "example" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"skill-{name}")
        remote = tmp_path / f"worker '{name}'"
        (profile / "config.yaml").write_text(
            f"terminal:\n  backend: ssh\n  ssh_host: fixture\n  ssh_user: worker\n"
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
                    assert (remote / "skills/example/SKILL.md").read_text() == skill.read_text()
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
