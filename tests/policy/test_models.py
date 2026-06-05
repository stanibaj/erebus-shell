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
