"""AgentAdapter contract. An adapter launches/resumes an agent and reports outcome."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class RunContext:
    run_id: str
    task: str
    allowlist_text: str
    resume: bool
    session_id: str | None
    message: str | None


@dataclass(frozen=True)
class RunOutcome:
    session_id: str | None
    exit_code: int
    raw_output: str


@dataclass(frozen=True)
class LaunchSpec:
    cmd: list[str]
    env: dict[str, str]
    mcp_config: dict = field(default_factory=dict)


class AgentAdapter(Protocol):
    name: str
    async def run(self, ctx: RunContext) -> RunOutcome: ...
