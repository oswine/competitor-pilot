"""Read-only TOP adapter; requires app permission and a seller authorization.

References: open.taobao.com/doc.htm?docId=101617&docType=1
and open.taobao.com/api.htm?docId=24625&docType=2.
No login automation, unsigned mobile APIs, scraping or CAPTCHA bypass.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .access import validate_public_url
from .commerce_models import ProductData


GATEWAY = "https://gw.api.taobao.com/router/rest"
METHOD = "taobao.item.seller.get"
FIELDS = "num_iid,title,desc,price,sku,pic_url,approve_status"


class TaobaoCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    tenant_id: str = Field(min_length=1, max_length=200)
    app_key: SecretStr = Field(min_length=1)
    app_secret: SecretStr = Field(min_length=8)
    session: SecretStr = Field(min_length=8)


class TaobaoAPIError(PermissionError):
    """Do not include raw platform messages, which may echo credentials."""


def load_credentials(name: str, tenant_id: str) -> TaobaoCredentials:
    if not re.fullmatch(r"CI_SOURCE_[A-Z0-9_]+_TAOBAO_CREDENTIALS", name):
        raise ValueError("invalid Taobao credential reference")
    value = os.environ.get(name)
    if value is None:
        value = dotenv_values(".env").get(name)
    if not value:
        raise RuntimeError("Taobao credentials missing: configure the registered server-side JSON credential reference")
    try:
        credentials = TaobaoCredentials.model_validate_json(value)
    except ValueError:
        raise RuntimeError("Taobao credentials invalid: tenant_id, app_key, app_secret and seller session are required") from None
    if credentials.tenant_id != tenant_id:
        raise PermissionError("Taobao credential binding does not belong to this tenant")
    return credentials


def validate_target(target):
    if str(target.url) != GATEWAY:
        raise PermissionError("official Taobao requests must use the fixed HTTPS gateway without query parameters")
    if not re.fullmatch(r"[1-9][0-9]*", target.product_id) or not re.fullmatch(r"[1-9][0-9]*", target.sku_id):
        raise ValueError("official Taobao adapter requires exact numeric item and SKU identifiers")


def signed_parameters(credentials, product_id: str, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("signing timestamp must be timezone-aware")
    params = {
        "method": METHOD, "app_key": credentials.app_key.get_secret_value(),
        "session": credentials.session.get_secret_value(), "format": "json", "v": "2.0",
        "sign_method": "hmac-sha256", "timestamp": now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "fields": FIELDS, "num_iid": product_id,
    }
    canonical = "".join(key + params[key] for key in sorted(params))
    params["sign"] = hmac.new(credentials.app_secret.get_secret_value().encode(),
                              canonical.encode(), hashlib.sha256).hexdigest().upper()
    return params


def response_item(raw: bytes):
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("Taobao response is not valid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("Taobao response must be an object")
    if "error_response" in payload:
        error = payload["error_response"]
        code = str(error.get("code", "unknown")) if isinstance(error, dict) else "unknown"
        code = code if re.fullmatch(r"[0-9]{1,8}", code) else "unknown"
        raise TaobaoAPIError(f"Taobao API rejected request (code={code}); check app permissions, seller authorization and quota")
    envelope = payload.get("item_seller_get_response")
    item = envelope.get("item") if isinstance(envelope, dict) else None
    if not isinstance(item, dict):
        raise ValueError("Taobao response does not contain item_seller_get_response.item")
    return item


def product_from_taobao(raw: bytes, target) -> ProductData:
    validate_target(target)
    item = response_item(raw)
    if str(item.get("num_iid")) != target.product_id:
        raise ValueError("Taobao response item does not match the requested item")
    container = item.get("skus")
    skus = container.get("sku") if isinstance(container, dict) else None
    if not isinstance(skus, list):
        raise ValueError("Taobao response has no explicit SKU list; do not substitute an item-level price")
    matches = [sku for sku in skus if isinstance(sku, dict) and str(sku.get("sku_id")) == target.sku_id]
    if len(matches) != 1:
        raise ValueError("Taobao response must match exactly one requested SKU")
    sku = matches[0]
    if sku.get("status") == "delete":
        raise ValueError("requested Taobao SKU has been deleted")
    description = item.get("desc")
    if description is not None:
        soup = BeautifulSoup(description, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        description = soup.get_text(" ", strip=True)
    image = item.get("pic_url")
    if isinstance(image, str) and image.startswith("//"):
        image = "https:" + image
    # TOP's SKU price is a CNY listing price, not coupon/member checkout price.
    # Reviews and promotion details are not returned by this endpoint.
    return ProductData.model_validate({
        "product_id": target.product_id, "sku_id": target.sku_id, "title": item.get("title"),
        "description": description, "currency": "CNY" if sku.get("price") is not None else None,
        "list_price": sku.get("price"), "sale_price": None,
        "availability": item.get("approve_status"), "images": [image] if image else [],
    })


async def fetch_item(source, target, guard, policy, *, tenant_id: str, client=None):
    validate_target(target)
    credentials = load_credentials(source.taobao_credentials_env, tenant_id)
    policy.check(GATEWAY)
    await validate_public_url(GATEWAY)
    host = "gw.api.taobao.com"
    await guard.reserve(host, source.min_interval_seconds)
    owned = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    try:
        try:
            async with client.stream("POST", GATEWAY, data=signed_parameters(credentials, target.product_id),
                                     headers={"Accept": "application/json", "User-Agent": "CI-Agent/1.0"}) as response:
                if 300 <= response.status_code < 400:
                    raise PermissionError("Taobao gateway redirect refused; credentials were not forwarded")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 5_000_000:
                        raise ValueError("Taobao response exceeds byte budget")
                raw = bytes(data)
                guard.inspect(host, response.status_code, response.headers, raw.decode("utf-8", errors="replace"),
                              html="html" in response.headers.get("content-type", ""))
                if "json" not in response.headers.get("content-type", "").lower():
                    raise ValueError("Taobao gateway returned non-JSON content")
        except httpx.TransportError:
            guard.record(host, error="taobao_transport_error")
            raise RuntimeError("Taobao transport failed; no alternate scraping route was attempted") from None
        # Rejected/error responses never enter the evidence store or model context.
        response_item(raw)
        if any(secret.get_secret_value().encode() in raw for secret in (credentials.app_secret, credentials.session)):
            raise PermissionError("Taobao response contains a credential echo and was not retained")
        return raw
    finally:
        if owned:
            await client.aclose()
