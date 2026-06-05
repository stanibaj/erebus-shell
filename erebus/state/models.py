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
