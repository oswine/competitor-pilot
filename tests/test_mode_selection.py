from ci_agent.mode import route_matches_mode, select_mode
from ci_agent.models import ResearchRoute


def route(kind: str) -> ResearchRoute:
    return ResearchRoute(
        task_kind=kind,
        complexity="standard",
        required_agents=["researcher"],
        max_agent_calls=1,
        debate_required=False,
        reason="test",
    )


def test_auto_selects_investigation_for_open_ended_comparison() -> None:
    decision = select_mode("比较 Notion 与 Coda 的企业版价格、AI 功能和安全能力")
    assert decision.selected_mode == "investigation"
    assert route_matches_mode(decision.selected_mode, route("compare"))


def test_auto_selects_monitor_for_change_request() -> None:
    decision = select_mode("持续监控 Notion 企业版价格和 AI 功能的变化")
    assert decision.selected_mode == "monitor"
    assert route_matches_mode(decision.selected_mode, route("monitor"))
    assert not route_matches_mode(decision.selected_mode, route("compare"))


def test_one_off_change_question_stays_investigation() -> None:
    decision = select_mode("比较 Notion 与 Coda 最近的价格变化")
    assert decision.selected_mode == "investigation"


def test_auto_selects_verify_for_fact_check() -> None:
    decision = select_mode("验证 Coda 是否支持企业 SSO")
    assert decision.selected_mode == "verify"
    assert route_matches_mode(decision.selected_mode, route("verify"))


def test_explicit_mode_remains_an_operator_override() -> None:
    decision = select_mode("比较定价", "monitor")
    assert decision.selected_mode == "monitor"
    assert decision.confidence == 1.0
