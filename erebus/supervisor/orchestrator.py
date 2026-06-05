"""Run lifecycle state machine: launch -> pending -> poll -> resume/deny/expire."""
from __future__ import annotations

from datetime import datetime, timezone

from erebus.agents.base import AgentAdapter, RunContext, RunOutcome
from erebus.state.models import PendingRequest, RequestStatus, RunStatus
from erebus.state.store import Store
from erebus.tickets.base import TicketProvider

_APPROVE_MSG = "The request was approved; the command will now succeed. Continue."
_DENY_MSG = "The request was denied. Do not retry; use an allowed alternative or stop."


class Orchestrator:
    def __init__(self, *, store: Store, tickets: TicketProvider,
                 adapters: dict[str, AgentAdapter], allowlist_text: str,
                 on_deny: str = "resume") -> None:
        self.store = store
        self.tickets = tickets
        self.adapters = adapters
        self.allowlist_text = allowlist_text
        self.on_deny = on_deny

    async def start_run(self, task: str, agent: str) -> str:
        run_id = self.store.create_run(agent=agent, task=task)
        ctx = RunContext(run_id=run_id, task=task, allowlist_text=self.allowlist_text,
                         resume=False, session_id=None, message=None)
        outcome = await self.adapters[agent].run(ctx)
        self._post_run(run_id, outcome)
        return run_id

    async def poll_and_resume(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        if run is None or run.status is not RunStatus.PENDING_APPROVAL:
            return run.status.value if run else "unknown"
        pending = self._pending_for_run(run_id)
        if pending is None:
            return run.status.value

        if datetime.now(timezone.utc) > datetime.fromisoformat(pending.expires_at):
            self.store.set_request_status(pending.id, RequestStatus.EXPIRED)
            self.store.set_run_status(run_id, RunStatus.EXPIRED)
            return RunStatus.EXPIRED.value

        status = await self.tickets.poll(pending.ticket_id)
        if status.decision is RequestStatus.PENDING:
            return RunStatus.PENDING_APPROVAL.value
        if status.decision is RequestStatus.APPROVED:
            self.store.set_request_status(pending.id, RequestStatus.APPROVED)
            self.store.set_run_status(run_id, RunStatus.RUNNING)
            await self._resume(run_id, _APPROVE_MSG)
        else:  # DENIED
            self.store.set_request_status(pending.id, RequestStatus.DENIED)
            if self.on_deny == "abort":
                self.store.set_run_status(run_id, RunStatus.DENIED)
                return RunStatus.DENIED.value
            self.store.set_run_status(run_id, RunStatus.RUNNING)
            await self._resume(run_id, _DENY_MSG)
        return self.store.get_run(run_id).status.value

    async def _resume(self, run_id: str, message: str) -> None:
        run = self.store.get_run(run_id)
        ctx = RunContext(run_id=run_id, task=run.task, allowlist_text=self.allowlist_text,
                         resume=True, session_id=run.session_id, message=message)
        outcome = await self.adapters[run.agent].run(ctx)
        self._post_run(run_id, outcome)

    def _post_run(self, run_id: str, outcome: RunOutcome) -> None:
        if outcome.session_id:
            self.store.set_run_session(run_id, outcome.session_id)
        if self._pending_for_run(run_id) is not None:
            self.store.set_run_status(run_id, RunStatus.PENDING_APPROVAL)
        elif outcome.exit_code == 0:
            self.store.set_run_status(run_id, RunStatus.COMPLETED)
        else:
            self.store.set_run_status(run_id, RunStatus.FAILED)

    def _pending_for_run(self, run_id: str) -> PendingRequest | None:
        for r in self.store.list_pending_requests():
            if r.run_id == run_id:
                return r
        return None
