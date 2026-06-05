"""Executor interface: run an argv with no shell and return its result."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ExecutionTimeout(Exception):
    """Raised when a command exceeds its allotted wall-clock time."""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class Executor(Protocol):
    async def execute(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult: ...
