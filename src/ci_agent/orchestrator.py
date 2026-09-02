from __future__ import annotations

import json
import asyncio
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from uuid import UUID

from .agents import build_agents
from .browser import BrowserCollector
from .context import build_context_pack
from .evidence import EvidenceLedger
from .models import Claim, CompetitiveResearchReport, DebatePlan, Evidence, ResearchBundle, ResearchRequest, ResearchRoute, RunBudget, RunTrace, SearchPlan, TenantContext, VerifiedFinding
from .mode import route_matches_mode, select_mode
from .object_store import ObjectStore
from .persistence import PostgresRepository
from .telemetry import span
from .metrics import RUNS, PAGES, RUN_SECONDS
import time
from .search import TavilySearch, validate_search_plan
from .settings import Settings
from .playbook import PlaybookReplay, ResearchPlaybookStore
from .route_policy import build_debate_plan, specialist_dimensions


class ResearchOrchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ledger = EvidenceLedger(settings.object_store_root / "evidence")
        self.search = TavilySearch(settings)
        self.agents = build_agents(settings)
        self.repository = PostgresRepository(settings.database_url) if settings.postgres_enabled else None
        self.browser = BrowserCollector(settings, self.ledger, self.repository)
        self.objects = ObjectStore(settings.minio_endpoint, settings.minio_access_key, settings.minio_secret_key,
                                   settings.minio_bucket, settings.object_store_root)
        self.playbooks = ResearchPlaybookStore(settings.object_store_root / "playbooks")

    async def _bounded_agent_call(self, agent_name: str, prompt: str, trace: RunTrace, budget: RunBudget, retries: int = 2):
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            started = asyncio.get_running_loop().time()
            trace.add("agent_call_started", agent=agent_name, attempt=attempt + 1)
            try:
                async with asyncio.timeout(min(180, budget.timeout_seconds)):
                    result = await self.agents[agent_name].run(prompt)
                trace.add("agent_call_completed", agent=agent_name, attempt=attempt + 1, seconds=round(asyncio.get_running_loop().time() - started, 3))
                return result
            except Exception as exc:
                last_error = exc
                trace.add("agent_call_failed", agent=agent_name, attempt=attempt + 1, error=type(exc).__name__)
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
        fallback_name = (
            "fallback_researcher"
            if agent_name in {"pricing", "product", "security", "challenger", "arbitrator"}
            else agent_name
        )
        if fallback_name != agent_name:
            trace.add("agent_fallback", from_agent=agent_name, to_agent=fallback_name)
            async with asyncio.timeout(min(180, budget.timeout_seconds)):
                return await self.agents[fallback_name].run(prompt)
        raise last_error or RuntimeError(f"agent call failed: {agent_name}")

    async def _route_with_search_plan(
        self,
        request: ResearchRequest,
        trace: RunTrace,
        budget: RunBudget,
        context: TenantContext,
        fixed_route: ResearchRoute | None = None,
    ) -> tuple[ResearchRoute, SearchPlan]:
        """Ask the Router once, then enforce mode, scope, and search budgets."""

        prompt: dict = {
            "brief": request.brief,
            "competitors": request.competitors,
            "mode": request.mode,
            "explicit_urls": [str(item) for item in request.urls],
            "search_budget": {
                "max_searches": budget.max_searches,
                "max_pages": budget.max_pages,
                "max_results_per_query": 5,
            },
            "required_output": (
                "Return route and search_plan. Use separate pricing, product, and security queries for every requested "
                "dimension. When the budget permits, cover every competitor/dimension pair."
            ),
        }
        if fixed_route is not None:
            prompt["fixed_route"] = fixed_route.model_dump(mode="json")
            prompt["fixed_route_rule"] = "Copy fixed_route exactly; only generate the missing search_plan."
        last_error: Exception | None = None
        for semantic_attempt in range(2):
            with span("agent.router", tenant_id=context.tenant_id, run_id=str(trace.run_id)):
                result = await self._bounded_agent_call(
                    "router", json.dumps(prompt, ensure_ascii=False), trace, budget
                )
            decision = result.output
            errors: list[str] = []
            route = fixed_route or decision.route
            if fixed_route is not None and decision.route != fixed_route:
                errors.append("route must exactly match the checkpoint fixed_route")
            if not route_matches_mode(request.mode, route):
                errors.append(
                    f"task_kind {route.task_kind} is incompatible with mode {request.mode}"
                )
            try:
                validate_search_plan(decision.search_plan, route, request, budget)
            except ValueError as exc:
                errors.append(str(exc))
            if not errors:
                trace.add(
                    "route_selected",
                    route=route.model_dump(mode="json"),
                    phase="pre_search",
                )
                trace.add(
                    "search_plan_selected",
                    search_plan=decision.search_plan.model_dump(mode="json"),
                )
                return route, decision.search_plan

            last_error = ValueError("; ".join(errors))
            trace.add(
                "router_decision_rejected",
                semantic_attempt=semantic_attempt + 1,
                errors=errors,
            )
            prompt = {
                "correction_required": True,
                "brief": request.brief,
                "competitors": request.competitors,
                "selected_mode": request.mode,
                "search_budget": {
                    "max_searches": budget.max_searches,
                    "max_pages": budget.max_pages,
                    "max_results_per_query": 5,
                },
                "previous_decision": decision.model_dump(mode="json"),
                "errors": errors,
                "rule": (
                    "Return a mode-compatible route and a bounded SearchPlan. Every query must name exactly one supplied "
                    "competitor. Cover every requested pricing/product/security dimension and, when query budget allows, "
                    "every competitor/dimension pair. Keep total max_results within max_pages."
                ),
            }
            if fixed_route is not None:
                prompt["fixed_route"] = fixed_route.model_dump(mode="json")
                prompt["fixed_route_rule"] = "Copy fixed_route exactly; only repair search_plan."
        raise ValueError(f"Router SearchPlan contract failed after correction: {last_error}")

    def _apply_bundle_harness(
        self,
        bundle: ResearchBundle,
        evidence: list[Evidence],
        competitors: list[str],
        trace: RunTrace,
        agent_name: str,
    ) -> ResearchBundle:
        bundle, repairs = self.ledger.repair_bundle_evidence_ids(bundle, evidence)
        for original, repaired in repairs:
            trace.add("evidence_id_repaired", agent=agent_name, original_id=original, repaired_id=repaired)
        bundle, claim_ids_repaired = self.ledger.repair_finding_claim_ids(bundle)
        if claim_ids_repaired:
            trace.add("finding_claim_ids_repaired", agent=agent_name, claim_count=len(bundle.claims))
        bundle, repairs = self.ledger.downgrade_unverbatim_fact_claims(bundle)
        for repair in repairs:
            trace.add("fact_downgraded_to_inference", agent=agent_name, **repair)
        bundle, repairs = self.ledger.ground_missing_finding_numbers(bundle, evidence, competitors)
        for repair in repairs:
            trace.add("numeric_claim_repaired", agent=agent_name, **repair)
        bundle, repairs = self.ledger.qualify_secondary_source_claims(bundle)
        for repair in repairs:
            trace.add("secondary_claim_qualified", agent=agent_name, **repair)
        bundle, repairs = self.ledger.cap_bundle_confidence(bundle)
        for repair in repairs:
            trace.add("confidence_capped", agent=agent_name, **repair)
        return bundle

    async def _research_bundle_with_repair(
        self,
        prompt: str,
        evidence: list[Evidence],
        competitors: list[str],
        dimensions: list[str],
        mode: str,
        trace: RunTrace,
        budget: RunBudget,
        agent_name: str = "researcher",
    ):
        allowed = [str(item.id) for item in evidence]
        current_prompt = prompt
        last_error: Exception | None = None
        for attempt in range(3):
            result = await self._bounded_agent_call(agent_name, current_prompt, trace, budget)
            bundle = self._apply_bundle_harness(result.output, evidence, competitors, trace, agent_name)
            try:
                return bundle, self._verify_bundle_scope(bundle, evidence, competitors, dimensions, mode)
            except (ValueError, KeyError) as exc:
                last_error = exc
                trace.add("evidence_contract_rejected", attempt=attempt + 1, error=str(exc)[:500])
                if attempt == 2:
                    break
                current_prompt = json.dumps({
                    "correction_required": True,
                    "previous_error": str(exc),
                    "previous_bundle": bundle.model_dump(mode="json"),
                    "allowed_evidence_ids": allowed,
                    "evidence": [item.model_dump(mode="json") for item in evidence],
                    "rule": (
                        "Use only allowed evidence UUIDs. A fact value must be a verbatim substring of every cited "
                        "excerpt; normally cite one source per atomic fact. Never use a UUID, locator, URL, paraphrase, "
                        "or translation as a fact value. Claim confidence cannot exceed the lowest credibility of its "
                        "citations, and finding confidence cannot exceed its supporting claims. Cover every requested "
                        "competitor and dimension. Prefer current official sources, put every material source conflict "
                        "in contradictions, and preserve unknowns. Delete claims outside the original brief and route "
                        "dimensions; focus first on the exact rejected claim and never repeat it unchanged. Never assign "
                        "a feature to a plan or tier unless the same source section explicitly links it to that tier; "
                        "otherwise mark unknown. Finding claim_ids must contain every returned claim ID exactly once. "
                        "End every non-verification finding with an explicit Next action: inference Claim."
                    ),
                    "original_task": prompt,
                }, ensure_ascii=False)
        raise ValueError(f"evidence contract failed after repair attempts: {last_error}")

    def _arbitrator_lite(
        self,
        bundles: list[ResearchBundle],
        evidence: list[Evidence],
        competitors: list[str],
        dimensions: list[str],
        mode: str,
        trace: RunTrace,
    ) -> tuple[ResearchBundle, VerifiedFinding]:
        """Deterministic terminal gate used only after LLM arbitration fails."""

        verified: list[tuple[ResearchBundle, VerifiedFinding]] = []
        for candidate in bundles:
            try:
                finding = self._verify_bundle_scope(candidate, evidence, competitors, dimensions, mode)
            except (ValueError, KeyError):
                continue
            verified.append((candidate, finding))
        if not verified:
            raise ValueError("no verified candidate available for ArbitratorLite")
        bundle, finding = max(
            verified,
            key=lambda item: (
                len(set(item[0].finding.evidence_ids)),
                len(item[0].claims),
                item[0].finding.confidence,
                len(item[0].finding.statement),
            ),
        )
        trace.add(
            "arbitrator_lite_selected",
            candidate_finding_id=finding.finding_id,
            candidate_count=len(bundles),
        )
        return bundle, finding

    def _verify_bundle_scope(
        self,
        bundle: ResearchBundle,
        evidence: list[Evidence],
        competitors: list[str] | None = None,
        dimensions: list[str] | None = None,
        mode: str = "investigation",
    ) -> VerifiedFinding:
        """Validate grounding against this run, not the process-wide ledger."""
        allowed = {item.id for item in evidence}
        referenced = set(bundle.finding.evidence_ids)
        for claim in bundle.claims:
            referenced.update(claim.evidence_ids)
        unknown = referenced - allowed
        if unknown:
            raise ValueError(f"bundle references evidence outside this tenant run: {sorted(map(str, unknown))}")
        finding = self.ledger.verify_finding(bundle.finding, bundle.claims)
        if competitors:
            self.ledger.verify_task_coverage(bundle, competitors, dimensions or [], mode)
        return finding

    async def run(self, request: ResearchRequest, context: TenantContext | None = None,
                  idempotency_key: str | None = None) -> tuple[CompetitiveResearchReport, RunTrace]:
        context = context or TenantContext(tenant_id=self.settings.tenant_default, user_id="local", role="researcher")
        idempotency_key = idempotency_key or hashlib.sha256((context.tenant_id + request.model_dump_json()).encode()).hexdigest()
        mode_decision = select_mode(request.brief, request.mode)
        request = request.model_copy(update={"mode": mode_decision.selected_mode})
        if self.repository:
            existing = self.repository.get_idempotent_run(idempotency_key, context.tenant_id)
            if existing and existing.report_json:
                return CompetitiveResearchReport.model_validate(existing.report_json), RunTrace.model_validate(existing.trace_json)
        trace = RunTrace()
        checkpoint_phase, checkpoint_payload = "started", {}
        started_at = time.monotonic()
        if self.repository:
            checkpoint = self.repository.get_checkpoint(idempotency_key, context.tenant_id)
            if checkpoint is None:
                existing = self.repository.reserve_idempotency(idempotency_key, context.tenant_id, trace.run_id)
                if existing and existing.report_json:
                    return CompetitiveResearchReport.model_validate(existing.report_json), RunTrace.model_validate(existing.trace_json)
            checkpoint_phase, checkpoint_payload, checkpoint_run_id = self.repository.acquire_checkpoint(
                idempotency_key, context.tenant_id, trace.run_id, self.settings.run_checkpoint_lease_seconds)
            if checkpoint_payload.get("trace"):
                trace = RunTrace.model_validate(checkpoint_payload["trace"])
                trace.add("run_resumed", checkpoint_phase=checkpoint_phase)
            elif checkpoint_run_id != str(trace.run_id):
                trace = RunTrace(run_id=UUID(checkpoint_run_id))
        budget = RunBudget(max_searches=self.settings.max_searches, max_pages=self.settings.max_pages,
                           max_model_requests=self.settings.max_model_requests, max_agent_calls=self.settings.max_agent_calls,
                           max_cost_cny=self.settings.max_cost_cny, timeout_seconds=self.settings.run_timeout_seconds)
        if checkpoint_phase == "started" and not checkpoint_payload:
            trace.add("run_started", budget=budget.model_dump(mode="json"), mode=request.mode, tenant_id=context.tenant_id)
            trace.add("mode_selected", decision=mode_decision.model_dump(mode="json"))
        active_playbook = self.playbooks.active(context.tenant_id)

        if checkpoint_payload.get("route") and checkpoint_payload.get("search_plan"):
            route = ResearchRoute.model_validate(checkpoint_payload["route"])
            search_plan = SearchPlan.model_validate(checkpoint_payload["search_plan"])
            if not route_matches_mode(request.mode, route):
                raise ValueError(f"checkpoint route {route.task_kind} is incompatible with mode {request.mode}")
            validate_search_plan(search_plan, route, request, budget)
            mode_decision.router_validated = True
            trace.add("checkpoint_routing_restored", query_count=len(search_plan.queries))
        else:
            legacy_route = (
                ResearchRoute.model_validate(checkpoint_payload["route"])
                if checkpoint_payload.get("route")
                else None
            )
            route, search_plan = await self._route_with_search_plan(
                request,
                trace,
                budget,
                context,
                fixed_route=legacy_route,
            )
            mode_decision.router_validated = True
            trace.add("mode_validated", decision=mode_decision.model_dump(mode="json"), task_kind=route.task_kind)
            if legacy_route is not None:
                trace.add("legacy_checkpoint_search_plan_created", query_count=len(search_plan.queries))

        debate_plan = DebatePlan.model_validate(checkpoint_payload["debate_plan"]) if checkpoint_payload.get("debate_plan") else build_debate_plan(
            route,
            mode=request.mode,
            dimensions=route.dimensions,
            brief=request.brief,
        )
        if debate_plan.max_agent_calls > budget.max_agent_calls:
            raise RuntimeError(
                "routing plan exceeds run agent budget: "
                f"required={debate_plan.max_agent_calls}; allowed={budget.max_agent_calls}"
            )
        if not checkpoint_payload.get("debate_plan"):
            trace.add("debate_plan_selected", plan=debate_plan.model_dump(mode="json"))

        evidence: list[Evidence] = []
        quarantined_evidence_ids: set[UUID] = set()
        reusable_knowledge: list[dict] = list(checkpoint_payload.get("reusable_knowledge", []))
        if checkpoint_phase in {"evidence_collected", "bundle_verified"} and checkpoint_payload.get("evidence"):
            evidence = [Evidence.model_validate(item) for item in checkpoint_payload["evidence"]]
            quarantined_evidence_ids = {UUID(item) for item in checkpoint_payload.get("quarantined_evidence_ids", [])}
            for item in evidence:
                self.ledger.append(item)
            trace.add("checkpoint_evidence_restored", evidence_count=len(evidence))
        else:
            if self.repository:
                records = self.repository.load_verified_knowledge(context.tenant_id, request.competitors)
                seen_ids: set[UUID] = set()
                seen_runs: set[str] = set()
                for record in records:
                    if record.source_run_id not in seen_runs:
                        reusable_knowledge.append(record.bundle_json)
                        seen_runs.add(record.source_run_id)
                    for raw_item in record.evidence_json:
                        item = Evidence.model_validate(raw_item)
                        if item.id not in seen_ids and (item.valid_to is None or item.valid_to > datetime.now(timezone.utc)):
                            evidence.append(item)
                            self.ledger.append(item)
                            seen_ids.add(item.id)
                if records:
                    trace.add("verified_knowledge_reused", records=len(records), evidence_count=len(evidence),
                              policy="fresh_tenant_scoped")
            urls = [str(u) for u in request.urls]
            if not urls:
                seen_urls: set[str] = set()
                for planned_query in search_plan.queries:
                    results = await self.search.search(
                        planned_query.query,
                        max_results=planned_query.max_results,
                    )
                    added = 0
                    for result in results.results:
                        url = str(result.url)
                        normalized = url.rstrip("/").casefold()
                        if normalized in seen_urls:
                            continue
                        seen_urls.add(normalized)
                        urls.append(url)
                        added += 1
                    trace.add(
                        "search_query_completed",
                        dimension=planned_query.dimension,
                        competitor=planned_query.competitor,
                        source_focus=planned_query.source_focus,
                        query=planned_query.query,
                        returned=len(results.results),
                        unique_added=added,
                    )
                trace.add(
                    "search_completed",
                    query_count=len(search_plan.queries),
                    unique_url_count=len(urls),
                )
            else:
                trace.add(
                    "search_plan_not_executed",
                    reason="explicit_urls_supplied",
                    query_count=len(search_plan.queries),
                )
            if not urls:
                raise RuntimeError("search returned no URLs")
            for url in urls[:budget.max_pages]:
                try:
                    snapshot = await self.browser.fetch(url, use_browser=False, bypass_cache=request.mode == "monitor")
                    item = self.browser.persist(snapshot)
                    if all(existing.id != item.id for existing in evidence):
                        evidence.append(item)
                    object_key = f"tenants/{context.tenant_id}/evidence/{item.content_hash.removeprefix('sha256:')}"
                    raw = snapshot.raw.encode("utf-8") if isinstance(snapshot.raw, str) else snapshot.raw
                    self.objects.put_bytes(object_key, raw, snapshot.content_type)
                    if snapshot.screenshot:
                        self.objects.put_bytes(f"tenants/{context.tenant_id}/screenshots/{item.id}.png", snapshot.screenshot, "image/png")
                    if snapshot.recording:
                        self.objects.put_bytes(f"tenants/{context.tenant_id}/recordings/{item.id}.har", snapshot.recording, "application/json")
                    if self.repository:
                        self.repository.save_evidence(context.tenant_id, item, object_key)
                    trace.add("page_snapshot", url=url, content_hash=item.content_hash, injection_flags=snapshot.injection_flags,
                              cache_policy="bypass" if request.mode == "monitor" else "reuse")
                    if snapshot.injection_flags:
                        quarantined_evidence_ids.add(item.id)
                        trace.add("evidence_quarantined", evidence_id=str(item.id), reason="prompt_injection_flag")
                    PAGES.labels(snapshot.content_type, "success").inc()
                except Exception as exc:
                    trace.add("page_fetch_failed", url=url, error=type(exc).__name__, detail=str(exc)[:240])
                    if not url.lower().endswith((".pdf", ".docx")):
                        try:
                            snapshot = await self.browser.fetch(url, use_browser=True, bypass_cache=request.mode == "monitor")
                            item = self.browser.persist(snapshot)
                            if all(existing.id != item.id for existing in evidence):
                                evidence.append(item)
                            raw = snapshot.raw.encode("utf-8") if isinstance(snapshot.raw, str) else snapshot.raw
                            object_key = f"tenants/{context.tenant_id}/evidence/{item.content_hash.removeprefix('sha256:')}"
                            self.objects.put_bytes(object_key, raw, snapshot.content_type)
                            if snapshot.screenshot:
                                self.objects.put_bytes(f"tenants/{context.tenant_id}/screenshots/{item.id}.png", snapshot.screenshot, "image/png")
                            if snapshot.recording:
                                self.objects.put_bytes(f"tenants/{context.tenant_id}/recordings/{item.id}.har", snapshot.recording, "application/json")
                            if self.repository:
                                self.repository.save_evidence(context.tenant_id, item, object_key)
                            trace.add("browser_escalated", url=url, content_hash=item.content_hash, injection_flags=snapshot.injection_flags)
                            if snapshot.injection_flags:
                                quarantined_evidence_ids.add(item.id)
                                trace.add("evidence_quarantined", evidence_id=str(item.id), reason="prompt_injection_flag")
                            PAGES.labels(snapshot.content_type, "browser_escalated").inc()
                        except Exception as browser_exc:
                            trace.add("browser_escalation_failed", url=url, error=type(browser_exc).__name__)
            if self.repository:
                self.repository.save_checkpoint(idempotency_key, context.tenant_id, "evidence_collected", {
                    "trace": trace.model_dump(mode="json"),
                    "evidence": [item.model_dump(mode="json") for item in evidence],
                    "quarantined_evidence_ids": [str(item) for item in quarantined_evidence_ids],
                    "reusable_knowledge": reusable_knowledge,
                    "route": route.model_dump(mode="json"),
                    "search_plan": search_plan.model_dump(mode="json"),
                    "debate_plan": debate_plan.model_dump(mode="json"),
                }, self.settings.run_checkpoint_lease_seconds)
        if not evidence:
            raise RuntimeError("all candidate pages failed browser collection")
        context_pack = build_context_pack(
            request.brief,
            evidence,
            total_token_budget=min(budget.max_input_tokens, self.settings.research_context_token_budget),
            per_card_token_budget=self.settings.evidence_card_token_budget,
            max_cards=self.settings.research_context_max_cards,
            excluded_evidence_ids=quarantined_evidence_ids,
        )
        if not context_pack.cards:
            raise RuntimeError("no safe evidence cards available after injection quarantine")
        evidence_text = context_pack.rendered
        trace.add("context_compacted", card_count=len(context_pack.cards), token_count=context_pack.token_count,
                  excluded_evidence_ids=[str(item) for item in context_pack.excluded_evidence_ids])
        if checkpoint_phase == "bundle_verified" and checkpoint_payload.get("bundle"):
            bundle = ResearchBundle.model_validate(checkpoint_payload["bundle"])
            finding = self._verify_bundle_scope(bundle, evidence, request.competitors, route.dimensions, request.mode)
            trace.add("checkpoint_bundle_restored", finding_id=finding.finding_id)
        else:
            finding_prompt = json.dumps({
                "brief": request.brief,
                "route": route.model_dump(mode="json"),
                "evidence": evidence_text,
                "reusable_verified_knowledge": reusable_knowledge,
                "research_playbook": active_playbook.model_dump(mode="json"),
                "allowed_evidence_ids": [str(item.id) for item in evidence],
                "strict_rule": (
                    "Use only allowed evidence IDs; fact values must be verbatim excerpt substrings. Reused knowledge is "
                    "context only and must still cite supplied evidence. Cover both competitors and every requested "
                    "dimension. Claim confidence cannot exceed source credibility and finding confidence cannot exceed "
                    "its supporting claims. Prefer current official pages, enumerate every material source conflict in "
                    "contradictions, preserve unknowns, and provide an evidence-grounded decision implication. Do not "
                    "create claims outside route.dimensions or the brief, even if a source contains unrelated features. "
                    "Never assign a feature to a plan or tier unless the same source section explicitly links it to that "
                    "tier; otherwise mark unknown."
                ),
            }, ensure_ascii=False)
            with span("agent.researcher", tenant_id=context.tenant_id, run_id=str(trace.run_id)):
                bundle, finding = await self._research_bundle_with_repair(
                    finding_prompt,
                    evidence,
                    request.competitors,
                    route.dimensions,
                    request.mode,
                    trace,
                    budget,
                )
            trace.add("finding_verified", finding_id=finding.finding_id, evidence_ids=[str(x) for x in finding.evidence_ids])
            candidate_bundles = [bundle]
            for specialist in debate_plan.specialists:
                scoped_dimensions = specialist_dimensions(specialist, route.dimensions)
                specialist_prompt = json.dumps(
                    {
                        "brief": request.brief,
                        "mode": request.mode,
                        "specialist": specialist,
                        "dimensions": scoped_dimensions,
                        "candidate": bundle.model_dump(mode="json"),
                        "evidence": evidence_text,
                        "allowed_evidence_ids": [str(item.id) for item in evidence],
                        "rule": (
                            "Audit and correct only this specialist's dimensions. Return a complete ResearchBundle "
                            "for both named competitors. Use exact evidence substrings for facts, preserve conflicts "
                            "and unknowns, never assign a plan tier without an explicit same-section link, and end "
                            "the finding with an explicit Next action: sentence."
                        ),
                    },
                    ensure_ascii=False,
                )
                specialist_bundle, _ = await self._research_bundle_with_repair(
                    specialist_prompt,
                    evidence,
                    request.competitors,
                    scoped_dimensions,
                    request.mode,
                    trace,
                    budget,
                    agent_name=specialist,
                )
                candidate_bundles.append(specialist_bundle)
                trace.add(
                    "specialist_verified",
                    specialist=specialist,
                    finding_id=specialist_bundle.finding.finding_id,
                    claim_count=len(specialist_bundle.claims),
                )

            if debate_plan.challenger_required:
                challenge_result = await self._bounded_agent_call(
                    "challenger",
                    json.dumps(
                        {
                            "brief": request.brief,
                            "candidate": bundle.model_dump(mode="json"),
                            "specialist_bundles": [item.model_dump(mode="json") for item in candidate_bundles[1:]],
                            "evidence": evidence_text,
                            "allowed_evidence_ids": [str(item.id) for item in evidence],
                        },
                        ensure_ascii=False,
                    ),
                    trace,
                    budget,
                )
                challenge = self._apply_bundle_harness(
                    challenge_result.output, evidence, request.competitors, trace, "challenger"
                )
                self._verify_bundle_scope(challenge, evidence, request.competitors, route.dimensions, request.mode)
                candidate_bundles.append(challenge)
                trace.add("challenger_verified", finding_id=challenge.finding.finding_id)

            try:
                arbitration = await self._bounded_agent_call(
                    "arbitrator",
                    json.dumps(
                        {
                            "brief": request.brief,
                            "mode": request.mode,
                            "debate_plan": debate_plan.model_dump(mode="json"),
                            "candidate_bundles": [item.model_dump(mode="json") for item in candidate_bundles],
                            "evidence": evidence_text,
                            "allowed_evidence_ids": [str(item.id) for item in evidence],
                            "rule": (
                                "You are the terminal decision gate. Reconcile all candidate bundles into exactly one "
                                "complete ResearchBundle covering every requested competitor and dimension. Keep only "
                                "evidence-grounded claims, preserve material conflicts and unknowns, and prefer current "
                                "official sources. Do not invent, silently merge incompatible values, or add facts."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    trace,
                    budget,
                )
                bundle = self._apply_bundle_harness(
                    arbitration.output, evidence, request.competitors, trace, "arbitrator"
                )
                finding = self._verify_bundle_scope(bundle, evidence, request.competitors, route.dimensions, request.mode)
                trace.add("arbitrator_verified", finding_id=finding.finding_id)
            except Exception as exc:
                trace.add("arbitrator_contract_failed", error=type(exc).__name__, detail=str(exc)[:500])
                bundle, finding = self._arbitrator_lite(
                    candidate_bundles,
                    evidence,
                    request.competitors,
                    route.dimensions,
                    request.mode,
                    trace,
                )
            if self.repository:
                self.repository.save_checkpoint(idempotency_key, context.tenant_id, "bundle_verified", {
                    "trace": trace.model_dump(mode="json"),
                    "evidence": [item.model_dump(mode="json") for item in evidence],
                    "quarantined_evidence_ids": [str(item) for item in quarantined_evidence_ids],
                    "reusable_knowledge": reusable_knowledge,
                    "route": route.model_dump(mode="json"),
                    "search_plan": search_plan.model_dump(mode="json"),
                    "debate_plan": debate_plan.model_dump(mode="json"),
                    "bundle": bundle.model_dump(mode="json"),
                }, self.settings.run_checkpoint_lease_seconds)
        # Claims are validated against the immutable ledger before Writer sees the result.
        for card in context_pack.cards:
            self.ledger.card(card.evidence_ids, card.summary, card.excerpt, card.locator)
        report_prompt = json.dumps({"brief": request.brief, "verified_finding": finding.model_dump(mode="json"), "writer_rule": "Copy every field of verified_finding verbatim and completely. Do not rewrite, summarize, translate, truncate, reorder, add, or remove content."}, ensure_ascii=False)
        with span("agent.writer", tenant_id=context.tenant_id, run_id=str(trace.run_id)):
            report_result = await self._bounded_agent_call("writer", report_prompt, trace, budget)
        written_finding = report_result.output
        if written_finding != finding:
            trace.add("writer_contract_rejected", error="lossy_or_mutated_verified_finding")
            repair = json.dumps({"correction_required": True, "verified_finding": finding.model_dump(mode="json"), "writer_rule": "Return an exact field-for-field copy. Preserve the full statement, ordering, contradictions, unknowns, confidence, and all IDs. Do not make any semantic or formatting change."}, ensure_ascii=False)
            written_finding = (await self._bounded_agent_call("writer", repair, trace, budget, retries=1)).output
        if written_finding != finding:
            raise ValueError("writer lossless-publication contract failed after repair")
        report = CompetitiveResearchReport(
            title=request.brief,
            findings=[written_finding],
            evidence_ids=written_finding.evidence_ids,
            status="draft",
        )
        self.ledger.validate_ids(report.evidence_ids)
        trace.add("report_drafted", report_id=str(report.report_id), evidence_count=len(report.evidence_ids))
        trace.add("run_completed", status="draft")
        trace_path = self.settings.object_store_root / f"run-{trace.run_id}.json"
        trace_path.write_text(json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
        self.ledger.dump(self.settings.object_store_root / f"evidence-{trace.run_id}.json")
        trace_payload = json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, indent=2)
        evidence_payload = json.dumps([e.model_dump(mode="json") for e in evidence], ensure_ascii=False, indent=2)
        self.objects.put_text(f"tenants/{context.tenant_id}/runs/{trace.run_id}/trace.json", trace_payload, "application/json")
        self.objects.put_text(f"tenants/{context.tenant_id}/runs/{trace.run_id}/evidence.json", evidence_payload, "application/json")
        if self.repository:
            self.repository.save_run(context.tenant_id, idempotency_key, request, report, trace)
            reusable_evidence = [item for item in evidence if item.id not in quarantined_evidence_ids]
            self.repository.save_verified_knowledge(context.tenant_id, request.competitors, bundle, reusable_evidence,
                                                    trace.run_id, self.settings.knowledge_ttl_seconds)
            self.repository.complete_checkpoint(idempotency_key, context.tenant_id)
            self.repository.create_approval(context.tenant_id, report.report_id)
            self.repository.audit(context.tenant_id, context.user_id, "research.run.completed", {"run_id": str(trace.run_id), "report_id": str(report.report_id)})
        try:
            # Playbook evolution is a quality sidecar and cannot invalidate a
            # report that has already passed the production evidence contract.
            self.playbooks.record_replay(PlaybookReplay(
                replay_id=hashlib.sha256(f"{context.tenant_id}:{trace.run_id}".encode()).hexdigest()[:24],
                tenant_id=context.tenant_id, run_id=trace.run_id, brief=request.brief,
                selected_mode=request.mode, report_title=report.title,
                trace_events=[str(item.get("event")) for item in trace.events], contract_passed=True,
            ))
            candidate = self.playbooks.propose(context.tenant_id, trace.run_id, request.brief, request.mode)
            trace.add("playbook_candidate_created", candidate_id=str(candidate.candidate_id), base_version=candidate.base_version)
        except Exception as exc:
            trace.add("playbook_evolution_failed", error=type(exc).__name__)
        RUNS.labels(context.tenant_id, "completed").inc()
        RUN_SECONDS.observe(time.monotonic() - started_at)
        return report, trace
