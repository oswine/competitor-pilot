from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks" / "ci_real"


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in (BENCH / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_ci_real_manifest_and_distribution() -> None:
    manifest = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))
    raw = (BENCH / "cases.jsonl").read_bytes()
    cases = load_cases()

    assert manifest["dataset"] == "CI-Real30"
    assert manifest["case_count"] == len(cases) == 30
    assert manifest["source_count"] == 42
    assert manifest["official_source_count"] == 33
    assert manifest["corroboration_source_count"] == 9
    assert manifest["cases_sha256"] == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert Counter(case["track"] for case in cases) == {
        "real_pricing": 6,
        "real_ai": 6,
        "real_security": 6,
        "real_enterprise": 6,
        "real_verify": 6,
    }


def test_ci_real_uses_real_sources_and_covers_both_products() -> None:
    for case in load_cases():
        assert case["metadata"]["real_products"] is True
        assert case["metadata"]["real_public_sources"] is True
        assert case["metadata"]["frozen_evidence"] is True
        assert all("ci-bench.local" not in item["source_url"] for item in case["evidence"])
        assert all(item["source_type"] != "synthetic_tool_result" for item in case["evidence"])
        publishers = {item["publisher"] for item in case["evidence"]}
        for competitor in case["task"]["competitors"]:
            assert competitor in publishers
        for item in case["evidence"]:
            digest = hashlib.sha256(item["excerpt"].encode("utf-8")).hexdigest()
            assert item["content_hash"] == f"sha256:{digest}"
