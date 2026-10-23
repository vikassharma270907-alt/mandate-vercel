"""Mandate — intent-bound purchase agent (sandbox prototype)."""

from __future__ import annotations

__version__ = "2.2.0"

from .application import MandateApplication
from .mandate_policy_engine import (
    POLICY_VERSION,
    CashContext,
    Flow,
    FlowKind,
    PurchaseTerms,
    evaluate_live_plan,
    evaluate_policy,
    policy_output,
)

__all__ = [
    "__version__",
    "POLICY_VERSION",
    "CashContext",
    "Flow",
    "FlowKind",
    "PurchaseTerms",
    "evaluate_policy",
    "evaluate_live_plan",
    "policy_output",
    "MandateApplication",
]
