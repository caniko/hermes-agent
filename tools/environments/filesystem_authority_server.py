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


def request(socket_path, message):
    payload = json.dumps(message).encode() + b"\n"
    if len(payload) > MAX_MESSAGE:
        raise ValueError("authority message exceeds limit")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(40)
        client.connect(socket_path)
        client.sendall(payload)
        with client.makefile("rb") as stream:
            return read_message(stream)


class AuthorityServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, socket_path, authority):
        self.authority = authority
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(socket_path, AuthorityHandler)
        os.chmod(socket_path, 0o660)

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
    if os.geteuid() != 0:  # windows-footgun: ok — Linux root-owned enrollment service.
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
    if socket_path.exists():
        metadata = socket_path.lstat()
        if metadata.st_uid != 0 or not stat.S_ISSOCK(metadata.st_mode):
            raise PermissionError("refusing to replace a foreign control socket")
        socket_path.unlink()  # Singleton flock already proves no old server owns it.
    done = threading.Event()
    def reconcile():
        while not done.wait(1):
            authority.reconcile()
    thread = threading.Thread(target=reconcile, name="target-ownership-recovery", daemon=True)
    try:
        authority.reconcile()
        with AuthorityServer(str(socket_path), authority) as server:
            os.chown(socket_path, 0, int(config["control_gid"]))
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
