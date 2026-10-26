"""Opt-in real PostgreSQL tests, isolated in a fresh random schema."""
import asyncio
import hashlib
import io
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from ci_agent.browser import BrowserCollector
from ci_agent.commerce import CommerceService
from ci_agent.commerce_api import commerce_router
from ci_agent.commerce_models import CommerceAnalysis, CommerceRequest, ProductSource, ProductTarget
from ci_agent.commerce_store import AccessPaused, CommerceStore, DomainAccessRecord
from ci_agent.evidence import EvidenceLedger
from ci_agent.models import TenantContext
from ci_agent.persistence import Base, MonitorJobRecord, PostgresRepository
from ci_agent.settings import Settings


@pytest.fixture
def repo():
    url = os.getenv("CI_TEST_DATABASE_URL")
    if not url:
        pytest.skip("CI_TEST_DATABASE_URL required for PostgreSQL integration")
    schema = "ci_test_" + uuid4().hex
    root = create_engine(url)
    with root.begin() as conn:
        conn.execute(text('CREATE SCHEMA "' + schema + '"'))
    engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
    Base.metadata.create_all(engine)
    repository = PostgresRepository.__new__(PostgresRepository)
    repository.engine = engine
    yield repository
    engine.dispose()
    assert schema.startswith("ci_test_") and len(schema) == 40
    with root.begin() as conn:
        conn.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
    root.dispose()


class Objects:
    def __init__(self):
        self.data = {}
    def put_bytes(self, key, body, content_type=None):
        self.data[key] = body
    def get_bytes(self, key):
        return self.data[key]


def make_service(repo, tmp_path, monkeypatch):
    settings = Settings(_env_file=None, run_timeout_seconds=30)
    objects = Objects()
    browser = BrowserCollector(settings, EvidenceLedger(tmp_path), repo)
    calls = []
    state = {"price": "100", "fail": False, "not_modified": False}
    async def get(url, **kwargs):
        calls.append(kwargs)
        if state["not_modified"]:
            return httpx.Response(304)
        return httpx.Response(200, json={"product_id": "one", "sku_id": "blue", "title": "Shoe", "currency": "USD", "sale_price": state["price"],
            "description": "Blue running shoe; see size chart", "unit": "pair", "promotion": "Seasonal sale",
            "reviews": [{"review_id": "r1", "text": "Sizing is smaller than expected", "rating": 2}]}, headers={"etag": state["price"]})
    monkeypatch.setattr(browser.access, "get", get)
    async def run(prompt, **kwargs):
        if state["fail"]:
            raise RuntimeError("test model failure")
        return SimpleNamespace(output=CommerceAnalysis(summary="A grounded product operations observation.", unknowns=["Margins are not provided."]))
    service = CommerceService(settings, repo, objects, browser, analyst=SimpleNamespace(run=run))
    source = service.store.add_source("a", ProductSource(name="Authorized feed", kind="authorized_json", origin="https://shop.example.com", authorization_reference="Test authorization"))
    target = ProductTarget(source_id=source["source_id"], url="https://shop.example.com/feed", product_id="one", sku_id="blue", label="Shoe")
    request = CommerceRequest(brief="Analyze price changes", targets=[target])
    return service, request, TenantContext(tenant_id="a", user_id="tester"), calls, state


def test_collect_resume_diff_idempotency_and_conditional_get(repo, tmp_path, monkeypatch):
    service, request, context, calls, state = make_service(repo, tmp_path, monkeypatch)
    async def exercise():
        state["fail"] = True
        with pytest.raises(RuntimeError, match="model failure"):
            await service.run(request, context, "first")
        assert len(calls) == 1
        state["fail"] = False
        first = await service.run(request, context, "first")
        assert len(calls) == 1 and first.changes == []
        again = await service.run(request, context, "first")
        assert again.report_id == first.report_id
        with pytest.raises(ValueError, match="different request"):
            await service.run(request.model_copy(update={"brief": "Different request"}), context, "first")
        state["price"] = "80"
        second = await service.run(request, context, "second")
        assert calls[-1]["headers"]["If-None-Match"] == "100"
        assert second.changes[0].field == "sale_price"
        assert float(second.changes[0].relative_change) == -0.2
        state["not_modified"] = True
        third = await service.run(request, context, "third")
        assert third.changes == []
        assert third.snapshots[0].content_hash == second.snapshots[0].content_hash
        with pytest.raises(PermissionError):
            await service.run(request, TenantContext(tenant_id="b", user_id="other"), "first")
    asyncio.run(exercise())


