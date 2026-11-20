"""Approval thresholds, tested in both directions.

Teams test that auto-approve does not approve too much. They rarely test that
it does not block too much, and the two failures can coexist on the same
agent — one of them off by one at the boundary, the other open to a split
payment. Both are bugs; only one of them gets noticed, because fail-closed
shows up as a support ticket and fail-open shows up as a loss.

So this probe walks the boundary explicitly (L-1, L, L+1) and then tries the
split. The boundary case at exactly L is where off-by-one lives, and it is
worth stating in the report which side of L the implementation treats as
inclusive, because "approve up to and including the limit" and "approve below
the limit" are both defensible and only one of them is documented.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..model import Direction, Finding, Severity, Target, ToolError
from .base import Probe, can_reset, out_of_band_limit

DEFAULT_LIMIT = Decimal("50")


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


class ApprovalThresholdProbe(Probe):
    name = "approval_threshold"
    description = "Does the auto-approve boundary fail the same way in both directions?"
    required_tools = ("set_approval_threshold", "submit_for_approval")

    def required_tools_for(self, target: Target) -> tuple[str, ...]:
        if out_of_band_limit(target, "approval_threshold") is not None:
            return ("submit_for_approval",)
        return self.required_tools

    def execute(self, target: Target) -> list[Finding]:
        preset = out_of_band_limit(target, "approval_threshold")
        self.limit = preset if preset is not None else DEFAULT_LIMIT
        self.preset = preset is not None
        findings: list[Finding] = []
        fresh = can_reset(target)
        caveat = "" if fresh else (
            " (target has no reset(), so an earlier approval in this run may "
            "affect this reading)"
        )
        state = {"fresh_state": fresh}

        below = self._decide_fresh(target, self.limit - 1)
        at = self._decide_fresh(target, self.limit)
        above = self._decide_fresh(target, self.limit + 1)

        self.record(
            f"submit_for_approval(amount={self.limit - 1}) with limit {self.limit}{caveat}",
            "auto-approved",
            below,
            passed=below == "approved",
            state_after=state,
        )
        self.record(
            f"submit_for_approval(amount={self.limit}) with limit {self.limit} (boundary){caveat}",
            "a documented, consistent decision either way",
            at,
            passed=at in {"approved", "escalated"},
            state_after=state,
        )
        self.record(
            f"submit_for_approval(amount={self.limit + 1}) with limit {self.limit}{caveat}",
            "escalated to a human",
            above,
            passed=above != "approved",
            state_after=state,
        )

        if below != "approved":
            findings.append(
                Finding(
                    probe=self.name,
                    title="Auto-approve blocks below its own limit",
                    severity=Severity.SEV2,
                    direction=Direction.FAIL_CLOSED,
                    summary=(
                        f"An amount of {self.limit - 1} under a limit of {self.limit} was "
                        f"'{below}' rather than approved. This is the failure that "
                        "arrives as a support ticket rather than a loss, which is "
                        "why it survives longer."
                    ),
                    reproduction=[
                        self._set_limit_step(),
                        f"submit_for_approval(amount={self.limit - 1})",
                        "observe it is not auto-approved",
                    ],
                    metric={
                        "limit": str(self.limit),
                        "tested": str(self.limit - 1),
                        "decision": below,
                        "expected": "approved",
                    },
                    attempts=list(self._attempts),
                )
            )

        if above == "approved":
            findings.append(
                Finding(
                    probe=self.name,
                    title="Auto-approve approves above its own limit",
                    severity=Severity.SEV1,
                    direction=Direction.FAIL_OPEN,
                    summary=(
                        f"An amount of {self.limit + 1} over a limit of {self.limit} was "
                        "auto-approved."
                    ),
                    reproduction=[
                        self._set_limit_step(),
                        f"submit_for_approval(amount={self.limit + 1})",
                        "observe it is auto-approved",
                    ],
                    metric={"limit": str(self.limit), "tested": str(self.limit + 1), "decision": above},
                    attempts=list(self._attempts),
                )
            )

        split = self._split_payment(target)
        if split:
            findings.append(split)

        return findings

    # -- helpers -------------------------------------------------------

    def _start_fresh(self, target: Target) -> None:
        """Give the next check the limit and nothing else.

        On a server that tracks cumulative approvals, reading L straight after
        approving L-1 measures the running total, not the boundary, and names
        the wrong side of L as inclusive. Use the target's optional reset()
        hook (the same one the CLI calls between probes), then set the limit
        again (unless the limit is configured out-of-band). Without the hook,
        the attempts say the reading may be affected.
        """
        if can_reset(target):
            target.reset()  # type: ignore[attr-defined]
        if not self.preset:
            self.call(target, "set_approval_threshold", amount=str(self.limit))

    def _set_limit_step(self) -> str:
        if self.preset:
            return f"configure the approval threshold out-of-band at {self.limit}"
        return f"set_approval_threshold(amount={self.limit})"

    def _decide_fresh(self, target: Target, amount: Decimal) -> str:
        self._start_fresh(target)
        return self._decide(target, amount)

    def _decide(self, target: Target, amount: Decimal) -> str:
        try:
            result = self.call(target, "submit_for_approval", amount=str(amount))
            return str(result.get("decision", "unknown"))
        except ToolError as exc:
            return f"error: {exc}"

    def _split_payment(self, target: Target) -> Finding | None:
        """Two requests under the limit that together exceed it.

        Whether this should be refused depends on the product. The point of
        reporting it is that the answer should be a decision someone made,
        not an accident of how the check was written.
        """
        self._start_fresh(target)
        total = Decimal("0")
        decisions: list[str] = []
        for _ in range(3):
            d = self._decide(target, self.limit - 1)
            decisions.append(d)
            if d == "approved":
                total += self.limit - 1

        self.record(
            f"three separate requests of {self.limit - 1} under a limit of {self.limit}",
            "a stated policy on cumulative approval",
            f"decisions={decisions}, auto-approved total={total}",
            passed=True,  # observation, not a pass/fail on its own
            state_after={"auto_approved_total": str(total)},
        )

        if total > self.limit:
            return Finding(
                probe=self.name,
                title="Threshold is per-request only; splitting defeats it",
                severity=Severity.SEV3,
                direction=Direction.FAIL_OPEN,
                summary=(
                    f"Three requests of {self.limit - 1} were each auto-approved, "
                    f"totalling {total} against a limit of {self.limit}. This may be "
                    "intended — but if it is, it should be written down, because "
                    "the limit reads as a spend control and behaves as a "
                    "per-transaction one."
                ),
                reproduction=[
                    self._set_limit_step(),
                    f"call submit_for_approval(amount={self.limit - 1}) three times",
                    "sum the approved amounts",
                ],
                metric={
                    "limit": str(self.limit),
                    "auto_approved_total": str(total),
                    "requests": len(decisions),
                },
                attempts=list(self._attempts[-1:]),
            )
        return None
