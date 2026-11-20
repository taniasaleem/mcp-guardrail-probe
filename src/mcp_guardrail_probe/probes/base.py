"""Probe base class.

A probe declares the tools it needs. If the target does not expose them the
probe skips with a reason rather than guessing at equivalents — a probe that
silently tests the wrong tool produces a clean report and a false sense of
safety, which is worse than no report.
"""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Iterator

from ..model import Attempt, Finding, ProbeResult, Target, ToolError


def can_reset(target: Target) -> bool:
    """True when the target offers the optional reset() hook."""
    return callable(getattr(target, "reset", None))


def out_of_band_limit(target: Target, key: str) -> Decimal | None:
    """A limit the operator configured outside the tool surface, if any.

    Some servers take their cap or approval threshold from an env var or an
    operator API, not from a tool the agent can call. Such a target declares
    the value in `out_of_band_limits` (keys `spending_limit` and
    `approval_threshold`), and the probe tests against it instead of setting
    its own.
    """
    limits = getattr(target, "out_of_band_limits", None) or {}
    value = limits.get(key)
    return None if value is None else Decimal(str(value))


class Probe(ABC):
    name: str = "probe"
    description: str = ""
    required_tools: tuple[str, ...] = ()
    #: Tools the probe uses when present and works without.
    optional_tools: tuple[str, ...] = ()

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

    def call(self, target: Target, tool: str, **kwargs: Any) -> Any:
        """Call a tool, treating an MCP-style in-band error as a refusal.

        MCP reports a failed tool call inside the result (`isError: true`)
        rather than as an exception. Probes count anything that is not a
        refusal as accepted, so an adapter that passes such a result through
        would otherwise turn a refused charge into an accepted one, and a cap
        that held into a sev1. Every probe goes through here, never straight
        to `target.call_tool`.
        """
        result = target.call_tool(tool, **kwargs)
        if isinstance(result, dict) and result.get("isError"):
            text = " ".join(
                str(block.get("text", ""))
                for block in result.get("content") or []
                if isinstance(block, dict)
            ).strip()
            raise ToolError(text or f"{tool} returned isError", result)
        return result

    def required_tools_for(self, target: Target) -> tuple[str, ...]:
        """The tools this probe needs on this target. Usually `required_tools`."""
        return self.required_tools

    def missing_tools(self, target: Target) -> list[str]:
        available = set(target.list_tools())
        return [t for t in self.required_tools_for(target) if t not in available]

    def skip_reason(self, target: Target) -> str | None:
        """Why this probe cannot give a trustworthy reading here, or None."""
        missing = self.missing_tools(target)
        if missing:
            return "target does not expose " + ", ".join(missing)
        return None

    # -- entry point ---------------------------------------------------

    def run(self, target: Target) -> ProbeResult:
        reason = self.skip_reason(target)
        if reason:
            return ProbeResult(
                probe=self.name,
                target=target.name,
                skipped_reason=reason,
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
