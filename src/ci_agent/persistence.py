from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Integer, JSON, LargeBinary, String, Text, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .models import CompetitiveResearchReport, Evidence, RunTrace


class Base(DeclarativeBase):
    pass


class RunRecord(Base):
    __tablename__ = "research_runs"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32))
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    trace_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    report_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class EvidenceRecord(Base):
    __tablename__ = "evidence_records"
    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    content_hash: Mapped[str] = mapped_column(String(80), index=True)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    object_key: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ApprovalRecord(Base):
    __tablename__ = "report_approvals"
    report_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditRecord(Base):
    __tablename__ = "audit_events"
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    actor_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(32), default="running")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PageCacheRecord(Base):
    __tablename__ = "page_cache"
    url: Mapped[str] = mapped_column(String(2048), primary_key=True)
    status_code: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(255))
    raw_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    clean_text: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class MonitorJobRecord(Base):
    __tablename__ = "monitor_jobs"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class RunCheckpointRecord(Base):
    __tablename__ = "run_checkpoints"
    idempotency_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    phase: Mapped[str] = mapped_column(String(64), default="started")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class VerifiedKnowledgeRecord(Base):
    __tablename__ = "verified_knowledge"
    knowledge_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    competitor: Mapped[str] = mapped_column(String(256), index=True)
    bundle_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    source_run_id: Mapped[str] = mapped_column(String(36), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PostgresRepository:
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url.replace("postgresql+asyncpg", "postgresql+psycopg2").replace("postgresql+psycopg://", "postgresql+psycopg2://"), pool_pre_ping=True)
        Base.metadata.create_all(self.engine)
        # create_all does not alter an existing local database; keep this tiny migration idempotent.
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS report_id VARCHAR(36)"))
            for statement in (
                "ALTER TABLE monitor_jobs ADD COLUMN IF NOT EXISTS next_run_at TIMESTAMPTZ",
                "ALTER TABLE monitor_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE monitor_jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 5",
                "ALTER TABLE monitor_jobs ADD COLUMN IF NOT EXISTS last_error TEXT",
                "ALTER TABLE monitor_jobs ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ",
            ):
                connection.execute(text(statement))

    def reserve_idempotency(self, key: str, tenant_id: str, run_id: UUID) -> RunRecord | None:
        """Atomically reserve a key; return the completed run when it already exists."""
        from sqlalchemy.exc import IntegrityError
        with Session(self.engine) as session:
            try:
                session.add(IdempotencyRecord(key=key, tenant_id=tenant_id, run_id=str(run_id), status="running"))
                session.commit()
                return None
            except IntegrityError:
                session.rollback()
                row = session.scalar(select(IdempotencyRecord).where(IdempotencyRecord.key == key, IdempotencyRecord.tenant_id == tenant_id))
                if row and row.status == "completed":
                    return session.scalar(select(RunRecord).where(RunRecord.run_id == row.run_id, RunRecord.tenant_id == tenant_id))
                if row and row.status == "failed":
                    session.delete(row)
                    session.commit()
                    return self.reserve_idempotency(key, tenant_id, run_id)
                raise RuntimeError("idempotency key is already in progress")

    def fail_idempotency(self, key: str, tenant_id: str) -> None:
        with Session(self.engine) as session:
            row = session.scalar(select(IdempotencyRecord).where(IdempotencyRecord.key == key, IdempotencyRecord.tenant_id == tenant_id))
            if row and row.status == "running":
                row.status = "failed"
                session.commit()

    def get_idempotent_run(self, key: str, tenant_id: str) -> RunRecord | None:
        with Session(self.engine) as s:
            return s.scalar(select(RunRecord).where(RunRecord.idempotency_key == key, RunRecord.tenant_id == tenant_id))

    def get_run(self, run_id: str, tenant_id: str) -> RunRecord | None:
        with Session(self.engine) as s:
            return s.scalar(select(RunRecord).where(RunRecord.run_id == run_id, RunRecord.tenant_id == tenant_id))

    def get_evidence(self, tenant_id: str, evidence_ids: list[str]) -> list[dict[str, Any]]:
        """Return only evidence belonging to the authenticated tenant."""
        if not evidence_ids:
            return []
        with Session(self.engine) as s:
            rows = s.scalars(select(EvidenceRecord).where(
                EvidenceRecord.tenant_id == tenant_id,
                EvidenceRecord.evidence_id.in_(evidence_ids),
            ))
            return [dict(row.evidence_json) for row in rows]

    def get_checkpoint(self, key: str, tenant_id: str) -> RunCheckpointRecord | None:
        with Session(self.engine) as s:
            return s.scalar(select(RunCheckpointRecord).where(RunCheckpointRecord.idempotency_key == key,
                                                               RunCheckpointRecord.tenant_id == tenant_id))

    def acquire_checkpoint(self, key: str, tenant_id: str, run_id: UUID, lease_seconds: int) -> tuple[str, dict[str, Any], str]:
        """Create or lease a resumable checkpoint without allowing concurrent resumes."""
        with Session(self.engine) as s:
            now = datetime.now(timezone.utc)
            row = s.scalar(select(RunCheckpointRecord).where(RunCheckpointRecord.idempotency_key == key,
                                                              RunCheckpointRecord.tenant_id == tenant_id).with_for_update())
            if row is None:
                row = RunCheckpointRecord(idempotency_key=key, tenant_id=tenant_id, run_id=str(run_id),
                                          phase="started", payload_json={})
                s.add(row)
            elif row.locked_until and row.locked_until > now:
                raise RuntimeError("research run checkpoint is currently leased")
            row.locked_until = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            s.commit()
            return row.phase, dict(row.payload_json or {}), row.run_id

    def save_checkpoint(self, key: str, tenant_id: str, phase: str, payload: dict[str, Any], lease_seconds: int) -> None:
        with Session(self.engine) as s:
            row = s.scalar(select(RunCheckpointRecord).where(RunCheckpointRecord.idempotency_key == key,
                                                              RunCheckpointRecord.tenant_id == tenant_id).with_for_update())
            if row is None:
                raise RuntimeError("checkpoint reservation is missing")
            now = datetime.now(timezone.utc)
            row.phase, row.payload_json, row.updated_at = phase, payload, now
            row.locked_until = now + timedelta(seconds=lease_seconds)
            s.commit()

    def release_checkpoint(self, key: str, tenant_id: str) -> None:
        with Session(self.engine) as s:
            row = s.scalar(select(RunCheckpointRecord).where(RunCheckpointRecord.idempotency_key == key,
                                                              RunCheckpointRecord.tenant_id == tenant_id))
            if row:
                row.locked_until = None
                row.updated_at = datetime.now(timezone.utc)
                s.commit()

    def complete_checkpoint(self, key: str, tenant_id: str) -> None:
        with Session(self.engine) as s:
            row = s.scalar(select(RunCheckpointRecord).where(RunCheckpointRecord.idempotency_key == key,
                                                              RunCheckpointRecord.tenant_id == tenant_id))
            if row:
                row.phase = "completed"
                row.locked_until = None
                row.updated_at = datetime.now(timezone.utc)
                s.commit()

    def save_verified_knowledge(self, tenant_id: str, competitors: list[str], bundle: Any,
                                evidence: list[Evidence], run_id: UUID, ttl_seconds: int) -> None:
        import uuid
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        with Session(self.engine) as s:
            for competitor in competitors:
                s.add(VerifiedKnowledgeRecord(
                    knowledge_id=str(uuid.uuid4()), tenant_id=tenant_id, competitor=competitor.strip().lower(),
                    bundle_json=bundle.model_dump(mode="json"),
                    evidence_json=[item.model_dump(mode="json") for item in evidence], source_run_id=str(run_id),
                    expires_at=expires,
                ))
            s.commit()

    def load_verified_knowledge(self, tenant_id: str, competitors: list[str]) -> list[VerifiedKnowledgeRecord]:
        names = {item.strip().lower() for item in competitors}
        if not names:
            return []
        with Session(self.engine) as s:
            return list(s.scalars(select(VerifiedKnowledgeRecord).where(
                VerifiedKnowledgeRecord.tenant_id == tenant_id,
                VerifiedKnowledgeRecord.competitor.in_(names),
                VerifiedKnowledgeRecord.expires_at > datetime.now(timezone.utc),
            ).order_by(VerifiedKnowledgeRecord.created_at.desc())))

    def save_run(self, tenant_id: str, idempotency_key: str, request: Any, report: CompetitiveResearchReport, trace: RunTrace) -> None:
        with Session(self.engine) as s:
            record = RunRecord(run_id=str(trace.run_id), tenant_id=tenant_id, status=report.status, report_id=str(report.report_id),
                               request_json=request.model_dump(mode="json"), report_json=report.model_dump(mode="json"),
                               trace_json=trace.model_dump(mode="json"), idempotency_key=idempotency_key)
            s.add(record)
            s.commit()
        with Session(self.engine) as s:
            row = s.get(IdempotencyRecord, idempotency_key)
            if row:
                row.status = "completed"
                s.commit()

    def save_evidence(self, tenant_id: str, evidence: Evidence, object_key: str) -> None:
        with Session(self.engine) as s:
            if s.get(EvidenceRecord, str(evidence.id)) is None:
                s.add(EvidenceRecord(evidence_id=str(evidence.id), tenant_id=tenant_id, content_hash=evidence.content_hash,
                                     evidence_json=evidence.model_dump(mode="json"), object_key=object_key))
                s.commit()

    def create_approval(self, tenant_id: str, report_id: UUID) -> None:
        with Session(self.engine) as s:
            if s.get(ApprovalRecord, str(report_id)) is None:
                s.add(ApprovalRecord(report_id=str(report_id), tenant_id=tenant_id))
                s.commit()

    def approve(self, tenant_id: str, report_id: UUID, reviewer_id: str, comment: str | None = None) -> None:
        with Session(self.engine) as s:
            row = s.get(ApprovalRecord, str(report_id))
            if row is None or row.tenant_id != tenant_id:
                raise PermissionError("approval is not visible to this tenant")
            row.status, row.reviewer_id, row.comment, row.approved_at = "approved", reviewer_id, comment, datetime.now(timezone.utc)
            if row.report_id:
                run = s.scalar(select(RunRecord).where(RunRecord.report_id == row.report_id, RunRecord.tenant_id == tenant_id))
                if run and run.report_json:
                    run.report_json = {**run.report_json, "status": "approved"}
                    run.status = "approved"
            s.commit()

    def audit(self, tenant_id: str, actor_id: str, action: str, payload: dict[str, Any]) -> None:
        with Session(self.engine) as s:
            s.add(AuditRecord(tenant_id=tenant_id, actor_id=actor_id, action=action, payload_json=redact(payload)))
            s.commit()

    def get_page_cache(self, url: str) -> PageCacheRecord | None:
        with Session(self.engine) as s:
            row = s.get(PageCacheRecord, url)
            if row and row.expires_at > datetime.now(timezone.utc):
                return row
            return None

    def put_page_cache(self, url: str, status_code: int, content_type: str, raw_bytes: bytes, clean_text: str, ttl_seconds: int) -> None:
        with Session(self.engine) as s:
            row = s.get(PageCacheRecord, url)
            values = dict(status_code=status_code, content_type=content_type, raw_bytes=raw_bytes, clean_text=clean_text, expires_at=datetime.now(timezone.utc).replace(microsecond=0))
            values["expires_at"] = values["expires_at"] + __import__("datetime").timedelta(seconds=ttl_seconds)
            if row:
                for key, value in values.items(): setattr(row, key, value)
            else:
                s.add(PageCacheRecord(url=url, **values))
            s.commit()

    def create_monitor_job(self, tenant_id: str, request: Any, interval_seconds: int) -> str:
        import uuid
        job_id = str(uuid.uuid4())
        with Session(self.engine) as s:
            now = datetime.now(timezone.utc)
            s.add(MonitorJobRecord(job_id=job_id, tenant_id=tenant_id, request_json=request.model_dump(mode="json"), interval_seconds=interval_seconds, next_run_at=now))
            s.commit()
        return job_id

    def list_monitor_jobs(self, tenant_id: str) -> list[MonitorJobRecord]:
        with Session(self.engine) as s:
            return list(s.scalars(select(MonitorJobRecord).where(MonitorJobRecord.tenant_id == tenant_id, MonitorJobRecord.enabled.is_(True))))

    def claim_due_monitor_jobs(self, worker_id: str, limit: int = 10) -> list[MonitorJobRecord]:
        """Claim due jobs with row locks; expired locks are recoverable after worker death."""
        with Session(self.engine) as s:
            now = datetime.now(timezone.utc)
            rows = list(s.scalars(select(MonitorJobRecord).where(MonitorJobRecord.enabled.is_(True), MonitorJobRecord.next_run_at <= now, (MonitorJobRecord.locked_until.is_(None) | (MonitorJobRecord.locked_until < now))).order_by(MonitorJobRecord.next_run_at).limit(limit).with_for_update(skip_locked=True)))
            for row in rows:
                row.locked_until = now.replace(microsecond=0) + __import__("datetime").timedelta(minutes=15)
                row.last_error = None
            s.commit()
            return rows

    def complete_monitor_job(self, job_id: str, interval_seconds: int) -> None:
        with Session(self.engine) as s:
            row = s.get(MonitorJobRecord, job_id)
            if row:
                now = datetime.now(timezone.utc); row.last_run_at = now; row.next_run_at = now + __import__("datetime").timedelta(seconds=interval_seconds); row.attempts = 0; row.locked_until = None; row.last_error = None; s.commit()

    def fail_monitor_job(self, job_id: str, error: str) -> None:
        with Session(self.engine) as s:
            row = s.get(MonitorJobRecord, job_id)
            if row:
                row.attempts += 1; row.last_error = redact(error)[:2000]; row.locked_until = None
                delay = min(3600, 2 ** min(row.attempts, 10) * 30)
                row.next_run_at = datetime.now(timezone.utc) + __import__("datetime").timedelta(seconds=delay)
                if row.attempts >= row.max_attempts: row.enabled = False
                s.commit()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if any(x in k.lower() for x in ("key", "token", "secret", "password", "authorization")) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and len(value) > 80 and ("sk-" in value or "tvly-" in value):
        return "[REDACTED]"
    return value
