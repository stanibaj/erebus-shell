# Erebus Shell — Phase 6: SSH Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Implements the `Executor` Protocol (Phase 2) for remote execution. Phases 1–5 merged. `asyncssh>=2.14` added; image rebuilt. All runs in Docker.

**Goal:** Add an `SSHExecutor` that runs an `argv` on a remote host with credentials held by the executor process (never the agent), preserving the no-shell-injection guarantee by `shlex.quote`-ing every argument, and wire executor selection (local vs ssh) into the gate's env factory.

**Architecture:** SSH inherently runs a command string through the remote login shell, so the local "no `sh -c`" guarantee becomes "**no shell *injection***": `SSHExecutor` builds the remote command by `shlex.quote`-ing each already-validated argv element (the gate has already rejected shell operators in the original command), so metacharacters in arguments are literal and cannot chain. The asyncssh connection is created by an injectable `connect` callable, so `execute()` — command building, result mapping, timeout — is fully unit-tested with a fake connection; only the real `asyncssh.connect` default is `pragma: no cover`. A `build_executor_from_env()` factory selects `LocalExecutor` or `SSHExecutor` from env, and `build_gate_from_env` uses it.

**Tech Stack:** `asyncssh`, `shlex`, Phase 2 `Executor`/`ExecResult`/`ExecutionTimeout`, `pytest`.

---

### Task 1: SSHExecutor

**Files:** Create `erebus/executor/ssh.py`; Test `tests/executor/test_ssh.py`.

- [ ] **Step 1: failing tests** — `tests/executor/test_ssh.py`:

```python
import asyncio
import pytest

from erebus.executor.ssh import SSHExecutor
from erebus.executor.base import ExecutionTimeout


class _FakeProc:
    def __init__(self, stdout="", stderr="", exit_status=0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class _FakeConn:
    def __init__(self, proc, *, sleep=0.0, record=None):
        self._proc = proc
        self._sleep = sleep
        self._record = record

    async def run(self, command):
        if self._record is not None:
            self._record.append(command)
        if self._sleep:
            await asyncio.sleep(self._sleep)
        return self._proc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _executor(proc=None, *, sleep=0.0, record=None):
    proc = proc or _FakeProc(stdout="ok\n", exit_status=0)
    return SSHExecutor(host="h", username="u",
                       connect=lambda: _FakeConn(proc, sleep=sleep, record=record))


def test_build_remote_command_quotes_args():
    cmd = SSHExecutor._build_remote_command(["echo", "hi && rm -rf /"], None)
    # The dangerous arg is single-quoted -> the remote shell cannot chain.
    assert cmd == "echo 'hi && rm -rf /'"


def test_build_remote_command_with_cwd():
    cmd = SSHExecutor._build_remote_command(["ls"], "/var/log")
    assert cmd == "cd /var/log && ls"


async def test_execute_maps_result():
    ex = _executor(_FakeProc(stdout="out\n", stderr="err\n", exit_status=3))
    r = await ex.execute(["echo", "x"])
    assert r.exit_code == 3
    assert r.stdout == "out\n"
    assert r.stderr == "err\n"


async def test_execute_sends_quoted_command():
    rec = []
    ex = _executor(record=rec)
    await ex.execute(["echo", "a b", "&&", "whoami"])
    assert rec == ["echo 'a b' '&&' whoami"]


async def test_empty_argv_raises():
    with pytest.raises(ValueError):
        await _executor().execute([])


async def test_timeout_raises():
    ex = _executor(sleep=5)
    with pytest.raises(ExecutionTimeout):
        await ex.execute(["sleep", "5"], timeout=0.1)
```

- [ ] **Step 2: run, expect fail** — `docker compose run --rm test pytest tests/executor/test_ssh.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: implement** — `erebus/executor/ssh.py`:

```python
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
```

- [ ] **Step 4: run, expect pass** (6 tests). **Step 5: commit** `feat(executor): SSHExecutor (remote no-shell-injection over SSH)`.

---

### Task 2: Executor factory + wire into the gate env

**Files:** Modify `erebus/mcp/server.py`; Test `tests/mcp/test_executor_factory.py`.

- [ ] **Step 1: failing tests** — `tests/mcp/test_executor_factory.py`:

```python
import pytest

from erebus.mcp.server import build_executor_from_env
from erebus.executor.local import LocalExecutor
from erebus.executor.ssh import SSHExecutor


