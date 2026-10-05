"""
Real litellm calls against a local OpenAI-compatible server that mimics OmniRoute
(``/v1/chat/completions``, ``/v1/embeddings``, ``X-OmniRoute-Response-Cost``).

Checks what actually goes over the wire: model ids verbatim, aliases, extra headers, bearer key,
and that the gateway-reported cost reaches ``LLMResponse.cost_usd`` (budget enforcement).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from pydantic import SecretStr

pytest.importorskip("litellm")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from kernel.config import KnowledgeSection, LLMSection, Settings
from kernel.knowledge.factory import build_embedder_from_settings
from kernel.llm.litellm_client import LiteLLMClient
from kernel.protocols import LLMMessage


class _Handler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []  # noqa: RUF012 - shared test log

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, **body})
        if self.path.endswith("/embeddings"):
            payload: dict[str, Any] = {
                "object": "list",
                "model": body["model"],
                "data": [
                    {"object": "embedding", "index": i, "embedding": [0.1] * 4} for i in range(len(body["input"]))
                ],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            }
        else:
            payload = {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-OmniRoute-Response-Cost", "0.0123000000")
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def omniroute() -> Iterator[str]:
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()


def _settings(url: str) -> Settings:
    return Settings(
        llm=LLMSection(
            gateway_url=url,
            api_key=SecretStr("sk-omni"),
            model_aliases={"*": "auto/coding", "router/classifier": "auto/fast"},
            extra_headers={"x-omniroute-compression": "off", "x-omniroute-no-memory": "true"},
        ),
        knowledge=KnowledgeSection(embedding_model="nebius/Qwen/Qwen3-Embedding-8B", embedding_dim=4),
    )


async def test_chat_through_openai_compatible_gateway(omniroute: str) -> None:
    client = LiteLLMClient(_settings(omniroute))
    msgs = [LLMMessage(role="user", content="hi")]
    out = await client.achat(msgs, "router/coder")
    await client.achat(msgs, "router/classifier")
    await client.achat(msgs, "cc/claude-sonnet-4-6")  # unmapped -> "*"

    assert out.content == "ok" and out.usage["total_tokens"] == 12
    assert out.cost_usd == pytest.approx(0.0123)  # from X-OmniRoute-Response-Cost
    first, second, third = _Handler.requests
    assert first["path"] == "/v1/chat/completions"
    assert [r["model"] for r in (first, second, third)] == ["auto/coding", "auto/fast", "auto/coding"]
    assert first["headers"]["authorization"] == "Bearer sk-omni"
    assert first["headers"]["x-omniroute-compression"] == "off"
    assert first["headers"]["x-omniroute-no-memory"] == "true"


async def test_model_ids_are_verbatim_without_aliases(omniroute: str) -> None:
    s = Settings(llm=LLMSection(gateway_url=omniroute, api_key=SecretStr("k")))
    client = LiteLLMClient(s)
    for model in ("router/coder", "openai/gpt-5.4", "auto/coding"):
        await client.achat([LLMMessage(role="user", content="x")], model)
    assert [r["model"] for r in _Handler.requests] == ["router/coder", "openai/gpt-5.4", "auto/coding"]


async def test_embeddings_through_gateway(omniroute: str) -> None:
    embedder = build_embedder_from_settings(_settings(omniroute))
    vectors = await embedder.embed(["a", "b"])
    assert vectors == [[0.1] * 4, [0.1] * 4]
    req = _Handler.requests[0]
    assert req["path"] == "/v1/embeddings" and req["model"] == "nebius/Qwen/Qwen3-Embedding-8B"
    assert req["headers"]["x-omniroute-compression"] == "off"


def test_omniroute_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOGEN_LLM__GATEWAY_URL", "http://omniroute:20128/v1")
    monkeypatch.setenv("AUTOGEN_LLM__MODEL_ALIASES", '{"*": "auto/coding", "router/classifier": "auto/fast"}')
    monkeypatch.setenv("AUTOGEN_LLM__EXTRA_HEADERS", '{"x-omniroute-compression": "off"}')
    s = Settings()
    assert s.llm.model_aliases == {"*": "auto/coding", "router/classifier": "auto/fast"}
    assert s.llm.extra_headers == {"x-omniroute-compression": "off"}


def test_llm_check_command(omniroute: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from kernel.config import get_settings
    from kernel.main import main

    monkeypatch.setenv("AUTOGEN_LLM__GATEWAY_URL", omniroute)
    monkeypatch.setenv("AUTOGEN_LLM__API_KEY", "sk-omni")
    monkeypatch.setenv("AUTOGEN_LLM__MODEL_ALIASES", '{"*": "auto/coding", "router/classifier": "auto/fast"}')
    monkeypatch.setenv("AUTOGEN_KNOWLEDGE__EMBEDDER", "hashing")
    get_settings.cache_clear()
    try:
        assert main(["llm-check"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "ok    router/planner -> auto/coding" in out and "ok    router/classifier -> auto/fast" in out
    assert "$0.012300" in out and "skip  embedding" in out
    assert {r["model"] for r in _Handler.requests} == {"auto/coding", "auto/fast"}

    monkeypatch.setenv("AUTOGEN_LLM__GATEWAY_URL", "http://127.0.0.1:9/v1")  # nothing listens
    monkeypatch.setenv("AUTOGEN_LLM__REQUEST_TIMEOUT", "5")
    get_settings.cache_clear()
    try:
        assert main(["llm-check"]) == 1
    finally:
        get_settings.cache_clear()
    assert "FAIL  router/planner" in capsys.readouterr().out
