"""Run five independent research-quality judges and emit an auditable report."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import LLMJudge
from pydantic_ai.settings import ModelSettings
from tenacity import stop_after_attempt, wait_exponential_jitter


RUBRIC_VERSION = "ci-real-five-dimension-v1"
RUBRICS = {
    "citation_entailment": (
        "Act as an adversarial citation auditor. Inspect EVERY fact Claim's subject, predicate, value, and cited "
        "excerpt, then inspect EVERY material assertion in the final finding, contradictions, unknowns, and Next "
        "action. A citation must entail the exact assertion, not merely be topically related; penalize tier/plan "
        "misattribution, product/source mismatch, unsupported URLs, cross-product reuse, over-specific paraphrase, "
        "and statements contradicted by another cited excerpt. A schema-valid Claim is not evidence of entailment. "
        "Score continuously with these anchors: 1.0 means no material defect; 0.9 means at most one minor wording "
        "issue; 0.75 means one material or several minor issues; 0.5 means multiple material issues; 0.25 means "
        "most claims are weakly supported; 0 means no usable report or pervasive contradiction. If the run has an "
        "error or no report, score 0."
    ),
    "citation_completeness": (
        "Act as a completeness auditor. Enumerate every material number, price, date, capability comparison, security "
        "statement, plan/tier assignment, contradiction, and strategic assertion in the final finding and verify that "
        "a relevant Claim and evidence citation supports it. Do not count claim_ids alone as coverage. Penalize any "
        "material assertion that is only implied, hidden in an inference, or supported by a mismatched source. Score "
        "continuously: 1.0 means all material assertions are covered; 0.9 allows one immaterial omission; 0.75 "
        "allows one material omission; 0.5 means several omissions; 0.25 means sparse coverage; 0 means no usable "
        "report or no citations. If the run has an error or no report, score 0."
    ),
    "source_quality": (
        "Act as a source auditor. Inspect the actual source_url, publisher, source_type, excerpt, and retrieved_at for "
        "each cited Claim. Reward current official vendor pricing, documentation, trust-center, and release-note pages "
        "whose scope matches the assertion; treat independent open datasets only as corroboration. Penalize stale, "
        "secondary, duplicate, weakly scoped, or publisher-mismatched citations, and penalize relying on a credibility "
        "number without checking the source. Score continuously: 1.0 means authoritative, current, well-matched sources "
        "throughout; 0.9 allows one minor source-quality limitation; 0.75 means one material limitation; 0.5 means "
        "several weak or mismatched sources; 0.25 means mostly secondary/unclear sources; 0 means no usable sources. "
        "If the run has an error or no report, score 0."
    ),
    "analysis_quality": (
        "Act as a research editor. Check coverage of BOTH named competitors and EVERY requested dimension, then verify "
        "that the comparison adds logically grounded differences rather than restating sources. Check fact-versus-"
        "inference labels, uncertainty and contradiction handling, plan/tier reasoning, and whether strategic conclusions "
        "follow from cited facts without importing outside knowledge. Penalize generic praise, unsupported causal claims, "
        "and conclusions that ignore a known conflict. Score continuously: 1.0 means complete, coherent, and fully "
        "grounded; 0.9 allows one minor clarity issue; 0.75 means one material reasoning gap; 0.5 means multiple gaps "
        "or weak comparison; 0.25 means mostly restatement; 0 means no usable analysis. If the run has an error or no "
        "report, score 0."
    ),
    "business_usefulness": (
        "Act as an enterprise decision reviewer. Check that the output directly answers the supplied task, names the "
        "decision-relevant differences, explains implications and risks, states meaningful unknowns, and proposes a "
        "proportionate Next action grounded in the evidence. Penalize fabricated links, generic advice, missing buyer "
        "criteria, or recommendations that outrun the evidence. Verification tasks may be concise but must state what "
        "is verified and what remains unknown. Score continuously: 1.0 means immediately decision-useful with no material "
        "gap; 0.9 allows one minor gap; 0.75 means one material usefulness gap; 0.5 means several gaps; 0.25 means "
        "generic or weakly actionable; 0 means no usable report. If the run has an error or no report, score 0."
    ),
}


class DimensionJudge(LLMJudge):
    dimension: str = "quality"

    def get_default_evaluation_name(self) -> str:
        return self.dimension


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def result_value(result: Any) -> float:
    value = getattr(result, "value", result)
    if isinstance(value, bool):
        return float(value)
    return max(0.0, min(1.0, float(value)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="data/research-quality-five-dimension.json")
    parser.add_argument("--judge-model", default="claude-sonnet-5")
    parser.add_argument("--judge-concurrency", type=int, default=3)
    parser.add_argument("--expected-count", type=int, default=30)
    parser.add_argument("--minimum-score", type=float, default=0.8)
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="evaluate only this case ID; repeat to select a deterministic subset",
    )
    parser.add_argument("--llm-judge", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    input_path = Path(args.input)
    rows = [json.loads(line) for line in input_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.case_id:
        requested = set(args.case_id)
        rows = [row for row in rows if row.get("case_id") in requested]
        missing = requested - {row.get("case_id") for row in rows}
        if missing:
            raise SystemExit(f"unknown case IDs: {', '.join(sorted(missing))}")
    if len(rows) != args.expected_count:
        raise SystemExit(f"expected {args.expected_count} rows, found {len(rows)}")
    if len({row["case_id"] for row in rows}) != len(rows):
        raise SystemExit("duplicate case IDs in evaluation input")

    from evaluate_bench import build_judge_model

    model = build_judge_model(args.judge_model)
    judges = []
    # A judge only needs a compact JSON score and a one-sentence reason. A
    # bounded completion also keeps GemAI's Claude pre-authorization within a
    # realistic evaluation budget.
    judge_settings = ModelSettings(temperature=0.0, max_tokens=512, timeout=120)
    for name, rubric in RUBRICS.items():
        judge = DimensionJudge(
            rubric=rubric,
            model=model,
            model_settings=judge_settings,
            include_input=True,
            include_expected_output=False,
            score={"include_reason": True},
            assertion=False,
        )
        judge.dimension = name
        judges.append(judge)

    def judge_output(row: dict[str, Any]) -> dict[str, Any]:
        output = row.get("output")
        if not isinstance(output, dict):
            return {"error": row.get("error", "missing output")}
        # Keep the judge prompt focused on publishable content. Evidence is
        # supplied once in the Input section below; traces and context cards
        # are engineering diagnostics, not research-quality evidence.
        return {
            "report": output.get("report"),
            "finding": output.get("finding"),
            "claims": output.get("claims", []),
            "mode_decision": output.get("mode_decision"),
        }

    result_map = {row["case_id"]: judge_output(row) for row in rows}
    judge_inputs = [
        {
            "case_id": row["case_id"],
            "track": row.get("track"),
            "task": row.get("task"),
            "benchmark_metadata": row.get("benchmark_metadata", {}),
            "evidence": row.get("output", {}).get("evidence", []),
        }
        for row in rows
    ]
    dataset = Dataset[dict[str, Any], dict[str, Any], dict[str, Any]](
        name="ci-real-five-dimension",
        cases=[Case(name=item["case_id"], inputs=item, expected_output={}) for item in judge_inputs],
        evaluators=judges,
    )
    evaluated = dataset.evaluate_sync(
        lambda item: result_map[item["case_id"]],
        max_concurrency=max(1, args.judge_concurrency),
        progress=True,
        retry_evaluators={
            "stop": stop_after_attempt(3),
            "wait": wait_exponential_jitter(initial=1, max=8),
            "reraise": True,
        },
        metadata={
            "judge_model": args.judge_model,
            "rubric_version": RUBRIC_VERSION,
            "input_sha256": sha256_file(input_path),
        },
    )

    case_reports = []
    values: dict[str, list[float]] = {name: [] for name in RUBRICS}
    for report_case in evaluated.cases:
        scores = {}
        reasons = {}
        for name in RUBRICS:
            result = report_case.scores.get(name)
            score = result_value(result) if result is not None else 0.0
            scores[name] = score
            reasons[name] = getattr(result, "reason", None) if result is not None else "judge result missing"
            values[name].append(score)
        case_reports.append(
            {
                "case_id": report_case.name,
                "scores": scores,
                "reasons": reasons,
                "evaluator_failures": [str(item) for item in report_case.evaluator_failures],
            }
        )

    averages = {name: statistics.fmean(scores) if scores else 0.0 for name, scores in values.items()}
    threshold_passed = all(score > args.minimum_score for score in averages.values())
    failed_runs = [row["case_id"] for row in rows if row.get("error")]
    payload = {
        "evaluation": "five_dimension_llm_judge",
        "rubric_version": RUBRIC_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "judge": {
            "provider": "gemai_openai_compatible",
            "model": args.judge_model,
            "concurrency": max(1, args.judge_concurrency),
        },
        "input": {
            "path": str(input_path),
            "sha256": sha256_file(input_path),
            "case_count": len(rows),
            "completed_agent_runs": len(rows) - len(failed_runs),
            "failed_agent_runs": failed_runs,
        },
        "minimum_score_exclusive": args.minimum_score,
        "threshold_passed": threshold_passed,
        "averages": averages,
        "rubrics": RUBRICS,
        "cases": case_reports,
        "judge_failures": [str(item) for item in evaluated.failures],
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output_path), "averages": averages, "threshold_passed": threshold_passed}, ensure_ascii=False))
    if not threshold_passed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
