from ci_agent.models import ResearchRoute
from ci_agent.route_policy import build_debate_plan, specialist_dimensions


def make_route(**updates) -> ResearchRoute:
    values = {
        "task_kind": "compare",
        "complexity": "standard",
        "dimensions": ["pricing"],
        "competitors": ["A", "B"],
        "required_agents": ["researcher", "pricing"],
        "max_agent_calls": 4,
        "debate_required": False,
        "reason": "test",
    }
    values.update(updates)
    return ResearchRoute(**values)


def test_pricing_route_gets_domain_specialist_and_terminal_arbitrator() -> None:
    plan = build_debate_plan(
        make_route(), mode="investigation", dimensions=["pricing"], brief="比较公开价格"
    )
    assert plan.specialists == ["pricing"]
    assert plan.challenger_required is False
    assert plan.arbitrator_required is True
    assert plan.max_agent_calls == 3


def test_enterprise_route_is_bounded_to_specialist_plus_challenger() -> None:
    route = make_route(
        dimensions=["pricing", "ai", "security"],
        required_agents=["researcher", "pricing", "product", "security"],
    )
    plan = build_debate_plan(
        route,
        mode="investigation",
        dimensions=route.dimensions,
        brief="比较两个企业产品的采购和合规能力",
    )
    assert len(plan.specialists) == 1
    assert plan.challenger_required is True
    assert plan.arbitrator_required is True
    assert plan.max_agent_calls == 4


def test_verify_security_route_is_narrow_but_still_arbitrated() -> None:
    route = make_route(
        task_kind="verify",
        dimensions=["security"],
        required_agents=["researcher", "security"],
    )
    plan = build_debate_plan(route, mode="verify", dimensions=["security"], brief="验证安全能力")
    assert plan.specialists == ["security"]
    assert plan.challenger_required is False
    assert plan.arbitrator_required is True


def test_specialist_dimensions_falls_back_to_all_dimensions() -> None:
    assert specialist_dimensions("pricing", ["billing", "pricing"]) == ["billing", "pricing"]
    assert specialist_dimensions("product", ["unknown_dimension"]) == ["unknown_dimension"]
