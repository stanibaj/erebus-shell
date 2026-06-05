"""Thin Erebus CLI. `run/pending/approve/deny` are HTTP clients of the service;
`serve` boots the FastAPI app from a YAML config.
"""
from __future__ import annotations

import argparse
import json
import os

import httpx


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="erebus")
    p.add_argument("--url", default=os.environ.get("EREBUS_URL", "http://localhost:8080"))
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a run")
    run.add_argument("--task", required=True)
    run.add_argument("--agent", default="claude_code")

    sub.add_parser("pending", help="list pending tickets")

    ap = sub.add_parser("approve", help="approve a ticket")
    ap.add_argument("ticket_id")
    ap.add_argument("--note", default=None)

    dn = sub.add_parser("deny", help="deny a ticket")
    dn.add_argument("ticket_id")
    dn.add_argument("--note", default=None)

    sv = sub.add_parser("serve", help="run the HTTP service")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--config", default=os.environ.get("EREBUS_CONFIG", "config/erebus.example.yaml"))
    return p


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin glue
    ns = build_parser().parse_args(argv)
    if ns.command == "serve":
        import uvicorn
        from erebus.supervisor.bootstrap import build_app_from_config
        uvicorn.run(build_app_from_config(ns.config), host=ns.host, port=ns.port)
        return 0
    if ns.command == "run":
        r = httpx.post(f"{ns.url}/runs", json={"task": ns.task, "agent": ns.agent})
    elif ns.command == "pending":
        r = httpx.get(f"{ns.url}/tickets/pending")
    elif ns.command == "approve":
        r = httpx.post(f"{ns.url}/tickets/{ns.ticket_id}/approve", json={"note": ns.note})
    elif ns.command == "deny":
        r = httpx.post(f"{ns.url}/tickets/{ns.ticket_id}/deny", json={"note": ns.note})
    else:  # unreachable
        return 2
    print(json.dumps(r.json(), indent=2))
    return 0
