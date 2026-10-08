#!/usr/bin/env python3
"""Exact-head hosted qualification receipts and provider-retention readback."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import urllib.request
import xml.etree.ElementTree as ET

MIN_RETENTION_SECONDS = 31 * 24 * 60 * 60


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def api(path):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{path}",
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def identity():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8-sig"))
    require(os.environ["GITHUB_EVENT_NAME"] == "pull_request", "Qualification requires a PR event")
    pr = event["pull_request"]
    head = pr["head"]["sha"]
    require(api(f"pulls/{pr['number']}")["head"]["sha"] == head, "PR head advanced; evidence is historical")
    require(int(os.environ["GITHUB_RUN_ATTEMPT"]) == 1, "Retries cannot qualify; publish a source successor")
    return {"repository": os.environ["GITHUB_REPOSITORY"], "pr": pr["number"], "head": head,
            "base": pr["base"]["sha"], "workflow_ref": os.environ["GITHUB_WORKFLOW_REF"],
            "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"], "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": 1}


def initialize(directory):
    require(os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "Only GitHub-hosted runners qualify")
    require(int(os.environ["GITHUB_RETENTION_DAYS"]) >= 31, "Repository/organization retention must allow 31 days")
    source = identity()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, encoding="utf-8", timeout=30).strip()
    require(revision == source["head"], "Checkout is not the exact PR head")
    source.update({"tested_source": revision, "platform": platform.platform(), "machine": platform.machine(),
                   "receipt_tool_sha256": sha256(__file__),
                   "locks": {str(path): sha256(path) for path in
                             (Path("pnpm-lock.yaml"), Path("flake.lock"), Path("Cargo.lock"),
                              Path("uv.lock"), Path("packages/paperclip-runner/runner/Cargo.lock")) if path.is_file()}})
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source.json").write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")


def seal(directory, outcome, strict_reports):
    source = json.loads((directory / "source.json").read_text(encoding="utf-8-sig"))
    reports = []
    rejected = []
    for path in sorted(directory.rglob("*.xml")):
        root = ET.parse(path).getroot()
        cases = root.findall(".//testcase")
        require(cases, f"Empty JUnit report: {path}")
        identities = [(case.get("classname"), case.get("name")) for case in cases]
        failures = root.findall(".//failure") + root.findall(".//error")
        skipped = root.findall(".//skipped")
        retried = [node for node in root.iter()
                   if any(marker in node.tag.lower() for marker in ("retry", "rerun", "flaky"))]
        if outcome == "success":
            if len(identities) != len(set(identities)):
                rejected.append(f"Duplicate/retried JUnit cases: {path}")
            if failures or retried:
                rejected.append(f"Failed or retried test: {path}")
            if strict_reports and skipped:
                rejected.append(f"Mandatory test skipped: {path}")
        reports.append({"path": str(path.relative_to(directory)), "cases": len(cases),
                        "failures": len(failures), "skipped": len(skipped), "retries": len(retried)})
    if strict_reports:
        if not reports:
            rejected.append("Mandatory JUnit evidence is missing")
    receipt = {"schema": "hosted-qualification.v1", **source, "outcome": outcome,
                "qualified": False, "reports": reports, "rejected": rejected,
               "members": {str(path.relative_to(directory)): sha256(path)
                           for path in sorted(directory.rglob("*")) if path.is_file() and path.name != "receipt.json"}}
    (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    require(not rejected, "; ".join(rejected))


def artifacts(prefix, count, destination):
    receipt = {"schema": "hosted-retention.v1", "qualified": False,
               "artifacts": [], "rejected": []}
    try:
        receipt.update(identity())
        page = 1
        while True:
            response = api(f"actions/runs/{receipt['run_id']}/artifacts?per_page=100&page={page}")
            for artifact in response["artifacts"]:
                if not artifact["name"].startswith(prefix):
                    continue
                created = dt.datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00"))
                expiry = dt.datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00"))
                lifetime = (expiry - created).total_seconds()
                digest = artifact.get("digest", "")
                receipt["artifacts"].append({
                    "id": artifact["id"], "name": artifact["name"], "sha256": digest,
                    "created_at": artifact["created_at"], "expires_at": artifact["expires_at"],
                    "retention_seconds": lifetime, "expired": artifact["expired"],
                    "workflow_run": artifact.get("workflow_run"),
                })
                require(not artifact["expired"] and lifetime >= MIN_RETENTION_SECONDS,
                        f"Artifact {artifact['id']} lifetime is only {lifetime} seconds")
                require(digest.startswith("sha256:") and len(digest) == 71, "Provider artifact SHA-256 is missing")
                require(artifact["workflow_run"]["head_sha"] == receipt["head"], "Artifact workflow source mismatch")
            if len(response["artifacts"]) < 100:
                break
            page += 1
        require(len(receipt["artifacts"]) == count,
                f"Expected {count} required artifacts, found {len(receipt['artifacts'])}")
    except Exception as error:
        receipt["rejected"].append(f"{type(error).__name__}: {error}")
        raise
    finally:
        Path(destination).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("initialize")
    init.add_argument("directory", type=Path)
    finish = sub.add_parser("seal")
    finish.add_argument("directory", type=Path)
    finish.add_argument("outcome")
    finish.add_argument("--strict-reports", action="store_true")
    audit = sub.add_parser("artifacts")
    audit.add_argument("prefix")
    audit.add_argument("count", type=int)
    audit.add_argument("destination")
    args = parser.parse_args()
    if args.command == "initialize":
        initialize(args.directory)
    elif args.command == "seal":
        seal(args.directory, args.outcome, args.strict_reports)
    else:
        artifacts(args.prefix, args.count, args.destination)


if __name__ == "__main__":
    main()
