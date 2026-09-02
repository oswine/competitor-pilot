from ci_agent.mode import mode_decision_from_intent, route_matches_mode, select_mode
from ci_agent.models import IntentClassification, ResearchRoute


def route(kind: str) -> ResearchRoute:
    return ResearchRoute(
        task_kind=kind,
        complexity="standard",
        required_agents=["researcher"],
        max_agent_calls=1,
        debate_required=False,
        reason="test",
    )


def test_auto_requires_router_semantic_classification() -> None:
    decision = select_mode("比较 Notion 与 Coda 的企业版价格、AI 功能和安全能力")
    assert decision.selected_mode == "investigation"
    assert decision.confidence == 0.0
    assert decision.signals == ["router_semantic_classification_required"]


def test_router_intent_selects_monitor_semantically() -> None:
    decision = mode_decision_from_intent(
        IntentClassification(
            requested_mode="auto",
            selected_mode="monitor",
            confidence=0.94,
            signals=["recurring_schedule", "change_alert"],
            rationale="The user asks for recurring change detection.",
        ),
        "auto",
    )
    assert decision.selected_mode == "monitor"
    assert decision.confidence == 0.94
    assert route_matches_mode(decision.selected_mode, route("monitor"))


def test_router_intent_distinguishes_one_off_change_from_monitoring() -> None:
    decision = mode_decision_from_intent(
        IntentClassification(
            requested_mode="auto",
            selected_mode="investigation",
            confidence=0.88,
            signals=["one_off_comparison"],
            rationale="The user asks for a current comparison, not recurring monitoring.",
        ),
        "auto",
    )
    assert decision.selected_mode == "investigation"


def test_router_intent_selects_verify_semantically() -> None:
    decision = mode_decision_from_intent(
        IntentClassification(
            requested_mode="auto",
            selected_mode="verify",
            confidence=0.91,
            signals=["fact_check"],
            rationale="The user asks whether a specified fact is true.",
        ),
        "auto",
    )
    assert decision.selected_mode == "verify"
    assert route_matches_mode(decision.selected_mode, route("verify"))


def test_explicit_mode_remains_an_operator_override() -> None:
    decision = select_mode("比较定价", "monitor")
    assert decision.selected_mode == "monitor"
    assert decision.confidence == 1.0


def test_router_intent_cannot_override_explicit_mode() -> None:
    intent = IntentClassification(
        requested_mode="monitor",
        selected_mode="investigation",
        confidence=0.9,
        rationale="conflicting model output",
    )
    try:
        mode_decision_from_intent(intent, "monitor")
    except ValueError as exc:
        assert "selected_mode" in str(exc)
    else:
        raise AssertionError("conflicting explicit mode should be rejected")


def test_router_intent_requested_mode_is_part_of_the_contract() -> None:
    intent = IntentClassification(
        requested_mode="auto",
        selected_mode="monitor",
        confidence=0.9,
        rationale="recurring change detection",
    )
    try:
        mode_decision_from_intent(intent, "monitor")
    except ValueError as exc:
        assert "requested_mode" in str(exc)
    else:
        raise AssertionError("mismatched requested mode should be rejected")
