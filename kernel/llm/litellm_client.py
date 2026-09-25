"""
LLM client on top of LiteLLM (port of ``specs/02_infra/llm_gateway/LLM_CLIENT.py``).

Differences from the spec (``specs/ISSUES.md`` I-04):

* Structured output is implemented directly (JSON schema in the prompt +
  ``response_format`` where the model supports it + pydantic validation with
  re-ask on error) instead of Instructor. The spec relied on the private
  ``_raw_response`` attribute to get usage/cost, which Instructor does not
  guarantee; here every attempt's usage and cost are accounted for.
* Settings are injected (``kernel.config.Settings``), no global ``settings``.
* ``litellm`` / ``tiktoken`` are imported lazily (optional extra ``llm``).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, ValidationError

from ..config import Settings, get_settings
from ..protocols import LLMMessage, LLMResponse

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class LLMCallError(RuntimeError):
    pass


def extract_json(text: str) -> str:
    """Strip markdown fences / leading prose around a JSON object."""
    m = _FENCE_RE.match(text)
    if m:
        return m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def schema_instruction(model: type[BaseModel]) -> str:
    schema = json.dumps(model.model_json_schema(), ensure_ascii=False)
    return (
        "Respond ONLY with a single JSON object that validates against this JSON Schema. "
        f"No markdown, no commentary.\nJSON Schema:\n{schema}"
    )


def _usage_dict(resp: Any) -> dict[str, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {}
    get = usage.get if isinstance(usage, dict) else lambda k, d=0: getattr(usage, k, d)
    return {
        "prompt_tokens": int(get("prompt_tokens", 0) or 0),
        "completion_tokens": int(get("completion_tokens", 0) or 0),
        "total_tokens": int(get("total_tokens", 0) or 0),
    }


def _merge_usage(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {k: a.get(k, 0) + b.get(k, 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}


# Per-call cost reported by the gateway (LiteLLM proxy / OmniRoute); litellm exposes response headers
# in ``_hidden_params["additional_headers"]`` (raw and ``llm_provider-`` prefixed).
COST_HEADERS = ("x-omniroute-response-cost", "x-litellm-response-cost")


def gateway_model(model: str) -> str:
    """litellm model for an OpenAI-compatible gateway: ``openai/`` + the gateway's model id, passed verbatim."""
    return f"openai/{model}"


def resolve_alias(model: str, aliases: dict[str, str]) -> str:
    return aliases.get(model) or aliases.get("*") or model


def header_cost(resp: Any) -> float | None:
    hidden = getattr(resp, "_hidden_params", None) or {}
    headers = hidden.get("additional_headers") or {}
    lowered = {str(k).lower(): v for k, v in headers.items()}
    for name in COST_HEADERS:
        for key in (name, f"llm_provider-{name}"):
            if lowered.get(key) not in (None, ""):
                try:
                    return float(lowered[key])
                except (TypeError, ValueError):
                    continue
    return None


