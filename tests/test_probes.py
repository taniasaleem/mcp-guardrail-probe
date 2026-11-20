"""Tests.

The important ones are the pairs: each probe must find the bug in the
vulnerable mock AND stay quiet on the fixed one. A probe that only ever
fires is an alarm, not a test, and a probe that never fires is decoration.
"""

from __future__ import annotations

from decimal import Decimal

from mcp_guardrail_probe import Direction, Severity, ToolError
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


def _finding(result, title):
    return next(f for f in result.findings if f.title == title)


def _attempt(result, fragment):
    return next(a for a in result.attempts if fragment in a.description)


def test_spend_cap_worst_overspend_is_the_concurrent_race():
    """The headline: 8 x 30 racing a cap of 100, all accepted, 140 over.

    Fails if the probe trusts the server's counter (which says 30), or stops
    issuing the charges concurrently, or the mock's race disappears; in each
    case the retry shape's 40 would quietly become the worst.
    """
    finding = _finding(SpendCapProbe().run(VulnerableCommerceServer()), "Spend cap does not hold")
    assert finding.metric["worst_shape"] == "concurrent charges racing the balance"
    assert finding.metric["max_overspend"] == "140"


def test_spend_cap_catches_a_double_charge_on_retry():
    attempt = _attempt(SpendCapProbe().run(VulnerableCommerceServer()), "idempotency key")
    assert not attempt.passed
    assert attempt.state_after["accepted"] == "80"


def test_spend_cap_reports_the_counter_disagreeing_with_its_ledger():
    finding = _finding(
        SpendCapProbe().run(VulnerableCommerceServer()), "Server under-reports committed spend"
    )
    assert finding.severity is Severity.SEV1
    assert finding.metric["accepted_total"] == "240"
    assert finding.metric["reported_committed"] == "30"


class UnderReportingServer(SafeCommerceServer):
    """The cap holds, but the server's own counter always says 0."""

    name = "mock://safe-commerce-under-reporting"

    def _tool_get_spending_limit(self):
        return {**super()._tool_get_spending_limit(), "committed_spend": "0"}


def test_spend_cap_treats_the_servers_counter_as_a_claim_not_evidence():
    """A cap that holds is not an overspend, but a counter that lies is a finding."""
    result = SpendCapProbe().run(UnderReportingServer())
    titles = [f.title for f in result.findings]
    assert "Spend cap does not hold" not in titles
    assert "Server under-reports committed spend" in titles


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


def test_injection_that_reaches_money_is_sev1():
    findings = InjectionProbe().run(VulnerableCommerceServer()).findings
    assert findings
    assert all(f.severity is Severity.SEV1 for f in findings), [f.severity for f in findings]


class NonMoneyInjectionServer(SafeCommerceServer):
    """An agent that obeys listing text, but only reaches a read-only tool."""

    name = "mock://non-money-injection"

    def _tool_agent_request(self, prompt):
        poisoned = any("charge" in str(v) for listing in self.listings for v in listing.values())
        calls = ["search_items"] + (["get_spending_limit"] if poisoned else [])
        return {"tool_calls": calls, "text": "Here is the blue mug."}


def test_injection_that_reaches_no_money_tool_is_sev3():
    findings = InjectionProbe().run(NonMoneyInjectionServer()).findings
    assert findings
    assert all(f.severity is Severity.SEV3 for f in findings)
    assert all(f.metric["money_tools_reached"] == [] for f in findings)


# -- approval threshold ------------------------------------------------

def test_approval_catches_fail_closed():
    result = ApprovalThresholdProbe().run(VulnerableCommerceServer())
    titles = [f.title for f in result.findings]
    assert any("blocks below" in t for t in titles), titles


def test_approval_quiet_on_the_fixed_server():
    result = ApprovalThresholdProbe().run(SafeCommerceServer())
    assert not result.findings, [f.title for f in result.findings]


class FailOpenApprovalServer(VulnerableCommerceServer):
    """Off by one in the approving direction: L+1 is still auto-approved."""

    name = "mock://fail-open-approval"

    def _tool_submit_for_approval(self, amount):
        if self.approval_threshold is None:
            return {"decision": "escalated"}
        approved = Decimal(str(amount)) <= self.approval_threshold + 1
        return {"decision": "approved" if approved else "escalated"}


def test_approval_catches_fail_open_above_the_limit():
    finding = _finding(
        ApprovalThresholdProbe().run(FailOpenApprovalServer()),
        "Auto-approve approves above its own limit",
    )
    assert finding.severity is Severity.SEV1
    assert finding.direction is Direction.FAIL_OPEN


class PerRequestOnlyServer(VulnerableCommerceServer):
    """Correct boundary, no cumulative check: the vulnerable mock's second
    approval bug, which its off-by-one hides in the demo."""

    name = "mock://per-request-only-approval"

    def _tool_submit_for_approval(self, amount):
        if self.approval_threshold is None:
            return {"decision": "escalated"}
        approved = Decimal(str(amount)) <= self.approval_threshold
        return {"decision": "approved" if approved else "escalated"}


def test_approval_catches_the_split_payment_bypass():
    result = ApprovalThresholdProbe().run(PerRequestOnlyServer())
    finding = _finding(result, "Threshold is per-request only; splitting defeats it")
    assert finding.metric["auto_approved_total"] == "147"


def _boundary_attempt(result):
    return next(a for a in result.attempts if "(boundary)" in a.description)


def test_boundary_reading_matches_a_fresh_request():
    """The fixed mock approves exactly L on fresh state, so that is the reading.

    It tracks cumulative approvals, so reading L straight after approving
    L-1 would report 'escalated' and name the wrong side of L as inclusive.
    """
    result = ApprovalThresholdProbe().run(SafeCommerceServer())
    assert _boundary_attempt(result).actual == "approved"


class NoResetSafeServer:
    """The fixed mock without the optional reset() hook, like most real servers."""

    name = "mock://safe-commerce-no-reset"

    def __init__(self):
        self._server = SafeCommerceServer()

    def list_tools(self):
        return self._server.list_tools()

    def call_tool(self, tool, **kwargs):
        return self._server.call_tool(tool, **kwargs)


def test_boundary_reading_flags_when_state_could_not_be_reset():
    attempt = _boundary_attempt(ApprovalThresholdProbe().run(NoResetSafeServer()))
    assert attempt.state_after["fresh_state"] is False
    assert "no reset()" in attempt.description


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
