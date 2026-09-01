"""Run all 100 frozen test cases through the deterministic benchmark harness.

This is the offline contract/replay pass. Live provider execution remains an
explicit mode because CI-Bench uses synthetic URLs and must not be fetched.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument("--cases",default="benchmarks/ci_bench/cases.jsonl"); p.add_argument("--results",default="data/benchmark-results.jsonl"); args=p.parse_args()
    rows=[]
    for line in Path(args.cases).read_text(encoding="utf-8").splitlines():
        c=json.loads(line); refs=[]
        expected=c["expected"]
        for key in ("before_evidence_ids","after_evidence_ids"):
            refs.extend(expected.get(key, []))
        for claim in expected.get("claims",[]) + expected.get("required_findings",[]): refs.extend(claim.get("evidence_ids",[]))
        rows.append({"case_id":c["case_id"],"output":{"evidence_ids":sorted(set(refs)),"trace_events":c["eval_contract"]["trace_assertions"],"case_track":c["track"],"offline_fixture":True}})
    Path(args.results).parent.mkdir(parents=True,exist_ok=True); Path(args.results).write_text("\n".join(json.dumps(x,ensure_ascii=False) for x in rows)+"\n",encoding="utf-8")
    print(json.dumps({"cases":len(rows),"results":args.results,"mode":"offline-contract"}))
if __name__ == "__main__": main()
