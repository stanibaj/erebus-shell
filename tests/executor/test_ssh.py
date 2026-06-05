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
