from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import time
import os
import tempfile
from io import BytesIO
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl
from playwright.async_api import async_playwright
from pypdf import PdfReader
from docx import Document

from .evidence import EvidenceLedger
from .persistence import PostgresRepository
from .models import Evidence, SourceType
from .settings import Settings
from robotexclusionrulesparser import RobotExclusionRulesParser
from cachetools import TTLCache


INJECTION_PATTERNS = (
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"system\s+message",
    r"reveal\s+(the\s+)?(api|secret|system)\s+key",
    r"disable\s+safety",
    r"you\s+are\s+chatgpt",
)


def detect_prompt_injection(text: str) -> list[str]:
    """Flag likely instruction-bearing web content; never execute or promote it."""
    lowered = text.lower()
    return [pattern for pattern in INJECTION_PATTERNS if re.search(pattern, lowered)]


def infer_source_profile(url: str, requested: SourceType) -> tuple[SourceType, float]:
    """Classify provenance conservatively so source quality is evidence-based."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host.endswith("ci-bench.local"):
        return (requested if requested != SourceType.WEBSITE else SourceType.SYNTHETIC, 0.65)
    if host == "github.com" or host.endswith(".github.com"):
        return SourceType.GITHUB, 0.86
    if "/pricing" in path or "pricing" in path:
        return SourceType.PRICING, 0.90
    if "/docs" in path or "/documentation" in path:
        return SourceType.DOCS, 0.86
    if "changelog" in path or "release" in path:
        return SourceType.CHANGELOG, 0.84
    if path.endswith(".pdf"):
        return SourceType.PDF, 0.78
    if any(part in host for part in ("news", "press", "journal")):
        return SourceType.NEWS, 0.68
    return requested, 0.78


class BrowserPolicy:
    def __init__(self, settings: Settings):
        self.allowlist = settings.domain_allowlist

    def check(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("only http(s) URLs are allowed")
        host = (parsed.hostname or "").lower()
        if not host or host in {"localhost", "metadata.google.internal"}:
            raise ValueError("private/local host is denied")
        try:
            if ipaddress.ip_address(host).is_private:
                raise ValueError("private IP is denied")
        except ValueError as exc:
            if "private IP" in str(exc):
                raise
        if self.allowlist and not any(host == d or host.endswith("." + d) for d in self.allowlist):
            raise ValueError(f"domain is not allowlisted: {host}")


class PageSnapshot:
    def __init__(self, url: str, status_code: int, raw: str | bytes, clean: str, screenshot: bytes | None, locator: str, content_type: str, recording: bytes | None = None, injection_flags: list[str] | None = None):
        self.url, self.status_code, self.raw, self.clean = url, status_code, raw, clean
        self.screenshot, self.locator, self.content_type = screenshot, locator, content_type
        self.recording = recording
        self.injection_flags = injection_flags or []


def clean_html(raw: str) -> tuple[str, str]:
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    return title, text


class BrowserCollector:
    def __init__(self, settings: Settings, ledger: EvidenceLedger, repository: PostgresRepository | None = None):
        self.settings, self.ledger = settings, ledger
        self.repository = repository
        self.policy = BrowserPolicy(settings)
        self._domain_last: dict[str, float] = {}
        self._robots_cache = TTLCache(maxsize=256, ttl=3600)
        self._response_cache = TTLCache(maxsize=512, ttl=300)
        self._pool = asyncio.Semaphore(max(1, settings.browser_pool_size))
        self._browser = None
        self._playwright = None

    async def _allowed_by_robots(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        robots_url = origin + "/robots.txt"
        parser = self._robots_cache.get(origin)
        if parser is None:
            parser = RobotExclusionRulesParser()
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(robots_url, headers={"User-Agent": "CI-Agent/1.0"})
                parser.parse(response.text if response.status_code < 400 else "")
            except Exception:
                parser.parse("")
            self._robots_cache[origin] = parser
        return parser.is_allowed("CI-Agent", url)

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def fetch(self, url: str, use_browser: bool = False, bypass_cache: bool = False) -> PageSnapshot:
        self.policy.check(url)
        if not await self._allowed_by_robots(url):
            raise PermissionError(f"robots.txt disallows {url}")
        host = urlparse(url).hostname or ""
        now = time.monotonic()
        delay = self.settings.domain_rate_limit_seconds - (now - self._domain_last.get(host, 0))
        if delay > 0:
            await asyncio.sleep(delay)
        self._domain_last[host] = time.monotonic()
        cached = None if bypass_cache else self._response_cache.get(url)
        if cached is not None and not use_browser:
            return cached
        if self.repository is not None and not use_browser and not bypass_cache:
            persisted = self.repository.get_page_cache(url)
            if persisted is not None:
                snapshot = PageSnapshot(url, persisted.status_code, persisted.raw_bytes, persisted.clean_text, None, "persistent-cache", persisted.content_type)
                self._response_cache[url] = snapshot
                return snapshot
        recording = None
        async with self._pool:
            if use_browser:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(headless=True)
                har_path = tempfile.mktemp(prefix="ci-recording-", suffix=".har")
                context = await self._browser.new_context(record_har_path=har_path, record_har_content="embed")
                page = await context.new_page()
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(500)
                    raw = await page.content()
                    screenshot = await page.screenshot(full_page=True)
                    status = response.status if response else 0
                finally:
                    await context.close()
                if os.path.exists(har_path):
                    recording = open(har_path, "rb").read()
                    os.unlink(har_path)
            else:
                async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                    response = await client.get(url, headers={"User-Agent": "CI-Agent/0.1 (+research)"})
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "text/html").split(";", 1)[0].lower()
                    raw, status, screenshot = response.content, response.status_code, None
        if use_browser:
            content_type = "text/html"
        if content_type == "application/pdf" or url.lower().endswith(".pdf"):
            clean = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(raw if isinstance(raw, bytes) else raw.encode())).pages)
            title, locator = "PDF document", "pdf:all-pages"
        elif content_type in {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"} or url.lower().endswith(".docx"):
            document = Document(BytesIO(raw if isinstance(raw, bytes) else raw.encode()))
            clean = "\n".join(p.text for p in document.paragraphs if p.text.strip())
            title, locator = "Word document", "docx:paragraphs"
        else:
            html = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            title, clean = clean_html(html)
            locator = f"title={title or 'document'};body"
        injection_flags = detect_prompt_injection(clean)
        snapshot = PageSnapshot(url, status, raw, clean, screenshot, locator, content_type, recording, injection_flags)
        if not use_browser:
            self._response_cache[url] = snapshot
            if self.repository is not None:
                self.repository.put_page_cache(url, status, content_type, raw if isinstance(raw, bytes) else raw.encode("utf-8"), clean, self.settings.browser_cache_ttl_seconds)
        return snapshot

    def persist(self, snapshot: PageSnapshot, source_type: SourceType = SourceType.WEBSITE) -> Evidence:
        raw = snapshot.raw.encode("utf-8") if isinstance(snapshot.raw, str) else snapshot.raw
        content_hash = self.ledger.content_hash(raw)
        inferred_type, credibility = infer_source_profile(snapshot.url, source_type)
        evidence = Evidence(
            source_url=HttpUrl(snapshot.url),
            publisher=urlparse(snapshot.url).hostname,
            content_hash=content_hash,
            excerpt=snapshot.clean[:4000],
            locator=snapshot.locator,
            source_type=inferred_type,
            credibility=credibility,
        )
        self.ledger.append(evidence, raw)
        return evidence
