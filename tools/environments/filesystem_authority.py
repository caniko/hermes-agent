"""Host-local authority shared by enrolled controllers, independent of their DBs.

Requests arrive over a protected Unix socket; peer credentials select a fixed
execution identity and allowed roots. Workloads never receive control credentials.
The CLI/transport lives in filesystem_authority_server, outside model tool schemas.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import threading
from pathlib import Path

from tools.environments.filesystem_claims import ClaimStore, directory_identity, roots_overlap
from tools.environments.job_supervision import JobReceipt, JobState, SupervisionError
from tools.environments.systemd_jobs import SystemdJobSupervisor, quote_systemd_path


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class FilesystemAuthority:
    def __init__(self, state: Path, principals: dict[int, dict], *, supervisor_factory=None):
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            raise RuntimeError("filesystem authority requires POSIX identity and locking")
        import fcntl

        if len({policy["id"] for policy in principals.values()}) != len(principals):
            raise ValueError("each control identity requires a distinct principal")
        self.state = state.resolve(strict=True)
        stat = self.state.stat()
        if stat.st_uid != getuid() or stat.st_mode & 0o077:
            raise ValueError("authority state must be private and owned by the service user")
        self._anchor = open(self.state / "authority.lock", "a", encoding="utf-8")
        try:
            fcntl.flock(self._anchor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._anchor.close()
            raise
        self.store = ClaimStore(self.state)
        self._lock = threading.RLock()
        self.principals = principals
        self._factory = supervisor_factory or self._system_supervisor
        self._supervisors = {}
        self._prepared = set()
        self._admissions = set()
        self._root_fds = {}
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS execution (
                claim TEXT PRIMARY KEY, uid INTEGER NOT NULL, boot TEXT NOT NULL,
                environment TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, claim TEXT NOT NULL, fingerprint TEXT NOT NULL,
                exit_code INTEGER, settled INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(claim);
        """)
        with self.store.transaction():
            if "settled" not in {column[1] for column in self.store.db.execute("PRAGMA table_info(jobs)")}:
                self.store.db.execute("ALTER TABLE jobs ADD COLUMN settled INTEGER NOT NULL DEFAULT 0")
            self.store.db.execute("CREATE INDEX IF NOT EXISTS jobs_unsettled ON jobs(claim, settled)")
        # An old gateway may still be alive. Fence its grants first, then drain
        # recorded units. Pending, never-granted requests remain in FIFO order.
        try:
            for row in self.store.live():
                if row["state"] != "pending":
                    self._seal(row)
        except BaseException:
            self.close()
            raise

    def close(self):
        for fds in self._root_fds.values():
            for fd in fds:
                os.close(fd)
        self.store.close()
        self._anchor.close()

    def _system_supervisor(self, row, uid):
        import pwd

        from tools.environments.local import _find_bash

        bash = _find_bash()
        def execute(script, stdin=None):
            return subprocess.run([bash, "--noprofile", "--norc", "-c", script],
                                  input=stdin, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30)
        # A read-only host filesystem still exposes same-UID credentials and
        # homes. Start from an authority-owned empty root, adding only platform
        # tools read-only, the requested directory binds, and this claim's runtime.
        props = [f"RootDirectory={self.state / row['id'] / 'rootfs'}", "MountAPIVFS=yes",
                 "BindReadOnlyPaths=-/usr -/bin -/sbin -/lib -/lib64 -/etc -/nix/store -/nix/var/nix/profiles -/run/current-system",
                 "ProtectSystem=strict", "ProtectHome=tmpfs", "PrivateTmp=yes", "PrivateDevices=yes",
                 "NoNewPrivileges=yes", "CapabilityBoundingSet=", "RestrictSUIDSGID=yes",
                 "ProtectControlGroups=yes", "ProtectKernelTunables=yes", "RestrictNamespaces=yes", "KeyringMode=private",
                  "ProtectProc=invisible", "PrivatePIDs=yes", "UMask=0077", "TasksMax=256", "MemoryMax=4G",
                 "IPAddressDeny=any", f"Group={pwd.getpwuid(uid).pw_gid}",
                 "InaccessiblePaths=-/run/user -/run/dbus/system_bus_socket -/run/systemd/private",
                 "RestrictAddressFamilies=AF_INET AF_INET6"]
        root_identities = []
        # Descriptor sources plus an in-namespace inode check prevent a changed
        # host root from redirecting a launch. The empty root also removes host
        # ancestor symlinks from the mount destinations.
        for root, fd in zip(row["roots"], self._root_fds.get(row["id"], [])):
            source = f"/proc/{os.getpid()}/fd/{fd}"
            # Keep the requested alias usable by shell/file wrappers without
            # exposing its parent or expanding the grant to its ownership domain.
            for destination in dict.fromkeys((root["canonical"], os.path.normpath(root["path"]))):
                if ":" in destination or "\n" in destination:
                    raise ValueError("systemd ownership roots cannot contain colon or newline")
                props.extend([f"BindPaths={quote_systemd_path(source)}:{quote_systemd_path(destination)}",
                              f"ReadWritePaths={quote_systemd_path(destination)}"])
                root_identities.append((destination, *root["ancestors"][0]))
        runtime = str(self._runtime(row))
        if ":" in runtime or "\n" in runtime:
            raise ValueError("systemd runtime path cannot contain colon or newline")
        props += [f"BindPaths={quote_systemd_path(runtime)}:{quote_systemd_path(runtime)}",
                  f"ReadWritePaths={quote_systemd_path(runtime)}"]
        return SystemdJobSupervisor(execute, str(self.state / row["id"]),
                                    execution_user=uid, properties=tuple(props),
                                    root_identities=tuple(root_identities))

    def _supervisor(self, row, *, prepare=True):
        if row["id"] not in self._supervisors:
            execution = self.store.db.execute("SELECT uid FROM execution WHERE claim=?", (row["id"],)).fetchone()
            if execution is None:
                raise SupervisionError("claim execution identity is unavailable")
            self._supervisors[row["id"]] = self._factory(row, execution["uid"])
        supervisor = self._supervisors[row["id"]]
        if prepare and row["id"] not in self._prepared:
            supervisor.prepare()
            self._prepared.add(row["id"])
        return supervisor

    def _runtime(self, row):
        return self.state.parent / "jobs" / row["id"]

    def _jobs(self, row, *, unsettled=False):
        query = "SELECT id FROM jobs WHERE claim=?" + (" AND settled=0" if unsettled else "")
        return [JobReceipt(item[0]) for item in self.store.db.execute(query, (row["id"],))]

    def _retire_job(self, row, job, supervisor, *, observed_exit_code=None):
        recorded = self.store.db.execute("SELECT exit_code,settled FROM jobs WHERE id=? AND claim=?",
                                        (job.id, row["id"])).fetchone()
        if recorded["settled"]:
            return recorded["exit_code"]
        code = recorded["exit_code"] if recorded["exit_code"] is not None else observed_exit_code
        if code is None:
            try:
                code = supervisor.main_exit_code(job)
            except SupervisionError:
                # A reboot, incomplete submission, or lost exit observation must
                # not prevent fencing and stopping the immutable job intent. The
                # stop proof below, never a missing exit code, settles ownership.
                code = None
        # Keep the observed exit code if the stop acknowledgement is lost. Only
        # the verified stop/fence permits a durable settlement receipt.
        with self.store.transaction():
            self.store.db.execute("UPDATE jobs SET exit_code=coalesce(exit_code,?) WHERE id=?",
                                  (code if code is not None else -15, job.id))
        supervisor.stop_job(job)
        with self.store.transaction():
            self.store.db.execute("UPDATE jobs SET settled=1 WHERE id=?", (job.id,))
        return code if code is not None else -15

    def _seal(self, row, *, stop=False):
        # Fence the execution host BEFORE recording a sealed ledger state. A
        # disconnected submission may still arrive during authority replacement.
        # Every workload has a durable job intent, so jobless partial preparation
        # needs no target probe (and may not have a prepared supervisor at all).
        if self._jobs(row):
            self._supervisor(row, prepare=False).seal()
        return self.store.seal(row["principal"], row["request"], row["fingerprint"], stop=stop)

    def _prepare_admission(self, row, policy):
        try:
            with self.store.transaction():
                self.store.db.execute("INSERT INTO execution VALUES (?,?,?,?)",
                                      (row["id"], policy["execution_uid"], Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8-sig"),
                                       json.dumps(policy.get("environment", {}))))
            self.store.validate_roots(row["id"])
            fds = self._root_fds.setdefault(row["id"], [])
            for root in row["roots"]:
                fd = os.open(root["canonical"], os.O_PATH | os.O_DIRECTORY)
                fds.append(fd)
                stat = os.fstat(fd)
                if [stat.st_dev, stat.st_ino] != root["ancestors"][0]:
                    raise ValueError("claim root identity changed during admission")
            runtime = self._runtime(row)
            runtime.parent.mkdir(mode=0o711, exist_ok=True)
            runtime.mkdir(mode=0o700)
            for name in ("tmp", "cache", "state", "hermes", "home", "data"):
                (runtime / name).mkdir(mode=0o700)
                os.chown(runtime / name, policy["execution_uid"], -1)
            os.chown(runtime, policy["execution_uid"], -1)
            control = self.state / row["id"]
            control.mkdir(mode=0o700)
            (control / "rootfs").mkdir(mode=0o755)
            self._supervisor(row)
            self._admissions.add(row["id"])
        except BaseException:
            # Never advertise a partial multi-root/runtime preparation. Restart
            # also seals active rows, including a crash before this fence.
            self._seal(row)
            raise

    def _release(self, row):
        self.store.settle(row["id"])
        self._admissions.discard(row["id"])
        for fd in self._root_fds.pop(row["id"], []):
            os.close(fd)

    def _settle(self, row, *, stop=False, release=False):
        if row["state"] == "settled":
            return True
        if row["state"] not in ("sealed", "stopping"):
            return False
        jobs = self._jobs(row, unsettled=True)
        if not jobs:
            # No command can pass the durable launch-intent boundary after seal.
            if release:
                self._release(row)
            return True
        supervisor = self._supervisor(row, prepare=False)
        supervisor.seal()
        for job in jobs:
            if stop or supervisor.inspect(job) is JobState.SETTLED:
                self._retire_job(row, job, supervisor)
        if self._jobs(row, unsettled=True):
            return False
        if release:
            self._release(row)
        return True

    def reconcile(self):
        with self._lock:
            for row in self.store.live():
                if row["state"] in ("sealed", "stopping"):
                    try:
                        self._settle(row, stop=row["state"] == "stopping")
                    except (OSError, SupervisionError):
                        continue  # UNKNOWN holds ownership; never run user commands here.

    def _admit(self, policy, message):
        paths = message["roots"]
        if not isinstance(paths, list) or not paths or len(paths) > 32:
            raise ValueError("roots must be a bounded directory list")
        # Policy roots are ownership domains: sharing writable metadata/hardlinks
        # between them requires the operator to select a common enclosing domain.
        roots = []
        for path in paths:
            identity = directory_identity(path)
            matches = [root for root in policy["roots"]
                       if os.path.commonpath([identity["canonical"], os.path.realpath(root)]) == os.path.realpath(root)]
            if not matches:
                raise PermissionError("directory is outside the enrolled write scope")
            for root in matches:
                if root not in roots:
                    roots.append(root)
        protected = [directory_identity(str(self.state))]
        runtime_root = self.state.parent / "jobs"
        runtime_root.mkdir(mode=0o711, exist_ok=True)
        protected.append(directory_identity(str(runtime_root)))
        if roots_overlap(protected, [directory_identity(root) for root in roots]):
            raise PermissionError("authority state overlaps the maintained scope")
        return paths, sorted(roots)

    def dispatch(self, peer_uid: int, message: dict) -> dict:
        with self._lock:
            if peer_uid not in self.principals:
                raise PermissionError("unenrolled control identity")
            policy = self.principals[peer_uid]
            principal = policy["id"]
            if message.get("principal") != principal:
                raise PermissionError("target principal does not match the authenticated peer")
            if message.get("version") != 1 or message.get("authority") != self.store.authority_id:
                raise ValueError("target authority identity/version mismatch")
            op = message.get("op")
            if op == "capabilities":
                return {"version": 1, "authority": self.store.authority_id}
            request, digest = message.get("request"), message.get("fingerprint")
            if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", v) for v in (request, digest)):
                raise ValueError("invalid ownership request identity")
            row = self.store.get(principal, request)
            if row and row["fingerprint"] != digest:
                raise ValueError("ownership request is immutable")
            if op == "reserve":
                if not row or row["roots"]:
                    roots, domains = self._admit(policy, message)
                    row = self.store.reserve(principal, request, digest, roots, domains=domains)
                if row["state"] == "active" and not self.store.db.execute(
                        "SELECT 1 FROM execution WHERE claim=?", (row["id"],)).fetchone():
                    self._prepare_admission(row, policy)
                if row["state"] == "active":
                    if row["id"] not in self._admissions:
                        raise SupervisionError("admission preparation is incomplete; stop this request before retrying")
                    self._supervisor(row)
                return {**self._receipt(row), "runtime_dir": str(self._runtime(row))}
            if op in ("stop", "seal", "release", "drain"):
                row = (self._seal(row, stop=op == "stop") if row is not None
                       else self.store.seal(principal, request, digest, stop=op == "stop"))
                # Sealing waits behind any in-flight target submission. The claim
                # remains held after drain, including across authority restarts;
                # only the controller's explicit release completes the lifetime.
                drained = self._settle(row, stop=row["state"] == "stopping", release=op == "release")
                return {**self._receipt(self.store.get(principal, request)), "drained": drained}
            if not row:
                raise ValueError("unknown ownership request")
            if op == "status":
                return self._receipt(row)
            if op == "jobs":
                return {"jobs": [job.id for job in self._jobs(row)]}
            if op == "start":
                return self._start(row, message)
            job = JobReceipt(message.get("job", ""))
            if job not in self._jobs(row):
                raise PermissionError("job does not belong to this ownership request")
            supervisor = self._supervisor(row)
            if op == "observe":
                offset = message.get("offset", 0)
                if type(offset) is not int or not 0 <= offset <= 2**63 - 1:
                    raise ValueError("invalid output offset")
                recorded = self.store.db.execute("SELECT exit_code,settled FROM jobs WHERE id=?", (job.id,)).fetchone()
                if recorded["settled"]:
                    state, code = JobState.SETTLED, recorded["exit_code"]
                    data = supervisor.read_output(job, offset)
                else:
                    state, code, data = supervisor.observe(job, offset)
                    if recorded["exit_code"] is not None:
                        code = recorded["exit_code"]
                if state is JobState.SETTLED and not recorded["settled"]:
                    code = self._retire_job(row, job, supervisor, observed_exit_code=code)
                return {"state": state.value, "exit_code": code, "output": base64.b64encode(data).decode()}
            if op == "stop_job":
                self._retire_job(row, job, supervisor)
                return {"state": JobState.SETTLED.value}
            raise ValueError("unknown authority operation")

    def _start(self, row, message):
        if row["state"] != "active":
            raise SupervisionError("target supervision is sealed")
        self.store.validate_roots(row["id"])
        cwd = directory_identity(message["cwd"])["canonical"]
        if not any(os.path.commonpath([cwd, root["canonical"]]) == root["canonical"] for root in row["roots"]):
            raise PermissionError("job cwd is outside the granted write scope")
        job_id = message.get("job")
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("invalid job identity")
        environment = message["environment"]
        if (not isinstance(environment, dict)
                or any(not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name)
                       or not isinstance(value, str) or "\0" in value for name, value in environment.items())
                or not isinstance(message.get("command"), str) or "\0" in message["command"]
                or (message.get("stdin") is not None and not isinstance(message["stdin"], str))):
            raise ValueError("invalid job command or environment")
        if row["id"] not in self._admissions:
            raise SupervisionError("admission preparation is incomplete")
        digest = fingerprint(message)
        previous = self.store.db.execute("SELECT claim,fingerprint,settled FROM jobs WHERE id=?", (job_id,)).fetchone()
        if previous:
            if (previous["claim"], previous["fingerprint"]) != (row["id"], digest):
                raise ValueError("job request is immutable")
            # Missing submission evidence is UNKNOWN. Never replay the command,
            # nor report a successful start merely because its intent persisted.
            if not previous["settled"] and self._supervisor(row).inspect(JobReceipt(job_id)) is JobState.UNKNOWN:
                raise SupervisionError("target job admission is unresolved")
            return {"job": job_id}
        with self.store.transaction():
            self.store.db.execute("INSERT INTO jobs(id,claim,fingerprint) VALUES (?,?,?)", (job_id, row["id"], digest))
        runtime = self._runtime(row)
        configured = json.loads(self.store.db.execute("SELECT environment FROM execution WHERE claim=?", (row["id"],)).fetchone()[0])
        environment = {**configured, **environment, "HOME": str(runtime / "home"), "TMPDIR": str(runtime / "tmp"), "TMP": str(runtime / "tmp"),
                       "TEMP": str(runtime / "tmp"), "XDG_CACHE_HOME": str(runtime / "cache"),
                       "XDG_DATA_HOME": str(runtime / "data"), "CARGO_HOME": str(runtime / "cache/cargo"),
                       "CARGO_TARGET_DIR": str(runtime / "cache/target"),
                       "XDG_STATE_HOME": str(runtime / "state"), "HERMES_HOME": str(runtime / "hermes")}
        self._supervisor(row).start(message["command"], cwd=cwd, environment_names=(),
                                     environment=environment, stdin=message.get("stdin"), job_id=job_id)
        return {"job": job_id}

    @staticmethod
    def _receipt(row):
        # Never reveal another controller's company, roots, job paths or identity.
        return {"claim": row["id"], "state": row["state"], "roots": [root["path"] for root in row["roots"]]}
