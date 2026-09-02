"""Mode contracts and independent Router compatibility checks.

Semantic classification is performed by the pre-search PydanticAI Router. This
module only handles an explicit operator override, validates the Router's
classification, and provides a provisional value for legacy callers that have
not yet invoked the Router.
"""
from __future__ import annotations

from .models import IntentClassification, ModeDecision, ResearchRoute


def select_mode(brief: str, requested_mode: str = "auto") -> ModeDecision:
    """Return an explicit override or a provisional pre-Router decision.

    ``brief`` is intentionally not inspected. Production semantic intent
    classification belongs to the structured Router output, not keyword rules.
    The provisional value keeps older callers type-safe until they invoke the
    Router and replace it with :func:`mode_decision_from_intent`.
    """

    if requested_mode != "auto":
        return ModeDecision(requested_mode=requested_mode, selected_mode=requested_mode, confidence=1.0,
                            signals=["explicit_mode"])
    return ModeDecision(
        requested_mode="auto",
        selected_mode="investigation",
        confidence=0.0,
        signals=["router_semantic_classification_required"],
    )


def mode_decision_from_intent(
    intent: IntentClassification,
    requested_mode: str,
) -> ModeDecision:
    """Convert a Router intent into the host-owned mode decision."""

    if requested_mode not in {"auto", "investigation", "monitor", "verify"}:
        raise ValueError(f"unsupported requested mode: {requested_mode}")
    if intent.requested_mode != requested_mode:
        raise ValueError(
            f"Router intent requested_mode {intent.requested_mode} does not match {requested_mode}"
        )
    if requested_mode != "auto" and intent.selected_mode != requested_mode:
        raise ValueError(
            f"Router selected_mode {intent.selected_mode} conflicts with explicit mode {requested_mode}"
        )
    signals = list(intent.signals)
    if requested_mode != "auto":
        signals = ["explicit_mode", "router_confirmed", *signals]
    return ModeDecision(
        requested_mode=requested_mode,
        selected_mode=requested_mode if requested_mode != "auto" else intent.selected_mode,
        confidence=1.0 if requested_mode != "auto" else intent.confidence,
        signals=signals,
        router_validated=True,
    )


def mode_from_route(route: ResearchRoute) -> str:
    """Best-effort mode recovery for checkpoints created before intent storage."""

    if route.task_kind == "monitor":
        return "monitor"
    if route.task_kind == "verify":
        return "verify"
    return "investigation"


def route_matches_mode(mode: str, route: ResearchRoute) -> bool:
    allowed = {
        "investigation": {"extract", "compare", "deep_research"},
        "monitor": {"monitor"},
        "verify": {"verify", "extract"},
    }
    return route.task_kind in allowed[mode]
