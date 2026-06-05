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
