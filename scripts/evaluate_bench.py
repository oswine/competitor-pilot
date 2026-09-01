"""Evaluate frozen CI-Bench results with pydantic-evals.

The default path is deterministic and does not call an LLM. Pass --llm-judge
to add Pydantic Evals' LLMJudge for subjective report quality.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "ci_bench"))
from schema import CIBenchCase


def build_judge_model(model_id: str):
    """Use the same GemAI OpenAI-compatible transport as production agents."""
    from ci_agent.agents import build_model
    from ci_agent.settings import Settings
    return build_model(Settings(), model_id)


@dataclass
class ContractEvaluator(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> bool:
        case = CIBenchCase.model_validate(ctx.inputs)
        output = ctx.output or {}
        checks = {"schema_valid": isinstance(output, dict)}
        expected_ids = {e.id for e in case.evidence}
        actual_ids = set(output.get("evidence_ids", []))
        needs_evidence = case.track.value not in {"routing", "reliability_security"}
        checks["evidence_ids_present"] = bool(actual_ids) if needs_evidence else True
        checks["evidence_ids_known"] = actual_ids.issubset(expected_ids)
        checks["trace_present"] = bool(output.get("trace_events", []))
        return all(checks.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="benchmarks/ci_bench/cases.jsonl")
    parser.add_argument("--results", required=True, help="JSONL: {case_id, output}")
    parser.add_argument("--report", default="data/eval-report.json")
    parser.add_argument("--llm-judge", action="store_true")
    parser.add_argument("--judge-model", default="claude-sonnet-5")
    args = parser.parse_args()
    cases = [json.loads(line) for line in Path(args.cases).read_text(encoding="utf-8").splitlines() if line.strip()]
    result_map = {row["case_id"]: row.get("output", {}) for row in (json.loads(line) for line in Path(args.results).read_text(encoding="utf-8").splitlines() if line.strip())}
    evaluators: list[Any] = [ContractEvaluator()]
    if args.llm_judge:
        evaluators.append(LLMJudge(
            rubric=("Score the candidate report from 0 to 1 on factual accuracy, citation entailment, "
                    "citation completeness, source quality, required-dimension coverage, analysis quality, "
                    "and business usefulness. Penalize unsupported prices, dates, numbers, and strategic claims. "
                    "Return a concise reason for each weakness. Judge only the supplied input, evidence and output."),
            model=build_judge_model(args.judge_model),
            include_input=True,
            include_expected_output=True,
            score={"include_reason": True},
            assertion=False,
        ))
    dataset = Dataset[dict[str, Any], dict[str, Any], dict[str, Any]](
        name="ci-bench-test-only",
        cases=[Case(name=case["case_id"], inputs=case, expected_output=result_map.get(case["case_id"], {})) for case in cases],
        evaluators=evaluators,
    )
    report = dataset.evaluate_sync(lambda case: result_map.get(case["case_id"], {"missing": True}))
    payload = report.model_dump(mode="json") if hasattr(report, "model_dump") else {"report": str(report)}
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": args.report, "llm_judge": args.llm_judge}, ensure_ascii=False))


if __name__ == "__main__":
    main()
