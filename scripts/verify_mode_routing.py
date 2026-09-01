"""Run the production Router once for each auto-selected research mode."""
from __future__ import annotations

import asyncio
import json

from ci_agent.agents import build_agents
from ci_agent.mode import route_matches_mode, select_mode
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
        decision = select_mode(brief)
        result = await router.run(json.dumps({
            "brief": brief,
            "competitors": ["Notion", "Coda"],
            "mode": decision.selected_mode,
        }, ensure_ascii=False))
        print(json.dumps({
            "brief": brief,
            "selected_mode": decision.selected_mode,
            "task_kind": result.output.task_kind,
            "compatible": route_matches_mode(decision.selected_mode, result.output),
        }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
