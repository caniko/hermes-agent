"""Contributor auditing follows the selected PR comparison, including frozen bases."""

import subprocess

from scripts import audit_pr_attribution as audit


def test_frozen_base_excludes_inherited_history_and_retains_new_authors(
    tmp_path, monkeypatch
):
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *args], text=True, encoding="utf-8"
        ).strip()

    git("init", "--initial-branch=main")
    # Disposable fixture objects: these identities never enter project history.
    stream = (
        "commit refs/heads/main\n"
        "committer Fixture <inherited@example.invalid> 1000000000 +0000\n"
        "data 10\ninherited\n\n"
        "commit refs/heads/qualification\n"
        "committer Fixture <repair@example.invalid> 1000000001 +0000\n"
        "data 7\nrepair\n\n"
        "from refs/heads/main\n"
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "fast-import", "--quiet"],
        input=stream,
        text=True,
        encoding="utf-8",
        check=True,
    )
    monkeypatch.setattr(audit, "REPO_ROOT", tmp_path)
    frozen = git("rev-parse", "refs/heads/main")
    head = git("rev-parse", "refs/heads/qualification")
    assert audit.new_emails(frozen, frozen) == []
    assert audit.new_emails(frozen, head) == ["repair@example.invalid"]
    assert not audit.is_mapped("repair@example.invalid")
    mappings = tmp_path / "contributors/emails"
    mappings.mkdir(parents=True)
    (mappings / "repair@example.invalid").write_text("fixture\n", encoding="utf-8")
    assert audit.is_mapped("repair@example.invalid")
