from __future__ import annotations

import httpx
from pydantic import BaseModel, Field, HttpUrl

from .models import ResearchRequest, ResearchRoute, RunBudget, SearchPlan
from .settings import Settings


class SearchResult(BaseModel):
    title: str
    url: HttpUrl
    content: str = ""


class SearchResultSet(BaseModel):
    results: list[SearchResult] = Field(default_factory=list)


_DIMENSION_TERMS = {
    "pricing": ("pricing", "price", "cost", "billing", "plan", "tier", "价格", "定价", "费用", "计费", "套餐"),
    "product": ("product", "feature", "capability", "integration", "workflow", " ai ", "产品", "功能", "集成", "人工智能"),
    "security": ("security", "compliance", "privacy", "governance", "sso", "scim", "安全", "合规", "隐私", "治理"),
}


def required_search_dimensions(route: ResearchRoute, brief: str) -> set[str]:
    """Derive the dimensions the Router's SearchPlan must cover."""

    text = f" {' '.join(route.dimensions)} {brief} ".casefold()
    required = {
        dimension
        for dimension, terms in _DIMENSION_TERMS.items()
        if any(term in text for term in terms)
    }
    required.update(route.specialists)
    required.update(
        agent for agent in route.required_agents if agent in {"pricing", "product", "security"}
    )
    # Open-ended research still needs a product/capability discovery query.
    return required or {"product"}


def validate_search_plan(
    plan: SearchPlan,
    route: ResearchRoute,
    request: ResearchRequest,
    budget: RunBudget,
) -> None:
    """Reject model plans that escape scope or cannot fit the run budget."""

    queries = plan.queries
    if len(queries) > budget.max_searches:
        raise ValueError(f"search plan exceeds search budget: {len(queries)} > {budget.max_searches}")
    if sum(item.max_results for item in queries) > budget.max_pages:
        raise ValueError("search plan result allocation exceeds page budget")

    competitors = {item.casefold(): item for item in request.competitors}
    routed_competitors = {item.casefold() for item in route.competitors}
    if routed_competitors != set(competitors):
        raise ValueError("route competitors must exactly match the requested competitors")

    allowed_source_focus = {
        "pricing": {"official_pricing", "primary_source"},
        "product": {"official_docs", "changelog", "primary_source"},
        "security": {"security_trust", "official_docs", "primary_source"},
    }
    planned_dimensions: set[str] = set()
    planned_competitors: set[str] = set()
    planned_pairs: set[tuple[str, str]] = set()
    seen_queries: set[str] = set()
    for item in queries:
        competitor_key = item.competitor.casefold()
        if competitor_key not in competitors:
            raise ValueError(f"search plan contains an unknown competitor: {item.competitor}")
        if competitor_key not in item.query.casefold():
            raise ValueError(f"search query must name its competitor: {item.competitor}")
        if item.source_focus not in allowed_source_focus[item.dimension]:
            raise ValueError(
                f"source focus {item.source_focus} is incompatible with {item.dimension}"
            )
        normalized_query = " ".join(item.query.casefold().split())
        if normalized_query in seen_queries:
            raise ValueError("search plan contains duplicate queries")
        seen_queries.add(normalized_query)
        planned_dimensions.add(item.dimension)
        planned_competitors.add(competitor_key)
        planned_pairs.add((competitor_key, item.dimension))

    required_dimensions = required_search_dimensions(route, request.brief)
    missing_dimensions = required_dimensions - planned_dimensions
    if missing_dimensions:
        raise ValueError(f"search plan misses dimensions: {sorted(missing_dimensions)}")
    missing_competitors = set(competitors) - planned_competitors
    if missing_competitors:
        raise ValueError(
            "search plan misses competitors: "
            + ", ".join(competitors[item] for item in sorted(missing_competitors))
        )

    pair_count = len(competitors) * len(required_dimensions)
    if pair_count <= budget.max_searches:
        expected_pairs = {
            (competitor, dimension)
            for competitor in competitors
            for dimension in required_dimensions
        }
        missing_pairs = expected_pairs - planned_pairs
        if missing_pairs:
            raise ValueError(f"search plan misses competitor/dimension pairs: {sorted(missing_pairs)}")


class TavilySearch:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def search(self, query: str, max_results: int = 5) -> SearchResultSet:
        if not self.settings.tavily_api_key:
            raise RuntimeError("TAVILY_API_KEY is required for live research")
        payload = {"api_key": self.settings.tavily_api_key, "query": query, "max_results": max_results, "search_depth": "advanced"}
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post("https://api.tavily.com/search", json=payload)
            response.raise_for_status()
            data = response.json()
        return SearchResultSet(results=[SearchResult(title=x.get("title", ""), url=x["url"], content=x.get("content", "")) for x in data.get("results", [])])
