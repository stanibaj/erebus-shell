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
