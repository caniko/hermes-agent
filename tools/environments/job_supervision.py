"""Execution-host ownership contract, independent of transport and supervisor.

An unknown observation never proves settlement. Sealing fences future launches;
stopping additionally terminates every job admitted before that fence.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class JobState(Enum):
    RUNNING = "running"
    SETTLED = "settled"
    UNKNOWN = "unknown"


class SupervisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class JobReceipt:
    id: str


class TargetJobSupervisor(Protocol):
    def prepare(self) -> None: ...
    def start(self, command: str, *, cwd: str, environment_names: tuple[str, ...],
              stdin: str | None = None) -> JobReceipt: ...
    def jobs(self) -> list[JobReceipt]: ...
    def inspect(self, job: JobReceipt) -> JobState: ...
    def output(self, job: JobReceipt) -> str: ...
    def read_output(self, job: JobReceipt, offset: int) -> bytes: ...
    def main_exit_code(self, job: JobReceipt) -> int | None: ...
    def exit_code(self, job: JobReceipt) -> int: ...
    def stop_job(self, job: JobReceipt) -> None: ...
    def seal(self) -> None: ...
    def stop(self) -> None: ...
    def settled(self) -> bool: ...
