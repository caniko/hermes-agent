"""Retained generation artifacts must contain every checksummed workflow."""

import glob
from pathlib import Path

import hermes_yaml as yaml


def test_generation_upload_covers_checksum_inputs():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/generate-native-ci.yaml").read_text())
    upload = next(step for step in workflow["jobs"]["generate"]["steps"]
                  if step.get("with", {}).get("name", "").startswith("native-ci-generation-"))
    retained = {
        Path(path).relative_to(root).as_posix()
        for pattern in upload["with"]["path"].splitlines()
        for path in glob.glob(str(root / pattern.removeprefix("${{ runner.temp }}/native-ci-generation/")),
                              recursive=True, include_hidden=True)
        if Path(path).is_file()
    }
    checksummed = {path.relative_to(root).as_posix()
                   for directory in (".github/workflows", ".simit/workflows")
                   for path in (root / directory).rglob("*") if path.is_file()}
    assert checksummed <= retained, sorted(checksummed - retained)
