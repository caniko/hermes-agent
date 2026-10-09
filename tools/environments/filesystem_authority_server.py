"""Unix control transport for the optional systemd filesystem authority.

python -m tools.environments.filesystem_authority_server init --state PATH
python -m tools.environments.filesystem_authority_server serve --config PATH
python -m tools.environments.filesystem_authority_server request --socket PATH

The request command is usable directly or through OpenSSH. It carries JSON on
stdin, never in process arguments, and the server authenticates SO_PEERCRED.
"""

import argparse
import json
import os
import socket
import socketserver
import stat
import struct
import sys
import threading
from pathlib import Path

from hermes_platform.host.facts import os_family
from tools.environments.filesystem_authority import FilesystemAuthority
from tools.environments.filesystem_claims import ClaimStore

MAX_MESSAGE = 8 * 1024 * 1024


def read_message(stream):
    data = stream.readline(MAX_MESSAGE + 1)
    if not data.endswith(b"\n") or len(data) > MAX_MESSAGE:
        raise ValueError("authority message exceeds limit or is incomplete")
    message = json.loads(data)
    if not isinstance(message, dict):
        raise ValueError("authority message must be an object")
    return message


def socket_parent(socket_path):
    """Pin an absolute protected directory chain, without following symlinks."""
    path = Path(socket_path)
    if not path.is_absolute() or ".." in path.parts:
        raise PermissionError("authority socket requires an absolute protected path")
    getuid = getattr(os, "geteuid", None)
    if os_family() != "linux" or getuid is None:
        raise RuntimeError("filesystem authority sockets require Linux")
    owner = getuid()
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
    try:
        for index, name in enumerate(path.parts[1:-1], 1):
            child = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            metadata = os.fstat(fd)
            writable = metadata.st_mode & 0o022
            # Root-owned sticky ancestors (e.g. /tmp) cannot replace the
            # service-owned next component. The immediate parent is never shared.
            sticky_ancestor = (index < len(path.parts) - 2 and metadata.st_uid == 0
                               and metadata.st_mode & stat.S_ISVTX)
            if metadata.st_uid not in (0, owner) or (writable and not sticky_ancestor):
                raise PermissionError("authority socket ancestry is not protected")
        return fd, path.name, owner
    except BaseException:
        os.close(fd)
        raise


def request(socket_path, message):
    payload = json.dumps(message).encode() + b"\n"
    if len(payload) > MAX_MESSAGE:
        raise ValueError("authority message exceeds limit")
    fd, name, owner = socket_parent(socket_path)
    try:
        metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid not in (0, owner):
            raise PermissionError("authority socket owner is not trusted")
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(40)
            client.connect(f"/proc/self/fd/{fd}/{name}")
            _, uid, _ = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != metadata.st_uid:
                raise PermissionError("authority server peer does not own the protected socket")
            client.sendall(payload)
            with client.makefile("rb") as stream:
                return read_message(stream)
    finally:
        os.close(fd)


class AuthorityServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, socket_path, authority, *, replace_existing=False):
        self.authority = authority
        self._slots = threading.BoundedSemaphore(32)
        self._parent_fd, name, owner = socket_parent(socket_path)
        pinned = f"/proc/self/fd/{self._parent_fd}/{name}"
        try:
            if replace_existing:
                try:
                    metadata = os.stat(name, dir_fd=self._parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if metadata.st_uid != owner or not stat.S_ISSOCK(metadata.st_mode):
                        raise PermissionError("refusing to replace a foreign control socket")
                    os.unlink(name, dir_fd=self._parent_fd)
            super().__init__(pinned, AuthorityHandler)
            os.chmod(pinned, 0o660)
        except BaseException:
            if hasattr(self, "socket"):
                self.server_close()
            elif self._parent_fd is not None:
                os.close(self._parent_fd)
                self._parent_fd = None
            raise

    def server_close(self):
        try:
            super().server_close()
        finally:
            if self._parent_fd is not None:
                os.close(self._parent_fd)
                self._parent_fd = None

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class AuthorityHandler(socketserver.StreamRequestHandler):
    timeout = 40

    def handle(self):
        try:
            _, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            result = self.server.authority.dispatch(uid, read_message(self.rfile))
            response = {"ok": True, "result": result}
        except PermissionError:
            response = {"ok": False, "code": "rejected", "error": "authority access denied"}
        except (ValueError, KeyError, TypeError):
            response = {"ok": False, "code": "rejected", "error": "invalid authority request or changed target identity"}
        except Exception:
            # No command text, env, foreign claim data, or credential paths leak
            # into the transport. An unavailable observation is never settlement.
            response = {"ok": False, "error": "target ownership unresolved"}
        self.wfile.write(json.dumps(response).encode() + b"\n")


def serve(config_path):
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise PermissionError("the cross-user authority must run as a system service")
    path = Path(config_path)
    metadata = path.stat()
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise PermissionError("authority enrollment must be root-owned and not writable by other users")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    principals = {int(key): value for key, value in config["principals"].items()}
    for uid, policy in principals.items():
        if uid <= 0 or type(policy["execution_uid"]) is not int or policy["execution_uid"] <= 0:
            raise ValueError("control and workload identities must be non-root")
        if uid == policy["execution_uid"]:
            raise ValueError("control and workload identities must be separate")
    authority = FilesystemAuthority(Path(config["state"]), principals)
    socket_path = Path(config["socket"])
    done = threading.Event()
    def reconcile():
        while not done.wait(1):
            authority.reconcile()
    thread = threading.Thread(target=reconcile, name="target-ownership-recovery", daemon=True)
    try:
        authority.reconcile()
        # The singleton authority flock proves the old service is gone. Parent
        # validation and pinning precede both stale-socket unlink and bind.
        with AuthorityServer(str(socket_path), authority, replace_existing=True) as server:
            os.chown(server.server_address, 0, int(config["control_gid"]))
            thread.start()
            server.serve_forever()
    finally:
        done.set()
        if thread.is_alive():
            thread.join()
        authority.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init").add_argument("--state", type=Path, required=True)
    commands.add_parser("serve").add_argument("--config", required=True)
    commands.add_parser("request").add_argument("--socket", required=True)
    args = parser.parse_args()
    if args.command == "init":
        ClaimStore.initialize(args.state)
        store = ClaimStore(args.state)
        try:
            print(store.authority_id)
        finally:
            store.close()
    elif args.command == "request":
        print(json.dumps(request(args.socket, read_message(sys.stdin.buffer))))
    else:
        serve(args.config)


if __name__ == "__main__":
    main()
