from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class Track(StrEnum):
    CHANGE_DETECTION = "change_detection"
    FACT_EXTRACTION = "fact_extraction"
    RESEARCH_SYNTHESIS = "research_synthesis"
    ROUTING = "routing"
    RELIABILITY_SECURITY = "reliability_security"


class Split(StrEnum):
    TEST = "test"


class SourceType(StrEnum):
    WEBSITE = "website"
    PRICING = "pricing"
    DOCS = "docs"
    CHANGELOG = "changelog"
    GITHUB = "github"
    NEWS = "news"
    RSS = "rss"
    SYNTHETIC_TOOL_RESULT = "synthetic_tool_result"


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^ev-[a-z0-9-]+$")
    source_url: HttpUrl
    publisher: str | None
    retrieved_at: datetime
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    excerpt: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    source_type: SourceType
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    credibility: float = Field(ge=0, le=1)


class ClaimExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    predicate: str
    value: Any
    evidence_ids: list[str] = Field(min_length=1)
    fact_or_inference: Literal["fact", "inference"] = "fact"


class EvidenceCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    card_id: str
    evidence_ids: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)
    token_count: int = Field(ge=1)
    source_urls: list[HttpUrl] = Field(min_length=1)
    quality_checks: list[str] = Field(min_length=1)


class VerifiedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    statement: str = Field(min_length=1)
    claim_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    fact_or_inference: Literal["fact", "inference"]
    confidence: float = Field(ge=0, le=1)
    contradiction_ids: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    token_count: int = Field(ge=1)


class CompetitiveResearchReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    title: str = Field(min_length=1)
    finding_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    unknowns: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    token_count: int = Field(ge=1)


class BaseExpected(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChangeExpected(BaseExpected):
    event_type: str
    material: bool
    before_evidence_ids: list[str] = Field(min_length=1)
    after_evidence_ids: list[str] = Field(min_length=1)
    required_fields: dict[str, Any] = Field(default_factory=dict)


class ExtractionExpected(BaseExpected):
    claims: list[ClaimExpectation] = Field(min_length=1)
    required_unknowns: list[str] = Field(default_factory=list)


class ResearchExpected(BaseExpected):
    required_findings: list[ClaimExpectation] = Field(min_length=1)
    required_evidence_coverage: float = Field(ge=0, le=1)
    must_report_unknowns: bool = True
    contradiction_count: int = Field(ge=0)


class RoutingExpected(BaseExpected):
    task_kind: str
    complexity: Literal["simple", "standard", "high_stakes"]
    required_agents: list[str]
    forbidden_agents: list[str] = Field(default_factory=list)
    max_agent_calls: int = Field(ge=0, le=10)
    debate_required: bool


class ReliabilityExpected(BaseExpected):
    expected_behavior: Literal[
        "retry_then_succeed",
        "fallback_model",
        "block_tool_result",
        "reject_invalid_tool_args",
        "resume_without_duplicate_side_effect",
        "deny_private_network",
        "require_human_approval",
        "redact_sensitive_data",
    ]
    max_attempts: int = Field(ge=0, le=10)
    must_not: list[str] = Field(default_factory=list)


class EvalContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome_metrics: list[str] = Field(min_length=1)
    trace_assertions: list[str] = Field(min_length=1)
    budget: dict[str, int | float] = Field(default_factory=dict)
    context_layers: dict[str, int] = Field(default_factory=dict)
    security_assertions: list[str] = Field(default_factory=list)
    resilience_assertions: list[str] = Field(default_factory=list)
    artifact_assertions: list[str] = Field(default_factory=list)


class Task(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str = Field(min_length=1)
    competitor: str | None = None
    competitors: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    before_snapshot_id: str | None = None
    after_snapshot_id: str | None = None
    failure_mode: str | None = None


class CIBenchCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^(CHG|EXT|RES|ROU|REL)-\d{3}$")
    split: Split
    track: Track
    difficulty: Literal["easy", "medium", "hard"]
    tags: list[str] = Field(min_length=1)
    task: Task
    evidence: list[Evidence] = Field(default_factory=list)
    expected: ChangeExpected | ExtractionExpected | ResearchExpected | RoutingExpected | ReliabilityExpected
    eval_contract: EvalContract
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_track_contract(self) -> "CIBenchCase":
        evidence_ids = {item.id for item in self.evidence}
        if self.track == Track.CHANGE_DETECTION:
            if not isinstance(self.expected, ChangeExpected):
                raise ValueError("change_detection requires ChangeExpected")
        elif self.track == Track.FACT_EXTRACTION:
            if not isinstance(self.expected, ExtractionExpected):
                raise ValueError("fact_extraction requires ExtractionExpected")
        elif self.track == Track.RESEARCH_SYNTHESIS:
            if not isinstance(self.expected, ResearchExpected):
                raise ValueError("research_synthesis requires ResearchExpected")
        elif self.track == Track.ROUTING:
            if not isinstance(self.expected, RoutingExpected):
                raise ValueError("routing requires RoutingExpected")
        elif self.track == Track.RELIABILITY_SECURITY:
            if not isinstance(self.expected, ReliabilityExpected):
                raise ValueError("reliability_security requires ReliabilityExpected")

        if isinstance(self.expected, ChangeExpected):
            referenced = set(self.expected.before_evidence_ids + self.expected.after_evidence_ids)
            missing = referenced - evidence_ids
            if missing:
                raise ValueError(f"{self.case_id}: missing change evidence IDs {sorted(missing)}")
        for field in ("claims", "required_findings"):
            values = getattr(self.expected, field, [])
            for claim in values:
                missing = set(claim.evidence_ids) - evidence_ids
                if missing:
                    raise ValueError(f"{self.case_id}: missing evidence IDs {sorted(missing)}")
        return self
