"""Upgrade CI must refresh official release refs even when fork tags resolve."""

import os
from pathlib import Path
import subprocess

import pytest

import hermes_yaml as yaml


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("stale_tags", [False, True, "conflicting"])
def test_upgrade_baseline_refreshes_release_tags(tmp_path, stale_tags):
    def git(directory, *args, input=None):
        return subprocess.check_output(["git", *args], cwd=directory, input=input,
                                       text=True, timeout=30).strip()

    upstream, fork = tmp_path / "upstream", tmp_path / "fork"
    # CI checkouts are shallow. Build disposable history using the source's exact
    # identity fields, without changing configured identity or signing policy.
    identities = "\n".join(line for line in git(ROOT, "cat-file", "commit", "HEAD").split("\n\n", 1)[0].splitlines()
                           if line.startswith(("author ", "committer ")))
    git(tmp_path, "init", "--bare", str(upstream))
    tree = git(upstream, "mktree", input="")
    commits = []
    for index in range(3):
        parent = f"parent {commits[-1]}\n" if commits else ""
        commits.append(git(upstream, "hash-object", "-w", "-t", "commit", "--stdin",
                           input=f"tree {tree}\n{parent}{identities}\n\nfixture {index}\n"))
    git(upstream, "update-ref", "HEAD", commits[-1])
    older, latest = "v2099.01.01", "v2099.01.02"
    git(upstream, "update-ref", "refs/tags/" + older, commits[0])
    git(upstream, "update-ref", "refs/tags/" + latest, commits[1])
    git(tmp_path, "clone", "--no-tags", "--no-checkout", str(upstream), str(fork))
    if stale_tags:
        git(fork, "update-ref", "refs/tags/" + older, "HEAD~2")
        assert git(fork, "describe", "--tags", "--abbrev=0", "HEAD~1") == older
    if stale_tags == "conflicting":
        git(fork, "update-ref", "refs/tags/" + latest, "HEAD~2")

    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    step = next(step for step in workflow["jobs"]["e2e-upgrade"]["steps"]
                if "RELEASE_TAG_SOURCE" in step.get("env", {}))
    result = subprocess.run(["bash", "--noprofile", "--norc", "-c", step["run"]],
                            cwd=fork, env={**os.environ, "RELEASE_TAG_SOURCE": str(upstream)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == latest
    assert git(fork, "rev-parse", "refs/tags/" + latest) == commits[1]
    assert git(fork, "describe", "--tags", "--abbrev=0", "HEAD~1") == latest
