"""Tenant-scoped product monitoring contracts. Unknown values stay unknown."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductSource(Contract):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["authorized_json", "public_product_page", "taobao_open_api"]
    origin: HttpUrl
    authorization_reference: str = Field(min_length=5, max_length=1000)
    min_interval_seconds: int = Field(default=60, ge=10, le=86400)
    render_js: bool = False
    credential_env: str | None = Field(default=None, pattern=r"^CI_SOURCE_[A-Z0-9_]+_TOKEN$")
    taobao_credentials_env: str | None = Field(default=None, pattern=r"^CI_SOURCE_[A-Z0-9_]+_TAOBAO_CREDENTIALS$")
    # JSON responses use {products: [ProductData, ...]}; a single ProductData is also valid.
    @model_validator(mode="after")
    def origin_only(self):
        if self.origin.username or self.origin.password or self.origin.query or self.origin.fragment:
            raise ValueError("source origin cannot contain credentials, query or fragment")
        if self.origin.path not in {None, "/"}:
            raise ValueError("source origin must not contain a path")
        if self.credential_env and self.kind != "authorized_json":
            raise ValueError("credentials are only supported for authorized JSON endpoints")
        if self.credential_env and self.origin.scheme != "https":
            raise ValueError("authenticated sources require HTTPS")
        if self.kind == "taobao_open_api":
            if str(self.origin) != "https://gw.api.taobao.com/":
                raise ValueError("Taobao official source requires https://gw.api.taobao.com")
            if not self.taobao_credentials_env or self.render_js:
                raise ValueError("Taobao official source requires credential reference and does not use browser rendering")
        elif self.taobao_credentials_env:
            raise ValueError("Taobao credential references are only allowed for the official adapter")
        return self


class ProductTarget(Contract):
    source_id: UUID
    url: HttpUrl
    product_id: str = Field(min_length=1, max_length=200)
    sku_id: str = Field(min_length=1, max_length=200)
    role: Literal["own", "competitor"] = "competitor"
    label: str = Field(min_length=1, max_length=200)


class CommerceRequest(Contract):
    kind: Literal["commerce"] = "commerce"
    brief: str = Field(min_length=3, max_length=4000)
    targets: list[ProductTarget] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_targets(self):
        keys = [(t.source_id, t.product_id, t.sku_id) for t in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate product/SKU target")
        return self


class ProductReview(Contract):
    review_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=4000)
    rating: Decimal | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)


class ProductData(Contract):
    product_id: str = Field(min_length=1, max_length=200)
    sku_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=2000)
    description: str | None = Field(default=None, max_length=12000)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    unit: str | None = Field(default=None, max_length=100)
    list_price: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    sale_price: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    promotion: str | None = Field(default=None, max_length=4000)
    availability: str | None = Field(default=None, max_length=500)
    rating: Decimal | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    review_count: int | None = Field(default=None, ge=0)
    reviews: list[ProductReview] = Field(default_factory=list, max_length=100)
    images: list[HttpUrl] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def currency_and_reviews(self):
        if (self.list_price is not None or self.sale_price is not None) and not self.currency:
            raise ValueError("a price requires an explicit currency")
        ids = [r.review_id for r in self.reviews]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate review IDs")
        return self


class ProductSnapshot(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot_id: UUID = Field(default_factory=uuid4)
    source_id: UUID
    source_url: HttpUrl
    role: Literal["own", "competitor"]
    label: str
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str
    artifact_key: str
    data: ProductData


class ProductChange(Contract):
    change_id: UUID = Field(default_factory=uuid4)
    previous_snapshot_id: UUID
    current_snapshot_id: UUID
    field: str
    before: object
    after: object
    kind: Literal["changed", "newly_observed", "unavailable"]
    relative_change: Decimal | None = None


class ReviewInsight(Contract):
    snapshot_id: UUID
    review_ids: list[str] = Field(min_length=1)
    theme: str = Field(min_length=1, max_length=300)
    sentiment: Literal["positive", "neutral", "negative", "mixed"]
    observation: str = Field(min_length=1, max_length=1500)
    quotes: list[str] = Field(min_length=1)


class OperationalAction(Contract):
    category: Literal["listing", "promotion", "pricing", "creative", "customer_experience"]
    priority: Literal["low", "medium", "high"]
    action: str = Field(min_length=10, max_length=2000)
    rationale: str = Field(min_length=10, max_length=2000)
    snapshot_ids: list[UUID] = Field(min_length=1)
    change_ids: list[UUID] = Field(default_factory=list)
    owner_role: str = Field(min_length=1, max_length=100)
    deliverable: str = Field(min_length=1, max_length=1500)
    success_metric: str = Field(min_length=1, max_length=500)
    prerequisites: list[str] = Field(min_length=1)


class CommerceAnalysis(Contract):
    summary: str = Field(min_length=10, max_length=4000)
    review_insights: list[ReviewInsight] = Field(default_factory=list, max_length=30)
    actions: list[OperationalAction] = Field(default_factory=list, max_length=15)
    unknowns: list[str] = Field(default_factory=list)


class CommerceReport(Contract):
    report_id: UUID = Field(default_factory=uuid4)
    snapshots: list[ProductSnapshot]
    changes: list[ProductChange]
    analysis: CommerceAnalysis
    status: Literal["draft", "approved"] = "draft"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
