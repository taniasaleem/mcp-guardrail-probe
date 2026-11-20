"""The exit-code contract CI gates on.

0 clean, 1 a sev1 is present, 2 only sev2/sev3, 3 nothing was tested, 4 the
run did not complete. 3 and 4 matter most: a target that exposes none of the
tools the probes need must not come back looking like a clean pass, and a run
that crashed must not come back looking like a sev1 (Python's own exit code
for an uncaught exception is 1).
"""

from __future__ import annotations

import json

import pytest

from mcp_guardrail_probe import cli
from mcp_guardrail_probe.mcp_target import McpTargetError
from mcp_guardrail_probe.mock_server import SafeCommerceServer
from mcp_guardrail_probe.model import ProbeResult


class Bare:
    name = "bare"

    def list_tools(self):
        return ["ping"]

    def call_tool(self, tool, **kwargs):
        raise AssertionError("should never be called")


def test_demo_exits_1_and_safe_demo_exits_0(capsys):
    assert cli.main(["--demo"]) == 1
    assert cli.main(["--demo-safe"]) == 0


def test_every_probe_skipped_exits_3(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_target", lambda spec: Bare())
    assert cli.main(["--target", "anything:bare"]) == 3


def test_every_probe_skipped_says_nothing_was_tested(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_target", lambda spec: Bare())
    cli.main(["--target", "anything:bare"])
    out = capsys.readouterr().out
    assert "Nothing was tested" in out
    assert "No findings" not in out


def test_json_counts_skipped_probes(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_target", lambda spec: Bare())
    cli.main(["--target", "anything:bare", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["probes"] == {"run": 0, "skipped": 3, "aborted": 0}


def test_the_target_is_closed_after_the_run(monkeypatch, capsys):
    """An MCP target holds a server process open; the CLI must let it go."""

    class Closable(SafeCommerceServer):
        closed = False

        def close(self):
            self.closed = True

    target = Closable()
    monkeypatch.setattr(cli, "load_target", lambda spec: target)
    assert cli.main(["--target", "anything:closable"]) == 0
    assert target.closed


class StopsAnswering(SafeCommerceServer):
    """Answers approvals, then stops answering on the third charge."""

    name = "mock://stops-answering"

    def __init__(self, error):
        super().__init__()
        self.error = error
        self.charges = 0

    def _tool_charge(self, amount, idempotency_key=None):
        self.charges += 1
        if self.charges == 3:
            raise self.error
        return super()._tool_charge(amount, idempotency_key)


def _run_until_it_stops(monkeypatch, tmp_path, error):
    out = tmp_path / "report.json"
    monkeypatch.setattr(cli, "load_target", lambda spec: StopsAnswering(error))
    code = cli.main(
        [
            "--target", "anything:stops",
            "--probe", "approval_threshold",
            "--probe", "spend_cap",
            "--probe", "content_injection",
            "--format", "json",
            "--out", str(out),
        ]
    )
    return code, json.loads(out.read_text())


def test_a_run_that_stops_part_way_exits_4_and_still_writes_the_report(
    monkeypatch, tmp_path, capsys
):
    error = McpTargetError("no answer from the MCP server within 60s")
    code, report = _run_until_it_stops(monkeypatch, tmp_path, error)
    assert code == 4
    finished, aborted, not_run = report["results"]
    assert finished["probe"] == "approval_threshold" and finished["attempts"]
    assert aborted["probe"] == "spend_cap"
    assert aborted["aborted_reason"] == "McpTargetError: no answer from the MCP server within 60s"
    assert len(aborted["attempts"]) == 1  # the single-charge shape finished first
    assert not_run["skipped_reason"] == "not run: the run stopped during spend_cap"
    assert report["probes"] == {"run": 1, "skipped": 1, "aborted": 1}
    err = capsys.readouterr().err
    assert "run aborted during spend_cap" in err
    assert "Traceback" not in err  # an expected failure says enough on its own


def test_an_adapter_bug_also_exits_4_with_its_traceback(monkeypatch, tmp_path, capsys):
    code, report = _run_until_it_stops(monkeypatch, tmp_path, KeyError("amount"))
    assert code == 4
    assert report["results"][1]["aborted_reason"] == "KeyError: 'amount'"
    assert "Traceback" in capsys.readouterr().err


def test_the_markdown_report_says_the_run_did_not_complete(monkeypatch, capsys):
    error = McpTargetError("the connection closed before charge answered")
    monkeypatch.setattr(cli, "load_target", lambda spec: StopsAnswering(error))
    assert cli.main(["--target", "anything:stops", "--probe", "spend_cap"]) == 4
    out = capsys.readouterr().out
    assert "This run did not complete." in out
    assert "**aborted** after 1 attempts" in out
    assert "No findings. Every probe that ran completed" not in out


def test_an_aborted_run_exits_4_even_after_a_sev1():
    from mcp_guardrail_probe.model import Finding, Severity

    sev1 = Finding("spend_cap", "Spend cap does not hold", Severity.SEV1, None, "", [])
    results = [
        ProbeResult("spend_cap", "t", findings=[sev1]),
        ProbeResult("approval_threshold", "t", aborted_reason="McpTargetError: timeout"),
    ]
    assert cli.exit_code(results) == 4


def test_a_target_that_cannot_be_loaded_exits_4_not_1(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--target", "no_such_module_here:build"])
    assert exc.value.code == 4
    assert "could not import 'no_such_module_here'" in capsys.readouterr().err


def test_closing_the_target_cannot_change_the_exit_code(monkeypatch, capsys):
    class FailsToClose(SafeCommerceServer):
        def close(self):
            raise McpTargetError("no answer from the MCP server within 20s")

    monkeypatch.setattr(cli, "load_target", lambda spec: FailsToClose())
    assert cli.main(["--target", "anything:fails-to-close"]) == 0
    assert "closing the target failed" in capsys.readouterr().err
