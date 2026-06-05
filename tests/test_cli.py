from erebus.cli import build_parser


def test_parser_run():
    ns = build_parser().parse_args(["run", "--task", "check disk", "--agent", "stub"])
    assert ns.command == "run"
    assert ns.task == "check disk"
    assert ns.agent == "stub"


def test_parser_approve():
    ns = build_parser().parse_args(["approve", "T-123", "--note", "ok"])
    assert ns.command == "approve"
    assert ns.ticket_id == "T-123"
    assert ns.note == "ok"


def test_parser_serve_defaults():
    ns = build_parser().parse_args(["serve"])
    assert ns.command == "serve"
    assert ns.host == "0.0.0.0"
    assert ns.port == 8080
