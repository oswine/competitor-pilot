import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ci_agent.access import AccessGuard, retry_after
from ci_agent.commerce import diff_product, product_from_html, product_from_json, validate_analysis
from ci_agent.commerce_models import (CommerceAnalysis, CommerceRequest, OperationalAction, ProductData,
                                     ProductReview, ProductSnapshot, ProductSource, ProductTarget, ReviewInsight)
from ci_agent.commerce_store import AccessPaused


def target():
    return ProductTarget(source_id=uuid4(), url="https://shop.example.com/products/one", product_id="one",
                         sku_id="blue", label="Competitor")


def data(**updates):
    return ProductData.model_validate({"product_id": "one", "sku_id": "blue", "title": "Test product",
                                      "currency": "USD", "sale_price": "100.00", **updates})


def snapshot(t, **updates):
    return ProductSnapshot(source_id=t.source_id, source_url=t.url, role=t.role, label=t.label,
                           content_hash="sha256:test", artifact_key="tests/raw", data=data(**updates))


def test_diff_is_sku_specific_decimal_and_unknown_safe():
    t = target()
    old = snapshot(t, promotion="Summer sale", reviews=[{"review_id": "r1", "text": "Comfortable"}])
    new = snapshot(t, sale_price="80.00")
    changes = {c.field: c for c in diff_product(old, new)}
    assert changes["sale_price"].relative_change == Decimal("-0.2")
    assert changes["promotion"].kind == "unavailable"
    assert "review:r1" not in changes
    assert diff_product(None, new) == []
    assert diff_product(new, snapshot(t, sale_price="80")) == []
    with pytest.raises(ValueError, match="identities"):
        diff_product(old, snapshot(target()))
    foreign = snapshot(t, currency="EUR", sale_price="80")
    assert next(c for c in diff_product(old, foreign) if c.field == "sale_price").relative_change is None


def test_feed_rejects_missing_or_ambiguous_variant():
    t = target()
    raw = data().model_dump(mode="json")
    assert product_from_json(json.dumps(raw).encode(), t).sale_price == Decimal("100")
    with pytest.raises(ValueError, match="exactly one"):
        product_from_json(json.dumps({"products": [raw, raw]}).encode(), t)
    with pytest.raises(ValueError, match="exactly one"):
        product_from_json(b'{"products": []}', t)
    with pytest.raises(ValidationError, match="currency"):
        data(currency=None)
    with pytest.raises(ValidationError):
        data(sale_price="NaN")


def test_jsonld_keeps_explicit_variant_and_review_evidence():
    t = target()
    product = {"@type": "Product", "productID": "one", "sku": "blue", "name": "Blue shoe",
               "offers": {"price": "80.00", "priceCurrency": "USD"},
               "review": [{"reviewBody": "Too narrow", "reviewRating": {"ratingValue": 2}}]}
    raw = ('<script type="application/ld+json">' + json.dumps(product) + '</script>').encode()
    result = product_from_html(raw, t)
    assert result.sale_price == Decimal("80")
    assert result.reviews[0].text == "Too narrow"
    assert result.promotion is None
    with pytest.raises(ValueError, match="multiple offers"):
        product["offers"] = [product["offers"], product["offers"]]
        product_from_html(('<script type="application/ld+json">' + json.dumps(product) + '</script>').encode(), t)


def test_analysis_rejects_fabricated_evidence_and_review_quotes():
    t = target()
    s = snapshot(t, reviews=[{"review_id": "r1", "text": "Too narrow for my feet"}])
    insight = ReviewInsight(snapshot_id=s.snapshot_id, review_ids=["r1"], theme="Fit", sentiment="negative",
                            observation="One sampled review reports a fit issue", quotes=["Too narrow"])
    result = CommerceAnalysis(summary="Review indicates a possible sizing issue", review_insights=[insight])
    validate_analysis(result, [s], [])
    result.review_insights[0].quotes = ["The shoe broke"]
    with pytest.raises(ValueError, match="verbatim"):
        validate_analysis(result, [s], [])
    result.review_insights = []
    result.actions = [OperationalAction(category="creative", priority="medium", action="Prepare a size guide image",
        rationale="The cited review mentions fit", snapshot_ids=[uuid4()], owner_role="designer", deliverable="size guide",
        success_metric="size-related inquiries", prerequisites=["validate size measurements"])]
    with pytest.raises(ValueError, match="unknown snapshot"):
        validate_analysis(result, [s], [])


def test_analysis_rejects_invented_numeric_claims():
    s = snapshot(target())
    result = CommerceAnalysis(summary="The proposed change will increase revenue by 37%.")
    with pytest.raises(ValueError, match="numeric assertions"):
        validate_analysis(result, [s], [])