class LiteLLMClient:
    """``ILLMClient`` implementation. Works with a LiteLLM proxy (``llm.gateway_url``) or direct providers."""

    def __init__(
        self, settings: Settings | None = None, *, completion_fn: Any | None = None, cost_fn: Any | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self._completion_fn = completion_fn  # injectable (tests, custom transports)
        self._cost_fn = cost_fn

    # -- internals -----------------------------------------------------------------
    def _litellm(self) -> Any:
        try:
            import litellm
        except ImportError as exc:  # pragma: no cover - optional extra
            raise LLMCallError("LiteLLMClient requires `pip install .[llm]`") from exc
        return litellm

    async def _complete(self, **kwargs: Any) -> Any:
        s = self.settings.llm
        model = resolve_alias(str(kwargs["model"]), s.model_aliases)
        if s.gateway_url:
            kwargs.setdefault("api_base", s.gateway_url)
            model = gateway_model(model)
        kwargs["model"] = model
        if s.extra_headers:
            kwargs["extra_headers"] = {**s.extra_headers, **(kwargs.get("extra_headers") or {})}
        if s.api_key is not None:
            kwargs.setdefault("api_key", s.api_key.get_secret_value())
        kwargs.setdefault("timeout", s.request_timeout)
        fn = self._completion_fn or self._litellm().acompletion
        return await fn(**kwargs)

    def _cost(self, resp: Any) -> float:
        if self._cost_fn is not None:
            return float(self._cost_fn(resp))
        reported = header_cost(resp)
        if reported is not None:
            return reported
        try:
            return float(self._litellm().completion_cost(completion_response=resp) or 0.0)
        except Exception:  # unknown model pricing (e.g. gateway aliases)
            return 0.0

    def _supports_schema(self, model: str) -> bool:
        try:
            return bool(self._litellm().supports_response_schema(model=model))
        except Exception:
            return False

    # -- ILLMClient ----------------------------------------------------------------
    async def achat(
        self,
        messages: list[LLMMessage],
        model: str,
        response_model: type[BaseModel] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        llm_messages: list[dict[str, Any]] = [m.model_dump(exclude_none=True) for m in messages]
        call: dict[str, Any] = {"model": model, "temperature": temperature, **kwargs}
        if max_tokens is not None:
            call["max_tokens"] = max_tokens

        if response_model is None:
            resp = await self._call(llm_messages, call)
            msg = resp.choices[0].message
            return LLMResponse(
                content=msg.content or "",
                tool_calls=[tc.model_dump() if hasattr(tc, "model_dump") else tc for tc in (msg.tool_calls or [])]
                or None,
                usage=_usage_dict(resp),
                model=model,
                cost_usd=self._cost(resp),
            )

        # Structured output with validation + re-ask
        llm_messages = [*llm_messages, {"role": "system", "content": schema_instruction(response_model)}]
        if self._supports_schema(model):
            call["response_format"] = response_model
        usage: dict[str, int] = {}
        cost = 0.0
        last_error = ""
        content = ""
        for _attempt in range(1 + max(0, self.settings.llm.max_structured_retries)):
            resp = await self._call(llm_messages, call)
            usage = _merge_usage(usage, _usage_dict(resp))
            cost += self._cost(resp)
            content = resp.choices[0].message.content or ""
            try:
                parsed = response_model.model_validate_json(extract_json(content))
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)[:2000]
                llm_messages = [
                    *llm_messages,
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"Your JSON was invalid:\n{last_error}\nReturn corrected JSON only."},
                ]
                continue
            return LLMResponse(content=content, usage=usage, model=model, cost_usd=cost, parsed=parsed)
        raise LLMCallError(f"Structured output for {response_model.__name__} invalid after retries: {last_error}")

    async def _call(self, llm_messages: list[dict[str, Any]], call: dict[str, Any]) -> Any:
        try:
            return await self._complete(messages=llm_messages, **call)
        except LLMCallError:
            raise
        except Exception as exc:
            raise LLMCallError(f"LLM call failed ({call.get('model')}): {type(exc).__name__}: {exc}") from exc

    async def astream_chat(self, messages: list[LLMMessage], model: str, **kwargs: Any) -> AsyncIterator[str]:
        llm_messages = [m.model_dump(exclude_none=True) for m in messages]
        stream = await self._complete(model=model, messages=llm_messages, stream=True, **kwargs)
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                yield delta.content

    def estimate_tokens(self, messages: list[LLMMessage], model: str) -> int:
        try:
            import tiktoken

            try:
                enc = tiktoken.encoding_for_model(model.split("/")[-1])
            except KeyError:
                enc = tiktoken.get_encoding("cl100k_base")
            return sum(len(enc.encode(m.content)) + 4 for m in messages)
        except Exception:  # tiktoken missing or offline: ~4 chars per token
            return sum(len(m.content) // 4 + 4 for m in messages)
