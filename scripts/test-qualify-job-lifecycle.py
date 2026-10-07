"""A failed, skipped, partial or mutable-source report cannot qualify a candidate."""

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from pathlib import Path

spec = importlib.util.spec_from_file_location("qualification", Path(__file__).with_name("qualify-job-lifecycle.py"))
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


class QualificationTests(unittest.TestCase):
    def report(self, directory):
        root = ET.Element("testsuites")
        suite = ET.SubElement(root, "testsuite")
        for name in sorted(qualification.REQUIRED):
            ET.SubElement(suite, "testcase", name=name)
        for index in range(176 - len(qualification.REQUIRED)):
            ET.SubElement(suite, "testcase", name=f"accepted-roster-fixture-{index}")
        path = Path(directory) / "report.xml"
        return path, root, suite

    def test_receipt_binds_report_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path, root, _ = self.report(directory)
            ET.ElementTree(root).write(path)
            receipt = qualification.qualify(path, {"revision": "a" * 40})
            self.assertEqual(receipt["reportSha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(receipt["tests"], 176)
            self.assertFalse(receipt["paperclipDispatchQualified"])

    def test_unsuccessful_or_missing_proof_is_rejected(self):
        for tag in ["failure", "error", "skipped", "missing", "suite-error",
                    "rerunFailure", "rerunError", "flakyFailure", "flakyError"]:
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as directory:
                path, root, suite = self.report(directory)
                if tag == "missing":
                    suite.remove(suite[0])
                elif tag == "suite-error":
                    ET.SubElement(suite, "error")
                else:
                    ET.SubElement(suite[0], tag)
                ET.ElementTree(root).write(path)
                with self.assertRaisesRegex(ValueError, "Incomplete lifecycle proof"):
                    qualification.qualify(path, {"revision": "a" * 40})

    def test_duplicate_cases_cannot_hide_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            path, root, suite = self.report(directory)
            ET.SubElement(suite, "testcase", name=suite[0].get("name"))
            ET.ElementTree(root).write(path)
            with self.assertRaisesRegex(ValueError, "duplicate or retried"):
                qualification.qualify(path, {"revision": "a" * 40})

    def diagnostic_fixture(self, directory):
        report, root, suite = self.report(directory)
        ET.SubElement(suite[0], "failure").text = "failed assertion " * 500
        ET.ElementTree(root).write(report)
        source = Path(directory) / "source.json"
        source.write_text(json.dumps({"revision": "a" * 40}), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            qualification.emit_diagnostics(report, source)
        return report, source, output.getvalue()

    def test_failed_report_survives_log_transport_without_a_passing_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            report, source, log = self.diagnostic_fixture(directory)
            # Driver prefixes, reordered chunks, and identical console echoes
            # preserve the original bytes. They cannot mask the failure.
            lines = log.splitlines()
            transported = "\n".join("worker: " + line for line in reversed(lines + lines))
            recovered = qualification.recover_diagnostics(transported, "a" * 40)
            self.assertEqual(recovered["lifecycle.xml"], report.read_bytes())
            self.assertEqual(recovered["source.json"], source.read_bytes())
            (Path(directory) / "revision").write_text("a" * 40, encoding="utf-8")
            (Path(directory) / "build.log").write_text(transported, encoding="utf-8")
            with patch.dict(os.environ, SIMIT_NIX_BUILD_RESULTS=directory):
                qualification.retain_diagnostics()
            diagnostics = json.loads((Path(directory) / "diagnostics.json").read_text(encoding="utf-8-sig"))
            self.assertFalse(diagnostics["qualified"])
            self.assertFalse((Path(directory) / "receipt.json").exists())
            with self.assertRaisesRegex(ValueError, "Incomplete lifecycle proof"):
                qualification.qualify(Path(directory) / "lifecycle.xml", {"revision": "a" * 40})

    def test_partial_corrupt_mixed_and_wrong_revision_diagnostics_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, log = self.diagnostic_fixture(directory)
            lines = log.splitlines()
            frame = json.loads(lines[0].split(qualification.DIAGNOSTIC_MARKER, 1)[1])
            frame["data"] = "AAAA" + frame["data"][4:]
            corrupt = qualification.DIAGNOSTIC_MARKER + json.dumps(frame)
            for modified in ["\n".join(lines[1:]), "\n".join([corrupt] + lines[1:]),
                             log + corrupt, ""]:
                with self.subTest(log=modified[:60]), self.assertRaises(ValueError):
                    qualification.recover_diagnostics(modified, "a" * 40)
            with self.assertRaisesRegex(ValueError, "checkout mismatch"):
                qualification.recover_diagnostics(log, "b" * 40)

    def test_unstarted_suite_does_not_create_completed_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, SIMIT_NIX_BUILD_RESULTS=directory):
                qualification.retain_diagnostics()
                (Path(directory) / "build.log").write_text("VM never completed\n", encoding="utf-8")
                qualification.retain_diagnostics()
            self.assertFalse((Path(directory) / "diagnostics.json").exists())

    def test_mutable_revision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path, root, _ = self.report(directory)
            ET.ElementTree(root).write(path)
            for revision in [None, "main", "g" * 40]:
                with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "immutable source"):
                    qualification.qualify(path, {"revision": revision})


if __name__ == "__main__":
    unittest.main()
