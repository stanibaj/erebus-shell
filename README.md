# Erebus Shell

A general-purpose **gating shell** that wraps an unmodified AI coding agent
(Claude Code or OpenCode) and forces every command through a single
allowlist-checked chokepoint. When the agent genuinely needs a not-allowed
command, Erebus creates an **approval ticket**, pauses the run, and resumes the
same agent session once a human approves.

```
POST /runs ─▶ supervisor launches agent ─▶ agent's only tool is run_command (MCP)
          ─▶ allowlisted?  execute (no shell)  :  create ticket + pause
          ─▶ human approves (CLI/HTTP) ─▶ resume same session ─▶ execute ─▶ continue
```

## Design

- **Wrap, don't replace.** The agent is unmodified; its native shell is denied and
  its only execution tool is the erebus `run_command` MCP tool.
- **Single chokepoint.** A gating MCP server holds credentials, owns the allowlist,
  and is the sole executor — locally or over SSH.
- **No-shell execution.** Commands run as `argv` with no shell, so `&&`/`|`/`$()`
  cannot chain or expand. Remote SSH execution quotes every argument.
- **Async approval.** A blocked command creates a ticket and ends the run cleanly;
  the supervisor polls and resumes the same session on approval.
- **Pluggable.** Agent (Claude Code, OpenCode), ticket provider (local, Zoho), and
  executor (local, SSH) are all swappable. Triggers are external projects that call
  `POST /runs`.

Full design and per-phase plans live in [`docs/superpowers/plans/`](docs/superpowers/plans/).

## Requirements

Everything runs in Docker (the virtualenv lives inside the image).

```bash
docker compose build               # build the image
docker compose run --rm test       # run the test suite (pytest)
docker compose up app              # start the HTTP service on :8080
```

## CLI

The `erebus` CLI is a thin client of the HTTP service:

```bash
erebus run --task "check why nginx is down" --agent claude_code
erebus pending                     # list tickets awaiting approval
erebus approve T-abc123 --note ok  # approve -> the paused run resumes
erebus deny T-abc123
erebus serve --config config/erebus.example.yaml
```

## HTTP API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/runs` | start a run (`{"task": "...", "agent": "claude_code"}`) |
| `GET`  | `/runs/{id}` | run status |
| `GET`  | `/tickets/pending` | tickets awaiting a human decision |
| `POST` | `/tickets/{id}/approve` | approve → resumes the paused run |
| `POST` | `/tickets/{id}/deny` | deny → resume with a denial (or abort) |

## Policy

The allowlist is YAML (see [`config/erebus.example.yaml`](config/erebus.example.yaml)).
Rules match per-binary with optional argument constraints; `deny_binaries` always wins.

```yaml
policy:
  deny_binaries: [rm, dd, mkfs]
  allow:
    - binary: git
      args: { first_in: [status, log, diff] }
    - binary: ls
    - binary: cat
      args: { all_match: ["^/var/log/.*"] }
```

## Configuration (env)

Credentials live in the service/executor process — never in the agent context.

| Var | Purpose |
|-----|---------|
| `EREBUS_EXECUTOR` | `local` (default) or `ssh` |
| `EREBUS_SSH_HOST` / `EREBUS_SSH_USER` / `EREBUS_SSH_KEY` | remote execution target |
| `EREBUS_DB_PATH` / `EREBUS_TICKETS_DB` / `EREBUS_POLICY_PATH` | per-run state + policy |

## Status

All 8 build phases are implemented and tested (110 tests). The real-agent
subprocess paths (`ClaudeCodeAdapter.run` / `OpenCodeAdapter.run`) and the live
Zoho/SSH integrations are covered for their pure logic in CI and validated
manually with real credentials.