def test_real_postgres_domain_reservation_and_shared_pause(repo):
    a, b = CommerceStore(repo.engine), CommerceStore(repo.engine)
    assert a.reserve_access("shop.example.com", 60) == 0
    assert b.reserve_access("shop.example.com", 60) > 0
    a.record_access("shop.example.com", error="http_429", cooldown=600)
    with pytest.raises(AccessPaused):
        b.reserve_access("shop.example.com", 60)


def test_monitor_tenant_lease_retry_and_manual_resume(repo, tmp_path, monkeypatch):
    service, request, context, _, _ = make_service(repo, tmp_path, monkeypatch)
    a = repo.create_monitor_job("a", request, 60)
    b = repo.create_monitor_job("b", request, 60)
    jobs = repo.claim_due_monitor_jobs("w1", tenant_id="a")
    assert [j.job_id for j in jobs] == [a]
    assert repo.claim_due_monitor_jobs("w2", tenant_id="a") == []
    assert repo.renew_monitor_job(a, "w1")
    assert not repo.renew_monitor_job(a, "w2")
    with pytest.raises(RuntimeError, match="lease lost"):
        repo.complete_monitor_job(a, 60, "w2")
    with Session(repo.engine) as session:
        row = session.get(MonitorJobRecord, a)
        row.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    assert repo.claim_due_monitor_jobs("w2", tenant_id="a")[0].job_id == a
    repo.fail_monitor_job(a, "upstream unavailable", "w2")
    row = repo.list_monitor_jobs("a")[0]
    assert row.attempts == 1 and row.next_run_at > datetime.now(timezone.utc)
    for _ in range(4):
        repo.fail_monitor_job(a, "upstream unavailable")
    assert not repo.list_monitor_jobs("a")[0].enabled
    with pytest.raises(KeyError):
        repo.set_monitor_enabled("b", a, True)
    repo.set_monitor_enabled("a", a, True)
    assert repo.list_monitor_jobs("a")[0].attempts == 0


def test_api_report_permissions_export_and_approval(repo, tmp_path, monkeypatch):
    service, request, context, _, _ = make_service(repo, tmp_path, monkeypatch)
    report = asyncio.run(service.run(request, context, "api"))
    def auth(x_tenant: str = Header("a"), x_role: str = Header("viewer")):
        return TenantContext(tenant_id=x_tenant, user_id="tester", role=x_role)
    app = FastAPI()
    app.include_router(commerce_router(service, auth))
    with TestClient(app) as client:
        root = "/v1/commerce/reports/" + str(report.report_id)
        assert client.get(root, headers={"x-tenant": "b"}).status_code == 404
        assert client.post(root + "/approve").status_code == 403
        assert client.post(root + "/approve", headers={"x-role": "approver"}).status_code == 200
        assert client.get(root).json()["status"] == "approved"
        response = client.get(root + "/export?format=bundle")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert "report.json" in archive.namelist()
            assert any(name.startswith("raw/") for name in archive.namelist())
        assert client.post("/v1/commerce/monitor-jobs", json={"request": request.model_dump(mode="json"), "interval_seconds": 0}, headers={"x-role": "admin"}).status_code == 422
        assert client.post("/v1/commerce/runs", json=request.model_dump(mode="json"), headers={"Idempotency-Key": "no"}).status_code == 403


