import json
from erebus.agents.claude_code import ClaudeCodeAdapter
from erebus.agents.base import RunContext


def _ctx(resume=False, session_id=None):
    return RunContext(run_id="run-1", task="check disk", allowlist_text="echo, ls",
                      resume=resume, session_id=session_id, message="continue")


def test_render_launch_fresh_run(tmp_path):
    a = ClaudeCodeAdapter(policy_path="/cfg/policy.yaml", db_path="/d/s.db",
                          tickets_db="/d/t.db", max_turns=12)
    spec = a.render_launch(_ctx())
    assert spec.cmd[0] == "claude"
    assert "-p" in spec.cmd
    assert "check disk" in spec.cmd
    assert "--output-format" in spec.cmd and "json" in spec.cmd
    # native shell denied; only the erebus MCP tool allowed
    assert "Bash" in spec.cmd[spec.cmd.index("--disallowedTools") + 1]
    assert "mcp__erebus__run_command" in spec.cmd[spec.cmd.index("--allowedTools") + 1]
    assert "--resume" not in spec.cmd
    # env carries the per-run identity for the spawned MCP server
    assert spec.env["EREBUS_RUN_ID"] == "run-1"
    assert spec.env["EREBUS_DB_PATH"] == "/d/s.db"
    assert spec.env["EREBUS_POLICY_PATH"] == "/cfg/policy.yaml"


def test_render_launch_resume_includes_session(tmp_path):
    a = ClaudeCodeAdapter(policy_path="/cfg/policy.yaml", db_path="/d/s.db",
                          tickets_db="/d/t.db")
    spec = a.render_launch(_ctx(resume=True, session_id="sess-9"))
    assert "--resume" in spec.cmd
    assert "sess-9" in spec.cmd


def test_parse_outcome_extracts_session_and_result():
    a = ClaudeCodeAdapter(policy_path="p", db_path="d", tickets_db="t")
    raw = json.dumps({"session_id": "abc123", "result": "done", "is_error": False})
    out = a.parse_outcome(raw)
    assert out.session_id == "abc123"
    assert out.exit_code == 0
    assert "done" in out.raw_output


def test_parse_outcome_error_sets_nonzero_exit():
    a = ClaudeCodeAdapter(policy_path="p", db_path="d", tickets_db="t")
    raw = json.dumps({"session_id": "x", "result": "boom", "is_error": True})
    out = a.parse_outcome(raw)
    assert out.exit_code != 0


def test_parse_outcome_handles_non_json():
    a = ClaudeCodeAdapter(policy_path="p", db_path="d", tickets_db="t")
    out = a.parse_outcome("not json at all")
    assert out.session_id is None
    assert out.exit_code != 0
