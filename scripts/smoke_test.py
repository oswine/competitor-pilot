"""Run one complete live flow per benchmark track; never runs the 100-case suite."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ci_agent.models import ResearchRequest
from ci_agent.orchestrator import ResearchOrchestrator
from ci_agent.settings import settings


SMOKE_CASES = {
    "change_detection": ("CHG-001", "https://www.python.org/"),
    "fact_extraction": ("EXT-001", "https://www.python.org/"),
    "research_synthesis": ("RES-001", "https://www.python.org/"),
    "routing": ("ROU-001", "https://www.python.org/"),
    "reliability_security": ("REL-001", "https://www.python.org/"),
}


def load_selected_cases() -> dict[str, dict]:
    cases = {}
    path = Path("benchmarks/ci_bench/cases.jsonl")
    wanted = {case_id for case_id, _ in SMOKE_CASES.values()}
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item["case_id"] in wanted:
            cases[item["case_id"]] = item
    if set(cases) != wanted:
        raise RuntimeError(f"smoke case selection incomplete: {wanted - set(cases)}")
    return cases


async def main() -> None:
    if not settings.gemai_api_key:
        raise SystemExit("Set GEMAI_API_KEY before smoke testing; no mock or downgraded path is used.")
    if not settings.tavily_api_key:
        raise SystemExit("Set TAVILY_API_KEY before smoke testing; no mock or downgraded path is used.")
    orchestrator = ResearchOrchestrator(settings)
    selected = load_selected_cases()
    results = []
    for track, (case_id, url) in SMOKE_CASES.items():
        case = selected[case_id]
        brief = case["task"]["prompt"]
        search_results = await orchestrator.search.search(f"Python {brief}", max_results=2)
        if not search_results.results:
            raise RuntimeError(f"{case_id}: Tavily returned no results; smoke test does not fall back")
        # Tavily results may include anti-bot pages (for example Stack Overflow).
        # Keep those URLs in the run for failure tracing and include the stable
        # benchmark fixture so the full live agent path remains testable.
        live_urls = [url] + [str(item.url) for item in search_results.results]
        report, trace = await orchestrator.run(ResearchRequest(brief=brief, competitors=["Python"], urls=live_urls[:2]))
        assert report.findings, f"{track}: no findings"
        assert report.evidence_ids, f"{track}: no report evidence"
        assert any(event["event"] == "route_selected" for event in trace.events)
        results.append({"track": track, "case_id": case_id, "run_id": str(trace.run_id), "report_id": str(report.report_id), "events": len(trace.events)})
    Path("data/smoke-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
