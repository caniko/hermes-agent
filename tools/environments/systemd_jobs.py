"""Reconnectable Linux job ownership using the execution user's systemd manager.

The supplied transport executes trusted Bash on the target and carries stdin
separately. All paths and cgroup observations belong to that target. No SSH
credentials, controller PIDs, or controller filesystem probes enter receipts.
"""

import base64
import hashlib
import posixpath
import re
import shlex
import subprocess
import uuid
from typing import Callable

from tools.environments.job_supervision import JobReceipt, JobState, SupervisionError


def quote_systemd_path(value: str) -> str:
    # systemd-run sends parsed values over D-Bus; literal '%' needs no specifier
    # escaping. Quote each path, not the ':' separators in structured properties.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class SystemdJobSupervisor:
    def __init__(self, execute: Callable[[str, str | None], subprocess.CompletedProcess], state_dir: str,
                 *, execution_user: int | None = None, properties: tuple[str, ...] = (),
                 root_identities: tuple[tuple[str, int, int], ...] = ()):
        if not posixpath.isabs(state_dir) or "\0" in state_dir:
            raise ValueError("supervision state directory must be absolute")
        self.execute = execute
        self.state_dir = state_dir
        self._namespace = hashlib.sha256(state_dir.encode()).hexdigest()[:24]
        self.execution_user = execution_user
        self.properties = properties
        self.root_identities = root_identities
        self._manager = "--user" if execution_user is None else "--system"

    def _run(self, script: str, stdin: str | None = None) -> str:
        # SSH without PAM may not set the bus variables. Resolve them on the
        # execution host, always for the SSH login identity, never the controller.
        prelude = ('set -eu; export XDG_RUNTIME_DIR="/run/user/$(id -u)"; '
                   'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"; ')
        try:
            # None makes subprocess transports inherit the gateway/backdoor
            # input. Control commands and staged job input must receive EOF.
            result = self.execute(prelude + script, "" if stdin is None else stdin)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SupervisionError("target supervisor is unreachable") from exc
        if result.returncode:
            # Do not include command text or inherited environment in errors.
            message = "target supervision is sealed" if result.returncode == 78 else "target supervisor command failed"
            raise SupervisionError(message)
        return result.stdout

    def _gate(self, script: str) -> str:
        root = shlex.quote(self.state_dir)
        return f"( flock -x 9; {script} ) 9>{root}/fence/gate"

    def _job(self, job: JobReceipt) -> tuple[str, str]:
        if not re.fullmatch(r"[a-f0-9]{32}", job.id):
            raise ValueError("invalid job receipt")
        return f"{self.state_dir}/job-{job.id}", f"hermes-job-{self._namespace}-{job.id}.service"

    def prepare(self) -> None:
        root = shlex.quote(self.state_dir)
        self._run(
            'test -f /sys/fs/cgroup/cgroup.controllers; '
            f'systemd-run {self._manager} --quiet --wait --collect --service-type=exec --expand-environment=no '
            '--property=ExitType=cgroup --property=KillMode=mixed -- "$(command -v true)"; '
            f"umask 077; mkdir -p -- {root}; test ! -L {root}; "
             f'test "$(stat -c %u {root})" = "$(id -u)"; '
             f'test "$(stat -c %a {root})" = 700; '
             f"mkdir -p -- {root}/fence; chmod 755 {root}/fence; "
            + self._gate(f"if test ! -f {root}/fence/boot; then cat /proc/sys/kernel/random/boot_id > {root}/fence/boot; fi; "
                         f"chmod 644 {root}/fence/boot {root}/fence/gate"))

    def start(self, command: str, *, cwd: str, environment_names: tuple[str, ...],
              stdin: str | None = None, environment: dict[str, str] | None = None,
              job_id: str | None = None) -> JobReceipt:
        if not posixpath.isabs(cwd) or "\0" in cwd:
            raise ValueError("job cwd must be absolute")
        if any(not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) for name in environment_names):
            raise ValueError("invalid environment name")
        job = JobReceipt(job_id or uuid.uuid4().hex)
        folder, unit = self._job(job)
        root, dest = shlex.quote(self.state_dir), shlex.quote(folder)
        # Persist the intent BEFORE systemd submission, under the same target-side
        # lock as seal/stop. A lost SSH acknowledgement leaves a discoverable job.
        # RemainAfterExit retains successful receipts until explicit stop; ExitType
        # keeps the job running when only daemonized descendants remain (systemd 250+).
        properties = ["ExitType=cgroup", "RemainAfterExit=yes", "KillMode=mixed", "TimeoutStopSec=5s",
                      "StandardOutput=append:" + folder + "/output",
                      "StandardError=inherit", "StandardInput=file:" + folder + "/input"]
        properties.extend(self.properties)
        fence = self.state_dir + "/fence"
        if self.execution_user is not None:
            if ":" in fence or "\n" in fence:
                raise ValueError("systemd fence path cannot contain colon or newline")
            properties += [f"BindReadOnlyPaths={quote_systemd_path(fence)}:/run/hermes-job-fence"]
            fence = "/run/hermes-job-fence"
            properties += [f"User={self.execution_user}",
                           f"LoadCredential=command:{folder}/command",
                           f"LoadCredential=environment:{folder}/environment"]
        argv = ["systemd-run", self._manager, "--quiet", "--service-type=exec", "--expand-environment=no", f"--unit={unit}"]
        argv += [f"--property={value}" for value in properties]
        source = dest if self.execution_user is None else '"$2"'
        fence = shlex.quote(fence)
        # A disconnected D-Bus submission can arrive after the caller released
        # its gate and seal observed no unit. Check the live, read-only fence in
        # ExecStart before loading any workload-controlled environment or code.
        # Once this shared gate opens, the unit/cgroup is already observable.
        # A mount parser may chase a proc-FD source to a pathname. Verify the
        # mounted inode inside the final namespace before loading any workload
        # environment, so replacement during namespace setup cannot redirect it.
        root_checks = "".join(
            f'test "$("$5" -Lc "%d:%i" -- {shlex.quote(path)})" = {shlex.quote(f"{device}:{inode}")} || exit 78; '
            for path, device, inode in self.root_identities)
        launch = (f'( "$3" -s 9; test ! -e {fence}/sealed && test ! -e {fence}/{job.id}.stopped || exit 78; '
                  f'"$4" -s {fence}/boot /proc/sys/kernel/random/boot_id ) 9<{fence}/gate; '
                  + root_checks
                  +
                  f"source {source}/environment; cd -- {shlex.quote(cwd)}; "
                  f'exec "$1" --noprofile --norc {source}/command')
        environment_script = 'declare -px ' + shlex.join(environment_names) if environment_names else ':'
        if environment is not None:
            if any(not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) or not isinstance(value, str) or "\0" in value
                   for name, value in environment.items()):
                raise ValueError("invalid job environment")
            # Never put workload env into the privileged supervisor's environment.
            exports = "\n".join(f"export {name}={shlex.quote(value)}" for name, value in environment.items())
            environment_script = "printf '%s' " + shlex.quote(exports)
        script = (
            f"test ! -e {root}/fence/sealed || exit 78; cmp -s {root}/fence/boot /proc/sys/kernel/random/boot_id; "
            f"umask 077; mkdir -- {dest}; cat > {dest}/input; "
            f"printf '%s' {shlex.quote(command)} > {dest}/command; "
            f"{environment_script} > {dest}/environment; "
            f"{shlex.join(argv)} -- \"$(command -v bash)\" --noprofile --norc -ec "
            + shlex.quote('exec "$1" -i "$2" --noprofile --norc -ec "$3" job "$2" "${CREDENTIALS_DIRECTORY:-}" "$4" "$5" "$6"')
            + f" launcher \"$(command -v env)\" \"$(command -v bash)\" {shlex.quote(launch)} \"$(command -v flock)\" \"$(command -v cmp)\" \"$(command -v stat)\"; "
            f"touch {dest}/accepted")
        self._run(self._gate(script), stdin)
        return job

    def jobs(self) -> list[JobReceipt]:
        root = shlex.quote(self.state_dir)
        output = self._run(f"test -f {root}/fence/boot; for job in {root}/job-*; do "
                           'test -d "$job" || continue; printf "%s\\n" "${job##*/job-}"; done')
        jobs = [JobReceipt(value) for value in output.splitlines()]
        for job in jobs:
            self._job(job)
        return sorted(jobs, key=lambda job: job.id)

    def inspect(self, job: JobReceipt) -> JobState:
        folder, unit = self._job(job)
        root, dest = shlex.quote(self.state_dir), shlex.quote(folder)
        # A failed control channel is not an empty cgroup. A missing unit is only
        # conclusive AFTER the launch fence, or after an execution-host reboot.
        script = (
            f"test -f {root}/fence/boot; test -d {dest} || test -f {root}/fence/sealed; "
            f"if ! cmp -s {root}/fence/boot /proc/sys/kernel/random/boot_id; then echo settled; exit; fi; "
            f"state=$(systemctl {self._manager} show {unit} -p LoadState -p ActiveState -p SubState -p ControlGroup) || "
            '{ case "$state" in *LoadState=not-found*) ;; *) exit 1;; esac; }; '
            'load= active= sub= group=; while IFS="=" read -r key value; do '
            'case "$key" in LoadState) load=$value;; ActiveState) active=$value;; '
            'SubState) sub=$value;; ControlGroup) group=$value;; esac; done <<< "$state"; '
            f'if test "$load" = not-found; then '
            f'if test -f {root}/fence/sealed || test -f {root}/fence/{job.id}.stopped; then echo settled; else echo unknown; fi; '
            'elif test -n "$group" && test -e "/sys/fs/cgroup$group/cgroup.events"; then '
            'if grep -qx "populated 0" "/sys/fs/cgroup$group/cgroup.events"; then echo settled; else echo running; fi; '
            'elif test "$active" = inactive || test "$active" = failed || test "$sub" = exited; then echo settled; '
            'else echo unknown; fi')
        try:
            return JobState(self._run(script).strip())
        except (SupervisionError, ValueError):
            return JobState.UNKNOWN

    def output(self, job: JobReceipt) -> str:
        folder, _ = self._job(job)
        return self._run(f"cat -- {shlex.quote(folder + '/output')}")

    def read_output(self, job: JobReceipt, offset: int) -> bytes:
        if offset < 0:
            raise ValueError("output offset must not be negative")
        folder, _ = self._job(job)
        encoded = self._run(f"set -o pipefail; dd if={shlex.quote(folder + '/output')} "
                            f"iflag=skip_bytes,count_bytes skip={int(offset)} count=65536 status=none | base64")
        return base64.b64decode(encoded)

    def main_exit_code(self, job: JobReceipt) -> int | None:
        _, unit = self._job(job)
        try:
            fields = dict(line.split("=", 1) for line in self._run(
                f"systemctl {self._manager} show {unit} -p ExecMainCode -p ExecMainStatus").splitlines())
        except SupervisionError:
            self._run(f"test -f {shlex.quote(self.state_dir + '/fence/' + job.id + '.stopped')}")
            if self.inspect(job) is JobState.SETTLED:
                return -15
            raise
        if not {"ExecMainCode", "ExecMainStatus"} <= fields.keys():
            raise SupervisionError("target job exit status is unavailable")
        code, status = int(fields["ExecMainCode"]), int(fields["ExecMainStatus"])
        return None if code == 0 else status if code == 1 else -status

    def exit_code(self, job: JobReceipt) -> int:
        _, unit = self._job(job)
        return int(self._run(f"systemctl {self._manager} show {unit} -p ExecMainStatus --value").strip())

    def seal(self) -> None:
        root = shlex.quote(self.state_dir)
        self._run(self._gate(f"test -f {root}/fence/boot; touch {root}/fence/sealed"))

    def stop(self) -> None:
        self.seal()
        self._stop_jobs(self.jobs())

    def stop_job(self, job: JobReceipt) -> None:
        self._stop_jobs([job])

    def _stop_jobs(self, jobs: list[JobReceipt]) -> None:
        if not jobs:
            return
        folders, units = zip(*(self._job(job) for job in jobs))
        # KillMode=mixed gives the original parent TERM first, then systemd
        # kills its remaining cgroup after the grace period. Fence every receipt
        # before waiting on the manager, which stops the units together instead
        # of paying each job's grace and control round trips serially.
        tombstones = [self.state_dir + '/fence/' + job.id + '.stopped' for job in jobs]
        try:
            self._run(self._gate(f"test -f {shlex.quote(self.state_dir + '/fence/boot')}; "
                                   f"mkdir -p -- {shlex.join(folders)}; touch -- {shlex.join(tombstones)}; "
                                   f"systemctl {self._manager} stop {shlex.join(units)}"))
        except SupervisionError:
            if not all(self.inspect(job) is JobState.SETTLED for job in jobs):
                raise
        if not all(self.inspect(job) is JobState.SETTLED for job in jobs):
            raise SupervisionError("target job has not settled")
        # Failed transient units are retained by the manager. Retire only this
        # verified-empty set; collected units may already have disappeared.
        try:
            self._run(f"systemctl {self._manager} reset-failed {shlex.join(units)}")
        except SupervisionError:
            if not all(self.inspect(job) is JobState.SETTLED for job in jobs):
                raise

    def settled(self) -> bool:
        try:
            self._run(f"test -f {shlex.quote(self.state_dir + '/fence/sealed')}")
            return all(self.inspect(job) is JobState.SETTLED for job in self.jobs())
        except SupervisionError:
            return False
