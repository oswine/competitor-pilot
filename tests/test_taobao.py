"""Contract tests with synthetic TOP payloads; not live platform authorization."""
import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ci_agent import taobao
from ci_agent.access import AccessGuard
from ci_agent.browser import BrowserPolicy
from ci_agent.commerce_models import ProductSource, ProductTarget
from ci_agent.commerce_store import AccessPaused
from ci_agent.settings import Settings


ENV = "CI_SOURCE_FIXTURE_TAOBAO_CREDENTIALS"
CREDS = {"tenant_id": "fixture", "app_key": "fixture-app", "app_secret": "fixture-secret-only", "session": "fixture-session-only"}


def source():
    return ProductSource(name="Synthetic authorized source", kind="taobao_open_api", origin="https://gw.api.taobao.com",
                         authorization_reference="Contract test only", taobao_credentials_env=ENV, min_interval_seconds=10)


def target(sku="123"):
    return ProductTarget(source_id=uuid4(), url=taobao.GATEWAY, product_id="456", sku_id=sku, label="Fixture")


def payload():
    return {"item_seller_get_response": {"item": {"num_iid": 456, "title": "Fixture shoe", "desc": "<p>Blue shoe</p>",
        "price": "999.00", "approve_status": "onsale", "skus": {"sku": [
            {"sku_id": 123, "price": "89.90", "status": "normal"}, {"sku_id": 124, "price": "99.90", "status": "normal"}]}}}}


def test_signature_read_only_fields_and_timezone():
    credentials = taobao.TaobaoCredentials.model_validate(CREDS)
    params = taobao.signed_parameters(credentials, "456", datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc))
    signature = params.pop("sign")
    expected = hmac.new(b"fixture-secret-only", "".join(k + params[k] for k in sorted(params)).encode(), hashlib.sha256).hexdigest().upper()
    assert signature == expected and len(signature) == 64
    assert params["timestamp"] == "2026-10-26 08:00:00"
    assert params["method"] == "taobao.item.seller.get" and "sku" in params["fields"].split(",")
    assert "app_secret" not in params and "fixture-secret-only" not in repr(credentials)


def test_credentials_missing_malformed_and_cross_tenant(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(taobao, "dotenv_values", lambda path: {})
    with pytest.raises(RuntimeError, match="missing"):
        taobao.load_credentials(ENV, "fixture")
    monkeypatch.setenv(ENV, '{"session":"do-not-log-this"}')
    with pytest.raises(RuntimeError) as error:
        taobao.load_credentials(ENV, "fixture")
    assert "do-not-log-this" not in str(error.value)
    monkeypatch.setenv(ENV, json.dumps(CREDS))
    with pytest.raises(PermissionError, match="tenant"):
        taobao.load_credentials(ENV, "other")
    assert taobao.load_credentials(ENV, "fixture").tenant_id == "fixture"


def test_exact_sku_listing_price_and_unknown_fields():
    data = taobao.product_from_taobao(json.dumps(payload()).encode(), target())
    assert data.list_price == Decimal("89.90") and data.currency == "CNY"
    assert data.sale_price is None and data.promotion is None and data.reviews == [] and data.review_count is None
    assert data.description == "Blue shoe"
    with pytest.raises(ValueError, match="exactly one"):
        taobao.product_from_taobao(json.dumps(payload()).encode(), target("999"))
    wrong = payload(); wrong["item_seller_get_response"]["item"]["num_iid"] = 111
    with pytest.raises(ValueError, match="item does not match"):
        taobao.product_from_taobao(json.dumps(wrong).encode(), target())
    missing = payload(); del missing["item_seller_get_response"]["item"]["skus"]
    with pytest.raises(ValueError, match="SKU list"):
        taobao.product_from_taobao(json.dumps(missing).encode(), target())


def test_origin_and_credential_contracts():
    with pytest.raises(ValidationError):
        ProductSource(**{**source().model_dump(mode="json"), "origin": "https://attacker.example"})
    with pytest.raises(ValidationError):
        ProductSource(**{**source().model_dump(mode="json"), "taobao_credentials_env": None})
    with pytest.raises(PermissionError):
        taobao.validate_target(target().model_copy(update={"url": "https://gw.api.taobao.com/router/rest?session=secret"}))


def test_platform_errors_never_echo_raw_messages():
    raw = json.dumps({"error_response": {"code": 27, "sub_msg": "session=do-not-log-this"}}).encode()
    with pytest.raises(taobao.TaobaoAPIError) as error:
        taobao.response_item(raw)
    assert "27" in str(error.value) and "do-not-log-this" not in str(error.value)


def test_signed_post_redirect_refusal_and_rate_pause(monkeypatch):
    monkeypatch.setenv(ENV, json.dumps(CREDS))
    async def public(url):
        assert url == taobao.GATEWAY
    monkeypatch.setattr(taobao, "validate_public_url", public)
    async def exercise():
        calls = []
        def handler(request):
            calls.append(request)
            params = parse_qs(request.content.decode())
            assert request.method == "POST" and str(request.url) == taobao.GATEWAY
            assert params["method"] == ["taobao.item.seller.get"] and "app_secret" not in params
            return httpx.Response(200, json=payload())
        policy = BrowserPolicy(Settings(_env_file=None))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            raw = await taobao.fetch_item(source(), target(), AccessGuard(), policy, tenant_id="fixture", client=client)
            assert taobao.product_from_taobao(raw, target()).list_price == Decimal("89.90")
        assert len(calls) == 1
        for status in (302, 429):
            requests = []
            def deny(request):
                requests.append(request)
                return httpx.Response(status, headers={"location": "https://other.example", "retry-after": "60"})
            guard = AccessGuard()
            async with httpx.AsyncClient(transport=httpx.MockTransport(deny)) as client:
                with pytest.raises(PermissionError):
                    await taobao.fetch_item(source(), target(), guard, policy, tenant_id="fixture", client=client)
                if status == 429:
                    with pytest.raises(AccessPaused):
                        await taobao.fetch_item(source(), target(), guard, policy, tenant_id="fixture", client=client)
            assert len(requests) == 1
    asyncio.run(exercise())
