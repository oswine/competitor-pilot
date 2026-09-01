"""Deterministic execution policy for the LLM Router's advisory output.

The Router identifies the task, but the host owns the security and cost
boundary.  This module turns a route into an executable specialist plan and
therefore prevents a model from silently adding agents or skipping the final
quality gate.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from .models import DebatePlan, ResearchRoute


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def build_debate_plan(
    route: ResearchRoute,
    *,
    mode: str,
    dimensions: Iterable[str],
    brief: str,
) -> DebatePlan:
    """Build a bounded plan from task semantics, not untrusted model counts.

    At most two domain specialists are run.  A high-risk task receives one
    specialist plus an adversarial challenger so that the pre-arbitration
    budget stays within four agent calls (researcher + experts + arbitrator).
    The Arbitrator is always the terminal decision gate.
    """

    dimension_list = list(dimensions)
    dim_text = " ".join(_norm(item) for item in dimension_list)
    dim_text_raw = " ".join(dimension_list).casefold()
    brief_text = _norm(brief)
    brief_text_raw = brief.casefold()

    def has_token(tokens: Iterable[str], *, normalized: str, raw: str) -> bool:
        return any((token in raw) or (_norm(token) and _norm(token) in normalized) for token in tokens)

    specialists: list[str] = []
    if has_token(("pricing", "price", "cost", "billing", "采购"), normalized=dim_text, raw=dim_text_raw):
        specialists.append("pricing")
    if has_token(("ai", "product", "integration", "capability", "feature"), normalized=dim_text, raw=dim_text_raw):
        specialists.append("product")
    if has_token(("security", "compliance", "governance", "privacy", "safety"), normalized=dim_text, raw=dim_text_raw):
        specialists.append("security")

    # Router hints can add a domain that the brief's dimensions name in a
    # different language, but never remove a dimension-derived specialist.
    for item in route.specialists:
        if item not in specialists:
            specialists.append(item)
    for item in route.required_agents:
        if item in {"pricing", "product", "security"} and item not in specialists:
            specialists.append(item)

    # Verification is narrow: one domain verifier is enough before the final
    # arbitrator.  Multi-dimensional enterprise work gets two independent
    # domain views.  Keep the list bounded for predictable cost.
    if mode == "verify":
        specialists = specialists[:1]
    else:
        specialists = specialists[:2]

    enterprise_signal = has_token(
        ("enterprise", "企业", "采购", "合规", "上线", "production", "highstakes"),
        normalized=brief_text,
        raw=brief_text_raw,
    )
    conflict_signal = route.debate_required or route.complexity == "high_stakes"

    challenger_required = False
    if conflict_signal or (enterprise_signal and specialists):
        challenger_required = True

    # researcher + specialists + challenger + arbitrator must be <= 4.
    if challenger_required and len(specialists) > 1:
        specialists = specialists[:1]

    calls = 1 + len(specialists) + int(challenger_required) + 1
    # A plan always has researcher and arbitrator, so calls is at least two.
    calls = max(2, min(4, calls))
    rationale_parts = [f"mode={mode}", f"complexity={route.complexity}"]
    rationale_parts.append("specialists=" + (",".join(specialists) or "none"))
    rationale_parts.append("challenger=" + ("required" if challenger_required else "optional"))
    rationale_parts.append("arbitrator=always")
    return DebatePlan(
        specialists=specialists,
        challenger_required=challenger_required,
        arbitrator_required=True,
        max_agent_calls=calls,
        rationale="; ".join(rationale_parts),
    )


def specialist_dimensions(specialist: str, dimensions: Iterable[str]) -> list[str]:
    """Return dimensions relevant to a specialist for its scoped prompt."""

    mapping = {
        "pricing": {"pricing", "price", "cost", "billing", "采购"},
        "product": {"ai", "product", "integration", "capability", "feature"},
        "security": {"security", "compliance", "governance", "privacy", "safety"},
    }
    tokens = mapping.get(specialist, set())
    normalized_tokens = {_norm(token) for token in tokens if _norm(token)}
    selected = [
        dimension
        for dimension in dimensions
        if dimension.casefold() in tokens or _norm(dimension) in normalized_tokens
    ]
    return selected or list(dimensions)
