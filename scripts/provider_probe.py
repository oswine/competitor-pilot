"""Check configured credentials without exposing tokens or raw error bodies."""
import argparse
import asyncio
import json

import httpx
from pydantic_ai import Agent

from ci_agent.agents import build_model, close_model_clients
from ci_agent.settings import Settings


async def main(call: bool = False) -> int:
    settings = Settings()
    if not settings.gemai_api_key:
        print(json.dumps({"status": "missing_model_key"}))
        return 1
    try:
        async with httpx.AsyncClient(timeout=30, proxy=settings.model_proxy_url or None, trust_env=False) as client:
            response = await client.get(settings.gemai_base_url.rstrip("/") + "/models",
                headers={"Authorization": "Bearer " + settings.gemai_api_key})
        result = {"models_http_status": response.status_code}
        if response.status_code != 200:
            result["status"] = "authentication_rejected" if response.status_code == 401 else "provider_error"
            print(json.dumps(result))
            return 1
        ids = {item["id"] for item in response.json().get("data", [])}
        result.update(model_count=len(ids), configured_models={model: model in ids for model in
            (settings.gemai_fast_model, settings.gemai_research_model, settings.gemai_judge_model)})
        if call:
            async with asyncio.timeout(90):
                agent = Agent(build_model(settings, settings.gemai_research_model), retries=0)
                response = await agent.run("Reply with exactly OK.", model_settings={"max_tokens": 256})
                result["inference_passed"] = response.output.strip() == "OK"
        print(json.dumps(result, ensure_ascii=False))
        return 0 if not call or result.get("inference_passed") else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    finally:
        await close_model_clients()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call", action="store_true", help="Also make one short, billable inference request")
    raise SystemExit(asyncio.run(main(parser.parse_args().call)))
