"""Hermes execution environment backends: one BaseEnvironment ABC for running shell commands
in a specific context (local, Docker, SSH, Singularity, Modal direct/Nous-managed, Daytona,
Vercel Sandbox). ``terminal_tool._create_environment`` selects the backend from TERMINAL_ENV."""

__all__ = ["BaseEnvironment"]


def __getattr__(name):
    # The ownership client imports this namespace for its stdlib-only transport.
    # Loading a terminal backend here repeats config/YAML startup for every RPC.
    if name == "BaseEnvironment":
        from tools.environments.base import BaseEnvironment

        return BaseEnvironment
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
