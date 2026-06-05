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
