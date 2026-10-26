import asyncio

from ci_agent import agents
from ci_agent.settings import Settings


def test_explicit_proxy_clients_close_without_global_proxy_mutation(monkeypatch):
    import os
    real_client = agents.httpx.AsyncClient
    created = []
    before = {key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}

    class RecordingClient(real_client):
        def __init__(self, **kwargs):
            assert kwargs["proxy"] == "http://127.0.0.1:7897"
            assert kwargs["trust_env"] is False
            super().__init__(**kwargs)
            created.append(self)

    monkeypatch.setattr(agents.httpx, "AsyncClient", RecordingClient)
    settings = Settings(_env_file=None, gemai_api_key="test-only",
                        model_proxy_url="http://127.0.0.1:7897")
    model = agents.build_model(settings, "deepseek-v4-flash")
    assert model is not None and len(created) == 1
    assert not created[0].is_closed
    asyncio.run(agents.close_model_clients())
    assert created[0].is_closed
    assert before == {key: os.environ.get(key) for key in before}
