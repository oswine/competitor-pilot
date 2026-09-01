from uuid import UUID
from contextlib import asynccontextmanager
import asyncio
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from .models import ResearchRequest, TenantContext
from .orchestrator import ResearchOrchestrator
from .persistence import PostgresRepository
from .security import ApiKeyManager
from .settings import settings
from .telemetry import configure_telemetry
from .metrics import start_metrics_server
from .object_store import ObjectStore
from .replay import load_persisted_replay, export_bundle, export_markdown
from .playbook import PlaybookCandidate

configure_telemetry()
try:
    start_metrics_server()
except OSError:
    pass
repository = PostgresRepository(settings.database_url)
key_manager = ApiKeyManager(repository)
if settings.service_api_key:
    key_manager.bootstrap(settings.tenant_default, "admin", settings.service_api_key)
orchestrator = ResearchOrchestrator(settings)
objects = ObjectStore(settings.minio_endpoint, settings.minio_access_key, settings.minio_secret_key, settings.minio_bucket, settings.object_store_root)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await orchestrator.browser.close()

app = FastAPI(title="Competitor Intelligence Agent", version="1.0.0", lifespan=lifespan)

def authenticated_context(x_api_key: str = Header(...), x_user_id: str = Header(...)) -> TenantContext:
    try:
        return key_manager.authenticate(x_api_key, x_user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

class ApprovalBody(BaseModel):
    comment: str | None = None

class RotationBody(BaseModel):
    current_key: str

@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "gemai_key_configured": bool(settings.gemai_api_key), "tavily_key_configured": bool(settings.tavily_api_key), "postgres": True, "otel": settings.otel_enabled}

@app.post("/v1/research-runs")
async def research(request: ResearchRequest, context: TenantContext = Depends(authenticated_context), idempotency_key: str = Header(...)) -> dict[str, object]:
    if context.role not in {"researcher", "admin"}:
        raise HTTPException(status_code=403, detail="researcher role required")
    try:
        report, trace = await asyncio.wait_for(orchestrator.run(request, context, idempotency_key), timeout=settings.run_timeout_seconds)
        report_json = report.model_dump(mode="json")
        return {"report": report_json, "trace": trace.model_dump(mode="json"),
                "evidence": repository.get_evidence(context.tenant_id, [str(item) for item in report.evidence_ids])}
    except asyncio.CancelledError:
        repository.release_checkpoint(idempotency_key, context.tenant_id)
        repository.audit(context.tenant_id, context.user_id, "research.run.interrupted", {"idempotency_key": idempotency_key})
        raise
    except Exception as exc:
        repository.release_checkpoint(idempotency_key, context.tenant_id)
        repository.fail_idempotency(idempotency_key, context.tenant_id)
        repository.audit(context.tenant_id, context.user_id, "research.run.failed", {"error": str(exc)})
        raise HTTPException(status_code=502, detail=str(exc)) from exc

@app.get("/v1/research-runs/{run_id}")
async def get_run(run_id: UUID, context: TenantContext = Depends(authenticated_context)) -> dict[str, object]:
    row = repository.get_run(str(run_id), context.tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    report_json = row.report_json or {}
    return {"report": report_json, "trace": row.trace_json, "status": row.status,
            "evidence": repository.get_evidence(context.tenant_id, [str(item) for item in report_json.get("evidence_ids", [])])}

@app.get("/v1/research-runs/{run_id}/export")
async def export_run(run_id: UUID, format: str = "json", context: TenantContext = Depends(authenticated_context)):
    if context.role not in {"viewer", "researcher", "approver", "admin"}:
        raise HTTPException(status_code=403, detail="viewer role required")
    try:
        replay = load_persisted_replay(repository, objects, context.tenant_id, str(run_id))
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if format == "json":
        return replay
    export_root = settings.object_store_root / "exports" / context.tenant_id
    if format == "markdown":
        path = export_markdown(replay, export_root / f"{run_id}.md")
        return {"format": format, "path": str(path)}
    if format in {"bundle", "zip"}:
        path = export_bundle(replay, export_root / f"{run_id}.zip")
        return {"format": "bundle", "path": str(path)}
    raise HTTPException(status_code=400, detail="format must be json, markdown, or bundle")

class MonitorBody(BaseModel):
    request: ResearchRequest
    interval_seconds: int = 3600

@app.post("/v1/monitor-jobs")
async def create_monitor(body: MonitorBody, context: TenantContext = Depends(authenticated_context)) -> dict[str, str]:
    if context.role not in {"researcher", "admin"}:
        raise HTTPException(status_code=403, detail="researcher role required")
    monitor_request = body.request.model_copy(update={"mode": "monitor"})
    job_id = repository.create_monitor_job(context.tenant_id, monitor_request, body.interval_seconds)
    repository.audit(context.tenant_id, context.user_id, "monitor.created", {"job_id": job_id})
    return {"job_id": job_id, "status": "enabled"}

@app.get("/v1/monitor-jobs")
async def list_monitors(context: TenantContext = Depends(authenticated_context)) -> list[dict[str, object]]:
    return [{"job_id": row.job_id, "request": row.request_json, "interval_seconds": row.interval_seconds, "enabled": row.enabled} for row in repository.list_monitor_jobs(context.tenant_id)]

@app.get("/v1/playbooks")
async def list_playbooks(context: TenantContext = Depends(authenticated_context)) -> dict[str, object]:
    active = orchestrator.playbooks.active(context.tenant_id)
    replays = orchestrator.playbooks.replays(context.tenant_id)
    candidates = []
    candidate_dir = orchestrator.playbooks._dir(context.tenant_id) / "candidates"
    for path in candidate_dir.glob("*.json") if candidate_dir.exists() else []:
        try:
            item = PlaybookCandidate.model_validate_json(path.read_text(encoding="utf-8"))
            candidates.append({**item.model_dump(mode="json"), "evaluation": orchestrator.playbooks.evaluate(item).model_dump(mode="json")})
        except Exception:
            continue
    return {"active": active.model_dump(mode="json"), "replay_count": len(replays), "candidates": candidates}

@app.post("/v1/playbooks/{candidate_id}/promote")
async def promote_playbook(candidate_id: UUID, context: TenantContext = Depends(authenticated_context)) -> dict[str, object]:
    if context.role not in {"approver", "admin"}:
        raise HTTPException(status_code=403, detail="approver role required")
    try:
        evaluation = orchestrator.playbooks.promote(candidate_id, context.tenant_id)
        repository.audit(context.tenant_id, context.user_id, "playbook.promoted", {"candidate_id": str(candidate_id)})
        return evaluation.model_dump(mode="json")
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@app.post("/v1/reports/{report_id}/approve")
async def approve(report_id: UUID, body: ApprovalBody, context: TenantContext = Depends(authenticated_context)) -> dict[str, str]:
    if context.role not in {"approver", "admin"}:
        raise HTTPException(status_code=403, detail="approver role required")
    try:
        repository.approve(context.tenant_id, report_id, context.user_id, body.comment)
        repository.audit(context.tenant_id, context.user_id, "report.approved", {"report_id": str(report_id), "comment": body.comment})
        return {"status": "approved", "report_id": str(report_id)}
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@app.post("/v1/api-keys/rotate")
async def rotate_key(body: RotationBody, context: TenantContext = Depends(authenticated_context)) -> dict[str, str]:
    if context.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    new_key = key_manager.rotate(context, body.current_key, settings.api_key_grace_seconds)
    repository.audit(context.tenant_id, context.user_id, "api_key.rotated", {"key": new_key})
    return {"api_key": new_key, "grace_seconds": str(settings.api_key_grace_seconds)}
