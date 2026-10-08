"""Successful qualification must reject JUnit reruns, even with one case identity."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
