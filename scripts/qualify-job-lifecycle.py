"""Require completed native lifecycle cases without skipped or failed proof."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


REQUIRED = {
    "test_supervisor_supplies_eof_without_consuming_parent_stdin",
    "test_supervisor_waits_for_daemon_and_recovers_stop_fence[local]",
    "test_supervisor_waits_for_daemon_and_recovers_stop_fence[ssh]",
    "test_stop_waits_for_slow_jobs_together_and_settles_every_cgroup[local]",
    "test_stop_waits_for_slow_jobs_together_and_settles_every_cgroup[ssh]",
    "test_systemd_submission_delayed_past_seal_cannot_execute_work[local]",
    "test_systemd_submission_delayed_past_seal_cannot_execute_work[ssh]",
    "test_two_gateways_park_before_tools_and_keep_key_rotation_identity[local]",
    "test_two_gateways_park_before_tools_and_keep_key_rotation_identity[ssh]",
    "test_restarted_gateway_stops_orphan_jobs_before_terminal_status",
    "test_stop_admission_fences_delayed_requests_after_restart[before_create]",
    "test_stop_admission_fences_delayed_requests_after_restart[during_admission]",
    "test_system_provider_preserves_uid_and_confines_same_uid_workers",
    "test_stop_during_supervisor_preparation_cannot_construct_an_agent[local]",
    "test_stop_during_supervisor_preparation_cannot_construct_an_agent[ssh]",
    "test_supervised_local_command_ignores_non_identifier_inherited_environment[local]",
}


def qualify(report, provenance):
    raw = report.read_bytes()
    root = ET.fromstring(raw)
    cases = root.findall(".//testcase")
    names = {case.get("name") for case in cases}
    missing = REQUIRED - names
    unsuccessful = [case.get("name") for case in cases
                    if any(case.find(tag) is not None for tag in ["failure", "error", "skipped"])]
    suite_errors = root.findall(".//testsuite/error")
    if missing or unsuccessful or suite_errors:
        raise ValueError(f"Incomplete lifecycle proof: missing={sorted(missing)}, unsuccessful={unsuccessful}")
    if not re.fullmatch(r"[0-9a-f]{40}", provenance.get("revision") or ""):
        raise ValueError("Lifecycle proof requires an immutable source revision")
    return {
        "schemaVersion": 1,
        "scope": "hermes-target-job-lifecycle",
        "passed": True,
        "source": provenance,
        "tests": len(cases),
        "reportSha256": hashlib.sha256(raw).hexdigest(),
        "productionCredentialsUsed": False,
        "paperclipDispatchQualified": False,
    }


def retain():
    evidence = Path(os.environ["SIMIT_NIX_BUILD_RESULTS"])
    if (evidence / "installable").read_text(encoding="utf-8-sig").strip() != ".#checks.x86_64-linux.target-job-lifecycle":
        raise ValueError("Unexpected lifecycle installable")
    results = json.loads((evidence / "result.json").read_text(encoding="utf-8-sig"))
    if len(results) != 1 or set(results[0]["outputs"]) != {"out"}:
        raise ValueError("Expected one lifecycle output")
    output = Path(results[0]["outputs"]["out"])
    if not re.fullmatch(r"/nix/store/[a-z0-9]{32}-[^/]+", str(output)):
        raise ValueError("Lifecycle output must be a store path")
    receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8-sig"))
    checked = qualify(output / "lifecycle.xml", receipt["source"])
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, encoding="utf-8").strip()
    if receipt != checked or receipt["source"]["revision"] != revision:
        raise ValueError("Lifecycle receipt does not match the checkout and report")
    for name in ["receipt.json", "lifecycle.xml"]:
        shutil.copyfile(output / name, evidence / name)


if __name__ == "__main__":
    if sys.argv[1:] == ["retain"]:
        retain()
    else:
        report, source, destination = map(Path, sys.argv[1:])
        receipt = qualify(report, json.loads(source.read_text(encoding="utf-8-sig")))
        destination.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
