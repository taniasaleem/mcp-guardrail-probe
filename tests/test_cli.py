"""The exit-code contract CI gates on.

0 clean, 1 a sev1 is present, 2 only sev2/sev3, 3 nothing was tested. The
last one matters most: a target that exposes none of the tools the probes
need must not come back looking like a clean pass.
"""

from __future__ import annotations

import json

from mcp_guardrail_probe import cli


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
    assert payload["probes"] == {"run": 0, "skipped": 3}
