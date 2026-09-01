from prometheus_client import Counter, Histogram, start_http_server

RUNS = Counter("ci_agent_runs_total", "Research runs", ["tenant", "status"])
PAGES = Counter("ci_agent_pages_total", "Collected pages", ["content_type", "result"])
MODEL_REQUESTS = Counter("ci_agent_model_requests_total", "Model requests", ["role", "model"])
RUN_SECONDS = Histogram("ci_agent_run_seconds", "Run duration")


def start_metrics_server(port: int = 9108) -> None:
    start_http_server(port)
