from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    gemai_api_key: str = ""
    gemai_base_url: str = "https://api.gemai.cc/v1"
    gemai_fast_model: str = "[满血A]gemini-2.5-flash"
    gemai_research_model: str = "deepseek-v4-flash"
    gemai_synthesis_model: str = "deepseek-v4-flash"
    gemai_judge_model: str = "claude-sonnet-5"
    tavily_api_key: str = ""
    database_url: str = "postgresql+psycopg://ci@127.0.0.1:5432/ci_agent"
    object_store_root: Path = Path("data/objects")
    postgres_enabled: bool = True
    minio_endpoint: str = "http://127.0.0.1:9000"
    minio_access_key: str = "ci-minio"
    minio_secret_key: str = "ci-minio-secret"
    minio_bucket: str = "ci-artifacts"
    otel_enabled: bool = True
    tenant_default: str = "default"
    api_key_grace_seconds: int = 86400
    service_api_key: str = ""
    allowed_domains: str = "*"
    max_searches: int = 12
    max_pages: int = 30
    max_model_requests: int = 24
    max_agent_calls: int = 4
    max_cost_cny: float = 3.0
    browser_pool_size: int = 4
    domain_rate_limit_seconds: float = 0.5
    browser_cache_ttl_seconds: int = 300
    robots_cache_ttl_seconds: int = 3600
    robots_fail_closed: bool = False
    run_timeout_seconds: int = 900
    research_context_token_budget: int = 10_000
    evidence_card_token_budget: int = 600
    research_context_max_cards: int = 16
    # Longer than the default end-to-end timeout, so a slow model call cannot
    # allow a second worker to enter the same run concurrently.
    run_checkpoint_lease_seconds: int = 960
    knowledge_ttl_seconds: int = 86_400

    @property
    def domain_allowlist(self) -> set[str] | None:
        if self.allowed_domains.strip() == "*":
            return None
        return {d.strip().lower() for d in self.allowed_domains.split(",") if d.strip()}


settings = Settings()
settings.object_store_root.mkdir(parents=True, exist_ok=True)
