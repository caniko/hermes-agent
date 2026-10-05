"""Opt-in ownership intent before workspace preparation or model admission."""

import asyncio
import re
import shlex
import subprocess

from aiohttp import web

from gateway.platforms.api_server_execution_context import ExecutionContextError


def validate_ownership(value):
    if (not isinstance(value, dict) or set(value) != {"authority", "principal", "request", "fingerprint", "roots"}
            or any(not isinstance(value[k], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", value[k])
                   for k in ("authority", "principal", "request", "fingerprint"))
            or not isinstance(value["roots"], list) or not 1 <= len(value["roots"]) <= 32
            or any(not isinstance(p, str) or not p.startswith("/") or "\0" in p for p in value["roots"])):
        raise ExecutionContextError("invalid filesystem ownership intent", status=400)


def worker_authority(config):
    value = config.get("filesystem_authority")
    if not value:
        return None
    if (not isinstance(value, dict) or set(value) != {"authority", "principal", "socket", "command"}
            or any(not isinstance(value[k], str) or not value[k] for k in ("authority", "principal", "socket"))
            or not value["socket"].startswith("/") or "\0" in value["socket"]
            or not isinstance(value["command"], list) or not value["command"]
            or any(not isinstance(arg, str) or "\0" in arg for arg in value["command"])):
        raise ExecutionContextError("invalid terminal.filesystem_authority enrollment")
    return value


def ownership_supervisor(context):
    from tools.environments.filesystem_supervisor import FilesystemSupervisor
    from tools.environments.local import _make_run_env
    from tools.environments.remote_common import client_env_with
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_backends import _build_ssh_env, _ssh_config_from_config

    config = _get_env_config()
    policy = worker_authority(config)
    descriptor = context.requested["ownership"]
    if policy is None or any(policy[key] != descriptor[key] for key in ("authority", "principal")):
        raise ExecutionContextError("filesystem ownership does not match worker enrollment")
    command = policy["command"] + ["request", "--socket", policy["socket"]]
    transport = None
    if context.requested["backend"] == "local":
        env = _make_run_env({})
        def call(payload):
            return subprocess.run(command, input=payload, env=env, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=45)
    else:
        transport = _build_ssh_env(cwd=context.requested["cwd"], timeout=10, probe_only=True,
                                   ssh_config=_ssh_config_from_config(config))
        def call(payload):
            return subprocess.run(transport._build_ssh_command() + [shlex.join(command)], input=payload,
                                  env=client_env_with({}), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=45)
    return FilesystemSupervisor(call, descriptor, {}), transport


async def handle_ownership(adapter, request, *, api):
    from gateway.platforms.api_server_execution_context import bind_execution_context, capture_execution_context
    from tools.environments.filesystem_supervisor import AuthorityRequestRejected, OwnershipPending
    from tools.environments.job_supervision import SupervisionError

    auth_error = adapter._check_auth(request)
    if auth_error is not None:
        return auth_error
    transport = None
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"operation", "execution_context"}:
            raise ExecutionContextError("ownership requires operation and execution_context", status=400)
        if body["operation"] not in {"reserve", "stop", "release", "status"}:
            raise ExecutionContextError("invalid ownership operation", status=400)
        with adapter._profile_scope(api._api_request_profile.get()):
            context = capture_execution_context(body["execution_context"])
            if "ownership" not in context.requested or not adapter._run_idempotency_store.durable:
                raise ExecutionContextError("ownership requires a durable supervised worker", status=400)
            with bind_execution_context(context):
                supervisor, transport = await asyncio.to_thread(ownership_supervisor, context)
                if body["operation"] == "reserve":
                    try:
                        await asyncio.to_thread(supervisor.prepare)
                    except OwnershipPending:
                        return web.json_response({"state": "pending"})
                elif body["operation"] == "stop":
                    await asyncio.to_thread(supervisor.stop)
                elif body["operation"] == "release":
                    await asyncio.to_thread(supervisor.release)
                receipt = await asyncio.to_thread(supervisor.status)
                return web.json_response(receipt)
    except ExecutionContextError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)
    except AuthorityRequestRejected:
        return web.json_response({"code": "rejected", "error": "target authority rejected the ownership request"}, status=409)
    except (SupervisionError, OSError, subprocess.SubprocessError):
        return web.json_response({"error": "target ownership is unresolved"}, status=503)
    finally:
        if transport is not None:
            await asyncio.to_thread(transport.cleanup)
