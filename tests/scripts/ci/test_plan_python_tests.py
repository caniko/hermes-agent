"""Native CI shards must cover discovery exactly and reject stale plans."""

import json
import shutil

import pytest

from scripts.ci import plan_python_tests
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


def test_duration_cache_merges_only_each_shards_owned_measurements(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    files = ["tests/test_alpha.py", "tests/test_beta.py"]
    for name in files:
        (root / name).write_text("def test_smoke():\n    assert True\n")
    output, results = tmp_path / "plan", tmp_path / "results"
    revision = "a" * 40
    plan = create_plan(root, output, 2, revision)
    expected = {}
    for shard in plan["shards"]:
        folder = results / f"native-tests-{shard['index']}-attempt-2"
        folder.mkdir(parents=True)
        for name in ("plan.json", shard["file"]):
            shutil.copyfile(output / name, folder / name)
        (folder / "revision").write_text(revision)
        (folder / "exit-code").write_text("0")
        owned = (output / shard["file"]).read_text().strip()
        # Every shard restored the same old full-suite cache. Its inherited
        # values must not overwrite another shard's fresh measurement.
        data = dict.fromkeys(files, 99)
        data[owned] = shard["index"]
        expected[owned] = shard["index"]
        (folder / "test_durations.json").write_text(json.dumps(data))
    plan_python_tests.merge_durations(root, output, results, 2, revision)
    assert json.loads((root / "test_durations.json").read_text()) == expected
    (folder / "revision").write_text("b" * 40)
    with pytest.raises(ValueError, match="source"):
        plan_python_tests.merge_durations(root, output, results, 2, revision)
    assert json.loads((root / "test_durations.json").read_text()) == expected
