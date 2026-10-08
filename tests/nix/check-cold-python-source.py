"""Evaluate the real Python environment with an unregistered filtered source."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import quote, urlencode
import uuid


def snapshot_git_source(root, destination, timeout):
    """Capture tracked working contents without ignored build/venv artifacts."""
    entries = subprocess.check_output(
        ["git", "ls-files", "--stage", "-z"], cwd=root, timeout=timeout
    )
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        if metadata.split()[0] == b"160000":
            raise RuntimeError("Cold-source fixture does not support Git submodules")
        relative = Path(os.fsdecode(name))
        source = root / relative
        target = destination / relative
        if not source.exists() and not source.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def check_cold_source(root, snapshot, fixture, timeout):
    # Nix 2.35 can hash a shallow worktree and its fetched Git tree differently.
    # Archive one tracked snapshot, leaving the UUID-renamed payload below cold.
    reference = "path:" + str(snapshot)
    archive = subprocess.run(
        ["nix", "flake", "archive", "--json", "--no-update-lock-file", reference],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    metadata = subprocess.run(
        [
            "nix",
            "flake",
            "metadata",
            "--json",
            "--no-update-lock-file",
            reference,
        ],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    captured = json.loads(metadata.stdout)
    if json.loads(archive.stdout)["path"] != captured["path"]:
        raise RuntimeError("The archived source changed during metadata capture")
    fields = {
        name: captured["locked"][name]
        for name in ["narHash", "rev", "revCount", "lastModified"]
        if name in captured["locked"]
    }
    source = "path:" + quote(captured["path"], safe="/") + "?" + urlencode(fields)
    source_literal = json.dumps(source).replace("${", "\\${")
    # A new source name makes a warm evaluator/store unable to hide this bug.
    name_literal = json.dumps("hermes-cold-python-" + uuid.uuid4().hex)
    expression = f"""{{
  outputs = _: let
    f = builtins.getFlake {source_literal};
    pkgs = f.inputs.nixpkgs.legacyPackages.x86_64-linux;
    npm = pkgs.callPackage (f.outPath + "/nix/lib.nix") {{
      npm-lockfile-fix = f.inputs.npm-lockfile-fix.packages.x86_64-linux.default;
    }};
    python = pkgs.callPackage (f.outPath + "/nix/python.nix") {{
      inherit (f.inputs) uv2nix pyproject-nix pyproject-build-systems;
      pythonSrc = pkgs.lib.cleanSourceWith {{
        src = npm.pythonSrc;
        name = {name_literal};
        filter = _: _: true;
      }};
    }};
  in {{ checks.x86_64-linux.cold-python-source = python.venv; }};
}}
"""
    fixture.mkdir()
    (fixture / "flake.nix").write_text(expression, encoding="utf-8")
    subprocess.run(
        [
            "nix",
            "flake",
            "check",
            "path:" + str(fixture),
            "--no-build",
            "--no-update-lock-file",
            "--no-allow-import-from-derivation",
            "--show-trace",
        ],
        cwd=root,
        check=True,
        timeout=timeout,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    root = args.root.resolve()
    with tempfile.TemporaryDirectory(
        prefix="hermes-cold-source-", dir=args.temp_root
    ) as directory:
        temporary = Path(directory)
        snapshot = temporary / "source"
        snapshot_git_source(root, snapshot, args.timeout_seconds)
        check_cold_source(root, snapshot, temporary / "fixture", args.timeout_seconds)
    print("PASS: the Python environment evaluates with a cold filtered build source")


if __name__ == "__main__":
    main()
