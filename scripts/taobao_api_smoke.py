"""Trusted local-operator preflight or full run through a registered Taobao source."""
import argparse
import asyncio
import json
from uuid import UUID, uuid4

from ci_agent.agents import close_model_clients
from ci_agent.browser import BrowserCollector
from ci_agent.commerce import CommerceService
from ci_agent.commerce_models import CommerceRequest, ProductTarget
from ci_agent.evidence import EvidenceLedger
from ci_agent.models import TenantContext
from ci_agent.object_store import ObjectStore
from ci_agent.persistence import PostgresRepository
from ci_agent.settings import Settings
from ci_agent.taobao import GATEWAY, load_credentials


async def main(args):
    # No network, DB mutation or model request before the credential gate.
    try:
        load_credentials(args.credentials_env, args.tenant)
    except (RuntimeError, PermissionError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc), "commerce_e2e_passed": False}))
        return 2
    if not args.run:
        print(json.dumps({"status": "credentials_present", "platform_authorization_verified": False,
                          "commerce_e2e_passed": False}))
        return 0
    settings = Settings()
    repo = PostgresRepository(settings.database_url)
    browser = BrowserCollector(settings, EvidenceLedger(settings.object_store_root / "taobao"), repo)
    try:
        objects = ObjectStore(settings.minio_endpoint, settings.minio_access_key, settings.minio_secret_key,
                              settings.minio_bucket, settings.object_store_root)
        service = CommerceService(settings, repo, objects, browser)
        target = ProductTarget(source_id=UUID(args.source_id), url=GATEWAY, product_id=args.product_id,
                               sku_id=args.sku_id, role="own", label=args.label)
        source = service.source_for(args.tenant, target)
        if source.kind != "taobao_open_api" or source.taobao_credentials_env != args.credentials_env:
            raise PermissionError("registered source must match the verified Taobao credential binding")
        report = await service.run(CommerceRequest(brief="分析授权商品的标价、标题与描述变化，提出有证据的运营建议；未提供的促销和评价保持未知。", targets=[target]),
            TenantContext(tenant_id=args.tenant, user_id="local-operator", role="researcher"), args.idempotency_key)
        print(json.dumps({"status": "completed", "report_id": str(report.report_id),
                          "snapshots": len(report.snapshots), "changes": len(report.changes),
                          "commerce_e2e_passed": True}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "commerce_e2e_passed": False}))
        return 1
    finally:
        await browser.close()
        await close_model_clients()
        repo.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--credentials-env", required=True)
    parser.add_argument("--run", action="store_true", help="Call the authorized API and model; persist a real draft report")
    parser.add_argument("--source-id")
    parser.add_argument("--product-id")
    parser.add_argument("--sku-id")
    parser.add_argument("--label", default="授权淘宝商品")
    parser.add_argument("--idempotency-key", default="taobao-smoke-" + uuid4().hex)
    args = parser.parse_args()
    if args.run and not all((args.source_id, args.product_id, args.sku_id)):
        parser.error("--run requires --source-id, --product-id and --sku-id")
    raise SystemExit(asyncio.run(main(args)))
