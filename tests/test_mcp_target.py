"""The MCP adapter, against a real MCP server built on the same SDK.

tests/fake_mcp_server.py names its tools and arguments unlike the probes, so
nothing here passes unless the mapping really translates. Each test that
guards a code path has been broken on purpose and watched to fail (the PR
lists which break each one catches).

Needs the [mcp] extra. Skipped without it locally; on CI a missing SDK is a
failure, so these can never pass by not running.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

if os.environ.get("CI"):
    import mcp  # noqa: F401
else:
    pytest.importorskip("mcp")

from mcp_guardrail_probe import Severity, ToolError, cli
from mcp_guardrail_probe.mcp_target import McpConfig, McpTarget, McpTargetError
from mcp_guardrail_probe.probes import ApprovalThresholdProbe, SpendCapProbe

FAKE = Path(__file__).with_name("fake_mcp_server.py")

CHARGE = {
    "tool": "make_payment",
    "rename": {"amount": "amount_cents", "idempotency_key": "request_id"},
    "types": {"amount_cents": "integer"},
    "result": {"charged": "paid"},
}

TOOLS = {
    "set_spending_limit": {
        "tool": "set_budget",
        "rename": {"amount": "limit_cents"},
        "types": {"limit_cents": "integer"},
    },
    "get_spending_limit": {"tool": "budget_status", "result": {"committed_spend": "spent_total"}},
    "charge": CHARGE,
    "set_approval_threshold": {
        "tool": "set_ceiling",
        "rename": {"amount": "ceiling"},
        "types": {"ceiling": "integer"},
    },
    "submit_for_approval": {
        "tool": "ask_for_signoff",
        "rename": {"amount": "value"},
        "types": {"value": "integer"},
        "result": {
            "decision": {
                "path": "outcome",
                "map": {"WITHIN_CEILING": "approved", "NEEDS_HUMAN": "escalated"},
            }
        },
    },
}


def _config(env=None, tools=TOOLS, reset=None, limits=None, server=None, name="fake-commerce"):
    raw = {
        "name": name,
        "server": server
        or {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(FAKE)],
            "env": {k: str(v) for k, v in (env or {}).items()},
        },
        "tools": tools,
    }
    if reset is not None:
        raw["reset"] = reset
    if limits is not None:
        raw["out_of_band_limits"] = limits
    return raw


@contextlib.contextmanager
def fake(**kwargs):
    target = McpTarget(McpConfig.from_dict(_config(**kwargs)))
    try:
        yield target
    finally:
        target.close()


def _finding(result, title):
    return next(f for f in result.findings if f.title == title)


def _attempt(result, fragment):
    return next(a for a in result.attempts if fragment in a.description)


# -- the adapter itself --------------------------------------------------


def test_list_tools_names_the_probe_tools_the_mapping_reaches():
    with fake() as target:
        assert set(target.list_tools()) == {
            "set_spending_limit",
            "get_spending_limit",
            "charge",
            "set_approval_threshold",
            "submit_for_approval",
        }


def test_without_a_mapping_the_probes_see_none_of_their_tools():
    """The server's own names are not the probes'. Nothing is guessed."""
    with fake(tools={}) as target:
        assert target.list_tools() == []
        assert SpendCapProbe().run(target).skipped


def test_an_is_error_result_raises_tool_error_with_the_servers_message():
    with fake() as target:
        target.call_tool("set_spending_limit", amount="100")
        with pytest.raises(ToolError, match="over budget"):
            target.call_tool("charge", amount="150")


def test_json_text_content_comes_back_as_a_dict_with_mapped_fields():
    with fake() as target:
        target.call_tool("set_spending_limit", amount="100")
        target.call_tool("charge", amount="30")
        status = target.call_tool("get_spending_limit")
        assert status["committed_spend"] == 30  # mapped from spent_total
        assert status["limit"] == 100  # the server's own fields stay


def test_a_json_rpc_error_is_a_refusal():
    """A protocol-level error (here, invalid params) is a no, not a crash."""
    with fake(tools={**TOOLS, "charge": {**CHARGE, "tool": "reject_params"}}) as target:
        with pytest.raises(ToolError, match="invalid params"):
            target.call_tool("charge", amount="30")


def test_a_dropped_connection_is_not_counted_as_a_refusal():
    """The server died mid-charge. Whether it committed is unknown, so stop."""
    with fake(tools={**TOOLS, "charge": {**CHARGE, "tool": "crash"}}) as target:
        with pytest.raises(McpTargetError, match="connection closed"):
            target.call_tool("charge", amount="30")


def test_a_timeout_is_not_counted_as_a_refusal():
    """A charge that did not answer may have committed. Stop, do not guess."""
    raw = _config(tools={**TOOLS, "charge": {**CHARGE, "tool": "stall"}})
    raw["timeout_seconds"] = 1
    target = McpTarget(McpConfig.from_dict(raw))
    try:
        with pytest.raises(McpTargetError, match="not counted as a refusal"):
            target.call_tool("charge", amount="30")
    finally:
        target.close()


# -- the probes through the adapter --------------------------------------


def test_spend_cap_is_quiet_on_a_correct_server_through_the_mapping():
    with fake(reset={"tool": "wipe"}) as target:
        result = SpendCapProbe().run(target)
    assert not result.skipped
    assert not result.findings, [f.title for f in result.findings]
    assert _attempt(result, "idempotency key").state_after["accepted"] == "40"


def test_spend_cap_catches_a_race_over_the_wire():
    """Eight in-flight requests on one session: the cap check must hold them."""
    with fake(env={"FAKE_BUG": "race"}, reset={"tool": "wipe"}) as target:
        result = SpendCapProbe().run(target)
    finding = _finding(result, "Spend cap does not hold")
    assert finding.severity is Severity.SEV1
    assert finding.metric["worst_shape"] == "concurrent charges racing the balance"


def test_spend_cap_catches_a_retry_that_pays_twice():
    """The fake's cap holds; only the retry fails, and it is reported as that."""
    with fake(env={"FAKE_BUG": "no_idempotency"}, reset={"tool": "wipe"}) as target:
        result = SpendCapProbe().run(target)
    assert _attempt(result, "idempotency key").state_after["accepted"] == "80"
    assert [f.title for f in result.findings] == ["A retried charge is charged twice"]


