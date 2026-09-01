# CI-Bench v0

CI-Bench is the unified offline benchmark for the competitor-intelligence agent. It combines the former benchmark tracks into one JSONL dataset with a stable case contract.

## Public benchmark provenance

The corpus is methodologically derived from five public research-agent benchmarks:

- [BrowseComp-Plus](https://github.com/texttron/BrowseComp-Plus): complex web retrieval, multi-hop evidence discovery, and retriever/agent separation.
- [ReportBench](https://github.com/ByteDance-BandAI/ReportBench): long-form reports, citation alignment, citation completeness, and source coverage.
- [DRBench](https://github.com/ServiceNow/drbench): enterprise open-ended deep research and multi-source synthesis.
- [GAIA](https://huggingface.co/gaia-benchmark): multi-step tool use, routing, bounded execution, and recovery.
- [DeepResearchEval](https://github.com/Infinity-AILab/DeepResearchEval): dynamic task construction, active fact verification, and process-oriented research evaluation.

CI-Bench does not copy, translate, or merge their original questions, evidence, or answers. Its 100 cases are newly generated synthetic competitor-intelligence fixtures with fictional products, stable `ci-bench.local` URLs, and immutable evidence. The public benchmarks informed the task taxonomy and evaluation design; they are not upstream row-level data sources.

## Contents

- `cases.jsonl`: 100 cases, one JSON object per line.
- `schema.py`: Pydantic models for validating the dataset, track-specific expectations, and the `Evidence -> EvidenceCard -> VerifiedFinding -> CompetitiveResearchReport` context contract.
- `generate_ci_bench.py`: deterministic generator for rebuilding the dataset.
- `validate_ci_bench.py`: validation and coverage report.
- `manifest.json`: dataset metadata, counts, and generation fingerprint.

## Tracks

| Track | Count | What it tests |
|---|---:|---|
| `change_detection` | 30 | Material page changes versus navigation/Cookie/noise changes |
| `fact_extraction` | 25 | Typed product, pricing, date, quota, and positioning claims |
| `research_synthesis` | 20 | Multi-source findings, contradictions, unknowns, and citations |
| `routing` | 15 | Router choice and bounded, need-based agent activation |
| `reliability_security` | 10 | Rate limits, malformed output, prompt injection, isolation, and replay |
| **Total** | **100** | |

Every case is in the single `test` split. CI-Bench is for evaluating the final complete system; it is not a training set, development set, prompt-tuning set, or model-selection set.

Before running it, freeze the agent version, model IDs, model settings, prompts, tool definitions, retrieval policy, budget policy, and evaluator version. Do not use individual case results to tune the system and then report the same run as an unbiased score. Changes should be made against separate local fixtures or newly collected post-release cases, followed by a fresh final test run.

## Offline evidence contract

Evidence is intentionally synthetic and immutable for v0. It uses stable `ci-bench.local` URLs and content hashes. The benchmark runner must never fetch these URLs. A future live set can replace the evidence payload while preserving the case schema.

Each evidence item contains:

- `id`
- `source_url`
- `publisher`
- `retrieved_at`
- `content_hash`
- `excerpt`
- `locator`
- `source_type`
- `valid_from` / `valid_to`
- `credibility`

The `expected` object is track-specific but always declares deterministic checks. Claims and findings must cite one or more evidence IDs. Unsupported claims are a benchmark failure.

## Run

```powershell
python .\generate_ci_bench.py
python .\validate_ci_bench.py
```

The generator is deterministic. It rewrites `cases.jsonl` and `manifest.json`; no network access is used.

## Evaluation role

CI-Bench is an offline engineering-contract suite. It checks schema, evidence, security, budget, routing, retries, recovery, tenant boundaries, browser policy, and artifact behavior. These checks are execution gates and are not mixed into research-quality scores.

Research quality is assessed with the real-source `benchmarks/ci_real/` corpus and `scripts/score_research_quality.py`, using five independent Pydantic Evals LLM judges: citation entailment, citation completeness, source quality, analysis quality, and business usefulness.

The benchmark is intentionally offline and immutable. It is not a live monitoring set; live traffic and future drift should be reported separately as production monitoring metrics.

## Coverage guarantee

The benchmark is intended to be the single evaluation corpus for the first production milestone. Its per-case `eval_contract` includes outcome metrics, trace assertions, execution budgets, context-layer budgets, security assertions, resilience assertions, and artifact/replay assertions. The validator fails if any required family is absent from the 100-case corpus.

The v0 corpus covers:

- schema and structured-output validity;
- exact and normalized facts, prices, dates, quotas, and change events;
- evidence existence, locator integrity, citation completeness, source provenance, contradiction handling, and unknown calibration;
- route selection, required/forbidden agents, tool order, bounded agent activation, concurrency, and cost/request budgets;
- transport retry, model fallback, tool retry, output rejection, step resume, idempotency, cache, and compaction;
- robots policy, rate limits, PDF/page locators, recorded replay, browser isolation, private-network blocking, prompt-injection blocking, tenant scope, human approval, and secret redaction.
