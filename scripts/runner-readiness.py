#!/usr/bin/env python3
"""Read supported GitHub larger-runner configuration before qualification."""
import argparse
import json
import os
from pathlib import Path
import urllib.request


def api(path):
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def configured(label, memory_gb):
    repository = os.environ["GITHUB_REPOSITORY"]
    owner = repository.split("/")[0]
    repo = api(f"repos/{repository}")
    require(repo["owner"]["type"] == "Organization",
            "GitHub larger runners require an organization-owned repository; owner readiness is absent")
    require(label, "Set QUALIFICATION_LARGER_RUNNER to the supported hosted runner name")
    runners = []
    page = 1
    while True:
        batch = api(f"orgs/{owner}/actions/hosted-runners?per_page=100&page={page}")["runners"]
        runners.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    matches = [runner for runner in runners if runner["name"] == label]
    require(len(matches) == 1, "The requested larger runner is not uniquely configured")
    runner = matches[0]
    require(runner["status"].lower() == "ready" and runner["maximum_runners"] > 0,
            "The requested hosted runner is not Ready or has no capacity allocation")
    require(runner["machine_size"]["memory_gb"] >= memory_gb, "Hosted runner memory is below the qualification floor")
    group = api(f"orgs/{owner}/actions/runner-groups/{runner['runner_group_id']}")
    require(not repo["private"] or group["visibility"] in ("all", "private", "selected"), "Runner group excludes this repository")
    require(repo["private"] or group.get("allows_public_repositories") is True,
            "Runner group does not allow this public repository")
    if group["visibility"] == "selected":
        allowed = []
        page = 1
        while True:
            batch = api(f"orgs/{owner}/actions/runner-groups/{group['id']}/repositories?per_page=100&page={page}")["repositories"]
            allowed.extend(item["id"] for item in batch)
            if len(batch) < 100:
                break
            page += 1
        require(repo["id"] in allowed, "Runner group has not granted this repository access")
    require(not group.get("restricted_to_workflows"),
            "Workflow-restricted runner groups require an explicit owner workflow-access readback")
    return {"schema": "hosted-runner-readiness.v1", "qualified": False,
            "repository": repository, "run_id": os.environ["GITHUB_RUN_ID"],
            "runner": runner, "runner_group": group, "minimum_memory_gb": memory_gb}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default=os.environ.get("QUALIFICATION_LARGER_RUNNER", ""))
    parser.add_argument("--memory-gb", type=int, default=64)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = configured(args.label, args.memory_gb)
    except Exception as error:
        receipt = {"schema": "hosted-runner-readiness.v1", "qualified": False,
                   "ready": False, "error": str(error), "requested_label": args.label,
                   "minimum_memory_gb": args.memory_gb}
        args.destination.write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    args.destination.write_text(json.dumps(receipt, indent=2) + "\n")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"label={args.label}\n")


if __name__ == "__main__":
    main()
