"""The existing shell/file environment, with every command admitted by a grant.

This deliberately isn't a LocalEnvironment: file operations and kernel staging
must use the target execution channel even when the authority is on this host.
"""

from tools.environments.base import BaseEnvironment
from tools.environments.job_supervision import SupervisionError
from tools.environments.supervised_execution import SupervisedProcessHandle


class OwnedEnvironment(BaseEnvironment):
    _stdin_mode = "payload"

    def __init__(self, binding, cwd: str, timeout: int):
        self.binding = binding
        self._admission_cwd = cwd
        if binding.supervisor is None or not binding.supervisor.runtime_dir:
            raise SupervisionError("filesystem ownership must be acquired before preparing tools")
        super().__init__(cwd, timeout)
        # Workload HOME and PATH come from target enrollment, never a login shell
        # that can overwrite them with the maintained user's runtime state.
        self._prefer_nonlogin = True

    def get_temp_dir(self):
        return self.binding.supervisor.runtime_dir + "/tmp"

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None,
                  wait_for_descendants=False):
        supervisor = self.binding.supervisor
        # BaseEnvironment's wrapper owns logical cwd, including absolute-path
        # kernel staging from '/'. Admission stays anchored to the acquired root;
        # the namespace confines every wrapper and its descendants to the grant.
        job = supervisor.start(cmd_string, cwd=self._admission_cwd, environment_names=(), stdin=stdin_data)
        return SupervisedProcessHandle(supervisor, job, wait_for_descendants=wait_for_descendants)

    def cleanup(self):
        # Run settlement owns target cleanup; per-tool teardown cannot release it.
        return None
