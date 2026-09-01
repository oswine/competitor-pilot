from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

from .agents import model_catalog
from .models import ResearchRequest, TenantContext
from .orchestrator import ResearchOrchestrator
from .replay import export_bundle, export_markdown, load_replay
from .settings import settings
from .playbook import PlaybookCandidate, ResearchPlaybookStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="ci-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("models")
    research = sub.add_parser("research")
    research.add_argument("brief")
    research.add_argument("--competitor", action="append", required=True)
    research.add_argument("--url", action="append", default=[])
    research.add_argument("--mode", choices=["auto", "investigation", "monitor", "verify"], default="auto")
    replay = sub.add_parser("replay")
    replay.add_argument("run_id")
    replay.add_argument("--markdown")
    replay.add_argument("--bundle")
    playbook = sub.add_parser("playbook")
    playbook_sub = playbook.add_subparsers(dest="playbook_command", required=True)
    playbook_sub.add_parser("show")
    promote = playbook_sub.add_parser("promote")
    promote.add_argument("candidate_id")
    args = parser.parse_args()
    if args.command == "doctor":
        print(json.dumps({"python": True, "gemai_key": bool(settings.gemai_api_key), "tavily_key": bool(settings.tavily_api_key), "object_store": str(settings.object_store_root)}, indent=2))
    elif args.command == "models":
        path = Path("config/model_catalog.json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(model_catalog(settings), indent=2), encoding="utf-8")
        print(path)
    elif args.command == "replay":
        artifact = load_replay(settings.object_store_root, args.run_id)
        if args.markdown:
            export_markdown(artifact, Path(args.markdown))
        if args.bundle:
            export_bundle(artifact, Path(args.bundle))
        print(json.dumps(artifact, ensure_ascii=False, indent=2))
    elif args.command == "playbook":
        store = ResearchPlaybookStore(settings.object_store_root / "playbooks")
        tenant = settings.tenant_default
        if args.playbook_command == "show":
            active = store.active(tenant)
            candidates = []
            candidate_dir = store._dir(tenant) / "candidates"
            for path in candidate_dir.glob("*.json") if candidate_dir.exists() else []:
                try:
                    item = PlaybookCandidate.model_validate_json(path.read_text(encoding="utf-8"))
                    candidates.append({**item.model_dump(mode="json"), "evaluation": store.evaluate(item).model_dump(mode="json")})
                except Exception:
                    continue
            print(json.dumps({"active": active.model_dump(mode="json"), "replay_count": len(store.replays(tenant)), "candidates": candidates}, ensure_ascii=False, indent=2))
        else:
            evaluation = store.promote(UUID(args.candidate_id), tenant)
            print(json.dumps(evaluation.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        request = ResearchRequest(brief=args.brief, competitors=args.competitor, urls=args.url, mode=args.mode)
        context = TenantContext(tenant_id=settings.tenant_default, user_id="local", role="researcher")
        idempotency_key = hashlib.sha256((context.tenant_id + request.model_dump_json()).encode()).hexdigest()
        runner = ResearchOrchestrator(settings)
        try:
            report, trace = asyncio.run(runner.run(request, context, idempotency_key))
        except BaseException:
            if runner.repository:
                runner.repository.release_checkpoint(idempotency_key, context.tenant_id)
                runner.repository.fail_idempotency(idempotency_key, context.tenant_id)
            raise
        print(json.dumps({"report": report.model_dump(mode="json"), "trace": trace.model_dump(mode="json")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
