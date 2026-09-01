"""Merge successful case reruns into a complete JSONL evaluation artifact."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--override", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-count", type=int, default=30)
    args = parser.parse_args()

    base = load(Path(args.base))
    order = [row["case_id"] for row in base]
    if len(order) != len(set(order)):
        raise SystemExit("base results contain duplicate case IDs")
    merged = {row["case_id"]: row for row in base}
    replacements = []
    ignored_failures = []
    for raw_path in args.override:
        path = Path(raw_path)
        for row in load(path):
            case_id = row["case_id"]
            if case_id not in merged:
                raise SystemExit(f"override contains unknown case ID: {case_id}")
            if row.get("error"):
                ignored_failures.append(case_id)
                continue
            merged[case_id] = row
            replacements.append(case_id)

    rows = [merged[case_id] for case_id in order]
    if len(rows) != args.expected_count:
        raise SystemExit(f"expected {args.expected_count} rows, found {len(rows)}")
    errors = [row["case_id"] for row in rows if row.get("error")]
    if errors:
        raise SystemExit(f"merged results still contain failures: {', '.join(errors)}")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "rows": len(rows),
                "replacements": sorted(set(replacements)),
                "ignored_failed_overrides": sorted(set(ignored_failures)),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
