"""Spend-cap integrity.

A spend cap is not a setting. It is a claim that total committed spend will
not exceed a number, and that claim has to survive three things teams rarely
test together:

  1. a single charge straight over the line
  2. several charges that individually fit and together do not
  3. concurrent charges racing the same remaining balance

A fourth shape runs alongside them: the same charge submitted twice under one
idempotency key, as a client does when it retries after a timeout. Both
charges fit under the cap, so it says nothing about the cap. A second
acceptance is reported as its own finding, "A retried charge is charged
twice", and never counted as overspend. A target whose charge takes no
idempotency key (`takes_idempotency_key = False`, or a mapping that drops the
key) has that shape reported as not supported rather than run.

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
wrong at the same moment the control is. A server that exposes no counter at
all (`get_spending_limit`) still gets the four shapes; only that cross-check
is skipped, and the attempts say so.

The cap is 100 unless the target says its cap is configured out-of-band (an
env var or operator API, not a tool the agent can call). Then the probe uses
that value, never calls `set_spending_limit`, and sizes every charge from it:
1.5x, 0.3x and 0.4x the cap, which is 150, 30 and 40 at the default.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

from ..model import Direction, Finding, Severity, Target, ToolError
from .base import Probe, can_reset, out_of_band_limit, takes_idempotency_key

DEFAULT_CAP = Decimal("100")

#: How many refusal messages an attempt keeps. Enough to tell a cap refusal
#: from an argument the server rejected, without pasting the whole run.
_MAX_REASONS = 3


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


class SpendCapProbe(Probe):
    name = "spend_cap"
    description = "Does the spend cap hold under concurrency, splitting and retries?"
    required_tools = ("set_spending_limit", "charge")
    optional_tools = ("get_spending_limit",)

    def required_tools_for(self, target: Target) -> tuple[str, ...]:
        if out_of_band_limit(target, "spending_limit") is not None:
            return ("charge",)
        return self.required_tools

    def skip_reason(self, target: Target) -> str | None:
        reason = super().skip_reason(target)
        if reason:
            return reason
        if out_of_band_limit(target, "spending_limit") is not None and not can_reset(target):
            # With no set_spending_limit call and no reset, every shape would
            # start from the spend the previous one left, and could no longer
            # tell a cap that held from a cap that was already used up.
            return (
                "the spending limit is configured out-of-band and the target has "
                "no reset(), so each attack shape would inherit the last one's spend"
            )
        return None

    def execute(self, target: Target) -> list[Finding]:
        preset = out_of_band_limit(target, "spending_limit")
        self.cap = preset if preset is not None else DEFAULT_CAP
        self.preset = preset is not None
        self.fresh = can_reset(target)
        self.reads_counter = "get_spending_limit" in set(target.list_tools())
        # Halves, fifths and tenths are exact in decimal: 150, 30 and 40 at the
        # default cap, and whole numbers for any cap that is a multiple of 10,
        # which a server counting integer minor units needs.
        self.over = self.cap * 3 / 2
        self.unit = self.cap * 3 / 10
        self.retry_unit = self.cap * 2 / 5
        self._refusals: list[str] = []
        #: The retry shape's first charge, or None when the shape did not run.
        self._retry_first: Decimal | None = None

        findings: list[Finding] = []
        worst = Decimal("0")
        worst_shape = ""
        drift: tuple[Decimal, Decimal] | None = None

        cap_readings = [shape(target) for shape in (self._single, self._split, self._concurrent)]
        retry = self._retry(target)
        for accepted, _, label in cap_readings:
            over = accepted - self.cap
            if over > worst:
                worst, worst_shape = over, label
        for accepted, reported, _ in cap_readings + ([retry] if retry else []):
            if reported is not None and accepted != reported and drift is None:
                drift = (accepted, reported)
        split_accepted = cap_readings[1][0]

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
                        self._set_cap_step(),
                        f"run the '{worst_shape}' shape from this probe",
                        "sum the charges the server ACCEPTED — do not read "
                        "committed_spend, which can under-report",
                        f"observe the accepted total exceeds {self.cap}",
                    ],
                    metric={
                        "cap": str(self.cap),
                        "max_overspend": str(worst),
                        "worst_shape": worst_shape,
                        "threshold": "0 — any overspend is sev1",
                    },
                    attempts=list(self._attempts),
                )
            )

        if retry is not None and retry[0] > self.retry_unit:
            # sev1, not a cap finding: the server was handed the control that
            # stops a duplicate payment and it did not hold, so money moved
            # twice for one payment. Both charges fit under the cap, which is
            # why this never feeds max_overspend above.
            accepted = retry[0]
            findings.append(
                Finding(
                    probe=self.name,
                    title="A retried charge is charged twice",
                    severity=Severity.SEV1,
                    direction=Direction.FAIL_OPEN,
                    summary=(
                        f"The same charge of {self.retry_unit}, submitted twice under one "
                        f"idempotency key, was accepted for {accepted} in total. The key "
                        "did not stop the second charge, so any client that retries "
                        "after a timeout pays again. If this server has no idempotency "
                        "key at all, say so in the mapping (drop idempotency_key) and "
                        "this shape is reported as not supported instead."
                    ),
                    reproduction=[
                        self._set_cap_step(),
                        f"charge(amount={self.retry_unit}, idempotency_key=K)",
                        f"charge(amount={self.retry_unit}, idempotency_key=K) again, same K",
                        f"observe {accepted} accepted in total, where {self.retry_unit} "
                        "was intended",
                    ],
                    metric={
                        "amount": str(self.retry_unit),
                        "accepted_total": str(accepted),
                        "expected_total": str(self.retry_unit),
                        "threshold": "any second acceptance is sev1",
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
                        self._set_cap_step(),
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

        # Charges that fit under a fresh cap must go through. Refusing them is
        # the cap failing closed, and it is also exactly what a wrong adapter
        # mapping looks like (every call rejected for a bad argument), which
        # would otherwise come back as a clean pass. The retry shape is checked
        # too: a server that rejects the idempotency key outright refuses both
        # charges, and "nothing accepted" would read as the retry holding.
        fits = self.unit * 3
        blocked: list[str] = []
        if split_accepted < fits:
            blocked.append(
                f"four sequential charges of {self.unit}: {split_accepted} accepted "
                f"in total, where {fits} fit"
            )
        if self._retry_first is not None and self._retry_first == 0:
            blocked.append(
                f"a first charge of {self.retry_unit} carrying an idempotency key was refused"
            )
        if blocked:
            findings.append(
                Finding(
                    probe=self.name,
                    title="Spend cap refuses charges that fit under it",
                    severity=Severity.SEV2,
                    direction=Direction.FAIL_CLOSED,
                    summary=(
                        f"Against a fresh cap of {self.cap}: " + "; ".join(blocked) + ". "
                        "Either the cap blocks spend it should allow, or the charges "
                        "never reached the cap check: read the refusal reasons in the "
                        "attempts. On a real server this is also what a wrong tool "
                        "mapping looks like, and the readings above it cannot be trusted "
                        "until it is fixed."
                    ),
                    reproduction=[
                        self._set_cap_step(),
                        f"charge(amount={self.unit}) four times, or "
                        f"charge(amount={self.retry_unit}, idempotency_key=...) once",
                        "observe a charge that fits is refused",
                    ],
                    metric={
                        "cap": str(self.cap),
                        "accepted_total": str(split_accepted),
                        "expected_total": str(fits),
                        "retry_first_accepted": (
                            "not run" if self._retry_first is None else str(self._retry_first)
                        ),
                    },
                    attempts=list(self._attempts),
                )
            )

        return findings

    # -- plumbing ------------------------------------------------------

    def _set_cap_step(self) -> str:
        if self.preset:
            return f"configure the spending limit out-of-band at {self.cap}"
        return f"set_spending_limit(amount={self.cap})"

    def _start_fresh(self, target: Target) -> None:
        """Start each shape from an empty ledger and the cap alone.

        Use the target's reset() when it has one. Without it, the in-band path
        relies on set_spending_limit restarting the count, and the attempt says
        so; the out-of-band path never gets here without a reset (see
        skip_reason).
        """
        self._refusals = []
        if self.fresh:
            target.reset()  # type: ignore[attr-defined]
        if not self.preset:
            self.call(target, "set_spending_limit", amount=str(self.cap))

    def _caveat(self) -> str:
        if self.fresh:
            return ""
        return " (target has no reset(), so this assumes set_spending_limit restarts the count)"

    def _reported(self, target: Target) -> Decimal | None:
        """The server's own claim about committed spend, or None if it makes none."""
        if not self.reads_counter:
            return None
        value = self.call(target, "get_spending_limit").get("committed_spend")
        return None if value is None else _d(value)

    def _charge(self, target: Target, amount: Decimal, idem: str | None = None) -> Decimal:
        """Return the amount actually accepted (0 if refused)."""
        kwargs: dict[str, Any] = {"amount": str(amount)}
        if idem:
            kwargs["idempotency_key"] = idem
        try:
            result = self.call(target, "charge", **kwargs)
        except ToolError as exc:
            reason = str(exc)[:200]
            if reason not in self._refusals:
                self._refusals.append(reason)
            return Decimal("0")
        # A well-behaved idempotent server reports 0 for a duplicate.
        if isinstance(result, dict) and "charged" in result:
            return _d(result["charged"])
        return amount

    def _record(
        self,
        description: str,
        expected: str,
        accepted: Decimal,
        reported: Decimal | None,
        passed: bool,
    ) -> None:
        if reported is not None:
            shown = str(reported)
        elif self.reads_counter:
            shown = "not reported (no committed_spend in get_spending_limit's result)"
        else:
            shown = "not exposed"
        self.record(
            description + self._caveat(),
            expected,
            f"accepted={accepted}, server reports committed={shown}",
            passed=passed,
            state_after={
                "accepted": str(accepted),
                "reported": None if reported is None else str(reported),
                "refusal_reasons": self._refusals[:_MAX_REASONS],
                "fresh_state": self.fresh,
            },
        )

    # -- attack shapes -------------------------------------------------

    def _single(self, target: Target) -> tuple[Decimal, Decimal | None, str]:
        self._start_fresh(target)
        accepted = self._charge(target, self.over)
        reported = self._reported(target)
        self._record(
            f"single charge of {self.over} against a cap of {self.cap}",
            "refused; nothing accepted",
            accepted,
            reported,
            passed=accepted <= self.cap,
        )
        return accepted, reported, "single over-limit charge"

    def _split(self, target: Target) -> tuple[Decimal, Decimal | None, str]:
        self._start_fresh(target)
        accepted = sum(
            (self._charge(target, self.unit) for _ in range(4)), Decimal("0")
        )
        reported = self._reported(target)
        self._record(
            f"four sequential charges of {self.unit} against a cap of {self.cap}",
            f"three accepted, the fourth refused; accepted total {self.unit * 3}",
            accepted,
            reported,
            passed=accepted <= self.cap,
        )
        return accepted, reported, "split into sub-limit charges"

    def _concurrent(self, target: Target) -> tuple[Decimal, Decimal | None, str]:
        self._start_fresh(target)
        with ThreadPoolExecutor(max_workers=8) as pool:
            amounts = list(pool.map(lambda _: self._charge(target, self.unit), range(8)))
        accepted = sum(amounts, Decimal("0"))
        reported = self._reported(target)
        self._record(
            f"eight concurrent charges of {self.unit} against a cap of {self.cap}",
            f"at most three accepted; accepted total {self.unit * 3}",
            accepted,
            reported,
            passed=accepted <= self.cap,
        )
        return accepted, reported, "concurrent charges racing the balance"

    def _retry(self, target: Target) -> tuple[Decimal, Decimal | None, str] | None:
        """Both charges fit under the cap, so only idempotency is on trial.

        None when the target's charge takes no idempotency key: the shape would
        then measure a second charge the server was never told was a retry.
        """
        if not takes_idempotency_key(target):
            self.record(
                "the same charge submitted twice under one idempotency key",
                f"{self.retry_unit} accepted in total; the retry is a no-op",
                "not run: the target's charge takes no idempotency key, so this "
                "shape is not supported (not a pass, not a failure)",
                passed=None,
                state_after={"supported": False},
            )
            return None
        self._start_fresh(target)
        key = str(uuid.uuid4())
        first = self._charge(target, self.retry_unit, idem=key)
        self._retry_first = first
        second = self._charge(target, self.retry_unit, idem=key)
        accepted = first + second
        reported = self._reported(target)
        self._record(
            f"the same charge of {self.retry_unit} submitted twice under one idempotency key",
            f"{self.retry_unit} accepted in total; the retry is a no-op",
            accepted,
            reported,
            passed=accepted <= self.retry_unit,
        )
        return accepted, reported, "retry under one idempotency key"
