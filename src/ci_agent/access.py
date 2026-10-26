"""Shared rate reservations, bounded GETs and explicit refusal handling."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx

from .commerce_store import AccessPaused


async def validate_public_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise PermissionError("invalid public URL")
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), 0, socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise PermissionError("non-public destination denied")


def retry_after(value: str | None) -> float:
    try:
        if value and value.strip().isdigit():
            return max(1, float(value))
        return max(1, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 300


def challenge_page(text: str) -> bool:
    text = text.casefold()
    return any(term in text for term in ("verify you are human", "verify that you are human",
        "unusual traffic", "cf-chl-", "access denied", "请完成安全验证", "访问过于频繁", "滑动验证"))


class AccessGuard:
    def __init__(self, store=None):
        self.store = store
        self.local = {}
        self.lock = asyncio.Lock()

    async def reserve(self, host: str, interval: float):
        while True:
            if self.store:
                delay = self.store.reserve_access(host, interval)
            else:
                async with self.lock:
                    state = self.local.setdefault(host, {"next": 0, "pause": 0, "failures": 0})
                    now = asyncio.get_running_loop().time()
                    if state["pause"] > now:
                        raise AccessPaused("domain is paused")
                    delay = max(0, state["next"] - now)
                    if not delay:
                        state["next"] = now + interval
            if not delay:
                return
            await asyncio.sleep(delay)

    def record(self, host, error=None, cooldown=0):
        if self.store:
            self.store.record_access(host, error=error, cooldown=cooldown)
        else:
            state = self.local.setdefault(host, {"next": 0, "pause": 0, "failures": 0})
            now = asyncio.get_running_loop().time()
            if error:
                state["failures"] += 1
                if cooldown or state["failures"] >= 3:
                    state["pause"] = max(state["pause"], now + max(300, cooldown))
            elif state["pause"] <= now:
                state["failures"] = 0

    def inspect(self, host, status, headers, body, *, html=True):
        if status in {401, 403, 429} or (html and challenge_page(body)):
            self.record(host, f"http_{status}_or_challenge", retry_after(headers.get("retry-after")))
            raise AccessPaused(f"access refused or challenged ({status}); domain paused")
        if status >= 500 or status == 0:
            self.record(host, f"http_{status}")
            raise RuntimeError(f"upstream failure ({status})")
        if status >= 400:
            raise RuntimeError(f"upstream response ({status})")
        self.record(host)

    async def get(self, url, *, interval=1, headers=None, same_origin=None, policy=None, max_bytes=5_000_000, client=None, allow_missing=False, redirect_check=None):
        owned = client is None
        client = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
        try:
            for _ in range(6):
                parsed = urlparse(url)
                if same_origin and (parsed.scheme, parsed.netloc) != (urlparse(same_origin).scheme, urlparse(same_origin).netloc):
                    raise PermissionError("source redirect escaped registered origin")
                if policy:
                    policy.check(url)
                await validate_public_url(url)
                await self.reserve(parsed.hostname, interval)
                try:
                    async with client.stream("GET", url, headers=headers) as response:
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > max_bytes:
                                raise ValueError("response exceeds byte budget")
                        result = httpx.Response(response.status_code, headers=response.headers, content=bytes(data), request=response.request)
                except httpx.TransportError:
                    self.record(parsed.hostname, "transport_error")
                    raise
                if not (allow_missing and result.status_code in {404, 410}):
                    self.inspect(parsed.hostname, result.status_code, result.headers, result.text,
                                 html="html" in result.headers.get("content-type", "text/html"))
                if result.status_code in {301, 302, 303, 307, 308}:
                    destination = urljoin(url, result.headers.get("location", ""))
                    if same_origin and (urlparse(destination).scheme, urlparse(destination).netloc) != (urlparse(same_origin).scheme, urlparse(same_origin).netloc):
                        raise PermissionError("source redirect escaped registered origin")
                    if redirect_check and not await redirect_check(destination):
                        raise PermissionError("redirect disallowed by robots")
                    if not same_origin and urlparse(destination).netloc != parsed.netloc:
                        headers = {k: v for k, v in (headers or {}).items() if k.lower() not in {"authorization", "cookie"}}
                    url = destination
                    continue
                return result
            raise ValueError("too many redirects")
        finally:
            if owned:
                await client.aclose()
