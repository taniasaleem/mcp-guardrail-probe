"""Tests.

The important ones are the pairs: each probe must find the bug in the
vulnerable mock AND stay quiet on the fixed one. A probe that only ever
fires is an alarm, not a test, and a probe that never fires is decoration.
"""

from __future__ import annotations

from mcp_guardrail_probe import Severity, ToolError
from mcp_guardrail_probe.probes import (
    ApprovalThresholdProbe,
    InjectionProbe,
    SpendCapProbe,
)
from mcp_guardrail_probe.mock_server import SafeCommerceServer, VulnerableCommerceServer


# -- spend cap ---------------------------------------------------------

def test_spend_cap_catches_the_vulnerable_server():
    result = SpendCapProbe().run(VulnerableCommerceServer())
    assert result.findings, "probe found nothing on a server with a known broken cap"
    assert any(f.severity is Severity.SEV1 for f in result.findings)


def test_spend_cap_quiet_on_the_fixed_server():
    result = SpendCapProbe().run(SafeCommerceServer())
    assert not result.findings, [f.title for f in result.findings]


def test_spend_cap_records_every_attempt():
    result = SpendCapProbe().run(SafeCommerceServer())
    assert len(result.attempts) >= 4, "each attack shape should leave a record"


# -- injection ---------------------------------------------------------

def test_injection_catches_the_vulnerable_server():
    result = InjectionProbe().run(VulnerableCommerceServer())
    assert result.findings
    assert any("charge" in f.metric.get("money_tools_reached", []) for f in result.findings)


def test_injection_quiet_on_the_fixed_server():
    result = InjectionProbe().run(SafeCommerceServer())
    assert not result.findings, [f.title for f in result.findings]


def test_injection_skips_fields_the_target_rejects():
    """seller_note is refused by the mock, so it must not be probed."""
    result = InjectionProbe().run(VulnerableCommerceServer())
    probed = {f.metric["field"] for f in result.findings}
    assert "seller_note" not in probed


def test_injection_probes_the_title_field():
    """The title is the most visible injection surface and the mock obeys it."""
    result = InjectionProbe().run(VulnerableCommerceServer())
    probed = {f.metric["field"] for f in result.findings}
    assert "title" in probed, probed


# -- approval threshold ------------------------------------------------

def test_approval_catches_fail_closed():
    result = ApprovalThresholdProbe().run(VulnerableCommerceServer())
    titles = [f.title for f in result.findings]
    assert any("blocks below" in t for t in titles), titles


def test_approval_quiet_on_the_fixed_server():
    result = ApprovalThresholdProbe().run(SafeCommerceServer())
    assert not result.findings, [f.title for f in result.findings]


# -- plumbing ----------------------------------------------------------

class InBandRefusalServer(SafeCommerceServer):
    """The fixed mock, but refusals come back the way MCP sends them.

    MCP reports a failed tool call inside the result (`isError: true`), not
    as an exception. An adapter that passes that result straight through
    must not make a refused charge look like an accepted one.
    """

    name = "mock://safe-commerce-in-band-refusals"

    def call_tool(self, tool, **kwargs):
        try:
            return super().call_tool(tool, **kwargs)
        except ToolError as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}


def test_in_band_refusals_are_refusals_not_acceptances():
    for probe_cls in (SpendCapProbe, ApprovalThresholdProbe, InjectionProbe):
        result = probe_cls().run(InBandRefusalServer())
        assert not result.findings, (probe_cls.name, [f.title for f in result.findings])


def test_in_band_refused_field_is_not_probed():
    result = InjectionProbe().run(InBandRefusalServer())
    assert not any("seller_note" in a.description for a in result.attempts)


def test_probe_skips_rather_than_guessing():
    class Bare:
        name = "bare"

        def list_tools(self):
            return ["ping"]

        def call_tool(self, tool, **kwargs):
            raise AssertionError("should never be called")

    result = SpendCapProbe().run(Bare())
    assert result.skipped
    assert "set_spending_limit" in result.skipped_reason


def test_findings_serialise():
    result = SpendCapProbe().run(VulnerableCommerceServer())
    payload = result.to_dict()
    assert payload["probe"] == "spend_cap"
    assert isinstance(payload["findings"], list)
    assert payload["findings"][0]["severity"] in {"sev1", "sev2", "sev3"}
