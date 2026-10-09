"""Canonical attribution honors scoped aliases without crediting unrelated authors."""

import subprocess

from scripts import audit_pr_attribution as audit


def test_branch_attribution_keeps_shared_placeholders_name_scoped(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True,
            text=True, encoding="utf-8",
        ).stdout.strip()

    git("init")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@local.test")
    git("config", "commit.gpgsign", "false")
    git("commit", "--allow-empty", "-m", "base")
    git("branch", "origin/main")
    (tmp_path / ".mailmap").write_text(
        "Known <123+known@users.noreply.github.com> Known <you@example.com>\n"
        "Alias <456+alias@users.noreply.github.com> <Alias@Machine.local>\n",
        encoding="utf-8",
    )
    for name, email in [
        ("Known", "you@example.com"),
        ("Other", "you@example.com"),
        ("Alias", "Alias@Machine.local"),
    ]:
        git("commit", "--allow-empty", "-m", name, "--author", f"{name} <{email}>")

    monkeypatch.setattr(audit, "REPO_ROOT", tmp_path)
    emails = audit.new_emails()
    assert emails == ["123+known@users.noreply.github.com", "456+alias@users.noreply.github.com", "you@example.com"]
    assert [email for email in emails if not audit.is_mapped(email)] == ["you@example.com"]
