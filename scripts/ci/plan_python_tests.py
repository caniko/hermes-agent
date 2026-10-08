"""Freeze the native runner's discovery into source-bound, disjoint CI shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.run_tests_parallel import (
    _compute_lpt_slices,
    _discover_files,
    _load_durations,
)  # noqa: E402


def _discovery(root: Path) -> list[Path]:
    return _discover_files([root / "tests"])


def create_plan(root: Path, output: Path, count: int, revision: str) -> dict:
    files = _discovery(root)
    if not 1 <= count <= len(files):
        raise ValueError("shard count must select nonempty shards")
    slices = _compute_lpt_slices(files, count, _load_durations(root), root)
    output.mkdir(parents=True, exist_ok=False)
    shards = []
    for index, selected in enumerate(slices, 1):
        raw = "".join(
            path.relative_to(root).as_posix() + "\n" for path in selected
        ).encode()
        name = f"shard-{index}.txt"
        (output / name).write_bytes(raw)
        shards.append({
            "index": index,
            "file": name,
            "count": len(selected),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    plan = {
        "schemaVersion": 1,
        "revision": revision,
        "fileCount": len(files),
        "shards": shards,
    }
    (output / "plan.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8"
    )
    for index in range(1, count + 1):
        verify_plan(root, output, index, revision)
    return plan


def verify_plan(root: Path, output: Path, index: int, revision: str) -> Path:
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8-sig"))
    if plan["schemaVersion"] != 1 or plan["revision"] != revision:
        raise ValueError("test plan revision does not match execution source")
    selected = None
    combined = []
    for expected_index, shard in enumerate(plan["shards"], 1):
        name = f"shard-{expected_index}.txt"
        if shard["index"] != expected_index or shard["file"] != name:
            raise ValueError("invalid shard identity")
        path = output / name
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != shard["sha256"]:
            raise ValueError("test shard digest does not match plan")
        files = raw.decode().splitlines()
        if not files or len(files) != shard["count"]:
            raise ValueError("test shard must contain its declared nonempty file set")
        combined.extend(files)
        if expected_index == index:
            selected = path
    discovery = [path.relative_to(root).as_posix() for path in _discovery(root)]
    if sorted(combined) != sorted(discovery) or len(combined) != plan["fileCount"]:
        raise ValueError("test plan must cover current discovery exactly once")
    if selected is None:
        raise ValueError("unknown shard index")
    return selected


def merge_durations(
    root: Path, directory: Path, results: Path, attempt: int, revision: str,
) -> None:
    plan_bytes = (directory / "plan.json").read_bytes()
    plan = json.loads(plan_bytes)
    merged = {}
    for shard in plan["shards"]:
        selected = verify_plan(root, directory, shard["index"], revision)
        # Failed-job reruns retain successful shards from earlier attempts.
        for number in range(attempt, 0, -1):
            artifact = results / f"native-tests-{shard['index']}-attempt-{number}"
            if artifact.is_dir():
                break
        else:
            raise FileNotFoundError("no shard artifact from this or a prior attempt")
        if (artifact / "revision").read_text(encoding="utf-8-sig").strip() != revision:
            raise ValueError("duration artifact source does not match execution")
        if (
            (artifact / "plan.json").read_bytes() != plan_bytes
            or (artifact / shard["file"]).read_bytes() != selected.read_bytes()
        ):
            raise ValueError("duration artifact does not match the test plan")
        if (artifact / "exit-code").read_text(encoding="utf-8-sig").strip() != "0":
            raise ValueError("cannot cache durations from a failed shard")
        durations = json.loads(
            (artifact / "test_durations.json").read_text(encoding="utf-8-sig")
        )
        if not isinstance(durations, dict):
            raise ValueError("duration artifact must contain a file-to-duration map")
        for name in selected.read_text(encoding="utf-8-sig").splitlines():
            # The runner omits unhealthy timings; inherited values for other
            # shards must never overwrite their owner's healthy measurements.
            if name not in durations:
                continue
            value = durations[name]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("duration must be a positive finite number")
            merged[name] = value
    if not merged:
        raise ValueError("no healthy shard durations to cache")
    # Validate every artifact before atomically replacing the previous cache.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as f:
        json.dump(merged, f, indent=2, sort_keys=True)
        f.write("\n")
        temporary = Path(f.name)
    temporary.replace(root / "test_durations.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "verify", "merge"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--index", type=int, default=1)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--attempt", type=int)
    args = parser.parse_args()
    root = Path.cwd()
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, encoding="utf-8", errors="replace"
    ).strip()
    if args.command == "create":
        plan = create_plan(root, args.directory, args.count, revision)
        print(json.dumps({"index": [shard["index"] for shard in plan["shards"]]}))
    elif args.command == "verify":
        print(verify_plan(root, args.directory, args.index, revision))
    else:
        if args.results is None or args.attempt is None:
            parser.error("merge requires --results and --attempt")
        merge_durations(root, args.directory, args.results, args.attempt, revision)


if __name__ == "__main__":
    main()
