# CI-Real30

CI-Real30 is the real-source research-quality benchmark for the competitor-intelligence agent. It contains 30 frozen tasks across six real product pairs and five task families: pricing, AI capabilities, security, enterprise suitability, and fact verification.

The product universe and pricing baseline are selected from [ComparEdge Awesome SaaS Comparison Data](https://github.com/comparedge/awesome-saas-comparison-data), pinned to commit `cd518965af74332e47cc5145074c41742834d6b3` and used under CC BY 4.0. Attribution: **Data from ComparEdge (comparedge.com)**. AI and security evidence is collected from official vendor pages listed in `source_catalog.json`.

This benchmark does not use fictional products or `ci-bench.local` evidence. `build_ci_real.py` fetches public pages through the project Browser Harness, stores raw and cleaned snapshots, extracts relevance windows without an LLM, and freezes immutable Evidence hashes. Generated cases must not be edited to improve a model score.

The public repository includes cleaned text snapshots, source metadata, and content hashes. Full `.raw` third-party page captures remain local and are excluded from Git; run `build_ci_real.py` to recreate them when source terms permit. The frozen case excerpts are sufficient for the checked-in offline research-quality tasks.

Research quality is assessed only through five independent Pydantic Evals LLM judges:

- citation entailment;
- citation completeness;
- source quality;
- analysis quality;
- business usefulness.

Schema, security, tenant, and budget checks remain execution gates rather than score dimensions.

The evaluated execution path is the production path: automatic mode selection, Router, host-generated DebatePlan, Researcher, selected pricing/product/security specialists, optional Challenger, terminal Arbitrator, and lossless Writer. The JSONL output preserves the selected mode, route, DebatePlan, role-level trace, evidence, claims, finding, and report so a judge result can be audited without treating trace text as research evidence.

CI-Real is for real-source research quality. Monitor scheduling, retry, replay, tenant isolation, browser safety, and other engineering properties are covered by the offline CI-Bench contract suite rather than mixed into the five research-quality dimensions.

```powershell
$env:PYTHONPATH = "src"
python .\benchmarks\ci_real\build_ci_real.py
python .\scripts\run_research_eval_subset.py --cases benchmarks/ci_real/cases.jsonl --all --output data/ci-real30-agent-output.jsonl
python .\scripts\score_research_quality.py --input data/ci-real30-agent-output.jsonl --output data/ci-real30-five-dimension.json --judge-model claude-sonnet-5
```
