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
