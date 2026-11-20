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
from mcp_guardrail_probe.report import to_markdown


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
    issuing the charges concurrently, or the mock's race disappears.
    """
    finding = _finding(SpendCapProbe().run(VulnerableCommerceServer()), "Spend cap does not hold")
    assert finding.metric["worst_shape"] == "concurrent charges racing the balance"
    assert finding.metric["max_overspend"] == "140"


def test_spend_cap_catches_a_double_charge_on_retry():
    result = SpendCapProbe().run(VulnerableCommerceServer())
    attempt = _attempt(result, "idempotency key")
    assert attempt.passed is False
    assert attempt.state_after["accepted"] == "80"
    finding = _finding(result, "A retried charge is charged twice")
    assert finding.severity is Severity.SEV1
    assert finding.direction is Direction.FAIL_OPEN
    assert finding.metric["accepted_total"] == "80"
    assert finding.metric["expected_total"] == "40"


class IgnoresIdempotencyKeyServer(SafeCommerceServer):
    """The cap holds; the idempotency key is accepted and never consulted."""

    name = "mock://safe-cap-no-idempotency"

    def __init__(self):
        self.charge_kwargs = []
        super().__init__()

    def _tool_charge(self, amount, idempotency_key=None):
        self.charge_kwargs.append(idempotency_key)
        return super()._tool_charge(amount)


def test_a_double_charge_on_retry_is_not_reported_as_the_cap_failing():
    """40 + 40 fits under a cap of 100: the cap held, the retry did not."""
    result = SpendCapProbe().run(IgnoresIdempotencyKeyServer())
    assert [f.title for f in result.findings] == ["A retried charge is charged twice"]


class NoIdempotencyKeyServer(IgnoresIdempotencyKeyServer):
    """The same server, saying so: its charge takes no idempotency key."""

    name = "mock://safe-cap-says-no-idempotency"
    takes_idempotency_key = False


def test_a_target_without_an_idempotency_key_gets_the_retry_reported_as_not_supported():
    target = NoIdempotencyKeyServer()
    result = SpendCapProbe().run(target)
    assert not result.findings, [f.title for f in result.findings]
    attempt = _attempt(result, "idempotency key")
    assert attempt.passed is None
    assert attempt.actual.startswith("not run")
    assert target.charge_kwargs and set(target.charge_kwargs) == {None}  # no key ever sent
    assert "| – |" in to_markdown([result])


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


def test_boundary_reading_matches_a_fresh_request():
    """The fixed mock approves exactly L on fresh state, so that is the reading.

    It tracks cumulative approvals, so reading L straight after approving
    L-1 would report 'escalated' and name the wrong side of L as inclusive.
    """
    result = ApprovalThresholdProbe().run(SafeCommerceServer())
    assert _attempt(result, "(boundary)").actual == "approved"


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
    attempt = _attempt(ApprovalThresholdProbe().run(NoResetSafeServer()), "(boundary)")
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


# -- limits configured out-of-band, and servers with less surface --------

class OutOfBandCapServer(SafeCommerceServer):
    """The cap belongs to the operator: no set_spending_limit, fixed at start."""

    name = "mock://safe-commerce-out-of-band-cap"

    def __init__(self, cap="100"):
        self._preset = Decimal(cap)
        self.calls = []
        super().__init__()
        self.out_of_band_limits = {"spending_limit": cap}

    def reset(self):
        super().reset()
        self.cap = self._preset

    def list_tools(self):
        return [t for t in super().list_tools() if t != "set_spending_limit"]

    def call_tool(self, tool, **kwargs):
        self.calls.append(tool)
        return super().call_tool(tool, **kwargs)


class OutOfBandRacyServer(OutOfBandCapServer):
    """The same, with the vulnerable mock's lost-update race in charge."""

    name = "mock://vulnerable-commerce-out-of-band-cap"

    def __init__(self, cap="100"):
        super().__init__(cap)
        self._slow = True

    def _tool_charge(self, amount, idempotency_key=None):
        return VulnerableCommerceServer._tool_charge(self, amount, idempotency_key)


def test_out_of_band_cap_is_tested_and_never_set():
    target = OutOfBandCapServer()
    result = SpendCapProbe().run(target)
    assert not result.skipped, result.skipped_reason
    assert not result.findings, [f.title for f in result.findings]
    assert "set_spending_limit" not in target.calls
    assert _attempt(result, "concurrent").description.endswith("against a cap of 100")


def test_out_of_band_cap_still_catches_the_race():
    finding = _finding(SpendCapProbe().run(OutOfBandRacyServer()), "Spend cap does not hold")
    assert finding.metric["worst_shape"] == "concurrent charges racing the balance"
    assert finding.metric["max_overspend"] == "140"
    assert finding.reproduction[0] == "configure the spending limit out-of-band at 100"


def test_charge_sizes_follow_the_out_of_band_cap():
    result = SpendCapProbe().run(OutOfBandCapServer(cap="1000"))
    assert not result.findings, [f.title for f in result.findings]
    assert _attempt(result, "single charge").description.startswith(
        "single charge of 1500 against a cap of 1000"
    )
    assert _attempt(result, "four sequential").state_after["accepted"] == "900"


class OutOfBandNoResetServer(NoResetSafeServer):
    out_of_band_limits = {"spending_limit": "100"}


