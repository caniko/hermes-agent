"""Successful qualification must reject JUnit reruns, even with one case identity."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location("evidence", Path(__file__).with_name("qualification-evidence.py"))
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class EvidenceTests(unittest.TestCase):
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
            evidence.seal(directory, "success", True)
            receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8-sig"))
            self.assertFalse(receipt["qualified"])
            self.assertEqual(receipt["reports"][0]["retries"], 0)
            self.assertEqual(receipt["members"]["report.xml"], evidence.sha256(directory / "report.xml"))

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
