"""Local no-shell executor. The ONLY process-spawning code path for local exec.

`asyncio.create_subprocess_exec` calls execve directly with the argv list — it
never routes through `/bin/sh`, so shell metacharacters in argv are passed as
literal arguments and cannot chain or expand. This is the enforcement point for
the no-shell security invariant (see overview hardening checklist).
"""
from __future__ import annotations

import asyncio

from erebus.executor.base import ExecResult, ExecutionTimeout


class LocalExecutor:
    async def execute(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        if not argv:
            raise ValueError("argv must be non-empty")

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ExecutionTimeout(
                f"command timed out after {timeout}s: {argv}"
            ) from None

        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )
