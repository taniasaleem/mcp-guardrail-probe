from .approval_threshold import ApprovalThresholdProbe
from .base import Probe
from .injection import InjectionProbe
from .spend_cap import SpendCapProbe

#: Registry used by the CLI. Order is deliberate: the cheapest, most
#: destructive-if-true probe runs first, so a broken cap is visible before
#: the slower injection sweep finishes.
ALL_PROBES: dict[str, type[Probe]] = {
    SpendCapProbe.name: SpendCapProbe,
    ApprovalThresholdProbe.name: ApprovalThresholdProbe,
    InjectionProbe.name: InjectionProbe,
}

__all__ = ["Probe", "SpendCapProbe", "InjectionProbe", "ApprovalThresholdProbe", "ALL_PROBES"]
