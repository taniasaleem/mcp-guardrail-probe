"""Spend-cap integrity.

A spend cap is not a setting. It is a claim that total committed spend will
not exceed a number, and that claim has to survive four things teams rarely
test together:

  1. a single charge straight over the line
  2. several charges that individually fit and together do not
  3. concurrent charges racing the same remaining balance
  4. a retry of a charge that already committed

**The probe keeps its own ledger.** It sums the charges the server *accepted*
rather than reading the server's `committed_spend`, and this is the single
most important design decision in the file. A read-then-write cap check with
no lock loses updates: eight concurrent charges of 30 against a cap of 100 can
all be accepted — 240 of real money — while the server's own counter reports
30 and its cap still reports itself active. A probe that trusted that counter
would pass. The first version of this file did exactly that, and passed
against a server known to be broken.

So the rule is: the client counts what it was allowed to do, and the server's
self-report is a separate claim to be checked against it. Those two numbers
disagreeing is its own finding, because it means the operator's dashboard is
wrong at the same moment the control is.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

from ..model import Direction, Finding, Severity, Target, ToolError
from .base import Probe

CAP = Decimal("100")


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


class SpendCapProbe(Probe):
    name = "spend_cap"
    description = "Does the spend cap hold under concurrency, splitting and retries?"
    required_tools = ("set_spending_limit", "get_spending_limit", "charge")

    def execute(self, target: Target) -> list[Finding]:
        findings: list[Finding] = []
        worst = Decimal("0")
        worst_shape = ""
        drift: tuple[Decimal, Decimal] | None = None

        for shape in (self._single, self._split, self._concurrent, self._retry):
            accepted, reported, label, budget = shape(target)
            over = accepted - budget
            if over > worst:
                worst, worst_shape = over, label
            if accepted != reported and drift is None:
                drift = (accepted, reported)

        if worst > 0:
            findings.append(
                Finding(
                    probe=self.name,
                    title="Spend cap does not hold",
                    severity=Severity.SEV1,
                    direction=Direction.FAIL_OPEN,
                    summary=(
                        f"The server accepted charges totalling {worst} more than it "
                        f"should have ({worst_shape}). A cap that can be crossed is "
                        "not a cap, and the margin is irrelevant because the next "
                        "caller chooses the margin."
                    ),
                    reproduction=[
                        f"set_spending_limit(amount={CAP})",
                        f"run the '{worst_shape}' shape from this probe",
                        "sum the charges the server ACCEPTED — do not read "
                        "committed_spend, which can under-report",
                        f"observe the accepted total exceeds {CAP}",
                    ],
                    metric={
                        "cap": str(CAP),
                        "max_overspend": str(worst),
                        "worst_shape": worst_shape,
                        "threshold": "0 — any overspend is sev1",
                    },
                    attempts=list(self._attempts),
                )
            )

        if drift is not None:
            accepted, reported = drift
            findings.append(
                Finding(
                    probe=self.name,
                    title="Server under-reports committed spend",
                    severity=Severity.SEV1 if accepted > reported else Severity.SEV3,
                    direction=Direction.FAIL_OPEN if accepted > reported else None,
                    summary=(
                        f"The server accepted {accepted} but reports committed_spend "
                        f"of {reported}. Any dashboard, alert or downstream check "
                        "reading that field is wrong by the difference, which is "
                        "why a cap can fail silently for a long time."
                    ),
                    reproduction=[
                        f"set_spending_limit(amount={CAP})",
                        "issue concurrent charges",
                        "compare the sum of accepted charges to get_spending_limit()"
                        ".committed_spend",
                    ],
                    metric={
                        "accepted_total": str(accepted),
                        "reported_committed": str(reported),
                        "discrepancy": str(accepted - reported),
                    },
                    attempts=list(self._attempts),
                )
            )

        return findings

    # -- plumbing ------------------------------------------------------

    def _reset(self, target: Target) -> None:
        target.call_tool("set_spending_limit", amount=str(CAP))

    def _reported(self, target: Target) -> Decimal:
        return _d(target.call_tool("get_spending_limit").get("committed_spend", 0))

    def _charge(self, target: Target, amount: str, idem: str | None = None) -> Decimal:
        """Return the amount actually accepted (0 if refused)."""
        kwargs: dict[str, Any] = {"amount": amount}
        if idem:
            kwargs["idempotency_key"] = idem
        try:
            result = target.call_tool("charge", **kwargs)
        except ToolError:
            return Decimal("0")
        # A well-behaved idempotent server reports 0 for a duplicate.
        if isinstance(result, dict) and "charged" in result:
            return _d(result["charged"])
        return _d(amount)

    # -- attack shapes -------------------------------------------------

    def _single(self, target: Target) -> tuple[Decimal, Decimal, str, Decimal]:
        self._reset(target)
        accepted = self._charge(target, "150")
        reported = self._reported(target)
        self.record(
            "single charge of 150 against a cap of 100",
            "refused; nothing accepted",
            f"accepted={accepted}, server reports committed={reported}",
            passed=accepted <= CAP,
            state_after={"accepted": str(accepted), "reported": str(reported)},
        )
        return accepted, reported, "single over-limit charge", CAP

    def _split(self, target: Target) -> tuple[Decimal, Decimal, str, Decimal]:
        self._reset(target)
        accepted = sum((self._charge(target, "30") for _ in range(4)), Decimal("0"))
        reported = self._reported(target)
        self.record(
            "four sequential charges of 30 against a cap of 100",
            "three accepted, the fourth refused; accepted total 90",
            f"accepted={accepted}, server reports committed={reported}",
            passed=accepted <= CAP,
            state_after={"accepted": str(accepted), "reported": str(reported)},
        )
        return accepted, reported, "split into sub-limit charges", CAP

    def _concurrent(self, target: Target) -> tuple[Decimal, Decimal, str, Decimal]:
        self._reset(target)
        with ThreadPoolExecutor(max_workers=8) as pool:
            amounts = list(pool.map(lambda _: self._charge(target, "30"), range(8)))
        accepted = sum(amounts, Decimal("0"))
        reported = self._reported(target)
        self.record(
            "eight concurrent charges of 30 against a cap of 100",
            "at most three accepted; accepted total 90",
            f"accepted={accepted}, server reports committed={reported}",
            passed=accepted <= CAP,
            state_after={"accepted": str(accepted), "reported": str(reported)},
        )
        return accepted, reported, "concurrent charges racing the balance", CAP

    def _retry(self, target: Target) -> tuple[Decimal, Decimal, str, Decimal]:
        """Both charges fit under the cap, so only idempotency is on trial."""
        self._reset(target)
        key = str(uuid.uuid4())
        first = self._charge(target, "40", idem=key)
        second = self._charge(target, "40", idem=key)
        accepted = first + second
        reported = self._reported(target)
        self.record(
            "the same charge of 40 submitted twice under one idempotency key",
            "40 accepted in total; the retry is a no-op",
            f"accepted={accepted}, server reports committed={reported}",
            passed=accepted <= Decimal("40"),
            state_after={"accepted": str(accepted), "reported": str(reported)},
        )
        # The budget for this shape is one charge, not the cap.
        return accepted, reported, "retry under one idempotency key", Decimal("40")
