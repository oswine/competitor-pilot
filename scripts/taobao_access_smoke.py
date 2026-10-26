"""A bounded public-page access probe, not a successful commerce E2E test."""
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ci_agent.browser import BrowserCollector
from ci_agent.evidence import EvidenceLedger
from ci_agent.settings import Settings


async def probe(url):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != "item.taobao.com"
            or parsed.path != "/item.htm" or parsed.username or parsed.password
            or not parse_qs(parsed.query).get("id", [""])[0].isdigit()):
        raise ValueError("expected a public HTTPS item.taobao.com/item.htm?id=<item-id> URL")
    browser = BrowserCollector(Settings(), EvidenceLedger(Path("data/taobao-smoke")))
    result = {"scope": "public_page_access_only", "commerce_e2e_passed": False}
    try:
        async with asyncio.timeout(60):
            page = await browser.fetch(url, bypass_cache=True, min_interval=10,
                                       same_origin="https://item.taobao.com")
        result.update(status="page_received", http_status=page.status_code,
                      next_step="Verify source permission and exact Product/SKU before commerce analysis")
        return result, 0
    except PermissionError as exc:
        result.update(status="blocked", reason=str(exc))
        return result, 2
    except Exception as exc:
        # Do not print request objects, headers or provider credentials.
        result.update(status="failed", error_type=type(exc).__name__)
        return result, 1
    finally:
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    result, code = asyncio.run(probe(args.url))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(code)