def test_host_derived_sample_count_and_normalized_rating_scale():
    s = snapshot(target(), reviews=[{"review_id": "r1", "text": "Too narrow", "rating": 2}])
    result = CommerceAnalysis(summary="The sample contains 1 review rated 2 out of 5.")
    validate_analysis(result, [s], [])
    result.summary = "The sample contains 6 reviews rated 2 out of 5."
    with pytest.raises(ValueError, match="unsupported values: 6"):
        validate_analysis(result, [s], [])


def test_source_and_target_contracts():
    with pytest.raises(ValidationError):
        ProductSource(name="bad", kind="authorized_json", origin="https://host.example/path", authorization_reference="contract 1")
    with pytest.raises(ValidationError):
        ProductSource(name="bad", kind="authorized_json", origin="http://host.example", authorization_reference="contract 1", credential_env="CI_SOURCE_TEST_TOKEN")
    t = target()
    with pytest.raises(ValidationError, match="duplicate"):
        CommerceRequest(brief="Analyze products", targets=[t, t])


def test_rate_limit_and_captcha_stop_further_requests(monkeypatch):
    async def exercise():
        from ci_agent import access
        async def valid(url):
            return None
        monkeypatch.setattr(access, "validate_public_url", valid)
        for status, body in [(429, "Too many requests"), (403, "Forbidden"), (200, "Verify you are human")]:
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, text=body, headers={"content-type": "text/html", "retry-after": "600"})
            guard = AccessGuard()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with pytest.raises(AccessPaused):
                    await guard.get("https://shop.example.com/a", interval=0, client=client)
                with pytest.raises(AccessPaused):
                    await guard.get("https://shop.example.com/b", interval=0, client=client)
            assert len(calls) == 1
    asyncio.run(exercise())


def test_conditional_304_is_not_a_redirect_and_credentials_cannot_escape(monkeypatch):
    async def exercise():
        from ci_agent import access
        async def valid(url):
            return None
        monkeypatch.setattr(access, "validate_public_url", valid)
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(304)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await AccessGuard().get("https://shop.example.com/a", interval=0, client=client)
            assert result.status_code == 304 and len(calls) == 1
        def redirect(request):
            return httpx.Response(302, headers={"location": "https://other.example/a"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as client:
            with pytest.raises(PermissionError, match="escaped"):
                await AccessGuard().get("https://shop.example.com/a", interval=0, client=client,
                                        headers={"Authorization": "Bearer test"}, same_origin="https://shop.example.com")
    asyncio.run(exercise())


def test_private_destination_rejected():
    from ci_agent.access import validate_public_url
    with pytest.raises(PermissionError, match="non-public"):
        asyncio.run(validate_public_url("http://127.0.0.1/private"))


def test_robots_fail_closed_and_404_allows(monkeypatch, tmp_path):
    from ci_agent.browser import BrowserCollector
    from ci_agent.evidence import EvidenceLedger
    from ci_agent.settings import Settings
    browser = BrowserCollector(Settings(_env_file=None, robots_fail_closed=True), EvidenceLedger(tmp_path))
    async def fail(*args, **kwargs):
        raise httpx.ConnectError("unavailable")
    monkeypatch.setattr(browser.access, "get", fail)
    with pytest.raises(PermissionError, match="robots policy unavailable"):
        asyncio.run(browser._allowed_by_robots("https://shop.example.com/a"))
    async def missing(*args, **kwargs):
        return httpx.Response(404)
    monkeypatch.setattr(browser.access, "get", missing)
    assert asyncio.run(browser._allowed_by_robots("https://shop.example.com/a"))


@pytest.mark.parametrize("render_js", [False, True])
def test_robots_denial_never_fetches_product_or_starts_browser(monkeypatch, tmp_path, render_js):
    from ci_agent.browser import BrowserCollector
    from ci_agent.evidence import EvidenceLedger
    from ci_agent.settings import Settings
    browser = BrowserCollector(Settings(_env_file=None), EvidenceLedger(tmp_path))
    requested = []

    async def robots_only(url, **kwargs):
        requested.append(url)
        assert url == "https://item.taobao.com/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    monkeypatch.setattr(browser.access, "get", robots_only)
    with pytest.raises(PermissionError, match="robots.txt disallows"):
        asyncio.run(browser.fetch("https://item.taobao.com/item.htm?id=1", use_browser=render_js))
    assert requested == ["https://item.taobao.com/robots.txt"]
    assert browser._browser is None and browser._playwright is None
