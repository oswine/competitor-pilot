"""Controlled evolution of research guidance, isolated from evidence contracts."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ResearchPlaybook(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = "1.0.0"
    dimensions: list[str] = Field(default_factory=list, max_length=20)
    query_templates: list[str] = Field(default_factory=list, max_length=20)
    writer_guidance: str = "Separate facts, inferences, and unknowns."
    trigger_conditions: list[str] = Field(default_factory=list, max_length=20)


class PlaybookCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: UUID = Field(default_factory=uuid4)
    tenant_id: str
    base_version: str
    playbook: ResearchPlaybook
    source_runs: list[UUID] = Field(default_factory=list)
    replay_count: int = 0
    deterministic_score: float = Field(default=0, ge=0, le=1)
    status: Literal["candidate", "approved", "rejected"] = "candidate"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PlaybookReplay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replay_id: str
    tenant_id: str
    run_id: UUID
    brief: str
    selected_mode: str
    report_title: str
    trace_events: list[str] = Field(default_factory=list)
    contract_passed: bool
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PlaybookEvaluation(BaseModel):
    candidate_id: UUID | None = None
    replay_count: int
    passed: int
    hard_failures: int
    score: float = Field(ge=0, le=1)
    eligible_for_promotion: bool
    reasons: list[str] = Field(default_factory=list)


def _stable(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]


class ResearchPlaybookStore:
    """Filesystem-backed, tenant-scoped store; PostgreSQL remains source of truth for runs/evidence."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, tenant_id: str) -> Path:
        path = self.root / hashlib.sha256(tenant_id.encode()).hexdigest()[:24]
        path.mkdir(parents=True, exist_ok=True)
        return path

    def active(self, tenant_id: str) -> ResearchPlaybook:
        path = self._dir(tenant_id) / "active.json"
        if not path.exists():
            playbook = ResearchPlaybook()
            path.write_text(playbook.model_dump_json(indent=2), encoding="utf-8")
            return playbook
        return ResearchPlaybook.model_validate_json(path.read_text(encoding="utf-8"))

    def record_replay(self, replay: PlaybookReplay) -> None:
        path = self._dir(replay.tenant_id) / "replay_pool.jsonl"
        existing = {item.replay_id: item for item in self.replays(replay.tenant_id)}
        existing[replay.replay_id] = replay
        with path.open("w", encoding="utf-8") as handle:
            for item in sorted(existing.values(), key=lambda row: row.replay_id):
                handle.write(item.model_dump_json() + "\n")

    def replays(self, tenant_id: str) -> list[PlaybookReplay]:
        path = self._dir(tenant_id) / "replay_pool.jsonl"
        if not path.exists():
            return []
        rows: list[PlaybookReplay] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(PlaybookReplay.model_validate_json(line))
            except Exception:
                continue
        return rows

    def propose(self, tenant_id: str, run_id: UUID, brief: str, selected_mode: str) -> PlaybookCandidate:
        current = self.active(tenant_id)
        low = brief.lower()
        dimensions = list(current.dimensions)
        signals = {
            "price": ("价格", "定价", "pricing", "price"),
            "ai": ("ai", "人工智能", "功能"),
            "security": ("安全", "sso", "scim", "合规", "security"),
            "implementation": ("实施", "部署", "集成", "implementation"),
        }
        for name, terms in signals.items():
            if any(term in low for term in terms) and name not in dimensions:
                dimensions.append(name)
        candidate = ResearchPlaybook(
            version=f"{current.version.rsplit('.', 1)[0]}.{int(current.version.rsplit('.', 1)[-1]) + 1}",
            dimensions=dimensions,
            query_templates=current.query_templates,
            writer_guidance=current.writer_guidance,
            trigger_conditions=sorted(set(current.trigger_conditions + [selected_mode])),
        )
        result = PlaybookCandidate(tenant_id=tenant_id, base_version=current.version, playbook=candidate, source_runs=[run_id])
        path = self._dir(tenant_id) / "candidates"; path.mkdir(exist_ok=True)
        (path / f"{result.candidate_id}.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result

    def evaluate(self, candidate: PlaybookCandidate) -> PlaybookEvaluation:
        replays = self.replays(candidate.tenant_id)
        if not replays:
            return PlaybookEvaluation(candidate_id=candidate.candidate_id, replay_count=0, passed=0, hard_failures=0, score=0, eligible_for_promotion=False, reasons=["no replay samples"])
        passed = sum(1 for item in replays if item.contract_passed)
        failures = len(replays) - passed
        score = passed / len(replays)
        reasons = [] if failures == 0 else [f"{failures} replay contract failure(s)"]
        eligible = len(replays) >= 2 and failures == 0 and score >= 1.0
        return PlaybookEvaluation(candidate_id=candidate.candidate_id, replay_count=len(replays), passed=passed, hard_failures=failures, score=score, eligible_for_promotion=eligible, reasons=reasons or ["candidate passed frozen replay gate"])

    def promote(self, candidate_id: UUID, tenant_id: str) -> PlaybookEvaluation:
        path = self._dir(tenant_id) / "candidates" / f"{candidate_id}.json"
        if not path.exists():
            raise FileNotFoundError("playbook candidate not found")
        candidate = PlaybookCandidate.model_validate_json(path.read_text(encoding="utf-8"))
        if candidate.tenant_id != tenant_id:
            raise PermissionError("playbook candidate is not visible to this tenant")
        evaluation = self.evaluate(candidate)
        if not evaluation.eligible_for_promotion:
            raise ValueError("candidate failed promotion gate: " + "; ".join(evaluation.reasons))
        active_path = self._dir(tenant_id) / "active.json"
        active_path.write_text(candidate.playbook.model_dump_json(indent=2), encoding="utf-8")
        candidate.status = "approved"
        path.write_text(candidate.model_dump_json(indent=2), encoding="utf-8")
        return evaluation
