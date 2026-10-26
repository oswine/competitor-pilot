from __future__ import annotations

import asyncio
from contextlib import suppress
from uuid import uuid4
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


async def run_repository_monitors(repository, orchestrator, tenant_id: str | None = None, stop: asyncio.Event | None = None, commerce=None) -> None:
    """Run all tenant jobs; PostgreSQL row locks provide multi-worker recovery."""
    stop = stop or asyncio.Event()
    worker_id = str(uuid4())
    while not stop.is_set():
        jobs = repository.claim_due_monitor_jobs(worker_id, limit=1, tenant_id=tenant_id)
        for job in jobs:
            run_key = f"monitor:{job.job_id}:{job.last_run_at.isoformat() if job.last_run_at else 'initial'}"
            async def execute():
                from .models import ResearchRequest, TenantContext
                context = TenantContext(tenant_id=job.tenant_id, user_id="monitor", role="researcher")
                if job.request_json.get("kind") == "commerce":
                    from .commerce_models import CommerceRequest
                    if commerce is None:
                        raise RuntimeError("commerce service is required for this job")
                    await commerce.run(CommerceRequest.model_validate(job.request_json), context, run_key)
                else:
                    request = ResearchRequest.model_validate(job.request_json).model_copy(update={"mode": "monitor"})
                    await orchestrator.run(request, context, run_key)

            async def heartbeat():
                while True:
                    await asyncio.sleep(20)
                    if not repository.renew_monitor_job(job.job_id, worker_id):
                        raise RuntimeError("monitor lease lost")

            task, renewal = asyncio.create_task(execute()), asyncio.create_task(heartbeat())
            try:
                done, _ = await asyncio.wait({task, renewal}, return_when=asyncio.FIRST_COMPLETED)
                for completed in done:
                    completed.result()
                repository.complete_monitor_job(job.job_id, job.interval_seconds, worker_id)
            except Exception as exc:
                repository.release_checkpoint(run_key, job.tenant_id)
                repository.fail_idempotency(run_key, job.tenant_id)
                repository.fail_monitor_job(job.job_id, f"{type(exc).__name__}: {exc}", worker_id)
            finally:
                for pending in (task, renewal):
                    pending.cancel()
                await asyncio.gather(task, renewal, return_exceptions=True)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=5 if jobs else 30)