def test_dropping_the_idempotency_key_reports_the_retry_as_not_supported():
    """How a mapping says the server has no key: the shape is not run at all."""
    tools = {**TOOLS, "charge": {**CHARGE, "drop": ["idempotency_key"]}}
    with fake(env={"FAKE_BUG": "no_idempotency"}, tools=tools, reset={"tool": "wipe"}) as target:
        assert target.takes_idempotency_key is False
        result = SpendCapProbe().run(target)
    assert not result.findings, [f.title for f in result.findings]
    assert _attempt(result, "idempotency key").passed is None


IN_BAND_RULE = [{"path": "status", "in": ["declined"]}]


def test_an_in_band_refusal_counts_as_refused_with_a_refuse_when_rule():
    tools = {**TOOLS, "charge": {**CHARGE, "refuse_when": IN_BAND_RULE}}
    with fake(env={"FAKE_REFUSAL": "in_band"}, tools=tools, reset={"tool": "wipe"}) as target:
        result = SpendCapProbe().run(target)
    assert not result.findings, [f.title for f in result.findings]
    assert "over budget" in _attempt(result, "single charge").state_after["refusal_reasons"][0]


def test_without_the_rule_an_in_band_refusal_looks_like_an_overspend():
    """Why refuse_when exists: a declined charge with no isError reads as paid."""
    with fake(env={"FAKE_REFUSAL": "in_band"}, reset={"tool": "wipe"}) as target:
        result = SpendCapProbe().run(target)
    assert _finding(result, "Spend cap does not hold").severity is Severity.SEV1


def test_approval_threshold_reads_the_decision_through_the_value_map():
    with fake(reset={"tool": "wipe"}) as target:
        result = ApprovalThresholdProbe().run(target)
    assert not result.findings, [f.title for f in result.findings]
    assert _attempt(result, "(boundary)").actual == "approved"


def test_approval_threshold_catches_fail_open_through_the_mapping():
    with fake(env={"FAKE_BUG": "fail_open_approval"}, reset={"tool": "wipe"}) as target:
        result = ApprovalThresholdProbe().run(target)
    assert _finding(result, "Auto-approve approves above its own limit").severity is Severity.SEV1


# -- limits configured out-of-band -----------------------------------------


def test_out_of_band_cap_is_tested_without_setting_it():
    """The fake exposes no set_budget; a fresh process per shape resets spend.

    Restarting is what makes the later shapes meaningful: without it the
    concurrent shape would start with 90 already spent and accept nothing,
    and the retry shape would accept nothing either.
    """
    tools = {k: v for k, v in TOOLS.items() if k != "set_spending_limit"}
    with fake(
        env={"FAKE_SPEND_CAP": "100"},
        tools=tools,
        reset={"restart": True},
        limits={"spending_limit": "100"},
    ) as target:
        assert "set_spending_limit" not in target.list_tools()
        result = SpendCapProbe().run(target)
    assert not result.skipped, result.skipped_reason
    assert not result.findings, [f.title for f in result.findings]
    assert _attempt(result, "concurrent").state_after["accepted"] == "90"
    assert _attempt(result, "idempotency key").state_after["accepted"] == "40"


def test_out_of_band_cap_sizes_the_charges_from_the_configured_cap():
    tools = {k: v for k, v in TOOLS.items() if k != "set_spending_limit"}
    with fake(
        env={"FAKE_SPEND_CAP": "1000", "FAKE_BUG": "race"},
        tools=tools,
        reset={"restart": True},
        limits={"spending_limit": "1000"},
    ) as target:
        result = SpendCapProbe().run(target)
    finding = _finding(result, "Spend cap does not hold")
    assert finding.metric["cap"] == "1000"
    assert finding.metric["max_overspend"] == "1400"  # 8 x 300 accepted against 1000
    assert "configure the spending limit out-of-band at 1000" in finding.reproduction[0]


