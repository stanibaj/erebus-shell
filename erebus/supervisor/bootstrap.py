"""Build a wired FastAPI app from a YAML config file (used by `erebus serve`)."""
from __future__ import annotations

import os  # pragma: no cover

import yaml  # pragma: no cover

from erebus.agents.claude_code import ClaudeCodeAdapter  # pragma: no cover
from erebus.policy.models import load_policy_from_yaml  # pragma: no cover
from erebus.state.store import Store  # pragma: no cover
from erebus.supervisor.orchestrator import Orchestrator  # pragma: no cover
from erebus.supervisor.service import create_app  # pragma: no cover
from erebus.tickets.local import LocalTicketProvider  # pragma: no cover


def build_app_from_config(config_path: str):  # pragma: no cover
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)
    db_path = cfg.get("db_path", "data/erebus.db")
    tickets_db = cfg.get("tickets_db", "data/tickets.db")
    for path in (db_path, tickets_db):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    store = Store(db_path); store.init_schema()
    tickets = LocalTicketProvider(tickets_db); tickets.init_schema()
    policy = load_policy_from_yaml(config_path)
    allowlist_text = "\n".join(
        f"- {r.binary}" + (f" {r.args}" if r.args else "") for r in policy.allow
    )
    adapter = ClaudeCodeAdapter(policy_path=config_path, db_path=db_path,
                                tickets_db=tickets_db,
                                ttl_hours=float(cfg.get("approval_ttl_hours", 24)))
    orch = Orchestrator(store=store, tickets=tickets,
                        adapters={"claude_code": adapter}, allowlist_text=allowlist_text,
                        on_deny=cfg.get("on_deny", "resume"))
    return create_app(orchestrator=orch, tickets=tickets)
