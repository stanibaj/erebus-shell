# Erebus Shell — Phase 1: Policy Engine + State Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Read `2026-06-05-erebus-shell-overview.md` first for the full architecture and locked interfaces.

**Goal:** Build the pure, dependency-free foundations of Erebus — command parsing with shell-operator detection, the YAML-validated allowlist policy model, the allow/block decision engine, and the SQLite-backed durable state store — all fully unit-tested.

**Architecture:** Three pure modules (`policy/parsing.py`, `policy/models.py`, `policy/engine.py`) with no I/O, plus one I/O module (`state/store.py`) that is the only thing touching SQLite. No network, no subprocess, no agent in this phase. Everything here is exercised directly by `pytest`.

**Tech Stack:** Python 3.11+, `pydantic` v2, `PyYAML`, stdlib `sqlite3`, `pytest`.

---

### Task 0: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `erebus/__init__.py`
- Create: `tests/__init__.py`
- Create: `config/erebus.example.yaml`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "erebus-shell"
version = "0.1.0"
description = "Gating shell that wraps AI agents with an allowlist + approval-ticket chokepoint"
requires-python = ">=3.11"
dependencies = [
    "pydantic>=2.6",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["erebus*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create empty package markers**

Create `erebus/__init__.py` with:

```python
"""Erebus Shell — gating chokepoint for AI agents."""
__version__ = "0.1.0"
```

Create `tests/__init__.py` as an empty file (no content).

- [ ] **Step 3: Create `config/erebus.example.yaml`** (reference policy; consumed in Task 2's tests as a fixture)

```yaml
# Erebus reference policy + service config.
# Allowlist rules are evaluated top-to-bottom; deny_binaries always wins.
agent: claude_code
ticket_provider: local
executor: local
approval_ttl_hours: 24
on_deny: resume            # resume | abort

policy:
  deny_binaries:
    - rm
    - dd
    - mkfs
  allow:
    - binary: git
      args:
        first_in: [status, log, diff, show, branch, fetch]
    - binary: ls
    - binary: cat
      args:
        all_match:
          - "^/var/log/.*"
```

- [ ] **Step 4: Install and verify the environment**

Run: `pip install -e ".[dev]"`
Expected: installs cleanly; `python -c "import erebus, pydantic, yaml, sqlite3; print('ok')"` prints `ok`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml erebus/__init__.py tests/__init__.py config/erebus.example.yaml
git commit -m "chore: project scaffold for erebus-shell"
```

---

### Task 1: Command parsing + shell-operator detection

**Files:**
- Create: `erebus/policy/__init__.py` (empty)
- Create: `erebus/policy/parsing.py`
- Test: `tests/policy/__init__.py` (empty), `tests/policy/test_parsing.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/policy/__init__.py` (empty file). Create `tests/policy/test_parsing.py`:

```python
import pytest
from erebus.policy.parsing import parse_command, ParsedCommand, CommandParseError


def test_simple_command_parses_to_argv():
    result = parse_command("git status")
    assert result.argv == ["git", "status"]
    assert result.contains_operators is False
    assert result.operators_found == []


def test_command_with_quoted_arg():
    result = parse_command('git commit -m "hello world"')
    assert result.argv == ["git", "commit", "-m", "hello world"]
    assert result.contains_operators is False


def test_detects_and_operator():
    result = parse_command("git status && rm -rf /")
    assert result.contains_operators is True
    assert "&&" in result.operators_found


def test_detects_pipe():
    result = parse_command("cat /var/log/syslog | grep error")
    assert result.contains_operators is True
    assert "|" in result.operators_found


def test_detects_semicolon():
    result = parse_command("ls; whoami")
    assert result.contains_operators is True
    assert ";" in result.operators_found


def test_detects_command_substitution():
    result = parse_command("echo $(whoami)")
    assert result.contains_operators is True
    assert "$(" in result.operators_found


def test_detects_backtick():
    result = parse_command("echo `whoami`")
    assert result.contains_operators is True
    assert "`" in result.operators_found


def test_detects_redirect():
    result = parse_command("echo hi > /etc/passwd")
    assert result.contains_operators is True
    assert ">" in result.operators_found


def test_empty_command_raises():
    with pytest.raises(CommandParseError):
        parse_command("   ")


def test_unbalanced_quote_raises():
    with pytest.raises(CommandParseError):
        parse_command('git commit -m "unterminated')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/policy/test_parsing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erebus.policy.parsing'`.

- [ ] **Step 3: Write the implementation**

Create `erebus/policy/__init__.py` (empty file). Create `erebus/policy/parsing.py`:

```python
"""Parse a command string into argv and flag shell metacharacters.

The executor runs argv with no shell, so operators here can never be
*interpreted* — but we detect them to give the agent a clear error and to
keep the allowlist meaningful. Detection is intentionally conservative
(a raw-string scan): a false positive only forces the rare shell-mode
escalation path, which is the safe failure direction.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

# Multi-char operators must be checked before single-char ones.
_OPERATOR_PATTERNS: list[str] = ["&&", "||", ">>", "$(", ";", "|", "&", ">", "<", "`"]


class CommandParseError(Exception):
    """Raised when a command string cannot be tokenized or is empty."""


@dataclass(frozen=True)
class ParsedCommand:
    argv: list[str]
    contains_operators: bool
    operators_found: list[str]


def _scan_operators(command: str) -> list[str]:
    """Scan the raw string for shell operators, ignoring matches inside
    single- or double-quoted spans so legitimately-quoted args don't trip it."""
    # Strip quoted spans to a placeholder, then scan what remains.
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", " ", command)
    found: list[str] = []
    for op in _OPERATOR_PATTERNS:
        if op in unquoted and op not in found:
            found.append(op)
    return found


def parse_command(command: str) -> ParsedCommand:
    stripped = command.strip()
    if not stripped:
        raise CommandParseError("empty command")
    try:
        argv = shlex.split(stripped)
    except ValueError as exc:
        raise CommandParseError(f"could not tokenize command: {exc}") from exc
    if not argv:
        raise CommandParseError("empty command")
    operators = _scan_operators(stripped)
    return ParsedCommand(
        argv=argv,
        contains_operators=bool(operators),
        operators_found=operators,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/policy/test_parsing.py -v`
Expected: PASS (all 10 tests).

> Note: `test_command_with_quoted_arg` exercises the quote-stripping so an inner space does not register as an operator; `&&`/`|`/`;`/`$(`/`` ` ``/`>` cases confirm detection.

- [ ] **Step 5: Commit**

```bash
git add erebus/policy/__init__.py erebus/policy/parsing.py tests/policy/__init__.py tests/policy/test_parsing.py
git commit -m "feat(policy): command parsing with shell-operator detection"
```

---

### Task 2: Policy model + YAML loader

**Files:**
- Create: `erebus/policy/models.py`
- Test: `tests/policy/test_models.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/policy/test_models.py`:

```python
import pytest
from pydantic import ValidationError
from erebus.policy.models import Policy, AllowRule, ArgConstraint, load_policy_from_yaml


def test_minimal_policy():
    p = Policy(allow=[AllowRule(binary="ls")])
    assert p.allow[0].binary == "ls"
    assert p.allow[0].args is None
    assert p.deny_binaries == []


def test_arg_constraint_fields():
    rule = AllowRule(
        binary="git",
        args=ArgConstraint(first_in=["status", "log"]),
    )
    assert rule.args.first_in == ["status", "log"]
    assert rule.args.all_match is None
    assert rule.args.max_args is None


def test_binary_must_be_nonempty():
    with pytest.raises(ValidationError):
        AllowRule(binary="")


def test_load_policy_from_yaml(tmp_path):
    yaml_text = """
deny_binaries:
  - rm
allow:
  - binary: git
    args:
      first_in: [status, log]
  - binary: ls
"""
    f = tmp_path / "policy.yaml"
    f.write_text(yaml_text)
    p = load_policy_from_yaml(str(f))
    assert p.deny_binaries == ["rm"]
    assert len(p.allow) == 2
    assert p.allow[0].binary == "git"
    assert p.allow[0].args.first_in == ["status", "log"]
    assert p.allow[1].args is None


def test_load_policy_rejects_unknown_field(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("allow:\n  - binary: ls\n    bogus: 1\n")
    with pytest.raises(ValidationError):
        load_policy_from_yaml(str(f))


def test_load_example_config_policy_section():
    # config/erebus.example.yaml is the committed reference; its policy must validate.
    import yaml
    with open("config/erebus.example.yaml") as fh:
        data = yaml.safe_load(fh)
    p = Policy.model_validate(data["policy"])
    assert "rm" in p.deny_binaries
    assert any(r.binary == "cat" for r in p.allow)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/policy/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erebus.policy.models'`.

- [ ] **Step 3: Write the implementation**

Create `erebus/policy/models.py`:

```python
"""Pydantic models for the allowlist policy, plus a YAML loader."""
from __future__ import annotations

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ArgConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_in: list[str] | None = None      # argv[1] must be one of these
    all_match: list[str] | None = None     # every arg in argv[1:] must match one regex here
    max_args: int | None = None            # max length of argv[1:]


class AllowRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binary: str = Field(min_length=1)      # matched against basename(argv[0])
    args: ArgConstraint | None = None      # None => any args allowed


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow: list[AllowRule] = Field(default_factory=list)
    deny_binaries: list[str] = Field(default_factory=list)


def load_policy_from_yaml(path: str) -> Policy:
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    # Accept either a bare policy doc or a config doc with a `policy:` key.
    if "policy" in data and "allow" not in data and "deny_binaries" not in data:
        data = data["policy"]
    return Policy.model_validate(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/policy/test_models.py -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Commit**

```bash
git add erebus/policy/models.py tests/policy/test_models.py
git commit -m "feat(policy): YAML-validated allowlist policy model"
```

---

### Task 3: Policy decision engine

**Files:**
- Create: `erebus/policy/engine.py`
- Test: `tests/policy/test_engine.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/policy/test_engine.py`:

```python
from erebus.policy.engine import PolicyEngine, Decision, DecisionType
from erebus.policy.models import Policy, AllowRule, ArgConstraint


def _engine(**kwargs) -> PolicyEngine:
    return PolicyEngine(Policy(**kwargs))


def test_empty_argv_blocks():
    d = _engine(allow=[AllowRule(binary="ls")]).evaluate([])
    assert d.type is DecisionType.BLOCK


def test_allowed_binary_no_constraint():
    d = _engine(allow=[AllowRule(binary="ls")]).evaluate(["ls", "-la", "/tmp"])
    assert d.type is DecisionType.ALLOW
    assert d.matched_rule == "ls"


def test_not_on_allowlist_blocks():
    d = _engine(allow=[AllowRule(binary="ls")]).evaluate(["whoami"])
    assert d.type is DecisionType.BLOCK
    assert "not on the allowlist" in d.reason


def test_deny_binary_wins_over_allow():
    d = _engine(
        allow=[AllowRule(binary="rm")],
        deny_binaries=["rm"],
    ).evaluate(["rm", "-rf", "/tmp/x"])
    assert d.type is DecisionType.BLOCK
    assert "denied" in d.reason


def test_basename_match_for_absolute_path():
    d = _engine(allow=[AllowRule(binary="ls")]).evaluate(["/bin/ls", "-l"])
    assert d.type is DecisionType.ALLOW


def test_first_in_constraint_allows_match():
    rule = AllowRule(binary="git", args=ArgConstraint(first_in=["status", "log"]))
    d = _engine(allow=[rule]).evaluate(["git", "status"])
    assert d.type is DecisionType.ALLOW


def test_first_in_constraint_blocks_nonmatch():
    rule = AllowRule(binary="git", args=ArgConstraint(first_in=["status", "log"]))
    d = _engine(allow=[rule]).evaluate(["git", "push"])
    assert d.type is DecisionType.BLOCK


def test_all_match_constraint():
    rule = AllowRule(binary="cat", args=ArgConstraint(all_match=[r"^/var/log/.*"]))
    eng = _engine(allow=[rule])
    assert eng.evaluate(["cat", "/var/log/syslog"]).type is DecisionType.ALLOW
    assert eng.evaluate(["cat", "/etc/passwd"]).type is DecisionType.BLOCK


def test_max_args_constraint():
    rule = AllowRule(binary="ls", args=ArgConstraint(max_args=1))
    eng = _engine(allow=[rule])
    assert eng.evaluate(["ls", "/tmp"]).type is DecisionType.ALLOW
    assert eng.evaluate(["ls", "/tmp", "/var"]).type is DecisionType.BLOCK


def test_multiple_rules_same_binary_second_matches():
    # First rule constrains to `status`; second allows `log`. `git log` should pass via the second.
    eng = _engine(allow=[
        AllowRule(binary="git", args=ArgConstraint(first_in=["status"])),
        AllowRule(binary="git", args=ArgConstraint(first_in=["log"])),
    ])
    assert eng.evaluate(["git", "log"]).type is DecisionType.ALLOW
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/policy/test_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erebus.policy.engine'`.

- [ ] **Step 3: Write the implementation**

Create `erebus/policy/engine.py`:

```python
"""Decide ALLOW/BLOCK for a parsed argv against a Policy. Pure, no I/O."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum

from erebus.policy.models import ArgConstraint, Policy


class DecisionType(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True)
class Decision:
    type: DecisionType
    reason: str
    matched_rule: str | None = None


class PolicyEngine:
    def __init__(self, policy: Policy) -> None:
        self._policy = policy

    def evaluate(self, argv: list[str]) -> Decision:
        if not argv:
            return Decision(DecisionType.BLOCK, "empty command")

        binary = os.path.basename(argv[0])
        args = argv[1:]

        if binary in self._policy.deny_binaries:
            return Decision(DecisionType.BLOCK, f"'{binary}' is explicitly denied")

        for rule in self._policy.allow:
            if rule.binary != binary:
                continue
            if self._args_ok(rule.args, args):
                return Decision(DecisionType.ALLOW, "matched allow rule", rule.binary)
            # binary matched but args failed; keep looking for another rule.

        return Decision(DecisionType.BLOCK, f"'{binary}' is not on the allowlist")

    @staticmethod
    def _args_ok(constraint: ArgConstraint | None, args: list[str]) -> bool:
        if constraint is None:
            return True
        if constraint.max_args is not None and len(args) > constraint.max_args:
            return False
        if constraint.first_in is not None:
            if not args or args[0] not in constraint.first_in:
                return False
        if constraint.all_match is not None:
            patterns = [re.compile(p) for p in constraint.all_match]
            for a in args:
                if not any(p.search(a) for p in patterns):
                    return False
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/policy/test_engine.py -v`
Expected: PASS (all 10 tests).

- [ ] **Step 5: Commit**

```bash
git add erebus/policy/engine.py tests/policy/test_engine.py
git commit -m "feat(policy): allow/block decision engine"
```

---

### Task 4: State models + SQLite store

**Files:**
- Create: `erebus/state/__init__.py` (empty)
- Create: `erebus/state/models.py`
- Create: `erebus/state/store.py`
- Test: `tests/state/__init__.py` (empty), `tests/state/test_store.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/state/__init__.py` (empty file). Create `tests/state/test_store.py`:

```python
import pytest
from erebus.state.store import Store
from erebus.state.models import RunStatus, RequestStatus


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "erebus.db"))
    s.init_schema()
    return s


def test_create_and_get_run(store):
    run_id = store.create_run(agent="claude_code", task="check disk")
    run = store.get_run(run_id)
    assert run is not None
    assert run.id == run_id
    assert run.agent == "claude_code"
    assert run.task == "check disk"
    assert run.status is RunStatus.RUNNING
    assert run.session_id is None


def test_get_missing_run_returns_none(store):
    assert store.get_run("nope") is None


def test_set_run_status(store):
    run_id = store.create_run(agent="claude_code", task="t")
    store.set_run_status(run_id, RunStatus.COMPLETED)
    assert store.get_run(run_id).status is RunStatus.COMPLETED


def test_set_run_session(store):
    run_id = store.create_run(agent="claude_code", task="t")
    store.set_run_session(run_id, "sess-abc")
    assert store.get_run(run_id).session_id == "sess-abc"


def test_create_and_find_pending_request(store):
    run_id = store.create_run(agent="claude_code", task="t")
    req_id = store.create_pending_request(
        run_id=run_id, command="systemctl restart nginx",
        justification="service is down", ticket_id="T-1",
        expires_at="2026-06-06T00:00:00",
    )
    found = store.find_request(run_id, "systemctl restart nginx")
    assert found is not None
    assert found.id == req_id
    assert found.status is RequestStatus.PENDING
    assert found.ticket_id == "T-1"


def test_find_request_no_match_returns_none(store):
    run_id = store.create_run(agent="claude_code", task="t")
    assert store.find_request(run_id, "anything") is None


def test_set_request_status(store):
    run_id = store.create_run(agent="claude_code", task="t")
    req_id = store.create_pending_request(
        run_id=run_id, command="c", justification="j",
        ticket_id="T-2", expires_at="2026-06-06T00:00:00",
    )
    store.set_request_status(req_id, RequestStatus.APPROVED)
    assert store.find_request(run_id, "c").status is RequestStatus.APPROVED


def test_list_pending_requests_only_returns_pending(store):
    run_id = store.create_run(agent="claude_code", task="t")
    p = store.create_pending_request(run_id=run_id, command="a", justification="j",
                                     ticket_id="T-3", expires_at="2026-06-06T00:00:00")
    store.create_pending_request(run_id=run_id, command="b", justification="j",
                                 ticket_id="T-4", expires_at="2026-06-06T00:00:00")
    store.set_request_status(p, RequestStatus.APPROVED)
    pending = store.list_pending_requests()
    assert {r.command for r in pending} == {"b"}


def test_audit_log_append(store):
    run_id = store.create_run(agent="claude_code", task="t")
    store.add_audit(run_id, event="executed", command="ls", decision="allow", detail=None)
    store.add_audit(run_id, event="escalated", command="rm x", decision="block", detail="ticket T-5")
    entries = store.list_audit(run_id)
    assert len(entries) == 2
    assert entries[0].event == "executed"
    assert entries[1].detail == "ticket T-5"


def test_state_survives_reopen(tmp_path):
    db = str(tmp_path / "erebus.db")
    s1 = Store(db)
    s1.init_schema()
    run_id = s1.create_run(agent="claude_code", task="persist me")
    # New Store instance on the same file (simulates a service restart).
    s2 = Store(db)
    assert s2.get_run(run_id).task == "persist me"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/state/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erebus.state.store'`.

- [ ] **Step 3: Write the models**

Create `erebus/state/__init__.py` (empty file). Create `erebus/state/models.py`:

```python
"""State enums and row dataclasses for the SQLite store."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RunStatus(str, Enum):
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    EXPIRED = "expired"


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Run:
    id: str
    agent: str
    task: str
    status: RunStatus
    session_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PendingRequest:
    id: str
    run_id: str
    command: str
    justification: str
    status: RequestStatus
    ticket_id: str
    created_at: str
    expires_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class AuditEntry:
    id: int
    run_id: str
    ts: str
    event: str
    command: str | None
    decision: str | None
    detail: str | None
```

- [ ] **Step 4: Write the store**

Create `erebus/state/store.py`:

```python
"""SQLite-backed durable state. The only module that touches the database."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from erebus.state.models import (
    AuditEntry,
    PendingRequest,
    RequestStatus,
    Run,
    RunStatus,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")

    def init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT NOT NULL,
                session_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_requests (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                command TEXT NOT NULL,
                justification TEXT NOT NULL,
                status TEXT NOT NULL,
                ticket_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                event TEXT NOT NULL,
                command TEXT,
                decision TEXT,
                detail TEXT
            );
            """
        )
        self._conn.commit()

    # ---- runs ---------------------------------------------------------
    def create_run(self, agent: str, task: str) -> str:
        run_id = _new_id()
        ts = _now()
        self._conn.execute(
            "INSERT INTO runs (id, agent, task, status, session_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (run_id, agent, task, RunStatus.RUNNING.value, ts, ts),
        )
        self._conn.commit()
        return run_id

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return Run(
            id=row["id"], agent=row["agent"], task=row["task"],
            status=RunStatus(row["status"]), session_id=row["session_id"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def set_run_status(self, run_id: str, status: RunStatus) -> None:
        self._conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, _now(), run_id),
        )
        self._conn.commit()

    def set_run_session(self, run_id: str, session_id: str) -> None:
        self._conn.execute(
            "UPDATE runs SET session_id = ?, updated_at = ? WHERE id = ?",
            (session_id, _now(), run_id),
        )
        self._conn.commit()

    # ---- pending requests --------------------------------------------
    def create_pending_request(self, run_id: str, command: str, justification: str,
                               ticket_id: str, expires_at: str) -> str:
        req_id = _new_id()
        self._conn.execute(
            "INSERT INTO pending_requests "
            "(id, run_id, command, justification, status, ticket_id, created_at, expires_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (req_id, run_id, command, justification, RequestStatus.PENDING.value,
             ticket_id, _now(), expires_at),
        )
        self._conn.commit()
        return req_id

    def _row_to_request(self, row: sqlite3.Row) -> PendingRequest:
        return PendingRequest(
            id=row["id"], run_id=row["run_id"], command=row["command"],
            justification=row["justification"], status=RequestStatus(row["status"]),
            ticket_id=row["ticket_id"], created_at=row["created_at"],
            expires_at=row["expires_at"], resolved_at=row["resolved_at"],
        )

    def find_request(self, run_id: str, command: str) -> PendingRequest | None:
        row = self._conn.execute(
            "SELECT * FROM pending_requests WHERE run_id = ? AND command = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (run_id, command),
        ).fetchone()
        return self._row_to_request(row) if row else None

    def set_request_status(self, request_id: str, status: RequestStatus) -> None:
        resolved = None if status is RequestStatus.PENDING else _now()
        self._conn.execute(
            "UPDATE pending_requests SET status = ?, resolved_at = ? WHERE id = ?",
            (status.value, resolved, request_id),
        )
        self._conn.commit()

    def list_pending_requests(self) -> list[PendingRequest]:
        rows = self._conn.execute(
            "SELECT * FROM pending_requests WHERE status = ? ORDER BY created_at",
            (RequestStatus.PENDING.value,),
        ).fetchall()
        return [self._row_to_request(r) for r in rows]

    # ---- audit --------------------------------------------------------
    def add_audit(self, run_id: str, event: str, command: str | None,
                  decision: str | None, detail: str | None) -> None:
        self._conn.execute(
            "INSERT INTO audit_log (run_id, ts, event, command, decision, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, _now(), event, command, decision, detail),
        )
        self._conn.commit()

    def list_audit(self, run_id: str) -> list[AuditEntry]:
        rows = self._conn.execute(
            "SELECT * FROM audit_log WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [
            AuditEntry(
                id=r["id"], run_id=r["run_id"], ts=r["ts"], event=r["event"],
                command=r["command"], decision=r["decision"], detail=r["detail"],
            )
            for r in rows
        ]
```

> **Note on reopen test:** `Store.__init__` connects but does not create tables; `init_schema()` uses `CREATE TABLE IF NOT EXISTS`, so a second `Store` on the same file (the restart test) reads existing rows without re-init. WAL mode is enabled for concurrent reader/writer access (supervisor + MCP server share this file).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/state/test_store.py -v`
Expected: PASS (all 10 tests).

- [ ] **Step 6: Commit**

```bash
git add erebus/state/__init__.py erebus/state/models.py erebus/state/store.py tests/state/__init__.py tests/state/test_store.py
git commit -m "feat(state): SQLite-backed durable store for runs, requests, audit"
```

---

### Task 5: Phase 1 gate — full test run

**Files:** none (verification only)

- [ ] **Step 1: Run the entire suite**

Run: `pytest -v`
Expected: PASS — all tests from Tasks 1–4 green (parsing 10, models 6, engine 10, store 10).

- [ ] **Step 2: Confirm no shell execution anywhere**

Run: `grep -rn "shell=True\|os.system\|sh -c" erebus/ ; echo "exit: $?"`
Expected: no matches (grep prints nothing; the security invariant from the overview's hardening checklist holds for Phase 1 — the executor that *could* violate it arrives in Phase 2 and the no-shell test is mandatory there).

- [ ] **Step 3: Commit any final tidy-ups (if needed)**

```bash
git add -A
git commit -m "test: phase 1 green — policy + state foundations" --allow-empty
```

---

## Self-Review (performed against the spec)

- **Spec coverage:** Phase 1's scope = parsing + operator detection (Decision #7), allowlist model + YAML (Decisions #14, #7), decision engine with deny-wins (Decision #7 / hardening), durable SQLite state that survives restart (Decisions #13, #4). All covered by Tasks 1–4. Execution, tickets, MCP, supervisor, adapters are explicitly later phases.
- **Placeholder scan:** No TBD/TODO; every code step contains complete code; every test step contains full test bodies; every run step has an exact command + expected result.
- **Type consistency:** `Decision`/`DecisionType` (engine) match the overview's locked interface; `Run`/`PendingRequest`/`AuditEntry`/`RunStatus`/`RequestStatus` match the overview's `state/models.py`; `Store` method signatures match the overview's `Store` excerpt (`create_run`, `get_run`, `set_run_status`, `set_run_session`, `create_pending_request`, `find_request`, `set_request_status`, `list_pending_requests`, `add_audit`) plus `list_audit` used by the audit test. `ArgConstraint` fields (`first_in`, `all_match`, `max_args`) are consistent between `models.py`, the engine, and the example YAML.

---

## Next

After Phase 1 is green, request the **Phase 2 (executor)** plan. It builds `LocalExecutor` on top of nothing from this phase except confirming the no-shell invariant, and adds the mandatory "`&&` does not chain" security test.
