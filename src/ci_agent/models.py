from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SourceType(StrEnum):
    WEBSITE = "website"
    PRICING = "pricing"
    DOCS = "docs"
    CHANGELOG = "changelog"
    GITHUB = "github"
    NEWS = "news"
    PDF = "pdf"
    SYNTHETIC = "synthetic_tool_result"


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    source_url: HttpUrl
    publisher: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    excerpt: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    source_type: SourceType = SourceType.WEBSITE
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    credibility: float = Field(ge=0, le=1)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    subject: str
    predicate: str
    value: str | int | float | bool
    unit: str | None = None
    evidence_ids: list[UUID] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    fact_or_inference: Literal["fact", "inference"]


class EvidenceCard(BaseModel):
    card_id: str
    evidence_ids: list[UUID] = Field(min_length=1)
    summary: str
    excerpt: str
    locator: str
    token_count: int = Field(ge=1, le=800)
    quality_checks: list[str] = Field(min_length=1)


class VerifiedFinding(BaseModel):
    finding_id: str
    statement: str = Field(min_length=20)
    claim_ids: list[UUID] = Field(min_length=1)
    evidence_ids: list[UUID] = Field(min_length=1)
    fact_or_inference: Literal["fact", "inference"]
    confidence: float = Field(ge=0, le=1)
    contradictions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class ResearchBundle(BaseModel):
    claims: list[Claim] = Field(min_length=1)
    finding: VerifiedFinding


class CompetitiveResearchReport(BaseModel):
    report_id: UUID = Field(default_factory=uuid4)
    title: str
    findings: list[VerifiedFinding] = Field(min_length=1)
    unknowns: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(min_length=1)
    status: Literal["draft", "approved", "published"] = "draft"


class ResearchRoute(BaseModel):
    task_kind: Literal["extract", "compare", "monitor", "deep_research", "verify"]
    complexity: Literal["simple", "standard", "high_stakes"]
    dimensions: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    required_agents: list[Literal["researcher", "pricing", "product", "security", "challenger", "arbitrator"]]
    max_agent_calls: int = Field(ge=0, le=4)
    debate_required: bool
    reason: str
    # Router output is advisory; the host converts it into a deterministic
    # DebatePlan after applying mode, dimension, and budget policy.
    specialists: list[Literal["pricing", "product", "security"]] = Field(default_factory=list)


class DebatePlan(BaseModel):
    """Executable specialist plan produced by the host routing policy."""

    specialists: list[Literal["pricing", "product", "security"]] = Field(default_factory=list, max_length=2)
    challenger_required: bool = False
    # Every run ends at an arbitrator. It may be an LLM arbitrator or the
    # deterministic lite gate when the LLM call exhausts its retry budget.
    arbitrator_required: bool = True
    max_agent_calls: int = Field(ge=2, le=4)
    rationale: str = Field(min_length=1)


class ModeDecision(BaseModel):
    requested_mode: Literal["auto", "investigation", "monitor", "verify"]
    selected_mode: Literal["investigation", "monitor", "verify"]
    confidence: float = Field(ge=0, le=1)
    signals: list[str] = Field(default_factory=list)
    router_validated: bool = False


class ResearchRequest(BaseModel):
    brief: str = Field(min_length=3)
    competitors: list[str] = Field(min_length=1, max_length=5)
    urls: list[HttpUrl] = Field(default_factory=list, max_length=20)
    mode: Literal["auto", "investigation", "monitor", "verify"] = "auto"


class TenantContext(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    role: Literal["viewer", "researcher", "approver", "admin"] = "researcher"


class RunBudget(BaseModel):
    max_searches: int = Field(default=12, ge=0)
    max_pages: int = Field(default=30, ge=0)
    max_pages_per_domain: int = Field(default=6, ge=0)
    max_model_requests: int = Field(default=24, ge=0)
    max_agent_calls: int = Field(default=4, ge=0, le=4)
    max_input_tokens: int = Field(default=120_000, ge=0)
    max_output_tokens: int = Field(default=30_000, ge=0)
    max_cost_cny: float = Field(default=3.0, ge=0)
    timeout_seconds: int = Field(default=900, ge=1)
    concurrency: int = Field(default=3, ge=1)


class RunTrace(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    events: list[dict[str, Any]] = Field(default_factory=list)

    def add(self, event: str, **data: Any) -> None:
        self.events.append({"at": datetime.now(timezone.utc).isoformat(), "event": event, **data})
