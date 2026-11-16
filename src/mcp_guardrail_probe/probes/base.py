"""Probe base class.

A probe declares the tools it needs. If the target does not expose them the
probe skips with a reason rather than guessing at equivalents — a probe that
silently tests the wrong tool produces a clean report and a false sense of
safety, which is worse than no report.
"""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from typing import Any, Iterator

from ..model import Attempt, Finding, ProbeResult, Target


class Probe(ABC):
    name: str = "probe"
    description: str = ""
    required_tools: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._counter: Iterator[int] = itertools.count(1)
        self._attempts: list[Attempt] = []

    # -- helpers -------------------------------------------------------

    def record(
        self,
        description: str,
        expected: str,
        actual: str,
        passed: bool,
        state_after: dict[str, Any] | None = None,
    ) -> Attempt:
        attempt = Attempt(
            attempt_id=f"{self.name}-{next(self._counter):03d}",
            description=description,
            expected=expected,
            actual=actual,
            passed=passed,
            state_after=state_after or {},
        )
        self._attempts.append(attempt)
        return attempt

    def missing_tools(self, target: Target) -> list[str]:
        available = set(target.list_tools())
        return [t for t in self.required_tools if t not in available]

    # -- entry point ---------------------------------------------------

    def run(self, target: Target) -> ProbeResult:
        missing = self.missing_tools(target)
        if missing:
            return ProbeResult(
                probe=self.name,
                target=target.name,
                skipped_reason=(
                    "target does not expose " + ", ".join(missing)
                ),
            )
        findings = self.execute(target)
        return ProbeResult(
            probe=self.name,
            target=target.name,
            findings=findings,
            attempts=list(self._attempts),
        )

    @abstractmethod
    def execute(self, target: Target) -> list[Finding]:
        """Run the probe and return findings. Record every attempt."""
        raise NotImplementedError
