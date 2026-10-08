"""Readiness must match GitHub's capacity and repository-access contracts."""

import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("readiness", Path(__file__).with_name("runner-readiness.py"))
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)


class ReadinessTests(unittest.TestCase):
    def configured(self, runner, *, private=False, visibility="all", allows_public=True, repositories=(1,)):
        responses = {
            "repos/owner/repository": {"id": 1, "private": private, "owner": {"type": "Organization"}},
            "orgs/owner/actions/hosted-runners?per_page=100&page=1": {"runners": [runner]},
            "orgs/owner/actions/runner-groups/2": {
                "id": 2, "visibility": visibility, "allows_public_repositories": allows_public,
            },
            "orgs/owner/actions/runner-groups/2/repositories?per_page=100&page=1": {
                "repositories": [{"id": repo_id} for repo_id in repositories],
            },
        }
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repository", GITHUB_RUN_ID="3"), \
                patch.object(readiness, "api", side_effect=responses.__getitem__):
            return readiness.configured("qualified-linux", 64)

    def runner(self):
        return {"name": "qualified-linux", "status": "Ready", "maximum_runners": 1,
                "runner_group_id": 2, "platform": "linux-x64",
                "machine_size_details": {"memory_gb": 128, "cpu_cores": 32}}

    def test_supported_runner_is_bound_to_provider_receipt(self):
        runner = self.runner()
        receipt = self.configured(runner)
        self.assertEqual(receipt["runner"], runner)
        self.assertFalse(receipt["qualified"])

    def test_memory_alone_cannot_qualify_an_incompatible_runner(self):
        for change in [{"platform": "linux-arm64"}, {"platform": "win-x64"},
                       {"machine_size_details": {"memory_gb": 128, "cpu_cores": 16}}]:
            with self.subTest(change=change):
                runner = self.runner()
                runner.update(change)
                with self.assertRaisesRegex(RuntimeError, "Linux/x64|32"):
                    self.configured(runner)

    def test_repository_visibility_and_public_access_are_both_required(self):
        for private, visibility, allows_public, repositories, allowed in [
            (False, "all", True, (), True),
            (False, "all", False, (), False),
            (False, "private", True, (), False),
            (False, "selected", True, (1,), True),
            (False, "selected", True, (2,), False),
            (True, "private", False, (), True),
            (True, "all", False, (), True),
            (True, "selected", False, (1,), True),
            (True, "selected", False, (2,), False),
        ]:
            with self.subTest(private=private, visibility=visibility, allows_public=allows_public,
                              repositories=repositories):
                arguments = dict(private=private, visibility=visibility, allows_public=allows_public,
                                 repositories=repositories)
                if allowed:
                    self.configured(self.runner(), **arguments)
                else:
                    with self.assertRaisesRegex(RuntimeError, "Runner group"):
                        self.configured(self.runner(), **arguments)


if __name__ == "__main__":
    unittest.main()
