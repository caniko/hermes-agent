"""Successful qualification must reject JUnit reruns, even with one case identity."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location("evidence", Path(__file__).with_name("qualification-evidence.py"))
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class EvidenceTests(unittest.TestCase):
    def test_retention_requires_each_named_proof_not_an_equal_sized_substitute(self):
        head = "a" * 40
        suffixes = ("admission-green", "admission-baseline-red", "readiness", "native-ordinary", "native-high-memory")
        names = [f"worker-proof-{suffix}-{head}" for suffix in suffixes]
        def artifact(index, name):
            return {"id": index, "name": name, "expired": False,
                    "created_at": "2026-10-09T00:00:00Z", "expires_at": "2026-11-10T00:00:00Z",
                    "digest": "sha256:" + "b" * 64, "workflow_run": {"head_sha": head}}
        with tempfile.TemporaryDirectory() as location:
            for replacements in (names, names[:-1] + [f"worker-proof-diagnostics-{head}"]):
                with self.subTest(names=replacements), patch.object(evidence, "identity", return_value={"head": head, "run_id": 1}), \
                        patch.object(evidence, "api", return_value={"artifacts": [artifact(i, name) for i, name in enumerate(replacements)]}):
                    destination = Path(location) / "retention.json"
                    if replacements == names:
                        evidence.artifacts("worker-proof-", 5, destination)
                        self.assertEqual({a["name"] for a in json.loads(destination.read_text(encoding="utf-8-sig"))["artifacts"]}, set(names))
                    else:
                        with self.assertRaisesRegex(RuntimeError, "identities"):
                            evidence.artifacts("worker-proof-", 5, destination)

    def report(self, directory, tag=None):
        (directory / "source.json").write_text(json.dumps({"head": "a" * 40}), encoding="utf-8")
        suite = ET.Element("testsuite")
        case = ET.SubElement(suite, "testcase", classname="contracts", name="completed")
        if tag:
            ET.SubElement(case, tag)
        ET.ElementTree(suite).write(directory / "report.xml")

    def test_passing_report_has_bound_members_but_no_acceptance(self):
        with tempfile.TemporaryDirectory() as location:
            directory = Path(location)
            self.report(directory)
            nested = directory / "native" / "receipt.json"
            nested.parent.mkdir()
            nested.write_text('{"passed": true}', encoding="utf-8")
            evidence.seal(directory, "success", True)
            receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8-sig"))
            self.assertFalse(receipt["qualified"])
            self.assertEqual(receipt["reports"][0]["retries"], 0)
            self.assertEqual(receipt["members"]["report.xml"], evidence.sha256(directory / "report.xml"))
            self.assertEqual(receipt["members"]["native/receipt.json"], evidence.sha256(nested))
            self.assertNotIn("receipt.json", receipt["members"])

    def test_initialize_requires_the_requested_32_day_retention_limit(self):
        for days in (31, 32):
            with self.subTest(days=days), tempfile.TemporaryDirectory() as location:
                directory = Path(location) / "evidence"
                with patch.dict(os.environ, {"RUNNER_ENVIRONMENT": "github-hosted",
                                             "GITHUB_RETENTION_DAYS": str(days)}), patch.object(
                    evidence, "identity", return_value={"head": "a" * 40}
                ), patch.object(evidence.subprocess, "check_output", return_value="a" * 40):
                    if days == 31:
                        with self.assertRaisesRegex(RuntimeError, "32 days"):
                            evidence.initialize(directory)
                        self.assertFalse(directory.exists())
                    else:
                        evidence.initialize(directory)
                        self.assertEqual(json.loads((directory / "source.json").read_text(encoding="utf-8-sig"))["head"], "a" * 40)

    def test_identity_rejects_later_runs_of_the_same_pr_head(self):
        with tempfile.TemporaryDirectory() as location:
            event = Path(location) / "event.json"
            event.write_text(json.dumps({"pull_request": {
                "number": 4, "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}}}), encoding="utf-8")
            environment = {"GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "pull_request",
                           "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "200",
                           "GITHUB_REPOSITORY": "owner/repo", "GITHUB_WORKFLOW_REF": "workflow@ref",
                           "GITHUB_WORKFLOW_SHA": "c" * 40}
            current = {"id": 200, "workflow_id": 3, "run_number": 200, "run_attempt": 1,
                       "head_sha": "a" * 40, "event": "pull_request", "pull_requests": [{"number": 4}]}
            unrelated = {**current, "id": 100, "run_number": 100, "pull_requests": [{"number": 9}]}
            for earlier_prs in ([{"number": 4}], [{"number": 9}], []):
                with self.subTest(earlier_prs=earlier_prs):
                    earlier = {**current, "id": 99, "run_number": 99, "pull_requests": earlier_prs}
                    def api(path):
                        if path == "pulls/4":
                            return {"head": {"sha": "a" * 40}}
                        if path == "actions/runs/200":
                            return current
                        # An earlier same-head run can be beyond the first page.
                        if path.endswith("page=1"):
                            return {"total_count": 101, "workflow_runs": [current] + [
                                {**unrelated, "id": 100 + index, "run_number": 100 + index}
                                for index in range(99)]}
                        if path.endswith("page=2"):
                            return {"total_count": 101, "workflow_runs": [earlier]}
                        self.fail(f"Unexpected API path: {path}")
                    with patch.dict(os.environ, environment), patch.object(evidence, "api", side_effect=api):
                        if earlier_prs == [{"number": 9}]:
                            self.assertEqual(evidence.identity()["run_id"], 200)
                        else:
                            with self.assertRaisesRegex(RuntimeError, "[Ee]arlier"):
                                evidence.identity()

    def test_identity_rejects_retry_attempts_before_querying_run_history(self):
        with tempfile.TemporaryDirectory() as location:
            event = Path(location) / "event.json"
            event.write_text(json.dumps({"pull_request": {"number": 4, "head": {"sha": "a" * 40}}}), encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event),
                                         "GITHUB_EVENT_NAME": "pull_request", "GITHUB_RUN_ATTEMPT": "2"}), patch.object(
                evidence, "api", return_value={"head": {"sha": "a" * 40}}
            ):
                with self.assertRaisesRegex(RuntimeError, "Retries"):
                    evidence.identity()

    def test_retry_records_reject_success_and_survive_in_diagnostics(self):
        for tag in ["rerunFailure", "rerunError", "flakyFailure", "flakyError"]:
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as location:
                directory = Path(location)
                self.report(directory, tag)
                with self.assertRaisesRegex(RuntimeError, "retried"):
                    evidence.seal(directory, "success", True)
                receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8-sig"))
                self.assertFalse(receipt["qualified"])
                self.assertEqual(receipt["reports"][0]["retries"], 1)

    def test_artifact_readback_requires_the_full_31_day_lifetime(self):
        source = {"head": "a" * 40, "run_id": 123}
        created = evidence.dt.datetime(2026, 10, 1, tzinfo=evidence.dt.timezone.utc)
        for seconds in (30 * 86400, 31 * 86400 - 1, 31 * 86400, 32 * 86400):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as location:
                artifact = {"id": 1, "name": "worker-proof-native", "expired": False,
                            "created_at": created.isoformat(),
                            "expires_at": (created + evidence.dt.timedelta(seconds=seconds)).isoformat(),
                            "digest": "sha256:" + "b" * 64,
                            "workflow_run": {"head_sha": source["head"]}}
                destination = Path(location) / "retention.json"
                with patch.object(evidence, "identity", return_value=source), patch.object(
                    evidence, "api", return_value={"artifacts": [artifact]}
                ):
                    if seconds < 31 * 86400:
                        with self.assertRaisesRegex(RuntimeError, "lifetime"):
                            evidence.artifacts("worker-proof-", 1, destination)
                        self.assertFalse(destination.exists())
                    else:
                        evidence.artifacts("worker-proof-", 1, destination)
                        receipt = json.loads(destination.read_text(encoding="utf-8-sig"))
                        self.assertEqual(receipt["artifacts"][0]["retention_seconds"], seconds)
                        self.assertFalse(receipt["qualified"])


if __name__ == "__main__":
    unittest.main()
