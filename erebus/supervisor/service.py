"""FastAPI surface: start/observe runs and approve/deny tickets (local provider)."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from erebus.supervisor.orchestrator import Orchestrator
from erebus.tickets.local import LocalTicketProvider


class CreateRun(BaseModel):
    task: str
    agent: str = "claude_code"


class Decision(BaseModel):
    note: str | None = None


def create_app(*, orchestrator: Orchestrator, tickets: LocalTicketProvider) -> FastAPI:
    app = FastAPI(title="Erebus Shell")

    def _run_json(run_id: str) -> dict:
        run = orchestrator.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"run_id": run.id, "agent": run.agent, "task": run.task,
                "status": run.status.value, "session_id": run.session_id}

    @app.post("/runs")
    async def create_run(body: CreateRun):
        run_id = await orchestrator.start_run(body.task, body.agent)
        return _run_json(run_id)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        return _run_json(run_id)

    @app.get("/tickets/pending")
    def pending():
        return [{"id": t.id, "run_id": t.run_id, "command": t.command,
                 "justification": t.justification} for t in tickets.list_pending()]

    @app.post("/tickets/{ticket_id}/approve")
    async def approve(ticket_id: str, body: Decision):
        try:
            t = tickets.get(ticket_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="ticket not found")
        tickets.approve(ticket_id, note=body.note)
        await orchestrator.poll_and_resume(t.run_id)
        return _run_json(t.run_id)

    @app.post("/tickets/{ticket_id}/deny")
    async def deny(ticket_id: str, body: Decision):
        try:
            t = tickets.get(ticket_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="ticket not found")
        tickets.deny(ticket_id, note=body.note)
        await orchestrator.poll_and_resume(t.run_id)
        return _run_json(t.run_id)

    return app
