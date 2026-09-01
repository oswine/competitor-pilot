from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .models import TenantContext
from .persistence import Base, PostgresRepository


class ApiKeyRecord(Base):
    __tablename__ = "tenant_api_keys"
    key_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def key_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ApiKeyManager:
    def __init__(self, repository: PostgresRepository):
        self.repository = repository
        Base.metadata.create_all(repository.engine)

    def bootstrap(self, tenant_id: str, role: str, raw_key: str) -> None:
        digest = key_hash(raw_key)
        with Session(self.repository.engine) as s:
            if s.scalar(select(ApiKeyRecord).where(ApiKeyRecord.key_hash == digest)) is None:
                s.add(ApiKeyRecord(key_id=secrets.token_hex(8), tenant_id=tenant_id, key_hash=digest, role=role))
                s.commit()

    def authenticate(self, raw_key: str, user_id: str) -> TenantContext:
        digest = key_hash(raw_key)
        with Session(self.repository.engine) as s:
            row = s.scalar(select(ApiKeyRecord).where(ApiKeyRecord.key_hash == digest))
            now = datetime.now(timezone.utc)
            if row is None or not hmac.compare_digest(row.key_hash, digest) or row.revoked_at or (row.expires_at and row.expires_at < now):
                raise PermissionError("invalid or expired API key")
            return TenantContext(tenant_id=row.tenant_id, user_id=user_id, role=row.role)

    def rotate(self, context: TenantContext, old_key: str, grace_seconds: int) -> str:
        new_key = "ci_" + secrets.token_urlsafe(32)
        with Session(self.repository.engine) as s:
            old = s.scalar(select(ApiKeyRecord).where(ApiKeyRecord.key_hash == key_hash(old_key), ApiKeyRecord.tenant_id == context.tenant_id))
            if old is None:
                raise PermissionError("key does not belong to tenant")
            old.expires_at = datetime.now(timezone.utc) + timedelta(seconds=grace_seconds)
            s.add(ApiKeyRecord(key_id=secrets.token_hex(8), tenant_id=context.tenant_id, key_hash=key_hash(new_key), role=context.role))
            s.commit()
        return new_key
