import asyncio
import os

import httpx


async def main() -> None:
    gemai_key = os.environ["GEMAI_API_KEY"]
    tavily_key = os.environ["TAVILY_API_KEY"]
    base = os.getenv("GEMAI_BASE_URL", "https://api.gemai.cc/v1")
    async with httpx.AsyncClient(timeout=45) as client:
        models = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {gemai_key}"})
        print("gemai_models_status", models.status_code)
        if models.status_code != 200:
            print(models.text[:500])
            raise SystemExit(1)
        payload = models.json()
        ids = [item.get("id") for item in payload.get("data", [])]
        print("gemai_model_count", len(ids))
        print("gemai_candidates", ",".join(str(x) for x in ids[:20]))
        tavily = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": tavily_key, "query": "Python programming language official", "max_results": 1},
        )
        print("tavily_status", tavily.status_code)
        if tavily.status_code != 200:
            print(tavily.text[:500])
            raise SystemExit(1)
        result = tavily.json().get("results", [])
        print("tavily_result_count", len(result))
        if not result:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
