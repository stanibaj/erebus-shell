import json
from erebus.agents.opencode import OpenCodeAdapter
from erebus.agents.base import RunContext


def _ctx(resume=False, session_id=None):
    return RunContext(run_id="run-9", task="inspect logs", allowlist_text="cat, ls",
                      resume=resume, session_id=session_id, message="continue")


def _adapter():
    return OpenCodeAdapter(policy_path="/cfg/p.yaml", db_path="/d/s.db", tickets_db="/d/t.db")


def test_render_launch_fresh_run():
    spec = _adapter().render_launch(_ctx())
    assert spec.cmd[0] == "opencode"
    assert "run" in spec.cmd
    assert "inspect logs" in spec.cmd
    assert "--session" not in spec.cmd
    # OpenCode config denies native bash and registers the erebus MCP server.
    assert spec.mcp_config["permission"]["bash"] == "deny"
    assert "erebus" in spec.mcp_config["mcp"]
    # allowlist is injected into the agent's instructions
    assert any("cat, ls" in line for line in spec.mcp_config["instructions"])
    assert spec.env["EREBUS_RUN_ID"] == "run-9"
    assert spec.env["EREBUS_POLICY_PATH"] == "/cfg/p.yaml"


def test_render_launch_resume_uses_session_and_message():
    spec = _adapter().render_launch(_ctx(resume=True, session_id="sess-77"))
    assert "--session" in spec.cmd
    assert "sess-77" in spec.cmd
    assert "continue" in spec.cmd          # message, not the original task


def test_mcp_server_command_is_erebus():
    spec = _adapter().render_launch(_ctx())
    server = spec.mcp_config["mcp"]["erebus"]
    assert server["type"] == "local"
    assert server["command"] == ["python", "-m", "erebus.mcp.server"]
    assert server["environment"]["EREBUS_RUN_ID"] == "run-9"


def test_parse_outcome_snake_case_session():
    out = OpenCodeAdapter(policy_path="p", db_path="d", tickets_db="t").parse_outcome(
        json.dumps({"session_id": "abc", "result": "ok"})
    )
    assert out.session_id == "abc"
    assert out.exit_code == 0


def test_parse_outcome_camel_case_session():
    out = OpenCodeAdapter(policy_path="p", db_path="d", tickets_db="t").parse_outcome(
        json.dumps({"sessionID": "xyz", "result": "ok"})
    )
    assert out.session_id == "xyz"


def test_parse_outcome_error_flag_sets_nonzero():
    out = OpenCodeAdapter(policy_path="p", db_path="d", tickets_db="t").parse_outcome(
        json.dumps({"sessionID": "x", "error": "boom"})
    )
    assert out.exit_code != 0


def test_parse_outcome_non_json():
    out = OpenCodeAdapter(policy_path="p", db_path="d", tickets_db="t").parse_outcome("oops")
    assert out.session_id is None
    assert out.exit_code != 0
