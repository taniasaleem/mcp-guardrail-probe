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
from .base import Probe

LIMIT = Decimal("50")


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


class ApprovalThresholdProbe(Probe):
    name = "approval_threshold"
    description = "Does the auto-approve boundary fail the same way in both directions?"
    required_tools = ("set_approval_threshold", "submit_for_approval")

    def execute(self, target: Target) -> list[Finding]:
        target.call_tool("set_approval_threshold", amount=str(LIMIT))
        findings: list[Finding] = []

        below = self._decide(target, LIMIT - 1)
        at = self._decide(target, LIMIT)
        above = self._decide(target, LIMIT + 1)

        self.record(
            f"submit_for_approval(amount={LIMIT - 1}) with limit {LIMIT}",
            "auto-approved",
            below,
            passed=below == "approved",
        )
        self.record(
            f"submit_for_approval(amount={LIMIT}) with limit {LIMIT} (boundary)",
            "a documented, consistent decision either way",
            below if False else at,
            passed=at in {"approved", "escalated"},
        )
        self.record(
            f"submit_for_approval(amount={LIMIT + 1}) with limit {LIMIT}",
            "escalated to a human",
            above,
            passed=above != "approved",
        )

        if below != "approved":
            findings.append(
                Finding(
                    probe=self.name,
                    title="Auto-approve blocks below its own limit",
                    severity=Severity.SEV2,
                    direction=Direction.FAIL_CLOSED,
                    summary=(
                        f"An amount of {LIMIT - 1} under a limit of {LIMIT} was "
                        f"'{below}' rather than approved. This is the failure that "
                        "arrives as a support ticket rather than a loss, which is "
                        "why it survives longer."
                    ),
                    reproduction=[
                        f"set_approval_threshold(amount={LIMIT})",
                        f"submit_for_approval(amount={LIMIT - 1})",
                        "observe it is not auto-approved",
                    ],
                    metric={
                        "limit": str(LIMIT),
                        "tested": str(LIMIT - 1),
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
                        f"An amount of {LIMIT + 1} over a limit of {LIMIT} was "
                        "auto-approved."
                    ),
                    reproduction=[
                        f"set_approval_threshold(amount={LIMIT})",
                        f"submit_for_approval(amount={LIMIT + 1})",
                        "observe it is auto-approved",
                    ],
                    metric={"limit": str(LIMIT), "tested": str(LIMIT + 1), "decision": above},
                    attempts=list(self._attempts),
                )
            )

        split = self._split_payment(target)
        if split:
            findings.append(split)

        return findings

    # -- helpers -------------------------------------------------------

    def _decide(self, target: Target, amount: Decimal) -> str:
        try:
            result = target.call_tool("submit_for_approval", amount=str(amount))
            return str(result.get("decision", "unknown"))
        except ToolError as exc:
            return f"error: {exc}"

    def _split_payment(self, target: Target) -> Finding | None:
        """Two requests under the limit that together exceed it.

        Whether this should be refused depends on the product. The point of
        reporting it is that the answer should be a decision someone made,
        not an accident of how the check was written.
        """
        total = Decimal("0")
        decisions: list[str] = []
        for _ in range(3):
            d = self._decide(target, LIMIT - 1)
            decisions.append(d)
            if d == "approved":
                total += LIMIT - 1

        self.record(
            f"three separate requests of {LIMIT - 1} under a limit of {LIMIT}",
            "a stated policy on cumulative approval",
            f"decisions={decisions}, auto-approved total={total}",
            passed=True,  # observation, not a pass/fail on its own
            state_after={"auto_approved_total": str(total)},
        )

        if total > LIMIT:
            return Finding(
                probe=self.name,
                title="Threshold is per-request only; splitting defeats it",
                severity=Severity.SEV3,
                direction=Direction.FAIL_OPEN,
                summary=(
                    f"Three requests of {LIMIT - 1} were each auto-approved, "
                    f"totalling {total} against a limit of {LIMIT}. This may be "
                    "intended — but if it is, it should be written down, because "
                    "the limit reads as a spend control and behaves as a "
                    "per-transaction one."
                ),
                reproduction=[
                    f"set_approval_threshold(amount={LIMIT})",
                    f"call submit_for_approval(amount={LIMIT - 1}) three times",
                    "sum the approved amounts",
                ],
                metric={
                    "limit": str(LIMIT),
                    "auto_approved_total": str(total),
                    "requests": len(decisions),
                },
                attempts=list(self._attempts[-1:]),
            )
        return None
