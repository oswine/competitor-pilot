from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ci_agent.browser import BrowserCollector  # noqa: E402
from ci_agent.evidence import EvidenceLedger  # noqa: E402
from ci_agent.settings import Settings  # noqa: E402

HERE = Path(__file__).resolve().parent
CATALOG_PATH = HERE / "source_catalog.json"
CASES_PATH = HERE / "cases.jsonl"
MANIFEST_PATH = HERE / "manifest.json"
FROZEN_SOURCES_PATH = HERE / "sources.json"
SNAPSHOT_ROOT = HERE / "snapshots"
UPSTREAM_ROOT = HERE / "upstream"


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def content_hash(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def relevant_excerpt(text: str, keywords: list[str], limit: int = 7000) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    lowered = clean.lower()
    windows: list[tuple[int, int]] = []
    for keyword in keywords:
        start = 0
        for _ in range(4):
            index = lowered.find(keyword.lower(), start)
            if index < 0:
                break
            windows.append((max(0, index - 360), min(len(clean), index + len(keyword) + 560)))
            start = index + len(keyword)
    if not windows:
        return clean[:limit]
    windows.sort()
    merged: list[list[int]] = []
    for start, end in windows:
        if merged and start <= merged[-1][1] + 80:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    excerpt = "\n...\n".join(clean[start:end].strip() for start, end in merged)
    return excerpt[:limit]


def source_type(dimension: str) -> str:
    return "pricing" if dimension == "pricing" else "docs"


async def fetch_official_sources(catalog: dict) -> dict[str, dict[str, dict]]:
    settings = Settings(postgres_enabled=False, object_store_root=HERE / ".build-ledger")
    ledger = EvidenceLedger(HERE / ".build-ledger")
    collector = BrowserCollector(settings, ledger)
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    frozen: dict[str, dict[str, dict]] = (
        json.loads(FROZEN_SOURCES_PATH.read_text(encoding="utf-8"))
        if FROZEN_SOURCES_PATH.exists()
        else {}
    )
    try:
        for product in catalog["products"]:
            frozen.setdefault(product["name"], {})
            for dimension in ("pricing", "ai", "security"):
                spec = product[dimension]
                cached = frozen[product["name"]].get(dimension)
                if cached and cached.get("requested_url") == spec["url"]:
                    raw_path = HERE / cached["snapshot_path"]
                    clean_path = HERE / cached["clean_snapshot_path"]
                    if raw_path.exists() and clean_path.exists() and content_hash(cached["excerpt"]) == cached["content_hash"]:
                        print(f"reuse {product['name']} {dimension}: {spec['url']}", flush=True)
                        continue
                print(f"fetch {product['name']} {dimension}: {spec['url']}", flush=True)
                transport = "http"
                try:
                    snapshot = await collector.fetch(spec["url"], bypass_cache=True)
                except (httpx.HTTPError, PermissionError):
                    snapshot = await collector.fetch(spec["url"], use_browser=True, bypass_cache=True)
                    transport = "playwright"
                excerpt = relevant_excerpt(snapshot.clean, spec["keywords"])
                keyword_hits = sum(keyword.lower() in excerpt.lower() for keyword in spec["keywords"])
                if len(excerpt) < 500 or keyword_hits < 1:
                    snapshot = await collector.fetch(spec["url"], use_browser=True, bypass_cache=True)
                    transport = "playwright"
                    excerpt = relevant_excerpt(snapshot.clean, spec["keywords"])
                    keyword_hits = sum(keyword.lower() in excerpt.lower() for keyword in spec["keywords"])
                if len(excerpt) < 500 or keyword_hits < 1:
                    raise RuntimeError(f"insufficient source content: {product['name']} {dimension}")
                if snapshot.injection_flags:
                    raise RuntimeError(f"prompt-injection flags in source: {product['name']} {dimension}")
                raw = snapshot.raw.encode("utf-8") if isinstance(snapshot.raw, str) else snapshot.raw
                snapshot_digest = hashlib.sha256(raw).hexdigest()
                raw_path = SNAPSHOT_ROOT / f"{snapshot_digest}.raw"
                clean_path = SNAPSHOT_ROOT / f"{snapshot_digest}.txt"
                if not raw_path.exists():
                    raw_path.write_bytes(raw)
                if not clean_path.exists():
                    clean_path.write_text(snapshot.clean, encoding="utf-8")
                evidence_key = f"{slug(product['name'])}-{dimension}"
                frozen[product["name"]][dimension] = {
                    "id": f"real-{evidence_key}",
                    "requested_url": spec["url"],
                    "source_url": snapshot.url,
                    "publisher": product["name"],
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "content_hash": content_hash(excerpt),
                    "snapshot_hash": f"sha256:{snapshot_digest}",
                    "snapshot_path": str(raw_path.relative_to(HERE)).replace("\\", "/"),
                    "clean_snapshot_path": str(clean_path.relative_to(HERE)).replace("\\", "/"),
                    "excerpt": excerpt,
                    "locator": f"{snapshot.locator};relevance-window={','.join(spec['keywords'])}",
                    "source_type": source_type(dimension),
                    "credibility": 0.95 if dimension in {"pricing", "security"} else 0.93,
                    "keyword_hits": keyword_hits,
                    "capture_transport": transport,
                }
                FROZEN_SOURCES_PATH.write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        await collector.close()
    return frozen


async def fetch_comparedge(catalog: dict) -> dict[str, dict]:
    upstream = catalog["upstream"]
    url = (
        "https://raw.githubusercontent.com/comparedge/awesome-saas-comparison-data/"
        f"{upstream['commit']}/data/products-full.json"
    )
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "CI-Agent/1.0"})
        response.raise_for_status()
    products = response.json()
    selected_names = {product["name"] for product in catalog["products"]}
    selected = {item["name"]: item for item in products if item.get("name") in selected_names}
    UPSTREAM_ROOT.mkdir(parents=True, exist_ok=True)
    (UPSTREAM_ROOT / "comparedge-selected.json").write_text(
        json.dumps(
            {
                "attribution": upstream["attribution"],
                "license": upstream["license"],
                "repository": upstream["repository"],
                "commit": upstream["commit"],
                "records": selected,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return selected


def comparedge_evidence(product: str, record: dict, catalog: dict) -> dict:
    excerpt = json.dumps(
        {
            "name": record.get("name"),
            "category": record.get("category"),
            "pricing": record.get("pricing"),
            "rating": record.get("rating"),
            "verifiedAt": record.get("verifiedAt"),
        },
        ensure_ascii=False,
    )
    commit = catalog["upstream"]["commit"]
    return {
        "id": f"real-{slug(product)}-comparedge",
        "source_url": f"https://github.com/comparedge/awesome-saas-comparison-data/blob/{commit}/data/products-full.json",
        "publisher": "ComparEdge",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": content_hash(excerpt),
        "excerpt": excerpt,
        "locator": f"JSON record name={product}",
        "source_type": "website",
        "credibility": 0.82,
    }


def make_cases(catalog: dict, frozen: dict[str, dict[str, dict]], comparedge: dict[str, dict]) -> list[dict]:
    cases: list[dict] = []
    prompts = {
        "pricing": "比较 {a} 与 {b} 当前公开定价、计费单位和企业购买方式。区分公开标价、联系销售项目与未知项，并给出采购含义。",
        "ai": "比较 {a} 与 {b} 官方公开的 AI 能力及其产品集成方式。不要推断证据未说明的套餐可用性，并给出企业采用时的关键差异。",
        "security": "比较 {a} 与 {b} 官方公开的安全、身份、加密和合规能力。只陈述证据明确支持的控制，并指出采购核验缺口。",
        "enterprise": "为企业选型比较 {a} 与 {b} 的定价模式、AI 能力和安全治理。综合多源证据，区分事实、推断与未知，并给出可执行的下一步。",
        "verify": "核实 {a} 与 {b} 是否在公开官方材料中明确说明企业身份访问、加密或合规能力。未在证据出现的能力必须标为未知，不能解释为不支持。",
    }
    dimensions_for_task = {
        "pricing": ("pricing",),
        "ai": ("ai", "pricing"),
        "security": ("security",),
        "enterprise": ("pricing", "ai", "security"),
        "verify": ("security",),
    }
    for pair_index, (a, b) in enumerate(catalog["pairs"], 1):
        for task_index, task_kind in enumerate(("pricing", "ai", "security", "enterprise", "verify"), 1):
            evidence: list[dict] = []
            for product in (a, b):
                evidence.extend(frozen[product][dimension] for dimension in dimensions_for_task[task_kind])
                if task_kind == "pricing" and product in comparedge:
                    evidence.append(comparedge_evidence(product, comparedge[product], catalog))
            cases.append(
                {
                    "case_id": f"REAL-{pair_index:02d}-{task_index:02d}",
                    "split": "test",
                    "track": f"real_{task_kind}",
                    "task": {
                        "prompt": prompts[task_kind].format(a=a, b=b),
                        "competitors": [a, b],
                        "dimensions": list(dimensions_for_task[task_kind]),
                        "mode": "verify" if task_kind == "verify" else "investigation",
                    },
                    "evidence": evidence,
                    "metadata": {
                        "dataset": catalog["dataset"],
                        "version": catalog["version"],
                        "real_products": True,
                        "real_public_sources": True,
                        "frozen_evidence": True,
                        "pair": [a, b],
                    },
                }
            )
    return cases


async def main() -> None:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    frozen, comparedge = await asyncio.gather(fetch_official_sources(catalog), fetch_comparedge(catalog))
    cases = make_cases(catalog, frozen, comparedge)
    CASES_PATH.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False, sort_keys=True) for case in cases) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "dataset": catalog["dataset"],
        "version": catalog["version"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases),
        "source_count": sum(len(dimensions) for dimensions in frozen.values()) + len(comparedge),
        "official_source_count": sum(len(dimensions) for dimensions in frozen.values()),
        "corroboration_source_count": len(comparedge),
        "track_counts": {
            track: sum(case["track"] == track for case in cases)
            for track in sorted({case["track"] for case in cases})
        },
        "cases_sha256": content_hash(CASES_PATH.read_bytes()),
        "upstream": catalog["upstream"],
        "products": [product["name"] for product in catalog["products"]],
        "pairs": catalog["pairs"],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
