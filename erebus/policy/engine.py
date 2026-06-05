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
