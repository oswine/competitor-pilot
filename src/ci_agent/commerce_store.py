"""Transactional commerce state and domain access reservations in PostgreSQL."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import DateTime, JSON, String, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from .persistence import Base


class CommerceRecord(Base):
    __tablename__ = "commerce_records"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class DomainAccessRecord(Base):
    __tablename__ = "domain_access"
    host: Mapped[str] = mapped_column(String(255), primary_key=True)
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failures: Mapped[int] = mapped_column(default=0)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)


class AccessPaused(PermissionError):
    pass


class CommerceStore:
    def __init__(self, engine):
        self.engine = engine

    def get(self, tenant: str, key: str):
        with Session(self.engine) as session:
            row = session.get(CommerceRecord, (tenant, key))
            return dict(row.payload) if row else None

    def put(self, tenant: str, key: str, kind: str, payload: dict):
        with Session(self.engine) as session:
            session.execute(insert(CommerceRecord).values(tenant_id=tenant, key=key, kind=kind, payload=payload)
                            .on_conflict_do_update(index_elements=["tenant_id", "key"], set_={"payload": payload}))
            session.commit()

    def list(self, tenant: str, kind: str, limit: int = 100):
        with Session(self.engine) as session:
            rows = session.scalars(select(CommerceRecord).where(CommerceRecord.tenant_id == tenant, CommerceRecord.kind == kind)
                                   .order_by(CommerceRecord.created_at.desc()).limit(limit))
            return [dict(row.payload) for row in rows]

    def add_source(self, tenant, source):
        source_id = str(uuid4())
        value = {"source_id": source_id, "enabled": True, "config": source.model_dump(mode="json")}
        self.put(tenant, "source:" + source_id, "source", value)
        return value

    @staticmethod
    def target_key(target):
        key = f"{target.source_id}|{target.product_id}|{target.sku_id}"
        return "latest:" + hashlib.sha256(key.encode()).hexdigest()

    def publish(self, tenant, run_key, request_hash, report):
        """Report, immutable snapshots and baseline pointers commit together."""
        payload = report.model_dump(mode="json")
        with Session(self.engine) as session:
            values = [("report:" + str(report.report_id), "report", payload),
                      ("result:" + run_key, "result", {"request_hash": request_hash, "report": payload})]
            for snapshot in report.snapshots:
                value = snapshot.model_dump(mode="json")
                values.append(("snapshot:" + str(snapshot.snapshot_id), "snapshot", value))
                identity = type("Identity", (), {"source_id": snapshot.source_id,
                                "product_id": snapshot.data.product_id, "sku_id": snapshot.data.sku_id})()
                latest_key = self.target_key(identity)
                # Serialise updates to this SKU; never replace a newer observation.
                session.execute(insert(CommerceRecord).values(tenant_id=tenant, key=latest_key, kind="baseline", payload=value)
                                .on_conflict_do_nothing())
                latest = session.scalar(select(CommerceRecord).where(CommerceRecord.tenant_id == tenant,
                                        CommerceRecord.key == latest_key).with_for_update())
                if datetime.fromisoformat(latest.payload["captured_at"]) <= snapshot.captured_at:
                    latest.payload = value
            for key, kind, value in values:
                session.execute(insert(CommerceRecord).values(tenant_id=tenant, key=key, kind=kind, payload=value)
                                .on_conflict_do_nothing())
            session.commit()

    def approve(self, tenant, report_id, actor):
        with Session(self.engine) as session:
            row = session.scalar(select(CommerceRecord).where(CommerceRecord.tenant_id == tenant,
                                 CommerceRecord.key == "report:" + str(report_id)).with_for_update())
            if row is None:
                raise KeyError("report not found")
            row.payload = {**row.payload, "status": "approved"}
            session.add(CommerceRecord(tenant_id=tenant, key="approval:" + str(uuid4()), kind="approval",
                        payload={"report_id": str(report_id), "actor": actor, "at": datetime.now(timezone.utc).isoformat()}))
            session.commit()

    def reserve_access(self, host, interval):
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            session.execute(insert(DomainAccessRecord).values(host=host, next_at=now, failures=0).on_conflict_do_nothing())
            row = session.scalar(select(DomainAccessRecord).where(DomainAccessRecord.host == host).with_for_update())
            if row.paused_until and row.paused_until > now:
                raise AccessPaused(f"domain paused: {row.reason}; until {row.paused_until.isoformat()}")
            if row.next_at > now:
                return (row.next_at - now).total_seconds()
            row.next_at = now + timedelta(seconds=interval)
            session.commit()
            return 0.0

    def record_access(self, host, *, error: str | None = None, cooldown: float = 0):
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            session.execute(insert(DomainAccessRecord).values(host=host, next_at=now, failures=0).on_conflict_do_nothing())
            row = session.scalar(select(DomainAccessRecord).where(DomainAccessRecord.host == host).with_for_update())
            if error:
                row.failures += 1
                row.reason = error
                if cooldown or row.failures >= 3:
                    until = now + timedelta(seconds=max(cooldown, 300))
                    row.paused_until = max(until, row.paused_until or until)
            elif not row.paused_until or row.paused_until <= now:
                row.failures = 0
                row.reason = None
                row.paused_until = None
            session.commit()