def test_defaults_to_local(monkeypatch):
    monkeypatch.delenv("EREBUS_EXECUTOR", raising=False)
    assert isinstance(build_executor_from_env(), LocalExecutor)


def test_explicit_local(monkeypatch):
    monkeypatch.setenv("EREBUS_EXECUTOR", "local")
    assert isinstance(build_executor_from_env(), LocalExecutor)


def test_ssh_builds_ssh_executor_without_connecting(monkeypatch):
    monkeypatch.setenv("EREBUS_EXECUTOR", "ssh")
    monkeypatch.setenv("EREBUS_SSH_HOST", "box.local")
    monkeypatch.setenv("EREBUS_SSH_USER", "ops")
    monkeypatch.setenv("EREBUS_SSH_KEY", "/keys/id_ed25519")
    ex = build_executor_from_env()
    assert isinstance(ex, SSHExecutor)


def test_ssh_missing_host_raises(monkeypatch):
    monkeypatch.setenv("EREBUS_EXECUTOR", "ssh")
    monkeypatch.delenv("EREBUS_SSH_HOST", raising=False)
    with pytest.raises(KeyError):
        build_executor_from_env()
```

- [ ] **Step 2: run, expect fail** (ImportError: `build_executor_from_env`).

- [ ] **Step 3: implement** — edit `erebus/mcp/server.py`. Add imports and the factory, and use it in `build_gate_from_env`:

Add near the other imports:

```python
from erebus.executor.ssh import SSHExecutor
```

Add the factory function (above `build_gate_from_env`):

```python
def build_executor_from_env():
    kind = os.environ.get("EREBUS_EXECUTOR", "local")
    if kind == "local":
        return LocalExecutor()
    if kind == "ssh":
        host = os.environ["EREBUS_SSH_HOST"]
        user = os.environ["EREBUS_SSH_USER"]
        key = os.environ.get("EREBUS_SSH_KEY")
        port = int(os.environ.get("EREBUS_SSH_PORT", "22"))
        known_hosts = os.environ.get("EREBUS_SSH_KNOWN_HOSTS")
        return SSHExecutor(
            host=host, username=user, port=port,
            client_keys=[key] if key else None, known_hosts=known_hosts,
        )
    raise ValueError(f"unknown EREBUS_EXECUTOR: {kind}")
```

Change `build_gate_from_env` to use the factory — replace the `executor=LocalExecutor()` line:

```python
    return CommandGate(
        run_id=run_id, engine=engine, executor=build_executor_from_env(),
        tickets=tickets, store=store, ttl_hours=ttl_hours,
    )
```

- [ ] **Step 4: run, expect pass** (4 tests). Also re-run `tests/mcp/test_server.py` — `build_gate_from_env` still returns a `CommandGate` (defaults to local).

- [ ] **Step 5: commit** `feat(mcp): executor factory (local|ssh) wired into gate env`.

---

### Task 3: Phase 6 gate

- [ ] **Step 1:** `docker compose run --rm test pytest -q` → Phases 1–5 (87) + Phase 6 (6 + 4 = 10) = **97 tests** pass.
- [ ] **Step 2:** Confirm no-shell-injection invariant note holds: `grep -rn "shell=True\|os.system\|create_subprocess_shell\|sh -c" erebus/` → exit 1 (the SSH path uses the remote shell by protocol necessity, defended by quoting — not a local shell call).

---

## Self-Review

- **Spec coverage:** `SSHExecutor` satisfies the Phase 2 `Executor` Protocol; credentials held server-side (Decision #6 strong isolation, homelab reference). Executor selection wired into the gate (Decision: pluggable executor). Quoting defense documented (overview hardening: enforce at the execution chokepoint; least-privilege remote user remains a deployment concern). Tested without a live SSH server via injected `connect`.
- **Placeholder scan:** none; only `_default_connect` is `pragma: no cover`.
- **Type consistency:** `SSHExecutor.execute(argv, *, cwd, timeout) -> ExecResult` matches the `Executor` Protocol exactly; raises `ValueError`/`ExecutionTimeout` like `LocalExecutor`. `build_executor_from_env()` returns an `Executor`; `build_gate_from_env` passes it to `CommandGate(executor=...)` unchanged.

## Next

Phase 7 (`ZohoTicketProvider`) and Phase 8 (`OpenCodeAdapter`). Deployment: document SSH key provisioning + a least-privilege remote user; set `EREBUS_EXECUTOR=ssh` + `EREBUS_SSH_*` for the homelab reference.
