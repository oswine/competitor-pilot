from __future__ import annotations

import httpx
from pydantic import BaseModel, Field, HttpUrl

from .settings import Settings


class SearchResult(BaseModel):
    title: str
    url: HttpUrl
    content: str = ""


class SearchResultSet(BaseModel):
    results: list[SearchResult] = Field(default_factory=list)


class TavilySearch:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def search(self, query: str, max_results: int = 5) -> SearchResultSet:
        if not self.settings.tavily_api_key:
            raise RuntimeError("TAVILY_API_KEY is required for live research")
        payload = {"api_key": self.settings.tavily_api_key, "query": query, "max_results": max_results, "search_depth": "advanced"}
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post("https://api.tavily.com/search", json=payload)
            response.raise_for_status()
            data = response.json()
        return SearchResultSet(results=[SearchResult(title=x.get("title", ""), url=x["url"], content=x.get("content", "")) for x in data.get("results", [])])
