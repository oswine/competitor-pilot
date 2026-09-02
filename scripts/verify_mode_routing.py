"""Run the production Router once for each auto-selected research mode."""
from __future__ import annotations

import asyncio
import json

from ci_agent.agents import build_agents
from ci_agent.mode import mode_decision_from_intent, route_matches_mode
from ci_agent.models import ResearchRequest, RunBudget
from ci_agent.search import validate_search_plan
from ci_agent.settings import Settings


CASES = (
    "比较 Notion 与 Coda 的企业版价格和安全能力",
    "持续监控 Notion 企业版价格变化并提醒我",
    "验证 Coda 是否支持企业 SSO",
)


async def main() -> None:
    settings = Settings()
    if not settings.gemai_api_key:
        raise SystemExit("GEMAI_API_KEY is required")
    router = build_agents(settings)["router"]
    for brief in CASES:
        result = await router.run(json.dumps({
            "brief": brief,
            "competitors": ["Notion", "Coda"],
            "mode": "auto",
            "search_budget": {"max_searches": 12, "max_pages": 30, "max_results_per_query": 5},
        }, ensure_ascii=False))
        intent = result.output.intent
        mode_decision = mode_decision_from_intent(intent, "auto")
        if not route_matches_mode(mode_decision.selected_mode, result.output.route):
            raise RuntimeError(
                f"Router route {result.output.route.task_kind} is incompatible with {mode_decision.selected_mode}"
            )
        validate_search_plan(
            result.output.search_plan,
            result.output.route,
            ResearchRequest(brief=brief, competitors=["Notion", "Coda"]),
            RunBudget(max_searches=12, max_pages=30),
        )
        print(json.dumps({
            "brief": brief,
            "selected_mode": mode_decision.selected_mode,
            "intent_confidence": mode_decision.confidence,
            "task_kind": result.output.route.task_kind,
            "compatible": route_matches_mode(mode_decision.selected_mode, result.output.route),
            "search_plan": result.output.search_plan.model_dump(mode="json"),
        }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
