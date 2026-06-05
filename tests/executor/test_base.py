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
