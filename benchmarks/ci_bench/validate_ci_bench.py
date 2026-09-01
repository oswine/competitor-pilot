from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from schema import CIBenchCase


ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "cases.jsonl"


def main() -> None:
    cases = []
    errors: list[str] = []
    for line_number, line in enumerate(CASES_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = CIBenchCase.model_validate(json.loads(line))
            cases.append(case)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            errors.append(f"line {line_number}: {exc}")
    if errors:
        raise SystemExit("\n".join(errors))
    if len(cases) != 100:
        raise SystemExit(f"Expected 100 cases, got {len(cases)}")
    non_test = [case.case_id for case in cases if case.split.value != "test"]
    if non_test:
        raise SystemExit(f"CI-Bench must contain only test cases: {non_test}")
    track_counts = Counter(case.track.value for case in cases)
    split_counts = Counter(case.split.value for case in cases)
    duplicate_ids = [case_id for case_id, count in Counter(case.case_id for case in cases).items() if count > 1]
    if duplicate_ids:
        raise SystemExit(f"Duplicate case IDs: {duplicate_ids}")
    required_context_layers = {"raw_snapshot", "evidence_card", "verified_finding", "final_report"}
    all_outcome_metrics = {metric for case in cases for metric in case.eval_contract.outcome_metrics}
    all_trace_assertions = {item for case in cases for item in case.eval_contract.trace_assertions}
    all_security_assertions = {item for case in cases for item in case.eval_contract.security_assertions}
    all_resilience_assertions = {item for case in cases for item in case.eval_contract.resilience_assertions}
    all_artifact_assertions = {item for case in cases for item in case.eval_contract.artifact_assertions}
    expected_outcome = {
        "schema_valid", "citation_exists", "citation_completeness", "finding_coverage",
        "event_type_exact", "materiality_exact", "required_fields_exact", "route_kind_exact",
        "required_agents_present", "forbidden_agents_absent", "expected_failure_behavior",
    }
    expected_trace = {"plan", "search", "fetch", "validate_structured_output", "route_once", "activate_only_selected_agents", "terminal_state_recorded"}
    expected_security = {"untrusted_web_content_is_data", "prompt_injection_blocked", "egress_allowlist_enforced", "human_approval_required", "secret_redaction", "tenant_scope_rechecked_in_tool"}
    expected_resilience = {"transport_retry_bounded", "model_fallback_without_retry_multiplication", "tool_retry_bounded", "output_retry_or_rejection", "step_resume_idempotent", "compaction_preserves_verified_findings", "oversized_tool_output_bounded_or_spilled", "concurrency_limit_respected"}
    expected_artifacts = {"raw_snapshot_retained", "content_hash_verified", "dom_locator_present", "evidence_cards_retained", "verified_findings_retained", "recorded_tool_result_replayable", "pdf_page_locator_present"}
    for name, required, actual in [
        ("outcome metrics", expected_outcome, all_outcome_metrics),
        ("trace assertions", expected_trace, all_trace_assertions),
        ("security assertions", expected_security, all_security_assertions),
        ("resilience assertions", expected_resilience, all_resilience_assertions),
        ("artifact assertions", expected_artifacts, all_artifact_assertions),
    ]:
        missing = required - actual
        if missing:
            raise SystemExit(f"Coverage gap in {name}: {sorted(missing)}")
    for case in cases:
        if set(case.eval_contract.context_layers) != required_context_layers:
            raise SystemExit(f"{case.case_id}: context layer contract must contain {sorted(required_context_layers)}")
        if any(value <= 0 for value in case.eval_contract.context_layers.values() if value != 0):
            raise SystemExit(f"{case.case_id}: context budgets must be positive or explicitly zero")
        if not case.eval_contract.budget:
            raise SystemExit(f"{case.case_id}: missing execution budget")
    print(f"valid cases: {len(cases)}")
    print(f"tracks: {dict(sorted(track_counts.items()))}")
    print(f"splits: {dict(sorted(split_counts.items()))}")
    print("invariants: evidence IDs, citation references, URLs, hashes, track contracts, budgets, and eval coverage pass")
    print(f"coverage: outcomes={len(all_outcome_metrics)}, traces={len(all_trace_assertions)}, security={len(all_security_assertions)}, resilience={len(all_resilience_assertions)}, artifacts={len(all_artifact_assertions)}")


if __name__ == "__main__":
    main()
