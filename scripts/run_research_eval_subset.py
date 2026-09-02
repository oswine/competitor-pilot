"""Run model-backed research cases against a frozen evidence benchmark.

The benchmark evidence is supplied as an offline fixture, while Router,
Researcher, and Writer are actually invoked through the production GemAI
transport. The runner accepts both the engineering CI-Bench fixtures and the
real-source CI-Real30 evidence pack.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5

from ci_agent.agents import build_agents
from ci_agent.context import build_context_pack
from ci_agent.evidence import EvidenceLedger
from ci_agent.models import Evidence, RunBudget, RunTrace, SourceType, TenantContext
from ci_agent.mode import route_matches_mode, select_mode
from ci_agent.route_policy import build_debate_plan, specialist_dimensions
from ci_agent.settings import Settings

NAMESPACE = UUID("2b6aaf94-2de1-4d4c-b86f-20f6ec68e34a")


def stable_id(case_id: str, evidence_id: str) -> UUID:
    return uuid5(NAMESPACE, f"{case_id}:{evidence_id}")


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def call_with_retry(agent, prompt: str, attempts: int = 2, trace: RunTrace | None = None, role: str = "agent"):
    last = None
    for attempt in range(attempts):
        if trace:
            trace.add("agent_call_started", agent=role, attempt=attempt + 1)
        try:
            async with asyncio.timeout(180):
                result = await agent.run(prompt)
        except Exception as exc:
            last = exc
            if trace:
                trace.add("agent_call_failed", agent=role, attempt=attempt + 1, error=type(exc).__name__)
            if attempt + 1 < attempts:
                await asyncio.sleep(1.5 * (attempt + 1))
        else:
            if trace:
                trace.add("agent_call_completed", agent=role, attempt=attempt + 1)
            return result
    raise last


async def verify_repair(agent, prompt: str, ledger: EvidenceLedger, evidence_models: list[Evidence], case: dict, attempts: int = 3, trace: RunTrace | None = None, role: str = "researcher"):
    current = prompt
    last = None
    for _ in range(attempts):
        result = await call_with_retry(agent, current, trace=trace, role=role)
        bundle, repairs = ledger.repair_bundle_evidence_ids(result.output, evidence_models)
        for original, repaired in repairs:
            trace.add("evidence_id_repaired", agent=role, original_id=original, repaired_id=repaired)
        bundle, claim_ids_repaired = ledger.repair_finding_claim_ids(bundle)
        if claim_ids_repaired:
            trace.add("finding_claim_ids_repaired", agent=role, claim_count=len(bundle.claims))
        bundle, inference_repairs = ledger.downgrade_unverbatim_fact_claims(bundle)
        for repair in inference_repairs:
            trace.add("fact_downgraded_to_inference", agent=role, **repair)
        bundle, numeric_repairs = ledger.ground_missing_finding_numbers(
            bundle, evidence_models, case["task"].get("competitors", [])
        )
        for repair in numeric_repairs:
            trace.add("numeric_claim_repaired", agent=role, **repair)
        bundle, secondary_repairs = ledger.qualify_secondary_source_claims(bundle)
        for repair in secondary_repairs:
            trace.add("secondary_claim_qualified", agent=role, **repair)
        bundle, confidence_repairs = ledger.cap_bundle_confidence(bundle)
        for repair in confidence_repairs:
            trace.add("confidence_capped", agent=role, **repair)
        try:
            ledger.verify_finding(bundle.finding, bundle.claims)
            ledger.verify_task_coverage(
                bundle,
                case["task"].get("competitors", []),
                case["task"].get("dimensions", []),
                case["task"].get("mode", "investigation"),
            )
            return bundle
        except Exception as exc:
            last = exc
            if trace:
                trace.add("evidence_contract_rejected", agent=role, attempt=_ + 1, error=str(exc)[:500])
            current = json.dumps({
                "correction_required": True,
                "error": str(exc),
                "previous_bundle": bundle.model_dump(mode="json"),
                "allowed_evidence_ids": [str(e.id) for e in evidence_models],
                "evidence": [e.model_dump(mode="json") for e in evidence_models],
                "rule": (
                    "Repair every rejected fact claim. Copy its value verbatim from the excerpt of EACH cited "
                    "evidence_id, preserving the source language, spacing, currency, and punctuation. Use one factual "
                    "claim per atomic source statement and normally one evidence_id per factual claim. Put comparison "
                    "or business synthesis in an inference claim. Return only listed evidence IDs; finding claim_ids "
                    "must contain every returned claim ID exactly once. Never use a UUID, locator, URL, paraphrase, or translation as a "
                    "fact value. Claim confidence must be at most the lowest credibility of its cited evidence, and "
                    "finding confidence must be at most every supporting claim confidence. Preserve all material source "
                    "conflicts in contradictions and prefer current official pages as the decision baseline. Delete "
                    "claims outside the original brief and route dimensions. Focus first on the exact rejected claim "
                    "named in error; do not repeat it unchanged. For a fact about a named product, every cited source "
                    "excerpt must name that same product; do not reuse a common value from another product's page. "
                    "End every non-verification finding with an explicit Next action: inference Claim."
                ),
            }, ensure_ascii=False)
    raise ValueError(f"evidence repair failed: {last}")


async def evaluate_case(case: dict, agents: dict, settings: Settings) -> dict:
    trace = RunTrace()
    budget = RunBudget(max_searches=settings.max_searches, max_pages=settings.max_pages,
                       max_model_requests=settings.max_model_requests, max_agent_calls=settings.max_agent_calls,
                       max_cost_cny=settings.max_cost_cny, timeout_seconds=settings.run_timeout_seconds)
    tenant = TenantContext(tenant_id=settings.tenant_default, user_id="eval-runner", role="researcher")
    trace.add("run_started", case_id=case["case_id"], tenant_id=tenant.tenant_id, budget=budget.model_dump(mode="json"))
    evidence_models: list[Evidence] = []
    evidence_lines: list[str] = []
    for item in case.get("evidence", []):
        eid = stable_id(case["case_id"], item["id"])
        raw = item["excerpt"].encode("utf-8")
        item_source_type = item.get("source_type", "website")
        evidence_models.append(Evidence(
            id=eid,
            source_url=item["source_url"],
            publisher=item.get("publisher"),
            retrieved_at=item["retrieved_at"],
            content_hash=item.get("content_hash", "sha256:" + hashlib.sha256(raw).hexdigest()),
            excerpt=item["excerpt"],
            locator=item["locator"],
            source_type=SourceType(item_source_type),
            valid_from=item.get("valid_from"),
            valid_to=item.get("valid_to"),
            credibility=item.get("credibility", 0.8),
        ))
        evidence_lines.append(f"[{eid}] {item['source_url']}\n{item['excerpt']}")

    brief = case["task"]["prompt"]
    mode_decision = select_mode(brief, "auto")
    expected_mode = case["task"].get("mode")
    if expected_mode and mode_decision.selected_mode != expected_mode:
        raise ValueError(
            f"auto mode {mode_decision.selected_mode} does not match benchmark mode {expected_mode}"
        )
    route_result = await call_with_retry(agents["router"], json.dumps({
        "brief": brief, "competitors": case["task"].get("competitors", []), "mode": mode_decision.selected_mode,
        "search_budget": {"max_searches": budget.max_searches, "max_pages": budget.max_pages, "max_results_per_query": 5},
    }, ensure_ascii=False), trace=trace, role="router")
    router_decision = route_result.output
    route = router_decision.route
    if not route_matches_mode(mode_decision.selected_mode, route):
        trace.add("mode_route_mismatch", selected_mode=mode_decision.selected_mode, task_kind=route.task_kind)
        corrected_decision = (await call_with_retry(agents["router"], json.dumps({
            "correction_required": True, "brief": brief,
            "competitors": case["task"].get("competitors", []),
            "selected_mode": mode_decision.selected_mode,
            "search_budget": {"max_searches": budget.max_searches, "max_pages": budget.max_pages, "max_results_per_query": 5},
            "rule": "investigation=>extract/compare/deep_research, monitor=>monitor, verify=>verify/extract",
            "previous_decision": router_decision.model_dump(mode="json"),
        }, ensure_ascii=False), trace=trace, role="router")).output
        router_decision = corrected_decision
        route = corrected_decision.route
    if not route_matches_mode(mode_decision.selected_mode, route):
        raise ValueError(f"router task_kind {route.task_kind} is incompatible with mode {mode_decision.selected_mode}")
    mode_decision.router_validated = True
    trace.add("mode_validated", decision=mode_decision.model_dump(mode="json"), task_kind=route.task_kind)
    debate_plan = build_debate_plan(
        route,
        mode=mode_decision.selected_mode,
        dimensions=case["task"].get("dimensions", route.dimensions),
        brief=brief,
    )
    if debate_plan.max_agent_calls > budget.max_agent_calls:
        raise ValueError(
            "routing plan exceeds evaluation agent budget: "
            f"required={debate_plan.max_agent_calls}; allowed={budget.max_agent_calls}"
        )
    trace.add("debate_plan_selected", plan=debate_plan.model_dump(mode="json"))
    ledger = EvidenceLedger(Path("data/eval-ledger") / case["case_id"])
    for evidence in evidence_models:
        ledger.append(evidence)
    context_pack = build_context_pack(
        brief,
        evidence_models,
        total_token_budget=min(budget.max_input_tokens, settings.research_context_token_budget),
        per_card_token_budget=settings.evidence_card_token_budget,
        max_cards=settings.research_context_max_cards,
    )
    trace.add("context_compacted", card_count=len(context_pack.cards), token_count=context_pack.token_count,
              excluded_evidence_ids=[])
    bundle = await verify_repair(agents["researcher"], json.dumps({
        "brief": brief, "route": route.model_dump(mode="json"), "evidence": context_pack.rendered,
        "evidence_cards": [card.model_dump(mode="json") for card in context_pack.cards],
        "allowed_evidence_ids": [str(e.id) for e in evidence_models],
        "strict_rule": (
            "Use only allowed evidence IDs and exact excerpt values. Cover both named competitors and every requested "
            "dimension supported by the evidence. Prefer official primary sources, state important unknowns, and make "
            "the final finding decision-useful without inventing facts. Claim confidence cannot exceed the lowest "
            "credibility of its citations; finding confidence cannot exceed its supporting claims. Record every material "
            "source conflict in contradictions and use current official pages as the decision baseline. Do not create "
            "claims outside route.dimensions or the brief, even if broad evidence records contain unrelated features. "
            "For facts about a named product, cite only excerpts that name that same product. Never assign a feature "
            "to a plan or tier unless the same source section explicitly links it to that tier; otherwise mark unknown. "
            "End non-verification findings with an explicit 'Next action:' grounded inference that also exists as an "
            "inference Claim. For pricing, include both annual and monthly public price variants when supplied."
        ),
    }, ensure_ascii=False), ledger, evidence_models, case, trace=trace, role="researcher")
    debate_events = []
    candidate_bundles = [bundle]
    for specialist in debate_plan.specialists:
        scoped_dimensions = specialist_dimensions(specialist, case["task"].get("dimensions", route.dimensions))
        specialist_prompt = json.dumps(
            {
                "brief": brief,
                "mode": mode_decision.selected_mode,
                "specialist": specialist,
                "dimensions": scoped_dimensions,
                "candidate": bundle.model_dump(mode="json"),
                "evidence": evidence_lines,
                "allowed_evidence_ids": [str(e.id) for e in evidence_models],
                "rule": (
                    "Audit and correct only this specialist's dimensions. Return a complete ResearchBundle for both "
                    "named competitors. Use exact evidence substrings for facts, preserve conflicts and unknowns, never "
                    "assign a plan tier without an explicit same-section link, and end the finding with an explicit "
                    "Next action: sentence."
                ),
            },
            ensure_ascii=False,
        )
        specialist_bundle = await verify_repair(
            agents[specialist],
            specialist_prompt,
            ledger,
            evidence_models,
            case,
            trace=trace,
            role=specialist,
        )
        candidate_bundles.append(specialist_bundle)
        debate_events.append(
            {
                "event": "specialist_verified",
                "specialist": specialist,
                "finding_id": specialist_bundle.finding.finding_id,
            }
        )
    if debate_plan.challenger_required:
        challenge = await verify_repair(
            agents["challenger"],
            json.dumps(
                {
                    "brief": brief,
                    "candidate": bundle.model_dump(mode="json"),
                    "specialist_bundles": [item.model_dump(mode="json") for item in candidate_bundles[1:]],
                    "evidence": evidence_lines,
                    "allowed_evidence_ids": [str(e.id) for e in evidence_models],
                },
                ensure_ascii=False,
            ),
            ledger,
            evidence_models,
            case,
            trace=trace,
            role="challenger",
        )
        candidate_bundles.append(challenge)
        debate_events.append({"event": "challenger_verified", "finding_id": challenge.finding.finding_id})
    try:
        bundle = await verify_repair(
            agents["arbitrator"],
            json.dumps(
                {
                    "brief": brief,
                    "mode": mode_decision.selected_mode,
                    "debate_plan": debate_plan.model_dump(mode="json"),
                    "candidate_bundles": [item.model_dump(mode="json") for item in candidate_bundles],
                    "evidence": evidence_lines,
                    "allowed_evidence_ids": [str(e.id) for e in evidence_models],
                    "rule": (
                        "You are the terminal decision gate. Reconcile all candidate bundles into exactly one complete "
                        "ResearchBundle covering every requested competitor and dimension. Keep only evidence-grounded "
                        "claims, preserve material conflicts and unknowns, and prefer current official sources."
                    ),
                },
                ensure_ascii=False,
            ),
            ledger,
            evidence_models,
            case,
            trace=trace,
            role="arbitrator",
        )
        debate_events.append({"event": "arbitrator_verified", "finding_id": bundle.finding.finding_id})
    except Exception as exc:
        trace.add("arbitrator_contract_failed", error=type(exc).__name__, detail=str(exc)[:500])
        bundle = max(
            candidate_bundles,
            key=lambda item: (
                len(set(item.finding.evidence_ids)),
                len(item.claims),
                item.finding.confidence,
                len(item.finding.statement),
            ),
        )
        debate_events.append({
            "event": "arbitrator_lite_selected",
            "finding_id": bundle.finding.finding_id,
            "candidate_count": len(candidate_bundles),
        })
    writer_result = await call_with_retry(agents["writer"], json.dumps({
        "brief": brief,
        "verified_finding": bundle.finding.model_dump(mode="json"),
        "claims": [claim.model_dump(mode="json") for claim in bundle.claims],
        "evidence": evidence_lines,
        "writer_rule": "Copy every field of verified_finding verbatim and completely. Do not rewrite, summarize, translate, truncate, reorder, add, or remove content.",
    }, ensure_ascii=False), trace=trace, role="writer")
    written = writer_result.output
    if written != bundle.finding:
        trace.add("writer_contract_rejected", error="lossy_or_mutated_verified_finding")
        written = (await call_with_retry(agents["writer"], json.dumps({
            "correction_required": True,
            "verified_finding": bundle.finding.model_dump(mode="json"),
            "writer_rule": (
                "Return an exact field-for-field copy. Preserve the full statement, ordering, contradictions, unknowns, "
                "confidence, and all IDs. Do not make any semantic or formatting change."
            ),
        }, ensure_ascii=False), trace=trace, role="writer")).output
    if written != bundle.finding:
        raise ValueError("writer lossless-publication contract failed after repair")
    ledger.validate_ids(written.evidence_ids)
    events = [
        {"event": "route_selected", "route": route.model_dump(mode="json")},
        {"event": "claims_verified", "claim_count": len(bundle.claims)},
        *debate_events,
        {"event": "writer_completed", "finding_id": written.finding_id},
        *trace.events,
    ]
    return {
        "case_id": case["case_id"],
        "track": case["track"],
        "task": case["task"],
        "benchmark_metadata": case.get("metadata", {}),
        "output": {
            "report": written.model_dump(mode="json"),
            "mode_decision": mode_decision.model_dump(mode="json"),
            "finding": bundle.finding.model_dump(mode="json"),
            "claims": [claim.model_dump(mode="json") for claim in bundle.claims],
            "debate_plan": debate_plan.model_dump(mode="json"),
            "search_plan": router_decision.search_plan.model_dump(mode="json"),
            "evidence": [e.model_dump(mode="json") for e in evidence_models],
            "evidence_ids": [str(x) for x in written.evidence_ids],
            "trace_events": events,
            "model_roles": {
                "router": settings.gemai_fast_model,
                "researcher": settings.gemai_research_model,
                "pricing": settings.gemai_research_model,
                "product": settings.gemai_research_model,
                "security": settings.gemai_research_model,
                "challenger": settings.gemai_research_model,
                "arbitrator": settings.gemai_synthesis_model,
                "writer": settings.gemai_synthesis_model,
            },
            "tenant_id": tenant.tenant_id,
            "budget": budget.model_dump(mode="json"),
            "context_cards": [card.model_dump(mode="json") for card in context_pack.cards],
        },
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="benchmarks/ci_bench/cases.jsonl")
    parser.add_argument("--per-track", type=int, default=3)
    parser.add_argument("--all", action="store_true", help="run every frozen test case")
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="run only this case ID; repeat the option to select more than one case",
    )
    parser.add_argument("--output", default="data/research-eval-subset.jsonl")
    parser.add_argument("--research-model", default="deepseek-v4-flash")
    parser.add_argument("--synthesis-model", default="deepseek-v4-flash")
    parser.add_argument("--case-concurrency", type=int, default=3)
    parser.add_argument("--case-attempts", type=int, default=2)
    args = parser.parse_args()
    settings = Settings(
        gemai_research_model=args.research_model,
        gemai_synthesis_model=args.synthesis_model,
    )
    if not settings.gemai_api_key:
        raise SystemExit("GEMAI_API_KEY is required")
    grouped: dict[str, list[dict]] = {}
    for case in load_cases(Path(args.cases)):
        grouped.setdefault(case["track"], []).append(case)
    if args.case_id:
        wanted = set(args.case_id)
        selected = [case for cases in grouped.values() for case in cases if case["case_id"] in wanted]
        missing = wanted - {case["case_id"] for case in selected}
        if missing:
            raise SystemExit(f"unknown case IDs: {', '.join(sorted(missing))}")
    else:
        selected = [
            case
            for track in sorted(grouped)
            for case in (grouped[track] if args.all else grouped[track][:args.per_track])
        ]
    agents = build_agents(settings)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    write_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(max(1, args.case_concurrency))

    async def write_progress() -> None:
        ordered = [results[case["case_id"]] for case in selected if case["case_id"] in results]
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in ordered) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output)

    async def run_case(index: int, case: dict) -> None:
        async with semaphore:
            print(f"[{index}/{len(selected)}] {case['case_id']}", flush=True)
            started = time.monotonic()
            result = None
            last_error: Exception | None = None
            case_attempt = 0
            for case_attempt in range(1, max(1, args.case_attempts) + 1):
                try:
                    result = await evaluate_case(case, agents, settings)
                    break
                except Exception as exc:
                    last_error = exc
                    if case_attempt < max(1, args.case_attempts):
                        print(
                            f"[{index}/{len(selected)}] {case['case_id']} restart "
                            f"{case_attempt + 1}/{max(1, args.case_attempts)} after {type(exc).__name__}",
                            flush=True,
                        )
                        await asyncio.sleep(2 * case_attempt)
            if result is None:
                assert last_error is not None
                result = {
                    "case_id": case["case_id"],
                    "track": case["track"],
                    "task": case.get("task"),
                    "benchmark_metadata": case.get("metadata", {}),
                    "error": f"{type(last_error).__name__}: {last_error}",
                    "failure_kind": type(last_error).__name__,
                }
            result["case_attempt"] = case_attempt
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            async with write_lock:
                results[case["case_id"]] = result
                await write_progress()

    await asyncio.gather(*(run_case(index, case) for index, case in enumerate(selected, 1)))
    ordered = [results[case["case_id"]] for case in selected]
    print(json.dumps({"selected": len(selected), "completed": sum("error" not in item for item in ordered), "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
