import pytest
from erebus.policy.parsing import parse_command, ParsedCommand, CommandParseError


def test_simple_command_parses_to_argv():
    result = parse_command("git status")
    assert result.argv == ["git", "status"]
    assert result.contains_operators is False
    assert result.operators_found == []


def test_command_with_quoted_arg():
    result = parse_command('git commit -m "hello world"')
    assert result.argv == ["git", "commit", "-m", "hello world"]
    assert result.contains_operators is False


def test_detects_and_operator():
    result = parse_command("git status && rm -rf /")
    assert result.contains_operators is True
    assert "&&" in result.operators_found


def test_detects_pipe():
    result = parse_command("cat /var/log/syslog | grep error")
    assert result.contains_operators is True
    assert "|" in result.operators_found


def test_detects_semicolon():
    result = parse_command("ls; whoami")
    assert result.contains_operators is True
    assert ";" in result.operators_found


def test_detects_command_substitution():
    result = parse_command("echo $(whoami)")
    assert result.contains_operators is True
    assert "$(" in result.operators_found


def test_detects_backtick():
    result = parse_command("echo `whoami`")
    assert result.contains_operators is True
    assert "`" in result.operators_found


def test_detects_redirect():
    result = parse_command("echo hi > /etc/passwd")
    assert result.contains_operators is True
    assert ">" in result.operators_found


def test_empty_command_raises():
    with pytest.raises(CommandParseError):
        parse_command("   ")


def test_unbalanced_quote_raises():
    with pytest.raises(CommandParseError):
        parse_command('git commit -m "unterminated')