def test_taobao_adapter_checkpoint_sku_dedup_and_price_change(repo, tmp_path, monkeypatch):
    """Actual adapter, PostgreSQL and PydanticAI; TOP network is a synthetic fixture."""
    from ci_agent import commerce as commerce_module, taobao
    from pydantic_ai import Agent, PromptedOutput
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart
    service, _, context, _, _ = make_service(repo, tmp_path, monkeypatch)
    env_name = "CI_SOURCE_FIXTURE_TAOBAO_CREDENTIALS"
    monkeypatch.setenv(env_name, json.dumps({"tenant_id": "a", "app_key": "fixture-app",
        "app_secret": "fixture-secret-only", "session": "fixture-session-only"}))
    source = service.store.add_source("a", ProductSource(name="TOP contract fixture", kind="taobao_open_api",
        origin="https://gw.api.taobao.com", authorization_reference="Synthetic transport fixture, not platform authorization",
        taobao_credentials_env=env_name))
    targets = [ProductTarget(source_id=source["source_id"], url=taobao.GATEWAY, product_id="456", sku_id=sku, label="Fixture " + sku)
               for sku in ("123", "124")]
    request = CommerceRequest(brief="Compare observed SKU listing prices", targets=targets)
    calls, state = [], {"price": "89.90", "error": False}
    def handler(request):
        calls.append(request)
        if state["error"]:
            return httpx.Response(200, json={"error_response": {"code": 27, "sub_msg": "redacted-fixture"}})
        return httpx.Response(200, json={"item_seller_get_response": {"item": {
            "num_iid": 456, "title": "Fixture shoe", "skus": {"sku": [
                {"sku_id": 123, "price": state["price"]}, {"sku_id": 124, "price": "99.90"}]}}}})
    async def public(url):
        assert url == taobao.GATEWAY
    async def no_wait(*args, **kwargs):
        pass  # Rate reservations are independently tested; no sleeping in this fixture.
    monkeypatch.setattr(taobao, "validate_public_url", public)
    monkeypatch.setattr(service.browser.access, "reserve", no_wait)
    async def recorded_fetch(*args, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await taobao.fetch_item(*args, **kwargs, client=client)
    monkeypatch.setattr(commerce_module, "fetch_taobao_item", recorded_fetch)
    def model(messages, info):
        return ModelResponse(parts=[TextPart(CommerceAnalysis(summary="Observed listing prices are recorded; checkout discounts and reviews remain unknown.").model_dump_json())])
    service.analyst = Agent(FunctionModel(model), output_type=PromptedOutput(CommerceAnalysis))
    async def exercise():
        first = await service.run(request, context, "top-first")
        assert len(first.snapshots) == 2 and len(calls) == 1 and first.changes == []
        assert all(s.data.sale_price is None for s in first.snapshots)
        assert len({s.content_hash for s in first.snapshots}) == 1
        assert (await service.run(request, context, "top-first")).report_id == first.report_id
        assert len(calls) == 1
        state["price"] = "79.90"
        second = await service.run(request, context, "top-second")
        assert len(calls) == 2 and len(second.changes) == 1
        assert second.changes[0].field == "list_price" and second.changes[0].after == "79.90"
        raw = service.objects.get_bytes(second.snapshots[0].artifact_key)
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == second.snapshots[0].content_hash
        state["error"] = True
        with pytest.raises(taobao.TaobaoAPIError):
            await service.run(request, context, "top-denied")
        latest = service.store.get("a", service.store.target_key(targets[0]))
        assert latest["snapshot_id"] == str(second.snapshots[0].snapshot_id)
        assert len(service.store.list("a", "report")) == 2
    asyncio.run(exercise())


def test_worker_dispatches_commerce_and_commits_schedule(repo, tmp_path, monkeypatch):
    from ci_agent.monitor import run_repository_monitors
    service, request, context, _, _ = make_service(repo, tmp_path, monkeypatch)
    job = repo.create_monitor_job("a", request, 60)
    async def exercise():
        stop = asyncio.Event()
        original = service.run
        async def run(*args, **kwargs):
            report = await original(*args, **kwargs)
            stop.set()
            return report
        monkeypatch.setattr(service, "run", run)
        await run_repository_monitors(repo, None, "a", stop, commerce=service)
    asyncio.run(exercise())
    row = repo.list_monitor_jobs("a")[0]
    assert row.job_id == job and row.last_run_at is not None and row.locked_until is None


@pytest.mark.skipif(os.getenv("CI_TEST_MINIO") != "1", reason="explicit real MinIO opt-in")
def test_minio_and_pydantic_output_repair_with_recorded_feed(repo, tmp_path, monkeypatch):
    from ci_agent.object_store import ObjectStore
    from pydantic_ai import Agent, PromptedOutput
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart, RetryPromptPart, UserPromptPart
    service, request, context, calls, state = make_service(repo, tmp_path, monkeypatch)
    live = Settings()
    service.objects = ObjectStore(live.minio_endpoint, live.minio_access_key, live.minio_secret_key, live.minio_bucket, tmp_path)
    tenant = "commerce-smoke-" + uuid4().hex
    context = TenantContext(tenant_id=tenant, user_id="smoke")
    source = service.store.add_source(tenant, ProductSource(name="Recorded fixture", kind="authorized_json",
        origin="https://shop.example.com", authorization_reference="Recorded synthetic test feed"))
    request = request.model_copy(update={"targets": [request.targets[0].model_copy(update={"source_id": UUID(source["source_id"])})]})
    messages_seen = []
    def model(messages, info):
        messages_seen.append(messages)
        if len(messages_seen) == 1:
            return ModelResponse(parts=[TextPart('{"summary": 5}')])
        prompt = next(json.loads(part.content) for message in messages for part in message.parts
                      if isinstance(part, UserPromptPart) and isinstance(part.content, str))
        snapshot_id = prompt["snapshots"][0]["snapshot_id"]
        analysis = CommerceAnalysis.model_validate({"summary": "One product baseline has been established.",
            "review_insights": [{"snapshot_id": snapshot_id, "review_ids": ["r1"], "theme": "Sizing", "sentiment": "negative",
                "observation": "A sampled reviewer reports a sizing concern.", "quotes": ["Sizing is smaller than expected"]}],
            "actions": [{"category": "creative", "priority": "medium", "action": "Prepare a clearer size-guide image",
                "rationale": "A sampled review indicates sizing confusion", "snapshot_ids": [snapshot_id],
                "owner_role": "designer", "deliverable": "size-guide illustration", "success_metric": "sizing inquiry rate",
                "prerequisites": ["Confirm our product measurements"]}]})
        return ModelResponse(parts=[TextPart(analysis.model_dump_json())])
    service.analyst = Agent(FunctionModel(model), output_type=PromptedOutput(CommerceAnalysis), retries={"output": 1})
    try:
        report = asyncio.run(service.run(request, context, "structured"))
        assert len(messages_seen) == 2
        assert any(isinstance(p, RetryPromptPart) for m in messages_seen[-1] for p in m.parts)
        raw = service.objects.get_bytes(report.snapshots[0].artifact_key)
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == report.snapshots[0].content_hash
        assert service.store.get(tenant, "report:" + str(report.report_id))["analysis"]["actions"]
    finally:
        prefix = f"tenants/{tenant}/commerce/raw/"
        for key in service.objects.list_keys(prefix):
            assert key.startswith(prefix)
            service.objects.client.delete_object(Bucket=service.objects.bucket, Key=key)


@pytest.mark.skipif(os.getenv("CI_TEST_LIVE_COMMERCE") != "1", reason="explicit live model/MinIO smoke opt-in")
def test_live_model_and_minio_with_recorded_feed(repo, tmp_path, monkeypatch):
    from ci_agent.object_store import ObjectStore
    service, request, context, calls, state = make_service(repo, tmp_path, monkeypatch)
    service.settings = Settings()
    live = service.settings
    if not live.gemai_api_key:
        pytest.skip("model API key not configured; live analysis is not verified")
    service.objects = ObjectStore(live.minio_endpoint, live.minio_access_key, live.minio_secret_key, live.minio_bucket, tmp_path)
    tenant = "commerce-smoke-" + uuid4().hex
    context = TenantContext(tenant_id=tenant, user_id="smoke")
    source = service.store.add_source(tenant, ProductSource(name="Recorded smoke fixture", kind="authorized_json",
        origin="https://shop.example.com", authorization_reference="Synthetic recorded fixture; not a real storefront"))
    request = request.model_copy(update={"targets": [request.targets[0].model_copy(update={"source_id": UUID(source["source_id"])})]})
    async def exercise():
        service.analyst = None
        await service.run(request, context, "baseline")
        state["price"] = "80"
        service.analyst = None
        report = await service.run(request, context, "live-analysis")
        assert report.changes and report.analysis.actions
        assert report.analysis.review_insights
        raw = service.objects.get_bytes(report.snapshots[0].artifact_key)
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == report.snapshots[0].content_hash
    try:
        asyncio.run(exercise())
    finally:
        prefix = f"tenants/{tenant}/commerce/raw/"
        for key in service.objects.list_keys(prefix):
            assert key.startswith(prefix)
            service.objects.client.delete_object(Bucket=service.objects.bucket, Key=key)
