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
