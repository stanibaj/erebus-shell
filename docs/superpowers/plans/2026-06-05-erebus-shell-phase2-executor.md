# Erebus Shell — Phase 2: Local Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development). Read `2026-06-05-erebus-shell-overview.md` for the locked `Executor`/`ExecResult` interface. Phase 1 (policy + state) is merged to `main`.

**Goal:** Build the `Executor` interface and a `LocalExecutor` that runs an `argv` list **with no shell**, captures stdout/stderr/exit code, and enforces a timeout — including the mandatory security test proving `&&` does not chain.

**Architecture:** `executor/base.py` defines the `Executor` Protocol, the `ExecResult` dataclass, and an `ExecutionTimeout` exception. `executor/local.py` implements `LocalExecutor` using `asyncio.create_subprocess_exec` (which `execve`s argv directly — never `sh -c`). This is the only place in the codebase that spawns processes.

**Tech Stack:** Python 3.11+ `asyncio` (subprocess), `pytest` + `pytest-asyncio` (auto mode already configured). All runs happen in the Docker container: `docker compose run --rm test pytest ...`.

---

### Task 1: Executor interface + ExecResult + timeout exception

**Files:**
- Create: `erebus/executor/__init__.py` (empty)
- Create: `erebus/executor/base.py`
- Test: `tests/executor/__init__.py` (empty), `tests/executor/test_base.py`

- [ ] **Step 1: Write the failing test**

Create `tests/executor/__init__.py` (empty). Create `tests/executor/test_base.py`:

```python
from erebus.executor.base import ExecResult, ExecutionTimeout, Executor


def test_exec_result_fields():
    r = ExecResult(exit_code=0, stdout="out", stderr="err")
    assert r.exit_code == 0
    assert r.stdout == "out"
    assert r.stderr == "err"


def test_execution_timeout_is_exception():
    assert issubclass(ExecutionTimeout, Exception)


def test_executor_is_protocol():
    # Protocol classes are not directly instantiable as concrete types,
    # but the symbol must exist and carry the execute attribute.
    assert hasattr(Executor, "execute")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm test pytest tests/executor/test_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erebus.executor'`.

- [ ] **Step 3: Write the implementation**

Create `erebus/executor/__init__.py` (empty). Create `erebus/executor/base.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm test pytest tests/executor/test_base.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add erebus/executor/__init__.py erebus/executor/base.py tests/executor/__init__.py tests/executor/test_base.py
git commit -m "feat(executor): Executor protocol, ExecResult, ExecutionTimeout"
```

---

### Task 2: LocalExecutor (no-shell subprocess) + the anti-chaining security test

**Files:**
- Create: `erebus/executor/local.py`
- Test: `tests/executor/test_local.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/executor/test_local.py`:

```python
import pytest
from erebus.executor.local import LocalExecutor
from erebus.executor.base import ExecutionTimeout


@pytest.fixture()
def ex() -> LocalExecutor:
    return LocalExecutor()


async def test_runs_simple_command(ex):
    r = await ex.execute(["echo", "hi"])
    assert r.exit_code == 0
    assert r.stdout.strip() == "hi"
    assert r.stderr == ""


async def test_captures_nonzero_exit_and_stderr(ex):
    r = await ex.execute(["ls", "/this_path_does_not_exist_xyz"])
    assert r.exit_code != 0
    assert "No such file" in r.stderr or "cannot access" in r.stderr


async def test_no_shell_chaining(ex):
    # The classic bypass: with a shell, this would run `echo pwned` too.
    # With execve, `&&` and the rest are literal args to the single `echo`.
    r = await ex.execute(["echo", "hi", "&&", "echo", "pwned"])
    assert r.exit_code == 0
    assert r.stdout.strip() == "hi && echo pwned"


async def test_respects_cwd(ex):
    r = await ex.execute(["pwd"], cwd="/tmp")
    assert r.stdout.strip() == "/tmp"


async def test_timeout_raises(ex):
    with pytest.raises(ExecutionTimeout):
        await ex.execute(["sleep", "5"], timeout=0.2)


async def test_empty_argv_raises(ex):
    with pytest.raises(ValueError):
        await ex.execute([])


async def test_missing_binary_raises_filenotfound(ex):
    with pytest.raises(FileNotFoundError):
        await ex.execute(["definitely_not_a_real_binary_xyz123"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose run --rm test pytest tests/executor/test_local.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erebus.executor.local'`.

- [ ] **Step 3: Write the implementation**

Create `erebus/executor/local.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose run --rm test pytest tests/executor/test_local.py -v`
Expected: PASS (7 tests). The key one is `test_no_shell_chaining`.

- [ ] **Step 5: Commit**

```bash
git add erebus/executor/local.py tests/executor/test_local.py
git commit -m "feat(executor): LocalExecutor with no-shell exec + anti-chaining test"
```

---

### Task 3: Phase 2 gate

- [ ] **Step 1: Run the full suite**

Run: `docker compose run --rm test pytest -q`
Expected: PASS — Phase 1 (36) + Phase 2 (3 + 7 = 10) = 46 tests.

- [ ] **Step 2: Re-confirm the no-shell invariant across the package**

Run: `grep -rn "shell=True\|os.system\|create_subprocess_shell\|sh -c" erebus/ ; echo "exit: $?"`
Expected: no matches (exit 1). Note we specifically forbid `create_subprocess_shell` (the shell variant of the asyncio API) in addition to the earlier patterns.

---

## Self-Review

- **Spec coverage:** Phase 2 scope = `Executor` interface + `LocalExecutor` with no-shell exec, capture, timeout (overview interfaces + hardening item "no-shell exec is mandatory; add a test asserting a command with `&&` does not chain"). Covered by Tasks 1–2; the anti-chaining test is `test_no_shell_chaining`.
- **Placeholder scan:** none — all code and tests are complete, commands are exact.
- **Type consistency:** `ExecResult(exit_code, stdout, stderr)` and `Executor.execute(argv, *, cwd, timeout)` match the overview's locked `executor/base.py` exactly. `LocalExecutor` satisfies the `Executor` Protocol structurally.

## Next

After Phase 2 is green, request the **Phase 3 (tickets)** plan: `TicketProvider` Protocol + `LocalTicketProvider` (SQLite-backed approve/deny), which combines with Phase 1's `Store` and this executor in Phase 4's MCP server.
