"""Durable, target-local arbitration. Only the target supervisor may settle claims.

The database must be explicitly initialized on local storage outside maintained
roots. Lost state is an error, never an empty lock table. No active claim expires.
"""

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path


def directory_identity(path: str) -> dict:
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise ValueError("claim roots must be absolute directories")
    canonical = os.path.realpath(path, strict=True)
    ancestors = []
    current = canonical
    while True:
        stat = os.stat(current)
        ancestors.append([stat.st_dev, stat.st_ino])
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    if not os.path.isdir(canonical):
        raise ValueError("claim root is not a directory")
    return {"path": path, "canonical": canonical, "ancestors": ancestors}


def roots_overlap(left: list[dict], right: list[dict]) -> bool:
    # Keep both the inode identity (renames/aliases) and the pathname reservation
    # (replacement at the old name). A changed root cannot make a live claim free.
    return any(
        a["ancestors"][0] in b["ancestors"] or b["ancestors"][0] in a["ancestors"]
        or any(os.path.commonpath([left_path, right_path]) in (left_path, right_path)
               for left_path in (os.path.normpath(a["path"]), a["canonical"])
               for right_path in (os.path.normpath(b["path"]), b["canonical"]))
        for a in left for b in right
    )


class ClaimStore:
    @staticmethod
    def initialize(state: Path) -> None:
        state.mkdir(mode=0o700)  # Refuse accidental replacement/reinitialization.
        connection = sqlite3.connect(state / "claims.sqlite")
        try:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE authority (id TEXT NOT NULL);
                CREATE TABLE claims (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, principal TEXT NOT NULL,
                    request TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    roots TEXT NOT NULL, domains TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL,
                    UNIQUE(principal, request)
                );
                CREATE INDEX claims_live ON claims(state, sequence);
            """)
            connection.execute("INSERT INTO authority VALUES (?)", (uuid.uuid4().hex,))
            connection.commit()
        finally:
            connection.close()
        os.chmod(state / "claims.sqlite", 0o600)
        fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def __init__(self, state: Path, *, max_pending: int = 128):
        self._lock = threading.RLock()
        self.db = sqlite3.connect((state / "claims.sqlite").as_uri() + "?mode=rw", uri=True,
                                  isolation_level=None, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.authority_id = self.db.execute("SELECT id FROM authority").fetchone()[0]
        self.max_pending = max_pending
        with self.transaction():
            if "domains" not in {column[1] for column in self.db.execute("PRAGMA table_info(claims)")}:
                self.db.execute("ALTER TABLE claims ADD COLUMN domains TEXT NOT NULL DEFAULT '[]'")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    @staticmethod
    def _row(row):
        if row is None:
            return None
        roots = json.loads(row["roots"])
        return {**dict(row), "roots": roots, "domains": json.loads(row["domains"]) or roots}

    def get(self, principal, request):
        with self._lock:
            return self._row(self.db.execute("SELECT * FROM claims WHERE principal=? AND request=?",
                                            (principal, request)).fetchone())

    def live(self):
        with self._lock:
            return [self._row(row) for row in self.db.execute(
                "SELECT * FROM claims WHERE state != 'settled' ORDER BY sequence")]

    def reserve(self, principal: str, request: str, fingerprint: str, paths: list[str],
                *, domains: list[str] | None = None) -> dict:
        if not all(isinstance(value, str) and 0 < len(value) <= 255 for value in (principal, request, fingerprint)):
            raise ValueError("invalid claim identity")
        domain_paths = paths if domains is None else domains
        with self.transaction():
            row = self.get(principal, request)
            if row:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("claim request is immutable")
                if row["roots"] and paths != [root["path"] for root in row["roots"]]:
                    raise ValueError("claim roots are immutable")
                if row["roots"] and domain_paths != [domain["path"] for domain in row["domains"]]:
                    raise ValueError("claim domains are immutable")
                if row["state"] != "pending":
                    return row
                self.validate_roots(row["id"])
            else:
                if not 1 <= len(paths) <= 32 or not 1 <= len(domain_paths) <= 32:
                    raise ValueError("claim requires 1..32 roots")
                if sum(item["state"] == "pending" for item in self.live()) >= self.max_pending:
                    raise ValueError("target ownership queue is full")
                roots = [directory_identity(path) for path in paths]
                domain_roots = [directory_identity(path) for path in domain_paths]
                if any(not any(os.path.commonpath([root["canonical"], domain["canonical"]]) == domain["canonical"]
                               for domain in domain_roots) for root in roots):
                    raise ValueError("claim roots must be within their ownership domains")
                self.db.execute("INSERT INTO claims(id,principal,request,fingerprint,roots,domains,state) VALUES (?,?,?,?,?,?,'pending')",
                                (uuid.uuid4().hex, principal, request, fingerprint, json.dumps(roots), json.dumps(domain_roots)))
                row = self.get(principal, request)
            # Older conflicting waiters cannot be overtaken. Unrelated roots do
            # not suffer head-of-line blocking. All roots grant in one transaction.
            blockers = (other for other in self.live() if other["id"] != row["id"]
                        and (other["state"] != "pending" or other["sequence"] < row["sequence"]))
            # Domains serialize shared metadata; requested roots also retain a
            # moved inode or swapped alias even when it leaves its original domain.
            if not any(roots_overlap(row["domains"] + row["roots"], other["domains"] + other["roots"])
                       for other in blockers):
                self.db.execute("UPDATE claims SET state='active' WHERE id=?", (row["id"],))
            return self.get(principal, request)

    def seal(self, principal, request, fingerprint, *, stop=False):
        with self.transaction():
            row = self.get(principal, request)
            if row is None:
                # A durable stop-before-create receipt survives arbitrarily late
                # duplicate admissions, independently of ordinary log retention.
                self.db.execute("INSERT INTO claims(id,principal,request,fingerprint,roots,state) VALUES (?,?,?,?,'[]','settled')",
                                (uuid.uuid4().hex, principal, request, fingerprint))
            else:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("claim request is immutable")
                state = ("settled" if row["state"] in ("pending", "settled") else
                         "stopping" if stop or row["state"] == "stopping" else "sealed")
                self.db.execute("UPDATE claims SET state=? WHERE id=?", (state, row["id"]))
            return self.get(principal, request)

    def validate_roots(self, claim_id):
        row = self._row(self.db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone())
        for root in row["roots"] + row["domains"]:
            if directory_identity(root["path"]) != root:
                raise ValueError("claim root identity changed")

    def settle(self, claim_id):
        # Caller has fenced launches AND obtained positive target settlement.
        with self.transaction():
            changed = self.db.execute("UPDATE claims SET state='settled' WHERE id=? AND state IN ('sealed','stopping','settled')",
                                      (claim_id,)).rowcount
            if not changed:
                raise ValueError("claim must be sealed before settlement")
