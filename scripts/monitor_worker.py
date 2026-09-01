"""Run persisted monitor jobs for one tenant with PostgreSQL row locking."""
from __future__ import annotations
import argparse, asyncio
from ci_agent.monitor import run_repository_monitors
from ci_agent.orchestrator import ResearchOrchestrator
from ci_agent.persistence import PostgresRepository
from ci_agent.settings import Settings

async def main(tenant: str) -> None:
    settings = Settings(); repo = PostgresRepository(settings.database_url); orchestrator = ResearchOrchestrator(settings)
    try:
        await run_repository_monitors(repo, orchestrator, tenant)
    finally:
        await orchestrator.browser.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--tenant")
    asyncio.run(main(parser.parse_args().tenant))
