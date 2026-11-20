"""Core types.

Design note: a Finding is only ever constructed from observed behaviour.
Nothing in this file lets a probe report a problem because a *setting*
looked wrong. A spend cap can report itself active after enforcement has
already failed, so config read-back is treated as a claim to be tested, never
as evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class Severity(str, Enum):
    """Severity is about what the failure lets through, not how clever it was.

    SEV1 is reserved for a guardrail that does not hold: money moves past a
    limit, or injected text changes what the agent does. There is no partial
    credit and no "but the margin was small" discount.
    """

    SEV1 = "sev1"  # a control that should hold, did not
    SEV2 = "sev2"  # a control held, but blocked something it should have allowed
    SEV3 = "sev3"  # observable inconsistency, no incorrect outcome yet


class Direction(str, Enum):
    """Guardrails fail in two directions and teams usually test one."""

    FAIL_OPEN = "fail_open"    # permitted something it should have refused
    FAIL_CLOSED = "fail_closed"  # refused something it should have permitted


@dataclass
class Attempt:
    """One probe interaction, recorded whether or not it found anything.

    `state_after` is read back *after* the call. Reading before is how you
    end up trusting a cap that is no longer enforced.

    `passed` is None for a check the target does not support, so it was not
    run. That is neither a pass nor a failure, and the report says so.
    """

    attempt_id: str
    description: str
    expected: str
    actual: str
    passed: bool | None
    state_after: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    probe: str
    title: str
    severity: Severity
    direction: Direction | None
    summary: str
    reproduction: list[str]
    metric: dict[str, Any] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["direction"] = self.direction.value if self.direction else None
        return d


@dataclass
class ProbeResult:
    probe: str
    target: str
    skipped_reason: str | None = None
    findings: list[Finding] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "target": self.target,
            "skipped_reason": self.skipped_reason,
            "findings": [f.to_dict() for f in self.findings],
            "attempts": [a.to_dict() for a in self.attempts],
        }


class ToolError(Exception):
    """Raised by a target when the server refuses or errors on a tool call.

    A refusal is a result, not a crash: probes catch this and record it.
    """

    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


@runtime_checkable
class Target(Protocol):
    """The surface a probe is allowed to touch.

    Deliberately small. A probe may list the tools and call them. It may not
    reach into server internals, because the whole point is to test what a
    model can actually reach.

    Three optional extras, which probes look for and do without:

    - `reset()`: put the server back to a fresh state. Probes call it between
      readings so one reading cannot leak into the next.
    - `out_of_band_limits`: a dict with `spending_limit` and/or
      `approval_threshold`, for a server whose limit is set by its operator
      (env var, config, admin API) rather than by a tool the agent can call.
      The probe then tests that value and does not try to set its own.
    - `takes_idempotency_key`: False when the server's `charge` has no
      idempotency key. The spend probe then reports its retry shape as not
      supported instead of sending a key the server never reads.
    """

    name: str

    def list_tools(self) -> list[str]:
        ...

    def call_tool(self, tool: str, **kwargs: Any) -> Any:
        ...


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)
