"""Authenticated operations API; credentials stay server-side."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .access import validate_public_url
from .commerce_models import CommerceRequest, ProductSource


class CommerceMonitorBody(BaseModel):
    request: CommerceRequest
    interval_seconds: int = Field(default=3600, ge=60, le=2592000)


class EnabledBody(BaseModel):
    enabled: bool


def commerce_router(service, authenticate):
    router = APIRouter(prefix="/v1/commerce", tags=["commerce"])

    def allow(context, roles):
        if context.role not in roles:
            raise HTTPException(403, "insufficient role")

    def find_report(context, report_id):
        value = service.store.get(context.tenant_id, "report:" + str(report_id))
        if value is None:
            raise HTTPException(404, "report not found")
        return value

    @router.post("/sources")
    async def register_source(body: ProductSource, context=Depends(authenticate)):
        allow(context, {"admin"})
        try:
            service.browser.policy.check(str(body.origin))
            await validate_public_url(str(body.origin))
        except (PermissionError, ValueError, OSError) as exc:
            raise HTTPException(422, "source origin is not allowed") from exc
        value = service.store.add_source(context.tenant_id, body)
        service.repository.audit(context.tenant_id, context.user_id, "commerce.source.created", {"source_id": value["source_id"]})
        return value

    @router.get("/sources")
    def sources(context=Depends(authenticate)):
        return service.store.list(context.tenant_id, "source")

    @router.patch("/sources/{source_id}")
    def source_status(source_id: UUID, body: EnabledBody, context=Depends(authenticate)):
        allow(context, {"admin"})
        key = "source:" + str(source_id)
        value = service.store.get(context.tenant_id, key)
        if value is None:
            raise HTTPException(404, "source not found")
        value["enabled"] = body.enabled
        service.store.put(context.tenant_id, key, "source", value)
        service.repository.audit(context.tenant_id, context.user_id, "commerce.source.enabled", {"source_id": str(source_id), "enabled": body.enabled})
        return value

    @router.post("/runs")
    async def run(body: CommerceRequest, context=Depends(authenticate), idempotency_key: str = Header(..., min_length=1, max_length=200)):
        allow(context, {"researcher", "admin"})
        try:
            return await service.run(body, context, idempotency_key)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            service.repository.audit(context.tenant_id, context.user_id, "commerce.failed", {"error_type": type(exc).__name__})
            raise HTTPException(502, "collection or analysis failed; retry with the same idempotency key to resume") from exc

    @router.post("/monitor-jobs")
    def monitor(body: CommerceMonitorBody, context=Depends(authenticate)):
        allow(context, {"researcher", "admin"})
        try:
            for target in body.request.targets:
                source = service.source_for(context.tenant_id, target)
                if body.interval_seconds < source.min_interval_seconds:
                    raise ValueError("monitor interval is shorter than the source policy")
        except (PermissionError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        job_id = service.repository.create_monitor_job(context.tenant_id, body.request, body.interval_seconds)
        service.repository.audit(context.tenant_id, context.user_id, "commerce.monitor.created", {"job_id": job_id})
        return {"job_id": job_id, "status": "enabled"}

    @router.get("/reports")
    def reports(context=Depends(authenticate)):
        return service.store.list(context.tenant_id, "report", limit=50)

    @router.get("/reports/{report_id}")
    def report(report_id: UUID, context=Depends(authenticate)):
        return find_report(context, report_id)

    @router.post("/reports/{report_id}/approve")
    def approve(report_id: UUID, context=Depends(authenticate)):
        allow(context, {"approver", "admin"})
        find_report(context, report_id)
        service.store.approve(context.tenant_id, report_id, context.user_id)
        return {"status": "approved"}

    @router.get("/reports/{report_id}/export")
    def export(report_id: UUID, format: str = "json", context=Depends(authenticate)):
        value = find_report(context, report_id)
        if format == "json":
            return Response(json.dumps(value, ensure_ascii=False, indent=2), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{report_id}.json"'})
        if format == "markdown":
            lines = ["# Product operations report", "", value["analysis"]["summary"], "", "## Changes", ""]
            for change in value["changes"]:
                lines.append(f'- {change["field"]}: {change["before"]} -> {change["after"]} ({change["current_snapshot_id"]})')
            for action in value["analysis"]["actions"]:
                lines += ["", f'## {action["category"]}: {action["priority"]}', action["action"], action["rationale"],
                          f'Owner: {action["owner_role"]}', f'Deliverable: {action["deliverable"]}',
                          f'Metric: {action["success_metric"]}', "Prerequisites: " + "; ".join(action["prerequisites"]),
                          "Snapshots: " + ", ".join(action["snapshot_ids"])]
            lines += ["", "## Unknowns", *value["analysis"]["unknowns"]]
            return Response("\n".join(lines), media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="{report_id}.md"'})
        if format != "bundle":
            raise HTTPException(422, "format must be json, markdown or bundle")
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.json", json.dumps(value, ensure_ascii=False, indent=2))
            snapshots = {s["snapshot_id"]: s for s in value["snapshots"]}
            for change in value["changes"]:
                previous = service.store.get(context.tenant_id, "snapshot:" + change["previous_snapshot_id"])
                if previous is None:
                    raise HTTPException(409, "previous evidence is missing")
                snapshots[previous["snapshot_id"]] = previous
            archive.writestr("snapshots.json", json.dumps(list(snapshots.values()), ensure_ascii=False, indent=2))
            written = set()
            total_bytes = 0
            for snapshot in snapshots.values():
                key = snapshot["artifact_key"]
                if not key.startswith(f"tenants/{context.tenant_id}/commerce/raw/"):
                    raise HTTPException(403, "artifact scope mismatch")
                if key in written:
                    continue
                raw = service.objects.get_bytes(key)
                total_bytes += len(raw)
                if total_bytes > 100_000_000:
                    raise HTTPException(413, "evidence bundle exceeds size limit")
                digest = hashlib.sha256(raw).hexdigest()
                if "sha256:" + digest != snapshot["content_hash"]:
                    raise HTTPException(409, "evidence integrity check failed")
                archive.writestr("raw/" + digest, raw)
                written.add(key)
        return Response(memory.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{report_id}.zip"'})

    return router
