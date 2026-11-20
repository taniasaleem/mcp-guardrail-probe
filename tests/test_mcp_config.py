"""The MCP mapping file and result parsing. None of this needs the SDK."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_guardrail_probe import ToolError
from mcp_guardrail_probe.mcp_target import ConfigError, McpConfig, ToolMapping, parse_result

STDIO = {"transport": "stdio", "command": "my-server"}


def _cfg(**overrides):
    return McpConfig.from_dict({"name": "x", "server": STDIO, **overrides})


def test_core_package_and_mapping_import_without_the_sdk():
    """Zero dependencies: nothing pulls in mcp until a target connects."""
    code = (
        "import sys, mcp_guardrail_probe, mcp_guardrail_probe.cli, "
        "mcp_guardrail_probe.mcp_target; assert 'mcp' not in sys.modules, 'mcp imported'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_unknown_keys_are_rejected_not_ignored():
    with pytest.raises(ConfigError, match="unknown key"):
        _cfg(tools={"charge": {"tool": "pay", "renames": {"amount": "value"}}})


def test_a_mapping_for_a_tool_no_probe_uses_is_a_typo():
    with pytest.raises(ConfigError, match="not a tool any probe uses"):
        _cfg(tools={"charges": {"tool": "pay"}})


def test_secrets_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("PROBE_TEST_KEY", "sk_test_123")
    cfg = McpConfig.from_dict(
        {"name": "x", "server": {**STDIO, "env": {"STRIPE_SECRET_KEY": "${PROBE_TEST_KEY}"}}}
    )
    assert cfg.env == {"STRIPE_SECRET_KEY": "sk_test_123"}


def test_a_missing_environment_variable_is_an_error_not_an_empty_string(monkeypatch):
    monkeypatch.delenv("PROBE_TEST_UNSET", raising=False)
    with pytest.raises(ConfigError, match="PROBE_TEST_UNSET"):
        McpConfig.from_dict({"name": "x", "server": {**STDIO, "env": {"K": "${PROBE_TEST_UNSET}"}}})


def test_an_unset_variable_can_have_a_default(monkeypatch):
    monkeypatch.delenv("PROBE_TEST_PORT", raising=False)
    cfg = McpConfig.from_dict(
        {"name": "x", "server": {"transport": "http", "url": "http://127.0.0.1:${PROBE_TEST_PORT:-4024}/mcp"}}
    )
    assert cfg.url == "http://127.0.0.1:4024/mcp"
    monkeypatch.setenv("PROBE_TEST_PORT", "5000")
    cfg = McpConfig.from_dict(
        {"name": "x", "server": {"transport": "http", "url": "http://127.0.0.1:${PROBE_TEST_PORT:-4024}/mcp"}}
    )
    assert cfg.url == "http://127.0.0.1:5000/mcp"


def test_reset_takes_exactly_one_strategy():
    with pytest.raises(ConfigError, match="exactly one"):
        _cfg(reset={"tool": "wipe", "restart": True})


def test_a_restart_reset_is_refused_over_http():
    """Reconnecting to an http server keeps its state: no reading would be fresh."""
    http = {"transport": "http", "url": "http://127.0.0.1:4024/mcp"}
    with pytest.raises(ConfigError, match="reset.restart only works for stdio"):
        McpConfig.from_dict({"name": "x", "server": http, "reset": {"restart": True}})
    assert _cfg(reset={"restart": True}).reset == {"restart": True}  # stdio: a new process
    assert McpConfig.from_dict({"name": "x", "server": http, "reset": {"tool": "wipe"}}).reset


def test_out_of_band_limits_are_decimals_and_known_keys():
    assert _cfg(out_of_band_limits={"spending_limit": "100"}).out_of_band_limits == {
        "spending_limit": __import__("decimal").Decimal("100")
    }
    with pytest.raises(ConfigError, match="unknown key"):
        _cfg(out_of_band_limits={"spend_cap": "100"})


def test_arguments_are_renamed_fixed_dropped_and_typed():
    mapping = ToolMapping.parse(
        "charge",
        {
            "tool": "pay",
            "rename": {"amount": "amount_cents"},
            "fixed": {"currency": "USD"},
            "drop": ["idempotency_key"],
            "types": {"amount_cents": "integer"},
        },
    )
    args = mapping.arguments("charge", {"amount": "150", "idempotency_key": "k"})
    assert args == {"amount_cents": 150, "currency": "USD"}


def test_dropping_idempotency_key_from_charge_says_the_server_has_none():
    assert _cfg().takes_idempotency_key is True
    assert _cfg(tools={"charge": {"tool": "pay"}}).takes_idempotency_key is True
    dropped = {"charge": {"tool": "pay", "drop": ["idempotency_key"]}}
    assert _cfg(tools=dropped).takes_idempotency_key is False


def test_a_fixed_argument_may_not_override_what_the_probe_sends():
    mapping = ToolMapping.parse("charge", {"fixed": {"amount": "1"}})
    with pytest.raises(ConfigError, match="also sends"):
        mapping.arguments("charge", {"amount": "150"})


def test_integer_type_refuses_to_round_a_fractional_amount():
    mapping = ToolMapping.parse("charge", {"types": {"amount": "integer"}})
    with pytest.raises(ConfigError, match="whole number"):
        mapping.arguments("charge", {"amount": "1.5"})


def test_refuse_when_rules():
    mapping = ToolMapping.parse(
        "charge",
        {"refuse_when": [{"path": "error", "present": True}, {"path": "result.status", "not_in": ["settled"]}]},
    )
    assert mapping.interpret({"result": {"status": "settled"}})["result"]["status"] == "settled"
    with pytest.raises(ToolError, match="result.status='denied'"):
        mapping.interpret({"result": {"status": "denied"}})
    with pytest.raises(ToolError, match="error is present"):
        mapping.interpret({"error": "no wallet", "result": {"status": "settled"}})
    with pytest.raises(ToolError, match="missing"):
        mapping.interpret({"something": "else"})


def test_result_fields_are_mapped_and_the_rest_kept():
    mapping = ToolMapping.parse(
        "submit_for_approval",
        {"result": {"decision": {"path": "status", "map": {"APPROVED": "approved"}}}},
    )
    out = mapping.interpret({"status": "APPROVED", "id": "tx_1"})
    assert out == {"status": "APPROVED", "id": "tx_1", "decision": "approved"}
    assert mapping.interpret({"status": "AWAITING_YOU"})["decision"] == "AWAITING_YOU"


def _result(*texts, is_error=False, structured=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=t) for t in texts],
        is_error=is_error,
        structured_content=structured,
    )


def test_parse_result():
    assert parse_result("t", _result('{"a": 1}')) == {"a": 1}
    assert parse_result("t", _result("[1, 2]")) == {"result": [1, 2]}
    assert parse_result("t", _result("plain words")) == {"text": "plain words"}
    assert parse_result("t", _result("plain", structured={"b": 2})) == {"b": 2}
    with pytest.raises(ToolError, match="over budget"):
        parse_result("t", _result("over budget", is_error=True))


SERVERS = sorted((Path(__file__).resolve().parent.parent / "servers").glob("*/*.json"))


@pytest.mark.parametrize("path", SERVERS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_committed_mapping_file_parses(path, monkeypatch):
    """A typo in a mapping file should fail here, not halfway through a paid run."""
    for name in ("ALMEGA_VENV", "STRIPE_SECRET_KEY"):
        monkeypatch.setenv(name, "placeholder")
    cfg = McpConfig.from_file(path)
    assert cfg.tools, f"{path} maps no tools"
    assert cfg.reset is not None, f"{path} has no reset; every reading would inherit the last one's state"
    if "charge" in cfg.tools:
        # None of the servers mapped here has an idempotency key. Sending the
        # probe's would make the retry shape read as a double charge.
        assert cfg.takes_idempotency_key is False, f"{path} must drop idempotency_key"


def test_the_committed_mapping_files_are_found():
    assert len(SERVERS) >= 5, SERVERS
