"""Require completed native lifecycle cases without skipped or failed proof."""

import base64
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
}

DIAGNOSTIC_MARKER = "HERMES_LIFECYCLE_DIAGNOSTIC_V1 "
DIAGNOSTIC_FILES = {"lifecycle.xml", "source.json"}
MAX_DIAGNOSTIC_BYTES = 1024 * 1024
CHUNK_BYTES = 1024


def emit_diagnostics(report, source):
    """Keep completed failed-suite bytes in the retained Nix log before assertion."""
    for name, path in [("lifecycle.xml", report), ("source.json", source)]:
        raw = path.read_bytes()
        if not raw or len(raw) > MAX_DIAGNOSTIC_BYTES:
            raise ValueError("Lifecycle diagnostic is empty or exceeds the byte bound")
        encoded = base64.b64encode(raw).decode("ascii")
        chunks = [encoded[i:i + CHUNK_BYTES] for i in range(0, len(encoded), CHUNK_BYTES)]
        digest = hashlib.sha256(raw).hexdigest()
        for index, chunk in enumerate(chunks):
            print(DIAGNOSTIC_MARKER + json.dumps({
                "file": name, "sha256": digest, "bytes": len(raw),
                "index": index, "chunks": len(chunks), "data": chunk,
            }, separators=(",", ":")), flush=True)


def recover_diagnostics(log, revision):
    """Reject missing, mixed or corrupt frames; never issue qualification proof."""
    files = {}
    for line in log.splitlines():
        if DIAGNOSTIC_MARKER not in line:
            continue
        frame = json.loads(line.split(DIAGNOSTIC_MARKER, 1)[1])
        name = frame["file"]
        if name not in DIAGNOSTIC_FILES:
            raise ValueError("Unexpected lifecycle diagnostic file")
        if (type(frame["bytes"]) is not int or not 0 < frame["bytes"] <= MAX_DIAGNOSTIC_BYTES
                or type(frame["chunks"]) is not int or not 0 < frame["chunks"] <= 1366
                or type(frame["index"]) is not int or not 0 <= frame["index"] < frame["chunks"]
                or not isinstance(frame["data"], str) or len(frame["data"]) > CHUNK_BYTES
                or not re.fullmatch(r"[0-9a-f]{64}", frame["sha256"])):
            raise ValueError("Invalid lifecycle diagnostic frame")
        identity = (frame["sha256"], frame["bytes"], frame["chunks"])
        entry = files.setdefault(name, {"identity": identity, "chunks": {}})
        if entry["identity"] != identity:
            raise ValueError("Mixed lifecycle diagnostic streams")
        prior = entry["chunks"].setdefault(frame["index"], frame["data"])
        if prior != frame["data"]:
            raise ValueError("Conflicting lifecycle diagnostic chunk")
    if set(files) != DIAGNOSTIC_FILES:
        raise ValueError("Missing lifecycle diagnostic files")
    recovered = {}
    for name, entry in files.items():
        digest, size, count = entry["identity"]
        if len(entry["chunks"]) != count:
            raise ValueError("Incomplete lifecycle diagnostic chunks")
        raw = base64.b64decode("".join(entry["chunks"][i] for i in range(count)), validate=True)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Lifecycle diagnostic digest mismatch")
        recovered[name] = raw
    source = json.loads(recovered["source.json"])
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or source.get("revision") != revision:
        raise ValueError("Lifecycle diagnostic checkout mismatch")
    ET.fromstring(recovered["lifecycle.xml"])
    return recovered


def retain_diagnostics():
    location = os.environ.get("SIMIT_NIX_BUILD_RESULTS")
    if not location:
        return  # Setup may fail before the evidence directory is prepared.
    evidence = Path(location)
    log_path = evidence / "build.log"
    if not log_path.exists():
        return
    log = log_path.read_text(encoding="utf-8-sig")
    if DIAGNOSTIC_MARKER not in log:
        return  # A timed-out or unstarted suite is not a completed report.
    revision = (evidence / "revision").read_text(encoding="utf-8-sig").strip()
    recovered = recover_diagnostics(log, revision)
    diagnostic = {
        "schemaVersion": 1, "scope": "hermes-lifecycle-diagnostics",
        "revision": revision, "qualified": False,
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in recovered.items()},
    }
    for name, raw in recovered.items():
        (evidence / name).write_bytes(raw)
    (evidence / "diagnostics.json").write_text(json.dumps(diagnostic, indent=2) + "\n", encoding="utf-8")


def qualify(report, provenance):
    raw = report.read_bytes()
    root = ET.fromstring(raw)
    cases = root.findall(".//testcase")
    names = {case.get("name") for case in cases}
    missing = REQUIRED - names
    unsuccessful = [case.get("name") for case in cases
                    if any(case.find(tag) is not None for tag in [
                        "failure", "error", "skipped", "rerunFailure", "rerunError", "flakyFailure", "flakyError",
                    ])]
    identities = [(case.get("classname"), case.get("name")) for case in cases]
    if len(identities) != len(set(identities)):
        raise ValueError("Lifecycle proof contains duplicate or retried cases")
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
    elif sys.argv[1:] == ["retain-diagnostics"]:
        retain_diagnostics()
    elif sys.argv[1:2] == ["emit-diagnostics"]:
        report, source = map(Path, sys.argv[2:])
        emit_diagnostics(report, source)
    else:
        report, source, destination = map(Path, sys.argv[1:])
    receipt = qualify(report, json.loads(source.read_text(encoding="utf-8-sig")))
    destination.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
