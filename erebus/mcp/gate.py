"""CommandGate: the single chokepoint algorithm behind the run_command tool.

Orchestrates parse -> policy -> {execute | escalate} over injected dependencies.
On a blocked command it reads the STORE's pending-request status (the supervisor
reconciles ticket decisions into the store); it only ever *creates* tickets.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from erebus.executor.base import Executor, ExecResult, ExecutionTimeout
from erebus.policy.engine import DecisionType, PolicyEngine
from erebus.policy.parsing import CommandParseError, parse_command
from erebus.state.models import RequestStatus, RunStatus
from erebus.state.store import Store
from erebus.tickets.base import TicketProvider, TicketRequest

_DENIAL_MSG = (
    "This command was denied by a human. Do not retry it. "
    "Use an allowed alternative or stop."
)
_FALLBACK_JUSTIFICATION = "(no rationale provided; see run transcript)"


class CommandGate:
    def __init__(
        self,
        *,
        run_id: str,
        engine: PolicyEngine,
        executor: Executor,
        tickets: TicketProvider,
        store: Store,
        ttl_hours: float = 24.0,
    ) -> None:
        self._run_id = run_id
        self._engine = engine
        self._executor = executor
        self._tickets = tickets
        self._store = store
        self._ttl_hours = ttl_hours

    async def handle(self, command: str, reason: str = "") -> str:
        run_id = self._run_id

        # 1. Parse + reject shell operators (usage error, not an escalation).
        try:
            parsed = parse_command(command)
        except CommandParseError as exc:
            self._store.add_audit(run_id, "parse_error", command, None, str(exc))
            return f"Error: {exc}. Issue a single, valid command."

        if parsed.contains_operators:
            ops = ", ".join(parsed.operators_found)
            self._store.add_audit(run_id, "operator_blocked", command, "block", ops)
            return (
                f"Shell operators ({ops}) are not supported. Issue a single command, "
                "or if a pipeline is essential, explain why and request approval."
            )

        # 2. Policy decision.
        decision = self._engine.evaluate(parsed.argv)
        if decision.type is DecisionType.ALLOW:
            return await self._execute(parsed.argv, command, event="executed")

        # 3. Blocked — has this exact command already been routed for this run?
        existing = self._store.find_request(run_id, command)
        if existing is not None:
            if existing.status is RequestStatus.APPROVED:
                return await self._execute(
                    parsed.argv, command, event="executed_after_approval"
                )
            if existing.status in (RequestStatus.DENIED, RequestStatus.EXPIRED):
                return _DENIAL_MSG
            return self._pending_msg(existing.ticket_id)

        # 4. First attempt -> create ticket on first attempt.
        justification = reason.strip() or _FALLBACK_JUSTIFICATION
        ticket_id = await self._tickets.create(
            TicketRequest(run_id=run_id, command=command, justification=justification)
        )
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=self._ttl_hours)
        ).isoformat()
        self._store.create_pending_request(
            run_id=run_id, command=command, justification=justification,
            ticket_id=ticket_id, expires_at=expires_at,
        )
        self._store.set_run_status(run_id, RunStatus.PENDING_APPROVAL)
        self._store.add_audit(run_id, "escalated", command, "block", f"ticket {ticket_id}")
        return self._pending_msg(ticket_id)

    async def _execute(self, argv: list[str], command: str, *, event: str) -> str:
        try:
            result = await self._executor.execute(argv)
        except (FileNotFoundError, ExecutionTimeout, ValueError) as exc:
            self._store.add_audit(self._run_id, "execution_error", command, "allow", str(exc))
            return f"Error executing command: {exc}"
        self._store.add_audit(
            self._run_id, event, command, "allow", f"exit={result.exit_code}"
        )
        return self._format(result)

    def _pending_msg(self, ticket_id: str) -> str:
        return (
            "This command is not on the allowlist. A request for human approval has "
            f"been created (ticket {ticket_id}). This run will pause; it resumes "
            "automatically if approved. Stop now."
        )

    @staticmethod
    def _format(result: ExecResult) -> str:
        parts = [f"exit_code: {result.exit_code}"]
        if result.stdout:
            parts.append("stdout:\n" + result.stdout.rstrip("\n"))
        if result.stderr:
            parts.append("stderr:\n" + result.stderr.rstrip("\n"))
        return "\n".join(parts)