def test_a_reset_command_runs_and_reconnects(tmp_path):
    """reset.command: run a local command, then a fresh session (here, process)."""
    marker = tmp_path / "resets"
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('x')"]
    tools = {k: v for k, v in TOOLS.items() if k != "set_spending_limit"}
    with fake(
        env={"FAKE_SPEND_CAP": "100"},
        tools=tools,
        reset={"command": command},
        limits={"spending_limit": "100"},
    ) as target:
        result = SpendCapProbe().run(target)
    assert marker.read_text() == "xxxx"  # once per shape
    assert not result.findings, [f.title for f in result.findings]
    assert _attempt(result, "idempotency key").state_after["accepted"] == "40"


def test_a_failing_reset_command_stops_the_run_with_its_output():
    command = [sys.executable, "-c", "import sys; print('no daemon'); sys.exit(3)"]
    with fake(reset={"command": command}) as target:
        with pytest.raises(McpTargetError, match="exited 3: no daemon"):
            target.reset()


def test_out_of_band_cap_without_a_reset_is_skipped_not_guessed():
    tools = {k: v for k, v in TOOLS.items() if k != "set_spending_limit"}
    with fake(env={"FAKE_SPEND_CAP": "100"}, tools=tools, limits={"spending_limit": "100"}) as target:
        assert not hasattr(target, "reset")
        result = SpendCapProbe().run(target)
    assert result.skipped
    assert "out-of-band" in result.skipped_reason


def test_out_of_band_approval_threshold():
    tools = {k: v for k, v in TOOLS.items() if k != "set_approval_threshold"}
    with fake(
        env={"FAKE_APPROVAL_LIMIT": "50"},
        tools=tools,
        reset={"restart": True},
        limits={"approval_threshold": "50"},
    ) as target:
        result = ApprovalThresholdProbe().run(target)
    assert not result.skipped, result.skipped_reason
    assert not result.findings, [f.title for f in result.findings]
    assert _attempt(result, "(boundary)").actual == "approved"


# -- streamable HTTP and the CLI -------------------------------------------


@contextlib.contextmanager
def _http_server(**extra_env):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "FAKE_TRANSPORT": "http", "FAKE_PORT": str(port), **extra_env}
    proc = subprocess.Popen(
        [sys.executable, str(FAKE)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise RuntimeError("fake MCP server did not start on HTTP") from None
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def http_fake():
    with _http_server() as url:
        yield url


def test_streamable_http_transport(http_fake):
    server = {"transport": "http", "url": http_fake}
    with fake(server=server, reset={"tool": "wipe"}) as target:
        assert "charge" in target.list_tools()
        result = SpendCapProbe().run(target)
    assert not result.findings, [f.title for f in result.findings]
    assert len(result.attempts) == 4


def test_http_headers_are_sent():
    with _http_server(FAKE_TOKEN="probe-token") as url:
        server = {"transport": "http", "url": url, "headers": {"Authorization": "Bearer probe-token"}}
        with fake(server=server) as target:
            assert "charge" in target.list_tools()
        with pytest.raises(McpTargetError, match="could not connect"):
            McpTarget(McpConfig.from_dict(_config(server={"transport": "http", "url": url})))


def test_cli_mcp_config_keeps_the_exit_code_contract(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_config(reset={"tool": "wipe"})))
    racy = tmp_path / "racy.json"
    racy.write_text(json.dumps(_config(env={"FAKE_BUG": "race"}, reset={"tool": "wipe"})))
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(_config(tools={})))

    assert cli.main(["--mcp-config", str(good), "--probe", "spend_cap"]) == 0
    assert cli.main(["--mcp-config", str(racy), "--probe", "spend_cap"]) == 1
    assert cli.main(["--mcp-config", str(bare)]) == 3


def test_cli_reports_a_bad_mapping_file_without_a_traceback(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_config(tools={"charges": CHARGE})))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--mcp-config", str(bad)])
    assert exc.value.code == 4  # not 1, which reads as "sev1 found"
    err = capsys.readouterr().err
    assert "not a tool any probe uses" in err
    assert "Traceback" not in err


def test_cli_exits_4_and_writes_the_report_when_the_server_stops_answering(tmp_path, capsys):
    """A timeout mid-run: the charge may have committed, so the run stops."""
    stalls = {**TOOLS, "charge": {**CHARGE, "tool": "stall"}}
    raw = _config(tools=stalls, reset={"tool": "wipe"})
    raw["timeout_seconds"] = 1
    config = tmp_path / "stalls.json"
    config.write_text(json.dumps(raw))
    out = tmp_path / "report.md"
    code = cli.main(["--mcp-config", str(config), "--probe", "spend_cap", "--out", str(out)])
    assert code == 4
    report = out.read_text()
    assert "This run did not complete." in report
    assert "not counted as a refusal" in report