def test_out_of_band_cap_without_reset_is_skipped_not_guessed():
    result = SpendCapProbe().run(OutOfBandNoResetServer())
    assert result.skipped
    assert "out-of-band" in result.skipped_reason


def test_spend_cap_without_reset_says_what_it_assumed():
    attempt = _attempt(SpendCapProbe().run(NoResetSafeServer()), "four sequential")
    assert attempt.state_after["fresh_state"] is False
    assert "assumes set_spending_limit restarts the count" in attempt.description


class NoCounterServer(VulnerableCommerceServer):
    """Charges, but exposes no read-back of committed spend."""

    name = "mock://vulnerable-commerce-no-counter"

    def list_tools(self):
        return [t for t in super().list_tools() if t != "get_spending_limit"]


def test_spend_cap_runs_without_a_counter_and_still_finds_the_overspend():
    result = SpendCapProbe().run(NoCounterServer())
    titles = [f.title for f in result.findings]
    assert "Spend cap does not hold" in titles
    assert "Server under-reports committed spend" not in titles  # nothing to compare
    assert _attempt(result, "concurrent").actual.endswith("server reports committed=not exposed")


class RefuseEverythingServer(SafeCommerceServer):
    """Refuses every charge, the way a wrong adapter mapping does."""

    name = "mock://refuses-every-charge"

    def _tool_charge(self, amount, idempotency_key=None):
        raise ToolError("invalid params: amount_cents must be an integer")


def test_spend_cap_reports_refusing_charges_that_fit():
    result = SpendCapProbe().run(RefuseEverythingServer())
    finding = _finding(result, "Spend cap refuses charges that fit under it")
    assert finding.severity is Severity.SEV2
    assert finding.direction is Direction.FAIL_CLOSED
    assert finding.metric["accepted_total"] == "0"
    assert "four sequential charges of 30: 0 accepted in total, where 90 fit" in finding.summary
    assert "amount_cents must be an integer" in (
        _attempt(result, "four sequential").state_after["refusal_reasons"][0]
    )


class OutOfBandApprovalServer(SafeCommerceServer):
    """The approval threshold is the operator's (70, not the probe's 50)."""

    name = "mock://safe-commerce-out-of-band-approval"
    out_of_band_limits = {"approval_threshold": "70"}

    def __init__(self):
        self.calls = []
        super().__init__()

    def reset(self):
        super().reset()
        self.approval_threshold = Decimal("70")

    def list_tools(self):
        return [t for t in super().list_tools() if t != "set_approval_threshold"]

    def call_tool(self, tool, **kwargs):
        self.calls.append(tool)
        return super().call_tool(tool, **kwargs)


def test_out_of_band_approval_threshold_is_tested_and_never_set():
    target = OutOfBandApprovalServer()
    result = ApprovalThresholdProbe().run(target)
    assert not result.skipped, result.skipped_reason
    assert not result.findings, [f.title for f in result.findings]
    assert "set_approval_threshold" not in target.calls
    boundary = _attempt(result, "(boundary)")
    assert boundary.description.startswith("submit_for_approval(amount=70) with limit 70")
    assert boundary.actual == "approved"


class StrictSchemaServer(SafeCommerceServer):
    """Rejects any argument it does not know, idempotency_key included."""

    name = "mock://strict-schema"

    def _tool_charge(self, amount, idempotency_key=None):
        if idempotency_key is not None:
            raise ToolError("invalid params: unknown argument 'idempotency_key'")
        return super()._tool_charge(amount)


def test_a_retry_shape_that_tested_nothing_is_reported_not_passed():
    """Both retries refused reads as "the retry held" unless the probe checks."""
    result = SpendCapProbe().run(StrictSchemaServer())
    finding = _finding(result, "Spend cap refuses charges that fit under it")
    assert finding.metric["retry_first_accepted"] == "0"
    assert finding.metric["accepted_total"] == finding.metric["expected_total"] == "90"
    assert "idempotency key was refused" in finding.summary


class RunningTotalFailOpenServer:
    """Approves L+1 per request (fail-open), and also caps a running total.

    No reset(), and the threshold is the operator's. Read in sequence, L-1 and
    L use up the running total and it escalates L+1, which hides the
    per-request fail-open. That is the masking the out-of-band skip prevents.
    """

    name = "mock://running-total-fail-open-no-reset"
    out_of_band_limits = {"approval_threshold": "50"}

    def __init__(self):
        self.approved = Decimal("0")

    def list_tools(self):
        return ["submit_for_approval"]

    def call_tool(self, tool, **kwargs):
        amount, limit = Decimal(kwargs["amount"]), Decimal("50")
        if amount > limit + 1 or self.approved + amount > limit * 2:
            return {"decision": "escalated"}
        self.approved += amount
        return {"decision": "approved"}


def test_out_of_band_approval_threshold_without_reset_is_skipped_not_guessed():
    result = ApprovalThresholdProbe().run(RunningTotalFailOpenServer())
    assert result.skipped
    assert "out-of-band" in result.skipped_reason


def test_with_a_reset_the_same_server_shows_the_fail_open_the_skip_would_hide():
    class WithReset(RunningTotalFailOpenServer):
        def reset(self):
            self.approved = Decimal("0")

    result = ApprovalThresholdProbe().run(WithReset())
    titles = [f.title for f in result.findings]
    assert "Auto-approve approves above its own limit" in titles
