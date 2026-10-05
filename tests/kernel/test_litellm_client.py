"""LiteLLMClient with an injected completion function (no network)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from kernel.config import LLMSection, Settings
from kernel.llm import LiteLLMClient, LLMCallError, extract_json
from kernel.protocols import LLMMessage


class Out(BaseModel):
    name: str
    n: int


def resp(content: str, tokens: int = 10) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=tokens, completion_tokens=tokens, total_tokens=2 * tokens),
    )


class Scripted:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return resp(self.contents.pop(0))


MSGS = [LLMMessage(role="user", content="hi")]


def test_extract_json() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json('Sure! {"a": {"b": 2}} done') == '{"a": {"b": 2}}'
    assert extract_json("plain") == "plain"


async def test_plain_chat_and_gateway_kwargs() -> None:
    fn = Scripted("hello")
    s = Settings(llm=LLMSection(gateway_url="http://gw:4000", api_key=SecretStr("sk-x")))
    client = LiteLLMClient(s, completion_fn=fn, cost_fn=lambda r: 0.01)
    out = await client.achat(MSGS, "gpt-x", max_tokens=5)
    assert out.content == "hello" and out.cost_usd == 0.01 and out.usage["total_tokens"] == 20
    call = fn.calls[0]
    assert call["api_base"] == "http://gw:4000" and call["api_key"] == "sk-x" and call["max_tokens"] == 5
    assert call["messages"] == [{"role": "user", "content": "hi"}]


async def test_direct_provider_has_no_api_base() -> None:
    fn = Scripted("x")
    await LiteLLMClient(Settings(), completion_fn=fn, cost_fn=lambda r: 0).achat(MSGS, "m")
    assert "api_base" not in fn.calls[0] and "api_key" not in fn.calls[0]


async def test_structured_with_reask_accumulates_usage() -> None:
    fn = Scripted('{"name": "a"}', '```json\n{"name": "a", "n": 3}\n```')
    client = LiteLLMClient(Settings(), completion_fn=fn, cost_fn=lambda r: 0.5)
    out = await client.achat(MSGS, "some-unknown-model", response_model=Out)
    assert out.parsed == Out(name="a", n=3)
    assert out.cost_usd == 1.0 and out.usage["total_tokens"] == 40
    second = fn.calls[1]["messages"]
    assert second[-1]["role"] == "user" and "invalid" in second[-1]["content"]
    assert "JSON Schema" in fn.calls[0]["messages"][-1]["content"]


async def test_structured_gives_up() -> None:
    s = Settings(llm=LLMSection(max_structured_retries=1))
    client = LiteLLMClient(s, completion_fn=Scripted("no", "still no"), cost_fn=lambda r: 0)
    with pytest.raises(LLMCallError, match="invalid after retries"):
        await client.achat(MSGS, "m", response_model=Out)


async def test_transport_error_wrapped() -> None:
    async def boom(**kw: Any) -> Any:
        raise TimeoutError("slow")

    with pytest.raises(LLMCallError, match="TimeoutError"):
        await LiteLLMClient(Settings(), completion_fn=boom).achat(MSGS, "m")


def test_estimate_tokens_and_real_cost_fallback() -> None:
    client = LiteLLMClient(Settings())
    assert client.estimate_tokens(MSGS, "gpt-4o") > 0
    assert client._cost(resp("x")) == 0.0  # not a real ModelResponse -> 0, never raises
