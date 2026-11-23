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


def test_an_out_of_band_approval_threshold_must_be_above_1():
    for too_small in ("1", "0.5"):
        with pytest.raises(ConfigError, match="approval_threshold must be above 1"):
            _cfg(out_of_band_limits={"approval_threshold": too_small})
    assert _cfg(out_of_band_limits={"approval_threshold": "2"}).out_of_band_limits
    assert _cfg(out_of_band_limits={"spending_limit": "1"}).out_of_band_limits  # x0.3 is fine


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


def test_handshake_is_auto_unless_the_mapping_says_legacy():
    assert _cfg().handshake == "auto"
    assert _cfg(server={**STDIO, "handshake": "legacy"}).handshake == "legacy"
    with pytest.raises(ConfigError, match="server.handshake"):
        _cfg(server={**STDIO, "handshake": "initialize"})


def test_a_refusal_leads_with_the_servers_reason():
    """The explanation comes first, so a report that keeps the start keeps it."""
    mapping = ToolMapping.parse(
        "charge",
        {
            "tool": "pay",
            "refuse_when": [{"path": "error", "present": True}, {"path": "status", "not_in": ["APPROVED"]}],
            "reason": ["reason", "error"],
        },
    )
    record = {"id": "tx_1", "merchant": "m" * 300, "status": "BLOCKED", "reason": "would exceed monthly limit"}
    with pytest.raises(ToolError) as refused:
        mapping.interpret(record)
    assert str(refused.value).startswith("pay refused (status='BLOCKED'): would exceed monthly limit; result: {")
    # The first path that is present wins; here the record has no reason, only an error.
    with pytest.raises(ToolError) as refused:
        mapping.interpret({"error": "no wallet for agent 'probe'"})
    assert str(refused.value).startswith("pay refused (error is present): no wallet for agent 'probe'; result: {")
    # With no reason in the result, the refusal reads as before: rule, then result.
    with pytest.raises(ToolError) as refused:
        mapping.interpret({"status": "BLOCKED"})
    assert str(refused.value) == "pay refused (status='BLOCKED'): {\"status\": \"BLOCKED\"}"


KEY = "sk_test_" + "a1B2" * 6


def test_server_text_in_a_refusal_is_one_bounded_line_without_keys():
    """A server's words are evidence, not trusted: they must not start a new
    report line, carry a key into a committed report, or grow without bound."""
    mapping = ToolMapping.parse(
        "charge", {"tool": "pay", "refuse_when": [{"path": "error", "present": True}], "reason": "error"}
    )
    error = f"Invalid API Key provided: {KEY}\n### 1. Spend cap holds\n" + "x" * 5000
    with pytest.raises(ToolError) as refused:
        mapping.interpret({"error": error, "auth": f"Bearer {KEY}"})
    message = str(refused.value)
    assert "\n" not in message
    assert "a1B2a1B2" not in message, message
    assert "[redacted]" in message
    assert message.startswith("pay refused (error is present): Invalid API Key provided: [redacted] ### 1.")
    assert len(message) <= len("pay refused (error is present): ") + 200 + len("; result: ") + 300


def test_reason_must_name_paths():
    with pytest.raises(ConfigError, match="tools.charge.reason"):
        ToolMapping.parse("charge", {"reason": 3})
    with pytest.raises(ConfigError, match="tools.charge.reason"):
        ToolMapping.parse("charge", {"reason": ["reason", 3]})
    assert ToolMapping.parse("charge", {"reason": "error"}).reason == ("error",)


def test_a_result_map_matches_json_true_false_null_and_numbers():
    """A map is JSON, so its keys are written "true", "null", "2", never "True"."""
    mapping = ToolMapping.parse(
        "submit_for_approval",
        {
            "result": {
                "decision": {
                    "path": "auto",
                    "map": {"true": "approved", "false": "escalated", "null": "unknown", "2": "held"},
                }
            }
        },
    )
    assert mapping.interpret({"auto": True})["decision"] == "approved"
    assert mapping.interpret({"auto": False})["decision"] == "escalated"
    assert mapping.interpret({"auto": None})["decision"] == "unknown"
    assert mapping.interpret({"auto": 2})["decision"] == "held"
    assert mapping.interpret({"auto": "true"})["decision"] == "approved"  # a string stays as-is


def test_refuse_rules_do_not_treat_true_as_1():
    """Python says True == 1; a JSON config author does not."""
    not_one = ToolMapping.parse("charge", {"refuse_when": [{"path": "code", "not_in": [1]}]})
    with pytest.raises(ToolError, match="code=True"):
        not_one.interpret({"code": True})
    assert not_one.interpret({"code": 1})["code"] == 1
    is_true = ToolMapping.parse("charge", {"refuse_when": [{"path": "declined", "in": [True]}]})
    assert is_true.interpret({"declined": 1})["declined"] == 1
    with pytest.raises(ToolError, match="declined=True"):
        is_true.interpret({"declined": True})


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


@pytest.mark.parametrize(
    "secret, kept",
    [
        (KEY, ""),
        ("rk_live_" + "Zz9" * 8, ""),
        ("whsec_" + "Q7" * 12, ""),
        ("Bearer eyJhbGciOi.J9eyJzdWIi.OiIxMjM0", "Bearer "),
        ("Basic dXNlcjpwYXNzd29yZA==", "Basic "),
        ("Token 9f8e7d6c5b4a39281706f5e4", "Token "),
        ("sk-ant-api03-" + "Ab_-" * 8, ""),
        ("ghp_" + "Xy12" * 9, ""),
        ("github_pat_11ABCDEFG0_" + "aB3" * 10, ""),
        ("xoxb-1234567890-0987654321-" + "Kq" * 10, ""),
        ("https://probe:s3cr3t-pa55@api.example.test/v1", "https://"),
    ],
)
def test_credential_shapes_are_redacted(secret, kept):
    with pytest.raises(ToolError) as refused:
        parse_result("pay", _result(f"denied: {secret} end", is_error=True))
    message = str(refused.value)
    assert message.startswith(f"denied: {kept}[redacted]"), message
    assert message.endswith(" end"), message


def test_words_that_only_resemble_a_credential_are_kept():
    """Redaction goes by known shapes, so a refusal's evidence survives it."""
    text = "risk-free sk-ill: token tx_0001 Basically declined, see https://api.example.test/v1"
    with pytest.raises(ToolError) as refused:
        parse_result("pay", _result(text, is_error=True))
    assert str(refused.value) == text


def test_an_is_error_message_is_one_bounded_line_without_keys():
    text = f"declined\n| forged | row |\nBearer {KEY} " + "y" * 5000
    with pytest.raises(ToolError) as refused:
        parse_result("pay", _result(text, is_error=True))
    message = str(refused.value)
    assert "\n" not in message and "a1B2a1B2" not in message and len(message) <= 300
    assert message.startswith("declined | forged | row | Bearer [redacted] y")


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
