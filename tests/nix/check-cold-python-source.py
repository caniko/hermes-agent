"""Evaluate the real Python environment with an unregistered filtered source."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import quote, urlencode
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    root = args.root.resolve()
    reference = "git+" + root.as_uri() + "?shallow=1"
    # Metadata may name a lazy Git source that has not reached the store yet.
    # Archive the locked inputs, leaving the UUID-renamed payload below cold.
    archive = subprocess.run(
        ["nix", "flake", "archive", "--json", "--no-update-lock-file", reference],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=args.timeout_seconds,
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
        timeout=args.timeout_seconds,
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
    with tempfile.TemporaryDirectory(
        prefix="hermes-cold-source-", dir=args.temp_root
    ) as directory:
        fixture = Path(directory)
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
            timeout=args.timeout_seconds,
        )
    print("PASS: the Python environment evaluates with a cold filtered build source")


if __name__ == "__main__":
    main()
