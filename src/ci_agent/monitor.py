from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def run_monitor(job: Callable[[], Awaitable[object]], interval_seconds: int, stop: asyncio.Event | None = None) -> None:
    """Small dependency-free scheduler; callers provide an idempotent job."""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        await job()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass


async def run_repository_monitors(repository, orchestrator, tenant_id: str | None = None, stop: asyncio.Event | None = None) -> None:
    """Run all tenant jobs; PostgreSQL row locks provide multi-worker recovery."""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        jobs = repository.claim_due_monitor_jobs("worker", limit=10)
        for job in jobs:
            run_key = f"monitor:{job.job_id}:{job.last_run_at.isoformat() if job.last_run_at else 'initial'}"
            try:
                from .models import ResearchRequest, TenantContext
                request = ResearchRequest.model_validate(job.request_json).model_copy(update={"mode": "monitor"})
                await orchestrator.run(request, TenantContext(tenant_id=job.tenant_id, user_id="monitor", role="researcher"), run_key)
                repository.complete_monitor_job(job.job_id, job.interval_seconds)
            except Exception as exc:
                repository.release_checkpoint(run_key, job.tenant_id)
                repository.fail_idempotency(run_key, job.tenant_id)
                repository.fail_monitor_job(job.job_id, f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(5 if jobs else 30)
