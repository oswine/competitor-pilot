from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any


def load_replay(root: Path, run_id: str) -> dict[str, Any]:
    """Load a run without network or model access."""
    trace_path = root / f"run-{run_id}.json"
    evidence_path = root / f"evidence-{run_id}.json"
    if not trace_path.exists() or not evidence_path.exists():
        raise FileNotFoundError(f"replay artifacts missing for {run_id}")
    return {
        "run_id": run_id,
        "trace": json.loads(trace_path.read_text(encoding="utf-8")),
        "evidence": json.loads(evidence_path.read_text(encoding="utf-8")),
        "offline": True,
    }


def load_persisted_replay(repository, objects, tenant_id: str, run_id: str) -> dict[str, Any]:
    row = repository.get_run(run_id, tenant_id)
    if row is None:
        raise PermissionError("run is not visible to this tenant")
    trace = row.trace_json
    evidence_key = f"tenants/{tenant_id}/runs/{run_id}/evidence.json"
    evidence = json.loads(objects.get_bytes(evidence_key).decode("utf-8"))
    return {"run_id": run_id, "tenant_id": tenant_id, "request": row.request_json, "report": row.report_json, "trace": trace, "evidence": evidence, "offline": True}


def export_markdown(replay: dict[str, Any], output: Path) -> Path:
    trace = replay["trace"]
    evidence = replay["evidence"]
    lines = [f"# Research replay {replay['run_id']}", "", "## Trace", ""]
    for event in trace.get("events", []):
        lines.append(f"- `{event.get('event')}`: {json.dumps(event, ensure_ascii=False)}")
    lines.extend(["", "## Evidence", ""])
    for item in evidence:
        lines.extend([f"### {item.get('id')}", f"- URL: {item.get('source_url')}", f"- Hash: `{item.get('content_hash')}`", "", item.get("excerpt", ""), ""])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def export_bundle(replay: dict[str, Any], output: Path) -> Path:
    """Export a self-contained, offline-readable replay bundle."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({"run_id": replay["run_id"], "offline": True}, ensure_ascii=False, indent=2))
        archive.writestr("trace.json", json.dumps(replay["trace"], ensure_ascii=False, indent=2))
        archive.writestr("evidence.json", json.dumps(replay["evidence"], ensure_ascii=False, indent=2))
        for item in replay["evidence"]:
            archive.writestr(f"evidence/{item['id']}.json", json.dumps(item, ensure_ascii=False, indent=2))
    return output
