import pytest

from ci_agent.models import ResearchRequest, ResearchRoute, RunBudget, SearchPlan, SearchQuery
from ci_agent.search import required_search_dimensions, validate_search_plan


def route() -> ResearchRoute:
    return ResearchRoute(
        task_kind="compare",
        complexity="high_stakes",
        dimensions=["pricing", "AI features", "security compliance"],
        competitors=["Notion", "Coda"],
        required_agents=["researcher", "pricing", "product", "security", "challenger", "arbitrator"],
        max_agent_calls=4,
        debate_required=True,
        reason="enterprise comparison",
        specialists=["pricing", "product", "security"],
    )


def request() -> ResearchRequest:
    return ResearchRequest(
        brief="比较 Notion 与 Coda 的企业版价格、AI 功能和安全能力",
        competitors=["Notion", "Coda"],
    )


def complete_plan() -> SearchPlan:
    focus = {
        "pricing": "official_pricing",
        "product": "official_docs",
        "security": "security_trust",
    }
    return SearchPlan(
        queries=[
            SearchQuery(
                dimension=dimension,
                competitor=competitor,
                query=f"{competitor} official {dimension} enterprise documentation",
                max_results=3,
                source_focus=focus[dimension],
            )
            for competitor in ("Notion", "Coda")
            for dimension in ("pricing", "product", "security")
        ],
        rationale="Cover every competitor and requested dimension with primary sources.",
    )


def test_three_dimension_plan_covers_every_competitor_pair() -> None:
    plan = complete_plan()
    validate_search_plan(plan, route(), request(), RunBudget(max_searches=6, max_pages=18))
    assert required_search_dimensions(route(), request().brief) == {"pricing", "product", "security"}


def test_plan_rejects_missing_competitor_dimension_pair() -> None:
    plan = complete_plan().model_copy(update={"queries": complete_plan().queries[:-1]})
    with pytest.raises(ValueError, match="competitor/dimension pairs"):
        validate_search_plan(plan, route(), request(), RunBudget(max_searches=6, max_pages=18))


def test_plan_rejects_unknown_competitor() -> None:
    plan = complete_plan()
    queries = list(plan.queries)
    queries[0] = queries[0].model_copy(
        update={"competitor": "UnknownCo", "query": "UnknownCo official pricing enterprise"}
    )
    with pytest.raises(ValueError, match="unknown competitor"):
        validate_search_plan(
            plan.model_copy(update={"queries": queries}),
            route(),
            request(),
            RunBudget(max_searches=6, max_pages=18),
        )


def test_plan_rejects_route_competitor_scope_drift() -> None:
    drifted_route = route().model_copy(update={"competitors": ["Notion", "UnknownCo"]})
    with pytest.raises(ValueError, match="route competitors"):
        validate_search_plan(
            complete_plan(),
            drifted_route,
            request(),
            RunBudget(max_searches=6, max_pages=18),
        )


def test_plan_rejects_dimension_source_mismatch() -> None:
    plan = complete_plan()
    queries = list(plan.queries)
    queries[0] = queries[0].model_copy(update={"source_focus": "security_trust"})
    with pytest.raises(ValueError, match="source focus"):
        validate_search_plan(
            plan.model_copy(update={"queries": queries}),
            route(),
            request(),
            RunBudget(max_searches=6, max_pages=18),
        )


def test_plan_rejects_duplicate_queries_and_page_overallocation() -> None:
    plan = complete_plan()
    duplicate = plan.queries[0].model_copy(
        update={"dimension": "product", "source_focus": "official_docs"}
    )
    queries = list(plan.queries)
    queries[1] = duplicate
    with pytest.raises(ValueError, match="duplicate queries"):
        validate_search_plan(
            plan.model_copy(update={"queries": queries}),
            route(),
            request(),
            RunBudget(max_searches=6, max_pages=18),
        )

    with pytest.raises(ValueError, match="page budget"):
        validate_search_plan(plan, route(), request(), RunBudget(max_searches=6, max_pages=17))
