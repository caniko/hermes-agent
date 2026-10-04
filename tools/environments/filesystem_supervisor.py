"""TargetJobSupervisor client for the shared authority, over local/SSH transport."""

import base64
import json
import subprocess
import threading
import time
import uuid

from tools.environments.job_supervision import JobReceipt, JobState, SupervisionError


class OwnershipPending(SupervisionError):
    pass


class AuthorityRequestRejected(SupervisionError):
    pass


class FilesystemSupervisor:
    def __init__(self, call, descriptor: dict, environment: dict):
        self.call = call
        self.descriptor = descriptor
        self.environment = environment
        self.runtime_dir = None
        self._observations = {}
        self._lock = threading.RLock()

    def _request(self, op, **fields):
        message = {"version": 1, "authority": self.descriptor["authority"],
                   "principal": self.descriptor["principal"],
                   "request": self.descriptor["request"], "fingerprint": self.descriptor["fingerprint"],
                   "op": op, **fields}
        try:
            response = self.call(json.dumps(message) + "\n")
            if response.returncode:
                raise SupervisionError("target authority is unreachable")
            body = json.loads(response.stdout)
            if not body.get("ok"):
                if body.get("code") == "rejected":
                    raise AuthorityRequestRejected("target authority rejected the request")
                raise SupervisionError("target ownership unresolved or sealed")
            return body["result"]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            raise SupervisionError("target authority observation is unavailable") from exc

    def prepare(self):
        result = self._request("reserve", roots=self.descriptor["roots"])
        if result["state"] == "pending":
            raise OwnershipPending("waiting for filesystem ownership")
        if result["state"] != "active":
            raise SupervisionError("target supervision is sealed")
        self.runtime_dir = result["runtime_dir"]

    def start(self, command, *, cwd, environment_names, stdin=None):
        job = JobReceipt(uuid.uuid4().hex)
        environment = {name: self.environment[name] for name in environment_names if name in self.environment}
        uncertain = False
        while True:
            try:
                self._request("start", job=job.id, command=command, cwd=cwd, stdin=stdin,
                              environment=environment)
                return job
            except AuthorityRequestRejected:
                if not uncertain:
                    raise
                # A previous attempt may have run before validation changed.
                # Fence that scope before letting the caller retry any work.
                self.stop()
                raise
            except SupervisionError:
                uncertain = True
            try:
                state = self.status()["state"]
            except SupervisionError:
                state = None
            if state is not None and state != "active":
                raise SupervisionError("target supervision is sealed")
            # Replay only this immutable job identity. Returning an ambiguous
            # error would let a tool retry the command under a fresh identity.
            time.sleep(.5)

    def jobs(self):
        return [JobReceipt(job) for job in self._request("jobs")["jobs"]]

    def status(self):
        return self._request("status")

    def _observe(self, job, offset=0):
        with self._lock:
            key = (job.id, offset)
            cached = self._observations.get(key)
            now = time.monotonic()
            if cached is None or now - cached[0] >= .5:
                observation = self._request("observe", job=job.id, offset=offset)
                # Slow target control can exceed the reuse window itself. Date
                # the coherent exit/state/output snapshot on receipt, so the
                # handle does not immediately repeat the same expensive query.
                cached = time.monotonic(), observation
                # Retain one observation per job, never a second log buffer.
                self._observations = {k: v for k, v in self._observations.items() if k[0] != job.id}
                self._observations[key] = cached
            return cached[1]

    def inspect(self, job):
        try:
            return JobState(self._observe(job)["state"])
        except (SupervisionError, ValueError):
            return JobState.UNKNOWN

    def main_exit_code(self, job):
        return self._observe(job)["exit_code"]

    def exit_code(self, job):
        result = self.main_exit_code(job)
        if result is None:
            raise SupervisionError("target job is still running")
        return result

    def read_output(self, job, offset):
        return base64.b64decode(self._observe(job, offset)["output"], validate=True)

    def output(self, job):
        result = bytearray()
        while data := self.read_output(job, len(result)):
            result.extend(data)
        return result.decode("utf-8", errors="replace")

    def seal(self):
        self._request("seal")

    def stop(self):
        self._request("stop")

    def release(self):
        return self._request("release")

    def stop_job(self, job):
        if self._request("stop_job", job=job.id)["state"] != "settled":
            raise SupervisionError("target job has not settled")
        with self._lock:
            self._observations.clear()

    def settled(self):
        try:
            return self._request("drain")["drained"] is True
        except SupervisionError:
            return False
