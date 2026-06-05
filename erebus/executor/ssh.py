"""Remote no-shell-injection executor over SSH. Credentials live here, never in
the agent. SSH runs a command string through the remote login shell, so safety
comes from shlex-quoting every (already policy-validated) argv element: shell
metacharacters in arguments become literal and cannot chain.
"""
from __future__ import annotations

import asyncio
import shlex
from typing import Callable

from erebus.executor.base import ExecResult, ExecutionTimeout


class SSHExecutor:
    def __init__(
        self,
        *,
        host: str,
        username: str,
        port: int = 22,
        client_keys: list[str] | None = None,
        known_hosts: str | None = None,
        connect: Callable[[], object] | None = None,
    ) -> None:
        self._host = host
        self._username = username
        self._port = port
        self._client_keys = client_keys
        self._known_hosts = known_hosts
        self._connect = connect or self._default_connect

    async def execute(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        if not argv:
            raise ValueError("argv must be non-empty")
        command = self._build_remote_command(argv, cwd)
        try:
            async with self._connect() as conn:
                result = await asyncio.wait_for(conn.run(command), timeout=timeout)
        except asyncio.TimeoutError:
            raise ExecutionTimeout(
                f"remote command timed out after {timeout}s: {argv}"
            ) from None
        return ExecResult(
            exit_code=int(getattr(result, "exit_status", 0) or 0),
            stdout=_as_text(result.stdout),
            stderr=_as_text(result.stderr),
        )

    @staticmethod
    def _build_remote_command(argv: list[str], cwd: str | None) -> str:
        command = " ".join(shlex.quote(a) for a in argv)
        if cwd:
            command = f"cd {shlex.quote(cwd)} && {command}"
        return command

    def _default_connect(self):  # pragma: no cover - needs a real SSH server
        import asyncssh

        return asyncssh.connect(
            self._host, port=self._port, username=self._username,
            client_keys=self._client_keys, known_hosts=self._known_hosts,
        )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)
