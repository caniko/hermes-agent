"""A failed, skipped, partial or mutable-source report cannot qualify a candidate."""

import hashlib
import importlib.util
import tempfile
import unittest
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
        path = Path(directory) / "report.xml"
        return path, root, suite

    def test_receipt_binds_report_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path, root, _ = self.report(directory)
            ET.ElementTree(root).write(path)
            receipt = qualification.qualify(path, {"revision": "a" * 40})
            self.assertEqual(receipt["reportSha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(receipt["tests"], len(qualification.REQUIRED))
            self.assertFalse(receipt["paperclipDispatchQualified"])

    def test_unsuccessful_or_missing_proof_is_rejected(self):
        for tag in ["failure", "error", "skipped", "missing", "suite-error"]:
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

    def test_mutable_revision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path, root, _ = self.report(directory)
            ET.ElementTree(root).write(path)
            for revision in [None, "main", "g" * 40]:
                with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "immutable source"):
                    qualification.qualify(path, {"revision": revision})


if __name__ == "__main__":
    unittest.main()
