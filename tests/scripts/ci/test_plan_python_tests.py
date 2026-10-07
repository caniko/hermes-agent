"""Native CI shards must cover discovery exactly and reject stale plans."""

import pytest

from scripts.ci.plan_python_tests import create_plan, verify_plan


def test_shards_run_each_discovered_file_once(tmp_path):
    root = tmp_path / "project"
    expected = []
    for name in (
        "agent/test_alpha.py",
        "tools/test_beta.py",
        "test_gamma.py",
        "test_delta.py",
    ):
        path = root / "tests" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
        expected.append(path.relative_to(root).as_posix())
    excluded = root / "tests/e2e/test_separate_lane.py"
    excluded.parent.mkdir(parents=True)
    excluded.write_text("def test_separate():\n    assert True\n", encoding="utf-8")
    output = tmp_path / "plan"
    revision = "a" * 40
    plan = create_plan(root, output, 3, revision)
    selected = []
    for shard in plan["shards"]:
        path = verify_plan(root, output, shard["index"], revision)
        assert path == output / shard["file"]
        files = path.read_text(encoding="utf-8").splitlines()
        assert files and len(files) == shard["count"]
        selected.extend(files)
    assert sorted(selected) == sorted(expected)


def test_stale_or_changed_shard_is_rejected(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_alpha.py").write_text(
        "def test_alpha():\n    assert True\n", encoding="utf-8"
    )
    output = tmp_path / "plan"
    revision = "a" * 40
    create_plan(root, output, 1, revision)
    with pytest.raises(ValueError, match="revision"):
        verify_plan(root, output, 1, "b" * 40)
    (tests / "test_new.py").write_text(
        "def test_new():\n    assert True\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="discovery"):
        verify_plan(root, output, 1, revision)
    (output / "shard-1.txt").write_text("tests/test_new.py\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        verify_plan(root, output, 1, revision)
