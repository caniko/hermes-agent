"""Readiness must preserve the Linux/x64, 32-worker runner contract."""

import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("readiness", Path(__file__).with_name("runner-readiness.py"))
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)


class ReadinessTests(unittest.TestCase):
    def configured(self, runner):
        responses = {
            "repos/owner/repository": {"id": 1, "private": False, "owner": {"type": "Organization"}},
            "orgs/owner/actions/hosted-runners?per_page=100&page=1": {"runners": [runner]},
            "orgs/owner/actions/runner-groups/2": {
                "id": 2, "visibility": "all", "allows_public_repositories": True,
            },
        }
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repository", GITHUB_RUN_ID="3"), \
                patch.object(readiness, "api", side_effect=responses.__getitem__):
            return readiness.configured("qualified-linux", 64)

    def runner(self):
        return {"name": "qualified-linux", "status": "Ready", "maximum_runners": 1,
                "runner_group_id": 2, "platform": "linux-x64",
                "machine_size": {"memory_gb": 128, "cpu_cores": 32}}

    def test_supported_runner_is_bound_to_provider_receipt(self):
        runner = self.runner()
        receipt = self.configured(runner)
        self.assertEqual(receipt["runner"], runner)
        self.assertFalse(receipt["qualified"])

    def test_memory_alone_cannot_qualify_an_incompatible_runner(self):
        for change in [{"platform": "linux-arm64"}, {"platform": "win-x64"},
                       {"machine_size": {"memory_gb": 128, "cpu_cores": 16}}]:
            with self.subTest(change=change):
                runner = self.runner()
                runner.update(change)
                with self.assertRaisesRegex(RuntimeError, "Linux/x64|32"):
                    self.configured(runner)


if __name__ == "__main__":
    unittest.main()
